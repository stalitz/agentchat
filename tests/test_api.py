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


if __name__ == "__main__":
    unittest.main()
