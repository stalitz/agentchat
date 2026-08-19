import asyncio
import json
import re

from . import store
from .executor import AgentRuntime
from .models import AgentConfig

# 讨论调性（tone）：发起讨论时可选择一个风格，注入每轮 prompt，整场保持一致。
# key 为前端下拉显示的调名，value 为注入 prompt 的风格描述（模型按此发言）。
TONE_STYLES: dict[str, str] = {
    "温和": "礼貌克制、委婉表达不同意见、优先寻找共识，避免尖锐对抗",
    "友好": "轻松亲和、强调建设性与合作，气氛融洽",
    "中立": "不预设立场，只摆事实、讲逻辑，克制主观情绪",
    "批判": "主动审视对方观点中的漏洞，挑战前提与假设，明确指出问题",
    "尖锐": "直接点破问题与矛盾，不留情面但就事论事",
    "激进": "强势推进己方立场，主动压制反方观点，措辞具有进攻性",
    "唱反调": "刻意寻找反例与反对理由，即使基本同意也要检验对立面",
    "严谨": "每步结论都要有证据与推理链支撑，避免跳跃与想当然",
    "审慎": "保守小心，反复验证，不轻易下结论，宁可留有余地",
    "怀疑": "对任何结论先假设其可能错误，要求对方证明",
    "务实": "只关心能否落地执行，拒绝空谈与过度理论化",
    "数据驱动": "一切观点用数据、事实与案例支撑，少用主观判断",
    "学术": "术语严谨、引经据典、论证结构完整，像学术论文",
    "简洁": "点到为止、惜字如金，直接给结论与关键理由",
    "详尽": "全面展开论证，覆盖所有角度，不遗漏细节",
    "幽默": "轻松诙谐，善用类比与玩笑化解对立，但不失深度",
}


def _is_end_discussion(name) -> bool:
    """判定是否为 end_discussion 调用——兼容干净名（end_discussion）与
    MCP 原名（mcp__terminate-signal__end_discussion，经网关转发/还原后出现）。"""
    return isinstance(name, str) and name.endswith("end_discussion")


def _now_str() -> str:
    """本地时间，与 store._now 同格式（工具日志 ts 用）。"""
    import time as _t
    return _t.strftime("%Y-%m-%d %H:%M:%S")


def _extract_text_tool_call(full: str) -> dict | None:
    """兜底解析：模型未拿到工具 schema 时（如 deepseek 走云端直连、不经本地网关），
    会把 end_discussion 调用写成 prompt 指令约定的文本 json：

        ```json
        {"tool": "end_discussion", "reason": "..."}
        ```

    编排层解析后并入 tool_uses，让终止状态机对两种表达（真 tool_use 块 / 文本 json）
    行为一致——否则 deepseek 的 json 不被识别、终止永不收敛、只能靠轮次上限强停。
    返回 {"name": "end_discussion", "input": {"reason": ...}} 或 None。
    """
    if not full:
        return None
    for m in re.finditer(r"```(?:json)?\s*\n?(\{[\s\S]*?\})\s*```", full):
        try:
            data = json.loads(m.group(1))
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and data.get("tool") == "end_discussion":
            reason = data.get("reason", "")
            if not isinstance(reason, str):
                reason = str(reason) if reason is not None else ""
            return {"name": "end_discussion", "input": {"reason": reason}}
    return None


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
                 max_rounds: int, broadcast, timeout: int = 300, tone: str = ""):
        self.project_path = project_path
        self.session_id = session_id
        self.agents = agents
        self.max_rounds = max_rounds
        self.timeout = timeout
        self.broadcast = broadcast  # async fn(event: dict)
        sess = store.read_session(project_path, session_id)
        self.history = sess.get("messages", []) if sess else []
        # 讨论调性：发起时传入优先；未传则沿用会话已存（历史会话续接保持同调）
        self.tone = tone or ((sess or {}).get("tone") or "")
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
        # 所有事件带 sid（会话 id）：多会话并发时前端据此区分事件归属，
        # 只渲染当前选中会话的流式内容、按会话更新按钮状态。
        kw.setdefault("sid", self.session_id)
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
        # 讨论调性：整场统一、每轮都注入，保证风格一致（chat / debate 均生效）
        if self.tone and self.tone in TONE_STYLES:
            parts.append(
                f"本场讨论风格：{TONE_STYLES[self.tone]}——"
                "请始终保持此风格发言，不要中途改变。"
            )

        if self.mode == "chat":
            # 人机对话：只把用户本轮新消息作为 delta 发给 agent（其 CLI 会话已含完整历史）
            if inject:
                parts.append(inject)
            else:
                parts.append("（请基于我们之前的对话继续）")
            parts.append("请以对话方式直接回复。这是人机对话，不要调用 end_discussion 工具或输出其调用文本（辩论模式专用）。")
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
            "【共识终止：唯一表达方式是输出 end_discussion 调用文本】\n"
            "如果你想结束本场讨论，**唯一的表达方式**是发起 end_discussion 调用"
            "（reason 参数用一两句话凝练双方达成的一致结论）：\n"
            "- **首选：直接调用 end_discussion 工具**（你所在环境已注册该工具，可直接调用）；\n"
            "- **若你的环境没有该工具可调**，则输出以下格式的代码块（与调用等价）：\n"
            '```json\n{"tool": "end_discussion", "reason": "双方达成的一致结论（一两句话）"}\n```\n'
            "**禁止**用普通文字表达终止意图——例如「我同意结束」「可以收尾了」"
            "「我认为已达成一致」等文字不会被当作终止信号，对方收不到你的结束意图，讨论会继续。\n"
            "调用前提（必须**全部**满足才可调用）：\n"
            "  1) 你已认真阅读并理解上一位 agent 的观点，而非自说自话、各说各话；\n"
            "  2) 你真正接受（认同或采纳）上一位 agent 的结论，而不是为了早点结束而草草收尾；\n"
            "  3) 本场至少要进行两轮完整往返才会考虑结束，不要在一两轮内就急于终止。\n"
            "调用后还需对方 agent 也发起 end_discussion 确认才会真正终止——若本轮只是你提议，对方可能继续讨论，"
            "请继续正常参与；只要你仍有不同意见、想补充或反驳，就不要调用它，继续正常讨论。"
        )
        # 达到最少轮数（双方各发言 2 次）后的每轮硬提醒：推着模型主动决策收尾，而非等它想起。
        # 本地弱工具调用模型（gemma/r1）在长辩论中几乎不主动调用工具（实测跑满轮次），
        # 故 floor 后每轮明确告知轮次与可选动作，并把「对方已提议」显式喂给本方推动确认。
        floor = 2 * len(self.agents) - 1
        if round_i >= floor:
            if self.terminate_proposed_by and self.terminate_proposed_by != agent.name:
                parts.append(
                    f"【对方已提议终止】{self.terminate_proposed_by} 已发起 end_discussion 提议终止"
                    f"（理由：{self.terminate_reason or '未说明'}）。"
                    "若你认同该结论，请**发起 end_discussion 确认**（reason 填你的确认意见；"
                    "可直接调用工具，无工具则输出 ```json 调用文本），"
                    "双方确认后讨论即终止；若仍有异议，继续正常讨论即可。"
                )
            elif self.terminate_proposed_by == agent.name:
                parts.append(
                    "【你已提议终止】你已发起 end_discussion 提议终止，正在等待对方确认。"
                    "请继续正常参与讨论；若对方也发起 end_discussion 确认，讨论即终止。"
                )
            else:
                parts.append(
                    f"【轮次提醒】本场已讨论到第 {round_i + 1} 轮（达到最少轮数，可以收尾）。"
                    "若你认为双方已达一致，**请发起 end_discussion 终止**（reason 填共识结论；"
                    "可直接调用工具，无工具则输出 ```json 调用文本："
                    '```json\n{"tool": "end_discussion", "reason": "..."}\n```）；'
                    "普通文字总结不会被当作终止信号，讨论会继续。若仍有分歧，继续讨论。"
                )
        return "\n\n".join(parts)

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
        collected = []
        thinking_parts = []
        errors: list[str] = []
        tool_uses: list[dict] = []
        tool_use_ts: str | None = None      # 首个 end_discussion tool_use 到达时刻（≈调用时刻）
        tool_use_errors: list[str] = []     # 工具执行失败文本（「有调用意图」≠「调用成功」）
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
                # 工具执行失败（如 claude 对注入工具报「No such tool available」）单独收集，
                # 写工具日志时据此标 exec_ok=false，避免「意图已记录」掩盖「调用失败」的事实
                if "tool" in text.lower():
                    tool_use_errors.append(text)
            elif kind == "tool_use":
                tool_uses.append(text)
                # 记录首个 end_discussion 调用的到达时刻（≈模型发出调用时刻）：
                # 日志 ts 应为「调用时间」，而非「整轮跑完的写入时间」（后者可能晚数分钟）
                if tool_use_ts is None and isinstance(text, dict) and _is_end_discussion(text.get("name")):
                    tool_use_ts = _now_str()
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
        # 兜底：模型未拿到工具 schema（deepseek 走云端直连）时会把 end_discussion 调用
        # 写成文本 json（prompt 指令约定）。解析后并入 tool_uses，与真 tool_use 块等价，
        # 否则 deepseek 的 json 不被识别、终止状态机永不收敛、只能靠轮次上限强停。
        text_tool = _extract_text_tool_call(full)
        if text_tool:
            if tool_use_ts is None:
                tool_use_ts = _now_str()   # 文本表达无独立时间戳，以解析时刻近似
            tool_uses.append(text_tool)
        # end_discussion 终止状态机（编排层权威判定）——必须先于 errors 检查：
        # 工具由网关注入、claude 侧无对应实现，模型调用后 claude 会因「未知工具」报错退出；
        # 若先查 errors 会把工具轮误判为失败而中止。工具轮的错误应忽略（意图已被捕获）。
        tool_intent = any(isinstance(tu, dict) and _is_end_discussion(tu.get("name")) for tu in tool_uses)
        floor = 2 * len(self.agents) - 1
        # 工具日志统一携带：调用时刻 ts（非写入时刻）、执行结果 exec_ok/exec_error——
        # 意图捕获与执行结果分离，日志能直接看出「调用是否真的成功」
        log_ts = tool_use_ts or _now_str()
        exec_ok = not tool_use_errors
        exec_err = "; ".join(dict.fromkeys(tool_use_errors))[:300]
        for tu in tool_uses:
            if not isinstance(tu, dict) or not _is_end_discussion(tu.get("name")):
                continue
            reason = (tu.get("input") or {}).get("reason", "") or ""
            if round_i < floor:
                store.append_tool_log(self.project_path, self.session_id, {
                    "action": "rejected", "agent": agent.name, "round_i": round_i,
                    "reason": reason, "msg": f"未达最小轮数（floor={floor}），终止被拒",
                    "ts": log_ts, "exec_ok": exec_ok, "exec_error": exec_err,
                })
                continue
            if self.terminate_proposed_by is None:
                self.terminate_proposed_by = agent.name
                self.terminate_reason = reason
                store.append_tool_log(self.project_path, self.session_id, {
                    "action": "proposed", "agent": agent.name, "round_i": round_i,
                    "reason": reason, "msg": "已记录终止提议，等待对方确认",
                    "ts": log_ts, "exec_ok": exec_ok, "exec_error": exec_err,
                })
            elif self.terminate_proposed_by == agent.name:
                store.append_tool_log(self.project_path, self.session_id, {
                    "action": "repeat", "agent": agent.name, "round_i": round_i,
                    "reason": reason, "msg": "本方重复提议，仍在等待对方确认",
                    "ts": log_ts, "exec_ok": exec_ok, "exec_error": exec_err,
                })
            else:
                self.terminate_confirm = True
                self.terminate_reason = reason or self.terminate_reason
                store.append_tool_log(self.project_path, self.session_id, {
                    "action": "confirmed", "agent": agent.name, "round_i": round_i,
                    "reason": self.terminate_reason, "msg": "对方确认，讨论终止",
                    "ts": log_ts, "exec_ok": exec_ok, "exec_error": exec_err,
                })
        if errors and not tool_intent:
            # 失败：不落 agent 发言，落 system 错误消息，调用方据此中止
            await self._record_system(f"{agent.name} 执行出错，会话中止：{errors[0][:200]}")
            return "", errors
        thinking_text = "\n\n".join(thinking_parts).strip()
        # 工具调用失败（如 Claude 对注入工具报「No such tool available」）不得吞掉，
        # 否则前端把工具轮显示成「本轮未输出独立结论」，用户看不到真实失败原因。
        # 按 Option A：失败作为结构化 tool_results 子块随本轮落盘（同一气泡内渲染），
        # 不再拼进正文（正文保持干净结论）；会话仍继续（返回空 errors，_debate 据此不中止）。
        tool_calls = [tu for tu in tool_uses if isinstance(tu, dict)]
        tool_results = []
        if tool_intent and errors:
            for e in errors:
                if e not in full:
                    tool_results.append({"is_error": True, "content": e})
        # 思考随答案一起落盘（刷新后仍可见）；正文只存 answer，不拿 thinking 回填，
        # 避免历史/实时把整段思考重复渲染到结论气泡。
        # 空轮判定：无正文、无思考、且无任何工具调用/工具结果 → 本地模型偶发空响应，
        # 不落空 agent 发言（避免刷新后一串空气泡）；仅广播提示，不持久化。
        # 纯工具轮（仅 tool_calls / tool_results）则保留为气泡，工具调用与失败可见。
        if not full and not thinking_text and not tool_calls and not tool_results:
            await self._emit(type="system", msg=f"{agent.name} 本轮未输出可显示内容，已跳过。")
            return "", errors
        entry = {
            "round": len(self.history), "speaker": agent.name, "role": agent.role,
            "content": full,
            "thinking": thinking_text,
            "tool_calls": tool_calls,
            "tool_results": tool_results,
        }
        # 纯工具轮（仅工具调用/失败、无正文无思考）：意图与状态已入工具日志，但工具调用
        # 与失败结果须保留为可折叠子块可见，故仍落盘（不造假「未输出结论」占位）。
        if full or thinking_text or tool_calls or tool_results:
            self.history.append(entry)
            store.append_message(self.project_path, self.session_id, entry)
        # 工具轮的错误已由状态机消化（claude 对注入工具报「未知工具」是预期行为），
        # 返回空 errors，_debate 据此继续下一轮而非中止。
        return full, ([] if tool_intent else errors)

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
