# -*- coding: utf-8 -*-
"""回归测试：人工介入（HITL, request_human_input）链路（2026-09-19 修的两个问题）。

问题 1（codex 上报断链 · 真 bug）
    executor 的两条 codex 通路——app-server 实时 `item/completed`、`codex exec` 与 rollout
    兜底的 `item.completed`——判定受控 MCP 工具时都只写 `"end_discussion" in tool`，
    `request_human_input` 掉进 else 分支被 `_codex_native_tool_use` 判 None 吞掉
    ⇒ codex 即便真的调了，编排层也收不到、不会暂停（界面上只看到它在正文里空问）。
    修法：受控工具判定集中在 `_guarded_mcp_tool`（与 `_is_mcp_tool_item` 同处），两条通路共用。

问题 2（提示词是软条件 · 模型行为问题）
    旧措辞「当你需要用户从多个方案中选择时」被 gemma 一律理解成「把方案摆出来即可」，
    于是直接在正文里反问；而辩论里正文反问会被**对方 agent** 接过去回答，用户根本看不到
    （实测：agent 连续三轮「等用户回复」，实际是自说自话）。
    修法：硬触发条件 + 明令禁止正文反问 + 议题要用户拍板时先问后答
    （prompt_defaults 新增 hitl_chat / hitl_debate / hitl_opening 三槽位）。

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
from backend.executor import _guarded_mcp_tool, _parse_codex_line, AgentRuntime  # noqa: E402
from backend.orchestrator import _task_needs_human_choice, _codex_answers_from_questions  # noqa: E402


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
        self.assertEqual(_guarded_mcp_tool(_mcp_item("request_human_input", {"question": "q"})),
                         "request_human_input")

    def test_mcp_prefixed_name_normalized(self):
        # codex 侧可能带上 MCP 前缀（网关还原后的形态）
        self.assertEqual(
            _guarded_mcp_tool(_mcp_item("mcp__terminate-signal__request_human_input", {})),
            "request_human_input")

    def test_app_server_camel_case_item_type(self):
        # app-server 通路 item.type 是驼峰 mcpToolCall
        self.assertEqual(
            _guarded_mcp_tool(_mcp_item("request_human_input", {}, type_="mcpToolCall")),
            "request_human_input")

    def test_other_tools_not_guarded(self):
        self.assertIsNone(_guarded_mcp_tool(_mcp_item("some_other_tool", {})))
        self.assertIsNone(_guarded_mcp_tool({"type": "command_execution"}))
        self.assertIsNone(_guarded_mcp_tool(None))


class TestCodexParseHumanInput(unittest.TestCase):
    """_parse_codex_line（exec / rollout 通路）：request_human_input 必须产出 tool_use。"""

    def test_human_input_yields_tool_use(self):
        line = ('{"type":"item.completed","item":{"id":"i1","type":"mcp_tool_call",'
                '"server":"terminate-signal","tool":"request_human_input",'
                '"arguments":{"question":"你的训练频率？","options":["每周2次","每周4次"]}}}')
        out = _parse_codex_line(line, None)
        self.assertEqual(len(out), 1, f"应产出一条 tool_use，实际 {out}")
        kind, payload = out[0]
        self.assertEqual(kind, "tool_use")
        self.assertEqual(payload["name"], "request_human_input")
        self.assertEqual(payload["input"]["question"], "你的训练频率？")
        self.assertEqual(payload["input"]["options"], ["每周2次", "每周4次"])

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
                '"tool":"request_human_input","arguments":{}}}')
        self.assertEqual(_parse_codex_line(line, None), [])


class TestHitlPrompt(unittest.TestCase):
    """提示词：硬触发条件 / 禁止正文反问 / 议题要拍板时先问后答。"""

    def setUp(self):
        self.proj = "/tmp/agentchat_test_hitl_" + str(id(self))
        self.sid = store.create_session(self.proj, "HITL 测试会话")["id"]

    def _session(self, agents, max_rounds=6):
        s = orch.Session(self.proj, self.sid, agents, max_rounds, Recorder(), timeout=300)
        return s

    def test_task_hint_detection(self):
        self.assertTrue(_task_needs_human_choice("请给多套方案让我选"))
        self.assertTrue(_task_needs_human_choice("帮我选一个更合适的"))
        self.assertTrue(_task_needs_human_choice("Give me a few options to choose from"))
        self.assertFalse(_task_needs_human_choice("请介绍一下跳高训练的基本原理"))
        self.assertFalse(_task_needs_human_choice(""))

    def test_chat_prompt_has_hard_rule(self):
        a = models.AgentConfig(name="A", type="claude-code")
        s = self._session([a])
        p = s._build_prompt(a, "给我几套方案让我选", round_i=0)
        self.assertIn("request_human_input", p, "chat 提示词必须点出工具名")
        self.assertIn("禁止用正文反问代替工具调用", p, "必须明令禁止正文反问")
        self.assertIn("先问后答", p, "诉求要拍板时应注入「先问后答」强指令")

    def test_chat_prompt_no_opening_when_task_is_plain(self):
        a = models.AgentConfig(name="A", type="claude-code")
        s = self._session([a])
        p = s._build_prompt(a, "请介绍跳高训练原理", round_i=0)
        self.assertIn("request_human_input", p, "通用规则每轮都在")
        self.assertNotIn("先问后答", p, "普通议题不应强制先问")

    def test_debate_prompt_warns_peer_will_answer(self):
        a1 = models.AgentConfig(name="A", type="claude-code")
        a2 = models.AgentConfig(name="B", type="codex")
        s = self._session([a1, a2])
        s._initial_task = "请给多套跳高训练方案让我选"
        p = s._build_prompt(a1, "请给多套跳高训练方案让我选", round_i=0)
        self.assertIn("request_human_input", p)
        self.assertIn("对方 agent", p, "必须说明正文反问会被对方 agent 接走")
        self.assertIn("先问后答", p, "首个发言者（round_i=0）应先问后答")

    def test_debate_opening_only_for_first_speaker(self):
        # 第二个发言者（round_i=1）不再重复要求先问，否则每人都问一遍
        a1 = models.AgentConfig(name="A", type="claude-code")
        a2 = models.AgentConfig(name="B", type="codex")
        s = self._session([a1, a2])
        s._initial_task = "请给多套方案让我选"
        p = s._build_prompt(a2, None, round_i=1)
        self.assertIn("request_human_input", p)
        self.assertNotIn("先问后答", p)

    def test_no_opening_after_already_asked(self):
        # 本场已问过用户 → 不再注入强指令（避免反复追问）
        a = models.AgentConfig(name="A", type="claude-code")
        s = self._session([a])
        s._hitl_asked = True
        p = s._build_prompt(a, "给我几套方案让我选", round_i=0)
        self.assertIn("request_human_input", p)
        self.assertNotIn("先问后答", p)


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
    """原生通道 item/tool/requestUserInput：不再静默空应答，转交编排层真暂停等人。"""

    def test_read_loop_forwards_to_queue_not_autoanswer(self):
        # 读循环遇到 requestUserInput，应 put {"_human_input":...} 进队列，
        # 且不向 codex 回空应答（旧 bug：_as_autoanswer 回 {"answers":{}} 假确认）。
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
        # 队列应收到 _human_input 与 EOF（顺序不重要，取全部）
        got = []
        while True:
            try:
                got.append(rt._as_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        human = next((g for g in got if g.get("_human_input")), None)
        self.assertIsNotNone(human, "应将 requestUserInput 转交为 _human_input 事件")
        self.assertEqual(human["_human_input"]["rid"], "x1")
        self.assertEqual(human["_human_input"]["questions"][0]["questionId"], "q1")
        self.assertTrue(any(g.get("_eof") for g in got), "EOF 仍应正常到达")
        # 关键：读循环不得自动回空应答，否则 codex 会被假确认糊弄过去
        self.assertEqual(rt._appserver_proc.stdin.writes, [],
                         "requestUserInput 不应被自动空应答")

    def test_respond_writes_result_frame(self):
        # 人回答后，编排层应回 JSON-RPC result 解锁 codex 阻塞的 ServerRequest。
        rt = AgentRuntime(models.AgentConfig(name="A", type="codex"), None)
        rt._appserver_proc = _FakeProc([])
        rt._as_respond_human_input("x1", {"q1": "每周4次"})
        self.assertEqual(len(rt._appserver_proc.stdin.writes), 1)
        frame = json.loads(rt._appserver_proc.stdin.writes[0].decode().strip())
        self.assertEqual(frame["id"], "x1")
        self.assertEqual(frame["result"], {"answers": {"q1": "每周4次"}})


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
    """替代 AgentRuntime：run() 吐构造好的事件序列，记录 _as_respond_human_input 调用。"""
    def __init__(self, events):
        self._events = events
        self.respond_calls = []
        self.cli_session_id = None
    def run(self, prompt, project_path, timeout, meta=None):
        async def gen():
            for ev in self._events:
                yield ev
        return gen()
    def _as_respond_human_input(self, rid, answers):
        self.respond_calls.append((rid, answers))


class TestOrchestratorNativeHumanInput(unittest.TestCase):
    """编排层 _run_turn：收到 human_input 事件 → 广播暂停 → 人回答 → 回包解锁 → 续跑。"""

    def setUp(self):
        self.proj = "/tmp/agentchat_test_native_orch_" + str(id(self))
        self.sid = store.create_session(self.proj, "原生HITL编排测试")["id"]

    def _session(self, agents):
        return orch.Session(self.proj, self.sid, agents, 4, Recorder(), timeout=300)

    def test_native_channel_pauses_and_unlocks(self):
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
            # 等编排层进入暂停（_human_input_request 已设、事件已广播、正在 await）
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
        # 回包解锁 codex（原生通道答案走 result，而非续跑 prompt）
        self.assertEqual(rt.respond_calls, [("q1", {"q1": "每周4次"})],
                         "应回 JSON-RPC result 解锁 codex")
        self.assertIsNone(s._human_input_request, "处理后应清空标记")
        self.assertEqual(errors, [])
        self.assertIn("每周4次", full, "codex 解锁后续跑的回答应进入本轮正文")


if __name__ == "__main__":
    unittest.main()
