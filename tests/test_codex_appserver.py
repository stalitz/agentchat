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
import os
import tempfile
import unittest
import unittest.mock

from backend import executor
from backend.executor import AgentRuntime, _CodexAppServerUnavailable
from backend.models import AgentConfig


def _write_rollout(lines):
    """写一个 rollout 文件（建在 /tmp：本机沙箱禁止在 /private/var/folders/... 建目录）。"""
    fd, path = tempfile.mkstemp(suffix=".jsonl", prefix="agentchat_rollout_", dir="/tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(x, ensure_ascii=False) for x in lines))
    return path


def _append_rollout(path, lines):
    """回合进行中追加记录（真实 rollout 是随事件落盘的，不是回合末一次性写）。"""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n" + "\n".join(json.dumps(x, ensure_ascii=False) for x in lines))


def _rollout_file(reasoning_texts):
    """写一个**真实 schema** 的 rollout（reasoning 明文放 summary，见 codex 0.146.1）。"""
    lines = [{"type": "session_meta", "payload": {"session_id": "tid-1", "cli_version": "0.146.1"}}]
    for i, t in enumerate(reasoning_texts, 1):
        lines.append({"type": "response_item",
                      "payload": {"type": "reasoning", "id": f"rs_{i}", "content": None,
                                  "summary": [{"type": "summary_text", "text": t}]}})
    return _write_rollout(lines)


class _FakeStdin:
    def __init__(self):
        self.writes = []

    def write(self, b):
        self.writes.append(b)


class _FakeProc:
    def __init__(self, lines=None):
        self._lines = list(lines or [])
        self.stdout = self
        self.stderr = None
        self.stdin = _FakeStdin()
        self.returncode = None
    async def readline(self):
        if self._lines:
            return (self._lines.pop(0) + "\n").encode()
        return b""


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

    def test_user_input_immediately_ack_and_forward(self):
        # 原生 HITL 通道 item/tool/requestUserInput（codex 内建 ask-user）兜底转
        # request_human_input 语义：读循环**立即回空 result**让 codex 本轮结束（不卡），
        # 同时把请求转交编排层走 human_input_required 真暂停——用户回答经 1:1 续接续发，
        # 而非回 result 解锁（否则 codex 会阻塞等人）。
        rt = _rt()
        rt._appserver_proc = _FakeProc([
            json.dumps({"jsonrpc": "2.0", "method": "item/tool/requestUserInput",
                        "id": 7, "params": {"questions": [
                            {"questionId": "q1", "question": "每周几次？",
                             "options": ["2次", "4次"]}]}}),
        ])
        rt._as_queue = asyncio.Queue()
        async def run():
            await rt._as_read_loop()
        asyncio.run(run())
        # 立即回空 result（codex 那轮结束，不卡）
        self.assertEqual(len(rt._appserver_proc.stdin.writes), 1, "应向 codex 立即回 1 帧 result")
        frame = json.loads(rt._appserver_proc.stdin.writes[0].decode().strip())
        self.assertEqual(frame["id"], 7)
        self.assertEqual(frame["result"], {"answers": {}})
        # 同时转交 HITL 给编排层（requestUserInput → request_human_input 语义兜底）
        got = []
        while True:
            try:
                got.append(rt._as_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        self.assertTrue(any(g.get("_human_input") for g in got), "应转交 _human_input 给编排层")

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


class TestCodexReasoningLiveItems(unittest.IsolatedAsyncioTestCase):
    """codex 的「思考」item 必须**按位实时**产出，思考才会与工具调用按原生顺序交错。

    2026-09-17 实测（s_5a0f8d10）：思考落盘已修好，但**流式期间是一整块、位置落在轮末**，
    要等会话重开（走 rollout 回放）才按工具调用分块。原因：`item/completed` 只认
    mcpToolCall/原生工具，`reasoning` 整类被忽略 ⇒ 只能靠回合末的 rollout 兜底补，
    而兜底是「一次性追加在所有正文/工具之后」⇒ 前端把连续同类型合并成一块。

    协议事实（`codex app-server generate-ts`，勿再猜）：
    - ThreadItem 的 reasoning 变体 = `{type:"reasoning", id, summary: string[], content: string[]}`
      —— 元素是**纯字符串**（rollout 里才是 `[{type:"summary_text", text}]` 字典形态）；
    - `item/reasoning/textDelta` / `summaryTextDelta` 通知带 `itemId` ⇒ 可据此避免与
      item/completed 重复产出同一段思考。
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
        return rt, [ev async for ev in rt._run_codex_appserver("hi", "/tmp", 5)]

    @staticmethod
    def _reasoning(item_id, text):
        return {"method": "item/completed",
                "params": {"item": {"type": "reasoning", "id": item_id,
                                    "summary": [text], "content": []}}}

    @staticmethod
    def _cmd(cmd_id, cmd):
        return {"method": "item/completed",
                "params": {"item": {"id": cmd_id, "type": "commandExecution", "command": cmd,
                                    "aggregatedOutput": "ok", "exitCode": 0}}}

    async def test_reasoning_item_yields_thinking_in_place(self):
        """思考 → 工具 → 思考：事件顺序必须是 thinking, tool_use, thinking（即分块交错）。"""
        rt, evs = await self._collect([
            self._reasoning("r1", "先看看有哪些工具"),
            self._cmd("c1", "ls"),
            self._reasoning("r2", "统计目录大小"),
            {"method": "turn/completed", "params": {}},
        ])
        kinds = [e[0] for e in evs]
        self.assertEqual(
            kinds,
            ["thinking", "tool_use", "tool_result", "thinking", "done"],
            "思考必须按位交错，不能挤到轮末：" + repr(evs),
        )
        self.assertEqual(evs[0][1], "先看看有哪些工具")
        self.assertEqual(evs[3][1], "统计目录大小")
        # 兜底游标不在此自增：它已在**回合开始时**按 rollout 现状取快照
        # （见 _codex_reasoning_cursor / TestCodexReasoningFallbackCursor），
        # 本测试无 rollout（cli_session_id=None）故快照为 0。
        self.assertEqual(rt._as_reasoning_seen, 0)

    async def test_content_array_form_also_read(self):
        """content 有值时优先取 content（元素同样是纯字符串）。"""
        rt, evs = await self._collect([
            {"method": "item/completed",
             "params": {"item": {"type": "reasoning", "id": "r1",
                                 "summary": ["摘要"], "content": ["正文思考"]}}},
            {"method": "turn/completed", "params": {}},
        ])
        self.assertEqual([e for e in evs if e[0] == "thinking"], [("thinking", "正文思考")])

    async def test_delta_then_completed_not_duplicated(self):
        """增量已流式吐过同一 item ⇒ 完成通知不得再吐一遍（否则思考出现两次）。"""
        rt, evs = await self._collect([
            {"method": "item/reasoning/textDelta",
             "params": {"itemId": "r1", "delta": "想一下"}},
            self._reasoning("r1", "想一下"),
            {"method": "turn/completed", "params": {}},
        ])
        self.assertEqual([e for e in evs if e[0] == "thinking"], [("thinking", "想一下")])

    async def test_reasoning_without_text_is_not_a_tool(self):
        """空 reasoning item 不得被当成工具条目（否则界面冒假调用）。"""
        rt, evs = await self._collect([
            {"method": "item/completed",
             "params": {"item": {"type": "reasoning", "id": "r1", "summary": [], "content": []}}},
            {"method": "turn/completed", "params": {}},
        ])
        self.assertEqual([e for e in evs if e[0] in ("tool_use", "tool_result")], [])

    async def test_end_discussion_mcp_still_works_alongside_reasoning(self):
        """新分支不得挡住原有 MCP 投票与原生工具捕获。"""
        rt, evs = await self._collect([
            self._reasoning("r1", "该收尾了"),
            {"method": "item/completed",
             "params": {"item": {"type": "mcpToolCall", "id": "m1", "server": "terminate-signal",
                                 "tool": "end_discussion", "arguments": {"reason": "一致"}}}},
            self._cmd("c1", "echo hi"),
            {"method": "turn/completed", "params": {}},
        ])
        tools = [e[1]["name"] for e in evs if e[0] == "tool_use"]
        self.assertEqual(tools, ["end_discussion", "exec_command"], evs)

    def test_rollout_dict_form_still_supported(self):
        """rollout / exec 的字典形态不能被新实现破坏（同一函数三通路共用）。"""
        self.assertEqual(
            executor._codex_reasoning_item_texts(
                {"content": [{"type": "reasoning_text", "text": "A"}], "summary": []}),
            ["A"])
        self.assertEqual(
            executor._codex_reasoning_item_texts({"content": None,
                                                  "summary": [{"type": "summary_text",
                                                               "text": "B"}]}),
            ["B"])
        # 纯字符串整体传入（防御性兜底）不得被逐字符拆开
        self.assertEqual(executor._codex_reasoning_item_texts({"summary": "C"}), ["C"])


class TestCodexReasoningFallbackCursor(unittest.IsolatedAsyncioTestCase):
    """回合末「无 reasoning 增量 ⇒ 从 rollout 兜底」的游标，必须以**回合开始时 rollout 现状**为基准。

    真实故障（2026-09-17 实测 agent-probes `s_89d54f21`，双壳本地 gemma）：游标初值 0 且只活在
    AgentRuntime 实例里 ⇒ runtime 重建后（「继续」/新 run/重开会话），本回合若没收到 reasoning
    增量（aborted 轮实测常如此），回合末兜底 `texts[0:]` 把**整条 rollout 的历史思考**当本轮思考
    吐出：落盘 msg8/msg17/msg22 思考 = 17577/19462/19462 字，正好是 rollout 前 16/20/20 条
    reasoning 明文之和，且 msg22 与 msg17 **逐字节相同**（该 rollout 真实切分是 10887/6718/0/1891）。
    """

    def setUp(self):
        self.texts = ["思考一：先看目录。", "思考二：统计大小。", "思考三：写下结论。"]
        self.rollout = _rollout_file(self.texts)

    def tearDown(self):
        try:
            os.unlink(self.rollout)
        except OSError:
            pass

    def _rt(self):
        rt = _rt()
        rt.cli_session_id = "01a0aef5-c719-7fa1-bff6-2cfdb31e4cd7"
        return rt

    def test_cursor_counts_existing_reasoning(self):
        rt = self._rt()
        with unittest.mock.patch.object(executor, "_codex_transcript_path", return_value=self.rollout):
            self.assertEqual(rt._codex_reasoning_cursor(), 3)
            # 快照之后没有新条目 ⇒ 兜底为空（修复前这里会从 0 起、把三条历史思考全部重放）
            self.assertEqual(rt._codex_new_reasoning(rt._codex_reasoning_cursor()), [])
            self.assertEqual(rt._codex_new_reasoning(0), self.texts, "旧行为：从 0 起就是整条历史")

    def test_cursor_is_zero_without_cli_session(self):
        rt = _rt()
        self.assertEqual(rt._codex_reasoning_cursor(), 0)

    async def _collect(self, rt, notifications):
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

    async def test_turn_end_fallback_does_not_replay_history(self):
        """本回合没有任何思考增量 ⇒ 兜底必须是空的，不能把历史思考当本轮思考吐出来。"""
        rt = self._rt()
        with unittest.mock.patch.object(executor, "_codex_transcript_path", return_value=self.rollout):
            evs = await self._collect(rt, [
                {"method": "item/agentMessage/delta", "params": {"delta": "本轮正文"}},
                {"method": "turn/completed", "params": {}},
            ])
        self.assertEqual([e for e in evs if e[0] == "thinking"], [],
                         "历史思考被整条重放（本 bug 的原始现象）：" + repr(evs))
        self.assertIn(("text", "本轮正文"), evs)

    async def test_turn_end_fallback_emits_only_new_items(self):
        """回合开始时 rollout 已有 1 条 ⇒ 兜底只补之后新增的那 2 条。"""
        rt = self._rt()
        with unittest.mock.patch.object(executor, "_codex_transcript_path", return_value=self.rollout), \
             unittest.mock.patch.object(executor.AgentRuntime, "_codex_reasoning_cursor", return_value=1):
            evs = await self._collect(rt, [
                {"method": "item/agentMessage/delta", "params": {"delta": "本轮正文"}},
                {"method": "turn/completed", "params": {}},
            ])
        self.assertEqual([e[1] for e in evs if e[0] == "thinking"], self.texts[1:])

    async def test_exec_path_fallback_also_seeded(self):
        """exec 兜底路径（`codex exec --json`）同样不能从 0 起，否则历史思考被重放。"""
        rt = self._rt()

        async def _fake_stream(cmd, cwd, timeout, parse=None, runtime=None):
            yield ("text", "exec 正文")

        with unittest.mock.patch.object(executor, "_codex_transcript_path", return_value=self.rollout), \
             unittest.mock.patch.object(executor, "_stream_subprocess", _fake_stream), \
             unittest.mock.patch.object(executor.AgentRuntime, "_codex_reasoning_cursor", return_value=2):
            evs = [ev async for ev in rt._run_codex("hi", "/tmp", 5)]
        self.assertEqual([e[1] for e in evs if e[0] == "thinking"], self.texts[2:],
                         "exec 路径也只应补本回合新增的思考：" + repr(evs))


class TestCodexRolloutToolFallback(unittest.IsolatedAsyncioTestCase):
    """回合内「rollout 工具兜底」：命令被 codex 沙盒拦下时 app-server **一个 item 都不发**，
    但 rollout 里照常落 `function_call` + `function_call_output`。

    用户现象：「成功的工具调用直接就显示了，失败的调用全部结束后才显示」。本兜底让失败调用
    也在回合内实时出现。fixture 用真实 rollout 原文（2026-09-17 `01a0aef5…`）的字段形态。
    """

    CID = "call_xzjzc0p5"
    OLD_CID = "call_old00001"
    SANDBOX_OUT = ("Chunk ID: 0c1fbb\nWall time: 0.0000 seconds\nProcess exited with code 71\n"
                   "Original token count: 14\nOutput:\n"
                   "sandbox-exec: sandbox_apply: Operation not permitted\n")

    @staticmethod
    def _call(cid, cmd, name="exec_command"):
        return {"type": "response_item",
                "payload": {"type": "function_call", "id": "fc_1", "name": name,
                            "arguments": json.dumps({"cmd": cmd}), "call_id": cid}}

    @staticmethod
    def _out(cid, output):
        return {"type": "response_item",
                "payload": {"type": "function_call_output", "id": "fco_1", "call_id": cid,
                            "output": output}}

    def setUp(self):
        # 历史（回合开始前就已落盘）：兜底绝不能把它重放出来
        self.rollout = _write_rollout([
            {"type": "session_meta", "payload": {"session_id": "tid-1"}},
            self._call(self.OLD_CID, "date +%s.%N"),
            self._out(self.OLD_CID, "Chunk ID: aa\nWall time: 0.0 seconds\nProcess exited with code 0\n"
                                    "Original token count: 3\nOutput:\n1789642000.0\n"),
        ])

    def tearDown(self):
        try:
            os.unlink(self.rollout)
        except OSError:
            pass

    def _rt(self):
        rt = _rt()
        rt.cli_session_id = "01a0aef5-c719-7fa1-bff6-2cfdb31e4cd7"
        return rt

    async def _run(self, rt, notifications, new_lines=(), delay=0.06):
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

        async def feeder():
            await asyncio.sleep(delay)
            if new_lines:                       # 回合进行中才落盘（与真实一致）
                _append_rollout(self.rollout, new_lines)
            await asyncio.sleep(delay)
            await rt._as_queue.put({"method": "turn/completed", "params": {}})

        task = asyncio.create_task(feeder())
        with unittest.mock.patch.object(executor, "_codex_transcript_path", return_value=self.rollout), \
             unittest.mock.patch.object(executor, "_AS_ROLLOUT_POLL", 0.02):
            evs = [ev async for ev in rt._run_codex_appserver("hi", "/tmp", 10)]
        await task
        return evs

    async def test_blocked_command_is_emitted_in_turn(self):
        """被沙盒拦下的命令（app-server 无 item）也要在回合内补出来：tool_use + tool_result(is_error)。"""
        evs = await self._run(self._rt(), [{"method": "item/agentMessage/delta", "params": {"delta": "跑一下"}}],
                              new_lines=[self._call(self.CID, "ls -la"), self._out(self.CID, self.SANDBOX_OUT)])
        tools = [e[1] for e in evs if e[0] == "tool_use"]
        results = [e[1] for e in evs if e[0] == "tool_result"]
        self.assertEqual([t["name"] for t in tools], ["exec_command"], repr(evs))
        self.assertEqual(tools[0]["input"], {"command": "ls -la"})
        self.assertEqual(len(results), 1)
        self.assertIs(results[0]["is_error"], True, "退出码 71 ⇒ is_error 必须为 True")
        self.assertIn("sandbox_apply", results[0]["content"])
        # 回执头（Chunk ID / Wall time / Process exited…）应已剥离，界面只见真实输出
        self.assertNotIn("Chunk ID", results[0]["content"])

    async def test_history_is_not_replayed(self):
        """回合开始前就存在的调用/回执不得被兜底重放（只补本轮新增）。"""
        evs = await self._run(self._rt(), [{"method": "item/agentMessage/delta", "params": {"delta": "只说不做"}}])
        self.assertEqual([e for e in evs if e[0] in ("tool_use", "tool_result")], [], repr(evs))

    async def test_dedup_with_live_item(self):
        """同一条命令既走实时 item 又在 rollout 里 ⇒ 只能产出一次 tool_use / tool_result。"""
        live = {"method": "item/completed",
                "params": {"item": {"id": self.CID, "type": "commandExecution", "command": "ls -la",
                                    "aggregatedOutput": "ok\n", "exitCode": 0}}}
        evs = await self._run(self._rt(), [live],
                              new_lines=[self._call(self.CID, "ls -la"), self._out(self.CID, self.SANDBOX_OUT)])
        self.assertEqual(len([e for e in evs if e[0] == "tool_use"]), 1, repr(evs))
        self.assertEqual(len([e for e in evs if e[0] == "tool_result"]), 1, repr(evs))

    async def test_end_discussion_not_taken_from_rollout(self):
        """end_discussion 由实时 MCP 通路产出，rollout 兜底**不得**再发一遍（会重复计票）。"""
        evs = await self._run(self._rt(), [], new_lines=[
            self._call("call_vote1", "", name="end_discussion"),
            {"type": "response_item", "payload": {"type": "function_call_output", "id": "fco_v",
                                                  "call_id": "call_vote1", "output": "已记录"}},
        ])
        self.assertEqual([e for e in evs if e[0] == "tool_use"], [], repr(evs))

    def test_rollout_tool_input_shapes(self):
        """兜底产出的 input 形状必须与实时通路一致（前端通用渲染同一条命令不能两种样子）。"""
        self.assertEqual(executor._codex_rollout_tool_input("exec_command", '{"cmd":"ls"}'),
                         {"command": "ls"})
        self.assertEqual(executor._codex_rollout_tool_input("write_stdin", '{"chars":"y\\n"}'),
                         {"input": "y\n"})
        self.assertEqual(executor._codex_rollout_tool_input("apply_patch", '{"patch":"P"}'),
                         {"patch": "P"})

    def test_scan_skips_history_by_cursor(self):
        """纯函数级：游标设在文件末尾 ⇒ 什么都不该捞到；回退游标才看得到（用于锁定语义）。"""
        rt = self._rt()
        with unittest.mock.patch.object(executor, "_codex_transcript_path", return_value=self.rollout):
            rt._as_rollout_line = rt._codex_rollout_lines()
            self.assertEqual(rt._codex_rollout_tool_events(), [])
            rt._as_rollout_line = 0
            rt._as_tool_call_seen.clear()
            evs = rt._codex_rollout_tool_events()
        self.assertEqual([e[0] for e in evs], ["tool_use", "tool_result"])


if __name__ == "__main__":
    unittest.main()
