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

    def test_env_sets_claude_max_output_tokens(self):
        # 单次响应 output token 上限应从全局配置注入（默认 64000，防 gemma 思考块爆 32k 上限）
        a = models.AgentConfig(name="G", type="claude-code", model="gemma4:26b",
                                base_url="http://localhost:4000")
        rt = executor.AgentRuntime(a)
        env = rt._claude_env()
        self.assertIn("CLAUDE_CODE_MAX_OUTPUT_TOKENS", env, "应注入 CLAUDE_CODE_MAX_OUTPUT_TOKENS")
        self.assertEqual(env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "64000", "默认输出上限应为 64000")
        # 未配网关的 agent 同样注入（该上限对所有 claude 子进程生效）
        b = models.AgentConfig(name="C", type="claude-code", model="claude-opus-4")
        env2 = executor.AgentRuntime(b)._claude_env()
        self.assertIn("CLAUDE_CODE_MAX_OUTPUT_TOKENS", env2)

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
        # 项目级 .mcp.json 的 MCP 自动加载总开关（end_discussion 工具可达；
        # 缺失实测「夏朝存在吗」会话：gemma 看不到工具、只能输出 json 文本兜底）
        self.assertTrue(s.get("enableAllProjectMcpServers"), "隔离 settings 应允许项目级 MCP 自动加载")
        env = rt._claude_env()
        self.assertEqual(env.get("CLAUDE_CONFIG_DIR"), cfg)
        # 隔离目录的 projects/ 必须软链到标准 ~/.claude/projects，使按目录的会话可被裸 claude 恢复、跨模型 resume
        link = os.path.join(cfg, "projects")
        self.assertTrue(os.path.islink(link), "projects 应为指向 ~/.claude/projects 的软链")
        self.assertEqual(os.path.realpath(link), os.path.realpath(os.path.join(os.path.expanduser("~"), ".claude", "projects")))
        # 无 base_url 的 agent 不应进入隔离逻辑
        self.assertIsNone(executor.AgentRuntime(models.AgentConfig(name="C", type="claude-code"))._claude_config_dir())

    def test_ensure_project_mcp_writes_trust_for_config_dir(self):
        # 隔离配置目录（CLAUDE_CONFIG_DIR）下必须有 .claude.json 的项目 trust + MCP 启用，
        # 否则 claude 以隔离目录为准时不信任项目、不加载 .mcp.json（gemma 看不到 end_discussion 工具）
        import tempfile
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as cfg_dir:
            executor._ensure_project_mcp(cwd, cfg_dir)
            # 项目 .mcp.json 生成
            mcp = os.path.join(cwd, ".mcp.json")
            self.assertTrue(os.path.exists(mcp), "应生成项目 .mcp.json")
            with open(mcp, encoding="utf-8") as f:
                self.assertIn("terminate-signal", json.load(f)["mcpServers"])
            # 隔离目录 .claude.json 的 trust + 启用
            cj = os.path.join(cfg_dir, ".claude.json")
            self.assertTrue(os.path.exists(cj), "应生成隔离目录 .claude.json")
            with open(cj, encoding="utf-8") as f:
                d = json.load(f)
            entry = d["projects"][os.path.realpath(cwd)]
            self.assertTrue(entry.get("hasTrustDialogAccepted"))
            self.assertIn("terminate-signal", entry.get("enabledMcpjsonServers", []))
            # 幂等：再次调用不报错、不重复启用
            executor._ensure_project_mcp(cwd, cfg_dir)
            with open(cj, encoding="utf-8") as f:
                d2 = json.load(f)
            self.assertEqual(
                d2["projects"][os.path.realpath(cwd)]["enabledMcpjsonServers"].count("terminate-signal"), 1)

    def test_cmd_includes_model_flag(self):
        a = models.AgentConfig(name="G", type="claude-code", model="ollama/gemma",
                                base_url="http://localhost:4000")
        rt = executor.AgentRuntime(a)
        cmd = rt._claude_cmd()
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "ollama/gemma")
        # 首次启动（cli_session_id 为 None）：用 --session-id 注册新 claude 会话
        self.assertIn("--session-id", cmd)
        # stream-json 模式（2026-08-19）：-p 旧模式 MCP 不通信（2.1.212 bug），
        # 须用 SDK 同款 --output-format/--input-format stream-json + --verbose；
        # prompt 经 stdin 传入，不再作位置参数
        self.assertNotIn("-p", cmd, "不得用 -p 旧模式（MCP 不通信）")
        self.assertIn("--output-format", cmd)
        self.assertIn("--input-format", cmd)
        self.assertIn("--verbose", cmd)
        self.assertIn("stream-json", cmd)
        self.assertNotEqual(cmd[-1], "议题：X", "prompt 不应再作位置参数（走 stdin 帧）")

    def test_cmd_uses_resume_when_continuing(self):
        # 续接（含历史会话恢复 / 同会话后续轮）：必须用 --resume，而非复用 --session-id
        a = models.AgentConfig(name="G", type="claude-code", model="ollama/gemma",
                                base_url="http://localhost:4000")
        rt = executor.AgentRuntime(a, cli_session_id="11111111-2222-3333-4444-555555555555")
        cmd = rt._claude_cmd()
        self.assertIn("--resume", cmd)
        self.assertNotIn("--session-id", cmd)
        self.assertEqual(cmd[cmd.index("--resume") + 1], "11111111-2222-3333-4444-555555555555")
        self.assertNotIn("-p", cmd)

    def test_cmd_without_model_has_no_model_flag(self):
        a = models.AgentConfig(name="C", type="claude-code")
        rt = executor.AgentRuntime(a)
        cmd = rt._claude_cmd()
        self.assertNotIn("--model", cmd)

    def test_user_frame_stream_json(self):
        # stdin 输入帧：SDK 同款 user 消息格式（stream-json input 要求）
        frame = executor._user_frame("议题：X")
        import json as _json
        d = _json.loads(frame.decode())
        self.assertEqual(d["type"], "user")
        self.assertEqual(d["message"]["content"][0]["text"], "议题：X")


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

    def test_user_tool_result_error_yields_tool_result(self):
        # claude-code 把失败工具结果以 user 事件里的 tool_result 块发出，解析为 tool_result 事件
        # （is_error=True），编排层据此把失败结果按时间线插到对应工具调用之后、并标 exec_ok=false
        line = json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_xxx", "is_error": True,
             "content": "<tool_use_error>Error: No such tool available: end_discussion</tool_use_error>"}]}})
        self.assertEqual(executor._parse_claude_line(line),
                         [("tool_result", {"is_error": True,
                                           "content": "Error: No such tool available: end_discussion"})])

    def test_user_tool_result_success_yields_tool_result(self):
        # 成功的 tool_result 也按时间线产出（不再丢弃），编排层插到对应工具调用之后实时可见
        line = json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_xxx", "is_error": False,
             "content": "【重要·讨论尚未结束】..."}]}})
        self.assertEqual(executor._parse_claude_line(line),
                         [("tool_result", {"is_error": False,
                                           "content": "【重要·讨论尚未结束】..."})])

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
        # assistant 调用工具后，user 事件里的 tool_result is_error 应作为结构化 tool_results
        # 随当前气泡落盘（不新开气泡），否则 reconcile 打开会话时看不到「No such tool available」。
        # 同时该 assistant 的 tool_use 块须收集进 tool_calls（之前被忽略，导致调用输出在 UI 消失）。
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
        self.assertEqual(len(turns), 1, "工具回执不新开气泡，合并为同一回合")
        self.assertEqual(turns[0]["thinking"], "我调用工具")
        # tool_use 收集进 tool_calls
        self.assertEqual(len(turns[0]["tool_calls"]), 1)
        self.assertEqual(turns[0]["tool_calls"][0]["name"], "end_discussion")
        self.assertEqual(turns[0]["tool_calls"][0]["input"], {"reason": "测试"})
        # 工具失败进 tool_results（结构化，不再拼进正文）
        self.assertEqual(len(turns[0]["tool_results"]), 1)
        self.assertTrue(turns[0]["tool_results"][0]["is_error"])
        self.assertIn("No such tool available", turns[0]["tool_results"][0]["content"])
        self.assertNotIn("No such tool available", turns[0]["content"], "报错不应进正文")

    def test_parse_claude_transcript_tool_result_list_content_flattened(self):
        # claude 把成功 tool_result 的 content 包成 [{type:text, text}] 列表，
        # 必须展平为字符串，否则原样落盘后前端 textContent 渲染成 [object Object]。
        p = self._write([
            json.dumps({"type": "user", "message": {"role": "user", "content": "问题"}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant",
                "content": [{"type": "tool_use", "name": "end_discussion", "input": {"reason": "测试"}}]}}),
            json.dumps({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_x", "is_error": False,
                 "content": [{"type": "text", "text": "已收到你的中止提议（理由：测试）。终止是否生效由编排层判定；请继续正常参与讨论。"}]}]}}),
        ])
        turns = executor._parse_claude_transcript(p)
        self.assertEqual(len(turns), 1)
        self.assertEqual(len(turns[0]["tool_results"]), 1)
        tr = turns[0]["tool_results"][0]
        self.assertFalse(tr["is_error"])
        self.assertIsInstance(tr["content"], str, "列表 content 必须展平为字符串，否则前端显示 [object Object]")
        self.assertIn("已收到你的中止提议", tr["content"])

    def test_parse_claude_transcript_merges_tool_loop_one_bubble(self):
        # Option A：一个 agent 回合内「思考 → 工具调用 → 工具失败 → 续写结论」合并为一个气泡，
        # tool_result(user) 之后 assistant 的续写仍属同一回合，不新开气泡。
        p = self._write([
            json.dumps({"type": "user", "message": {"role": "user", "content": "问题"}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant",
                "content": [{"type": "thinking", "thinking": "先调用 end_discussion 收尾"},
                            {"type": "tool_use", "name": "end_discussion", "input": {"reason": "R"}}]}}),
            json.dumps({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_x", "is_error": True,
                 "content": "<tool_use_error>Error: No such tool available: end_discussion</tool_use_error>"}]}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant",
                "content": [{"type": "text", "text": "工具不可用，我直接给出结论：讨论达成收敛。"}]}}),
        ])
        turns = executor._parse_claude_transcript(p)
        self.assertEqual(len(turns), 1, "工具循环合并为一个气泡")
        self.assertIn("讨论达成收敛", turns[0]["content"], "工具失败后的续写应并入同一气泡正文")
        self.assertEqual(len(turns[0]["tool_calls"]), 1)
        self.assertEqual(len(turns[0]["tool_results"]), 1)
        self.assertTrue(turns[0]["tool_results"][0]["is_error"])

    def test_parse_claude_transcript_next_round_prompt_splits_bubble(self):
        # 下一轮真实 prompt（user 文本）才是新气泡边界：工具循环后的新回合应隔开
        p = self._write([
            json.dumps({"type": "user", "message": {"role": "user", "content": "第一轮"}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant",
                "content": [{"type": "text", "text": "第一轮结论"}]}}),
            json.dumps({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "c", "is_error": False, "content": "ok"}]}}),
            json.dumps({"type": "user", "message": {"role": "user", "content": "第二轮"}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant",
                "content": [{"type": "text", "text": "第二轮结论"}]}}),
        ])
        turns = executor._parse_claude_transcript(p)
        self.assertEqual(len(turns), 2, "跨轮 prompt 分隔为两个气泡")
        self.assertIn("第一轮结论", turns[0]["content"])
        self.assertIn("第二轮结论", turns[1]["content"])
        # 第一轮的成功 tool_result 也应随之带入（同气泡）
        self.assertEqual(len(turns[0]["tool_results"]), 1)
        self.assertFalse(turns[0]["tool_results"][0]["is_error"])

    def test_parse_claude_transcript_blocks_preserve_native_order(self):
        # 历史 reconcile 直接用 claude 原生 content 顺序产 blocks（type 对齐原生），
        # 不得把思考/正文/工具拆成四类再重排。验证「思考→正文→工具调用→工具结果→续写正文」交错保留。
        p = self._write([
            json.dumps({"type": "user", "message": {"role": "user", "content": "问题"}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant",
                "content": [{"type": "thinking", "thinking": "先想一下"},
                            {"type": "text", "text": "调用前正文"},
                            {"type": "tool_use", "name": "end_discussion", "input": {"reason": "R"}}]}}),
            json.dumps({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_x", "is_error": False,
                 "content": [{"type": "text", "text": "已收到中止提议"}]}]}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant",
                "content": [{"type": "thinking", "thinking": "调用后继续想"},
                            {"type": "text", "text": "调用后正文"}]}}),
        ])
        turns = executor._parse_claude_transcript(p)
        self.assertEqual(len(turns), 1, "工具循环合并为一个气泡")
        blocks = turns[0]["blocks"]
        self.assertTrue(blocks, "历史消息必须产出 blocks（对齐原生 content 顺序）")
        seq = [(b["type"], b.get("thinking") or b.get("text") or b.get("name") or b.get("content"))
               for b in blocks]
        self.assertEqual(seq, [
            ("thinking", "先想一下"),
            ("text", "调用前正文"),
            ("tool_use", "end_discussion"),
            ("tool_result", "已收到中止提议"),
            ("thinking", "调用后继续想"),
            ("text", "调用后正文"),
        ], "blocks 必须按 claude 原生 content 时间线顺序，不重排")

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
        p2 = self._write(json.dumps({"type": "world_state", "foo": "bar"}))
        self.assertEqual(executor._parse_codex_transcript(p2), [])


class FakeStdin:
    def __init__(self):
        self.frames = []

    def write(self, b):
        self.frames.append(b.decode("utf-8"))

    async def drain(self):
        return None

    def close(self):
        return None


class FakeProc:
    def __init__(self, returncode=None):
        self.stdin = FakeStdin()
        self.returncode = returncode


class TestInterrupt(unittest.TestCase):
    """turn 级中断（「中止」功能）：codex 走 turn/interrupt、claude 走 control_request、兜底整组硬停。"""

    def _rt(self, atype):
        return executor.AgentRuntime(models.AgentConfig(name="A", type=atype))

    def test_user_frame_ends_with_newline(self):
        # stream-json 输入是按行解析的 NDJSON：stdin 保持打开后缺换行会让 claude 卡在等这一行
        frame = executor._user_frame("议题")
        self.assertTrue(frame.endswith(b"\n"), "输入帧必须以换行结尾")

    def test_is_claude_turn_end(self):
        self.assertTrue(executor._is_claude_turn_end(
            json.dumps({"type": "result", "subtype": "success"})))
        self.assertTrue(executor._is_claude_turn_end(
            json.dumps({"type": "result", "subtype": "error_during_execution"})))
        self.assertFalse(executor._is_claude_turn_end(
            json.dumps({"type": "assistant", "message": {"content": []}})))
        self.assertFalse(executor._is_claude_turn_end("不是 json"))

    def test_codex_interrupt_sends_turn_interrupt(self):
        rt = self._rt("codex")
        rt._appserver_proc = FakeProc()
        rt._appserver_thread_id = "th_1"
        rt._as_turn_id = "turn_1"
        ch = asyncio.run(rt.interrupt())
        self.assertEqual(ch, "turn_interrupt")
        frames = [json.loads(f) for f in rt._appserver_proc.stdin.frames]
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["method"], "turn/interrupt")
        self.assertEqual(frames[0]["params"], {"threadId": "th_1", "turnId": "turn_1"})

    def test_codex_interrupt_without_turn_id_is_noop(self):
        # 尚未拿到 turn id（turn/start 应答未回）→ 不发帧，交给编排层看门狗硬停兜底
        rt = self._rt("codex")
        rt._appserver_proc = FakeProc()
        rt._appserver_thread_id = "th_1"
        self.assertEqual(asyncio.run(rt.interrupt()), "none")
        self.assertEqual(rt._appserver_proc.stdin.frames, [])

    def test_claude_interrupt_writes_control_request(self):
        rt = self._rt("claude-code")
        rt._live_proc = FakeProc()
        ch = asyncio.run(rt.interrupt())
        self.assertEqual(ch, "control_request")
        frame = json.loads(rt._live_proc.stdin.frames[0])
        self.assertEqual(frame["type"], "control_request")
        self.assertEqual(frame["request"]["subtype"], "interrupt")
        self.assertTrue(frame["request"]["cancel_queued"])
        self.assertTrue(frame["request_id"])

    def test_interrupt_none_when_nothing_in_flight(self):
        rt = self._rt("claude-code")
        self.assertEqual(asyncio.run(rt.interrupt()), "none")
        rt._live_proc = FakeProc(returncode=0)   # 进程已退出
        self.assertEqual(asyncio.run(rt.interrupt()), "none")

    def test_codex_exec_fallback_falls_back_to_killpg(self):
        # codex exec（无 app-server）没有协议中断通道 → 整组硬停
        rt = self._rt("codex")
        killed = []
        orig = executor._kill_proc_group
        executor._kill_proc_group = lambda p, grace=0.6: killed.append(p)
        try:
            rt._live_proc = FakeProc()
            self.assertEqual(asyncio.run(rt.interrupt()), "killpg")
        finally:
            executor._kill_proc_group = orig
        self.assertEqual(len(killed), 1)


if __name__ == "__main__":
    unittest.main()
