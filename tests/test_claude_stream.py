# -*- coding: utf-8 -*-
"""回归测试：claude 的「真流式」（--include-partial-messages）解析与去重。

背景（2026-09-11 实测）：
- 不加 `--include-partial-messages` 时，claude `--output-format stream-json` 只发**整块**
  assistant 消息（实测一轮：38 system / 2 assistant / 1 result，零 delta）。界面上看到的
  "逐字效果"其实是 executor 的 `_replay` 把整块文本演出来的。
- 加上后才会发 `stream_event` → `content_block_delta`（text_delta / thinking_delta）。
  实测一轮：72 × text_delta + 53 × thinking_delta。
- ⚠️ 开 partial 后，**同一段内容会既以 delta 增量到达、又出现在最终那条整块 assistant
  消息里** —— 不去重界面会显示两遍。本文件锁住这两件事：能解包 + 不重复。

运行：/Users/stalitz/.workbuddy/binaries/python/envs/default/bin/python -m pytest tests/test_claude_stream.py -q
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.executor import (  # noqa: E402
    AgentRuntime, _is_unknown_partial_flag, _parse_claude_line,
)
from backend.models import AgentConfig  # noqa: E402


def _rt(session_id=None):
    return AgentRuntime(AgentConfig(id="ag_x", name="x", type="claude-code"), session_id)


def _wrap(event: dict) -> str:
    """按真实形态包成 stream_event 行。"""
    return json.dumps({"type": "stream_event", "event": event}, ensure_ascii=False)


def _deltas(rt, deltas):
    """喂一串 content_block_delta，收集事件。"""
    out = []
    for d in deltas:
        out += _parse_claude_line(_wrap({"type": "content_block_delta", "index": 0, "delta": d}), rt)
    return out


# ---------------- 解包 stream_event ----------------

def test_unwraps_stream_event_text_delta():
    rt = _rt()
    evs = _parse_claude_line(_wrap({"type": "content_block_delta", "index": 1,
                                    "delta": {"type": "text_delta", "text": "五子"}}), rt)
    assert evs == [("text", "五子")], evs


def test_unwraps_stream_event_thinking_delta():
    rt = _rt()
    evs = _parse_claude_line(_wrap({"type": "content_block_delta", "index": 0,
                                    "delta": {"type": "thinking_delta", "thinking": "先看棋盘"}}), rt)
    assert evs == [("thinking", "先看棋盘")], evs


def test_input_json_delta_emits_nothing():
    """tool_use 的 input_json_delta 是半截 JSON，绝不能流到编排层（tool_use 由整块消息给）。"""
    rt = _rt()
    evs = _parse_claude_line(_wrap({"type": "content_block_delta", "index": 2,
                                    "delta": {"type": "input_json_delta", "partial_json": '{"rea'}}), rt)
    assert evs == [], evs


def test_control_stream_events_emit_nothing():
    rt = _rt()
    for e in ({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
              {"type": "content_block_stop", "index": 0},
              {"type": "message_delta", "usage": {}},
              {"type": "message_stop"}):
        assert _parse_claude_line(_wrap(e), rt) == [], e


# ---------------- 去重：delta 与整块不得各出一遍 ----------------

def test_assistant_block_suppressed_after_deltas():
    """核心：已用 delta 吐过正文/思考后，整块 assistant 不得再吐一遍。"""
    rt = _rt()
    _parse_claude_line(_wrap({"type": "message_start"}), rt)
    got = _deltas(rt, [{"type": "thinking_delta", "thinking": "想法"},
                       {"type": "text_delta", "text": "正文"}])
    assert got == [("thinking", "想法"), ("text", "正文")]

    whole = json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "想法"},
        {"type": "text", "text": "正文"},
    ]}}, ensure_ascii=False)
    assert _parse_claude_line(whole, rt) == [], "整块内容与 delta 重复，必须被抑制"


def test_assistant_block_still_emits_tool_use_after_deltas():
    """抑制只针对正文/思考：tool_use 必须照旧从整块消息取出（否则终止票丢失）。"""
    rt = _rt()
    _parse_claude_line(_wrap({"type": "message_start"}), rt)
    _deltas(rt, [{"type": "text_delta", "text": "我要收尾了"}])
    whole = json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "我要收尾了"},
        {"type": "tool_use", "name": "end_discussion", "input": {"reason": "一致"}},
    ]}}, ensure_ascii=False)
    evs = _parse_claude_line(whole, rt)
    assert evs == [("tool_use", {"name": "end_discussion", "input": {"reason": "一致"}})], evs


def test_message_start_resets_dedup_flags():
    """新一轮 message_start 必须复位去重标志，否则后续整块会被误抑制（丢内容）。"""
    rt = _rt()
    _parse_claude_line(_wrap({"type": "message_start"}), rt)
    _deltas(rt, [{"type": "text_delta", "text": "第一轮"}])
    whole = json.dumps({"type": "assistant", "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "第一轮"}]}}, ensure_ascii=False)
    assert _parse_claude_line(whole, rt) == []

    _parse_claude_line(_wrap({"type": "message_start"}), rt)  # 第二轮开始
    whole2 = json.dumps({"type": "assistant", "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "第二轮"}]}}, ensure_ascii=False)
    assert _parse_claude_line(whole2, rt) == [("text", "第二轮")], "复位后整块必须重新生效"


def test_without_deltas_whole_block_still_works():
    """向后兼容：没有 partial（旧版本 claude / 未解包路径）时，整块解析行为不变。"""
    rt = _rt()
    whole = json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "想法"},
        {"type": "text", "text": "正文"},
    ]}}, ensure_ascii=False)
    evs = _parse_claude_line(whole, rt)
    assert ("thinking", "想法") in evs and ("text", "正文") in evs, evs


def test_runtime_none_never_crashes():
    """runtime 为空（非流式解析场景）时不得崩，也不做去重。"""
    evs = _parse_claude_line(_wrap({"type": "content_block_delta", "index": 0,
                                    "delta": {"type": "text_delta", "text": "x"}}), None)
    assert evs == [("text", "x")]


# ---------------- 命令与自愈降级 ----------------

def test_claude_cmd_has_partial_flag():
    """命令必须带 --include-partial-messages；自愈降级后要去掉它。"""
    rt = _rt()
    cmd = rt._claude_cmd()
    assert "--include-partial-messages" in cmd, (
        "缺该 flag 时 claude 不会发 delta（退回整块 + _replay 假流式）"
    )
    rt._partial_messages = False
    assert "--include-partial-messages" not in rt._claude_cmd(), "降级后不得再带该 flag"


def test_unknown_partial_flag_detector():
    assert _is_unknown_partial_flag(
        "error: unknown option '--include-partial-messages'") is True
    assert _is_unknown_partial_flag("Session ID abc is already in use") is False
    assert _is_unknown_partial_flag("") is False
