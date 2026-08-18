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
        self.assertEqual(oa["messages"][0], {"role": "system", "content": "你是架构师"})

    def test_system_array_blocks(self):
        oa = gateway.anthropic_to_openai({
            "model": "gemma", "system": [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}],
            "messages": [],
        })
        self.assertEqual(oa["messages"][0]["content"], "AB")

    def test_user_text_message(self):
        oa = gateway.anthropic_to_openai({
            "model": "gemma", "messages": [{"role": "user", "content": "hi"}],
        })
        self.assertEqual(oa["messages"][0], {"role": "user", "content": "hi"})

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
        # 文本消息
        self.assertEqual(oa["messages"][0], {"role": "user", "content": "查一下"})
        # assistant：文本 + tool_calls
        asst = oa["messages"][1]
        self.assertEqual(asst["content"], "好的")
        self.assertEqual(asst["tool_calls"][0]["function"]["name"], "read")
        self.assertEqual(json.loads(asst["tool_calls"][0]["function"]["arguments"]), {"path": "/x"})
        # tool_result -> OpenAI tool role 消息
        self.assertEqual(oa["messages"][2], {"role": "tool", "tool_call_id": "t1", "content": "file content"})

    def test_tools_translated(self):
        oa = gateway.anthropic_to_openai({
            "model": "gemma", "messages": [],
            "tools": [{"name": "f", "description": "d", "input_schema": {"type": "object", "properties": {}}}],
        })
        self.assertEqual(oa["tools"][0]["type"], "function")
        self.assertEqual(oa["tools"][0]["function"]["name"], "f")
        self.assertEqual(oa["tools"][0]["function"]["parameters"], {"type": "object", "properties": {}})

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

    def test_end_discussion_tool_injected(self):
        # 网关向模型注入 end_discussion 工具（claude MCP 加载在 -p/隔离配置下不可靠，
        # 注入后凡经网关的模型必可见工具、可主动收敛）——tools 无该工具时自动追加
        oa = gateway.anthropic_to_openai({
            "model": "gemma", "messages": [],
            "tools": [{"name": "Bash", "description": "d", "input_schema": {"type": "object"}}],
        })
        names = [t["function"]["name"] for t in oa["tools"]]
        self.assertIn("end_discussion", names, "应自动注入 end_discussion 工具")
        ed = next(t["function"] for t in oa["tools"] if t["function"]["name"] == "end_discussion")
        self.assertIn("reason", ed["parameters"]["properties"])
        self.assertIn("reason", ed["parameters"]["required"])
        # 已存在则不重复注入
        oa2 = gateway.anthropic_to_openai({
            "model": "gemma", "messages": [],
            "tools": [
                {"name": "Bash", "description": "d", "input_schema": {"type": "object"}},
                {"name": "end_discussion", "description": "d", "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}}},
            ],
        })
        names2 = [t["function"]["name"] for t in oa2["tools"]]
        self.assertEqual(names2.count("end_discussion"), 1, "已有 end_discussion 时不应重复注入")
        # 无 tools 时不注入（纯文本请求不需要）
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
    def __init__(self, *a, **k):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    def stream(self, method, url, json=None, headers=None):
        return _FakeOllamaCtx([
            'data: {"choices":[{"delta":{"content":"你好"},"finish_reason":null}]}',
            'data: [DONE]',
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
                    _FakeRequest({"model": "gemma", "messages": [{"role": "user", "content": "hi"}], "stream": True}),
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
                    _FakeRequest({"model": "gemma", "messages": [{"role": "user", "content": "hi"}], "stream": True}),
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
                    _FakeRequest({"model": "gemma", "messages": [{"role": "user", "content": "hi"}], "stream": True}),
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


if __name__ == "__main__":
    unittest.main()
