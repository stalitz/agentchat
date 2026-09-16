"""已有对话的会话「Team 不可改」（FR18）：Team 只在首轮写回，续轮锁定。

用户约定：**首轮可写回 / 续轮锁定**，不能一刀切 —— `update_session_team` 本身是为修
「首轮切 Team 不落库 ⇒ stored 停留 teams[0]、重开被静默降级成 2 agent」而加的。
但「每次发起都写回」的副作用是：打开有内容的会话 → 改 composer 的 Team → 继续讨论，
会话 stored team 被静默改写、旧 Team 的历史与另一个 Team 的 agent 混进同一条会话
（单 agent 直跑 vs 多 agent 讨论一旦错配，后面每一轮都是错的）。

本文件锁住三件事：
1. `store.is_session_team_locked` 真值表（system 留痕不算发言；human 提问算）；
2. `main._resolve_start_team` 的决定（首轮采信前端 / 续轮用 stored / stored 为空的老会话不锁死）；
3. 端到端（WS handle_start）：已有发言的会话带另一个 team_id 发起 ⇒ 会话 team_id 不变 + 广播锁定说明。
"""
from fastapi.testclient import TestClient

from tests.base import store, models, main, TMP, fresh_config


def _proj(tag):
    p = TMP / f"proj_{tag}"
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _save(proj, msgs, team_id="tm_a", sid="s_1"):
    store._save_sessions(proj, [{"id": sid, "title": "t", "team_id": team_id,
                                 "messages": msgs, "agent_sessions": {}}])
    return sid


def test_is_session_team_locked_truth_table():
    fresh_config()
    proj = _proj("tlock1")
    # 新会话（无消息）⇒ 未锁
    assert store.is_session_team_locked(proj, _save(proj, [])) == False  # noqa: E712
    # 只有系统留痕（终止原因 / CLI 报错）⇒ 未锁：这种会话应能换 Team 重试
    _save(proj, [{"round": 0, "speaker": "system", "role": "系统", "content": "本次运行失败：API Error"}])
    assert store.is_session_team_locked(proj, "s_1") == False  # noqa: E712
    # human 提问 ⇒ 已锁（提问也已落盘为会话历史）
    _save(proj, [{"round": 0, "speaker": "human", "role": "任务", "content": "议题"}])
    assert store.is_session_team_locked(proj, "s_1") == True  # noqa: E712
    # agent 发言 ⇒ 已锁
    _save(proj, [{"round": 0, "speaker": "human", "role": "任务", "content": "议题"},
                 {"round": 1, "speaker": "g1", "role": "", "content": "我的看法"}])
    assert store.is_session_team_locked(proj, "s_1") == True  # noqa: E712
    # 只有工具块、正文为空的残留发言也算「已有对话」
    _save(proj, [{"round": 1, "speaker": "g1", "role": "", "content": "",
                  "blocks": [{"type": "tool_use", "name": "ls"}]}])
    assert store.is_session_team_locked(proj, "s_1") == True  # noqa: E712
    # 会话不存在 ⇒ 未锁（新建会话路径）
    assert store.is_session_team_locked(proj, "s_gone") == False  # noqa: E712


def test_resolve_start_team_first_turn_vs_locked():
    fresh_config()
    proj = _proj("tlock2")
    # 首轮：会话还不存在 ⇒ 采信前端选择
    assert main._resolve_start_team(proj, None, "tm_x") == ("tm_x", False)
    # 空会话（还没有任何发言）⇒ 同样采信前端选择（首轮可写回）
    _save(proj, [])
    assert main._resolve_start_team(proj, "s_1", "tm_x") == ("tm_x", False)
    # 已有对话 ⇒ 锁定：改用 stored，并告知被改写
    _save(proj, [{"round": 0, "speaker": "human", "role": "任务", "content": "议题"}], team_id="tm_a")
    assert main._resolve_start_team(proj, "s_1", "tm_b") == ("tm_a", True)
    # 前端传的就是 stored ⇒ 无需改写（不算锁定事件，不刷提示）
    assert main._resolve_start_team(proj, "s_1", "tm_a") == ("tm_a", False)
    # 老数据：stored 为空但已有对话 ⇒ 不锁死（否则「锁了却无从发起」）
    _save(proj, [{"round": 0, "speaker": "human", "role": "任务", "content": "议题"}], team_id="")
    assert main._resolve_start_team(proj, "s_1", "tm_b") == ("tm_b", False)


def test_ws_start_keeps_stored_team_when_session_has_turns():
    """端到端：已有对话的会话，即使前端传了另一个 Team，也不会被改写。"""
    fresh_config()
    ag = store.upsert_agent(models.AgentConfig(name="假A", type="fake", fake_terminate_round=1))
    tm_a = store.upsert_team(models.TeamConfig(name="TA", agent_ids=[ag.id]))
    tm_b = store.upsert_team(models.TeamConfig(name="TB", agent_ids=[ag.id]))
    pdir = TMP / "proj_tlock_ws"
    pdir.mkdir(parents=True, exist_ok=True)
    pr = store.upsert_project(models.ProjectConfig(name="P", path=str(pdir), team_ids=[tm_a.id, tm_b.id]))
    _save(str(pdir), [{"round": 0, "speaker": "human", "role": "任务", "content": "上一轮议题"}],
          team_id=tm_a.id, sid="s_lock")

    client = TestClient(main.app)
    events = []
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"action": "start", "project_id": pr.id, "team_id": tm_b.id,
                      "session_id": "s_lock", "task": "继续讨论"})
        for _ in range(80):
            try:
                ev = ws.receive_json()
            except Exception:
                break
            events.append(ev)
            if ev.get("type") == "run_end":
                break

    assert store.read_session(str(pdir), "s_lock")["team_id"] == tm_a.id, "续轮不得改写会话的 team_id"
    assert any(e.get("type") == "system" and "Team 已锁定" in (e.get("msg") or "") for e in events), \
        [e.get("msg") for e in events if e.get("type") == "system"]


def test_ws_start_writes_team_back_for_empty_session():
    """首轮仍要写回：空会话用 composer 选的 Team 发起 ⇒ stored team_id 随之更新。"""
    fresh_config()
    ag = store.upsert_agent(models.AgentConfig(name="假A", type="fake", fake_terminate_round=1))
    tm_a = store.upsert_team(models.TeamConfig(name="TA", agent_ids=[ag.id]))
    tm_b = store.upsert_team(models.TeamConfig(name="TB", agent_ids=[ag.id]))
    pdir = TMP / "proj_tlock_ws2"
    pdir.mkdir(parents=True, exist_ok=True)
    pr = store.upsert_project(models.ProjectConfig(name="P", path=str(pdir), team_ids=[tm_a.id, tm_b.id]))
    _save(str(pdir), [], team_id=tm_a.id, sid="s_new")

    client = TestClient(main.app)
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"action": "start", "project_id": pr.id, "team_id": tm_b.id,
                      "session_id": "s_new", "task": "首轮议题"})
        for _ in range(80):
            try:
                ev = ws.receive_json()
            except Exception:
                break
            if ev.get("type") == "run_end":
                break

    assert store.read_session(str(pdir), "s_new")["team_id"] == tm_b.id, "首轮必须把所选 Team 写回"
