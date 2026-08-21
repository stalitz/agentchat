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
        # 2026-08-19：只转发「含 end_discussion 的 MCP 工具」给模型，不转发 claude 内置工具；
        # 且转发时去 mcp__ 前缀（模型见干净名 end_discussion）
        oa = gateway.anthropic_to_openai({
            "model": "gemma", "messages": [],
            "tools": [
                {"name": "Bash", "description": "d", "input_schema": {"type": "object"}},
                {"name": "mcp__terminate-signal__end_discussion", "description": "d",
                 "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
            ],
        })
        tools = oa.get("tools") or []
        names = [t["function"]["name"] for t in tools]
        self.assertEqual(names, ["end_discussion"], "只转发 end_discussion 且去前缀")
        self.assertNotIn("Bash", names, "claude 内置工具不得转发")
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

    def test_internal_tools_not_forwarded(self):
        # claude 内置工具（Bash 等）不转发给模型（防乱调）；无 end_discussion 的请求不带 tools 字段
        oa = gateway.anthropic_to_openai({
            "model": "gemma", "messages": [],
            "tools": [{"name": "Bash", "description": "d", "input_schema": {"type": "object"}}],
        })
        self.assertNotIn("tools", oa, "claude 内置工具不应转发")
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
        self.assertEqual(seq[-1], ("stop", 2), "tool_use 块正常收口")

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


class TestNonStreamResponse(unittest.TestCase):
    def test_translate(self):
        body = {"model": "gemma"}
        oa_resp = {
            "id": "chatcmpl-1", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }
        a = gateway._non_stream_response(body, oa_resp)
        self.assertEqual(a["type"], "message")
        self.assertEqual(a["role"], "assistant")
        self.assertEqual(a["content"][0], {"type": "text", "text": "hi"})
        self.assertEqual(a["stop_reason"], "end_turn")
        self.assertEqual(a["usage"]["input_tokens"], 5)
        self.assertEqual(a["usage"]["output_tokens"], 2)

    def test_translate_with_tool(self):
        body = {"model": "gemma"}
        oa_resp = {
            "id": "c", "choices": [{"index": 0, "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None,
                    "tool_calls": [{"id": "t1", "function": {"name": "read", "arguments": "{\"path\":\"/x\"}"}}]}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        a = gateway._non_stream_response(body, oa_resp)
        self.assertEqual(a["content"][0]["type"], "tool_use")
        self.assertEqual(a["content"][0]["name"], "read")
        self.assertEqual(a["content"][0]["input"], {"path": "/x"})
        self.assertEqual(a["stop_reason"], "tool_use")


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
    """模拟 Ollama 原生 /api/chat 流式响应（增量在 message.content，done 事件带计数）。"""
    def __init__(self, *a, **k):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    def stream(self, method, url, json=None, headers=None):
        return _FakeOllamaCtx([
            'data: {"message":{"role":"assistant","content":"你好"}}',
            'data: {"done":true,"done_reason":"stop","prompt_eval_count":5,"eval_count":2}',
        ])


class _FakeOllamaClientThinkingTool:
    """模拟 Ollama 原生 /api/chat：thinking → text → tool_calls → done(tool_calls)。"""
    def __init__(self, *a, **k):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    def stream(self, method, url, json=None, headers=None):
        return _FakeOllamaCtx([
            'data: {"message":{"role":"assistant","thinking":"让我想想"}}',
            'data: {"message":{"role":"assistant","content":"结论"}}',
            'data: {"message":{"role":"assistant","content":"","tool_calls":[{"function":{"name":"end_discussion","arguments":"{\\"reason\\":\\"x\\"}"}}]}}',
            'data: {"done":true,"done_reason":"tool_calls","prompt_eval_count":10,"eval_count":3}',
        ])


class _FakeOllamaClientNativeNoPrefix:
    """模拟 Ollama 原生 /api/chat 真实流式格式：不带 data: 前缀的纯 JSON 行
    （8e0eef0 的假数据带 data: 前缀，掩盖了网关要求 data: 前缀导致空流的 bug）。
    同时 tool_calls.arguments 用对象（gemma4 实测返回对象），覆盖参数类型兼容。"""
    def __init__(self, *a, **k):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    def stream(self, method, url, json=None, headers=None):
        return _FakeOllamaCtx([
            '{"message":{"role":"assistant","thinking":"让我想想"}}',
            '{"message":{"role":"assistant","content":"结论"}}',
            '{"message":{"role":"assistant","content":"","tool_calls":[{"function":{"name":"end_discussion","arguments":{"reason":"x"}}}]}}',
            '{"done":true,"done_reason":"stop","prompt_eval_count":10,"eval_count":3}',
        ])


class _FakeCaptureBody:
    """捕获发给 Ollama 的请求体，验证 options.num_ctx 注入。"""
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
            'data: {"message":{"role":"assistant","content":"ok"}}',
            'data: {"done":true,"done_reason":"stop","prompt_eval_count":1,"eval_count":1}',
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


class TestOllamaNativeGateway(unittest.TestCase):
    """原生 /api/chat 端点（options.num_ctx 控制上下文）相关测试。"""

    def test_ollama_tool_calls_to_openai(self):
        # Ollama 原生 tool_calls（无 index/id）→ OpenAI 增量格式，供 translator 复用
        out = gateway._ollama_tool_calls_to_openai([
            {"function": {"name": "end_discussion", "arguments": '{"reason":"x"}'}},
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["index"], 0)
        self.assertEqual(out[0]["id"], "tool_0")
        self.assertEqual(out[0]["function"]["name"], "end_discussion")
        self.assertEqual(out[0]["function"]["arguments"], '{"reason":"x"}')
        self.assertIsNone(gateway._ollama_tool_calls_to_openai(None))

    def test_openai_messages_to_ollama(self):
        # 历史含 OpenAI 格式 tool_calls（arguments 字符串、content null）→ Ollama 原生格式
        #（arguments 对象、content 空串），否则 Ollama /api/chat 直接 400 拒绝
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "t1", "type": "function",
                             "function": {"name": "end_discussion", "arguments": '{"reason":"x"}'}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "已收到"},
        ]
        out = gateway._openai_messages_to_ollama(msgs)
        self.assertEqual(out[0], msgs[0])
        self.assertEqual(out[1]["content"], "", "null content 应转为空串")
        tc = out[1]["tool_calls"][0]
        self.assertEqual(tc["function"]["name"], "end_discussion")
        self.assertEqual(tc["function"]["arguments"], {"reason": "x"}, "arguments 字符串应解析为对象")
        self.assertNotIn("id", tc, "Ollama 原生不需要 id/type")
        self.assertEqual(out[2]["role"], "tool")
        self.assertEqual(out[2]["content"], "已收到")

    def test_map_stop_ollama(self):
        self.assertEqual(gateway._map_stop_ollama("stop"), "end_turn")
        self.assertEqual(gateway._map_stop_ollama("tool_calls"), "tool_use")
        self.assertEqual(gateway._map_stop_ollama("length"), "max_tokens")
        self.assertEqual(gateway._map_stop_ollama("unknown"), "end_turn")

    def test_non_stream_response_ollama(self):
        # Ollama 原生非流式响应（message.thinking/content/tool_calls + prompt_eval_count/eval_count）
        # → Anthropic Messages 响应
        resp = {
            "message": {"role": "assistant", "thinking": "推理", "content": "答案",
                        "tool_calls": [{"function": {"name": "read", "arguments": '{"path":"/x"}'}}]},
            "done_reason": "tool_calls", "prompt_eval_count": 7, "eval_count": 4,
        }
        a = gateway._non_stream_response_ollama({"model": "gemma"}, resp)
        self.assertEqual(a["type"], "message")
        # thinking 块在 answer 块之前
        self.assertEqual(a["content"][0], {"type": "thinking", "thinking": "推理", "signature": a["content"][0]["signature"]})
        self.assertEqual(a["content"][1], {"type": "text", "text": "答案"})
        self.assertEqual(a["content"][2]["type"], "tool_use")
        self.assertEqual(a["content"][2]["name"], "read")
        self.assertEqual(a["content"][2]["input"], {"path": "/x"})
        self.assertEqual(a["stop_reason"], "tool_use")
        # 原生计数映射到 Anthropic usage
        self.assertEqual(a["usage"]["input_tokens"], 7)
        self.assertEqual(a["usage"]["output_tokens"], 4)

    def test_stream_route_injects_num_ctx(self):
        # 请求必须发往 /api/chat 且带 options.num_ctx（默认 16384）
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
        self.assertTrue(body.get("model") == "gemma")
        self.assertEqual(body["options"]["num_ctx"], 16384, "默认 num_ctx 应为 16384")
        # 历史 tool_calls 必须转为 Ollama 原生（arguments 对象），否则 Ollama 400 拒绝整轮
        hist = [m for m in body["messages"] if m.get("tool_calls")]
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["tool_calls"][0]["function"]["arguments"], {"reason": "x"})
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

    def test_stream_route_parses_ollama_native_thinking_and_tool(self):
        # 原生 /api/chat 流式（thinking→text→tool_calls→done）应被正确翻译为 Anthropic SSE
        orig = gateway.httpx.AsyncClient
        gateway.httpx.AsyncClient = _FakeOllamaClientThinkingTool
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
            out = asyncio.run(run())
        finally:
            gateway.httpx.AsyncClient = orig
        self.assertIn("message_start", out)
        self.assertIn("thinking_delta", out, "原生 thinking 块应产出 thinking_delta")
        self.assertIn("text_delta", out)
        self.assertIn("tool_use", out, "tool_calls 应翻译为 tool_use 块")
        self.assertIn("message_stop", out)
        self.assertIn('"input_tokens": 10', out, "done 事件 prompt_eval_count 应回填 usage")
        self.assertIn('"output_tokens": 3', out)
        self.assertLess(out.index("thinking_delta"), out.index("tool_use"), "思考块应在工具块之前")

    def test_stream_route_forwards_tools_to_ollama(self):
        # 真工具调用路径：请求带 end_discussion 工具时，必须透传给 Ollama 原生 /api/chat，
        # 否则 gemma 收不到工具定义、永不产出 tool_calls（8e0eef0 改原生端点时漏传，回归）。
        # 这里端到端经过 anthropic_to_openai（过滤+去前缀+input_schema→parameters）再发出。
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
        names = [t["function"]["name"] for t in body["tools"]]
        self.assertEqual(names, ["end_discussion"], "只转发 end_discussion 且去前缀")
        self.assertNotIn("Bash", names, "claude 内置工具不转发")
        self.assertNotIn("mcp__", names[0], "模型侧应为干净名")
        self.assertEqual(body["tools"][0]["function"]["parameters"],
                         {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
                         "input_schema 应映射为 parameters 透传")

    def test_ollama_tool_calls_arguments_object(self):
        # gemma4 原生 /api/chat 返回 tool_calls.arguments 为对象（非字符串增量），
        # 网关必须统一 json.dumps 成字符串 partial_json，否则下游把 dict 当 partial_json 出错
        out = gateway._ollama_tool_calls_to_openai([
            {"function": {"name": "end_discussion", "arguments": {"reason": "x"}}},
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["function"]["name"], "end_discussion")
        self.assertEqual(out[0]["function"]["arguments"], '{"reason": "x"}', "对象 arguments 应转成字符串")
        # 字符串 arguments 保持原样（旧模型增量格式）
        out2 = gateway._ollama_tool_calls_to_openai([
            {"function": {"name": "end_discussion", "arguments": '{"reason":"x"}'}},
        ])
        self.assertEqual(out2[0]["function"]["arguments"], '{"reason":"x"}')

    def test_stream_route_parses_native_no_prefix(self):
        # 真实 Ollama 原生 /api/chat 流式不带 data: 前缀，网关必须能解析（否则空流）；
        # 同时覆盖 tool_calls.arguments 为对象的真实格式。断言翻译出 tool_use 块、无空流错误。
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
        self.assertIn('"input_tokens": 10', out, "done 事件 usage 应回填")
        self.assertIn('"stop_reason": "tool_use"', out, "有 tool_use 块时 stop_reason 必须为 tool_use（gemma4 实测 done_reason=stop）")


if __name__ == "__main__":
    unittest.main()
