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


class TestMcpToolCallNaming(unittest.IsolatedAsyncioTestCase):
    """codex 的 MCP 工具调用条目在两条输出通路上命名不同，必须都认。

    2026-09-15 实测：app-server（本 app 默认走的通路）发 `mcpToolCall`（驼峰），
    exec / rollout 发 `mcp_tool_call`（下划线）。原实现只判下划线 ⇒ app-server 下
    codex 的 end_discussion 投票**实时全丢**（s_f85043cf 实测 2 票都没进状态机，
    阈值 ⌊2/2⌋+1=2 永不满足，辩论只能靠轮次耗尽收尾）。
    """

    def _item(self, itype):
        return {"type": itype, "id": "call_1", "server": "terminate-signal",
                "tool": "end_discussion", "status": "completed",
                "arguments": {"reason": "双方一致"}}

    async def _collect(self, notifications):
        """驱动 app-server 主循环：跳过握手与残留清理，直接喂通知。"""
        rt = _rt()

        async def _noop_ensure(cwd):
            return None

        async def _noop_drain():
            return None

        rt._ensure_appserver = _noop_ensure
        rt._as_drain_stale = _noop_drain
        rt._appserver_thread_id = "th_1"
        rt._as_queue = asyncio.Queue()   # 真实实现里由 _ensure_appserver 建立，这里跳过握手故自建
        for o in notifications:
            await rt._as_queue.put(o)
        return [ev async for ev in rt._run_codex_appserver("hi", "/tmp", 5)]

    def test_item_type_predicate_accepts_both_namings(self):
        self.assertTrue(executor._is_mcp_tool_item({"type": "mcpToolCall"}))
        self.assertTrue(executor._is_mcp_tool_item({"type": "mcp_tool_call"}))
        self.assertFalse(executor._is_mcp_tool_item({"type": "reasoning"}))
        self.assertFalse(executor._is_mcp_tool_item(None))

    async def test_appserver_camelcase_emits_tool_use(self):
        # item/started + item/completed 都带同一个 mcpToolCall：只应产一次 tool_use（不重复投票）
        evs = await self._collect([
            {"method": "item/started", "params": {"item": self._item("mcpToolCall")}},
            {"method": "item/completed", "params": {"item": self._item("mcpToolCall")}},
            {"method": "turn/completed", "params": {}},
        ])
        tools = [e for e in evs if e[0] == "tool_use"]
        self.assertEqual(len(tools), 1, "完成通知只应产一次 tool_use")
        self.assertEqual(tools[0][1]["name"], "end_discussion")
        self.assertEqual(tools[0][1]["input"], {"reason": "双方一致"})

    async def test_appserver_snakecase_still_works(self):
        evs = await self._collect([
            {"method": "item/completed", "params": {"item": self._item("mcp_tool_call")}},
            {"method": "turn/completed", "params": {}},
        ])
        tools = [e for e in evs if e[0] == "tool_use"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0][1]["name"], "end_discussion")

    async def test_appserver_other_mcp_tool_ignored(self):
        # 非 end_discussion 的 MCP 调用（如内置工具）不产 tool_use，也不打断流式
        evs = await self._collect([
            {"method": "item/completed",
             "params": {"item": {"type": "mcpToolCall", "server": "x", "tool": "read_file",
                                 "arguments": {}}}},
            {"method": "turn/completed", "params": {}},
        ])
        self.assertEqual([e for e in evs if e[0] == "tool_use"], [])

    def test_exec_path_parses_both_namings(self):
        """exec（`codex exec --json`）/ rollout 回放走 _parse_codex_line，同样兼容两种命名。"""
        want = [("tool_use", {"name": "end_discussion", "input": {"reason": "双方一致"}})]
        for itype in ("mcpToolCall", "mcp_tool_call"):
            ev = {"type": "item.completed", "item": self._item(itype)}
            self.assertEqual(executor._parse_codex_line(json.dumps(ev), None), want, itype)


class TestCodexNativeToolItems(unittest.IsolatedAsyncioTestCase):
    """codex **原生**工具（自带 shell / 打补丁）在 app-server 通路也必须被捕获。

    2026-09-16 实测（codex-cli 0.146.1）：`codex exec --json` 放开沙箱后
    `item.type="command_execution"`、`command="/bin/bash -lc 'echo codex-tool-test'"`、
    `aggregated_output="codex-tool-test\\n"`、`exit_code=0`；app-server 通路同一工具用驼峰
    `commandExecution`。原先两者都被整条忽略 ⇒ codex 调了自带工具也界面空白
    （test5 实测：claude 调 Bash 有痕、codex 侧空白，即此因）。
    """

    async def _collect(self, notifications):
        rt = _rt()

        async def _noop_ensure(cwd):
            return None

        async def _noop_drain():
            return None

        rt._ensure_appserver = _noop_ensure
        rt._as_drain_stale = _noop_drain
        rt._appserver_thread_id = "th_1"
        rt._as_queue = asyncio.Queue()
        for o in notifications:
            await rt._as_queue.put(o)
        return [ev async for ev in rt._run_codex_appserver("hi", "/tmp", 5)]

    def _cmd_item(self, itype):
        return {"id": "item_1", "type": itype,
                "command": "/bin/bash -lc 'echo codex-tool-test'",
                "aggregated_output": "codex-tool-test\n", "exit_code": 0}

    async def test_camelcase_command_emits_tool_use_once(self):
        evs = await self._collect([
            {"method": "item/started", "params": {"item": self._cmd_item("commandExecution")}},
            {"method": "item/completed", "params": {"item": self._cmd_item("commandExecution")}},
            {"method": "turn/completed", "params": {}},
        ])
        tools = [e for e in evs if e[0] == "tool_use"]
        self.assertEqual(len(tools), 1, "started/completed 成对上报，只能显示一次")
        self.assertEqual(tools[0][1]["name"], "exec_command")
        self.assertEqual(tools[0][1]["input"]["command"], "/bin/bash -lc 'echo codex-tool-test'")
        self.assertEqual(tools[0][1]["input"]["exit_code"], 0)
        # 同一 item 的输出也要补发 tool_result（否则「只有调用、没有结果」）
        trs = [e for e in evs if e[0] == "tool_result"]
        self.assertEqual(len(trs), 1, evs)
        self.assertEqual(trs[0][1], {"is_error": False, "content": "codex-tool-test"})

    async def test_camelcase_output_fields_emit_tool_result(self):
        """app-server 通路的输出字段是驼峰（aggregatedOutput / exitCode），也要认。"""
        item = {"id": "item_9", "type": "commandExecution", "command": "echo hi",
                "aggregatedOutput": "hi\n", "exitCode": 0}
        evs = await self._collect([
            {"method": "item/completed", "params": {"item": item}},
            {"method": "turn/completed", "params": {}},
        ])
        trs = [e for e in evs if e[0] == "tool_result"]
        self.assertEqual(len(trs), 1, evs)
        self.assertEqual(trs[0][1]["content"], "hi")

    async def test_failed_command_marks_tool_result_error(self):
        """exitCode 非 0 ⇒ is_error=True（前端按失败样式渲染 + 编排层标 exec_ok=false）。"""
        item = {"id": "item_10", "type": "commandExecution", "command": "false",
                "aggregatedOutput": "boom", "exitCode": 1}
        evs = await self._collect([
            {"method": "item/completed", "params": {"item": item}},
            {"method": "turn/completed", "params": {}},
        ])
        trs = [e for e in evs if e[0] == "tool_result"]
        self.assertEqual(len(trs), 1)
        self.assertTrue(trs[0][1]["is_error"])
        self.assertEqual(trs[0][1]["content"], "boom")

    async def test_snakecase_command_emits_tool_use(self):
        evs = await self._collect([
            {"method": "item/completed", "params": {"item": self._cmd_item("command_execution")}},
            {"method": "turn/completed", "params": {}},
        ])
        tools = [e for e in evs if e[0] == "tool_use"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0][1]["name"], "exec_command")

    async def test_file_change_emits_tool_use(self):
        evs = await self._collect([
            {"method": "item/completed",
             "params": {"item": {"type": "fileChange", "patch": "*** Begin Patch\n*** End Patch"}}},
            {"method": "turn/completed", "params": {}},
        ])
        tools = [e for e in evs if e[0] == "tool_use"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0][1]["name"], "apply_patch")
        # 打补丁条目这条样例没有输出 ⇒ 不产空 tool_result 块
        self.assertEqual([e for e in evs if e[0] == "tool_result"], [])

    async def test_unknown_item_type_still_ignored(self):
        """未识别的 item 类型不能被当成工具（否则界面会冒出一堆假调用）。"""
        evs = await self._collect([
            {"method": "item/completed", "params": {"item": {"type": "web_search_call"}}},
            {"method": "turn/completed", "params": {}},
        ])
        self.assertEqual([e for e in evs if e[0] == "tool_use"], [])
        self.assertEqual([e for e in evs if e[0] == "tool_result"], [])


if __name__ == "__main__":
    unittest.main()
