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

from backend.executor import (  # noqa: E402
    AgentRuntime, _parse_codex_line, _codex_text,
    _parse_codex_transcript, _codex_reasoning_texts, _codex_parse_args,
)
import json  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402
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


def test_parse_command_execution_item_emits_tool_use_not_text():
    """command_execution 是 codex 的**原生工具调用**：应映射成 tool_use，且绝不当发言文本。

    2026-09-16 改：原先这里整条忽略（只断言"不产文本"），代价是 codex 调了自带 shell
    也完全不落盘、界面一片空白（test5 实测：claude 调 Bash 有痕、codex 侧空白）。
    样例 EV_ITEM_COMMAND 是真实 `codex exec --json` 抓下来的原文。
    """
    rt = _runtime()
    evs = _parse_codex_line(EV_ITEM_COMMAND, rt)
    assert len(evs) == 1, evs
    kind, payload = evs[0]
    assert kind == "tool_use", "原生工具必须映射为 tool_use（前端通用渲染才显示得出来）"
    assert payload["name"] == "exec_command"
    assert payload["input"]["command"] == "ls"


def test_parse_command_execution_started_not_duplicated():
    """item.started / item.completed 成对上报：只认 completed，避免同一次调用显示两遍。"""
    rt = _runtime()
    started = EV_ITEM_COMMAND.replace("item.completed", "item.started")
    assert _parse_codex_line(started, rt) == []


def test_native_tool_use_predicate_and_naming():
    """驼峰/下划线两种命名都要认，非工具条目一律返回 None（见 _CODEX_NATIVE_TOOLS）。"""
    from backend.executor import _codex_native_tool_use
    # exec/rollout 命名（下划线）
    assert _codex_native_tool_use({"type": "command_execution", "command": "ls"}) == {
        "name": "exec_command", "input": {"command": "ls"}}
    # app-server 命名（驼峰）——同一工具的另一条通路
    assert _codex_native_tool_use({"type": "commandExecution", "command": "pwd"}) == {
        "name": "exec_command", "input": {"command": "pwd"}}
    # 带退出码时一并带出（排查失败调用用）
    assert _codex_native_tool_use(
        {"type": "command_execution", "command": "ls", "exit_code": 0})["input"]["exit_code"] == 0
    # 文件改动类
    assert _codex_native_tool_use({"type": "file_change", "patch": "*** Begin Patch"})["name"] == "apply_patch"
    assert _codex_native_tool_use({"type": "fileChange", "changes": "x"})["name"] == "apply_patch"
    # 非工具条目
    for it in ({"type": "reasoning"}, {"type": "agent_message", "text": "hi"}, None, "x", {}):
        assert _codex_native_tool_use(it) is None, it


def test_codex_cmd_injects_ollama_provider_for_local_base_url():
    """codex agent 配了本机 Ollama 地址 → 必须注入内置 provider ollama。

    codex 的默认 provider 来自用户全局 config.toml（本机是 deepseek = 远程付费）；不注入的话
    这个 agent 每轮都花真钱。注入一律走命令行 `-c`，**不动用户的 ~/.codex/config.toml**。
    """
    rt = _runtime()
    rt.agent.base_url = "http://localhost:11434"
    cmd = rt._codex_cmd("hi")
    assert 'model_provider="ollama"' in cmd, cmd
    assert not [t for t in cmd if t.startswith("model_providers.ollama")], (
        "内置 provider 名不可覆盖（实测 reserved），只应设 model_provider")


def test_codex_cmd_custom_provider_for_other_base_url():
    """非本机 Ollama 的地址 → 注册自定义 provider（内置名不可覆盖），按 responses 协议接。"""
    rt = _runtime()
    rt.agent.base_url = "http://192.168.1.9:8000"
    cmd = rt._codex_cmd("hi")
    assert 'model_provider="agentchat"' in cmd
    assert 'model_providers.agentchat.base_url="http://192.168.1.9:8000/v1"' in cmd
    assert 'model_providers.agentchat.wire_api="responses"' in cmd


def test_codex_cmd_no_base_url_keeps_global_default():
    """未配 base_url → 不注入任何 provider，沿用 codex 全局配置（既有行为不变）。"""
    rt = _runtime()
    cmd = rt._codex_cmd("hi")
    assert not [t for t in cmd if "model_provider" in t], cmd


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


# ---------------- codex rollout 解析（历史重建）+ reasoning 回收 ----------------
# fixture 用 codex 0.146.1 的**真实 schema**（2026-09-11 从 rollout 原文抄的字段结构）：
#   {"type":"response_item","payload":{...}} / {"type":"event_msg","payload":{...}}
# 旧的 _parse_codex_transcript 读顶层 o["message"]/o["role"]，对这种格式返回 0 回合 →
# codex 的历史重建（reconcile）完全失效。

def _rollout(lines):
    """写一个临时 rollout 文件。

    不用 pytest 的 tmp_path fixture —— 本机（WorkBuddy 沙箱）禁止在
    /private/var/folders/.../pytest-of-* 建目录，会 PermissionError。
    """
    fd, path = tempfile.mkstemp(suffix=".jsonl", prefix="agentchat_rollout_", dir="/tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(x, ensure_ascii=False) for x in lines))
    return path


def _ri(payload):
    return {"type": "response_item", "payload": payload}


def _em(payload):
    return {"type": "event_msg", "payload": payload}


ROLLOUT_LINES = [
    {"type": "session_meta", "payload": {"session_id": "tid-1", "cli_version": "0.146.1"}},
    _ri({"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "permissions..."}]}),
    # ---- 回合 1：user 提示 → 思考 → 正文（event_msg/agent_message 与正文重复，不得双计）----
    _ri({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "第一轮提示"}]}),
    # event_msg/user_message 也是 message 字符串，但那是**用户提示词**、不是 assistant 正文
    _em({"type": "user_message", "message": "你是：选手1。\n\n人工介入 / 任务：提示词"}),
    _ri({"type": "reasoning", "summary": [],
         "content": [{"type": "reasoning_text", "text": "思考一：先看工具列表。"}]}),
    _em({"type": "agent_message", "message": "正文一"}),  # 与下一行 response_item 内容完全一致（真实 rollout 即如此）
    _ri({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "正文一"}]}),
    # ---- 回合 2：user 提示 → 工具搜索 → 思考 → 工具调用 → 正文 ----
    _ri({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "第二轮提示"}]}),
    _ri({"type": "tool_search_call", "arguments": {"query": "end_discussion"}}),
    _ri({"type": "tool_search_output", "tools": []}),
    _ri({"type": "reasoning", "summary": [],
         "content": [{"type": "reasoning_text", "text": "思考二：没有这个工具。"}]}),
    _ri({"type": "function_call", "name": "end_discussion", "arguments": '{"reason": "达成一致"}'}),
    _ri({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "正文二"}]}),
]


def test_codex_transcript_parses_two_turns():
    turns = _parse_codex_transcript(_rollout(ROLLOUT_LINES))
    assert len(turns) == 2, f"应按 user 消息切出 2 个回合，实际 {len(turns)}（旧实现是 0）"
    assert turns[0]["content"] == "正文一"
    assert turns[0]["thinking"] == "思考一：先看工具列表。"
    assert turns[1]["content"] == "正文二"
    assert turns[1]["thinking"] == "思考二：没有这个工具。"


def test_codex_transcript_does_not_double_count_event_msg():
    """event_msg/agent_message 与 response_item 的 assistant message 内容重复（真实 rollout
    里两份文本完全一致），正文只能出现一次、不得被拼两遍。"""
    turns = _parse_codex_transcript(_rollout(ROLLOUT_LINES))
    assert turns[0]["content"] == "正文一"
    assert turns[0]["content"].count("正文一") == 1
    assert [b["text"] for b in turns[0]["blocks"] if b["type"] == "text"] == ["正文一"]


def test_codex_transcript_does_not_leak_user_prompt(tmp_path_unused=None):
    """event_msg/user_message 的 message 是用户提示词，绝不能被当成 assistant 正文。

    这是实测踩到的回归：把「message 为字符串」当成 assistant 正文后，提示词
    （"你是：5子棋选手1。\n\n人工介入 / 任务：..."）会整段混进正文。
    """
    turns = _parse_codex_transcript(_rollout(ROLLOUT_LINES))
    for t in turns:
        assert not t["content"].startswith("你是："), t["content"][:60]
        assert "人工介入" not in t["content"]


def test_codex_transcript_thinking_block_precedes_text():
    """时间线 blocks 必须按原生顺序：思考在前、正文在后（不另造顺序逻辑）。"""
    turns = _parse_codex_transcript(_rollout(ROLLOUT_LINES))
    types = [b["type"] for b in turns[0]["blocks"]]
    assert types == ["thinking", "text"], types


# 原生工具（非 end_discussion）的 function_call —— 2026-09-16 从真实 rollout 抓的原文形态：
# codex 调自带 shell 时 name=exec_command、arguments 是 JSON 字符串，紧随一条 function_call_output。
ROLLOUT_NATIVE_TOOL = [
    {"type": "session_meta", "payload": {"session_id": "tid-2", "cli_version": "0.146.1"}},
    _ri({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "跑个命令"}]}),
    _ri({"type": "function_call", "id": "fc_1", "name": "exec_command",
         "arguments": '{"cmd":"echo codex-tool-test"}', "call_id": "call_1"}),
    _ri({"type": "function_call_output", "call_id": "call_1",
         "output": "Chunk ID: 47e47e\nWall time: 0.0000 seconds\nProcess exited with code 0\n"
                   "Output:\ncodex-tool-test\n"}),
    _ri({"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "输出是 codex-tool-test"}]}),
]


def test_codex_transcript_keeps_native_tool_use():
    """rollout 里的原生工具（exec_command）也必须对账成 tool_use。

    否则重开历史会话时，实时落盘的工具块会被 CLI 侧整体覆盖抹掉
    （store._merge_tool_blocks：CLI 侧一旦有工具块就整体采信）——表现就是「刚看过的工具调用，
    重开一次就没了」。
    """
    turns = _parse_codex_transcript(_rollout(ROLLOUT_NATIVE_TOOL))
    assert len(turns) == 1, turns
    t = turns[0]
    assert t["content"] == "输出是 codex-tool-test"
    assert t["tool_calls"] == [{"name": "exec_command", "input": {"cmd": "echo codex-tool-test"}}]
    blocks = [b for b in t["blocks"] if b["type"] == "tool_use"]
    assert blocks and blocks[0]["name"] == "exec_command"


def test_codex_transcript_ignores_tool_output_as_text():
    """function_call_output 是工具回执，绝不能被当成 assistant 正文混进气泡。"""
    turns = _parse_codex_transcript(_rollout(ROLLOUT_NATIVE_TOOL))
    for t in turns:
        assert "Chunk ID" not in t["content"], t["content"][:80]


def test_codex_transcript_extracts_tool_use_from_function_call():
    """function_call 的 arguments 是 JSON 字符串，须解析成 input.reason。"""
    turns = _parse_codex_transcript(_rollout(ROLLOUT_LINES))
    t1 = turns[1]
    assert t1["tool_calls"] == [{"name": "end_discussion", "input": {"reason": "达成一致"}}]
    blocks = [b for b in t1["blocks"] if b["type"] == "tool_use"]
    assert blocks and blocks[0]["input"] == {"reason": "达成一致"}


def test_codex_reasoning_texts_in_order():
    texts = _codex_reasoning_texts(_rollout(ROLLOUT_LINES))
    assert texts == ["思考一：先看工具列表。", "思考二：没有这个工具。"]


def test_codex_new_reasoning_incremental(monkeypatch):
    """_codex_new_reasoning 必须只返回「新增」部分，避免同一段思考被重复吐。"""
    import backend.executor as ex
    path = _rollout(ROLLOUT_LINES)
    monkeypatch.setattr(ex, "_codex_transcript_path", lambda sid: path)
    rt = _runtime(session_id="tid-1")
    first = rt._codex_new_reasoning(0)
    assert first == ["思考一：先看工具列表。", "思考二：没有这个工具。"]
    assert rt._codex_new_reasoning(len(first)) == [], "已吐过的思考不得重复返回"


def test_codex_new_reasoning_survives_missing_file(monkeypatch):
    """读不到 rollout 时必须安静返回空，绝不能打断本轮运行。"""
    import backend.executor as ex
    monkeypatch.setattr(ex, "_codex_transcript_path", lambda sid: "/nonexistent/x.jsonl")
    rt = _runtime(session_id="tid-1")
    assert rt._codex_new_reasoning(0) == []
    assert _runtime(session_id=None)._codex_new_reasoning(0) == []


def test_codex_parse_args_variants():
    assert _codex_parse_args('{"reason": "r"}') == {"reason": "r"}
    assert _codex_parse_args({"reason": "r"}) == {"reason": "r"}
    assert _codex_parse_args("not json") == {}
    assert _codex_parse_args(None) == {}
