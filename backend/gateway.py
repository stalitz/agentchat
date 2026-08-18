"""内置 Anthropic↔Ollama 翻译网关。

为什么需要它：Claude Code CLI 说 Anthropic Messages API 格式（system / tool_use /
tool_result），而本地 Ollama 说 OpenAI Chat Completions 格式。两者协议不兼容。
把这一层翻译做进 app 本身，claude-code 类型的 agent 就能把 `base_url` 指向本网关
（如 http://127.0.0.1:8777），从而直连 Ollama 上已 pull 好的模型（gemma / deepseek 等），
**无需用户另起 litellm、也完全不改动 Ollama 自身配置**——本网关只是去读 Ollama 既有的
OpenAI 兼容端点（默认 http://localhost:11434/v1/chat/completions）。

路由：POST /v1/messages（与 Anthropic SDK 追加的路径一致：ANTHROPIC_BASE_URL + /v1/messages）。
"""
import base64
import hashlib
import json
import os
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

# Ollama 的 OpenAI 兼容端点（只读，不改 Ollama 配置）；可被全局设置 ollama_base_url 或
# 环境变量 OLLAMA_BASE_URL 覆盖。
# 思考/答案块的边界哨兵：仅用于**读取旧 transcript 时的兜底清洗**（executor._clean_sentinels），
# 新版网关已改为原生 Anthropic `thinking` 块（带 signature）+ `text` 块，claude-code 会原样落盘、
# 原生 claude / 外部工具可直接识别思考与答案，不再依赖哨兵。
THINK_SENT = "\x01THINK\x01"
ANSW_SENT = "\x01ANSW\x01"


def _make_signature(model: str, reasoning: str) -> str:
    """为本地网关生成的 `thinking` 块造一个可被 claude-code 透传/续接的占位签名（base64）。

    真实 Anthropic 的 signature 由模型私钥签发、resume 时需校验；本地 Ollama 模型无此能力，
    这里用 (model+reasoning) 的派生值拼成 base64 串，足够让 claude-code 正常存储与后续 resume
    （resume 时 thinking 块回传给我们网关，网关翻译给 Ollama 时直接忽略，不做校验）。
    """
    if not reasoning:
        reasoning = model or "agentchat"
    h = hashlib.sha256((model + "|" + reasoning).encode("utf-8")).digest()
    head = base64.b64encode(b'{"model":"' + model.encode("utf-8") + b'"}').decode("ascii")
    return head + "." + base64.b64encode(h).decode("ascii")

DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"

# OpenAI Chat Completions 接受的字段白名单（避免把 Anthropic 专有字段透传导致 Ollama 报错）。
# 仅保留 Ollama OpenAI 兼容端点普遍支持的字段，降低 400 风险。
_OA_FORWARD_FIELDS = (
    "temperature", "top_p", "max_tokens", "stop", "tools", "tool_choice",
)


def _ollama_base_url(settings: dict) -> str:
    u = (settings or {}).get("ollama_base_url") or os.environ.get("OLLAMA_BASE_URL") or DEFAULT_OLLAMA_BASE_URL
    return u.rstrip("/")


def _tool_result_content(c: Any) -> str:
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return str(c)


def anthropic_to_openai(body: dict) -> dict:
    """把 Anthropic Messages 请求体翻译为 OpenAI Chat Completions 请求体。"""
    oa_messages: list[dict] = []
    system = body.get("system")
    if system:
        if isinstance(system, list):
            system = "".join(b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text")
        oa_messages.append({"role": "system", "content": system})

    for m in body.get("messages", []):
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, str):
            oa_messages.append({"role": role, "content": content})
            continue
        texts: list[str] = []
        tool_calls: list[dict] = []
        for b in content or []:
            t = b.get("type")
            if t == "text":
                texts.append(b.get("text", ""))
            elif t == "tool_use":
                tool_calls.append({
                    "id": b.get("id"),
                    "type": "function",
                    "function": {
                        "name": b.get("name"),
                        "arguments": json.dumps(b.get("input", {}), ensure_ascii=False),
                    },
                })
            elif t == "tool_result":
                oa_messages.append({
                    "role": "tool",
                    "tool_call_id": b.get("tool_use_id"),
                    "content": _tool_result_content(b.get("content")),
                })
            # image 等其它块：Ollama 多数不支持，跳过
        if tool_calls:
            oa_messages.append({
                "role": role,
                "content": "".join(texts) if texts else None,
                "tool_calls": tool_calls,
            })
        else:
            oa_messages.append({"role": role, "content": "".join(texts)})

    oa: dict[str, Any] = {
        "model": body.get("model"),
        "messages": oa_messages,
        "stream": body.get("stream", False),
    }
    for f in _OA_FORWARD_FIELDS:
        if body.get(f) is not None:
            oa[f] = body[f]
    tools = body.get("tools")
    if tools:
        oa["tools"] = [{
            "type": "function",
            "function": {
                "name": t.get("name"),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            },
        } for t in tools]
    return oa


def _map_stop(reason: str | None) -> str:
    return {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "max_tokens",
        "content_filter": "end_turn",
    }.get(reason or "", "end_turn")


class _AnthropicTranslator:
    """把 OpenAI 流式 chunk 增量翻译为 Anthropic SSE 事件序列（可单测的纯逻辑）。"""

    def __init__(self, model: str = ""):
        self.model = model
        self.started = False
        self.text_started = False
        self.cur_block = 0  # 当前 text 块 index：0=reasoning（思考），1=answer（正文）
        self.tool_blocks: dict[int, dict] = {}  # openai index -> {our, id, name}
        self.stop_reason = None
        self.usage: dict = {}
        self._reasoning_acc = ""   # 累积 reasoning，用于生成 thinking 块签名
        self._think_closed = False  # 思考块是否已收口（signature_delta + stop）
        self._think_started = False  # 是否已开原生 thinking 块（用于 finish 区分思考/答案）
        self._text_started = False  # 正文 text 块是否已开

    def _start(self) -> list[dict]:
        self.started = True
        return [{
            "type": "message_start",
            "message": {
                "id": "msg_" + os.urandom(8).hex(),
                "type": "message",
                "role": "assistant",
                "model": self.model,
                "content": [],
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }]

    def _close_thinking(self, events: list[dict]):
        """思考块收口：补 signature_delta 并 content_block_stop（原生格式，claude-code 原样落盘）。"""
        if self._think_closed:
            return
        self._think_closed = True
        events.append({"type": "content_block_delta", "index": 0,
                       "delta": {"type": "signature_delta", "signature": _make_signature(self.model, self._reasoning_acc)}})
        events.append({"type": "content_block_stop", "index": 0})

    def feed(self, delta: dict) -> list[dict]:
        events: list[dict] = []
        if not self.started:
            events += self._start()
        # 思考模型（gemma 等）：reasoning 作为原生 Anthropic `thinking` 块（index 0，带 signature），
        # answer 作为 `text` 块（index 1）。原生 thinking 块会被 claude-code 原样落盘，原生 claude /
        # 外部工具可直接识别「思考」与「答案」，无需哨兵。
        reasoning = delta.get("reasoning") or delta.get("reasoning_content") or ""
        if reasoning:
            if not self.text_started:
                self.text_started = True
                self.cur_block = 0
                self._think_started = True
                events.append({"type": "content_block_start", "index": 0,
                               "content_block": {"type": "thinking", "thinking": ""}})
            events.append({"type": "content_block_delta", "index": 0,
                           "delta": {"type": "thinking_delta", "thinking": reasoning}})
            self._reasoning_acc += reasoning
        text = delta.get("content") or ""
        if text:
            if self.text_started and self.cur_block == 0 and not self._think_closed:
                # reasoning 块已开，先收口思考块，再开 answer 块（index 1）
                self._close_thinking(events)
                self.cur_block = 1
                self._text_started = True
                events.append({"type": "content_block_start", "index": 1,
                               "content_block": {"type": "text", "text": ""}})
            elif not self.text_started:
                self.text_started = True
                self.cur_block = 0
                self._text_started = True
                events.append({"type": "content_block_start", "index": 0,
                               "content_block": {"type": "text", "text": ""}})
            events.append({"type": "content_block_delta", "index": self.cur_block,
                           "delta": {"type": "text_delta", "text": text}})
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            if idx not in self.tool_blocks:
                func = tc.get("function") or {}
                self.tool_blocks[idx] = {
                    "our": len(self.tool_blocks) + 2,
                    "id": tc.get("id") or ("tool_" + str(idx)),
                    "name": func.get("name", ""),
                }
                events.append({"type": "content_block_start", "index": self.tool_blocks[idx]["our"],
                               "content_block": {"type": "tool_use", "id": self.tool_blocks[idx]["id"],
                                                 "name": self.tool_blocks[idx]["name"]}})
            func = tc.get("function") or {}
            if func.get("arguments"):
                events.append({"type": "content_block_delta", "index": self.tool_blocks[idx]["our"],
                               "delta": {"type": "input_json_delta", "partial_json": func["arguments"]}})
        return events

    def finish(self) -> list[dict]:
        events: list[dict] = []
        if not self.started:
            events += self._start()
        # 收尾：若思考块已开但未收口（只有思考、无答案），先收思考块
        if self._think_started and not self._think_closed:
            self._close_thinking(events)
        if self._text_started:
            events.append({"type": "content_block_stop", "index": 1 if self._think_started else 0})
        for idx in sorted(self.tool_blocks):
            events.append({"type": "content_block_stop", "index": self.tool_blocks[idx]["our"]})
        stop = self.stop_reason or ("tool_use" if self.tool_blocks else "end_turn")
        usage = self.usage or {}
        events.append({
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": usage.get("completion_tokens", 0)},
        })
        events.append({"type": "message_stop"})
        return events


def _sse_event(data: dict) -> str:
    return "event: " + data["type"] + "\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n"


def _non_stream_response(body: dict, oa_resp: dict) -> dict:
    """把 Ollama 非流式响应翻译为 Anthropic 非流式 Messages 响应。"""
    choice = (oa_resp.get("choices") or [{}])[0]
    msg = choice.get("message", {})
    content: list[dict] = []
    reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""
    text = msg.get("content") or ""
    # 思考模型：reasoning 作为原生 thinking 块（带 signature），answer 作为 text 块；
    # claude-code 原样落盘，原生 claude / 外部工具可直接识别「思考」与「答案」。
    if reasoning:
        content.append({"type": "thinking", "thinking": reasoning, "signature": _make_signature(body.get("model", ""), reasoning)})
        if text:
            content.append({"type": "text", "text": text})
    elif text:
        content.append({"type": "text", "text": text})
    for tc in msg.get("tool_calls") or []:
        func = tc.get("function") or {}
        try:
            inp = json.loads(func.get("arguments") or "{}")
        except Exception:
            inp = {}
        content.append({"type": "tool_use", "id": tc.get("id"), "name": func.get("name"), "input": inp})
    usage = oa_resp.get("usage") or {}
    return {
        "id": oa_resp.get("id", "msg_" + os.urandom(8).hex()),
        "type": "message",
        "role": "assistant",
        "model": body.get("model"),
        "stop_reason": _map_stop(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
        "content": content,
    }


async def handle_messages(req: Request, settings: dict):
    """FastAPI 路由处理函数：POST /v1/messages。"""
    body = await req.json()
    oa = anthropic_to_openai(body)
    url = _ollama_base_url(settings) + "/v1/chat/completions"

    auth = req.headers.get("x-api-key") or req.headers.get("authorization", "")
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = auth if auth.lower().startswith("bearer ") else "Bearer " + auth

    want_stream = bool(body.get("stream", True))

    if not want_stream:
        async with httpx.AsyncClient(timeout=600) as client:
            r = await client.post(url, json=oa, headers=headers)
            r.raise_for_status()
            return JSONResponse(_non_stream_response(body, r.json()))

    oa["stream"] = True

    async def gen():
        async with httpx.AsyncClient(timeout=600) as client:
            async with client.stream("POST", url, json=oa, headers=headers) as r:
                if r.status_code >= 400:
                    err = await r.aread()
                    try:
                        em = json.loads(err).get("error", {}).get("message") or err.decode("utf-8", "replace")
                    except Exception:
                        em = err.decode("utf-8", "replace")
                    yield _sse_event({"type": "error", "error": {"type": "api_error", "message": "Ollama gateway error: " + str(em)[:500]}})
                    return
                translator = _AnthropicTranslator(model=body.get("model", ""))
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                    except Exception:
                        continue
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    if chunk.get("usage"):
                        translator.usage = chunk["usage"]
                    for ev in translator.feed(delta):
                        yield _sse_event(ev)
                    if choice.get("finish_reason"):
                        translator.stop_reason = _map_stop(choice["finish_reason"])
                        break
                for ev in translator.finish():
                    yield _sse_event(ev)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
