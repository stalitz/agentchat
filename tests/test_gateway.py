import asyncio
import json
import unittest

import backend.gateway as gateway


class TestAnthropicToOpenAI(unittest.TestCase):
    def test_system_string(self):
        oa = gateway.anthropic_to_openai({
            "model": "gemma", "max_tokens": 100,
            "system": "你是架构师", "messages": [],
        })
        # 网关在 system 层注入简体中文强制指令（压住 gemma 英文待命反射），原 system 保留
        self.assertEqual(oa["messages"][0]["role"], "system")
        self.assertIn("你是架构师", oa["messages"][0]["content"])
        self.assertIn("【语言强制指令】", oa["messages"][0]["content"])

    def test_system_array_blocks(self):
        oa = gateway.anthropic_to_openai({
            "model": "gemma", "system": [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}],
            "messages": [],
        })
        self.assertEqual(oa["messages"][0]["role"], "system")
        self.assertIn("AB", oa["messages"][0]["content"])
        self.assertIn("【语言强制指令】", oa["messages"][0]["content"])

    def test_user_text_message(self):
        oa = gateway.anthropic_to_openai({
            "model": "gemma", "messages": [{"role": "user", "content": "hi"}],
        })
        # 无 system 时网关注入中文强制指令作为首条 system 消息，原 user 后移
        self.assertEqual(oa["messages"][0]["role"], "system")
        self.assertIn("【语言强制指令】", oa["messages"][0]["content"])
        self.assertEqual(oa["messages"][1], {"role": "user", "content": "hi"})

    def test_tool_use_and_tool_result(self):
        oa = gateway.anthropic_to_openai({
            "model": "gemma",
            "messages": [
                {"role": "user", "content": "查一下"},
                {"role": "assistant", "content": [
                    {"type": "text", "text": "好的"},
                    {"type": "tool_use", "id": "t1", "name": "read", "input": {"path": "/x"}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "file content"},
                ]},
            ],
        })
        # 第 0 条是注入的中文强制 system；用户/assistant/tool 整体后移一位
        self.assertEqual(oa["messages"][0]["role"], "system")
        self.assertIn("【语言强制指令】", oa["messages"][0]["content"])
        # 文本消息
        self.assertEqual(oa["messages"][1], {"role": "user", "content": "查一下"})
        # assistant：文本 + tool_calls
        asst = oa["messages"][2]
        self.assertEqual(asst["content"], "好的")
        self.assertEqual(asst["tool_calls"][0]["function"]["name"], "read")
        self.assertEqual(json.loads(asst["tool_calls"][0]["function"]["arguments"]), {"path": "/x"})
        # tool_result -> OpenAI tool role 消息
        self.assertEqual(oa["messages"][3], {"role": "tool", "tool_call_id": "t1", "content": "file content"})

    def test_tools_not_translated(self):
        # 2026-09-18 工具政策：受控工具 end_discussion 始终转发（去前缀）、claude 内置工具
        # （无 mcp__ 前缀）原样转发让其能调用；第三方 MCP（mcp__ 前缀、非受控）与交互型工具
        # （无 TTY 通道）仍剥。转发时去 mcp__ 前缀（模型见干净名 end_discussion）。
        oa = gateway.anthropic_to_openai({
            "model": "gemma", "messages": [],
            "tools": [
                {"name": "Bash", "description": "d", "input_schema": {"type": "object"}},
                {"name": "mcp__terminate-signal__end_discussion", "description": "d",
                 "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
                {"name": "mcp__some-server__secret", "description": "z", "input_schema": {}},
                {"name": "AskUserQuestion", "description": "w", "input_schema": {}},
            ],
        })
        tools = oa.get("tools") or []
        names = [t["function"]["name"] for t in tools]
        self.assertIn("end_discussion", names, "受控工具应转发且去前缀")
        self.assertIn("Bash", names, "claude 内置工具应转发（让其能调用）")
        self.assertNotIn("secret", names, "第三方 MCP 非受控工具不得泄漏")
        self.assertNotIn("AskUserQuestion", names, "交互型工具无 TTY 通道，必须剥")
        self.assertNotIn("mcp__", names[0], "模型侧应为干净名")

    def test_mcp_name_mapping(self):
        # 双向映射：转发去前缀（模型侧干净名）、翻译还原前缀（claude 侧原名）
        self.assertEqual(gateway._strip_mcp_prefix("mcp__terminate-signal__end_discussion"), "end_discussion")
        self.assertEqual(gateway._strip_mcp_prefix("end_discussion"), "end_discussion")
        self.assertEqual(gateway._strip_mcp_prefix("Bash"), "Bash")
        self.assertEqual(gateway._restore_mcp_name("end_discussion"), "mcp__terminate-signal__end_discussion")
        self.assertEqual(gateway._restore_mcp_name("anthropic:end_discussion"), "mcp__terminate-signal__end_discussion")
        self.assertEqual(gateway._restore_mcp_name("mcp__terminate-signal__end_discussion"), "mcp__terminate-signal__end_discussion")
        self.assertEqual(gateway._restore_mcp_name("Bash"), "Bash")

    def test_unknown_fields_dropped(self):
        oa = gateway.anthropic_to_openai({
            "model": "gemma", "messages": [],
            "thinking": {"type": "enabled"}, "metadata": {"x": 1}, "temperature": 0.7,
        })
        # Anthropic 专有字段不得透传（否则 Ollama 可能 400）
        self.assertNotIn("thinking", oa)
        self.assertNotIn("metadata", oa)
        # 受支持的通用字段应保留
        self.assertEqual(oa["temperature"], 0.7)

    def test_internal_tools_forwarded(self):
        # claude 内置工具（Bash 等）原样转发给模型（让其能调用），第三方 MCP 非受控工具不得泄漏；
        # 无 tools 的请求不带 tools 字段。
        oa = gateway.anthropic_to_openai({
            "model": "gemma", "messages": [],
            "tools": [
                {"name": "Bash", "description": "d", "input_schema": {"type": "object"}},
                {"name": "mcp__some-server__secret", "description": "z", "input_schema": {}},
            ],
        })
        names = [t["function"]["name"] for t in oa.get("tools") or []]
        self.assertIn("Bash", names, "claude 内置工具应转发")
        self.assertNotIn("secret", names, "第三方 MCP 非受控工具不得泄漏")
        # 无 tools 请求同样不带 tools 字段
        oa3 = gateway.anthropic_to_openai({"model": "gemma", "messages": []})
        self.assertNotIn("tools", oa3)


class TestStreamTranslator(unittest.TestCase):
    def test_text_only(self):
        t = gateway._AnthropicTranslator(model="gemma")
        evs = t.feed({"content": "你好"})
        kinds = [e["type"] for e in evs]
        self.assertIn("message_start", kinds)
        self.assertIn("content_block_start", kinds)
        self.assertIn("content_block_delta", kinds)
        self.assertEqual(evs[-1]["delta"]["type"], "text_delta")
        self.assertEqual(evs[-1]["delta"]["text"], "你好")
        fin = t.finish()
        # text block 关闭 + message_delta(end_turn) + message_stop
        self.assertIn("content_block_stop", [e["type"] for e in fin])
        self.assertEqual(fin[-2]["delta"]["stop_reason"], "end_turn")
        self.assertEqual(fin[-1]["type"], "message_stop")

    def test_tool_use_streaming(self):
        t = gateway._AnthropicTranslator(model="gemma")
        evs = []
        evs += t.feed({"content": "让我调用工具"})
        evs += t.feed({"tool_calls": [{"index": 0, "id": "t1", "function": {"name": "read", "arguments": "{\"path\":"}}]})
        evs += t.feed({"tool_calls": [{"index": 0, "function": {"arguments": "\"/x\"}"}}]})
        t.stop_reason = "tool_use"
        evs += t.finish()
        types = [e["type"] for e in evs]
        self.assertIn("content_block_stop", types)
        # 应出现 tool_use 的 content_block_start（在 feed 阶段产出）
        starts = [e for e in evs if e["type"] == "content_block_start" and e["content_block"]["type"] == "tool_use"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["content_block"]["name"], "read")
        # 最后 message_delta 的 stop_reason 应为 tool_use
        md = [e for e in evs if e["type"] == "message_delta"][-1]
        self.assertEqual(md["delta"]["stop_reason"], "tool_use")

    def test_plain_text_without_thinking_keeps_block0(self):
        # 纯 text（无 reasoning，如 gemma 对工具结果的短回复）：text 块全程 index 0，
        # 不得误发 signature_delta / 切到 index 1——否则块序错乱 claude 判 malformed
        t = gateway._AnthropicTranslator(model="gemma")
        evs = []
        evs += t.feed({"content": "No"})
        evs += t.feed({"content": " input provided"})
        evs += t.feed({"content": "。"})
        evs += t.finish()
        seq = []
        for e in evs:
            t2 = e["type"]
            if t2 == "content_block_start":
                seq.append(("start", e.get("index"), e.get("content_block", {}).get("type")))
            elif t2 == "content_block_delta":
                d = e.get("delta", {})
                seq.append(("delta", e.get("index"), d.get("type")))
            elif t2 == "content_block_stop":
                seq.append(("stop", e.get("index")))
        # 全程只有 index 0 的 text 块，无 signature_delta，无 index 1
        self.assertEqual(seq, [
            ("start", 0, "text"),
            ("delta", 0, "text_delta"),
            ("delta", 0, "text_delta"),
            ("delta", 0, "text_delta"),
            ("stop", 0),
        ], "纯 text 不得误触发 thinking 收口或切块")
        self.assertNotIn(1, [x[1] for x in seq], "不应出现 index 1 块")
        self.assertNotIn("signature_delta", [x[2] for x in seq if len(x) > 2], "text 块不应有 signature_delta")

    def test_tool_use_after_thinking_closes_thinking_first(self):
        # gemma 先 reasoning 后直接 tool_calls（无 text 答案）时：thinking 块必须在
        # tool_use 块 start 之前收口（signature_delta + content_block_stop），否则违反
        # Anthropic 流式块序、claude 判「empty or malformed (HTTP 200)」（实测复现）
        t = gateway._AnthropicTranslator(model="gemma")
        evs = []
        evs += t.feed({"reasoning": "我需要调用工具收尾"})
        evs += t.feed({"tool_calls": [{"index": 0, "id": "t1",
                                       "function": {"name": "end_discussion", "arguments": "{\"reason\":\"x\"}"}}]})
        t.stop_reason = "tool_use"
        evs += t.finish()
        # 找到 thinking stop 与 tool_use start 的位置，断言顺序
        seq = []
        for e in evs:
            if e["type"] == "content_block_delta" and e.get("delta", {}).get("type") == "signature_delta":
                seq.append("thinking_sig")
            elif e["type"] == "content_block_stop" and e.get("index") == 0:
                seq.append("thinking_stop")
            elif e["type"] == "content_block_start" and e.get("content_block", {}).get("type") == "tool_use":
                seq.append("tool_start")
        self.assertEqual(seq, ["thinking_sig", "thinking_stop", "tool_start"],
                         "thinking 块须先收口（signature+stop）再开 tool_use 块")

    def test_tool_use_after_text_closes_text_first(self):
        # 「先总结发言再调用工具」两步表达（2026-08-20 新 prompt 引导）：纯 text 总结
        # （无 reasoning，text 块在 index 0）后接 tool_calls——text 块必须在 tool_use
        # start 之前收口（stop），否则块未 stop 就 start 违反块序，claude 判
        # 「empty or malformed (HTTP 200)」（实测 s_8dac9d7c 光速会话 gemma 复现）
        t = gateway._AnthropicTranslator(model="gemma")
        evs = []
        evs += t.feed({"content": "我们已达成一致，光速是局部时空约束。"})
        evs += t.feed({"tool_calls": [{"index": 0, "id": "t1",
                                       "function": {"name": "end_discussion", "arguments": "{\"reason\":\"x\"}"}}]})
        t.stop_reason = "tool_use"
        evs += t.finish()
        seq = []
        for e in evs:
            t2 = e["type"]
            if t2 == "content_block_start":
                seq.append(("start", e.get("index"), e.get("content_block", {}).get("type")))
            elif t2 == "content_block_delta":
                seq.append(("delta", e.get("index"), e.get("delta", {}).get("type")))
            elif t2 == "content_block_stop":
                seq.append(("stop", e.get("index")))
        # text(0) start → delta → stop(0) → tool_use(2) start → input_json_delta → stop
        self.assertEqual(seq[0], ("start", 0, "text"), "纯 text 先开 index 0 的 text 块")
        self.assertIn(("stop", 0), seq, "text 块必须在 tool_use 前收口")
        self.assertLess(seq.index(("stop", 0)), next(i for i, x in enumerate(seq) if x[0] == "start" and x[2] == "tool_use"),
                        "text 收口必须早于 tool_use start")
        tool_start = next(x for x in seq if x[0] == "start" and x[2] == "tool_use")
        self.assertEqual(seq[-1], ("stop", tool_start[1]), "tool_use 块正常收口（index 动态分配）")

    def _replay(self, evs):
        """回放事件序列并校验 Anthropic 流式块序合法性（单一开块 / delta 落在开块上 / 配对）。"""
        seq = []
        open_idx = None
        for e in evs:
            t = e["type"]
            if t == "content_block_start":
                self.assertIsNone(open_idx, "同一时刻只能有一个开块（不得 start 未 stop 的块）")
                open_idx = e["index"]
                seq.append(("start", e["index"], e["content_block"]["type"]))
            elif t == "content_block_delta":
                self.assertEqual(e["index"], open_idx, "delta 必须落在当前开块上（不得跨块混排）")
                seq.append(("delta", e["index"], e["delta"]["type"]))
            elif t == "content_block_stop":
                self.assertEqual(e["index"], open_idx, "stop 必须收口当前开块")
                seq.append(("stop", e["index"]))
                open_idx = None
        self.assertIsNone(open_idx, "结尾不得残留未收口的块")
        return seq

    def test_late_thinking_closes_text_block_first(self):
        # gemma4:26b 实测真实顺序：正文片段("---") → 思考 → 正文(答案)。旧实现把迟到的思考塞进
        # 已打开的 text 块（同一 index 先 text_delta 再 thinking_delta）→ claude SDK 判流非法、
        # 掐断流并触发非流式回退 → 报「API Error: empty or malformed response (HTTP 200)」
        # （2026-09-18 抓包 probe/app-repro/raw/tap.jsonl）。必须切块：text(0) → thinking(1) → text(2)
        t = gateway._AnthropicTranslator(model="gemma")
        evs = []
        evs += t.feed({"content": "---"})
        evs += t.feed({"reasoning": "先想一下。"})
        evs += t.feed({"content": "1"})
        evs += t.finish()
        seq = self._replay(evs)
        self.assertEqual(seq, [
            ("start", 0, "text"), ("delta", 0, "text_delta"), ("stop", 0),
            ("start", 1, "thinking"), ("delta", 1, "thinking_delta"),
            ("delta", 1, "signature_delta"), ("stop", 1),
            ("start", 2, "text"), ("delta", 2, "text_delta"), ("stop", 2),
        ], "正文→思考→正文 必须各自成块，不得同 index 混排")

    def test_thinking_then_text_then_tool_all_get_distinct_blocks(self):
        # 思考 → 正文 → 工具三连（辩论收尾常见）：三个块 index 连续、各自配对收口
        t = gateway._AnthropicTranslator(model="gemma")
        evs = []
        evs += t.feed({"reasoning": "该收尾了"})
        evs += t.feed({"content": "我们已达成一致。"})
        evs += t.feed({"tool_calls": [{"index": 0, "id": "t1",
                                       "function": {"name": "end_discussion", "arguments": "{\"reason\":\"x\"}"}}]})
        evs += t.finish()
        seq = self._replay(evs)
        self.assertEqual([(x[0], x[1]) for x in seq if x[0] == "start"],
                         [("start", 0), ("start", 1), ("start", 2)], "块 index 应连续分配")
        self.assertEqual(seq[-1], ("stop", 2))
        starts = [x[2] for x in seq if x[0] == "start"]
        self.assertEqual(starts, ["thinking", "text", "tool_use"])

    def test_message_start_uses_estimated_input_tokens(self):
        # message_start 的 input_tokens 用请求估算值（Ollama 真实值流末才返回），避免恒为 0
        t = gateway._AnthropicTranslator(model="gemma", est_input_tokens=88)
        evs = t.feed({"content": "hi"})
        ms = [e for e in evs if e["type"] == "message_start"][0]
        self.assertEqual(ms["message"]["usage"]["input_tokens"], 88)

    def test_finish_usage_from_ollama(self):
        # Ollama 流末 chunk 返回 usage 后，message_delta 应回填真实 tokens（UI 工具不再显示 0）
        t = gateway._AnthropicTranslator(model="gemma", est_input_tokens=42)
        t.feed({"content": "你好"})
        t.usage = {"prompt_tokens": 100, "completion_tokens": 50}
        fin = t.finish()
        md = [e for e in fin if e["type"] == "message_delta"][-1]
        self.assertEqual(md["usage"]["input_tokens"], 100)
        self.assertEqual(md["usage"]["output_tokens"], 50)

    def test_estimate_input_tokens(self):
        # 估算：文本长度/4 粗估，至少 1
        body = {"system": "你是助手", "messages": [{"role": "user", "content": "你好世界"}]}
        self.assertGreater(gateway._estimate_input_tokens(body), 0)
        self.assertEqual(gateway._estimate_input_tokens({"messages": []}), 1)


class _FakeOllamaCtx:
    def __init__(self, lines, status_code=200, err=b""):
        self.lines = lines
        self.status_code = status_code
        self._err = err
    async def aread(self):
        return self._err
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def aiter_lines(self):
        for l in self.lines:
            yield l


class _FakeOllamaClient:
    """模拟 Ollama /v1/responses 流式响应（text 在 response.output_text.delta）。"""
    def __init__(self, *a, **k):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    def stream(self, method, url, json=None, headers=None):
        return _FakeOllamaCtx([
            'data: {"type":"response.output_text.delta","delta":"你好"}',
            'data: {"type":"response.completed","response":{"output":[],"usage":{"input_tokens":5,"output_tokens":2}}}',
        ])


class _FakeOllamaClientThinkingTool:
    """模拟 Ollama /v1/responses：reasoning_summary_text.done → output_text.delta →
    function_call_arguments.delta/done → completed(tool_use)。"""
    def __init__(self, *a, **k):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    def stream(self, method, url, json=None, headers=None):
        return _FakeOllamaCtx([
            'data: {"type":"response.reasoning_summary_text.done","item_id":"rs1","text":"让我想想"}',
            'data: {"type":"response.output_text.delta","delta":"结论"}',
            'data: {"type":"response.output_item.added","item":{"type":"function_call","id":"fc1","call_id":"call_1","name":"end_discussion"}}',
            'data: {"type":"response.function_call_arguments.delta","item_id":"fc1","output_index":0,"delta":"{\\"reason\\":\\"x\\"}"}',
            'data: {"type":"response.function_call_arguments.done","item_id":"fc1","output_index":0,"arguments":"{\\"reason\\":\\"x\\"}"}',
            'data: {"type":"response.completed","response":{"output":[],"usage":{"input_tokens":10,"output_tokens":3}}}',
        ])


class _FakeOllamaClientNativeNoPrefix:
    """模拟 Ollama /v1/responses 真实流式格式：不带 data: 前缀的纯 JSON 行
    （8e0eef0 的假数据带 data: 前缀，掩盖了网关要求 data: 前缀导致空流的 bug）。"""
    def __init__(self, *a, **k):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    def stream(self, method, url, json=None, headers=None):
        return _FakeOllamaCtx([
            '{"type":"response.reasoning_summary_text.done","item_id":"rs1","text":"让我想想"}',
            '{"type":"response.output_text.delta","delta":"结论"}',
            '{"type":"response.output_item.added","item":{"type":"function_call","id":"fc1","call_id":"call_1","name":"end_discussion"}}',
            '{"type":"response.function_call_arguments.delta","item_id":"fc1","output_index":0,"delta":"{\\"reason\\":\\"x\\"}"}',
            '{"type":"response.function_call_arguments.done","item_id":"fc1","output_index":0,"arguments":"{\\"reason\\":\\"x\\"}"}',
            '{"type":"response.completed","response":{"output":[],"usage":{"input_tokens":10,"output_tokens":3}}}',
        ])


class _FakeCaptureBody:
    """捕获发给 Ollama 的 /v1/responses 请求体，验证 options.num_ctx / input 数组 / tools。"""
    captured: dict = {}
    def __init__(self, *a, **k):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    def stream(self, method, url, json=None, headers=None):
        _FakeCaptureBody.captured = json or {}
        return _FakeOllamaCtx([
            'data: {"type":"response.output_text.delta","delta":"ok"}',
            'data: {"type":"response.completed","response":{"output":[],"usage":{"input_tokens":1,"output_tokens":1}}}',
        ])


class _FakeCaptureShowBody:
    """捕获 /v1/responses 请求体，并支持 /api/show 能力查询（思考开关显式透传需查能力）。"""
    caps: list = ["completion", "tools", "thinking"]
    captured: dict = {}
    show_calls: int = 0
    def __init__(self, *a, **k):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def post(self, url, json=None, **kw):
        cls = _FakeCaptureShowBody
        cls.show_calls += 1
        class _R:
            status_code = 200
            def json(self):
                return {"capabilities": list(cls.caps)}
        return _R()
    def stream(self, method, url, json=None, headers=None):
        cls = _FakeCaptureShowBody
        cls.captured = json or {}
        return _FakeOllamaCtx([
            'data: {"type":"response.output_text.delta","delta":"ok"}',
            'data: {"type":"response.completed","response":{"output":[],"usage":{"input_tokens":1,"output_tokens":1}}}',
        ])


class _FakeErrOllamaClient:
    def __init__(self, *a, **k):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    def stream(self, method, url, json=None, headers=None):
        return _FakeOllamaCtx([], status_code=400, err=b'{"error":{"message":"model \'gemma\' not found"}}')


class _FakeEmptyOllamaClient:
    """HTTP 200 但流完全为空（Ollama 空/畸形响应，claude-code 会渲染成 API Error 文本）。"""
    def __init__(self, *a, **k):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    def stream(self, method, url, json=None, headers=None):
        return _FakeOllamaCtx([])


class _FakeRequest:
    def __init__(self, body):
        self._body = body
        self.headers = {}
    async def json(self):
        return self._body


class _FakeNonStreamClient:
    """模拟 Ollama /v1/responses **非流式** JSON 响应（claude SDK 的非流式回退走这条）。"""
    captured: dict = {}
    def __init__(self, *a, **k):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def post(self, url, json=None, **kw):
        cls = _FakeNonStreamClient
        if "/api/show" in str(url):
            class _Show:
                status_code = 200
                def json(self):
                    return {"capabilities": ["completion", "tools", "thinking"]}
            return _Show()
        cls.captured = json or {}
        class _R:
            status_code = 200
            def json(self):
                return {"output": [{"type": "message", "content": [
                            {"type": "output_text", "text": "你好"}]}],
                        "usage": {"input_tokens": 4, "output_tokens": 2}}
        return _R()


class TestMessagesRoute(unittest.IsolatedAsyncioTestCase):
    def test_stream_route_wires_translation(self):
        orig = gateway.httpx.AsyncClient
        gateway.httpx.AsyncClient = _FakeOllamaClient
        try:
            async def run():
                resp = await gateway.handle_messages(
                    _FakeRequest({"model": "gemma", "messages": [
                        {"role": "user", "content": "hi"},
                        {"role": "assistant", "content": [
                            {"type": "tool_use", "id": "t1", "name": "end_discussion", "input": {"reason": "x"}}]},
                    ], "stream": True}),
                    {"ollama_base_url": "http://x"},
                )
                self.assertEqual(resp.media_type, "text/event-stream")
                out = "".join([c async for c in resp.body_iterator])
                return out
            out = asyncio.run(run())
        finally:
            gateway.httpx.AsyncClient = orig
        self.assertIn("message_start", out)
        self.assertIn("text_delta", out)
        self.assertIn("你好", out)
        self.assertIn("message_stop", out)
        # message_start 必须先行于任何内容事件（网关进流即发，不等 Ollama 首 chunk）：
        # 否则 claude 进程 SDK 对「200 已建立但长时间无首事件」有内部超时（约 60s），
        # gemma 等本地模型慢 prefill 时会被误判「API Error: empty or malformed」退出。
        self.assertLess(out.index("message_start"), out.index("text_delta"),
                        "message_start 应先于内容事件发出")

    def test_non_stream_request_returns_json_not_sse(self):
        # 不带 stream 字段的请求（claude SDK 的非流式回退）必须回 application/json。
        # 旧实现 want_stream 缺省 True → 回 138KB SSE，claude 拿 SSE 当 JSON 解析失败，
        # 报「API returned an empty or malformed response (HTTP 200)」（抓包实证）。
        orig = gateway.httpx.AsyncClient
        gateway.httpx.AsyncClient = _FakeNonStreamClient
        try:
            async def run():
                return await gateway.handle_messages(
                    _FakeRequest({"model": "gemma4:26b",
                                  "messages": [{"role": "user", "content": "hi"}]}),
                    {"ollama_base_url": "http://x", "think": False},
                )
            resp = asyncio.run(run())
        finally:
            gateway.httpx.AsyncClient = orig
        self.assertNotIn("text/event-stream", str(resp.media_type),
                         "缺 stream 字段时不得按流式回 SSE")
        body = json.loads(resp.body.decode())
        self.assertEqual(body["content"][0]["text"], "你好")
        # 转给 Ollama 的 body 也必须是非流式，否则 Ollama 回 SSE、r.json() 解析失败
        self.assertFalse(_FakeNonStreamClient.captured.get("stream"),
                         "非流式请求不得给 Ollama 带 stream=true")

    def test_stream_route_propagates_ollama_error(self):
        # Ollama 返回非 200 时，网关必须把错误透传为 SSE error 事件，
        # 而不是静默返回空流（否则表现为「gemma 不通但无报错」）。
        orig = gateway.httpx.AsyncClient
        gateway.httpx.AsyncClient = _FakeErrOllamaClient
        try:
            async def run():
                resp = await gateway.handle_messages(
                    _FakeRequest({"model": "gemma", "messages": [
                        {"role": "user", "content": "hi"},
                        {"role": "assistant", "content": [
                            {"type": "tool_use", "id": "t1", "name": "end_discussion", "input": {"reason": "x"}}]},
                    ], "stream": True}),
                    {"ollama_base_url": "http://x"},
                )
                out = "".join([c async for c in resp.body_iterator])
                return out
            out = asyncio.run(run())
        finally:
            gateway.httpx.AsyncClient = orig
        self.assertIn("event: error", out)
        self.assertIn("model 'gemma' not found", out)

    def test_stream_route_empty_stream_yields_error(self):
        # HTTP 200 但空流（Ollama 空/畸形响应）：网关必须产出 SSE error 事件，
        # 否则 claude-code 会把「API Error: empty or malformed response」渲染成普通文本
        # 混进正文、被编排器当正常发言喂给下一个 agent（历史 bug）。
        orig = gateway.httpx.AsyncClient
        gateway.httpx.AsyncClient = _FakeEmptyOllamaClient
        try:
            async def run():
                resp = await gateway.handle_messages(
                    _FakeRequest({"model": "gemma", "messages": [
                        {"role": "user", "content": "hi"},
                        {"role": "assistant", "content": [
                            {"type": "tool_use", "id": "t1", "name": "end_discussion", "input": {"reason": "x"}}]},
                    ], "stream": True}),
                    {"ollama_base_url": "http://x"},
                )
                out = "".join([c async for c in resp.body_iterator])
                return out
            out = asyncio.run(run())
        finally:
            gateway.httpx.AsyncClient = orig
        self.assertIn("event: error", out)
        self.assertIn("empty or malformed stream", out)
        self.assertNotIn("message_stop", out, "空流应直接报错，不产出正常 message_stop")
        # message_start 已先行发出（防 claude 首事件超时），随后才 error
        self.assertLess(out.index("message_start"), out.index("event: error"),
                        "message_start 应先发，claude 才不至于误判空响应")


class TestResponsesGateway(unittest.TestCase):
    """网关走 Ollama /v1/responses（Responses API）相关测试。"""

    def setUp(self):
        # 思考能力探测结果按 (base, model) 进程内缓存；部分 fake 无 .post 会让探测失败并
        # 缓存 False，清除避免跨用例污染（尤其 think 开关用例依赖 fresh 探测）。
        gateway._THINK_CAP_CACHE.clear()

    def test_non_stream_response_responses(self):
        # Ollama /v1/responses 非流式响应（output[]: reasoning/message/function_call + usage）
        # → Anthropic Messages 响应
        resp = {
            "output": [
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": "推理"}]},
                {"type": "message", "content": [{"type": "output_text", "text": "答案"}]},
                {"type": "function_call", "call_id": "c1", "name": "end_discussion",
                 "arguments": '{"reason":"x"}'},
            ],
            "usage": {"input_tokens": 7, "output_tokens": 4},
        }
        a = gateway._non_stream_response_responses({"model": "gemma4:26b"}, resp)
        self.assertEqual(a["type"], "message")
        # thinking 块在 answer 块之前（干净名 end_discussion 还原为 MCP 原名）
        self.assertEqual(a["content"][0]["type"], "thinking")
        self.assertIn("推理", a["content"][0]["thinking"])
        self.assertEqual(a["content"][1], {"type": "text", "text": "答案"})
        self.assertEqual(a["content"][2]["type"], "tool_use")
        self.assertEqual(a["content"][2]["name"], "mcp__terminate-signal__end_discussion")
        self.assertEqual(a["content"][2]["input"], {"reason": "x"})
        self.assertEqual(a["stop_reason"], "tool_use")
        self.assertEqual(a["usage"]["input_tokens"], 7)
        self.assertEqual(a["usage"]["output_tokens"], 4)

    def test_stream_route_injects_num_ctx(self):
        # 请求必须发往 /v1/responses 且带 options.num_ctx（默认 16384）；
        # 历史 tool_use 必须转为 input 数组独立 function_call 项（去前缀），否则 Ollama 400
        orig = gateway.httpx.AsyncClient
        gateway.httpx.AsyncClient = _FakeCaptureBody
        try:
            async def run():
                resp = await gateway.handle_messages(
                    _FakeRequest({"model": "gemma", "messages": [
                        {"role": "user", "content": "hi"},
                        {"role": "assistant", "content": [
                            {"type": "tool_use", "id": "t1", "name": "end_discussion", "input": {"reason": "x"}}]},
                    ], "stream": True}),
                    {"ollama_base_url": "http://x"},
                )
                return "".join([c async for c in resp.body_iterator])
            asyncio.run(run())
        finally:
            gateway.httpx.AsyncClient = orig
        body = _FakeCaptureBody.captured
        self.assertEqual(body.get("model"), "gemma")
        self.assertEqual(body["options"]["num_ctx"], 16384, "默认 num_ctx 应为 16384")
        fcs = [it for it in body["input"] if it.get("type") == "function_call"]
        self.assertEqual(len(fcs), 1, "历史 tool_use 必须转为独立 function_call 项")
        self.assertEqual(fcs[0]["name"], "end_discussion")
        self.assertEqual(fcs[0]["call_id"], "t1")
        # 覆盖 settings 生效
        gateway.httpx.AsyncClient = _FakeCaptureBody
        try:
            async def run2():
                resp = await gateway.handle_messages(
                    _FakeRequest({"model": "gemma", "messages": [
                        {"role": "user", "content": "hi"},
                        {"role": "assistant", "content": [
                            {"type": "tool_use", "id": "t1", "name": "end_discussion", "input": {"reason": "x"}}]},
                    ], "stream": True}),
                    {"ollama_base_url": "http://x", "ollama_num_ctx": 8192},
                )
                return "".join([c async for c in resp.body_iterator])
            asyncio.run(run2())
        finally:
            gateway.httpx.AsyncClient = orig
        self.assertEqual(_FakeCaptureBody.captured["options"]["num_ctx"], 8192, "settings.ollama_num_ctx 应覆盖默认")

    def test_stream_route_parses_responses_thinking_and_tool(self):
        # /v1/responses 流式（reasoning_summary_text.done → output_text.delta →
        # function_call_arguments → completed）应被正确翻译为 Anthropic SSE
        orig = gateway.httpx.AsyncClient
        gateway.httpx.AsyncClient = _FakeOllamaClientThinkingTool
        try:
            async def run():
                resp = await gateway.handle_messages(
                    _FakeRequest({"model": "gemma4:26b", "messages": [
                        {"role": "user", "content": "hi"}], "stream": True}),
                    {"ollama_base_url": "http://x"},
                )
                return "".join([c async for c in resp.body_iterator])
            out = asyncio.run(run())
        finally:
            gateway.httpx.AsyncClient = orig
        self.assertIn("message_start", out)
        self.assertIn("thinking_delta", out, "reasoning_summary_text.done 应产出 thinking_delta")
        self.assertIn("text_delta", out)
        self.assertIn("tool_use", out, "function_call 应翻译为 tool_use 块")
        self.assertIn("message_stop", out)
        self.assertIn('"input_tokens": 10', out, "response.completed usage 应回填")
        self.assertIn('"output_tokens": 3', out)
        self.assertLess(out.index("thinking_delta"), out.index("tool_use"), "思考块应在工具块之前")

    def test_stream_route_respects_think_switch(self):
        # think=False → reasoning.effort=none（关思考）；think=True 且模型支持 →
        # reasoning.effort=low（与 codex 路径一致）；默认 True 同 think=True
        def send(settings):
            orig = gateway.httpx.AsyncClient
            gateway.httpx.AsyncClient = _FakeCaptureShowBody
            try:
                async def run():
                    resp = await gateway.handle_messages(
                        _FakeRequest({"model": "gemma4:26b", "messages": [{"role": "user", "content": "hi"}], "stream": True}),
                        settings,
                    )
                    return "".join([c async for c in resp.body_iterator])
                asyncio.run(run())
            finally:
                gateway.httpx.AsyncClient = orig
            return _FakeCaptureShowBody.captured

        body_on = send({"ollama_base_url": "http://x", "think": True})
        self.assertEqual(body_on.get("reasoning"), {"effort": "low"}, "think=True 且支持思考 → effort=low")
        body_off = send({"ollama_base_url": "http://x", "think": False})
        self.assertEqual(body_off.get("reasoning"), {"effort": "none"}, "think=False → effort=none")
        body_default = send({"ollama_base_url": "http://x"})
        self.assertEqual(body_default.get("reasoning"), {"effort": "low"}, "默认 think=True → effort=low")

    def test_stream_route_forwards_tools(self):
        # 真工具调用路径：请求带 end_discussion 工具时，必须透传给 Ollama /v1/responses，
        # 否则 gemma 收不到工具定义、永不产出 tool_calls。格式为扁平 name/parameters。
        orig = gateway.httpx.AsyncClient
        gateway.httpx.AsyncClient = _FakeCaptureBody
        try:
            async def run():
                resp = await gateway.handle_messages(
                    _FakeRequest({"model": "gemma", "messages": [
                        {"role": "user", "content": "我们达成一致了吗"},
                    ], "stream": True,
                    "tools": [
                        {"name": "Bash", "description": "d", "input_schema": {"type": "object"}},
                        {"name": "mcp__terminate-signal__end_discussion", "description": "d",
                         "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
                    ]}),
                    {"ollama_base_url": "http://x"},
                )
                return "".join([c async for c in resp.body_iterator])
            asyncio.run(run())
        finally:
            gateway.httpx.AsyncClient = orig
        body = _FakeCaptureBody.captured
        self.assertIn("tools", body, "tools 必须透传给 Ollama，否则 gemma 无法调用 end_discussion")
        names = [t["name"] for t in body["tools"]]
        self.assertIn("end_discussion", names, "受控工具应转发且去前缀")
        self.assertIn("Bash", names, "claude 内置工具应转发（让其能调用）")
        self.assertNotIn("mcp__", names[0], "模型侧应为干净名")
        # end_discussion 的 input_schema 应映射为 parameters 透传（扁平 Responses 格式）
        ed = next(t for t in body["tools"] if t["name"] == "end_discussion")
        self.assertEqual(ed["parameters"],
                         {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
                         "input_schema 应映射为 parameters 透传")

    def test_stream_route_parses_native_no_prefix(self):
        # 真实 Ollama /v1/responses 流式不带 data: 前缀，网关必须能解析（否则空流）；
        # 断言翻译出 tool_use 块、无空流错误、usage 回填。
        orig = gateway.httpx.AsyncClient
        gateway.httpx.AsyncClient = _FakeOllamaClientNativeNoPrefix
        try:
            async def run():
                resp = await gateway.handle_messages(
                    _FakeRequest({"model": "gemma4:26b", "messages": [
                        {"role": "user", "content": "hi"},
                    ], "stream": True,
                    "tools": [{"name": "mcp__terminate-signal__end_discussion", "description": "d",
                               "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}}]}),
                    {"ollama_base_url": "http://x"},
                )
                return "".join([c async for c in resp.body_iterator])
            out = asyncio.run(run())
        finally:
            gateway.httpx.AsyncClient = orig
        self.assertIn("tool_use", out, "无前缀原生流式应翻译出 tool_use 块")
        self.assertIn("thinking_delta", out)
        self.assertNotIn("empty or malformed stream", out, "不得再误判空流")
        self.assertIn('"input_tokens": 10', out, "response.completed usage 应回填")
        self.assertIn('"stop_reason": "tool_use"', out, "有 tool_use 块时 stop_reason 必须为 tool_use")


class TestSessionThinkOverride(unittest.TestCase):
    """三级「思考」开关：网关按 会话级 > 全局（+ 编排层 think_override）解析。"""

    def _setup_session(self, think_value):
        """建临时会话并（可选地）写入会话级 think；返回 (project_path, sid)。"""
        import tempfile
        import pathlib
        import backend.store as store
        tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="agentchat_think_"))
        # 隔离存储，避免污染真实 data/config.json
        prev = store.CONFIG_FILE
        cfg = tmpdir / "config.json"
        store.CONFIG_FILE = cfg
        try:
            proj_path = str(tmpdir / "myproj")
            pathlib.Path(proj_path).mkdir(parents=True, exist_ok=True)
            s = store.create_session(proj_path, "测试会话")
            sid = s["id"]
            if think_value is not None:
                store.set_session_think(proj_path, sid, think_value)
            return proj_path, sid, prev
        finally:
            # CONFIG_FILE 还原交给调用方（下面用 addCleanup 思路，简单起见这里返回 prev）
            pass

    def _restore(self, prev):
        import backend.store as store
        if prev is not None:
            store.CONFIG_FILE = prev

    def test_global_default_when_no_session_value(self):
        import backend.store as store
        proj_path, sid, prev = self._setup_session(None)
        try:
            # 全局 True、会话未设置 → 回退 True
            self.assertTrue(gateway._resolve_think({"think": True}, proj_path, sid))
            # 全局 False、会话未设置 → 回退 False
            self.assertFalse(gateway._resolve_think({"think": False}, proj_path, sid))
        finally:
            self._restore(prev)

    def test_session_false_overrides_global_true(self):
        # 会话显式关思考 → 即使全局开，也应为 False
        proj_path, sid, prev = self._setup_session(False)
        try:
            self.assertFalse(gateway._resolve_think({"think": True}, proj_path, sid))
            self.assertFalse(gateway._resolve_think({"think": False}, proj_path, sid))
        finally:
            self._restore(prev)

    def test_session_true_overrides_global_false(self):
        # 会话显式开思考 → 即使全局关，也应为 True（会话级覆盖全局）
        proj_path, sid, prev = self._setup_session(True)
        try:
            self.assertTrue(gateway._resolve_think({"think": False}, proj_path, sid))
            self.assertTrue(gateway._resolve_think({"think": True}, proj_path, sid))
        finally:
            self._restore(prev)

    def test_no_sid_falls_back_to_global(self):
        # 不带 sid（无会话路径的 /v1/messages 路由）→ 只看全局默认
        self.assertTrue(gateway._resolve_think({"think": True}, None, None))
        self.assertFalse(gateway._resolve_think({"think": False}, None, None))

    def test_override_wins_over_session_and_global(self):
        # 编排层 think_override（已按 会话>agent>全局 算好）优先级最高：
        # 全局开 + 会话未设 + override=False（agent 级关）→ False
        proj_path, sid, prev = self._setup_session(None)
        try:
            self.assertFalse(gateway._resolve_think({"think": True}, proj_path, sid, think_override=False))
        finally:
            self._restore(prev)
        # 全局关 + override=True（agent/会话级开）→ True
        self.assertTrue(gateway._resolve_think({"think": False}, None, None, think_override=True))


class TestThinkRouteRegistered(unittest.TestCase):
    """三级思考开关经 **路径段** think/<0|1> 传入（claude 以 base_url + "/v1/messages"
    字符串拼接，base_url 带 ?query 会被拼坏），确认新路由已注册、旧路由保留。"""

    def test_think_path_routes_registered(self):
        from backend.main import app
        paths = {getattr(r, "path", "") for r in app.routes}
        self.assertIn("/gw/{pid}/{sid}/think/{flag}/v1/messages", paths,
                      "带 think 路径段的路由应已注册（agent 级生效值经此下发）")
        self.assertIn("/gw/{pid}/{sid}/v1/messages", paths, "旧路由保留（无 agent 维度的会话/全局解析）")


class _FakeShowAndChat:
    """同时支持 /api/show（thinking 能力查询，走 client.post）与 /v1/responses 流式，并捕获请求体。"""
    caps: list = ["completion", "tools", "thinking"]
    show_calls: int = 0
    captured: dict = {}
    fail_show: bool = False

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, **kw):
        cls = _FakeShowAndChat
        cls.show_calls += 1
        if cls.fail_show:
            raise RuntimeError("/api/show 不可用")
        caps = list(cls.caps)

        class _R:
            status_code = 200

            def json(self):
                return {"capabilities": caps}

        return _R()

    def stream(self, method, url, json=None, headers=None):
        _FakeShowAndChat.captured = json or {}
        return _FakeOllamaCtx([
            'data: {"type":"response.output_text.delta","delta":"ok"}',
            'data: {"type":"response.completed","response":{"output":[],"usage":{"input_tokens":1,"output_tokens":1}}}',
        ])


class TestThinkExplicitPassthrough(unittest.IsolatedAsyncioTestCase):
    """思考开关必须**显式双向透传**（2026-09-17 修，2026-09-18 改为 Responses API 契约）。

    网关走 Ollama /v1/responses：生效值 False → reasoning.effort=none（关思考）；True 且模型
    声明支持 thinking → reasoning.effort=low（与 codex 路径一致，显式比依赖默认更稳）。
    实测 Ollama 0.33.3 的 /v1/responses 不带 reasoning 时 gemma4:26b 思考不稳定，故生效值 True
    必须显式下发 effort，否则用户「勾了思考」在 claude→网关 链路静默失效。
    """

    def setUp(self):
        gateway._THINK_CAP_CACHE.clear()
        _FakeShowAndChat.caps = ["completion", "tools", "thinking"]
        _FakeShowAndChat.show_calls = 0
        _FakeShowAndChat.captured = {}
        _FakeShowAndChat.fail_show = False
        self._orig = gateway.httpx.AsyncClient
        gateway.httpx.AsyncClient = _FakeShowAndChat

    def tearDown(self):
        gateway.httpx.AsyncClient = self._orig
        gateway._THINK_CAP_CACHE.clear()

    async def _run(self, settings, body=None):
        resp = await gateway.handle_messages(
            _FakeRequest(body or {"model": "gemma4:26b", "messages": [{"role": "user", "content": "hi"}],
                                 "stream": True}),
            settings,
        )
        await resp.body_iterator.__anext__()
        return _FakeShowAndChat.captured

    async def test_think_true_sent_explicitly(self):
        body = await self._run({"ollama_base_url": "http://x", "think": True})
        self.assertEqual(body.get("reasoning"), {"effort": "low"},
                          "生效值 True 且模型支持思考必须显式带 reasoning.effort=low")

    async def test_think_true_via_override(self):
        # 编排层算好的 think_override 优先（claude 会话路径 /gw/<pid>/<sid>/think/1）
        resp = await gateway.handle_messages(
            _FakeRequest({"model": "gemma4:26b", "messages": [{"role": "user", "content": "hi"}], "stream": True}),
            {"ollama_base_url": "http://x", "think": False}, think_override=True)
        await resp.body_iterator.__anext__()
        self.assertEqual(_FakeShowAndChat.captured.get("reasoning"), {"effort": "low"})

    async def test_think_false_still_sent(self):
        body = await self._run({"ollama_base_url": "http://x", "think": False})
        self.assertEqual(body.get("reasoning"), {"effort": "none"}, "think=False → reasoning.effort=none")
        self.assertEqual(_FakeShowAndChat.show_calls, 0, "关思考无需查能力")

    async def test_think_omitted_when_model_lacks_thinking(self):
        _FakeShowAndChat.caps = ["completion", "tools"]
        body = await self._run({"ollama_base_url": "http://x", "think": True})
        self.assertNotIn("reasoning", body, "模型不声明 thinking 时保持旧行为（不传 reasoning）")

    async def test_capability_probe_cached(self):
        await self._run({"ollama_base_url": "http://x", "think": True})
        await self._run({"ollama_base_url": "http://x", "think": True})
        self.assertEqual(_FakeShowAndChat.show_calls, 1, "/api/show 结果应按 (base, model) 缓存")

    async def test_probe_failure_keeps_old_behaviour(self):
        _FakeShowAndChat.fail_show = True
        body = await self._run({"ollama_base_url": "http://x", "think": True})
        self.assertNotIn("think", body, "能力探测失败不得让整轮请求出错，回退为不传")

    async def test_unsupported_model_name_returns_false(self):
        self.assertFalse(await gateway._ollama_supports_thinking({"ollama_base_url": "http://x"}, None))


class TestControlledToolForward(unittest.TestCase):
    """HITL + 终止：网关只把受控工具（end_discussion / ask_human_input）透传给模型。

    锁定：其它 MCP 工具（如真实 bash）不得泄漏给本地模型；两个受控工具都转发且名被剥前缀；
    _restore_mcp_name 对干净名 / anthrothic: 变体都能还原成 MCP 原名。
    """

    def _oa_tools(self, names):
        body = {"model": "gemma", "messages": [], "tools": [
            {"name": n, "description": "d", "input_schema": {}} for n in names
        ]}
        return gateway.anthropic_to_openai(body).get("tools", [])

    def test_only_controlled_tools_forwarded(self):
        out = self._oa_tools([
            "mcp__terminate-signal__end_discussion",
            "mcp__terminate-signal__ask_human_input",
            "mcp__some-server__bash",
        ])
        kept = [t["function"]["name"] for t in out]
        self.assertIn("end_discussion", kept)
        self.assertIn("ask_human_input", kept)
        self.assertNotIn("bash", kept)
        self.assertEqual(len(out), 2, "只应转发 2 个受控工具，实际 " + str(kept))

    def test_no_controlled_tools_drops_all(self):
        # 没有任何受控工具时不应伪造 tools 给本地模型
        out = self._oa_tools(["mcp__some-server__bash"])
        self.assertEqual(out, [])

    def test_restore_mcp_name_both(self):
        self.assertEqual(gateway._restore_mcp_name("end_discussion"),
                         "mcp__terminate-signal__end_discussion")
        self.assertEqual(gateway._restore_mcp_name("ask_human_input"),
                         "mcp__terminate-signal__ask_human_input")
        self.assertEqual(gateway._restore_mcp_name("anthropic:ask_human_input"),
                         "mcp__terminate-signal__ask_human_input")
        # 非受控工具名原样返回
        self.assertEqual(gateway._restore_mcp_name("bash"), "bash")

    def test_restore_mcp_name_legacy_maps_to_new(self):
        """旧名 request_human_input 必须归一到 ask_human_input —— MCP server 已只注册新名，
        还原成旧名会让 claude 报「No such tool available」（历史提示词残留时踩到）。"""
        for n in ("request_human_input", "anthropic:request_human_input"):
            self.assertEqual(gateway._restore_mcp_name(n),
                             "mcp__terminate-signal__ask_human_input")

    def test_legacy_controlled_tool_still_forwarded(self):
        # 旧名受控工具仍要透传（本地模型可能沿用旧名），且还原时会被归一到新名
        out = self._oa_tools(["mcp__terminate-signal__request_human_input"])
        self.assertEqual([t["function"]["name"] for t in out], ["request_human_input"])

    def test_claude_builtin_tools_forwarded(self):
        # claude 内置工具（无 mcp__ 前缀）应原样转发给本地模型，让其能调；
        # 第三方 MCP 工具与交互型（无 TTY 通道）工具必须剥。
        out = self._oa_tools([
            "Bash", "Read", "Glob", "Grep",
            "mcp__terminate-signal__end_discussion",
            "mcp__some-server__secret",
            "AskUserQuestion",
        ])
        kept = [t["function"]["name"] for t in out]
        for n in ("Bash", "Read", "Glob", "Grep", "end_discussion"):
            self.assertIn(n, kept)
        self.assertNotIn("secret", kept)
        self.assertNotIn("AskUserQuestion", kept)
        self.assertEqual(len(out), 5, "应保留 4 内置 + 1 受控，实际 " + str(kept))

    def test_claude_builtin_without_controlled_kept(self):
        # 无受控工具、只有 claude 内置工具时，内置工具仍应转发（不误剥）
        out = self._oa_tools(["Bash", "Read"])
        kept = [t["function"]["name"] for t in out]
        self.assertEqual(set(kept), {"Bash", "Read"})


class TestResponsesTranslation(unittest.TestCase):
    def _run_adapter(self, events):
        """把一串 Responses SSE 事件喂给 _ResponsesStreamAdapter，收集所有 Anthropic SSE dict。"""
        a = gateway._ResponsesStreamAdapter(model="gemma4:26b", est_input_tokens=10)
        out = list(a.start())
        for ev in events:
            out += a.handle(ev)
        out += a.finish()
        return out

    def test_anthropic_to_responses_basic(self):
        resp = gateway.anthropic_to_responses({
            "model": "gemma4:26b", "stream": True,
            "system": [{"type": "text", "text": "你是架构师"}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        })
        self.assertEqual(resp["model"], "gemma4:26b")
        self.assertIn("你是架构师", resp["instructions"])
        self.assertIn("【语言强制指令】", resp["instructions"])
        self.assertEqual(resp["input"][0], {"role": "user", "content": "hi"})
        self.assertTrue(resp["stream"])

    def test_anthropic_to_responses_stream_defaults_false(self):
        # Anthropic Messages 的 stream 缺省为 false（缺字段=非流式）。非流式回退请求不带该
        # 字段，若这里按 True 兜底，Ollama 会回 SSE 而 handle_messages 又走 r.json() → 必错。
        r = gateway.anthropic_to_responses({"model": "gemma4:26b", "messages": []})
        self.assertFalse(r["stream"], "缺 stream 字段应按 Anthropic 缺省(false)处理")

    def test_adapter_text_before_reasoning_switches_blocks(self):
        # 真实 Ollama 事件序（curl 实测 probe/app-repro/raw/ollama_event_order.sse）：
        # output_text.delta("---") → [一批 reasoning_summary_text.delta，网关忽略] →
        # reasoning_summary_text.done("先想一下") → output_text.delta("1") → completed。
        # 即「正文 → 思考 → 正文」，翻译器必须切块（text0 / thinking1 / text2），不得混排。
        events = [
            {"type": "response.output_item.added", "item": {"type": "reasoning", "id": "rs1"}},
            {"type": "response.output_text.delta", "delta": "---"},
            {"type": "response.reasoning_summary_text.delta", "item_id": "rs1", "delta": "先"},
            {"type": "response.reasoning_summary_text.done", "item_id": "rs1", "text": "先想一下"},
            {"type": "response.output_text.delta", "delta": "1"},
            {"type": "response.completed",
             "response": {"output": [], "usage": {"input_tokens": 8, "output_tokens": 4}}},
        ]
        out = self._run_adapter(events)
        seq = []
        open_idx = None
        for e in out:
            if e["type"] == "content_block_start":
                self.assertIsNone(open_idx, "同一时刻只能有一个开块")
                open_idx = e["index"]
                seq.append(("start", e["index"], e["content_block"]["type"]))
            elif e["type"] == "content_block_delta":
                self.assertEqual(e["index"], open_idx, "delta 必须落在当前开块上（不得跨块混排）")
                seq.append(("delta", e["index"], e["delta"]["type"]))
            elif e["type"] == "content_block_stop":
                self.assertEqual(e["index"], open_idx)
                seq.append(("stop", e["index"]))
                open_idx = None
        self.assertIsNone(open_idx, "结尾不得残留未收口的块")
        self.assertEqual(seq, [
            ("start", 0, "text"), ("delta", 0, "text_delta"), ("stop", 0),
            ("start", 1, "thinking"), ("delta", 1, "thinking_delta"),
            ("delta", 1, "signature_delta"), ("stop", 1),
            ("start", 2, "text"), ("delta", 2, "text_delta"), ("stop", 2),
        ], "正文→思考→正文 必须各自成块（旧实现同 index 混排使 claude 判流非法）")

    def test_anthropic_to_responses_tool_history(self):
        # assistant tool_use → 独立 function_call 项（去 mcp__ 前缀）；tool_result → function_call_output
        resp = gateway.anthropic_to_responses({
            "model": "gemma4:26b", "stream": True,
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "算一下"}]},
                {"role": "assistant", "content": [
                    {"type": "text", "text": "我来算"},
                    {"type": "tool_use", "id": "tool_abc", "name": "mcp__terminate-signal__end_discussion", "input": {}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tool_abc", "content": "ok"},
                ]},
            ],
        })
        items = resp["input"]
        # 顺序：user 文本 → assistant 文本(output_text) → 独立 function_call → function_call_output
        self.assertEqual(items[0], {"role": "user", "content": "算一下"})
        self.assertEqual(items[1], {"role": "assistant", "content": [{"type": "output_text", "text": "我来算"}]})
        self.assertEqual(items[2]["type"], "function_call")
        self.assertEqual(items[2]["call_id"], "tool_abc")
        self.assertEqual(items[2]["name"], "end_discussion")  # 历史名也去前缀，与广告一致
        self.assertEqual(items[3], {"type": "function_call_output", "call_id": "tool_abc", "output": "ok"})

    def test_anthropic_to_responses_tools_filtered(self):
        # 注意：claude 请求里的 tools 是 Anthropic 格式（name/description/input_schema 平铺），
        # 经 anthropic_to_openai 过滤后转成 OpenAI 嵌套（function:{name,...}），
        # anthropic_to_responses 再展平成 Responses 扁平格式。
        resp = gateway.anthropic_to_responses({
            "model": "gemma4:26b", "stream": True,
            "messages": [],
            "tools": [
                {"name": "mcp__terminate-signal__end_discussion", "description": "x", "input_schema": {}},
                {"name": "Bash", "description": "y", "input_schema": {}},
                {"name": "mcp__some-server__secret", "description": "z", "input_schema": {}},
                {"name": "AskUserQuestion", "description": "w", "input_schema": {}},
            ],
        })
        names = [t["name"] for t in resp["tools"]]
        self.assertIn("end_discussion", names)
        self.assertIn("Bash", names)
        self.assertNotIn("secret", names)
        self.assertNotIn("AskUserQuestion", names)
        # Responses 格式应为扁平 name/description/parameters（非嵌套 function）
        self.assertIn("parameters", resp["tools"][0])

    def test_adapter_text_and_thinking(self):
        events = [
            {"type": "response.output_item.added", "item": {"type": "reasoning", "id": "rs1"}},
            {"type": "response.reasoning_summary_text.done", "item_id": "rs1",
             "text": "先想一下：2+3=5。"},
            {"type": "response.output_item.added", "item": {"type": "message", "id": "m1"}},
            {"type": "response.content_part.added", "output_index": 1},
            {"type": "response.output_text.delta", "delta": "结果是"},
            {"type": "response.output_text.delta", "delta": " 5。"},
            {"type": "response.output_text.done", "text": "结果是 5。"},
            {"type": "response.content_part.done", "output_index": 1},
            {"type": "response.output_item.done", "item": {"type": "message", "id": "m1"}},
            {"type": "response.completed", "response": {"output": [], "usage": {"input_tokens": 12, "output_tokens": 9}}},
        ]
        out = self._run_adapter(events)
        types = [e["type"] for e in out]
        self.assertIn("message_start", types)
        self.assertIn("content_block_start", types)
        self.assertIn("message_stop", types)
        # 思考块：content_block_start(type=thinking) + content_block_delta(delta.type=thinking_delta)
        # + signature_delta（原生 thinking 块，claude 原样落盘）
        thinking_start = [e for e in out if e["type"] == "content_block_start" and e["content_block"]["type"] == "thinking"]
        self.assertEqual(len(thinking_start), 1)
        self.assertTrue(any(e["type"] == "content_block_delta" and e.get("delta", {}).get("type") == "signature_delta" for e in out))
        # 思考内容随 thinking_delta 出现（可能被拆成多片，拼接后校验）
        think_txt = "".join(e["delta"].get("thinking", "") for e in out
                            if e["type"] == "content_block_delta" and e.get("delta", {}).get("type") == "thinking_delta")
        self.assertIn("2+3=5", think_txt)
        # 正文随 text_delta 出现（拼接后校验）
        ans_txt = "".join(e["delta"].get("text", "") for e in out
                         if e["type"] == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta")
        self.assertIn("结果是 5", ans_txt)
        # 结束 stop_reason=end_turn
        md = [e for e in out if e["type"] == "message_delta"][0]
        self.assertEqual(md["delta"]["stop_reason"], "end_turn")
        self.assertEqual(md["usage"]["output_tokens"], 9)

    def test_adapter_tool_call_restores_mcp_name(self):
        events = [
            {"type": "response.output_item.added", "item": {"type": "function_call", "id": "fc1",
             "call_id": "call_1", "name": "end_discussion", "arguments": ""}},
            {"type": "response.function_call_arguments.delta", "item_id": "fc1", "output_index": 0, "delta": "{\"reason\""},
            {"type": "response.function_call_arguments.delta", "item_id": "fc1", "output_index": 0, "delta": ":\"x\"}"},
            {"type": "response.function_call_arguments.done", "item_id": "fc1", "output_index": 0, "arguments": "{\"reason\":\"x\"}"},
            {"type": "response.output_item.done", "item": {"type": "function_call", "id": "fc1", "call_id": "call_1", "name": "end_discussion", "arguments": "{\"reason\":\"x\"}"}},
            {"type": "response.completed", "response": {"output": [], "usage": {"input_tokens": 5, "output_tokens": 3}}},
        ]
        out = self._run_adapter(events)
        tus = [e for e in out if e["type"] == "content_block_start" and e["content_block"]["type"] == "tool_use"]
        self.assertEqual(len(tus), 1)
        # 干净名 end_discussion 应被还原为 mcp__terminate-signal__end_discussion（claude 才能匹配 MCP server）
        self.assertEqual(tus[0]["content_block"]["name"], "mcp__terminate-signal__end_discussion")
        md = [e for e in out if e["type"] == "message_delta"][0]
        self.assertEqual(md["delta"]["stop_reason"], "tool_use")


if __name__ == "__main__":
    unittest.main()
