import unittest
import json

from fastapi.testclient import TestClient

from tests.base import store, models, main, TMP, fresh_config


def seed():
    ag = store.upsert_agent(models.AgentConfig(name="假A", type="fake", fake_terminate_round=1))
    tm = store.upsert_team(models.TeamConfig(name="T", agent_ids=[ag.id]))
    pdir = TMP / "api_proj"
    pdir.mkdir(parents=True, exist_ok=True)
    pr = store.upsert_project(models.ProjectConfig(name="P", path=str(pdir), team_ids=[tm.id]))
    return ag.id, tm.id, pr.id


class TestAPI(unittest.TestCase):
    def setUp(self):
        fresh_config()
        self.client = TestClient(main.app)
        self.ag_id, self.tm_id, self.pr_id = seed()
        self.pr_name, self.tm_name = "P", "T"

    def test_config_shape(self):
        r = self.client.get("/api/config")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertIn("projects", d)
        self.assertIn("teams", d)
        self.assertIn("agents", d)
        self.assertEqual(len(d["agents"]), 1)

    def test_config_includes_global_settings(self):
        d = self.client.get("/api/config").json()
        self.assertIn("settings", d)
        self.assertEqual(d["settings"]["default_timeout"], 300)
        # 默认轮次上限也作为全局设置返回（composer 轮次默认值来源）
        self.assertEqual(d["settings"]["default_max_rounds"], 12)
        # 默认模型名：key 必须存在（值可能是探测到的真实名或空串，仅校验类型）
        self.assertIn("default_model", d["settings"])
        self.assertIsInstance(d["settings"]["default_model"], str)
        self.assertIn("default_model_codex", d["settings"])
        self.assertIsInstance(d["settings"]["default_model_codex"], str)

    def test_settings_endpoint_persists(self):
        r = self.client.post("/api/settings", json={"default_timeout": 99, "default_max_rounds": 8, "default_model": "claude-sonnet-4-20250514", "default_model_codex": "deepseek-v4-flash"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["settings"]["default_timeout"], 99)
        self.assertEqual(r.json()["settings"]["default_max_rounds"], 8)
        self.assertEqual(r.json()["settings"]["default_model"], "claude-sonnet-4-20250514")
        self.assertEqual(r.json()["settings"]["default_model_codex"], "deepseek-v4-flash")
        # 再次读取确认已落盘
        self.assertEqual(self.client.get("/api/config").json()["settings"]["default_timeout"], 99)
        self.assertEqual(self.client.get("/api/config").json()["settings"]["default_max_rounds"], 8)
        self.assertEqual(self.client.get("/api/config").json()["settings"]["default_model"], "claude-sonnet-4-20250514")
        self.assertEqual(self.client.get("/api/config").json()["settings"]["default_model_codex"], "deepseek-v4-flash")

    def test_agent_has_no_timeout_field(self):
        # timeout / cwd 已移出 agent：前者全局设置，后者跟随项目目录；agent 不应再含这些字段
        r = self.client.post("/api/agents", json={"name": "X", "type": "fake"})
        a = r.json()
        self.assertNotIn("timeout", a)
        self.assertNotIn("cwd", a)
        self.assertNotIn("max_rounds", a)

    def test_agent_crud_via_api(self):
        r = self.client.post("/api/agents", json={"name": "X", "type": "fake"})
        self.assertEqual(r.status_code, 200)
        aid = r.json()["id"]
        self.assertTrue(aid)
        agents = self.client.get("/api/config").json()["agents"]
        self.assertEqual(next(a["name"] for a in agents if a["id"] == aid), "X")
        self.assertEqual(self.client.delete(f"/api/agents/{aid}").status_code, 200)

    def test_team_crud_via_api(self):
        r = self.client.post("/api/teams", json={"name": "TT", "agent_ids": [self.ag_id]})
        self.assertEqual(r.status_code, 200)
        self.assertIn(self.ag_id, r.json()["agent_ids"])

    def test_bad_agent_type_rejected(self):
        r = self.client.post("/api/agents", json={"name": "X", "type": "gemini"})
        self.assertEqual(r.status_code, 422)

    def test_sessions_endpoint(self):
        # 初始无会话
        r = self.client.get(f"/api/sessions/{self.pr_id}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["sessions"], [])
        # 新建会话并确认出现在列表中
        c = self.client.post(f"/api/sessions/{self.pr_id}", json={"title": "会话甲"})
        self.assertEqual(c.status_code, 200)
        sid = c.json()["session"]["id"]
        listed = self.client.get(f"/api/sessions/{self.pr_id}").json()["sessions"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], sid)
        # 会话详情接口
        d = self.client.get(f"/api/session/{self.pr_id}/{sid}").json()
        self.assertTrue(d["exists"])
        self.assertEqual(d["messages"], [])
        # 删除
        self.assertEqual(self.client.delete(f"/api/session/{self.pr_id}/{sid}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/sessions/{self.pr_id}").json()["sessions"], [])

    def test_project_sessions_expose_team_name(self):
        """项目会话列表必须与全局同口径带 team_name（中栏卡片的 team 徽章数据来源）。"""
        # 未挂 team：字段存在且为空串（前端 if (s.team_name) 自然不画徽章）
        c = self.client.post(f"/api/sessions/{self.pr_id}", json={"title": "无 team"})
        sid0 = c.json()["session"]["id"]
        # 挂了 team：team_name 为真实名称
        c = self.client.post(f"/api/sessions/{self.pr_id}",
                             json={"title": "有 team", "team_id": self.tm_id})
        sid1 = c.json()["session"]["id"]
        # 无论断言是否通过都清理，避免污染共享项目目录里的其他用例
        for sid in (sid0, sid1):
            self.addCleanup(self.client.delete, f"/api/session/{self.pr_id}/{sid}")
        by_id = {s["id"]: s for s in self.client.get(f"/api/sessions/{self.pr_id}").json()["sessions"]}
        self.assertIn("team_name", by_id[sid0])
        self.assertEqual(by_id[sid0]["team_name"], "")
        self.assertEqual(by_id[sid1]["team_id"], self.tm_id)
        self.assertEqual(by_id[sid1]["team_name"], self.tm_name)
        # 会话详情接口同口径返回 team_name（会话头 ctxMeta 也读它）
        d = self.client.get(f"/api/session/{self.pr_id}/{sid1}").json()
        self.assertEqual(d["team_name"], self.tm_name)

    def test_rename_session(self):
        c = self.client.post(f"/api/sessions/{self.pr_id}", json={"title": "旧标题"})
        sid = c.json()["session"]["id"]
        # 改名成功并落盘
        r = self.client.post(f"/api/session/{self.pr_id}/{sid}/title", json={"title": "新标题"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["title"], "新标题")
        self.assertEqual(self.client.get(f"/api/session/{self.pr_id}/{sid}").json()["title"], "新标题")
        # 空标题拒绝（ok=False，标题不变）
        r2 = self.client.post(f"/api/session/{self.pr_id}/{sid}/title", json={"title": "  "})
        self.assertEqual(r2.status_code, 200)
        self.assertFalse(r2.json()["ok"])
        self.assertEqual(self.client.get(f"/api/session/{self.pr_id}/{sid}").json()["title"], "新标题")
        # 清理
        self.client.delete(f"/api/session/{self.pr_id}/{sid}")

    def test_global_sessions_endpoint(self):
        # 全局 /api/sessions 跨项目扁平化，附带 project/team 名称
        c = self.client.post(f"/api/sessions/{self.pr_id}", json={"title": "会话甲", "team_id": self.tm_id})
        self.assertEqual(c.status_code, 200)
        s = c.json()["session"]
        self.assertEqual(s["team_id"], self.tm_id)  # 创建时写入 team_id
        g = self.client.get("/api/sessions").json()["sessions"]
        self.assertEqual(len(g), 1)
        item = g[0]
        self.assertEqual(item["id"], s["id"])
        self.assertEqual(item["project_id"], self.pr_id)
        self.assertEqual(item["project_name"], self.pr_name)
        self.assertEqual(item["team_id"], self.tm_id)
        self.assertEqual(item["team_name"], self.tm_name)
        # 清理，避免污染共享项目目录（TMP/api_proj/.agentchat）影响其他用例
        self.assertEqual(self.client.delete(f"/api/session/{self.pr_id}/{s['id']}").status_code, 200)

    def test_ws_start_with_fake_agent(self):
        sid = store.create_session(str(TMP / "api_proj"), "ws会话")["id"]
        with self.client.websocket_connect("/ws") as ws:
            ws.send_json({"action": "start", "project_id": self.pr_id,
                          "team_id": self.tm_id, "session_id": sid, "task": "设计登录"})
            types = []
            for _ in range(40):
                try:
                    ev = ws.receive_json()
                except Exception:
                    break
                types.append(ev.get("type"))
                if ev.get("type") == "run_end":
                    break
        self.assertIn("turn_start", types)
        self.assertIn("turn_end", types)
        self.assertIn("system", types)
        self.assertIn("human", types)     # 用户消息回显
        self.assertIn("run_end", types)   # 终止标记（前端据此恢复按钮）

    def test_prompts_endpoint_returns_log(self):
        # 提示词日志应可经接口读取，且含发给首 agent 的完整 prompt
        sid = store.create_session(str(TMP / "api_proj"), "ws会话")["id"]
        with self.client.websocket_connect("/ws") as ws:
            ws.send_json({"action": "start", "project_id": self.pr_id,
                          "team_id": self.tm_id, "session_id": sid, "task": "设计登录"})
            for _ in range(40):
                try:
                    ev = ws.receive_json()
                except Exception:
                    break
                if ev.get("type") == "run_end":
                    break
        r = self.client.get(f"/api/session/{self.pr_id}/{sid}/prompts")
        self.assertEqual(r.status_code, 200)
        prompts = r.json()["prompts"]
        self.assertTrue(prompts, "应返回非空提示词日志")
        self.assertIn("设计登录", prompts[0]["prompt"], "首条提示词应含任务文本")
        # 清理，避免污染共享项目目录（TMP/api_proj/.agentchat）影响其他用例
        self.client.delete(f"/api/session/{self.pr_id}/{sid}")

    def test_ws_inject_without_session_reports_error(self):
        # 历史 bug：没有运行中的会话时点「介入」毫无反应（后端静默丢弃）
        main.session = None
        with self.client.websocket_connect("/ws") as ws:
            ws.send_json({"action": "inject", "text": "改需求"})
            ev = ws.receive_json()
        self.assertEqual(ev.get("type"), "error")
        self.assertIn("介入", ev.get("text", ""))

    def test_ws_pause_without_session_reports_error(self):
        main.session = None
        with self.client.websocket_connect("/ws") as ws:
            ws.send_json({"action": "pause"})
            ev = ws.receive_json()
        self.assertEqual(ev.get("type"), "error")

    def test_ws_start_missing_project_errors(self):
        with self.client.websocket_connect("/ws") as ws:
            ws.send_json({"action": "start", "project_id": "nope", "team_id": "nope", "task": "x"})
            ev = ws.receive_json()
        self.assertEqual(ev.get("type"), "error")

    def test_ws_start_empty_task_reports_error(self):
        # 空议题防御：辩论模式不应对空内容发起，避免 agent 空跑一轮
        main.session = None
        with self.client.websocket_connect("/ws") as ws:
            ws.send_json({"action": "start", "project_id": self.pr_id,
                          "team_id": self.tm_id, "session_id": "s_empty", "task": "   "})
            ev = ws.receive_json()
        self.assertEqual(ev.get("type"), "error")
        self.assertIn("空", ev.get("text", ""))

    def test_ws_abort_without_running_session_reports_error(self):
        # 「中止」没有在跑的发言时给明确反馈（不静默丢弃）
        main.sessions.clear()
        with self.client.websocket_connect("/ws") as ws:
            ws.send_json({"action": "abort", "session_id": "s_none"})
            ev = ws.receive_json()
        self.assertEqual(ev.get("type"), "error")
        self.assertIn("中止", ev.get("text", ""))

    def test_ws_resume_turn_without_pending_reports_error(self):
        # 没有待续接记录时点「继续」→ 明确报错（不空跑一轮）
        main.sessions.clear()
        sid = store.create_session(str(TMP / "api_proj"), "无待续接")["id"]
        with self.client.websocket_connect("/ws") as ws:
            ws.send_json({"action": "resume_turn", "project_id": self.pr_id, "session_id": sid})
            ev = ws.receive_json()
        self.client.delete(f"/api/session/{self.pr_id}/{sid}")  # 清理，避免污染共享项目目录
        self.assertEqual(ev.get("type"), "error")
        self.assertIn("没有待继续的发言", ev.get("text", ""))

    def test_ws_abort_allowed_in_chat_mode(self):
        # 「中止」是 turn 级能力，两种模式都放行——单 agent 对话最容易被死循环卡住
        # （对比：pause/resume/stop 在 chat 模式必须继续被拒，二者互不干扰）
        sid = store.create_session(str(TMP / "api_proj"), "对话")["id"]
        with self.client.websocket_connect("/ws") as ws:
            ws.send_json({"action": "start", "project_id": self.pr_id,
                          "team_id": self.tm_id, "session_id": sid, "task": "你好", "max_rounds": 1})
            ws.receive_json()  # 会话开始 / turn_start 等首个事件
            ws.send_json({"action": "abort", "session_id": sid})
            seen = []
            for _ in range(30):
                ev = ws.receive_json()
                seen.append(ev)
                if ev.get("type") == "run_end":
                    break
        self.client.delete(f"/api/session/{self.pr_id}/{sid}")  # 清理，避免污染共享项目目录
        texts = [e.get("text", "") + e.get("msg", "") for e in seen]
        self.assertFalse(any("不适用" in t for t in texts), "chat 模式下「中止」不应被当作不适用动作拒绝")

    def test_get_session_exposes_pending_turn(self):
        # 前端靠 GET 会话的 pending_turn 决定主按钮是否显示「继续」（刷新后仍要准）
        sid = store.create_session(str(TMP / "api_proj"), "待续接")["id"]
        store.set_pending_turn(str(TMP / "api_proj"), sid, {"agent": "假A", "round_i": 0})
        d = self.client.get(f"/api/session/{self.pr_id}/{sid}").json()
        self.client.delete(f"/api/session/{self.pr_id}/{sid}")  # 清理，避免污染共享项目目录
        self.assertEqual(d.get("pending_turn"), {"agent": "假A", "round_i": 0})

    def test_fs_list_returns_subdirs(self):
        # 建一个含子目录的临时目录
        base = TMP / "fs_test"
        base.mkdir(parents=True, exist_ok=True)
        (base / "sub_a").mkdir(exist_ok=True)
        (base / "sub_b").mkdir(exist_ok=True)
        r = self.client.get("/api/fs", params={"path": str(base)})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["path"], str(base))
        names = [x["name"] for x in d["dirs"]]
        self.assertIn("sub_a", names)
        self.assertIn("sub_b", names)
        # 不返回父目录自身，且返回 parent 供前端「上一级」
        self.assertIn("parent", d)


    def test_ws_parallel_sessions_both_run(self):
        # 多会话并行：会话 A 正在跑时，会话 B 可正常发起（不被全局锁拒绝），两会话独立 turn
        sid_a = store.create_session(str(TMP / "api_proj"), "并行A")["id"]
        sid_b = store.create_session(str(TMP / "api_proj"), "并行B")["id"]
        with self.client.websocket_connect("/ws") as ws:
            ws.send_json({"action": "start", "project_id": self.pr_id,
                          "team_id": self.tm_id, "session_id": sid_a, "task": "任务A", "max_rounds": 50})
            # 等 A 开跑（收到首个 turn_start）
            got_a_start = False
            for _ in range(20):
                ev = ws.receive_json()
                if ev.get("type") == "turn_start":
                    got_a_start = True
                    break
            self.assertTrue(got_a_start, "A 应开始运行")
            # A 还在跑时发起 B：不应被拒，B 应正常开始
            ws.send_json({"action": "start", "project_id": self.pr_id,
                          "team_id": self.tm_id, "session_id": sid_b, "task": "任务B", "max_rounds": 2})
            got_b_start, got_err = False, False
            for _ in range(30):
                ev = ws.receive_json()
                if ev.get("type") == "turn_start":
                    got_b_start = True
                    break
                if ev.get("type") == "error":
                    got_err = True
                    break
            self.assertFalse(got_err, "并行会话 B 不应被拒绝")
            self.assertTrue(got_b_start, "B 应正常开始（与 A 并行）")
            # 停止 A（避免后台任务挂住测试进程）；B 会自己跑完（max_rounds=2）
            ws.send_json({"action": "stop", "session_id": sid_a})
        self.client.delete(f"/api/session/{self.pr_id}/{sid_a}")
        self.client.delete(f"/api/session/{self.pr_id}/{sid_b}")

    def test_tools_endpoint_returns_log(self):
        # 工具调用日志（end_discussion）应可经接口读取（本用例 fake 无 tool_use，返回空数组即可）
        sid = store.create_session(str(TMP / "api_proj"), "工具日志")["id"]
        with self.client.websocket_connect("/ws") as ws:
            ws.send_json({"action": "start", "project_id": self.pr_id,
                          "team_id": self.tm_id, "session_id": sid, "task": "设计登录", "max_rounds": 1})
            for _ in range(30):
                try:
                    ev = ws.receive_json()
                except Exception:
                    break
                if ev.get("type") == "run_end":
                    break
        r = self.client.get(f"/api/session/{self.pr_id}/{sid}/tools")
        self.assertEqual(r.status_code, 200)
        self.assertIn("tools", r.json())
        self.client.delete(f"/api/session/{self.pr_id}/{sid}")

if __name__ == "__main__":
    unittest.main()
