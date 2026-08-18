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


if __name__ == "__main__":
    unittest.main()
