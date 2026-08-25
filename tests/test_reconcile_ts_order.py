"""reconcile_agent_messages 回归：补 claude 真实时间 + 按 ts 稳定排序归位 abort 缺口。

背景（2026-08-25 修复）：
- API-error 时 orchestrator 不落 agent 最后一轮，reconcile 曾把缺失轮 append 到数组末尾
  （落在「会话结束」系统消息之后）→ 显示错序 + 无时间。
- 修复：reconcile 回填 claude 原生 timestamp（转本地格式），并按 ts 稳定排序兜底归位。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import store
import pytest


def test_claude_ts_to_local():
    # UTC ISO(Z) -> 本地 `%Y-%m-%d %H:%M:%S`，且与时序排序同格式
    r = store._claude_ts_to_local("2026-08-24T04:09:23.390Z")
    assert r and len(r) == 19 and r[4] == "-" and r[10] == " "
    # 兜底
    assert store._claude_ts_to_local("") is None
    assert store._claude_ts_to_local(None) is None
    assert store._claude_ts_to_local("not-a-date") is None


def test_reorder_by_ts():
    msgs = [
        {"speaker": "g1", "ts": None, "round": 0},
        {"speaker": "g2", "ts": "2026-01-01 00:00:02", "round": 1},
        {"speaker": "g3", "ts": "2026-01-01 00:00:01", "round": 2},
    ]
    changed = store._reorder_by_ts(msgs)
    assert changed is True
    # 无 ts 排末尾，其余按 ts 升序；round 重排为下标
    assert [m["speaker"] for m in msgs] == ["g3", "g2", "g1"]
    assert [m["round"] for m in msgs] == [0, 1, 2]


def test_reconcile_fills_ts_and_reorders(tmp_path):
    proj = str(tmp_path)
    # 本地缺 g2 最后一轮（abort 缺口），且「会话结束」已落在正确位置
    t_human = store._claude_ts_to_local("2026-01-01T00:00:00Z")
    t_g2a = store._claude_ts_to_local("2026-01-01T00:00:01Z")
    t_err = store._claude_ts_to_local("2026-01-01T00:00:09Z")  # 晚于 g2 最后一轮
    sessions = [{
        "id": "s_x",
        "team_id": "t1",
        "messages": [
            {"round": 0, "speaker": "human", "role": "任务", "content": "开始", "ts": t_human},
            {"round": 1, "speaker": "g2", "role": "选手", "content": "g2-第一轮", "ts": t_g2a},
            {"round": 2, "speaker": "system", "role": "系统", "content": "g2 执行出错", "ts": t_err},
            {"round": 3, "speaker": "system", "role": "系统", "content": "会话结束", "ts": t_err},
        ],
    }]
    store._save_sessions(proj, sessions)

    # claude 里 g2 有两轮，第二轮带 ts（介于第一轮与 执行出错 之间）
    t_g2b = "2026-01-01T00:00:05Z"
    cli_turns = [
        {"content": "g2-第一轮", "thinking": "", "tool_calls": [], "tool_results": [], "blocks": [],
         "ts": "2026-01-01T00:00:01Z"},
        {"content": "g2-最后一轮", "thinking": "", "tool_calls": [], "tool_results": [], "blocks": [],
         "ts": t_g2b},
    ]
    res = store.reconcile_agent_messages(proj, "s_x", "g2", cli_turns)
    assert res.get("appended", 0) >= 1, res

    msgs = store._load_sessions(proj)[0]["messages"]
    assert len(msgs) == 5, [m["content"] for m in msgs]
    # 全量按 ts 升序（稳定排序兜底）
    ts_seq = [m["ts"] for m in msgs]
    assert ts_seq == sorted(ts_seq), ts_seq
    # g2-最后一轮 带真实时间且排在「会话结束」之前
    g2last = [m for m in msgs if m["content"] == "g2-最后一轮"][0]
    assert g2last["ts"] == store._claude_ts_to_local(t_g2b)
    assert g2last["ts"] < t_err
    # 顺序：开始 < g2-第一轮 < g2-最后一轮 < 执行出错/会话结束
    order = [m["content"] for m in msgs]
    assert order.index("g2-最后一轮") < order.index("会话结束")
