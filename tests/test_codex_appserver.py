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
import unittest.mock

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
    """三级「思考」开关生效值 False → codex 注入最低 reasoning effort（best-effort，见 FR1.1）。"""

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


class _KillableProc:
    """只用于 _kill_proc_group 的假进程：记录 kill() 调用，可指定 returncode。"""

    def __init__(self, pid=4242, returncode=None):
        self.pid = pid
        self.returncode = returncode
        self.killed = False

    def kill(self):
        self.killed = True


class TestKillProcGroup(unittest.TestCase):
    """子进程整组回收：codex 是「node 壳 → 原生 app-server 子进程」两层，只杀壳会留孤儿。

    2026-09-14 实测：壳 49585 → 原生子 49586；只 kill 壳后 49586 仍存活（PPID=1），
    仍持有 thread 与管道 → 进程泄漏。故 spawn 一律 start_new_session=True + killpg 整组杀。
    """

    def test_group_kill_reaps_child_processes(self):
        """真实进程：整组杀必须连子进程一起带走（这是泄漏的直接修复）。"""
        import os
        import signal as _sig
        import subprocess
        import time as _t

        proc = subprocess.Popen(["/bin/sh", "-c", "sleep 30 & wait"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            pgid = os.getpgid(proc.pid)
            self.assertEqual(pgid, proc.pid, "start_new_session=True 应使子进程自成进程组")
            _t.sleep(0.4)
            in_group = subprocess.run(["pgrep", "-g", str(pgid)],
                                      capture_output=True, text=True).stdout.split()
            self.assertGreaterEqual(len(in_group), 2, "应有壳 + sleep 子进程同组")
            self.assertIn(str(proc.pid), in_group)
            executor._kill_proc_group(proc)
            proc.wait(timeout=5)
            left = subprocess.run(["pgrep", "-g", str(pgid)],
                                  capture_output=True, text=True).stdout.split()
            self.assertEqual(left, [], f"整组杀后不应有残留进程，实际 {left}")
        finally:
            if proc.returncode is None:
                try:
                    os.killpg(os.getpgid(proc.pid), _sig.SIGKILL)
                except Exception:
                    pass
                proc.wait(timeout=5)

    def test_noop_when_process_already_exited(self):
        p = _KillableProc(returncode=0)
        with unittest.mock.patch.object(executor.os, "killpg") as kp:
            executor._kill_proc_group(p)
        kp.assert_not_called()
        self.assertFalse(p.killed, "已退出进程不应再杀")

    def test_falls_back_to_single_kill_when_no_pgid(self):
        p = _KillableProc()
        with unittest.mock.patch.object(executor.os, "getpgid", side_effect=OSError("no pgid")), \
                unittest.mock.patch.object(executor.os, "killpg") as kp:
            executor._kill_proc_group(p, grace=0)
        kp.assert_not_called()
        self.assertTrue(p.killed, "拿不到 pgid 时应退回单体 kill")

    def test_never_kills_own_process_group(self):
        """子进程若与本服务同组（start_new_session 未生效），绝不能 killpg——会杀掉自己。"""
        own = executor.os.getpgid(0)
        p = _KillableProc()
        with unittest.mock.patch.object(executor.os, "getpgid", return_value=own), \
                unittest.mock.patch.object(executor.os, "killpg") as kp:
            executor._kill_proc_group(p, grace=0)
        kp.assert_not_called()
        self.assertTrue(p.killed, "同组时应退回单体 kill，而非整组杀")

    def test_eof_detail_includes_stderr_tail_and_returncode(self):
        rt = _rt()
        rt._as_returncode = 1
        rt._as_stderr_tail = ["", "panic: sqlite busy", "thread xyz"]
        d = rt._as_eof_detail()
        self.assertIn("退出码 1", d)
        self.assertIn("panic: sqlite busy", d)
        # 无信息时不产生空括号噪音
        rt2 = _rt()
        self.assertEqual(rt2._as_eof_detail(), "")

    def test_kill_appserver_resets_diagnostics(self):
        rt = _rt()
        rt._as_stderr_tail = ["boom"]
        rt._as_returncode = 137
        rt._appserver_proc = None
        rt._kill_appserver()
        self.assertEqual(rt._as_stderr_tail, [])
        self.assertIsNone(rt._as_returncode)


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
