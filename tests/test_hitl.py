# -*- coding: utf-8 -*-
"""回归测试：人工介入（HITL, ask_human_input）链路（2026-09-19 修的两个问题）。

问题 1（codex 上报断链 · 真 bug）
    executor 的两条 codex 通路——app-server 实时 `item/completed`、`codex exec` 与 rollout
    兜底的 `item.completed`——判定受控 MCP 工具时都只写 `"end_discussion" in tool`，
    `ask_human_input` 掉进 else 分支被 `_codex_native_tool_use` 判 None 吞掉
    ⇒ codex 即便真的调了，编排层也收不到、不会暂停（界面上只看到它在正文里空问）。
    修法：受控工具判定集中在 `_guarded_mcp_tool`（与 `_is_mcp_tool_item` 同处），两条通路共用。

问题 2（提示词是软条件 · 模型行为问题）
    旧措辞「当你需要用户从多个方案中选择时」被 gemma 一律理解成「把方案摆出来即可」，
    于是直接在正文里反问；而辩论里正文反问会被**对方 agent** 接过去回答，用户根本看不到
    （实测：agent 连续三轮「等用户回复」，实际是自说自话）。
    修法：判断条件 + 明令禁止正文反问 + 「先问后答（按需）」软提示**每轮常驻**
    （prompt_defaults 的 hitl_chat / hitl_debate 两槽位）。
    2026-09-21 修正：删去按议题词表强制的 hitl_opening「本场必须先问后答」——
    「是否要问用户」交给 agent 判断，不因议题含「多套/帮我选」等字样就强制先问。

运行：
  .venv/bin/python tests/run.py
"""
import sys
import unittest
import asyncio
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.base import models, orch, store  # noqa: E402
from backend.executor import _guarded_mcp_tool, _parse_codex_line, AgentRuntime, _hitl_tool_result  # noqa: E402
from backend.orchestrator import _codex_answers_from_questions  # noqa: E402


class Recorder:
    def __init__(self):
        self.events = []
    async def __call__(self, ev):
        self.events.append(ev)


def _mcp_item(tool, arguments, type_="mcp_tool_call"):
    return {"type": type_, "id": "item_1", "server": "terminate-signal",
            "tool": tool, "arguments": arguments}


class TestGuardedMcpTool(unittest.TestCase):
    """受控 MCP 工具判定：两个都要认，且产出干净名。"""

    def test_end_discussion_and_human_input_both_recognized(self):
        self.assertEqual(_guarded_mcp_tool(_mcp_item("end_discussion", {"reason": "x"})),
                         "end_discussion")
        self.assertEqual(_guarded_mcp_tool(_mcp_item("ask_human_input", {"question": "q"})),
                         "ask_human_input")

    def test_mcp_prefixed_name_normalized(self):
        # codex 侧可能带上 MCP 前缀（网关还原后的形态）
        self.assertEqual(
            _guarded_mcp_tool(_mcp_item("mcp__terminate-signal__ask_human_input", {})),
            "ask_human_input")

    def test_legacy_name_normalized_to_new(self):
        """改名兼容（2026-09-20）：旧名 request_human_input 仍要认，且**归一到新名**
        —— 历史会话的 tool_use / rollout 里存的就是旧名。"""
        self.assertEqual(
            _guarded_mcp_tool(_mcp_item("request_human_input", {"question": "q"})),
            "ask_human_input")
        self.assertEqual(
            _guarded_mcp_tool(_mcp_item("mcp__terminate-signal__request_human_input", {})),
            "ask_human_input")

    def test_app_server_camel_case_item_type(self):
        # app-server 通路 item.type 是驼峰 mcpToolCall
        self.assertEqual(
            _guarded_mcp_tool(_mcp_item("ask_human_input", {}, type_="mcpToolCall")),
            "ask_human_input")

    def test_other_tools_not_guarded(self):
        self.assertIsNone(_guarded_mcp_tool(_mcp_item("some_other_tool", {})))
        self.assertIsNone(_guarded_mcp_tool({"type": "command_execution"}))
        self.assertIsNone(_guarded_mcp_tool(None))


class TestCodexParseHumanInput(unittest.TestCase):
    """_parse_codex_line（exec / rollout 通路）：ask_human_input 必须产出 tool_use。"""

    def test_human_input_yields_tool_use(self):
        line = ('{"type":"item.completed","item":{"id":"i1","type":"mcp_tool_call",'
                '"server":"terminate-signal","tool":"ask_human_input",'
                '"arguments":{"question":"你的训练频率？","options":["每周2次","每周4次"]}}}')
        out = _parse_codex_line(line, None)
        self.assertEqual(len(out), 1, f"应产出一条 tool_use，实际 {out}")
        kind, payload = out[0]
        self.assertEqual(kind, "tool_use")
        self.assertEqual(payload["name"], "ask_human_input")
        self.assertEqual(payload["input"]["question"], "你的训练频率？")
        self.assertEqual(payload["input"]["options"], ["每周2次", "每周4次"])

    def test_legacy_tool_name_still_yields_tool_use(self):
        """改名兼容：rollout 里存的旧名 request_human_input 仍产出 tool_use（归一为新名）。"""
        line = ('{"type":"item.completed","item":{"id":"i9","type":"mcp_tool_call",'
                '"server":"terminate-signal","tool":"request_human_input",'
                '"arguments":{"question":"旧名提问"}}}')
        out = _parse_codex_line(line, None)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][1]["name"], "ask_human_input")
        self.assertEqual(out[0][1]["input"]["question"], "旧名提问")

    def test_end_discussion_still_yields_tool_use(self):
        # 回归：放开过滤不能把 end_discussion 弄丢
        line = ('{"type":"item.completed","item":{"id":"i2","type":"mcp_tool_call",'
                '"server":"terminate-signal","tool":"end_discussion",'
                '"arguments":{"reason":"达成一致"}}}')
        out = _parse_codex_line(line, None)
        self.assertEqual(out[0][1]["name"], "end_discussion")
        self.assertEqual(out[0][1]["input"]["reason"], "达成一致")

    def test_started_event_does_not_duplicate(self):
        # 只认 completed：started 不产事件，避免同一次调用计两次
        line = ('{"type":"item.started","item":{"id":"i3","type":"mcp_tool_call",'
                '"tool":"ask_human_input","arguments":{}}}')
        self.assertEqual(_parse_codex_line(line, None), [])


class TestHitlPrompt(unittest.TestCase):
    """提示词：该问用户的判断条件 / 禁止正文反问 / 「先问后答」软提示**每轮常驻**。

    2026-09-21 口径修正：不再有按议题词表强制的「本场必须先问后答」（hitl_opening 槽与
    _HITL_TASK_HINTS 词表已删）。「是否要问用户」交给 agent 判断，软提示「先问后答（按需）」
    写进 hitl_chat / hitl_debate，每轮、每议题都在。
    """

    def setUp(self):
        self.proj = "/tmp/agentchat_test_hitl_" + str(id(self))
        self.sid = store.create_session(self.proj, "HITL 测试会话")["id"]

    def _session(self, agents, max_rounds=6):
        s = orch.Session(self.proj, self.sid, agents, max_rounds, Recorder(), timeout=300)
        return s

    def test_chat_prompt_has_soft_hint(self):
        a = models.AgentConfig(name="A", type="claude-code")
        s = self._session([a])
        p = s._build_prompt(a, "给我几套方案让我选", round_i=0)
        self.assertIn("ask_human_input", p, "chat 提示词必须点出工具名")
        self.assertIn("禁止用正文反问代替工具调用", p, "必须明令禁止正文反问")
        self.assertIn("先问后答", p, "「先问后答（按需）」软提示应常驻")
        self.assertIn("要不要问由你自己判断", p, "判断权归 agent，不得强制要问")

    def test_chat_prompt_soft_hint_even_when_task_is_plain(self):
        # 软提示是每轮常驻的通用规则，与议题「像不像要拍板」无关（词表机制已删）
        a = models.AgentConfig(name="A", type="claude-code")
        s = self._session([a])
        p = s._build_prompt(a, "请介绍跳高训练原理", round_i=0)
        self.assertIn("ask_human_input", p, "通用规则每轮都在")
        self.assertIn("先问后答", p, "软提示每轮都在（不再挑议题）")
        self.assertIn("信息已经足够", p, "须给出「够信息就直接推进」的反向引导")

    def test_debate_prompt_warns_peer_will_answer(self):
        a1 = models.AgentConfig(name="A", type="claude-code")
        a2 = models.AgentConfig(name="B", type="codex")
        s = self._session([a1, a2])
        s._initial_task = "请给多套跳高训练方案让我选"
        p = s._build_prompt(a1, "请给多套跳高训练方案让我选", round_i=0)
        self.assertIn("ask_human_input", p)
        self.assertIn("对方 agent", p, "必须说明正文反问会被对方 agent 接走")
        self.assertIn("先问后答", p, "软提示在辩论首轮也在")

    def test_debate_soft_hint_on_later_rounds(self):
        # 每轮都带（不再只首轮）：第二个发言者 / 后续轮次同样有软提示
        a1 = models.AgentConfig(name="A", type="claude-code")
        a2 = models.AgentConfig(name="B", type="codex")
        s = self._session([a1, a2])
        s._initial_task = "请给多套方案让我选"
        p = s._build_prompt(a2, None, round_i=1)
        self.assertIn("ask_human_input", p)
        self.assertIn("先问后答", p, "后续轮次同样常驻软提示")

    def test_hitl_opening_slot_removed(self):
        # hitl_opening 槽与词表强触发已删除：不再有任何「本场必须先问后答」的强制文案
        from backend.prompt_defaults import PROMPT_TEMPLATES_DEBATE as D
        self.assertNotIn("hitl_opening", D)
        self.assertNotIn("必须先问后答", "\n".join(D.values()))


class _FakeStdin:
    def __init__(self):
        self.writes = []
    def write(self, b):
        self.writes.append(b)


class _FakeProc:
    """模拟 codex app-server 子进程：stdout 按行喂入，stdin 捕获写帧。"""
    def __init__(self, lines):
        self._lines = list(lines)
        self.stdout = self
        self.stderr = None
        self.stdin = _FakeStdin()
        self.returncode = 0
    async def readline(self):
        if self._lines:
            return (self._lines.pop(0) + "\n").encode()
        return b""


class TestNativeCodexHumanInput(unittest.TestCase):
    """原生通道 item/tool/requestUserInput：兜底转 ask_human_input 语义，且不卡 codex。

    关键不变量（用户纠正 2026-09-19）：requestUserInput 只是 codex 自发中断的**兜底**——
    转成与真 ask_human_input 同语义的暂停；但 codex 那轮必须立即结束（回 result），
    不能阻塞等人。用户回答经 1:1 续接作为下一次 prompt 续发 codex。
    """

    def test_read_loop_immediately_acks_and_forwards(self):
        # 读循环遇到 requestUserInput：立即回空 result（codex 那轮结束、不卡）+ 转交 _human_input。
        rt = AgentRuntime(models.AgentConfig(name="A", type="codex"), None)
        rt._appserver_proc = _FakeProc([
            json.dumps({"jsonrpc": "2.0", "method": "item/tool/requestUserInput",
                        "id": "x1", "params": {"questions": [
                            {"questionId": "q1", "question": "每周几次训练？",
                             "options": ["2次", "4次"]}]}}),
        ])
        rt._as_queue = asyncio.Queue()
        async def run():
            await rt._as_read_loop()
        asyncio.run(run())
        got = []
        while True:
            try:
                got.append(rt._as_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        human = next((g for g in got if g.get("_human_input")), None)
        self.assertIsNotNone(human, "应将 requestUserInput 转交为 _human_input 事件（兜底）")
        self.assertEqual(human["_human_input"]["rid"], "x1")
        self.assertEqual(human["_human_input"]["questions"][0]["questionId"], "q1")
        # 关键：立即回空 result，codex 那轮结束、不卡——而非阻塞等人回答
        self.assertEqual(len(rt._appserver_proc.stdin.writes), 1, "应向 codex 立即回 1 帧 result")
        frame = json.loads(rt._appserver_proc.stdin.writes[0].decode().strip())
        self.assertEqual(frame["id"], "x1")
        self.assertEqual(frame["result"], {"answers": {}})

    def test_hitl_tool_result_has_interrupt_semantics(self):
        # 受控工具回包：ask_human_input 必须带中断语义（告知 codex「本轮已中止、等下次」）。
        r = _hitl_tool_result("ask_human_input")
        self.assertTrue(r["success"])
        self.assertIn("本次发言到此结束", r["contentItems"][0]["text"])
        self.assertIn("后续输入", r["contentItems"][0]["text"])
        # 其余受控工具（end_discussion）正常成功、无中断语义
        self.assertEqual(_hitl_tool_result("end_discussion"), {"contentItems": [], "success": True})


class TestCodexAnswersMapping(unittest.TestCase):
    """前端纯文本回答 → codex answers map（按 questionId 为键）。"""

    def test_maps_by_question_id(self):
        qs = [{"questionId": "q1", "question": "频率？", "options": ["2", "4"]}]
        self.assertEqual(_codex_answers_from_questions(qs, "每周4次"),
                         {"q1": "每周4次"})

    def test_multi_question_same_answer(self):
        qs = [{"questionId": "q1"}, {"questionId": "q2"}]
        self.assertEqual(_codex_answers_from_questions(qs, "好"),
                         {"q1": "好", "q2": "好"})

    def test_fallback_when_no_id(self):
        qs = [{"question": "频率？"}]
        ans = _codex_answers_from_questions(qs, "每周4次")
        self.assertEqual(list(ans.values()), ["每周4次"])

    def test_empty_questions(self):
        self.assertEqual(_codex_answers_from_questions([], "x"), {})


class _FakeRuntime:
    """替代 AgentRuntime：run() 吐构造好的事件序列。"""
    def __init__(self, events):
        self._events = events
        self.cli_session_id = None
    def run(self, prompt, project_path, timeout, meta=None):
        async def gen():
            for ev in self._events:
                yield ev
        return gen()


class TestOrchestratorNativeHumanInput(unittest.TestCase):
    """编排层 _run_turn：收到 human_input 事件 → 广播暂停 → 用户回答后续发（不回 result 解锁）。"""

    def setUp(self):
        self.proj = "/tmp/agentchat_test_native_orch_" + str(id(self))
        self.sid = store.create_session(self.proj, "原生HITL编排测试")["id"]

    def _session(self, agents):
        return orch.Session(self.proj, self.sid, agents, 4, Recorder(), timeout=300)

    def test_native_channel_pauses_without_blocking_codex(self):
        a = models.AgentConfig(name="A", type="codex")
        s = self._session([a])
        rt = _FakeRuntime([
            ("human_input", {"rid": "q1", "questions": [
                {"questionId": "q1", "question": "每周几次训练？", "options": ["2次", "4次"]}]}),
            ("text", "收到，按每周4次安排。"),
            ("done", ""),
        ])
        s.runtimes[a.name] = rt

        async def drive():
            task = asyncio.create_task(s._run_turn(a, 0, "设计训练计划"))
            for _ in range(400):
                if s._human_input_request:
                    break
                await asyncio.sleep(0.005)
            self.assertIsNotNone(s._human_input_request, "应进入 HITL 暂停")
            req_ev = [e for e in s.broadcast.events
                      if e.get("type") == "human_input_required"]
            self.assertTrue(req_ev, "应广播 human_input_required")
            self.assertEqual(req_ev[0]["question"], "每周几次训练？")
            self.assertEqual(req_ev[0]["options"], ["2次", "4次"])
            # 模拟前端交付回答（provide_human_input 置位 event 唤醒编排层）
            s.provide_human_input("每周4次")
            return await task

        full, errors = asyncio.run(drive())
        # 关键不变量：原生通道不再回 result 解锁 codex（_FakeRuntime 已无 _as_respond_human_input
        # 即证明——若编排层仍调用会 AttributeError）。用户回答经 1:1 续接作为下次 prompt 续发。
        self.assertIsNone(s._human_input_request, "处理后应清空标记")
        self.assertEqual(errors, [])
        self.assertIn("每周4次", full, "用户回答续跑后的发言应进入本轮正文")


class _ScriptedRuntime:
    """按调用次序吐不同事件序列（替代 AgentRuntime），并留存每次拿到的 prompt。"""

    def __init__(self, scripts):
        self._scripts = list(scripts)
        self.prompts = []
        self.cli_session_id = None

    def run(self, prompt, project_path, timeout, meta=None):
        self.prompts.append(prompt)
        script = self._scripts.pop(0) if self._scripts else [("done", "")]
        async def gen():
            for ev in script:
                yield ev
        return gen()


async def _wait_until(pred, tries=800, delay=0.005):
    for _ in range(tries):
        if pred():
            return True
        await asyncio.sleep(delay)
    return False


def _ask(q, name="ask_human_input", options=None):
    return ("tool_use", {"name": name, "input": {"question": q, "options": options or []}})


class TestHitlReplyPrompt(unittest.TestCase):
    """HITL「回复轮」轻量提示词：只带回答 + 续问/收尾引导，不重灌整轮指令。

    用户口径（2026-09-19）：回复时不需要带每轮那种完整提示词，只需告诉 agent
    「还有问题可以继续调工具问；没问题了就总结这几次交互得出完整结论，交给另一个 agent 发言」。
    """

    def setUp(self):
        self.proj = "/tmp/agentchat_test_hitl_reply_" + str(id(self))
        self.sid = store.create_session(self.proj, "HITL 回复轮测试")["id"]

    def _session(self, agents):
        return orch.Session(self.proj, self.sid, agents, 6, Recorder(), timeout=300)

    def test_chat_reply_prompt_is_light_and_guiding(self):
        a = models.AgentConfig(name="A", type="codex")
        s = self._session([a])
        p = s._build_hitl_reply_prompt(a, "每周 3-4 次")
        self.assertIn("每周 3-4 次", p, "必须带上用户回答原文")
        self.assertIn("ask_human_input", p, "要告知还能继续调工具问（不限次数）")
        self.assertIn("归纳", p, "要引导把几轮问答归纳成结论")
        self.assertNotIn("轮次门槛", p, "回复轮不重灌轮次门槛")
        self.assertNotIn("共识终止", p, "回复轮不重灌终止规则")
        self.assertNotIn("禁止用正文反问", p, "回复轮不重灌完整 HITL 规则")
        self.assertNotIn("end_discussion", p, "chat 回复轮不该出现终止指令")

    def test_debate_reply_prompt_tells_peer_sees_only_your_words(self):
        a1 = models.AgentConfig(name="A", type="codex")
        a2 = models.AgentConfig(name="B", type="codex")
        s = self._session([a1, a2])
        p = s._build_hitl_reply_prompt(a1, "每周 3-4 次")
        self.assertIn("对方 agent", p, "要说明「对方只看得到你的发言正文，看不到问答过程」")
        self.assertIn("归纳", p)
        self.assertNotIn("轮次门槛", p)
        self.assertNotIn("共识终止", p)

    def test_collect_single_question_per_turn(self):
        """一次发言只留**一个**待问问题（没有队列，用户口径 2026-09-20）。"""
        a = models.AgentConfig(name="A", type="codex")
        s = self._session([a])
        n = s._collect_human_input_questions([
            {"name": "ask_human_input", "input": {"question": "Q1", "options": ["a"]}},
            {"name": "mcp__terminate-signal__end_discussion", "input": {"reason": "x"}},
        ])
        self.assertEqual(n, 1, "一次发言里的一次提问")
        self.assertEqual(s._human_input_request["question"], "Q1")
        self.assertEqual(s._human_input_request["options"], ["a"])
        self.assertFalse(hasattr(s, "_human_input_queue"), "不应再有队列")
        # 极端情况：真的出现多次调用 → 取最后一个（覆盖），绝不排队
        s._collect_human_input_questions([
            {"name": "ask_human_input", "input": {"question": "Q1"}},
            {"name": "mcp__terminate-signal__ask_human_input", "input": {"question": "Q2"}},
        ])
        self.assertEqual(s._human_input_request["question"], "Q2", "多次调用取最后一个，不排队")

    def test_pause_asks_one_question_only(self):
        """一问一答的来回：问掉当前这一个问题、答完即返回并清空，不残留下一个问题。

        后续提问必须由 agent「答完后再说一轮」时重新发起 —— 每次提问都是独立发言
        （用户口径 2026-09-20：没有队列，也不需要）。
        """
        a = models.AgentConfig(name="A", type="codex")
        s = self._session([a])
        s._collect_human_input_questions([
            {"name": "ask_human_input", "input": {"question": "Q1"}},
            {"name": "ask_human_input", "input": {"question": "Q2"}},
        ])

        async def drive():
            task = asyncio.create_task(s._pause_for_human_input(a))
            self.assertTrue(await _wait_until(lambda: s._human_input_request is not None))
            self.assertEqual(s._human_input_request["question"], "Q2")
            s.provide_human_input("答1")
            return await task

        answer = asyncio.run(drive())
        self.assertEqual(answer, "答1", "一次只返回这一问的回答")
        self.assertIsNone(s._human_input_request, "答完即清空：不会有遗留问题在下一轮突然冒出来")

    def test_others_since_last_excludes_interim(self):
        # 对方只应看到「起问发言 + 最后结论」，看不到与用户的来回交互过程
        a1 = models.AgentConfig(name="A", type="codex")
        a2 = models.AgentConfig(name="B", type="codex")
        s = self._session([a1, a2])
        s.history = [
            {"speaker": "human", "role": "任务", "content": "议题"},
            {"speaker": "A", "role": "r", "content": "起问 + 观点"},
            {"speaker": "A", "role": "r", "content": "又问了一次", "interim": True},
            {"speaker": "A", "role": "r", "content": "结论：按每周4次"},
        ]
        others = s._others_since_last(a2, round_i=1)
        self.assertEqual([m["content"] for m in others],
                         ["起问 + 观点", "结论：按每周4次"],
                         "interim 的交互过程发言不应转述给对方 agent")

    def test_format_conclusion_skips_interim(self):
        a1 = models.AgentConfig(name="A", type="codex")
        a2 = models.AgentConfig(name="B", type="codex")
        s = self._session([a1, a2])
        s.history = [
            {"speaker": "A", "role": "r", "content": "结论正文"},
            {"speaker": "A", "role": "r", "content": "又问了一次", "interim": True},
        ]
        self.assertEqual(s._format_conclusion(), "结论正文",
                         "交互过程发言不能当结论来源")


class TestChatMultiAskEndToEnd(unittest.TestCase):
    """chat 模式端到端：agent 连续两轮问用户（不限次数）→ 每次回复走轻量提示词 → 收尾给结论。"""

    def setUp(self):
        self.proj = "/tmp/agentchat_test_multiask_" + str(id(self))
        self.sid = store.create_session(self.proj, "多次提问测试")["id"]

    def test_two_asks_then_conclusion(self):
        a = models.AgentConfig(name="A", type="codex")
        s = orch.Session(self.proj, self.sid, [a], 4, Recorder(), timeout=300)
        rt = _ScriptedRuntime([
            [("text", "先问频率。"),
             _ask("每周几次？", options=["2", "4"]), ("done", "")],
            [("text", "再问时段。"),
             _ask("偏好时段？", name="mcp__terminate-signal__ask_human_input"), ("done", "")],
            [("text", "结论：每周 4 次、早上训练。"), ("done", "")],
        ])
        s.runtimes[a.name] = rt

        async def drive():
            task = asyncio.create_task(s._chat_turn("设计训练计划"))
            self.assertTrue(await _wait_until(
                lambda: (s._human_input_request or {}).get("question") == "每周几次？"))
            s.provide_human_input("每周 4 次")
            self.assertTrue(await _wait_until(
                lambda: (s._human_input_request or {}).get("question") == "偏好时段？"),
                "第一次回答后 agent 再问一次，应再次暂停（不限次数）")
            s.provide_human_input("早上")
            await task

        asyncio.run(drive())

        # 1) 三次调用：起问 / 回复后续问 / 回复后收尾
        self.assertEqual(len(rt.prompts), 3)
        # 2) 回复轮 prompt 是轻量的（无轮次门槛/终止规则），且带用户回答与续问引导
        for p, ans in ((rt.prompts[1], "每周 4 次"), (rt.prompts[2], "早上")):
            self.assertIn(ans, p)
            self.assertIn("ask_human_input", p)
            self.assertNotIn("轮次门槛", p, "回复轮不重灌整轮提示词")
            self.assertNotIn("共识终止", p, "回复轮不重灌整轮提示词")
        # 3) 起问轮（非回复轮）仍是完整提示词（含完整 HITL 规则），证明只对回复轮瘦身
        self.assertIn("禁止用正文反问", rt.prompts[0])

        msgs = [m for m in s.history if m.get("speaker") == "A"]
        self.assertEqual(len(msgs), 3)
        self.assertFalse(msgs[0].get("interim"), "起问发言是正式发言，不算交互过程")
        self.assertTrue(msgs[1].get("interim"), "回复后又问的那条 = 交互过程，不转述给对方")
        self.assertFalse(msgs[2].get("interim"), "收尾结论必须转述给对方")
        # 4) 落盘的 interim 标记可被 _others_since_last 识别（chat 无对方，这里直接断言字段）
        self.assertIn("结论", msgs[2]["content"])
        # 5) 持久化回读：interim 必须随消息落盘（刷新后前端才能继续灰化标注）
        saved = store.read_session(self.proj, self.sid) or {}
        saved_a = [m for m in (saved.get("messages") or []) if m.get("speaker") == "A"]
        self.assertEqual(len(saved_a), 3)
        self.assertTrue(saved_a[1].get("interim"), "interim 应持久化（刷新后仍可见标注）")
        self.assertFalse(saved_a[2].get("interim"))


class TestChatOneAskOneReplyLoop(unittest.TestCase):
    """一问一答的来回：每次提问都是**独立的一次发言**，没有队列（用户口径 2026-09-20）。

    agent 说一轮 → 问一个问题 → 本次发言结束 → 用户答 → agent **再说一轮**（新发言），
    由它根据回答决定再问一次还是给结论。次数不限，但绝不存在「同一次发言问多次、
    把回答攒成 1) … 2) … 一次灌回」。
    """

    def setUp(self):
        self.proj = "/tmp/agentchat_test_oneask_" + str(id(self))
        self.sid = store.create_session(self.proj, "一问一答来回测试")["id"]

    def test_each_turn_asks_once_then_speaks_again(self):
        a = models.AgentConfig(name="A", type="codex")
        s = orch.Session(self.proj, self.sid, [a], 4, Recorder(), timeout=300)
        rt = _ScriptedRuntime([
            # 第 1 次发言：只问一个（预算）
            [("text", "先确认预算。"), _ask("Q1 预算？"), ("done", "")],
            # 第 2 次发言：拿到预算后再问一个（时段）
            [("text", "收到预算，再确认时段。"), _ask("Q2 时段？"), ("done", "")],
            # 第 3 次发言：不再问，给结论
            [("text", "结论：按预算与时段执行。"), ("done", "")],
        ])
        s.runtimes[a.name] = rt

        asked: list[str] = []

        async def drive():
            task = asyncio.create_task(s._chat_turn("做方案"))
            for ans in ("答1-预算", "答2-时段"):
                ok = await _wait_until(lambda: s._human_input_request is not None)
                self.assertTrue(ok, "每次提问都应暂停等人")
                asked.append(s._human_input_request["question"])
                s.provide_human_input(ans)
                await _wait_until(lambda: s._human_input_request is None)
            await task

        asyncio.run(drive())

        self.assertEqual(asked, ["Q1 预算？", "Q2 时段？"], "两次提问都问到：一次发言问一个")
        self.assertEqual(len(rt.prompts), 3, "起问一轮 + 每条回答各续跑一轮")
        # 关键：回复轮 prompt 每次只带**这一条**回答，不是两条一起灌
        self.assertIn("答1-预算", rt.prompts[1])
        self.assertNotIn("答2-时段", rt.prompts[1], "第一条回复里不该出现第二条回答")
        self.assertIn("答2-时段", rt.prompts[2])
        self.assertNotIn("1) 答1", "\n".join(rt.prompts), "不应拼成编号列表一次灌回")

    def test_duplicate_asks_in_one_turn_keep_only_last(self):
        """极端情况：某次发言真出现两次提问 → 只问最后一个，绝不排队到下一轮。

        排队会让「上一轮遗留的问题」在下一轮突然冒出来问，破坏一问一答的来回结构。
        """
        a = models.AgentConfig(name="A", type="codex")
        s = orch.Session(self.proj, self.sid, [a], 4, Recorder(), timeout=300)
        rt = _ScriptedRuntime([
            [("text", "一次说了两个问题（异常）。"),
             _ask("Q1"), _ask("Q2"), ("done", "")],
            [("text", "结论：收尾。"), ("done", "")],
        ])
        s.runtimes[a.name] = rt

        asked: list[str] = []

        async def drive():
            task = asyncio.create_task(s._chat_turn("做方案"))
            ok = await _wait_until(lambda: s._human_input_request is not None)
            self.assertTrue(ok)
            asked.append(s._human_input_request["question"])
            s.provide_human_input("答")
            await task

        asyncio.run(drive())
        self.assertEqual(asked, ["Q2"], "只问最后一个，Q1 不排队下次再问")
        self.assertEqual(len(rt.prompts), 2)


class TestDebateMultiAskHandoff(unittest.TestCase):
    """debate 端到端：A 连续两次提问后才给结论；交给 B 的只有「起问发言 + 结论」。

    这是需求第三条的直接验证——对方 agent 不需要知道 A 与用户之间的来回交互过程，
    连用户回答原文也不进对方 delta（human 消息本就被 _others_since_last 排除）。
    """

    def setUp(self):
        self.proj = "/tmp/agentchat_test_multiask_debate_" + str(id(self))
        self.sid = store.create_session(self.proj, "辩论多次提问测试")["id"]

    def test_peer_sees_only_start_and_conclusion(self):
        a1 = models.AgentConfig(name="A", type="codex")
        a2 = models.AgentConfig(name="B", type="codex")
        s = orch.Session(self.proj, self.sid, [a1, a2], 1, Recorder(), timeout=300)
        rt_a = _ScriptedRuntime([
            [("text", "观点甲：先问用户频率。"), _ask("每周几次？"), ("done", "")],
            [("text", "过程中：还想确认时段。"), _ask("偏好时段？"), ("done", "")],
            [("text", "结论：按答A与答B执行。"), ("done", "")],
        ])
        rt_b = _ScriptedRuntime([[("text", "B 收到结论。"), ("done", "")]])
        s.runtimes[a1.name] = rt_a
        s.runtimes[a2.name] = rt_b

        async def drive():
            task = asyncio.create_task(s._debate("设计训练计划"))
            self.assertTrue(await _wait_until(
                lambda: (s._human_input_request or {}).get("question") == "每周几次？"))
            s.provide_human_input("答A-每周四次")
            self.assertTrue(await _wait_until(
                lambda: (s._human_input_request or {}).get("question") == "偏好时段？"))
            s.provide_human_input("答B-早上")
            await task

        asyncio.run(drive())

        # A 的三次发言：起问 / 交互过程（又问一次） / 结论
        msgs_a = [m for m in s.history if m.get("speaker") == "A"]
        self.assertEqual(len(msgs_a), 3)
        self.assertTrue(msgs_a[1].get("interim"), "又问一次的那条应标为交互过程")
        # 轮到 B 时（round_i 由 0 增到 1），它拿到的 delta 里应有 A 的观点与结论
        b_prompt = rt_b.prompts[0]
        self.assertIn("观点甲", b_prompt)
        self.assertIn("结论：按答A与答B执行。", b_prompt)
        # …但不含 A 与用户的来回交互过程，也不含用户回答原文
        self.assertNotIn("过程中：还想确认时段。", b_prompt, "交互过程发言不应转述给对方")
        self.assertNotIn("答A-每周四次", b_prompt, "用户回答不应进对方 delta")
        self.assertNotIn("答B-早上", b_prompt, "用户回答不应进对方 delta")


if __name__ == "__main__":
    unittest.main()
