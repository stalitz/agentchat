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

# 「对抗互动型」风格：其描述里含「对方 / 对立面 / 反方」语义，必须有对方发言才有对象。
# 首个发言者（场上还没有任何他人观点）若照此注入，唯一能反驳的对象就是自己的立场
# （2026-09-15 实测：tone=唱反调 时，role=有神论者的第一个发言者开场即自我拆台——
# 「我必须立刻对自己持有的这一信仰进行最严苛的审视」，议题方向从一开始就跑偏）。
# 故这类风格在「无交锋对象的立论轮」不注入，改为要求先立住己方立场（见 _tone_block）。
_TONE_ADVERSARIAL: set[str] = {"唱反调", "批判", "尖锐", "激进", "怀疑"}


def _is_end_discussion(name) -> bool:
    """判定是否为 end_discussion 调用——兼容干净名（end_discussion）与
    MCP 原名（mcp__terminate-signal__end_discussion，经网关转发/还原后出现）。"""
    return isinstance(name, str) and name.endswith("end_discussion")


def _is_human_input(name) -> bool:
    """判定是否为 request_human_input 调用（HITL 人工介入请求）——兼容干净名与
    MCP 原名（mcp__terminate-signal__request_human_input）。"""
    return isinstance(name, str) and "request_human_input" in name


def _join_answers(answers: list) -> str:
    """把同一次发布内收集到的多条人工回答拼成续跑 delta。

    单条原样返回（保持旧行为与既有测试口径不变）；多条按序号列出，便于模型分辨
    「哪个回答对应哪个问题」。
    """
    answers = [a for a in (answers or []) if (a or "").strip()]
    if not answers:
        return ""
    if len(answers) == 1:
        return answers[0]
    return "\n".join(f"{i + 1}) {a}" for i, a in enumerate(answers))


def _codex_answers_from_questions(questions, answer_text):
    """把前端纯文本回答映射成 codex requestUserInput 期望的 answers map。

    codex 文档：questions 每项含 questionId（唯一键），回包 answers 以 questionId 为键。
    多问时（罕见）统一映射到同一回答文本；无 questionId 则按 index 兜底。
    空 questions 返回 {}（codex 接受空 answers，仅丢失本次回答）。
    """
    ans = {}
    for i, q in enumerate(questions or []):
        if not isinstance(q, dict):
            continue
        qid = q.get("questionId") or q.get("id") or q.get("question") or str(i)
        ans[qid] = answer_text
    return ans



# 「议题明显要用户拍板」的措辞：命中且本场尚未问过用户时，首个发言者必须先问后答（hitl_opening）。
# 背景（2026-09-19 实测）：议题写成「请给多套方案让我选」时，两个 gemma 都理解成「把方案摆出来
# 即可」，各自假设条件直接开讲，用户要的「按我的情况选一套」反而没发生。命中词表刻意放宽：
# 漏判的代价（又一场自说自话）远大于误判的代价（多问一个问题）。
_HITL_TASK_HINTS = (
    "多套", "多种方案", "几个方案", "两套", "三套", "备选", "方案让我", "让我选", "帮我选",
    "让我看看", "给我方案", "哪个更好", "哪个更合适", "选一个", "挑一个", "你建议", "推荐哪个",
    "适合我", "我该选",
    "options", "alternatives", "choose", "pick one", "which one", "which do",
)


def _task_needs_human_choice(text) -> bool:
    """议题是否明显要用户做选择（据此决定是否注入「先问后答」强指令）。"""
    t = (text or "").lower()
    return any(h in t for h in _HITL_TASK_HINTS)


# 「中止 → 继续」时发给被中止 agent 的提示：那半截内容已作废，必须显式告知别再往下接，
# 否则它会在 CLI 会话里接着自己的半句继续说（历史上下文里那半截仍在），出现「从中间续写」。
_RESUME_NOTICE = (
    "（你上一次的发言被用户中止了，那半截内容已作废、不作为讨论依据，也请不要接着它继续写。"
    "请就当前议题重新完整发言一遍。）"
)


def _now_str() -> str:
    """本地时间，与 store._now 同格式（工具日志 ts 用）。"""
    import time as _t
    return _t.strftime("%Y-%m-%d %H:%M:%S")


def _extract_text_tool_call(full: str) -> dict | None:
    """兜底解析：模型未拿到工具 schema 时（如 deepseek 走云端直连、不经本地网关），
    会把 end_discussion / request_human_input 调用写成 prompt 指令约定的文本 json：

        ```json
        {"tool": "end_discussion", "reason": "..."}
        {"tool": "request_human_input", "question": "..."}
        ```

    编排层解析后并入 tool_uses，让终止/人工介入状态机对两种表达（真 tool_use 块 / 文本 json）
    行为一致——否则 deepseek 的 json 不被识别、终止永不收敛、只能靠轮次上限强停。
    返回 {"name": ..., "input": {...}} 或 None。
    """
    if not full:
        return None
    for m in re.finditer(r"```(?:json)?\s*\n?(\{[\s\S]*?\})\s*```", full):
        try:
            data = json.loads(m.group(1))
        except (ValueError, TypeError):
            continue
        tool = data.get("tool")
        if tool == "end_discussion":
            reason = data.get("reason", "")
            if not isinstance(reason, str):
                reason = str(reason) if reason is not None else ""
            return {"name": "end_discussion", "input": {"reason": reason}}
        if tool == "request_human_input":
            question = data.get("question", "")
            if not isinstance(question, str):
                question = str(question) if question is not None else ""
            opts = data.get("options") or []
            if not isinstance(opts, list):
                opts = []
            return {"name": "request_human_input",
                    "input": {"question": question, "options": opts}}
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
# 判定与 reconcile 共用 `store.is_cli_error_text`（唯一权威定义在 store，此处不再各写一份）。
_looks_like_claude_error = store.is_cli_error_text


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
            _settings = store.get_settings()
        except Exception:
            _settings = {}
        try:
            _pt_cfg = (_settings.get("prompt_templates") or {}).get("debate") or {}
        except Exception:
            _pt_cfg = {}
        self._pt = {**PROMPT_TEMPLATES_DEBATE, **(_pt_cfg if isinstance(_pt_cfg, dict) else {})}
        # 三级「思考」开关生效值（会话级 > agent 级 > 全局），按 agent 各解析一次。用于：
        #   ① 注入网关 URL 查询（Ollama 路径，网关据此带 think:false）
        #   ② 传给 AgentRuntime（codex 据此降 model_reasoning_effort）
        #   ③ 真实 claude 直连无干净开关（已知限制，见需求文档）
        self.think_by_agent = {a.name: store.resolve_think(a, sess or {}, _settings) for a in agents}
        # 每个 agent 的「持续 CLI 会话」id，用于 1:1 续接；从磁盘读回（历史会话恢复用）
        self.agent_sessions = (sess or {}).get("agent_sessions", {})
        self.runtimes = {a.name: AgentRuntime(a, self.agent_sessions.get(a.name),
                                               session_base_url=self._gw_url(a),
                                               think=self.think_by_agent.get(a.name)) for a in agents}
        self.single = len(agents) < 2
        self.mode = "chat" if self.single else "debate"
        self.stop_event = asyncio.Event()
        # resume_event 默认 set = 未暂停；pause() 清除后循环阻塞在 wait()。
        # 历史 bug：曾用 pause_event.set() + await pause_event.wait()，
        # 而 asyncio.Event.wait() 在事件已 set 时立即返回 —— 暂停形同虚设。
        self.resume_event = asyncio.Event()
        self.resume_event.set()
        # 「中止」（turn 级，与轮边界的暂停/停止**互不干扰**）：立即掐断正在生成的输出。
        # 与 stop_event 分开，是因为两者语义不同：stop = 跑完当前发言后不再开下一轮；
        # abort = 当场打断当前发言，且这次发言作废（落盘可见、不计轮次、不喂他人）。
        self._abort_requested = False
        self._current_runtime = None   # 当前在途 turn 的 runtime（中止时定位中断目标）
        self._turn_in_flight = False   # 当前是否有 turn 在执行（看门狗据此决定是否硬停）
        self.last_abort_channel = ""   # 实际使用的中断通道（诊断用）
        # 本次 run 是否为「中止后的继续」（resume）：续接进来的发言已有前文交锋，不算立论轮
        # （立论轮只在会话真正第一次开口时出现，见 _is_opening_turn）。
        self._resumed = False
        self.inject_queue: asyncio.Queue = asyncio.Queue()
        # HITL 人工介入：agent 调 request_human_input 时编排层暂停整场、等用户回答。
        # human_input_event 初始未置位（await 即阻塞），provide_human_input 置位并存入回答。
        self.human_input_event = asyncio.Event()
        self._human_input_request = None   # {question, options}：**当前正在问**用户的问题
        self._human_input_answer = None    # str：用户对当前问题的回答
        # 待问队列（2026-09-19）：同一次发言内 agent 可多次调用 request_human_input，
        # 旧实现只取最后一个（reversed+break）/ 只取 codex questions[0]，其余提问被丢弃。
        # 改为全部收进队列，由 _pause_for_human_input 逐个问完 —— 「只要 agent 调用工具，
        # 就走回复」，次数不限（同轮多次调工具 → 连续多次问；跨发言再问 → 下一轮继续）。
        self._human_input_queue: list[dict] = []
        # 本场是否已向用户提过问：用于抑制「先问后答」强指令重复注入（问过一次即不再要求）。
        self._hitl_asked = False
        # 本场议题（debate 由 _debate 写入 / resume 时从历史回填；chat 不用）。
        # 在此先初始化为空：_build_prompt 在 _debate 之外（单测、chat 首轮）也会读它。
        self._initial_task = ""
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
        """按会话拼出 claude 网关地址；仅对指向本服务的 base_url 注入 /gw/<pid>/<sid>/think/<0|1> 路径。

        该路径让网关（/gw/{pid}/{sid}/think/{flag}/v1/messages）按会话读取 think 覆盖全局默认；
        末段 /think/<0|1> 是编排层按 会话>agent>全局 算好的生效值，供网关直接采用 —— 使 agent 级
        开关对走网关的 Ollama agent 也生效（网关本身只能看到 pid/sid，看不到 agent）。
        **用路径段而非查询参数**：claude-code 以 base_url + "/v1/messages" 字符串拼接，带 ?query 会拼坏。
        判定「本服务」：host 为 localhost/127.0.0.1/::1 且端口 8777（即本网关标准端口）。
        外部网关（litellm 等）不加路径、保持原样，避免 404。非 base_url agent（如真实
        claude / codex 直连）返回 None，走各自原生后端。
        """
        bu = agent.base_url
        if not bu or not self.project_id:
            return bu
        try:
            from urllib.parse import urlparse
            p = urlparse(bu)
            if (p.hostname or "").lower() in ("127.0.0.1", "localhost", "::1") and p.port == 8777:
                base = f"{bu.rstrip('/')}/gw/{self.project_id}/{self.session_id}"
                think = getattr(self, "think_by_agent", {}).get(agent.name)
                if think is not None:
                    base += f"/think/{1 if think else 0}"
                return base
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

    @property
    def aborted(self) -> bool:
        return self._abort_requested

    async def abort(self) -> str:
        """中止**当前在途 turn**（turn 级中断），返回实际使用的中断通道。

        与 stop()/pause() 是不同层的能力，互不干扰：
        - pause(): 当前发言跑完，在下一位开始前挂起（轮边界）；
        - stop():  当前发言跑完，不再开下一轮（轮边界）；
        - abort(): 当场掐断正在生成的输出，且**这次发言作废**。

        作废口径（用户确认）：被中止的半截发言仍然落盘并显示（标 interrupted，可追溯「中间出过错」），
        但不计入轮次、不作为其他 agent 的上下文、不作为结论来源；续接时仍由**被中止的这个 agent
        重新发言**（round_i 从待续接记录恢复，不重置回第一个 agent）。
        """
        if self._abort_requested:
            return self.last_abort_channel  # 幂等：重复点不重复发帧
        self._abort_requested = True
        rt = self._current_runtime
        self.last_abort_channel = (await rt.interrupt()) if rt is not None else "none"
        if self._turn_in_flight and self.last_abort_channel != "none":
            # 看门狗：协议帧发出后 turn 仍未收尾（个别后端/版本可能不响应中断）→ 整组硬停，
            # 否则「中止」形同虚设，死循环照样跑下去。
            asyncio.create_task(self._abort_watchdog(rt, 2.5))
        return self.last_abort_channel

    async def _abort_watchdog(self, rt, grace: float):
        await asyncio.sleep(grace)
        if self._turn_in_flight and rt is not None:
            rt.hard_kill()
            await self._emit(type="system",
                             msg="中断未及时生效，已强制终止该 agent 的进程（本次发言作废）。")

    async def inject(self, text: str):
        await self.inject_queue.put(text)

    def provide_human_input(self, text: str):
        """前端交付人工介入的回答：存回答并置位 human_input_event，唤醒暂停中的编排层。"""
        self._human_input_answer = text
        self.human_input_event.set()

    async def _pause_for_human_input(self, agent) -> list[str]:
        """检测到 request_human_input 后：把待问问题逐个推给前端、阻塞等回答、落盘回答。

        同一次发言内 agent 可多次调用工具（不限次数）⇒ 队列里的问题连续问完，返回全部回答
        （调用方用它拼续跑 delta）。被停止/中止则提前退出，剩余问题作废（会话已不再等人）。
        """
        self._hitl_asked = True   # 已问过：后续轮次不再注入「先问后答」强指令
        answers: list[str] = []
        while self._human_input_queue:
            if self.stop_event.is_set() or self._abort_requested:
                break
            self._human_input_request = self._human_input_queue.pop(0)
            req = self._human_input_request or {}
            await self._emit(type="human_input_required", agent=agent.name,
                             question=req.get("question", ""), options=req.get("options") or [])
            await self.human_input_event.wait()
            self.human_input_event.clear()
            answer = (self._human_input_answer or "").strip()
            self._human_input_answer = None
            if answer:
                await self._record_human(answer, "人工回答")
                answers.append(answer)
        self._human_input_request = None
        return answers

    # ---------------- prompt ----------------
    def _render_slot(self, slot: str, **kw) -> str:
        """渲染提示词模板槽位：配置模板优先，缺省回退内置默认；<name> 占位符 replace 替换。"""
        t = self._pt.get(slot) or PROMPT_TEMPLATES_DEBATE.get(slot, "")
        for k, v in kw.items():
            t = t.replace("<" + k + ">", "" if v is None else str(v))
        return t

    def _others_since_last(self, agent: AgentConfig, round_i: int) -> list:
        """回传「该 agent 上次发言之后的全部他人有效发言」（按发生顺序）。

        辩论信息对称用：每个 agent 都看得到全体其他参与者，而非只看到紧邻上一个。
        全新会话首轮（本方从未发言且 round_i == 0）不回传——此时 history 里若混着旧议题的
        他人发言，回传会让 agent 把旧议题内容误当本轮的对方观点。
        """
        my_idx = -1
        for i, m in enumerate(self.history):
            # 只认**有效**发言：被中止那条已作废（不是「本方上次发言」）。若把它算作锚点，
            # 续接重说时增量会从被中止那一刻起算 → 对方在本方上次有效发言之后的发言全丢。
            if m.get("speaker") == agent.name and not m.get("interrupted"):
                my_idx = i
        if not (round_i > 0 or my_idx >= 0):
            return []
        return [
            m for m in self.history[my_idx + 1:]
            if m.get("speaker") not in (agent.name, "system", "human")
            # 被中止的发言已作废：不喂给其他 agent（用户口径：作废但仍落盘可见）
            and not m.get("interrupted")
            # HITL 交互过程发言（interim）：agent 与用户的来回问答（续跑中又发起提问的那些条）
            # 是**人机交互过程**，不是给对方的观点。用户口径（2026-09-19）：只把最后归纳出的结论
            # 带过去，不带与用户的来回交互 —— 否则对方要读一串「我又问了用户什么」，
            # 且这些条里夹带的工具决策文本（如 gemma 自造的 `<tool_code>`）会在 agent 间互相传染。
            and not m.get("interim")
        ]

    def _is_opening_turn(self, agent: AgentConfig, round_i: int) -> bool:
        """本轮是否为「没有交锋对象的立论轮」：本方本场尚未有效发言，或本轮无可回应的他人发言。

        立论轮只属于**会话真正第一次开口**：续接（中止后「继续」）与同会话续开新议题都已有前文，
        不算（前者由 `_resumed` 标记排除，后者因本方已有效发言而自然不成立）。
        只有对抗互动型风格（_TONE_ADVERSARIAL）需要区分；chat 模式恒为 False——风格是针对
        用户刚说的那句话生效的，不存在「没有对象」的问题。
        """
        if self.mode != "debate" or self.tone not in _TONE_ADVERSARIAL:
            return False
        # 续接（中止后「继续」）不算立论轮：本场已有前文交锋，只是被中止者重说这一轮。
        # 不排除的话，「本场首个发言者被中止 → 继续」会因它那条已作废（spoke 为假）而被判成
        # 立论轮，重说时又拿到「先陈述立场」而非该有的对抗风格。
        if self._resumed:
            return False
        spoke = any(m.get("speaker") == agent.name and not m.get("interrupted")
                    for m in self.history)
        if not spoke:
            return True  # 本场首轮：全体各自先陈述立场，避免首个发言者自拆
        return not self._others_since_last(agent, round_i)

    def _tone_block(self, opening: bool) -> str:
        """讨论调性注入文本；opening=True 且风格为对抗互动型时，改为「先立论」指令。

        不能只是删掉风格描述：首轮仍要保证 agent 完整陈述己方立场（辩论的意义是立论后交锋，
        而非一上来就替对方拆自己），故用 opening_statement 模板替代；其第 2 次发言起照常注入
        原风格描述，CLI 会话有状态续接，届时自然进入交锋。
        """
        if not (self.tone and self.tone in TONE_STYLES):
            return ""
        if opening and self.tone in _TONE_ADVERSARIAL:
            return self._render_slot("opening_statement")
        return (f"本场讨论风格：{TONE_STYLES[self.tone]}——"
                "请始终保持此风格发言，不要中途改变。")

    def _build_prompt(self, agent: AgentConfig, inject: str | None, round_i: int = 0) -> str:
        # 状态式 agent：每个 agent 在自己的 CLI 会话里持有完整历史（含历次议题与结论）。
        # 编排器每轮只发「增量 delta」——对手最近一句 + 本轮任务 / 介入，token 极省，
        # 不再重新搬运整段双方对话 / 累积记忆 / 上轮结论（那是无状态 agent 时代的补丁，现已多余）。
        parts = []
        if agent.role:
            # 极便宜的身份锚定：避免 resume 后续接时角色设定漂移（一行，远小于重搬历史）
            parts.append(f"你是：{agent.role}。")
        # 讨论调性：整场统一、每轮都注入，保证风格一致（chat / debate 均生效）。
        # 对抗互动型风格在「立论轮」降级为「先陈述己方立场」——原因见 _tone_block。
        _tone = self._tone_block(opening=self._is_opening_turn(agent, round_i))
        if _tone:
            parts.append(_tone)

        if self.mode == "chat":
            # 人机对话：只把用户本轮新消息作为 delta 发给 agent（其 CLI 会话已含完整历史）
            if inject:
                parts.append(inject)
            else:
                parts.append("（请基于我们之前的对话继续）")
            parts.append("请以对话方式直接回复。这是人机对话，不要调用 end_discussion 工具或输出其调用文本（辩论模式专用）。")
            # 人工介入（HITL）：硬触发条件 + 禁止正文反问 + 仅无工具时的 json 兜底写法。
            parts.append(self._render_slot("hitl_chat"))
            # 本轮诉求要用户拍板、且本场还没问过 → 先问后答（否则只能替用户假设条件）。
            if not self._hitl_asked and _task_needs_human_choice(inject):
                parts.append(self._render_slot("hitl_opening", task=(inject or "")[:120]))
            return "\n\n".join(parts)

        # 多 agent 辩论（FR5.3b 子项2）：回传「本方上次发言之后的全部他人发言」（按发生顺序），
        # 保证每个 agent 都看得到全体其他参与者的观点，而非只看到紧邻上一个（信息对称）。
        # 2 agent 时与旧「只取紧邻上家」等价（对方上一句 = 全部增量），无需特判。
        # agent 的持续会话已记住自身历史与历次议题，故只回传「他人增量」，不回传本方旧发言。
        # 续接场景（同会话内再次 start）round_i 重新从 0 开始，但本方已发过言（my_idx>=0），
        # 必须回传——否则续接首轮收不到对方在上一场末尾的发言
        # （象棋g+ 实测：g1 续接时缺 g2 的红3 马3进4，误当新一轮重新开局）。
        # 取数逻辑与「立论轮」判定共用同一实现（_others_since_last），避免两处分叉。
        others = self._others_since_last(agent, round_i)
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
        # 人工介入提示（HITL）：需要用户拍板时必须调 request_human_input 工具，而非在正文里
        # 反问对方 agent——辩论中对方会替你回答，导致结果偏离用户意图（2026-09-19 实测：agent
        # 连续三轮在正文里「等用户回复」，实际是它在自说自话）。措辞见 prompt_defaults.hitl_debate。
        parts.append(self._render_slot("hitl_debate"))
        # 议题要用户拍板、且本场还没问过 → 首个发言者（round_i == 0）先问后答。
        if not self._hitl_asked and round_i == 0 and _task_needs_human_choice(self._initial_task):
            parts.append(self._render_slot("hitl_opening", task=(self._initial_task or "")[:120]))
        return "\n\n".join(parts)

    def _build_hitl_reply_prompt(self, agent: AgentConfig, answer: str) -> str:
        """HITL「回复轮」专用轻量提示词（2026-09-19 用户口径）。

        用户回答经 request_human_input 交回时，只注入「回答 + 续问/收尾引导」，
        **不复用** `_build_prompt` 的整轮提示词。理由：
        - 用户回答本质是**一次工具交互的回执**，不是新一轮议题，不需要共识终止 / 轮次门槛 /
          调性 / 完整 HITL 规则这些「轮到该 agent 正式发言」才需要的指令；
        - 旧行为把一句回答用 ~2000 字包住（含两段「用代码块代替工具调用」的兜底格式说明），
          每答一次就重灌一次工具指令 —— 把模型反复置于「表达工具决策」的语境，
          正是 gemma 自造 `<tool_code>` 的滋生土壤；
        - 引导语要求「没问题了就把这几轮问答归纳成结论写进正式发言正文」，因为对方 agent
          看不到问答过程（human 已被 _others_since_last 排除、中间过程发言标 interim 也不再转述）。
        """
        parts = []
        if agent.role:
            parts.append(f"你是：{agent.role}。")
        parts.append(f"用户回复：\n{answer}")
        parts.append(self._render_slot(
            "hitl_followup_chat" if self.mode == "chat" else "hitl_followup_debate"))
        return "\n\n".join(parts)

    def _collect_human_input_questions(self, tool_uses: list) -> int:
        """把本轮**全部** request_human_input 调用收进待问队列，返回收进的问题数。

        旧实现 `for tu in reversed(tool_uses): ... break` 只取最后一个，同一次发言里
        多次调用会被丢弃（用户口径：不限次数，只要 agent 调用工具就走回复）。
        """
        n = 0
        for tu in tool_uses:
            if not (isinstance(tu, dict) and _is_human_input(tu.get("name"))):
                continue
            inp = tu.get("input") or {}
            self._human_input_queue.append({
                "question": str(inp.get("question", "") or ""),
                "options": inp.get("options") or [],
            })
            n += 1
        return n

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
        """从历史中取最近一条 agent 发言（即达成一致的收敛发言），去掉 TERMINATE 行，作为格式化结论。

        跳过被中止（interrupted）的发言：那半截内容已作废，不能当结论。
        """
        for m in reversed(self.history):
            if m.get("speaker") not in ("system", "human") and not m.get("interrupted") \
                    and not m.get("interim"):
                text = "\n".join(l for l in m["content"].splitlines() if l.strip() != "TERMINATE")
                return text.strip()
        return ""

    async def _run_turn(self, agent: AgentConfig, round_i: int, inject: str | None,
                        hitl_reply: bool = False) -> tuple[str, list[str]]:
        """执行单轮，返回 (full, errors)。

        hitl_reply=True：本轮的 inject 是**人工介入回答**（HITL 回复轮）——
        走轻量提示词（只注入回答 + 续问/收尾引导，不重灌整轮指令，见 _build_hitl_reply_prompt）。

        errors 非空 = 本轮执行失败（agent 没有产出有效回答）：
        - 不把任何内容当作 agent 发言落盘（错误文本/空响应会被误读为正常回答，
          并作为「对手发言」喂给下一个 agent，导致两个 agent 基于报错信息继续讨论——历史 bug）；
        - 落一条 system 错误消息说明原因，调用方（_debate/_chat_turn）据此中止。
        """
        # 首轮（round_i==0 且 debate 模式）标记 first_turn 仅用于记录/排查；
        # 共识判定交由提示词（见 _build_prompt）引导，不再下发明示「不可讨论就 TERMINATE」指令。
        first_turn = round_i == 0 and not self.single
        prompt = (self._build_hitl_reply_prompt(agent, inject or "")
                  if hitl_reply else self._build_prompt(agent, inject, round_i))
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
        # 登记在途 turn：中止动作据此定位中断目标（runtime）与是否仍需要硬停（_turn_in_flight）
        self._current_runtime = runtime
        self._turn_in_flight = True
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
            elif kind == "tool_plan":
                # claude 的 <tool_code> 规划块（comment-only，如 `// 无需工具调用`）：原生 Claude Code
                # 渲染为工具面板，这里对齐——作为独立时间线块，区别于已执行的 tool_use。
                content = text.get("content", "") if isinstance(text, dict) else str(text)
                _push_block("tool_plan", content=content)
                await self._emit(type="tool_plan", agent=agent.name, content=content)
            elif kind == "human_input":
                # 原生 HITL 通道（codex requestUserInput）：codex 那轮已在 executor 立即回
                # result 结束（不阻塞），此处仅真暂停整场等人；用户回答经 1:1 续接作为
                # 下一次 prompt 续发 codex（不在本分支回 result 解锁，否则 codex 会卡死等）。
                # 与 MCP request_human_input（tool_use）共用待问队列与暂停 UI，生命周期不重叠：
                # native 在 _run_turn 返回前已把队列清空，不会触发调用方的二次 pause。
                # 旧实现只取 questions[0]（codex 原生支持一次问多个，其余被丢弃）→ 改为全部入队逐个问。
                req = text or {}
                questions = [q for q in (req.get("questions") or []) if isinstance(q, dict)]
                if questions:
                    for q in questions:
                        self._human_input_queue.append({
                            "question": str(q.get("question") or req.get("question") or ""),
                            "options": list(q.get("options") or []),
                        })
                else:
                    self._human_input_queue.append({
                        "question": str(req.get("question") or ""),
                        "options": [],
                    })
                try:
                    await self._pause_for_human_input(agent)
                finally:
                    self._human_input_queue.clear()
                    self._human_input_request = None
                    self._human_input_answer = None
                continue
            # done: 忽略
        # 流已结束 → 本轮不再是「在途」，看门狗不应再硬停（硬停会误杀已闲置的 app-server 长连接）
        self._turn_in_flight = False
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
        # 被中止：本次发言作废（落盘可见但不计入轮次/他人上下文），并记录「待续接」。
        # 必须在终止状态机之前返回——半截发言不该产生 end_discussion 投票。
        if self._abort_requested:
            await self._finish_aborted_turn(
                agent, round_i, content=full,
                thinking="\n\n".join(thinking_parts).strip(), blocks=blocks)
            return "", []
        # 兜底：模型未拿到工具 schema（deepseek 走云端直连）时会把 end_discussion 调用
        # 写成文本 json（prompt 指令约定）。解析后并入 tool_uses，与真 tool_use 块等价，
        # 否则 deepseek 的 json 不被识别、终止状态机永不收敛、只能靠轮次上限强停。
        # （解析与提问收集都放在 turn_end 广播之前：interim 标记依赖最终 tool_uses，
        #   而该标记决定这条发言是否属于「HITL 交互过程」、要不要转述给对方 agent。）
        text_tool = _extract_text_tool_call(full)
        if text_tool:
            if tool_use_ts is None:
                tool_use_ts = _now_str()   # 文本表达无独立时间戳，以解析时刻近似
            tool_uses.append(text_tool)
        # request_human_input（HITL 人工介入）：把本轮**全部**提问收进待问队列，
        # _debate/_chat_turn 据此暂停整场等人；不中止本轮——发言正常落盘，由调用方续跑。
        asked = self._collect_human_input_questions(tool_uses)
        # HITL 交互过程发言（interim）：本轮是「回复用户回答」的一次续跑（hitl_reply），
        # 且 agent 又问了一次 ⇒ 这条属于「人与 agent 的来回交互过程」，**不转述给对方 agent**
        # （用户口径 2026-09-19：只把最后归纳出的结论带过去，不带来回交互的发言）。
        # 续跑中不再提问的那条（收尾发言）不算 interim，它承载结论、必须转述。
        interim = bool(hitl_reply and asked)
        await self._emit(type="turn_end", agent=agent.name, interim=interim)
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
        # <tool_code> 规划块：纯规划轮（无正文/思考/工具调用）也应落盘可见，不判空轮跳过
        tool_plans = [b for b in blocks if b["type"] == "tool_plan"]
        # 思考随答案一起落盘（刷新后仍可见）；正文只存 answer，不拿 thinking 回填，
        # 避免历史/实时把整段思考重复渲染到结论气泡。
        # 空轮判定：无正文、无思考、且无任何工具调用/工具结果 → 本地模型偶发空响应，
        # 不落空 agent 发言（避免刷新后一串空气泡）；仅广播提示，不持久化。
        # 纯工具轮（仅 tool_calls / tool_results）则保留为气泡，工具调用与失败可见。
        if not full and not thinking_text and not tool_calls and not tool_results and not tool_plans:
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
        if interim:
            # HITL 交互过程发言：落盘/显示可见（用户能看到 agent 又问了一次），
            # 但不作为其他 agent 的上下文（_others_since_last 过滤），也不作结论来源。
            entry["interim"] = True
        # 纯工具轮（仅工具调用/失败、无正文无思考）：意图与状态已入工具日志，但工具调用
        # 与失败结果须保留为可折叠子块可见，故仍落盘（不造假「未输出结论」占位）。
        if full or thinking_text or tool_calls or tool_results or tool_plans:
            self.history.append(entry)
            store.append_message(self.project_path, self.session_id, entry)
            # 落盘一条**有效**发言即视为「待续接」已被兑现：清除待续接标记。
            # 单一规则，覆盖两种情形：① 续接后该 agent 正常说完 ② 用户改发新议题把它顶掉。
            store.clear_pending_turn(self.project_path, self.session_id)
        # 工具轮的错误已由状态机消化（claude 对注入工具报「未知工具」是预期行为），
        # 返回空 errors，_debate 据此继续下一轮而非中止。
        return full, ([] if tool_intent else errors)

    async def _finish_aborted_turn(self, agent: AgentConfig, round_i: int,
                                   content: str, thinking: str, blocks: list):
        """收尾一次被中止的发言：内容照落盘（可见、可追溯「中间出过错」），但标记作废。

        作废口径（用户确认）：
        - 落盘 + 显示：半截内容保留，前端标「已中止」，用户能看到被告知曾出错；
        - 不计入轮次 / 不作为其他 agent 的上下文（_build_prompt 的 others 过滤掉 interrupted）；
        - 不作为结论来源（_format_conclusion 跳过）；
        - 记 pending_turn：续接时仍由**这个** agent、从**这个** round_i 重新发言，
          既不重置回第一个 agent，也不消耗轮次预算。
        """
        entry = {
            "round": len(self.history), "speaker": agent.name, "role": agent.role,
            "content": content,
            "thinking": thinking,
            "tool_calls": [],
            "tool_results": [],
            "blocks": blocks,
            # 作废标记：前端据此置灰 + 标「已中止」，编排层据此排除出轮次/上下文/结论
            "interrupted": True,
        }
        self.history.append(entry)
        store.append_message(self.project_path, self.session_id, entry)
        store.set_pending_turn(self.project_path, self.session_id, {
            "agent": agent.name, "round_i": round_i,
        })
        await self._emit(type="turn_end", agent=agent.name, interrupted=True)
        await self._record_system(
            f"{agent.name} 的本次发言已被中止（内容作废，不计入轮次）。"
            f"点「继续」将由 {agent.name} 就当前议题重新完整发言。"
        )

    # ---------------- 入口 ----------------
    async def _close_runtimes(self):
        """回收各 agent 的 CLI 会话运行时（codex app-server 长连接在此 kill）。"""
        for rt in self.runtimes.values():
            try:
                await rt.aclose()
            except Exception:
                pass

    async def start(self, initial_task: str, resume: bool = False):
        """执行一次运行（run）。

        resume=False：常规发起 / 续跑（底部「开始讨论 / 继续讨论」）——传入内容是**新议题**，
          发言顺序从第一个 agent 开始、轮次预算重新计。
        resume=True：**中止后的继续**（主按钮槽位的「继续」）——不追加议题，从待续接记录恢复
          round_i 与发言 agent，让被中止的那个 agent 重新发言（作废半轮不消耗轮次）。
        """
        self.running = True
        # 中止标记只管「这一次在途发言」，不是整场的持久状态 → 每次 run 复位
        self._abort_requested = False
        self.last_abort_channel = ""
        # 续接（resume=True）时被中止的 agent 是「接着已有交锋重说这一轮」，不是立论轮；
        # 常规发起/续跑（新议题）同样不算——立论轮只属于本场第一次有效发言（见 _is_opening_turn）。
        self._resumed = bool(resume)
        # 同一个 Session 实例可被多次 run 复用（点「继续」/「继续讨论」续跑）：
        # 上一场的 stop_event（「停止」留下的）与暂停态必须复位，否则新 run 一进循环就退出。
        self.stop_event.clear()
        self.resume_event.set()
        if not resume:
            # 常规发起/续跑以新议题开始，旧的待续接标记随之作废（被顶掉，不再提示续接）
            store.clear_pending_turn(self.project_path, self.session_id)
        try:
            if self.mode == "chat":
                await self._chat_turn(initial_task, resume=resume)
            else:
                await self._debate(initial_task, resume=resume)
        finally:
            self.running = False
            await self._close_runtimes()

    async def _chat_turn(self, text: str, resume: bool = False):
        """单 agent 对话模式：一次调用 = 用户一条 + agent 一条。
        用户本轮消息作为 delta 发给 agent（其 CLI 会话已持有完整历史）。"""
        # 模式提示只在会话开头播一次，否则每发一条都刷一行系统气泡
        if not self.history:
            await self._emit(
                type="system",
                msg="单 agent 模式 · 对话模式：直接输入消息发送，agent 逐条回复（轮次 / 介入 / 暂停不适用）",
            )
        text = (text or "").strip()
        if resume:
            # 中止后继续：不重复追加用户消息（agent 侧已有该条），只发「上次被中止」提示
            await self._run_turn(self.agents[0], len(self.history), _RESUME_NOTICE)
        else:
            if text:
                await self._record_human(text, "用户")
            await self._run_turn(self.agents[0], len(self.history), text or None)
        # HITL 人工介入（chat 模式）：agent 调 request_human_input → 暂停等人 → 同 agent 续跑。
        # while 支持连续多次提问（不限次数：每次续跑都可能再问一次，问题经 _pause_for_human_input
        # 逐个问完）；被停止/中止则退出。
        while self._human_input_queue:
            answers = await self._pause_for_human_input(self.agents[0])
            if self.stop_event.is_set() or self._abort_requested:
                break
            # hitl_reply=True → 走轻量提示词（只带回答 + 续问/收尾引导，不重灌整轮指令）
            await self._run_turn(self.agents[0], len(self.history),
                                 _join_answers(answers), hitl_reply=True)
        # 失败时 _run_turn 已落 system 错误消息并广播 error；单 agent 本轮即结束，无需额外处理
        await self._emit(type="run_end", mode="chat", aborted=self._abort_requested)

    async def _debate(self, initial_task: str, resume: bool = False):
        pending = store.get_pending_turn(self.project_path, self.session_id) if resume else None
        if resume and not pending:
            resume = False  # 待续接已被兑现/清除 → 退回常规行为，避免空跑
            self._resumed = False  # 退回常规行为时「续接」语义同步撤销
        if resume:
            # 续接沿用语会已存的议题（不追加 human「任务」，否则会被当成新议题）
            self._initial_task = ""
            for m in self.history:
                if m.get("speaker") == "human" and m.get("role") == "任务":
                    self._initial_task = m.get("content") or ""
            round_i = int(pending.get("round_i") or 0)
            pending_inject = _RESUME_NOTICE
            await self._emit(
                type="system",
                msg=f"继续讨论：由 {pending.get('agent')} 重新发言（上一次发言已作废，不消耗轮次）",
            )
        else:
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
            if self._abort_requested:
                break  # 轮边界处被中止（极端时序）：同样结束本场，等「继续」重说
            if self.paused:
                await self._emit(type="system", msg="已暂停")
                await self.resume_event.wait()
                if self.stop_event.is_set():
                    break
                await self._emit(type="system", msg="已继续")

            current = self.agents[round_i % len(self.agents)]
            full, errors = await self._run_turn(current, round_i, pending_inject)
            pending_inject = None

            # HITL 人工介入：agent 调 request_human_input → 暂停整场等人，回答由「发起提问的 agent」
            # 续跑（round_i 不增，不消耗额外轮次）。内层 while 支持同一次发言内连续多次提问
            # （不限次数：只要 agent 再调工具就走回复；同轮多次调用的问题由 _pause_for_human_input
            # 逐个问完）。须先于 errors 检查：工具轮可能带「未知工具」类误报错误，意图已被捕获。
            while self._human_input_queue:
                answers = await self._pause_for_human_input(current)
                if self.stop_event.is_set() or self._abort_requested:
                    break
                # hitl_reply=True → 走轻量提示词（只带回答 + 续问/收尾引导，不重灌整轮指令）；
                # 该轮若又发起提问，落盘时标 interim（交互过程，不转述给对方）。
                full, errors = await self._run_turn(
                    current, round_i, _join_answers(answers), hitl_reply=True)

            # 发言被中止：整场停下（不推进 round_i、不喂给下一位），等「继续」由同一 agent 重说。
            # 中止 ≠ 停止：停止是「跑完当前发言不再开下一轮」，中止是当场作废这次发言。
            if self._abort_requested or self.stop_event.is_set():
                break

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

        if self._abort_requested:
            # 中止收尾：本次在途发言已作废（_finish_aborted_turn 已落消息与待续接记录），
            # 这里只标一个场次结束语，明确「不是正常结束」。
            await self._record_system("本场已中止（点「继续」由被中止的 agent 重新发言，轮次接着算）")
        else:
            await self._record_system("会话结束")
        await self._emit(type="run_end", mode="debate", aborted=self._abort_requested)
