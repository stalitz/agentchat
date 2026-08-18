import asyncio
import json
import re
from pathlib import Path

from . import store
from .executor import AgentRuntime
from .models import AgentConfig

# MCP 共享状态文件：end_discussion 工具（backend/terminate_signal_mcp.py）读取它做
# 最小轮数 gate 与两阶段确认判定；编排层每轮启动 claude 前写入（app 单会话串行，无竞态）。
TERMINATE_STATE_FILE = "/tmp/agentchat_terminate_state.json"


def _clean_answer(text: str) -> str:
    """清洗 agent 答案文本：剥离 gemma 等推理模型在 reasoning/answer 之间输出的
    '---' 分隔符噪声（Ollama 常把 --- 作为思考与答案的分界，原样落库会被前端显示成一条横线）。"""
    t = (text or "").strip()
    t = re.sub(r"^\s*---\s*", "", t)
    t = re.sub(r"\s*---\s*$", "", t).strip()
    return t


# claude-code 外壳把 API 失败 / resume 异常渲染为普通文本而非结构化错误（实测出现过的）：
# 这类"回答"不是有效发言，须按执行失败处理——否则会被落盘成正常内容、再被当议题喂给下一个 agent。
_CLAUDE_ERROR_PREFIXES = ("API Error:", "No response requested.", "Request Failed:")


def _looks_like_claude_error(text: str) -> bool:
    t = (text or "").strip()
    return any(t.startswith(p) for p in _CLAUDE_ERROR_PREFIXES)


class Session:
    """一场会话的编排器。

    模式由 Team 内 agent 数量自动决定，用户不需要手动切换：

    - **chat（单 agent）**：人机对话模式。一次 `start(text)` = 「用户发一条 + agent 回一条」，
      用户消息同样入库并回显；不存在轮次、收敛、暂停、介入等辩论概念。
    - **debate（≥2 agent）**：多 agent 轮流辩论，受 `max_rounds` / `TERMINATE` / 手动停止三种终止条件约束。
    """

    def __init__(self, project_path: str, session_id: str, agents: list[AgentConfig],
                 max_rounds: int, broadcast, timeout: int = 300):
        self.project_path = project_path
        self.session_id = session_id
        self.agents = agents
        self.max_rounds = max_rounds
        self.timeout = timeout
        self.broadcast = broadcast  # async fn(event: dict)
        sess = store.read_session(project_path, session_id)
        self.history = sess.get("messages", []) if sess else []
        # 每个 agent 的「持续 CLI 会话」id，用于 1:1 续接；从磁盘读回（历史会话恢复用）
        self.agent_sessions = (sess or {}).get("agent_sessions", {})
        self.runtimes = {a.name: AgentRuntime(a, self.agent_sessions.get(a.name)) for a in agents}
        self.single = len(agents) < 2
        self.mode = "chat" if self.single else "debate"
        self.stop_event = asyncio.Event()
        # resume_event 默认 set = 未暂停；pause() 清除后循环阻塞在 wait()。
        # 历史 bug：曾用 pause_event.set() + await pause_event.wait()，
        # 而 asyncio.Event.wait() 在事件已 set 时立即返回 —— 暂停形同虚设。
        self.resume_event = asyncio.Event()
        self.resume_event.set()
        self.inject_queue: asyncio.Queue = asyncio.Queue()
        self.running = False
        # end_discussion 两阶段终止状态：提议方 / 理由 / 是否已获对方确认（确认后整场收敛）
        self.terminate_proposed_by = None
        self.terminate_reason = ""
        self.terminate_confirm = False

    async def _emit(self, **kw):
        await self.broadcast(kw)

    def stop(self):
        self.stop_event.set()
        self.resume_event.set()  # 释放可能的暂停等待

    def pause(self):
        self.resume_event.clear()

    def resume(self):
        self.resume_event.set()

    @property
    def paused(self) -> bool:
        return not self.resume_event.is_set()

    async def inject(self, text: str):
        await self.inject_queue.put(text)

    # ---------------- prompt ----------------
    def _build_prompt(self, agent: AgentConfig, inject: str | None, round_i: int = 0) -> str:
        # 状态式 agent：每个 agent 在自己的 CLI 会话里持有完整历史（含历次议题与结论）。
        # 编排器每轮只发「增量 delta」——对手最近一句 + 本轮任务 / 介入，token 极省，
        # 不再重新搬运整段双方对话 / 累积记忆 / 上轮结论（那是无状态 agent 时代的补丁，现已多余）。
        parts = []
        if agent.role:
            # 极便宜的身份锚定：避免 resume 后续接时角色设定漂移（一行，远小于重搬历史）
            parts.append(f"你是：{agent.role}。")

        if self.mode == "chat":
            # 人机对话：只把用户本轮新消息作为 delta 发给 agent（其 CLI 会话已含完整历史）
            if inject:
                parts.append(inject)
            else:
                parts.append("（请基于我们之前的对话继续）")
            parts.append("请以对话方式直接回复。这是人机对话，不要调用 end_discussion 工具（辩论模式专用）。")
            return "\n\n".join(parts)

        # 多 agent 辩论：只把「对手上一句」作为 delta 回传；首轮（round_i==0）没有对手句，
        # 也不回传上一场残骸（续开新议题时 history 里混着旧议题的 agent 发言）。
        # agent 的持续会话已记住历次议题与结论，无需手动回传——它自行结合上下文判断。
        last = None
        if round_i > 0:
            for m in reversed(self.history):
                if m.get("speaker") not in ("system", agent.name, "human"):
                    last = m
                    break
        if last:
            parts.append(
                f"{last['speaker']}（{last.get('role', '')}）刚才说：\n{last['content']}"
            )
        # 任务 / 人工介入（本轮新议题通常经此传入）
        if inject:
            parts.append(f"人工介入 / 任务：\n{inject}")
        parts.append(
            "请基于以上继续推进。\n"
            "【共识终止：只能通过调用 end_discussion 工具表达】\n"
            "如果你想结束本场讨论，**唯一的表达方式**是调用 `end_discussion` 工具（reason 参数用一两句话"
            "凝练双方达成的一致结论）。**禁止**用普通文字表达终止意图——例如「我同意结束」「可以收尾了」"
            "「我认为已达成一致」等文字不会被当作终止信号，对方收不到你的结束意图，讨论会继续。\n"
            "调用前提（必须**全部**满足才可调用）：\n"
            "  1) 你已认真阅读并理解上一位 agent 的观点，而非自说自话、各说各话；\n"
            "  2) 你真正接受（认同或采纳）上一位 agent 的结论，而不是为了早点结束而草草收尾；\n"
            "  3) 本场至少要进行两轮完整往返才会考虑结束，不要在一两轮内就急于终止。\n"
            "调用后还需对方 agent 也调用 end_discussion 确认才会真正终止——若本轮只是你提议，对方可能继续讨论，"
            "请继续正常参与；只要你仍有不同意见、想补充或反驳，就不要调用它，继续正常讨论。"
        )
        return "\n\n".join(parts)

    def _write_terminate_state(self, agent: AgentConfig, round_i: int):
        """把本轮终止状态写入 MCP 共享文件（terminate-signal 服务读取，做 gate 判定）。

        每轮启动 claude 之前调用；提议状态（proposed_by/reason）由 _run_turn 状态机更新后
        立即重写，供下一轮对方 claude 进程的 MCP 实例读取。
        """
        try:
            Path(TERMINATE_STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
            Path(TERMINATE_STATE_FILE).write_text(json.dumps({
                "round_i": round_i,
                "agent_count": len(self.agents),
                "current_agent": agent.name,
                "proposed_by": self.terminate_proposed_by or "",
                "reason": self.terminate_reason or "",
            }, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # ---------------- 单轮执行（两种模式共用） ----------------
    async def _record_human(self, text: str, role: str):
        entry = {"round": len(self.history), "speaker": "human", "role": role, "content": text}
        self.history.append(entry)
        store.append_message(self.project_path, self.session_id, entry)
        await self._emit(type="human", role=role, text=text)

    async def _record_system(self, msg: str):
        """落库并广播一条系统消息（如终止原因 / 会话结束）。

        普通过程提示（会话开始、暂停、继续）仍用 _emit 只广播不落库，
        避免污染「首条 human 即任务」语义与首轮判定；
        但终止 / 会话结束这类结论性系统消息必须持久化，
        否则辩论模式 run_end 重载后会被冲掉、刷新即丢失。
        """
        entry = {"round": len(self.history), "speaker": "system", "role": "系统", "content": msg}
        self.history.append(entry)
        store.append_message(self.project_path, self.session_id, entry)
        await self._emit(type="system", msg=msg)

    def _format_conclusion(self) -> str:
        """从历史中取最近一条 agent 发言（即达成一致的收敛发言），去掉 TERMINATE 行，作为格式化结论。"""
        for m in reversed(self.history):
            if m.get("speaker") not in ("system", "human"):
                text = "\n".join(l for l in m["content"].splitlines() if l.strip() != "TERMINATE")
                return text.strip()
        return ""

    async def _run_turn(self, agent: AgentConfig, round_i: int, inject: str | None) -> tuple[str, list[str]]:
        """执行单轮，返回 (full, errors)。

        errors 非空 = 本轮执行失败（agent 没有产出有效回答）：
        - 不把任何内容当作 agent 发言落盘（错误文本/空响应会被误读为正常回答，
          并作为「对手发言」喂给下一个 agent，导致两个 agent 基于报错信息继续讨论——历史 bug）；
        - 落一条 system 错误消息说明原因，调用方（_debate/_chat_turn）据此中止。
        """
        # 首轮（round_i==0 且 debate 模式）标记 first_turn 仅用于记录/排查；
        # 共识判定交由提示词（见 _build_prompt）引导，不再下发明示「不可讨论就 TERMINATE」指令。
        first_turn = round_i == 0 and not self.single
        prompt = self._build_prompt(agent, inject, round_i)
        # 记录本轮完整提示词（含首轮判定指令），便于排查与查看
        try:
            store.append_prompt_log(self.project_path, self.session_id, {
                "round": round_i, "agent": agent.name, "role": agent.role or "",
                "first_turn": first_turn, "prompt": prompt,
            })
        except Exception:
            pass
        await self._emit(type="turn_start", agent=agent.name, role=agent.role, atype=agent.type)
        runtime = self.runtimes[agent.name]
        # 写 MCP 共享状态（本轮轮次/调用者/提议状态）——claude 启动后其 MCP 实例读它做判定
        self._write_terminate_state(agent, round_i)
        collected = []
        thinking_parts = []
        errors: list[str] = []
        tool_uses: list[dict] = []
        async for kind, text in runtime.run(
            prompt, self.project_path, self.timeout,
            meta={"round_index": round_i, "max_rounds": self.max_rounds},
        ):
            if kind == "text":
                collected.append(text)
                await self._emit(type="token", agent=agent.name, text=text)
            elif kind == "thinking":
                # 思考块实时单独渲染，并随本轮答案一起落盘（刷新后仍可见）
                thinking_parts.append(text)
                await self._emit(type="thinking", agent=agent.name, text=text)
            elif kind == "error":
                errors.append(text)
                await self._emit(type="error", agent=agent.name, text=text)
            elif kind == "tool_use":
                tool_uses.append(text)
            # done: 忽略
        full = "".join(collected).strip()
        # 剥离 gemma 等推理模型在 reasoning/answer 之间输出的 '---' 分隔符噪声
        # （Ollama 常把 --- 作为思考与答案的分界，原样落库会被前端显示成一条横线）
        full = _clean_answer(full)
        # claude 外壳把 API 失败/resume 异常渲染成普通文本（如 API Error: ... / No response requested.），
        # 这类"回答"不是有效发言，必须按失败处理，否则会被当议题继续讨论。
        if full and _looks_like_claude_error(full):
            errors.append(full)
        # 持久化首次创建的 CLI 会话 id（resume 后续不变）——供点历史会话时恢复对应 agent 会话
        # （无论成败 claude 进程都已启动，失败轮也要记录，避免下次重开 CLI 会话）
        if runtime.cli_session_id and self.agent_sessions.get(agent.name) != runtime.cli_session_id:
            self.agent_sessions[agent.name] = runtime.cli_session_id
            store.set_agent_session_id(self.project_path, self.session_id, agent.name, runtime.cli_session_id)
        await self._emit(type="turn_end", agent=agent.name)
        if errors:
            # 失败：不落 agent 发言，落 system 错误消息，调用方据此中止
            await self._record_system(f"{agent.name} 执行出错，会话中止：{errors[0][:200]}")
            return "", errors
        # end_discussion 两阶段终止状态机（与 MCP server 判定配合）：
        # 未达最小轮数时 MCP 已返回 isError 拒绝（这里不记录）；达标后：无提议→记提议（写状态文件
        # 供下一轮对方读取）；对方已提议→确认达成（terminate_confirm 由 _debate 据此收敛）。
        floor = 2 * len(self.agents) - 1
        for tu in tool_uses:
            if not isinstance(tu, dict) or tu.get("name") != "end_discussion" or round_i < floor:
                continue
            reason = (tu.get("input") or {}).get("reason", "") or ""
            if self.terminate_proposed_by is None:
                self.terminate_proposed_by = agent.name
                self.terminate_reason = reason
                self._write_terminate_state(agent, round_i)  # 立即落盘，下一轮对方 MCP 可读到
            elif self.terminate_proposed_by != agent.name:
                self.terminate_confirm = True
                self.terminate_reason = reason or self.terminate_reason
        thinking_text = "\n\n".join(thinking_parts).strip()
        # 思考随答案一起落盘（刷新后仍可见）；正文只存 answer，不拿 thinking 回填，
        # 避免历史/实时把整段思考重复渲染到结论气泡。无答案时正文为空，前端显示提示。
        entry = {
            "round": round_i, "speaker": agent.name, "role": agent.role,
            "content": full,
            "thinking": thinking_text,
        }
        self.history.append(entry)
        store.append_message(self.project_path, self.session_id, entry)
        return full, errors

    # ---------------- 入口 ----------------
    async def start(self, initial_task: str):
        self.running = True
        try:
            if self.mode == "chat":
                await self._chat_turn(initial_task)
            else:
                await self._debate(initial_task)
        finally:
            self.running = False

    async def _chat_turn(self, text: str):
        """单 agent 对话模式：一次调用 = 用户一条 + agent 一条。
        用户本轮消息作为 delta 发给 agent（其 CLI 会话已持有完整历史）。"""
        # 模式提示只在会话开头播一次，否则每发一条都刷一行系统气泡
        if not self.history:
            await self._emit(
                type="system",
                msg="单 agent 模式 · 对话模式：直接输入消息发送，agent 逐条回复（轮次 / 介入 / 暂停不适用）",
            )
        text = (text or "").strip()
        if text:
            await self._record_human(text, "用户")
        # 失败时 _run_turn 已落 system 错误消息并广播 error；单 agent 本轮即结束，无需额外处理
        await self._run_turn(self.agents[0], len(self.history), text or None)
        await self._emit(type="run_end", mode="chat")

    async def _debate(self, initial_task: str):
        await self._emit(
            type="system",
            msg=f"会话开始 · agent 数={len(self.agents)} · 上限 {self.max_rounds} 轮",
        )
        if (initial_task or "").strip():
            await self._record_human(initial_task.strip(), "任务")
        round_i = 0
        pending_inject = initial_task
        # max_rounds 语义 = 「每个 agent 发言次数」（N 轮 = 每个 agent 发 N 次 = N×agent数 次发言）。
        # 故循环上限为 max_rounds * agent数；TERMINATE 收敛门槛沿用「至少两轮完整往返」(见下)。
        while round_i < self.max_rounds * len(self.agents) and not self.stop_event.is_set():
            if self.paused:
                await self._emit(type="system", msg="已暂停")
                await self.resume_event.wait()
                if self.stop_event.is_set():
                    break
                await self._emit(type="system", msg="已继续")

            current = self.agents[round_i % len(self.agents)]
            full, errors = await self._run_turn(current, round_i, pending_inject)
            pending_inject = None

            # agent 执行失败（API 错误 / 空响应 / resume 异常）：不把报错信息当议题继续讨论，
            # 直接中止（_run_turn 已落 system 错误消息说明原因）。
            if errors:
                break

            # end_discussion 两阶段确认收敛：对方已确认（terminate_confirm）→ 结束整场，
            # reason 作共识结论（若确认方未给 reason 用提议方的）。
            if self.terminate_confirm:
                conclusion = self.terminate_reason or self._format_conclusion()
                store.set_discussion_conclusion(self.project_path, self.session_id, conclusion)
                await self._record_system(f"双方通过 end_discussion 达成一致，会话收敛。结论：{conclusion}")
                break

            # 共识终止（旧机制，保留为兜底）：至少完成两轮完整往返（每个 agent 都发过两次言），
            # 否则把 TERMINATE 当普通文本忽略，继续讨论——
            # 避免第二个 agent 在第一轮就 TERMINATE 腰斩、首轮 agent 没机会再发言。
            if any(line.strip() == "TERMINATE" for line in full.splitlines()) \
                    and round_i >= 2 * len(self.agents) - 1:
                conclusion = self._format_conclusion()
                store.set_discussion_conclusion(self.project_path, self.session_id, conclusion)
                await self._record_system(f"{current.name} 标记 TERMINATE，会话收敛。结论：{conclusion}")
                break

            if not self.inject_queue.empty():
                human = await self.inject_queue.get()
                await self._record_human(human, "人工介入")
                pending_inject = human

            round_i += 1

        await self._record_system("会话结束")
        await self._emit(type="run_end", mode="debate")
