import os
import unittest
import json

from tests.base import store, models, fresh_config, TMP


class TestStore(unittest.TestCase):
    def setUp(self):
        # 每个用例用独立干净存储
        fresh_config()

    def test_get_settings_defaults_include_new_fields(self):
        # 新配置项缺省兜底：输出上限/思考开关/提示词模板（空配置不报错、有默认值）
        s = store.get_settings()
        self.assertEqual(s["ollama_think"], True, "思考开关默认开（保持现状）")
        self.assertEqual(s["claude_max_output_tokens"], 64000, "输出上限默认 64000")
        self.assertEqual(s["prompt_templates"], {}, "提示词模板默认空（回退内置默认）")
        # 写入后可读回
        store.update_settings({"ollama_think": False, "claude_max_output_tokens": 99999,
                               "prompt_templates": {"debate": {"consensus": "自定义"}}})
        s2 = store.get_settings()
        self.assertIs(s2["ollama_think"], False)
        self.assertEqual(s2["claude_max_output_tokens"], 99999)
        self.assertEqual(s2["prompt_templates"]["debate"]["consensus"], "自定义")

    def test_agent_crud(self):
        a = models.AgentConfig(name="架构师", type="claude-code", role="r")
        saved = store.upsert_agent(a)
        self.assertTrue(saved.id)
        self.assertEqual(store.get_agent(saved.id)["name"], "架构师")
        self.assertIn(saved.id, store.list_agents())
        store.delete_agent(saved.id)
        self.assertIsNone(store.get_agent(saved.id))

    def test_persistence_across_reload(self):
        a = store.upsert_agent(models.AgentConfig(name="A", type="fake"))
        # 重新从磁盘读取（模拟新进程）
        data = json.loads(store.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertIn(a.id, data["agents"])

    def test_team_and_project(self):
        ag = store.upsert_agent(models.AgentConfig(name="A", type="fake"))
        tm = store.upsert_team(models.TeamConfig(name="T", agent_ids=[ag.id]))
        pr = store.upsert_project(models.ProjectConfig(name="P", path="/tmp/x", team_ids=[tm.id]))
        self.assertEqual(store.get_team(tm.id)["agent_ids"], [ag.id])
        self.assertEqual(store.get_project(pr.id)["team_ids"], [tm.id])
        store.delete_project(pr.id)
        self.assertIsNone(store.get_project(pr.id))

    def test_session_crud(self):
        p = TMP / "sp"
        s = store.create_session(str(p), "会话一")
        self.assertTrue(s["id"])
        self.assertEqual(store.read_session(str(p), s["id"])["title"], "会话一")
        store.append_message(str(p), s["id"], {"round": 0, "speaker": "A", "content": "hi"})
        sess = store.read_session(str(p), s["id"])
        self.assertEqual(len(sess["messages"]), 1)
        self.assertEqual(sess["messages"][0]["content"], "hi")
        # 列表返回元数据（含计数与预览），按 updated_at 倒序
        listed = store.list_sessions(str(p))
        self.assertEqual(listed[0]["id"], s["id"])
        self.assertEqual(listed[0]["message_count"], 1)
        self.assertEqual(listed[0]["preview"], "hi")
        # 删除
        store.delete_session(str(p), s["id"])
        self.assertIsNone(store.read_session(str(p), s["id"]))

    def test_set_session_title(self):
        p = TMP / "stitle"
        s = store.create_session(str(p), "会话一")
        # 改名成功
        self.assertTrue(store.set_session_title(str(p), s["id"], "重命名"))
        self.assertEqual(store.read_session(str(p), s["id"])["title"], "重命名")
        # 空标题拒绝（返回 False，且原标题不变）
        self.assertFalse(store.set_session_title(str(p), s["id"], "   "))
        self.assertEqual(store.read_session(str(p), s["id"])["title"], "重命名")
        # 不存在的会话返回 False
        self.assertFalse(store.set_session_title(str(p), "nope", "x"))

    def test_agent_session_persist_roundtrip(self):
        p = TMP / "smp"
        s = store.create_session(str(p), "会话二")
        self.assertEqual(store.get_agent_sessions(str(p), s["id"]), {})
        store.set_agent_session_id(str(p), s["id"], "A", "sid-aaa")
        store.set_agent_session_id(str(p), s["id"], "B", "sid-bbb")
        self.assertEqual(store.get_agent_sessions(str(p), s["id"]),
                         {"A": "sid-aaa", "B": "sid-bbb"})
        # 已写盘（重读一致）
        from pathlib import Path
        raw = Path(p) / ".agentchat" / "sessions.json"
        import json as _json
        data = _json.loads(raw.read_text(encoding="utf-8"))
        self.assertEqual(data[0]["agent_sessions"]["A"], "sid-aaa")

    def test_append_message_adds_timestamp(self):
        # 每条消息落盘自动带 ts（时间线）：前端据此展示发言时间，历史数据由回填脚本补齐
        p = TMP / "ts"
        s = store.create_session(str(p), "时间线会话")
        store.append_message(str(p), s["id"], {"round": 0, "speaker": "human", "content": "你好"})
        m = store.read_session(str(p), s["id"])["messages"][0]
        self.assertTrue(m.get("ts"), "落盘消息应自动带 ts 时间戳")
        self.assertRegex(m["ts"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        # 显式传入的 ts 不被覆盖
        store.append_message(str(p), s["id"], {"round": 1, "speaker": "A", "content": "回", "ts": "2026-08-17 16:00:00"})
        self.assertEqual(store.read_session(str(p), s["id"])["messages"][1]["ts"], "2026-08-17 16:00:00")

    def test_reconcile_overwrites_when_count_matches(self):
        # 数量一致（CLI 回合数 == 本地该 agent 消息数）：按顺序覆盖 content/thinking（以 agent 为准）
        p = TMP / "rec"
        s = store.create_session(str(p), "对账会话")
        # 本地：A 两条（内容过时/与 agent 不一致），B 一条
        store.append_message(str(p), s["id"], {"round": 0, "speaker": "A", "role": "甲", "content": "本地旧答案A1"})
        store.append_message(str(p), s["id"], {"round": 1, "speaker": "B", "role": "乙", "content": "B的回答"})
        store.append_message(str(p), s["id"], {"round": 2, "speaker": "A", "role": "甲", "content": "本地旧答案A2"})
        cli_turns = [
            {"content": "agent真实答案A1", "thinking": "A1的思考",
             "blocks": [{"type": "thinking", "thinking": "A1的思考"}, {"type": "text", "text": "agent真实答案A1"}]},
            {"content": "agent真实答案A2", "thinking": "A2的思考",
             "blocks": [{"type": "text", "text": "agent真实答案A2"}]},
        ]
        res = store.reconcile_agent_messages(str(p), s["id"], "A", cli_turns)
        self.assertEqual(res["changed"], 6)   # A1/A2 各 content+thinking+blocks 被覆盖
        self.assertEqual(res["appended"], 0)
        msgs = store.read_session(str(p), s["id"])["messages"]
        # B 不受影响
        self.assertEqual([m for m in msgs if m["speaker"] == "B"][0]["content"], "B的回答")
        a_msgs = [m for m in msgs if m["speaker"] == "A"]
        self.assertEqual(a_msgs[0]["content"], "agent真实答案A1")
        self.assertEqual(a_msgs[0]["thinking"], "A1的思考")
        self.assertEqual(a_msgs[1]["content"], "agent真实答案A2")
        self.assertEqual(a_msgs[1]["thinking"], "A2的思考")
        # blocks 必须随 transcript 一起落盘（旧消息无 blocks 时刷新重建时间线，不回退固定顺序）
        self.assertEqual(a_msgs[0]["blocks"], cli_turns[0]["blocks"])
        self.assertEqual(a_msgs[1]["blocks"], cli_turns[1]["blocks"])

    def test_reconcile_appends_cli_only_when_no_match(self):
        # 数量不一致且本地无内容匹配：CLI 多出的回合追加（崩写/掉轮恢复）；本地旧内容保留
        p = TMP / "rec5"
        s = store.create_session(str(p), "掉轮恢复")
        store.append_message(str(p), s["id"], {"round": 0, "speaker": "A", "role": "甲", "content": "本地已有回答"})
        cli_turns = [
            {"content": "agent真实答案A1", "thinking": "A1的思考"},
            {"content": "agent真实答案A2", "thinking": ""},
        ]
        res = store.reconcile_agent_messages(str(p), s["id"], "A", cli_turns)
        self.assertEqual(res["appended"], 2)
        a_msgs = [m for m in store.read_session(str(p), s["id"])["messages"] if m["speaker"] == "A"]
        self.assertEqual(a_msgs[0]["content"], "本地已有回答", "本地内容不与 cli 匹配时保留（不顺序覆盖）")
        self.assertEqual(a_msgs[1]["content"], "agent真实答案A1")
        self.assertEqual(a_msgs[2]["content"], "agent真实答案A2")

    def test_reconcile_skips_claude_error_turns(self):
        # claude 外壳的错误文本回合（API Error / No response requested.）不是有效发言：
        # reconcile 对账时必须剔除，不得匹配/追加回本地当发言（s_54d1232e 曾因此被带回）
        p = TMP / "rec6"
        s = store.create_session(str(p), "错误过滤")
        store.append_message(str(p), s["id"], {"round": 0, "speaker": "A", "role": "甲", "content": "正常回答1"})
        cli_turns = [
            {"content": "API Error: API returned an empty or malformed response (HTTP 200)", "thinking": ""},
            {"content": "正常回答2", "thinking": ""},
            {"content": "正常回答3", "thinking": ""},
        ]
        res = store.reconcile_agent_messages(str(p), s["id"], "A", cli_turns)
        a_msgs = [m for m in store.read_session(str(p), s["id"])["messages"] if m["speaker"] == "A"]
        self.assertEqual(len(a_msgs), 3, "错误文本回合被过滤，仅追加正常回合")
        self.assertEqual(a_msgs[0]["content"], "正常回答1")
        self.assertEqual(a_msgs[1]["content"], "正常回答2")
        self.assertEqual(a_msgs[2]["content"], "正常回答3")
        self.assertTrue(all(not store._is_claude_error_text(m["content"]) for m in a_msgs))

    def test_reconcile_skips_when_no_cli_turns(self):
        # 找不到 transcript（离线 / 全新会话）→ 不动本地，作为回退
        p = TMP / "rec2"
        s = store.create_session(str(p), "离线会话")
        store.append_message(str(p), s["id"], {"round": 0, "speaker": "A", "content": "仅本地"})
        res = store.reconcile_agent_messages(str(p), s["id"], "A", None)
        self.assertTrue(res["skipped"])
        self.assertEqual(store.read_session(str(p), s["id"])["messages"][0]["content"], "仅本地")

    def test_reconcile_removes_extra_when_cli_fewer(self):
        # CLI 回合数少于本地（本地曾有拆条残留）：只删除「think-only 残留」（content 空、thinking 非空），
        # 有 content 的条目保守保留——transcript 可能因 auto-compact/截断而不完整，盲删会丢真实历史
        # （实测 s_2289e1cf 曾因此丢 4 条回复，数据已从备份恢复并收紧删除条件）。
        p = TMP / "rec3"
        s = store.create_session(str(p), "拆条修复")
        store.append_message(str(p), s["id"], {"round": 0, "speaker": "A", "role": "甲", "content": "完整回答1", "thinking": "思考1"})
        store.append_message(str(p), s["id"], {"round": 1, "speaker": "B", "role": "乙", "content": "B的回答"})
        store.append_message(str(p), s["id"], {"round": 2, "speaker": "A", "role": "甲", "content": "", "thinking": "思考2残留"})
        store.append_message(str(p), s["id"], {"round": 3, "speaker": "A", "role": "甲", "content": "完整回答2", "thinking": "思考2"})
        store.append_message(str(p), s["id"], {"round": 4, "speaker": "A", "role": "甲", "content": "", "thinking": "思考2残留2"})
        cli_turns = [
            {"content": "完整回答1", "thinking": "思考1"},
            {"content": "完整回答2", "thinking": "思考2"},
        ]
        res = store.reconcile_agent_messages(str(p), s["id"], "A", cli_turns)
        msgs = store.read_session(str(p), s["id"])["messages"]
        a_msgs = [m for m in msgs if m["speaker"] == "A"]
        # 内容匹配覆盖：完整回答1 ↔ 本地第1条；完整回答2 ↔ 本地「完整回答2」（而非空位的思考2残留）；
        # 两条 think-only 残留（思考2残留/思考2残留2）被删 → 收敛为 2 条，无重复无丢失
        self.assertEqual(len(a_msgs), 2, "think-only 残留删除；内容匹配不覆盖空位")
        self.assertEqual(a_msgs[0]["content"], "完整回答1")
        self.assertEqual(a_msgs[1]["content"], "完整回答2")
        self.assertEqual(a_msgs[1]["thinking"], "思考2")
        # B 不受影响
        self.assertEqual([m for m in msgs if m["speaker"] == "B"][0]["content"], "B的回答")
        self.assertTrue(res["appended"] == 0)

    def test_reconcile_keeps_content_when_cli_fewer(self):
        # CLI 回合少于本地且多出的条目**有 content**：一律保留，绝不盲删——
        # transcript 可能因 auto-compact/截断而不完整（实测 s_2289e1cf：cli 3 条 vs 本地 7 条
        # 真实回复，旧逻辑盲删丢了 4 条历史）；只删「think-only 残留」（content 空、thinking 非空）。
        p = TMP / "rec4"
        s = store.create_session(str(p), "防误删")
        store.append_message(str(p), s["id"], {"round": 0, "speaker": "A", "role": "甲", "content": "回答1", "thinking": ""})
        store.append_message(str(p), s["id"], {"round": 1, "speaker": "B", "role": "乙", "content": "B的回答"})
        store.append_message(str(p), s["id"], {"round": 2, "speaker": "A", "role": "甲", "content": "回答2", "thinking": ""})
        store.append_message(str(p), s["id"], {"round": 3, "speaker": "A", "role": "甲", "content": "", "thinking": "思考3残留"})
        store.append_message(str(p), s["id"], {"round": 4, "speaker": "A", "role": "甲", "content": "回答4", "thinking": ""})
        # transcript 只记录到 A 的前 3 次回复（后续被截断）：回答4 必须保留
        cli_turns = [
            {"content": "回答1", "thinking": ""},
            {"content": "回答2", "thinking": ""},
            {"content": "", "thinking": "思考3"},
        ]
        res = store.reconcile_agent_messages(str(p), s["id"], "A", cli_turns)
        msgs = store.read_session(str(p), s["id"])["messages"]
        a_msgs = [m for m in msgs if m["speaker"] == "A"]
        # 覆盖：回答1/回答2 内容匹配；思考3残留（think-only 未匹配）按残留删除；
        # 回答4 有 content → 保留（防 transcript 不完整误删）
        self.assertEqual(len(a_msgs), 3, "think-only 残留删除；有 content 的多出条目必须保留")
        self.assertEqual(a_msgs[2]["content"], "回答4")
        # B 不受影响
        self.assertEqual([m for m in msgs if m["speaker"] == "B"][0]["content"], "B的回答")



        self.assertEqual(store.list_sessions(str(TMP / "nope")), [])

    def test_detect_claude_default_model_from_env(self):
        old = os.environ.get("ANTHROPIC_DEFAULT_MODEL")
        os.environ["ANTHROPIC_DEFAULT_MODEL"] = "claude-env-model"
        try:
            self.assertEqual(store.detect_claude_default_model(), "claude-env-model")
        finally:
            if old is None:
                os.environ.pop("ANTHROPIC_DEFAULT_MODEL", None)
            else:
                os.environ["ANTHROPIC_DEFAULT_MODEL"] = old

    def test_default_model_pin_overrides_detection(self):
        # 设置 pin 后，get_settings 返回 pin 值而非探测值（探测依赖文件系统，pin 应稳定覆盖）
        store.update_settings({"default_model": "pinned-model"})
        self.assertEqual(store.get_settings()["default_model"], "pinned-model")

    def test_detect_claude_from_settings_env_block(self):
        # 真实场景（用户机器）：默认模型写在 ~/.claude/settings.json 的 env.ANTHROPIC_MODEL，
        # 而非顶层 model 字段。探测必须能取到这一层。
        real = store._read_json_file
        store._read_json_file = lambda p: (  # noqa: E731
            {"env": {"ANTHROPIC_MODEL": "deepseek-v4-flash"}} if "settings.json" in p else {}
        )
        old = os.environ.pop("ANTHROPIC_MODEL", None)
        old2 = os.environ.pop("ANTHROPIC_DEFAULT_MODEL", None)
        try:
            self.assertEqual(store.detect_claude_default_model(), "deepseek-v4-flash")
        finally:
            store._read_json_file = real
            if old is not None:
                os.environ["ANTHROPIC_MODEL"] = old
            if old2 is not None:
                os.environ["ANTHROPIC_DEFAULT_MODEL"] = old2

    def test_detect_claude_from_anthropic_model_env(self):
        old = os.environ.get("ANTHROPIC_MODEL")
        os.environ["ANTHROPIC_MODEL"] = "claude-env-x"
        try:
            self.assertEqual(store.detect_claude_default_model(), "claude-env-x")
        finally:
            if old is None:
                os.environ.pop("ANTHROPIC_MODEL", None)
            else:
                os.environ["ANTHROPIC_MODEL"] = old

    def test_detect_codex_default_model_from_env(self):
        old = [os.environ.get(e) for e in ("CODEX_MODEL", "OPENAI_MODEL", "OPENAI_API_MODEL")]
        os.environ["CODEX_MODEL"] = "codex-env-model"
        try:
            self.assertEqual(store.detect_codex_default_model(), "codex-env-model")
        finally:
            for e, val in zip(("CODEX_MODEL", "OPENAI_MODEL", "OPENAI_API_MODEL"), old):
                if val is None:
                    os.environ.pop(e, None)
                else:
                    os.environ[e] = val

    def test_default_model_codex_pin_overrides_detection(self):
        store.update_settings({"default_model_codex": "pinned-codex-model"})
        self.assertEqual(store.get_settings()["default_model_codex"], "pinned-codex-model")


if __name__ == "__main__":
    unittest.main()
