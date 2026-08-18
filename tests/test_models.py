import unittest
from tests.base import models


class TestModels(unittest.TestCase):
    def test_defaults(self):
        a = models.AgentConfig(name="x", type="fake")
        self.assertEqual(a.fake_terminate_round, 0)
        self.assertIsNone(a.model)
        # max_rounds / timeout / cwd 已移出 agent：
        # 前者会话级，后两者全局/项目级（运行目录跟随所属项目）
        self.assertFalse(hasattr(a, "max_rounds"))
        self.assertFalse(hasattr(a, "timeout"))
        self.assertFalse(hasattr(a, "cwd"))

    def test_settings_default(self):
        s = models.AppSettings()
        self.assertEqual(s.default_timeout, 300)
        # 辩论模式默认轮次上限也作为全局设置项，默认 12
        self.assertEqual(s.default_max_rounds, 12)
        # claude-code 默认模型名：留空=运行时自动探测 Claude Code 当前默认
        self.assertEqual(s.default_model, "")
        # codex 默认模型名：留空=运行时自动探测 Codex 当前默认
        self.assertEqual(s.default_model_codex, "")

    def test_id_optional_empty(self):
        a = models.AgentConfig(name="x", type="fake")
        self.assertEqual(a.id, "")  # 服务端生成

    def test_reject_bad_type(self):
        with self.assertRaises(Exception):
            models.AgentConfig(name="x", type="gemini")

    def test_accepts_all_three_types(self):
        for t in ("claude-code", "codex", "fake"):
            a = models.AgentConfig(name="x", type=t)
            self.assertEqual(a.type, t)

    def test_fake_terminate_round_field(self):
        a = models.AgentConfig(name="x", type="fake", fake_terminate_round=3)
        self.assertEqual(a.fake_terminate_round, 3)


if __name__ == "__main__":
    unittest.main()
