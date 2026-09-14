"""codex app-server 真流式改造的单元测试。

覆盖：
- 审批类 ServerRequest 的自动应答（纯函数，无需真实进程）；
- _as_send 在进程缺失时的安全 no-op；
- run() 握手失败时自动回退 codex exec（兜底路径）。

真流式端到端（initialize→thread/start→turn/start→delta）依赖真实 codex 二进制，
由手动探针验证（见 docs/开发进度.md 任务 #105 记录），不在 CI 内跑。
"""
import asyncio
import json
import unittest

from backend import executor
from backend.executor import AgentRuntime, _CodexAppServerUnavailable
from backend.models import AgentConfig


class _FakeStdin:
    def __init__(self):
        self.writes = []

    def write(self, b):
        self.writes.append(b)


class _FakeProc:
    def __init__(self):
        self.stdin = _FakeStdin()
        self.returncode = None


def _rt(**kw):
    return AgentRuntime(AgentConfig(name="C", type="codex", **kw))


class TestAppServerAutoApprove(unittest.TestCase):
    def _captured(self, rt, method, rid=7, params=None):
        rt._as_autoanswer(method, rid, params or {})
        raw = rt._appserver_proc.stdin.writes[-1]
        return json.loads(raw.decode("utf-8"))

    def test_exec_approval(self):
        rt = _rt(); rt._appserver_proc = _FakeProc()
        out = self._captured(rt, "execCommandApproval")
        self.assertEqual(out["id"], 7)
        self.assertEqual(out["result"]["decision"], "accept")

    def test_apply_patch_approval(self):
        rt = _rt(); rt._appserver_proc = _FakeProc()
        out = self._captured(rt, "applyPatchApproval")
        self.assertEqual(out["result"]["decision"], "approved")

    def test_elicitation_accept(self):
        rt = _rt(); rt._appserver_proc = _FakeProc()
        out = self._captured(rt, "mcpServer/elicitation/request")
        self.assertEqual(out["result"]["action"], "accept")

    def test_user_input_empty(self):
        rt = _rt(); rt._appserver_proc = _FakeProc()
        out = self._captured(rt, "item/tool/requestUserInput")
        self.assertEqual(out["result"]["answers"], {})

    def test_permissions_echo(self):
        rt = _rt(); rt._appserver_proc = _FakeProc()
        out = self._captured(rt, "item/permissions/requestApproval",
                             params={"permissions": {"fileSystem": {"read": "allow"}}})
        self.assertEqual(out["result"]["scope"], "session")
        self.assertEqual(out["result"]["permissions"]["fileSystem"]["read"], "allow")

    def test_dynamic_tool_call_empty_success(self):
        rt = _rt(); rt._appserver_proc = _FakeProc()
        out = self._captured(rt, "item/tool/call")
        self.assertEqual(out["result"]["success"], True)
        self.assertEqual(out["result"]["contentItems"], [])

    def test_unknown_method_not_handled(self):
        rt = _rt(); rt._appserver_proc = _FakeProc()
        handled = rt._as_autoanswer("some/unknown", 9, {})
        self.assertFalse(handled)
        self.assertEqual(len(rt._appserver_proc.stdin.writes), 0)


class TestAppServerSendSafety(unittest.TestCase):
    def test_send_noop_when_proc_none(self):
        rt = _rt()
        rt._appserver_proc = None
        # 不应抛异常
        rt._as_send({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        rt._next_id()

    def test_send_noop_when_stdin_none(self):
        rt = _rt()
        proc = _FakeProc(); proc.stdin = None
        rt._appserver_proc = proc
        rt._as_send({"jsonrpc": "2.0"})


class TestCodexThinkSwitch(unittest.TestCase):
    """三级「思考」开关生效值 False → codex 注入最低 reasoning effort（best-effort，见 FR13）。"""

    def _cmd(self, think):
        rt = AgentRuntime(AgentConfig(name="C", type="codex", model="deepseek-v4-flash"), think=think)
        return rt._codex_cmd("hi")

    def test_think_false_injects_reasoning_effort(self):
        cmd = self._cmd(False)
        self.assertIn('model_reasoning_effort="low"', cmd, "think=False 应经 -c 降 reasoning effort")
        self.assertIn("-c", cmd)

    def test_think_true_or_none_no_injection(self):
        self.assertNotIn('model_reasoning_effort="low"', self._cmd(True))
        self.assertNotIn('model_reasoning_effort="low"', self._cmd(None),
                         "think=None（未指定）不应注入")


class TestCodexFallback(unittest.IsolatedAsyncioTestCase):
    async def test_run_falls_back_to_exec_on_handshake_failure(self):
        rt = _rt(model="deepseek-v4-flash")

        async def _boom(*a, **k):
            raise _CodexAppServerUnavailable("app-server 不可用")

        rt._ensure_appserver = _boom

        captured = []

        async def _fake_exec(prompt, cwd, timeout):
            yield ("text", "EXEC_FALLBACK")
            captured.append(prompt)

        rt._run_codex = _fake_exec
        out = [ev async for ev in rt.run("你好", "/tmp", 30)]
        self.assertEqual(out, [("text", "EXEC_FALLBACK")])
        self.assertEqual(captured, ["你好"])

    async def test_next_id_increments(self):
        rt = _rt()
        rt._as_req_id = 100
        self.assertEqual(rt._next_id(), 101)
        self.assertEqual(rt._next_id(), 102)

    async def test_kill_appserver_safe_when_none(self):
        rt = _rt()
        rt._appserver_proc = None
        rt._kill_appserver()  # 不应抛
        rt.aclose()


if __name__ == "__main__":
    unittest.main()
