# -*- coding: utf-8 -*-
"""回归测试：codex agent 的调用与事件解析（2026-09-11 修的两个 bug 的锁定）。

背景（真实故障）：用户换 codex 下五子棋，app 里 codex「不通」。真实会话
/Users/stalitz/www/aitest/test3/.agentchat/sessions.json 落库的报错是
    c1 执行出错，会话中止：退出码 1: Not inside a trusted directory and
    --skip-git-repo-check was not specified.
两个独立 bug：

  Bug 1 `_run_codex` 没带 `--skip-git-repo-check`：codex 默认拒绝在「非 git 仓库、且不在
        ~/.codex/config.toml 受信项目列表」的目录运行，直接退出码 1。app 的项目目录由用户
        任选、不可能都预先受信 → 必挂。

  Bug 2 `_codex_text` 不认 codex 0.146.1 的真实事件结构：文本嵌在 `item.text`，不在顶层
        （`{"type":"item.completed","item":{"type":"agent_message","text":"..."}}`）。
        只修 Bug 1 的话 codex 会「跑起来但一声不吭」——界面收不到任何输出。

下面的事件样例是 **2026-09-11 用真实 `codex exec --json` 抓下来的原文**，不是手写构造，
以免又被 codex 版本改结构坑到。

运行：
  /Users/stalitz/.workbuddy/binaries/python/envs/default/bin/python -m pytest tests/test_codex_parse.py -q
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.executor import AgentRuntime, _parse_codex_line, _codex_text  # noqa: E402
from backend.models import AgentConfig  # noqa: E402


# ---- 真实抓取的 codex exec --json 事件（原文，逐字保留）----
EV_THREAD_STARTED = '{"type":"thread.started","thread_id":"01a08f6e-2541-7d73-98bb-2e3db6c4776c"}'
EV_TURN_STARTED = '{"type":"turn.started"}'
EV_ITEM_AGENT_MESSAGE = (
    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message",'
    '"text":"五子棋是一种两人对弈的棋类游戏，双方在棋盘上轮流落子，先在横、竖或斜方向连成五子者获胜。"}}'
)
EV_TURN_COMPLETED = (
    '{"type":"turn.completed","usage":{"input_tokens":60682,"cached_input_tokens":59776,'
    '"cache_write_input_tokens":0,"output_tokens":570,"reasoning_output_tokens":193}}'
)
EV_ITEM_COMMAND = (
    '{"type":"item.completed","item":{"id":"item_1","type":"command_execution",'
    '"command":"ls","aggregated_output":"total 0"}}'
)

# ---- end_discussion MCP 工具调用（原文抓取，2026-09-11 实测 codex 0.146.1）----
EV_MCP_STARTED = (
    '{"type":"item.started","item":{"id":"item_0","type":"mcp_tool_call","server":"terminate-signal",'
    '"tool":"end_discussion","arguments":{"reason":"连通性测试"},"result":null,"error":null,'
    '"status":"in_progress"}}'
)
EV_MCP_COMPLETED = (
    '{"type":"item.completed","item":{"id":"item_0","type":"mcp_tool_call","server":"terminate-signal",'
    '"tool":"end_discussion","arguments":{"reason":"连通性测试"},'
    '"result":{"content":[{"type":"text","text":"【工具回执】你的 end_discussion 调用已收到（理由：连通性测试）。"}],'
    '"structured_content":null},"error":null,"status":"completed"}}'
)
EV_MCP_CANCELLED = (
    '{"type":"item.completed","item":{"id":"item_0","type":"mcp_tool_call","server":"terminate-signal",'
    '"tool":"end_discussion","arguments":{"reason":"连通性测试"},"result":null,'
    '"error":{"message":"user cancelled MCP tool call"},"status":"failed"}}'
)


def _runtime(session_id=None, model=None):
    agent = AgentConfig(id="ag_c1", name="c1", type="codex", model=model)
    return AgentRuntime(agent, session_id)


# ---------------- Bug 1：命令必须带 --skip-git-repo-check ----------------

def test_codex_cmd_has_skip_git_repo_check():
    rt = _runtime()
    cmd = rt._codex_cmd("hello")
    assert cmd[:3] == ["codex", "exec", "--json"], cmd
    assert "--skip-git-repo-check" in cmd, (
        "codex 命令必须带 --skip-git-repo-check，否则在非 git / 未受信目录直接退出码 1："
        "Not inside a trusted directory（test3 五子棋会话必现）"
    )


def test_codex_cmd_first_round_shape():
    """首轮：codex exec --json --skip-git-repo-check <prompt>（无 resume）。"""
    rt = _runtime(session_id=None)
    cmd = rt._codex_cmd("下棋")
    assert cmd[:3] == ["codex", "exec", "--json"]
    assert "--skip-git-repo-check" in cmd
    assert "resume" not in cmd
    assert cmd[-1] == "下棋"


def test_codex_cmd_resume_shape_and_order():
    """续接：flag 必须在 resume 子命令之前（实测 `codex exec --json --skip-git-repo-check resume <id> <prompt>` 成立）。"""
    rt = _runtime(session_id="01a08f6e-2541-7d73-98bb-2e3db6c4776c")
    cmd = rt._codex_cmd("继续")
    assert "resume" in cmd
    assert cmd.index("--skip-git-repo-check") < cmd.index("resume"), (
        "--skip-git-repo-check 必须排在 resume 子命令之前"
    )
    assert cmd[cmd.index("resume") + 1] == "01a08f6e-2541-7d73-98bb-2e3db6c4776c"
    assert cmd[-1] == "继续"


def test_codex_cmd_keeps_model_and_cli_args():
    rt = _runtime(model="deepseek-v4-flash")
    rt.agent.cli_args = "--foo bar"
    cmd = rt._codex_cmd("x")
    assert cmd[cmd.index("-m") + 1] == "deepseek-v4-flash"
    assert "--foo" in cmd and "bar" in cmd


# ---------------- Bug 2：必须能解析出 item.text ----------------

def test_parse_item_agent_message_text():
    """核心断言：codex 的正式发言（item.completed / agent_message / text）必须被提取出来。"""
    rt = _runtime()
    events = _parse_codex_line(EV_ITEM_AGENT_MESSAGE, rt)
    assert events, "item.completed 的 agent_message 文本必须被解析出来（修复前返回空 → 界面无输出）"
    kind, text = events[0]
    assert kind == "text"
    assert "五子棋是一种两人对弈的棋类游戏" in text


def test_codex_text_reads_nested_item_text():
    import json
    ev = json.loads(EV_ITEM_AGENT_MESSAGE)
    assert _codex_text(ev) != "", "_codex_text 必须读取 ev['item']['text']（codex 0.146.1 的真实结构）"


def test_parse_thread_started_captures_session_id():
    """首轮要从 thread.started 抓到 thread_id，供后续 resume。"""
    rt = _runtime(session_id=None)
    _parse_codex_line(EV_THREAD_STARTED, rt)
    assert rt.cli_session_id == "01a08f6e-2541-7d73-98bb-2e3db6c4776c"


def test_parse_control_events_emit_no_text():
    """thread.started / turn.started / turn.completed 不应产出文本（避免污染气泡）。"""
    rt = _runtime()
    for raw in (EV_TURN_STARTED, EV_TURN_COMPLETED):
        assert _parse_codex_line(raw, rt) == []
    # thread.started 只负责抓 id，也不产文本
    rt2 = _runtime()
    assert _parse_codex_line(EV_THREAD_STARTED, rt2) == []


def test_parse_command_execution_item_emits_nothing():
    """command_execution 类型的 item 没有 text 字段，不应被当发言输出。"""
    rt = _runtime()
    assert _parse_codex_line(EV_ITEM_COMMAND, rt) == []


def test_parse_non_json_line_falls_back_to_text():
    """非 JSON 行（codex 打印的普通文本/报错）应按原文当作文本透传，不静默丢弃。"""
    rt = _runtime()
    events = _parse_codex_line("Not inside a trusted directory and --skip-git-repo-check was not specified.", rt)
    assert events and events[0][0] == "text"
    assert "trusted directory" in events[0][1]


# ---------------- end_discussion MCP 工具：命令注入 + 事件映射 ----------------

def _config_flags(cmd):
    """取出所有 `-c <value>` 的 value。"""
    return [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-c"]


def test_codex_cmd_injects_end_discussion_mcp_server():
    """命令必须按次注入 terminate-signal MCP 服务（claude 靠项目 .mcp.json，codex 无此机制）。"""
    rt = _runtime()
    cmd = rt._codex_cmd("x")
    flags = _config_flags(cmd)
    joined = " ".join(flags)
    assert any("mcp_servers.terminate-signal.command=" in f for f in flags), (
        "必须注入 mcp_servers.terminate-signal.command，否则 codex 看不到 end_discussion 工具"
    )
    assert any("mcp_servers.terminate-signal.args=" in f for f in flags)
    assert "terminate_signal_mcp.py" in joined, "args 必须指向 claude 侧同一个 MCP 脚本"
    # 用 TOML 字面字符串（单引号）避免路径转义问题
    assert "'" in joined


def test_codex_cmd_sets_approval_mode_approve():
    """审批模式必须是 approve —— 默认值与 auto 都会让 codex 自动取消 MCP 调用。"""
    rt = _runtime()
    flags = _config_flags(rt._codex_cmd("x"))
    want = 'mcp_servers.terminate-signal.default_tools_approval_mode="approve"'
    assert want in flags, (
        "缺 default_tools_approval_mode=approve 时，codex exec 非交互无审批人，"
        "MCP 工具调用会被自动取消（实测报 'user cancelled MCP tool call'）"
    )


def test_codex_cmd_mcp_flags_come_before_resume():
    """-c 覆盖必须排在 resume 子命令之前（实测该顺序下 resume 仍能生效）。"""
    rt = _runtime(session_id="01a08fa7-7e95-72c3-aebf-897933ff1ff5")
    cmd = rt._codex_cmd("继续")
    assert "resume" in cmd
    assert max(i for i, tok in enumerate(cmd) if tok == "-c") < cmd.index("resume")


def test_parse_mcp_completed_emits_tool_use():
    """核心：item.completed 的 mcp_tool_call 必须映射成 tool_use，编排层才能收到 codex 的终止票。"""
    rt = _runtime()
    events = _parse_codex_line(EV_MCP_COMPLETED, rt)
    assert events, "mcp_tool_call（completed）必须产出 tool_use 事件（修复前被完全忽略）"
    kind, payload = events[0]
    assert kind == "tool_use"
    assert payload["name"] == "end_discussion"
    assert payload["input"]["reason"] == "连通性测试"


def test_parse_mcp_started_does_not_duplicate_tool_use():
    """item.started 不产事件，避免同一次调用被记两次（只认 completed）。"""
    rt = _runtime()
    assert _parse_codex_line(EV_MCP_STARTED, rt) == []


def test_parse_mcp_cancelled_still_counts_as_intent():
    """被审批取消的调用同样算「模型发起了终止」——与 claude 侧「见 tool_use 即计票」一致。"""
    rt = _runtime()
    events = _parse_codex_line(EV_MCP_CANCELLED, rt)
    assert events and events[0][0] == "tool_use"
    assert events[0][1]["name"] == "end_discussion"


def test_mcp_item_emits_no_text():
    """mcp_tool_call 的回执文本嵌在 result.content 里，不得被当发言文本输出到气泡。"""
    import json
    assert _codex_text(json.loads(EV_MCP_COMPLETED)) == ""
    assert _codex_text(json.loads(EV_MCP_STARTED)) == ""
