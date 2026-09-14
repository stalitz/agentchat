import asyncio
import json
import re

from . import store
from .executor import AgentRuntime
from .models import AgentConfig
from .prompt_defaults import PROMPT_TEMPLATES_DEBATE

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
                 max_rounds: int, broadcast, timeout: int = 300, tone: str = "",
                 project_id: str | None = None):
        self.project_path = project_path
        self.session_id = session_id
        self.project_id = project_id or ""
        self.agents = agents
        self.max_rounds = max_rounds
        self.timeout = timeout
        self.broadcast = broadcast  # async fn(event: dict)
        sess = store.read_session(project_path, session_id)
        self.history = sess.get("messages", []) if sess else []
        # 讨论调性：发起时传入优先；未传则沿用会话已存（历史会话续接保持同调）
        self.tone = tone or ((sess or {}).get("tone") or "")
        # 通用辩论提示词模板快照：会话启动时读一次，改配置对新会话生效（运行中会话不受影响）。
        # 结构 settings.prompt_templates.debate.<slot>，缺省槽位回退代码内置默认（PROMPT_TEMPLATES_DEBATE）。
        try:
            _pt_cfg = (store.get_settings().get("prompt_templates") or {}).get("debate") or {}
        except Exception:
            _pt_cfg = {}
        self._pt = {**PROMPT_TEMPLATES_DEBATE, **(_pt_cfg if isinstance(_pt_cfg, dict) else {})}
        # 每个 agent 的「持续 CLI 会话」id，用于 1:1 续接；从磁盘读回（历史会话恢复用）
        self.agent_sessions = (sess or {}).get("agent_sessions", {})
        self.runtimes = {a.name: AgentRuntime(a, self.agent_sessions.get(a.name),
                                               session_base_url=self._gw_url(a)) for a in agents}
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
        # end_discussion 终止状态机（FR5.3b 多数同意）：
        # - terminate_proposed_by：首个提议方（用于 prompt 提示「谁先提的」）
        # - terminate_approvers：已显式同意终止的 agent 集合（每 agent 一次 end_discussion 计一票，
        #   同方重复调用仍只算一票）；集合大小 ≥ ⌊N/2⌋+1（多数）即收敛
        # - terminate_confirm：已达多数同意、整场收敛的标志（_debate 据此结束）
        self.terminate_proposed_by = None
        self.terminate_approvers = set()
        self.terminate_reason = ""
        self.terminate_confirm = False
        # 最近一次「终止被拒」的回执待注入：被拒只记日志不够，必须把原因回传给模型，
        # 否则弱模型（gemma）把通用「请继续参与讨论」回执误解为「已终止·待命」。
        self._reject_inject = None
        # 最近一次「对方被拒」待注入：某 agent 的 end_discussion 被拒后，下一轮**对方** agent
        # 需知道「对方尝试终止但被系统拒绝」——否则弱模型把对方被拒的调用误读为「已提议终止、
        # 等我确认」（实测「谁聪明」gemma 因此连调 2 次 end_discussion 去"确认"）。
        self._peer_rejected = None

    def _gw_url(self, agent: AgentConfig) -> str | None:
        """按会话拼出 claude 网关地址；仅对指向本服务的 base_url 注入 /gw/<pid>/<sid> 路径。

        该路径让网关（/gw/{pid}/{sid}/v1/messages）按会话读取 ollama_think 覆盖全局默认，
        实现「思考」会话级开关。判定「本服务」：host 为 localhost/127.0.0.1/::1 且端口 8777
        （即本网关标准端口）。外部网关（litellm 等）不加路径、保持原样，避免 404。
        非 base_url agent（如真实 claude / codex 直连）返回 None，走各自原生后端。
        """
        bu = agent.base_url
        if not bu or not self.project_id:
            return bu
        try:
            from urllib.parse import urlparse
            p = urlparse(bu)
            if (p.hostname or "").lower() in ("127.0.0.1", "localhost", "::1") and p.port == 8777:
                return f"{bu.rstrip('/')}/gw/{self.project_id}/{self.session_id}"
        except Exception:
            pass
        return bu

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
    def _render_slot(self, slot: str, **kw) -> str:
        """渲染提示词模板槽位：配置模板优先，缺省回退内置默认；<name> 占位符 replace 替换。"""
        t = self._pt.get(slot) or PROMPT_TEMPLATES_DEBATE.get(slot, "")
        for k, v in kw.items():
            t = t.replace("<" + k + ">", "" if v is None else str(v))
        return t

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

        # 多 agent 辩论（FR5.3b 子项2）：回传「本方上次发言之后的全部他人发言」（按发生顺序），
        # 保证每个 agent 都看得到全体其他参与者的观点，而非只看到紧邻上一个（信息对称）。
        # 2 agent 时本方法结果与旧「只取紧邻上家」等价（对方上一句 = 全部增量），无需特判。
        # agent 的持续会话已记住自身历史与历次议题，故只回传「他人增量」，不回传本方旧发言。
        # 全新会话首轮（history 只有 human 任务、本方从未发言）others 为空 → 自然不回传；
        # 续接场景（同会话内再次 start）round_i 重新从 0 开始，但本方已发过言（my_idx>=0），
        # 必须回传「本方上次发言之后的他人增量」——否则续接首轮收不到对方在上一场末尾的
        # 发言（象棋g+ 实测：g1 续接时缺 g2 的红3 马3进4，误当新一轮重新开局）。
        my_idx = -1
        for i, m in enumerate(self.history):
            if m.get("speaker") == agent.name:
                my_idx = i
        if round_i > 0 or my_idx >= 0:
            others = [
                m for m in self.history[my_idx + 1:]
                if m.get("speaker") not in (agent.name, "system", "human")
            ]
            if others:
                delta = "\n\n".join(
                    f"{m['speaker']}（{m.get('role', '')}）说：\n{m['content']}" for m in others
                )
                parts.append("其他 agent 的发言（自你上次发言之后）：\n" + delta)
        # 任务 / 人工介入（本轮新议题通常经此传入）
        if inject:
            parts.append(f"人工介入 / 任务：\n{inject}")
        # 任务背景补齐（2026-08-21）：任务只在 round0 经 inject 传给第一个 agent，
        # 其他 agent 首轮若只看到 delta，可能无法从上一 agent 发言推断出议题——
        # 象棋实测：d1 首轮直接走棋「炮8平5」不复述任务，d2 首轮把 CLI 系统提示的技能
        # 列表误当议题，编出「代码仓库管理」总结并误调 end_discussion（deepseek 急于中止疑同因）。
        # 故每个 agent「就当前议题首次发言」时补任务背景；非首轮不再带（避免每轮重复搬运）。
        if inject is None:
            task = (getattr(self, "_initial_task", "") or "").strip()
            if task:
                # 最后一条 human「任务」消息 = 当前议题（续接场景 history 混着旧议题）
                task_idx = -1
                for i, m in enumerate(self.history):
                    if m.get("speaker") == "human" and m.get("role") == "任务":
                        task_idx = i
                my_last = -1
                for i, m in enumerate(self.history):
                    if m.get("speaker") == agent.name:
                        my_last = i
                # 该 agent 尚未就当前议题发过言 → 补任务背景
                if my_last < task_idx:
                    parts.append(f"任务背景（本场议题）：\n{task}")
        # 终止被拒回执：上一轮本方发起 end_discussion 因未达最小轮数被拒，明确告知模型继续，
        # 不要进入待命/等待状态（弱模型易把通用工具回执误解为「已终止」）
        if self._reject_inject and self._reject_inject[0] == agent.name:
            rj_round = self._reject_inject[1]
            parts.append(self._render_slot("rejected_inject", agent=agent.name, round=rj_round // len(self.agents) + 1))
            self._reject_inject = None
        # 对方被拒提示：上一轮对方尝试 end_discussion 被系统拒绝（未达最小轮数）——
        # 弱模型（gemma）会把对方被拒的调用误读为「对方已提议终止、等我确认」，
        # 进而盲目跟调 end_discussion（「谁聪明」实测连调 2 次去"确认"）。须明确告知。
        if self._peer_rejected and self._peer_rejected[0] != agent.name:
            peer, pr = self._peer_rejected
            parts.append(self._render_slot(
                "peer_rejected",
                agent=agent.name, peer=peer, round=pr // len(self.agents) + 1,
            ))
            self._peer_rejected = None
        # 门槛前每轮预吿：让 agent 明确知道当前轮次与最小轮数要求，避免它在未达门槛时
        # 误调用 end_discussion 后被拒、还把原因归咎于「对方未确认」（gemma 实测归因错误：
        # 当轮回执只收到 MCP 的无状态文案，而精确原因要下一轮才注入，须在此提前框定）。
        _floor = 2 * len(self.agents) - 1
        if round_i < _floor:
            _n = len(self.agents)
            _my_turn = round_i // _n + 1
            _peer_turns = (round_i - 1) // _n + 1 if round_i > 0 else 0
            parts.append(self._render_slot(
                "floor_notice", agent=agent.name, my_turn=_my_turn, peer_turns=_peer_turns,
            ))
        # 附加规则（场景专属大段，如盲棋/游戏规则）：配置后每轮常驻注入，缺省为空
        _extra = (self._pt.get("extra_rules") or "").strip()
        if _extra:
            parts.append(_extra)
        parts.append(self._render_slot("consensus", agent=agent.name))
        # 达到最少轮数（双方各发言 2 次）后的每轮硬提醒：推着模型主动决策收尾，而非等它想起。
        # 本地弱工具调用模型（gemma/r1）在长辩论中几乎不主动调用工具（实测跑满轮次），
        # 故 floor 后每轮明确告知轮次与可选动作，并把「对方已提议」显式喂给本方推动确认。
        floor = 2 * len(self.agents) - 1
        if round_i >= floor:
            n_agents = len(self.agents)
            threshold = n_agents // 2 + 1
            # 投票计数（防御性：测试可能直接置 proposed_by 而未走状态机入 approvers）
            votes = len(self.terminate_approvers) or (1 if self.terminate_proposed_by else 0)
            if self.terminate_proposed_by and self.terminate_proposed_by != agent.name:
                parts.append(self._render_slot(
                    "proposal_peer",
                    agent=agent.name,
                    proposer=self.terminate_proposed_by,
                    reason=self.terminate_reason or "未说明",
                    votes=votes, n_agents=n_agents, threshold=threshold,
                    needed=max(threshold - votes, 0),
                ))
            elif self.terminate_proposed_by == agent.name:
                parts.append(self._render_slot(
                    "proposal_self",
                    agent=agent.name,
                    votes=votes, n_agents=n_agents, threshold=threshold,
                ))
            else:
                parts.append(self._render_slot(
                    "proposal_none",
                    agent=agent.name,
                    turn=round_i // len(self.agents) + 1, max_rounds=self.max_rounds,
                ))
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
        blocks: list[dict] = []  # 时间线有序块：thinking/text/tool_use/tool_result（连续同类型合并）

        def _push_block(btype: str, **kw):
            # 合并逻辑与 executor._normalize_claude_blocks 一致：相邻同类型（thinking/text）合并。
            # 字段名对齐 claude 原生（thinking 块用 thinking，text 块用 text），不另造字段。
            if blocks and blocks[-1]["type"] == btype and btype in ("thinking", "text"):
                if btype == "thinking":
                    blocks[-1]["thinking"] = (blocks[-1].get("thinking") or "") + kw.get("thinking", "")
                else:
                    blocks[-1]["text"] = (blocks[-1].get("text") or "") + kw.get("text", "")
            else:
                blocks.append({"type": btype, **kw})
        tool_use_ts: str | None = None      # 首个 end_discussion tool_use 到达时刻（≈调用时刻）
        tool_use_errors: list[str] = []     # 工具执行失败文本（「有调用意图」≠「调用成功」）
        async for kind, text in runtime.run(
            prompt, self.project_path, self.timeout,
            meta={"round_index": round_i, "max_rounds": self.max_rounds,
                  "agent_count": len(self.agents)},
        ):
            if kind == "text":
                collected.append(text)
                _push_block("text", text=text)
                await self._emit(type="token", agent=agent.name, text=text)
            elif kind == "thinking":
                # 思考块实时单独渲染，并随本轮答案一起落盘（刷新后仍可见）
                thinking_parts.append(text)
                _push_block("thinking", thinking=text)
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
                _push_block("tool_use", name=text.get("name", ""), input=text.get("input", {}) or {})
                # 实时把工具调用块推给前端（按时间线排在思考/正文之后），无需等整轮跑完刷新
                await self._emit(type="tool_use", agent=agent.name,
                                 name=text.get("name", ""), input=text.get("input", {}) or {})
                # 记录首个 end_discussion 调用的到达时刻（≈模型发出调用时刻）：
                # 日志 ts 应为「调用时间」，而非「整轮跑完的写入时间」（后者可能晚数分钟）
                if tool_use_ts is None and isinstance(text, dict) and _is_end_discussion(text.get("name")):
                    tool_use_ts = _now_str()
            elif kind == "tool_result":
                # 工具回执（成功/失败都走这里，executor 已展平为字符串）：按时间线拼块，
                # 失败计入 tool_use_errors 以标 exec_ok；实时推给前端显示在对应工具调用之后。
                is_err = bool(text.get("is_error"))
                content = text.get("content", "") or ""
                _push_block("tool_result", is_error=is_err, content=content)
                if is_err:
                    tool_use_errors.append(content)
                await self._emit(type="tool_result", agent=agent.name, is_error=is_err, content=content)
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
        # msg_round = 本轮落盘消息将取的历史下标（len(history) 在 append 前即为该值）；
        # 前端状态标注用它匹配工具日志，避免 round_i（辩论循环下标）与 h.round（历史下标）差 1。
        msg_round = len(self.history)
        for tu in tool_uses:
            if not isinstance(tu, dict) or not _is_end_discussion(tu.get("name")):
                continue
            reason = (tu.get("input") or {}).get("reason", "") or ""
            if round_i < floor:
                store.append_tool_log(self.project_path, self.session_id, {
                    "action": "rejected", "agent": agent.name, "round_i": round_i,
                    "reason": reason, "msg": f"未达最小轮数（floor={floor}），终止被拒",
                    "ts": log_ts, "exec_ok": exec_ok, "exec_error": exec_err,
                    "msg_round": msg_round,
                })
                # 把「被拒」回传给模型：下一轮该 agent 的 prompt 注入明确原因，
                # 避免其把通用工具回执当「已终止」而进入待命状态
                self._reject_inject = (agent.name, round_i)
                # 同时让下一轮**对方** agent 知道「对方尝试终止但被拒」——弱模型会把
                # 对方被拒的调用误读为「已提议终止、等我确认」，进而盲目跟调（「谁聪明」实测）。
                self._peer_rejected = (agent.name, round_i)
                continue
            if self.terminate_proposed_by is None:
                self.terminate_proposed_by = agent.name
                self.terminate_approvers.add(agent.name)
                self.terminate_reason = reason
                store.append_tool_log(self.project_path, self.session_id, {
                    "action": "proposed", "agent": agent.name, "round_i": round_i,
                    "reason": reason, "msg": "已记录终止提议，等待多数同意",
                    "ts": log_ts, "exec_ok": exec_ok, "exec_error": exec_err,
                    "msg_round": msg_round,
                })
            elif agent.name in self.terminate_approvers:
                # 本人重复调用 end_discussion：仍只计一票，记录 repeat 不增票
                store.append_tool_log(self.project_path, self.session_id, {
                    "action": "repeat", "agent": agent.name, "round_i": round_i,
                    "reason": reason, "msg": "本方重复提议，仍在等待多数同意",
                    "ts": log_ts, "exec_ok": exec_ok, "exec_error": exec_err,
                    "msg_round": msg_round,
                })
            else:
                # 不同 agent 追加一票（FR5.3b 多数同意）
                self.terminate_approvers.add(agent.name)
                threshold = len(self.agents) // 2 + 1
                if len(self.terminate_approvers) >= threshold:
                    self.terminate_confirm = True
                    self.terminate_reason = reason or self.terminate_reason
                    store.append_tool_log(self.project_path, self.session_id, {
                        "action": "confirmed", "agent": agent.name, "round_i": round_i,
                        "reason": self.terminate_reason,
                        "msg": f"已达多数同意（{len(self.terminate_approvers)}/{len(self.agents)}），讨论终止",
                        "ts": log_ts, "exec_ok": exec_ok, "exec_error": exec_err,
                        "msg_round": msg_round,
                    })
                else:
                    store.append_tool_log(self.project_path, self.session_id, {
                        "action": "agreed", "agent": agent.name, "round_i": round_i,
                        "reason": reason,
                        "msg": f"已计一票（{len(self.terminate_approvers)}/{len(self.agents)}），尚未达多数（需 {threshold}）",
                        "ts": log_ts, "exec_ok": exec_ok, "exec_error": exec_err,
                        "msg_round": msg_round,
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
        # 工具结果（成功 + 失败）按时间线已从流式 blocks 收集；同时保留 legacy 字段兼容旧渲染
        tool_results = [b for b in blocks if b["type"] == "tool_result"]
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
            "blocks": blocks,
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
        # 记录本场议题：任务只在 round0 经 inject 传给第一个 agent，
        # 后续 agent 首轮需要它作「任务背景」补齐（否则可能无法从上一 agent 发言推断议题，象棋实测走偏）。
        self._initial_task = initial_task or ""
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
