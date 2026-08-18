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
    # 注入 end_discussion 工具：claude-code 的 MCP 加载在 -p 非交互/隔离配置下不可靠
    # （实测 2.1.212：.mcp.json 服务器 Pending approval、claude mcp add 后 Failed to connect，
    # 请求 tools 里 25 个全是内置工具、无 end_discussion → 模型看不到工具、无法主动收敛）。
    # 改为在网关层直接向模型注入工具定义——凡经网关的模型（gemma/r1 等）必可见，
    # 调用后网关把 tool_calls 翻译为 tool_use 事件，编排层据此走两阶段终止状态机；
    # 不依赖 claude 的 MCP 机制（terminate_signal_mcp.py 保留为备用，不再必需）。
    if tools and not any((t.get("name") or "") == "end_discussion" for t in tools):
        oa["tools"].append({
            "type": "function",
            "function": {
                "name": "end_discussion",
                "description": (
                    "结束当前多 agent 讨论。只有当本场讨论已充分进行（至少两轮完整往返）且你真正认为"
                    "双方已达成一致、可以收尾时才调用；否则不要调用。reason 请用一两句话说明达成的一致结论。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string", "description": "双方达成的一致结论（一两句话）"},
                    },
                    "required": ["reason"],
                },
            },
        })
    return oa


def _map_stop(reason: str | None) -> str:
    return {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "max_tokens",
        "content_filter": "end_turn",
    }.get(reason or "", "end_turn")


def _estimate_input_tokens(body: dict) -> int:
    """估算请求输入 token 数。

    message_start 阶段 Ollama 尚未返回 usage（其真实 prompt_tokens 在流末 chunk 才有），
    先用文本长度/4 粗估，避免 transcript 里 input_tokens 恒为 0；message_delta 会用
    Ollama 真实 usage 覆盖。
    """
    total = 0
    sys = body.get("system")
    if isinstance(sys, str):
        total += len(sys)
    elif isinstance(sys, list):
        total += sum(len(b.get("text", "") or "") for b in sys if isinstance(b, dict))
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        elif isinstance(c, list):
            for b in c:
                if not isinstance(b, dict):
                    continue
                total += len(b.get("text", "") or "")
                if b.get("type") == "tool_use":
                    total += len(json.dumps(b.get("input", {}), ensure_ascii=False))
    return max(1, total // 4)


class _AnthropicTranslator:
    """把 OpenAI 流式 chunk 增量翻译为 Anthropic SSE 事件序列（可单测的纯逻辑）。"""

    def __init__(self, model: str = "", est_input_tokens: int = 0):
        self.model = model
        self.est_input_tokens = est_input_tokens
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
                # input_tokens 先用请求文本估算（Ollama 真实值流末才返回）；
                # output_tokens 由 finish() 的 message_delta 用 Ollama usage 回填
                "usage": {"input_tokens": self.est_input_tokens, "output_tokens": 0},
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
            # 用 Ollama 真实 usage 回填（流式请求已带 stream_options.include_usage）：
            # 否则 transcript 里 output_tokens 恒为 0，UI 工具显示「输出 0」
            "usage": {
                "input_tokens": usage.get("prompt_tokens") or self.est_input_tokens or 0,
                "output_tokens": usage.get("completion_tokens", 0),
            },
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
    # 请求 Ollama 在流末 chunk 返回真实 usage（OpenAI 标准字段，Ollama 已支持），
    # 使 transcript 里 input/output tokens 非 0（问题：UI 工具显示「输入 0 输出 0」）。
    oa["stream_options"] = {"include_usage": True}

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
                translator = _AnthropicTranslator(model=body.get("model", ""), est_input_tokens=_estimate_input_tokens(body))
                # 立即发 message_start（不等 Ollama 首个 chunk）：claude 进程的 SDK 对「HTTP 200
                # 已建立但长时间无首个 SSE 事件」有内部超时（约 60s），gemma 等本地模型冷启 /
                # 慢 prefill 时首 chunk 可达 60s+，网关若等首 chunk 才发 message_start，claude
                # 会误判「API Error: empty or malformed (HTTP 200)」并退出（实测复现）。
                # 先发 message_start 让 claude 确认流已开始，Ollama 慢则由 600s 超时兜底；
                # 若最终真空流，下方空流检测仍会产出 SSE error 事件。
                for ev in translator._start():
                    yield _sse_event(ev)
                got_any = False  # 是否收到过任何内容增量（空流检测）
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except Exception:
                        continue
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    if chunk.get("usage"):
                        translator.usage = chunk["usage"]
                    if delta.get("content") or delta.get("reasoning") or delta.get("reasoning_content") or delta.get("tool_calls"):
                        got_any = True
                    for ev in translator.feed(delta):
                        yield _sse_event(ev)
                    if choice.get("finish_reason") and not translator.stop_reason:
                        translator.stop_reason = _map_stop(choice["finish_reason"])
                if not got_any and not (translator.usage or {}).get("completion_tokens"):
                    # HTTP 200 但空流/畸形流：claude-code 会把这种情况渲染成普通文本
                    # 「API Error: API returned an empty or malformed response ...」混进正文，
                    # 随后被编排器当正常发言喂给下一个 agent。这里主动产出 SSE error，
                    # 让 claude 走错误通道（result is_error），编排器据此中止会话而非继续讨论。
                    yield _sse_event({"type": "error", "error": {"type": "api_error",
                        "message": "Ollama gateway error: empty or malformed stream (HTTP 200), no content received"}})
                    return
                for ev in translator.finish():
                    yield _sse_event(ev)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
