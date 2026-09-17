"""CLI 报错回合 → 可见的系统留痕（2026-09-16，用户报「cli 报错加系统记录」）。

claude 外壳把 API 失败渲染成普通文本（`API Error: …` / `No response requested.` /
`Request Failed:`），reconcile 原先**整条剔除**（防止报错被当发言喂给其他 agent，见 NF4），
副作用是会话只剩提问、一片空白，看不出是「运行失败」还是「漏落」。

现改为：错误回合仍不作为 agent 发言落盘，但转成一条 `speaker="system"` 的可见消息
（保留错误原文），并按 ts 归位。派生标记 `sys_error_of` 保证幂等；该 agent 记录里
全是报错回合时不动本地已记录的发言。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import store

_ERR_TURN = {
    "content": "API Error: API returned an empty or malformed response (HTTP 200)"
               " — check for a proxy or gateway intercepting the request",
    "thinking": "", "tool_calls": [], "tool_results": [], "blocks": [],
    "ts": "2026-01-01T00:00:05Z",
}


def _tmp_proj(prefix):
    # 不用 pytest 的 tmp_path：本机沙箱禁止在系统临时目录下建 pytest-of-* 目录
    return tempfile.mkdtemp(prefix=prefix, dir="/tmp")


def test_error_turn_becomes_visible_system_message_and_is_idempotent():
    proj = _tmp_proj("agentchat_errturn_")
    store._save_sessions(proj, [{
        "id": "s_1", "team_id": "t1", "messages": [
            {"round": 0, "speaker": "human", "role": "任务", "content": "开始",
             "ts": "2026-01-01 00:00:00"},
        ],
    }])

    store.reconcile_agent_messages(proj, "s_1", "g1", [dict(_ERR_TURN)])
    msgs = store._load_sessions(proj)[0]["messages"]
    assert len(msgs) == 2, msgs
    sysm = msgs[1]
    assert sysm["speaker"] == "system"
    assert "g1 本次运行失败" in sysm["content"]
    assert "API Error" in sysm["content"]          # 错误原文保留，便于定位
    assert sysm["sys_error_of"] == "g1"

    # 幂等：重跑对账（每次打开会话都会跑）不得重复堆叠同一条留痕
    store.reconcile_agent_messages(proj, "s_1", "g1", [dict(_ERR_TURN)])
    msgs2 = store._load_sessions(proj)[0]["messages"]
    assert len(msgs2) == 2, [m["content"] for m in msgs2]


def test_error_only_turns_do_not_touch_local_agent_messages():
    """CLI 记录里全是报错回合（无有效发言）时，本地已记录的发言一律保持不动。"""
    proj = _tmp_proj("agentchat_erronly_")
    store._save_sessions(proj, [{
        "id": "s_1", "team_id": "t1", "messages": [
            {"round": 0, "speaker": "human", "role": "任务", "content": "开始",
             "ts": "2026-01-01 00:00:00"},
            {"round": 1, "speaker": "g1", "role": "", "content": "", "thinking": "只有思考的残留",
             "ts": "2026-01-01 00:00:03"},
        ],
    }])

    store.reconcile_agent_messages(proj, "s_1", "g1", [dict(_ERR_TURN)])
    msgs = store._load_sessions(proj)[0]["messages"]
    # 本地 think-only 残留不被删（原语义：CLI 侧无有效回合 ⇒ 保持本地不动）
    assert any(m.get("speaker") == "g1" for m in msgs), msgs
    assert any(m.get("speaker") == "system" and "本次运行失败" in m.get("content", "") for m in msgs)


# --- 错误文本被**拼接在正文之后**（2026-09-17 s_5a0f8d10 实测；前缀判定认不出，会整轮当发言落盘）---
_TAIL_ERR_TEXT = (
    "收到，我已准备好作为“探针”进行对比测试。\n\n**开始执行测试：**\n\n\n\n"
    "There's an issue with the selected model (gemma4:26b). It may not exist or you may not "
    "have access to it. Run --model to pick a different model."
)


def test_is_cli_error_text_covers_appended_error():
    """claude 失败时会把错误**接在正文尾部**：仅靠 startswith 判定会漏（本轮落成了正常发言）。"""
    assert store.is_cli_error_text(_TAIL_ERR_TEXT)                  # 尾部错误 → 含判定命中
    assert store.is_cli_error_text("There's an issue with the selected model (x)")
    assert store.is_cli_error_text("API Error: API returned an empty or malformed response")
    assert store.is_cli_error_text("No response requested.")
    assert store.is_cli_error_text("Request Failed: 500")
    # 正常发言不得被误判（否则会整轮丢弃、把有效回答变系统报错）
    assert not store.is_cli_error_text("我认为模型表现取决于提示词。")
    assert not store.is_cli_error_text("")
    assert not store.is_cli_error_text(None)


def test_appended_error_turn_becomes_system_message_not_speech():
    """尾部错误回合：reconcile 不把它当发言带回，而是转可见系统留痕。"""
    proj = _tmp_proj("agentchat_errtail_")
    store._save_sessions(proj, [{
        "id": "s_1", "team_id": "t1", "messages": [
            {"round": 0, "speaker": "human", "role": "任务", "content": "开始",
             "ts": "2026-01-01 00:00:00"},
        ],
    }])
    store.reconcile_agent_messages(proj, "s_1", "g1", [
        {"content": _TAIL_ERR_TEXT, "thinking": "", "tool_calls": [], "tool_results": [],
         "blocks": [], "ts": "2026-01-01T00:00:05Z"},
    ])
    msgs = store._load_sessions(proj)[0]["messages"]
    assert not any(m.get("speaker") == "g1" for m in msgs), msgs
    assert any(m.get("speaker") == "system" and "本次运行失败" in m.get("content", "")
               for m in msgs), msgs


def test_orchestrator_uses_same_error_judgement():
    """实时编排（orchestrator）与 reconcile 必须同一套判定 —— 分叉会让一侧漏掉错误文本。"""
    from backend import orchestrator
    assert orchestrator._looks_like_claude_error(_TAIL_ERR_TEXT)
    assert orchestrator._looks_like_claude_error("API Error: x")
    assert not orchestrator._looks_like_claude_error("正常回答")
