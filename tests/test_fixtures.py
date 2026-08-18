import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "fixtures" / "mock-config.json"
SESSIONS = ROOT / "fixtures" / "mock-project" / ".agentchat" / "sessions.json"


class TestMockData(unittest.TestCase):
    def test_fixture_config_valid(self):
        d = json.loads(FIXTURE.read_text(encoding="utf-8"))
        for section in ("projects", "teams", "agents"):
            self.assertIn(section, d)
        # 调试 Team 必须含 fake agent
        agents = d["agents"]
        fake_ids = [aid for aid, a in agents.items() if a["type"] == "fake"]
        self.assertTrue(fake_ids)
        debug_team = next(t for t in d["teams"].values() if "调试" in t["name"])
        self.assertTrue(set(debug_team["agent_ids"]) & set(fake_ids))

    def test_fixture_sessions_present(self):
        sessions = json.loads(SESSIONS.read_text(encoding="utf-8"))
        self.assertIsInstance(sessions, list)
        self.assertTrue(sessions)
        s = sessions[0]
        self.assertIn("id", s)
        self.assertIn("messages", s)
        self.assertTrue(s["messages"])
        self.assertIn("agent_sessions", s, "会话应含 agent_sessions 字段（各 agent 持续 CLI 会话映射）")


if __name__ == "__main__":
    unittest.main()
