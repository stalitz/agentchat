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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.base import models, orch, store  # noqa: E402
from backend.executor import _guarded_mcp_tool, _parse_codex_line  # noqa: E402
from backend.orchestrator import _task_needs_human_choice  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
