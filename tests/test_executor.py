import asyncio
import json
import os
import unittest

from tests.base import models, executor
from backend.gateway import THINK_SENT, ANSW_SENT


def collect(agent, prompt, meta=None, cli_session_id=None):
    out = []
    async def run():
        rt = executor.AgentRuntime(agent, cli_session_id)
        async for kind, text in rt.run(prompt, "/tmp", 30, meta=meta or {}):
            out.append((kind, text))
    asyncio.run(run())
    return out


class TestExecutorFake(unittest.TestCase):
    def test_fake_streams_text_then_done(self):
        a = models.AgentConfig(name="F", type="fake", role="调试")
        out = collect(a, "任务：设计登录")
        kinds = [k for k, _ in out]
        self.assertIn("text", kinds)
        self.assertEqual(kinds[-1], "done")
        self.assertNotIn("error", kinds)

    def test_fake_does_not_invoke_cli(self):
        # fake 类型不应产生 error（即不会去执行 claude/codex 子进程）
        a = models.AgentConfig(name="F", type="fake")
        out = collect(a, "x")
        self.assertFalse(any(k == "error" for k, _ in out))

    def test_fake_terminate_at_round(self):
        # fake 收敛改为结构化工具调用（end_discussion），不再输出 TERMINATE 文本
        a = models.AgentConfig(name="F", type="fake", fake_terminate_round=2)
        out = collect(a, "任务", meta={"round_index": 1, "max_rounds": 5})
        full = "".join(t for k, t in out if k == "text")
        self.assertNotIn("TERMINATE", full)
        tools = [t for k, t in out if k == "tool_use"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "end_discussion")
        self.assertIn("reason", tools[0]["input"])

    def test_fake_no_terminate_before_round(self):
        a = models.AgentConfig(name="F", type="fake", fake_terminate_round=3)
        out = collect(a, "任务", meta={"round_index": 0, "max_rounds": 5})
        full = "".join(t for k, t in out if k == "text")
        self.assertNotIn("TERMINATE", full)
        self.assertEqual([t for k, t in out if k == "tool_use"], [], "未到轮次不应发出 end_discussion")

    def test_fake_is_stateful_across_turns(self):
        # 同一 AgentRuntime 实例跨轮应记住前文（模拟有状态 CLI 会话），用作 1:1 续接的忠实性校验
        a = models.AgentConfig(name="F", type="fake", role="架构师")
        rt = executor.AgentRuntime(a)
        async def run():
            async for _k, _t in rt.run("第一轮", "/tmp", 30, meta={"round_index": 0, "max_rounds": 5}):
                pass
            async for _k, _t in rt.run("第二轮", "/tmp", 30, meta={"round_index": 1, "max_rounds": 5}):
                pass
        asyncio.run(run())
        self.assertEqual(len(rt._fake_history), 2, "同一 runtime 应跨轮累积自身历史")


class TestClaudeGatewayEnv(unittest.TestCase):
    """agent 级「本地模型网关」隔离：拉起 claude 子进程时注入 env，不动 Claude Code 配置、不串默认后端。"""

    def test_env_overrides_base_url_when_set(self):
        a = models.AgentConfig(name="G", type="claude-code", model="ollama/gemma",
                                base_url="http://localhost:4000", api_key="")
        rt = executor.AgentRuntime(a)
        env = rt._claude_env()
        # 关键隔离保证：ANTHROPIC_BASE_URL 被强制覆盖为该 agent 的网关，而非继承全局配置
        self.assertEqual(env.get("ANTHROPIC_BASE_URL"), "http://localhost:4000")
        # key 头必须存在（claude 要求），网关侧放行任意值
        self.assertIn("ANTHROPIC_API_KEY", env)
        self.assertTrue(env["ANTHROPIC_API_KEY"])
        # 自定义 model 注册，使 claude 接受任意 model id
        self.assertEqual(env.get("ANTHROPIC_CUSTOM_MODEL_OPTION"), "ollama/gemma")

    def test_env_does_not_inject_when_no_base_url(self):
        # 未配置网关的 claude-code agent：不应主动注入 ANTHROPIC_BASE_URL（沿用 Claude Code 全局配置）
        a = models.AgentConfig(name="C", type="claude-code", model="claude-opus-4")
        rt = executor.AgentRuntime(a)
        env = rt._claude_env()
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertNotIn("ANTHROPIC_CUSTOM_MODEL_OPTION", env)

    def test_claude_config_dir_isolation(self):
        # 带 base_url 的 claude agent：必须生成隔离配置目录，使 per-agent 网关真正生效
        # （claude 2.x 会把 ~/.claude/settings.json 的 env 覆盖同名进程 env，仅靠子进程 env 注入会被全局配置盖掉）
        a = models.AgentConfig(name="G", type="claude-code", model="gemma4:26b",
                                base_url="http://localhost:8777", api_key="")
        rt = executor.AgentRuntime(a)
        cfg = rt._claude_config_dir()
        self.assertIsNotNone(cfg)
        self.assertTrue(os.path.isdir(cfg))
        with open(os.path.join(cfg, "settings.json"), encoding="utf-8") as f:
            s = json.load(f)
        self.assertEqual(s["env"]["ANTHROPIC_BASE_URL"], "http://localhost:8777")
        # 去掉插件，避免临时目录缺失插件文件导致 claude 加载报错
        self.assertNotIn("enabledPlugins", s)
        env = rt._claude_env()
        self.assertEqual(env.get("CLAUDE_CONFIG_DIR"), cfg)
        # 隔离目录的 projects/ 必须软链到标准 ~/.claude/projects，使按目录的会话可被裸 claude 恢复、跨模型 resume
        link = os.path.join(cfg, "projects")
        self.assertTrue(os.path.islink(link), "projects 应为指向 ~/.claude/projects 的软链")
        self.assertEqual(os.path.realpath(link), os.path.realpath(os.path.join(os.path.expanduser("~"), ".claude", "projects")))
        # 无 base_url 的 agent 不应进入隔离逻辑
        self.assertIsNone(executor.AgentRuntime(models.AgentConfig(name="C", type="claude-code"))._claude_config_dir())

    def test_cmd_includes_model_flag(self):
        a = models.AgentConfig(name="G", type="claude-code", model="ollama/gemma",
                                base_url="http://localhost:4000")
        rt = executor.AgentRuntime(a)
        cmd = rt._claude_cmd("议题：X")
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "ollama/gemma")
        # 首次启动（cli_session_id 为 None）：用 --session-id 注册新 claude 会话；prompt 在末尾
        self.assertIn("--session-id", cmd)
        self.assertEqual(cmd[-1], "议题：X")

    def test_cmd_uses_resume_when_continuing(self):
        # 续接（含历史会话恢复 / 同会话后续轮）：必须用 --resume，而非复用 --session-id
        a = models.AgentConfig(name="G", type="claude-code", model="ollama/gemma",
                                base_url="http://localhost:4000")
        rt = executor.AgentRuntime(a, cli_session_id="11111111-2222-3333-4444-555555555555")
        cmd = rt._claude_cmd("议题：X")
        self.assertIn("--resume", cmd)
        self.assertNotIn("--session-id", cmd)
        self.assertEqual(cmd[cmd.index("--resume") + 1], "11111111-2222-3333-4444-555555555555")

    def test_cmd_without_model_has_no_model_flag(self):
        a = models.AgentConfig(name="C", type="claude-code")
        rt = executor.AgentRuntime(a)
        cmd = rt._claude_cmd("hi")
        self.assertNotIn("--model", cmd)


class TestParseClaudeLine(unittest.TestCase):
    def test_assistant_content_text(self):
        # claude -p stream-json 真实形态：文本在 assistant.message.content[].text
        line = json.dumps({"type": "assistant",
                           "message": {"content": [{"type": "text", "text": "你好"}]}})
        self.assertEqual(executor._parse_claude_line(line), [("text", "你好")])

    def test_delta_text_incremental(self):
        line = json.dumps({"type": "content_block_delta",
                           "delta": {"type": "text_delta", "text": "hi"}})
        self.assertEqual(executor._parse_claude_line(line), [("text", "hi")])

    def test_assistant_thinking_and_text_separated(self):
        # 思考模型：assistant 整块含 thinking + text，应分离为独立事件
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "让我想想"},
            {"type": "text", "text": "答案是 42"},
        ]}})
        self.assertEqual(executor._parse_claude_line(line),
                         [("thinking", "让我想想"), ("text", "答案是 42")])

    def test_thinking_delta_incremental(self):
        line = json.dumps({"type": "content_block_delta",
                           "delta": {"type": "thinking_delta", "thinking": "嗯"}})
        self.assertEqual(executor._parse_claude_line(line), [("thinking", "嗯")])

    def test_result_error_yields_error(self):
        line = json.dumps({"type": "result", "subtype": "error", "is_error": True,
                           "result": "Execution error: x"})
        self.assertEqual(executor._parse_claude_line(line), [("error", "Execution error: x")])

    def test_result_success_not_duplicated(self):
        # 成功结果已在 assistant 事件中产出，result 行不再重复吐文本
        line = json.dumps({"type": "result", "subtype": "success", "result": "final"})
        self.assertEqual(executor._parse_claude_line(line), [])

    def test_user_tool_result_error_yields_error(self):
        # claude-code 把失败工具结果以 user 事件里的 tool_result 块发出，必须解析为 error 事件
        line = json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_xxx", "is_error": True,
             "content": "<tool_use_error>Error: No such tool available: end_discussion</tool_use_error>"}]}})
        self.assertEqual(executor._parse_claude_line(line),
                         [("error", "Error: No such tool available: end_discussion")])

    def test_user_tool_result_success_ignored(self):
        # 成功的 tool_result 已在 assistant 文本中体现，不重复产出
        line = json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_xxx", "is_error": False,
             "content": "ok"}]}})
        self.assertEqual(executor._parse_claude_line(line), [])

    def test_non_json_line_is_text(self):
        self.assertEqual(executor._parse_claude_line("plain text"), [("text", "plain text")])

    def _rt(self):
        return executor.AgentRuntime(models.AgentConfig(name="G", type="claude-code"))

    def test_think_answer_split_across_events(self):
        # 思考模型：claude 把 reasoning(块0) 与 answer(块1) 拆成多个独立 assistant 事件，
        # 网关在首个分片加哨兵；executor 应按哨兵归属「思考/答案」且剥离哨兵、不残留。
        rt = self._rt()
        evs = []
        evs += executor._parse_claude_line(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": THINK_SENT + "User identity statement..."}]}}), rt)
        evs += executor._parse_claude_line(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "continue reasoning"}]}}), rt)
        evs += executor._parse_claude_line(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": ANSW_SENT + "我是 Claude"}]}}), rt)
        evs += executor._parse_claude_line(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "，一个AI助手。"}]}}), rt)
        self.assertEqual(evs, [
            ("thinking", "User identity statement..."),
            ("thinking", "continue reasoning"),
            ("text", "我是 Claude"),
            ("text", "，一个AI助手。"),
        ])
        self.assertFalse(any("\x01" in t for _, t in evs), "哨兵残留")

    def test_think_answer_merged_single_chunk(self):
        # claude 把 reasoning+answer 合并进同一文本块（哨兵在文本中部，前有 gemma 自带 --- 分隔符）：
        # 必须扫描哨兵切分，而非仅看开头。
        rt = self._rt()
        merged = "---" + THINK_SENT + "Target Language: Chinese.\nProblem: chicken/rabbit" + ANSW_SENT + "鸡 4 只，兔 6 只。"
        evs = executor._parse_claude_line(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": merged}]}}), rt)
        self.assertIn(("thinking", "Target Language: Chinese.\nProblem: chicken/rabbit"), evs)
        self.assertIn(("text", "鸡 4 只，兔 6 只。"), evs)
        self.assertFalse(any("\x01" in t for _, t in evs), "哨兵残留")

    def test_no_thinking_model_single_block(self):
        # 非思考模型：唯一文本块无哨兵 → 默认答案
        rt = self._rt()
        evs = executor._parse_claude_line(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "直接给答案"}]}}), rt)
        self.assertEqual(evs, [("text", "直接给答案")])

    def test_no_thinking_merged_with_answ_sentinel(self):
        # 非思考模型且 claude 把 block0(无哨兵)+block1(ANSW) 合并：整段当答案，剥离哨兵
        rt = self._rt()
        merged = "我" + ANSW_SENT + "是一个AI助手。"
        evs = executor._parse_claude_line(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": merged}]}}), rt)
        self.assertEqual(evs, [("text", "我是一个AI助手。")])


class TestBadSessionId(unittest.TestCase):
    def test_already_in_use(self):
        self.assertTrue(executor._is_bad_session_id("Error: Session ID abc is already in use."))

    def test_no_conversation_found(self):
        # claude 2.x 续接一个不存在的 session 时的真实措辞（历史恢复读到坏 id）
        self.assertTrue(executor._is_bad_session_id("No conversation found with session ID: abc-123"))

    def test_session_not_found(self):
        self.assertTrue(executor._is_bad_session_id("session abc-123 not found, cannot resume"))

    def test_unknown_session(self):
        self.assertTrue(executor._is_bad_session_id("unknown session ID abc-123"))

    def test_other_error_not_flagged(self):
        # 普通报错不应误触发自愈重试（否则会掩盖真实错误）
        self.assertFalse(executor._is_bad_session_id("API Error: 400 model not supported"))
        self.assertFalse(executor._is_bad_session_id("Execution error: timeout"))


class TestCliTranscriptParse(unittest.TestCase):
    def _write(self, lines):
        import tempfile, os
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        f.write("\n".join(lines) + "\n")
        f.close()
        return f.name

    def test_parse_claude_transcript_thinking_and_answer(self):
        # 真实 claude 格式：assistant 的 content 为 block 列表，thinking 块 + text 块分离
        p = self._write([
            json.dumps({"type": "user", "message": {"role": "user", "content": "问题"}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant",
                "content": [{"type": "thinking", "thinking": "我先想一下思路"},
                            {"type": "text", "text": "这是最终答案"}]}}),
            json.dumps({"type": "queue-operation", "operation": "flush"}),
        ])
        turns = executor._parse_claude_transcript(p)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["content"], "这是最终答案")
        self.assertEqual(turns[0]["thinking"], "我先想一下思路")

    def test_parse_claude_transcript_sentinel_split(self):
        # 网关 agent（gemma 等）经外壳落盘的 text 块带哨兵：按 _classify_text 切分思考/答案
        raw = THINK_SENT + "推理链内容" + ANSW_SENT + "网关答案"
        p = self._write([
            json.dumps({"type": "assistant", "message": {"role": "assistant",
                "content": [{"type": "text", "text": raw}]}}),
        ])
        turns = executor._parse_claude_transcript(p)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["content"], "网关答案")
        self.assertEqual(turns[0]["thinking"], "推理链内容")

    def test_parse_claude_transcript_thinking_only_kept_separate(self):
        # gemma 把结论也吐在 reasoning 流里、无独立 answer：思考就是思考，不做提升——
        # content 保持为空，thinking 保留原样；前端正文为空时显示占位提示，不重复渲染。
        raw = THINK_SENT + "这是完整推理，包含最终结论：猫和狗各有优势"
        p = self._write([
            json.dumps({"type": "assistant", "message": {"role": "assistant",
                "content": [{"type": "text", "text": raw}]}}),
        ])
        turns = executor._parse_claude_transcript(p)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["content"], "")  # 正文不造假
        self.assertEqual(turns[0]["thinking"], "这是完整推理，包含最终结论：猫和狗各有优势")

    def test_parse_claude_transcript_both_keeps_two_blocks(self):
        # 同时有 thinking + answer：保留双块（思考折叠、答案展开），不做提升
        raw = THINK_SENT + "元推理" + ANSW_SENT + "正式答案"
        p = self._write([
            json.dumps({"type": "assistant", "message": {"role": "assistant",
                "content": [{"type": "text", "text": raw}]}}),
        ])
        turns = executor._parse_claude_transcript(p)
        self.assertEqual(turns[0]["content"], "正式答案")
        self.assertEqual(turns[0]["thinking"], "元推理")

    def test_parse_claude_transcript_strips_dash_noise(self):
        # answer 首尾 --- 噪声应剥离（与 _clean_answer 一致），中间正文 --- 保留
        p = self._write([
            json.dumps({"type": "assistant", "message": {"role": "assistant",
                "content": [{"type": "text", "text": "--- 真正答案 --- 中间分隔 ---"}]}}),
        ])
        turns = executor._parse_claude_transcript(p)
        self.assertEqual(turns[0]["content"], "真正答案 --- 中间分隔")

    def test_parse_claude_transcript_merges_split_turn(self):
        # claude-code 外壳把「一次回复」拆成 thinking-only + text-only 两个独立 assistant 事件
        # （中间无 user 事件，实测 2.1.212 对原生 thinking 块的行为）。
        # 解析器必须以 user 事件为分界合并为一条，否则 reconcile 会把一次回复拆成两条（历史 bug）。
        p = self._write([
            json.dumps({"type": "user", "message": {"role": "user", "content": "问题"}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant",
                "content": [{"type": "thinking", "thinking": "思考过程内容"}]}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant",
                "content": [{"type": "text", "text": "最终答案内容"}]}}),
        ])
        turns = executor._parse_claude_transcript(p)
        self.assertEqual(len(turns), 1, "同一 user 后的 thinking/text 应合并为一条回复")
        self.assertEqual(turns[0]["content"], "最终答案内容")
        self.assertEqual(turns[0]["thinking"], "思考过程内容")

    def test_parse_claude_transcript_user_boundary_keeps_separate(self):
        # 两次真实回复之间必有 user 事件：user 分界处不得合并
        p = self._write([
            json.dumps({"type": "user", "message": {"role": "user", "content": "问题1"}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant",
                "content": [{"type": "thinking", "thinking": "第一次思考"}]}}),
            json.dumps({"type": "user", "message": {"role": "user", "content": "问题2"}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant",
                "content": [{"type": "text", "text": "第二次答案"}]}}),
        ])
        turns = executor._parse_claude_transcript(p)
        self.assertEqual(len(turns), 2, "两次回复中间有 user 事件，应保持两条")
        self.assertEqual(turns[0]["content"], "")
        self.assertEqual(turns[0]["thinking"], "第一次思考")
        self.assertEqual(turns[1]["content"], "第二次答案")
        self.assertEqual(turns[1]["thinking"], "")

    def test_parse_claude_transcript_tool_result_error_attached(self):
        # assistant 调用工具后，user 事件里的 tool_result is_error 应拼入刚结束的回复，
        # 否则 reconcile 打开会话时看不到「No such tool available」等报错。
        p = self._write([
            json.dumps({"type": "user", "message": {"role": "user", "content": "问题"}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant",
                "content": [{"type": "thinking", "thinking": "我调用工具"},
                            {"type": "tool_use", "name": "end_discussion", "input": {"reason": "测试"}}]}}),
            json.dumps({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_x", "is_error": True,
                 "content": "<tool_use_error>Error: No such tool available: end_discussion</tool_use_error>"}]}}),
        ])
        turns = executor._parse_claude_transcript(p)
        self.assertEqual(len(turns), 1)
        self.assertIn("No such tool available", turns[0]["content"])
        self.assertIn("[工具调用失败]", turns[0]["content"])
        self.assertEqual(turns[0]["thinking"], "我调用工具")

    def test_parse_codex_transcript_best_effort(self):
        # codex 格式多变：尽力解析出 assistant 文本，解析不出返回空（对账时跳过）
        p = self._write([
            json.dumps({"type": "event_msg", "message": {"role": "assistant",
                "content": [{"type": "text", "text": "codex 答案"}]}}),
        ])
        turns = executor._parse_codex_transcript(p)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["content"], "codex 答案")
        # 无法识别的结构 → 返回空列表（不报错）
        p2 = self._write([json.dumps({"type": "world_state", "foo": "bar"})])
        self.assertEqual(executor._parse_codex_transcript(p2), [])


if __name__ == "__main__":
    unittest.main()
