"""内置 Anthropic↔Ollama 翻译网关。

为什么需要它：Claude Code CLI 说 Anthropic Messages API 格式（system / tool_use /
tool_result），而本地 Ollama 说 OpenAI Chat Completions 格式。两者协议不兼容。
把这一层翻译做进 app 本身，claude-code 类型的 agent 就能把 `base_url` 指向本网关
（如 http://127.0.0.1:8777），从而直连 Ollama 上已 pull 好的模型（gemma / deepseek 等），
**无需用户另起 litellm、也完全不改动 Ollama 自身配置**——本网关只是去读 Ollama 既有的
原生 /api/chat 端点（默认 http://localhost:11434/api/chat，可传 options.num_ctx 控制上下文长度）。

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

# Ollama 原生 /api/chat 端点（只读 Ollama，不改 Ollama 配置）；可被全局设置 ollama_base_url 或
# 环境变量 OLLAMA_BASE_URL 覆盖。走原生端点而非 OpenAI 兼容端点，因为只有原生端点接受
# options.num_ctx，才能由本网关控制上下文长度（KV cache 上限），不影响 Ollama GUI/全局设置。
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
# 注：tools / tool_choice 不再转发（2026-08-19）——工具统一走「prompt 文本 json + 编排层
# 兜底解析」，见 anthropic_to_openai 内的说明。
_OA_FORWARD_FIELDS = (
    "temperature", "top_p", "max_tokens", "stop",
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

    # 语言强制：本地模型（gemma 等）英文原生，调用 end_discussion 后极易回退到英文「待命」
    # 反射，忽略我们中文「请继续」指令。在 system 层注入简体中文强制指令——整场最权威、
    # 持续生效，能压住 claude-code 自带英文 system prompt 带来的语码回退（meta/待命类回复
    # 默认跟着 system 语言走）。本网关只服务本地模型，不影响真实 claude。
    LANG_MANDATE = (
        "【语言强制指令】你是一个用简体中文参与多 agent 辩论的本地模型。"
        "每轮你收到的输入是上一位 agent 的发言或系统指令，**这不是空输入**——"
        "即使消息看起来简短或只是工具回执，你也必须基于它继续参与讨论，"
        "绝不因『用户没发新文字』而进入待命/等待状态。"
        "无论任何情况、任何回复（包括总结发言、对工具回执的回应、待命/说明性文字），"
        "都必须且只能用简体中文输出，绝对禁止输出英文或其他语言。"
        "收到 end_discussion 工具回执后，若回执要求继续，你必须用中文继续参与讨论、"
        "推进观点或反驳，不要进入待命/结束状态，也不要输出任何英文。"
    )
    _injected = False
    for _m in oa_messages:
        if _m.get("role") == "system":
            _m["content"] = LANG_MANDATE + "\n\n" + (_m.get("content") or "")
            _injected = True
            break
    if not _injected:
        oa_messages.insert(0, {"role": "system", "content": LANG_MANDATE})

    oa: dict[str, Any] = {
        "model": body.get("model"),
        "messages": oa_messages,
        "stream": body.get("stream", False),
    }
    for f in _OA_FORWARD_FIELDS:
        if body.get(f) is not None:
            oa[f] = body[f]
    # 工具转发（2026-08-19 终版）：只转发「MCP 注册的 end_discussion」给模型——
    # 不转发 claude 内置工具（Bash/Read 等，避免模型乱调产生副作用），也不再手工注入 schema。
    # 链路：claude-code 通过项目级 .mcp.json（stdio）注册 end_discussion（terminate_signal_mcp.py）
    # → 请求 tools 里出现该工具（名 mcp__terminate-signal__end_discussion）→ 网关仅转发它（去前缀）
    # → Ollama 模型（gemma 等支持 function calling 的）看到干净名 end_discussion、产出真 tool_calls
    # → 网关翻译 tool_use 回 claude 时**还原** mcp__ 前缀 → claude 匹配 MCP server 执行返回**成功**
    # （不再 No such tool available）。不支持工具调用的模型（deepseek-r1 实测 tools=Y 仍 NONE）
    # 自然回落到 prompt 文本 json，由编排层 _extract_text_tool_call 兜底解析——两条路径并存。
    tools = body.get("tools")
    if tools:
        keep = [t for t in tools if "end_discussion" in (t.get("name") or "")]
        if keep:
            oa["tools"] = [{
                "type": "function",
                "function": {
                    "name": _strip_mcp_prefix(t.get("name")),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {}),
                },
            } for t in keep]
    return oa


def _strip_mcp_prefix(name: str) -> str:
    """mcp__terminate-signal__end_discussion → end_discussion（给模型侧的干净名）。

    claude 对 MCP 工具加 mcp__<server>__ 前缀，而本地模型（gemma）对长前缀名不稳定
    （实测会把原名改写成 end_discussion / anthropic:end_discussion 等变体），
    故转发 tools 时剥掉前缀，让模型始终看到稳定的简单名。
    """
    if name and name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return parts[2]
    return name


def _restore_mcp_name(name: str) -> str:
    """end_discussion（或任意 *:end_discussion 变体）→ mcp__terminate-signal__end_discussion。

    模型可能以干净名或带其他前缀的名调用，claude 侧只注册了 MCP 原名——
    翻译 tool_calls 时必须还原，否则 claude 报「No such tool available」。
    """
    if name and name.endswith("end_discussion") and not name.startswith("mcp__"):
        return "mcp__terminate-signal__end_discussion"
    return name


def _map_stop(reason: str | None) -> str:
    return {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "max_tokens",
        "content_filter": "end_turn",
    }.get(reason or "", "end_turn")


def _map_stop_ollama(done_reason: str | None) -> str:
    """Ollama 原生 /api/chat 的 done_reason → Anthropic stop_reason。"""
    return {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "max_tokens",
    }.get(done_reason or "", "end_turn")


def _ollama_tool_calls_to_openai(tcs: Any) -> list[dict] | None:
    """Ollama 原生 /api/chat 流式 tool_calls（无 index/id）转 OpenAI 增量格式，供 translator 复用。

    Ollama 原生 tool_calls 形如 [{function:{name, arguments:"{...增量json}"}]，arguments 是
    字符串增量；OpenAI 格式 translator 期望 {index, id, function:{name, arguments}}，二者兼容，
    这里补上 index/id 即可。
    """
    if not tcs:
        return None
    out = []
    for i, tc in enumerate(tcs):
        f = tc.get("function") or {}
        arg = f.get("arguments")
        # Ollama 原生 /api/chat 的 tool_calls.arguments 可能是字符串增量（旧模型），
        # 也可能是对象（gemma4 实测返回对象）。translator 需要字符串 partial_json，
        # 故对象统一 json.dumps 成字符串，避免下游把 dict 当 partial_json 出错
        if isinstance(arg, dict):
            arg = json.dumps(arg, ensure_ascii=False)
        elif arg is None:
            arg = ""
        out.append({
            "index": i,
            "id": "tool_" + str(i),
            "function": {"name": f.get("name", ""), "arguments": arg},
        })
    return out


def _openai_messages_to_ollama(messages: Any) -> list[dict]:
    """把 OpenAI 格式的 messages（tool_calls.arguments 为 JSON 字符串，content 可能为 null）

    转 Ollama 原生 /api/chat 格式（tool_calls.arguments 为对象、content 为空串而非 null）。
    Ollama 原生端点拒绝字符串型 arguments（实测 HTTP 400），且对 null content 不友好。
     debate 场景下 agent 历史含上一轮的 end_discussion tool_use（OpenAI 字符串格式），
    必须转换否则 Ollama 直接 400 拒绝整轮请求。
    """
    out = []
    for m in messages or []:
        nm = dict(m)
        if nm.get("content") is None:
            nm["content"] = ""
        tcs = m.get("tool_calls")
        if tcs:
            converted = []
            for tc in tcs:
                f = tc.get("function") or {}
                arg = f.get("arguments")
                if isinstance(arg, str):
                    try:
                        arg = json.loads(arg) if arg.strip() else {}
                    except Exception:
                        arg = {}
                converted.append({"function": {"name": f.get("name", ""), "arguments": arg}})
            nm["tool_calls"] = converted
        out.append(nm)
    return out


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
            # 思考块开着（_think_started 且未收口）时，正文到来先收口思考块再开 answer 块；
            # 必须校验 _think_started——纯 text（无 reasoning，如 gemma 对工具结果的短回复）
            # 时 text_started/cur_block==0 也会满足旧条件，误发 signature_delta+stop 并把
            # 后续文本切到 index 1，块序错乱 → claude 判「empty or malformed (HTTP 200)」
            if self._think_started and not self._think_closed and self.cur_block == 0:
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
                # 开工具块前必须先收口已开的 thinking / text 块——否则块未 stop 就 start
                # tool_use，违反 Anthropic 流式块序（块须 start→stop 配对、按序创建），
                # claude 会把响应判为「empty or malformed (HTTP 200)」。三种场景：
                # ① thinking 开着（先 reasoning 后直接 tool_calls，无 text）；② 正文 text 在
                # index 1（有 thinking，正文在 answer 块）；③ **纯 text 总结 + tool_calls**
                # （无 reasoning，正文 text 块在 index 0——「先总结发言再调用工具」的
                # 两步表达引导下 gemma 常见，实测 s_8dac9d7c 触发 malformed）。
                if self._think_started and not self._think_closed:
                    self._close_thinking(events)
                elif self._text_started:
                    events.append({"type": "content_block_stop",
                                   "index": 1 if self._think_started else 0})
                    self._text_started = False  # 已中途收口，finish() 不再重复 stop
                func = tc.get("function") or {}
                self.tool_blocks[idx] = {
                    "our": len(self.tool_blocks) + 2,
                    "id": tc.get("id") or ("tool_" + str(idx)),
                    "name": _restore_mcp_name(func.get("name", "")),
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
        # 只要本轮产了 tool_use 块，stop_reason 必须为 tool_use——即使 Ollama 原生端点把
        # done_reason 标成 stop（gemma4 实测：产出 tool_calls 但 done_reason="stop"）。否则
        # claude-code 会因 end_turn 而不回传工具结果，终止逻辑失效
        stop = "tool_use" if self.tool_blocks else (self.stop_reason or "end_turn")
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
        content.append({"type": "tool_use", "id": tc.get("id"), "name": _restore_mcp_name(func.get("name", "")), "input": inp})
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


def _non_stream_response_ollama(body: dict, resp: dict) -> dict:
    """把 Ollama 原生 /api/chat 非流式响应翻译为 Anthropic 非流式 Messages 响应。"""
    msg = resp.get("message") or {}
    content: list[dict] = []
    reasoning = msg.get("thinking") or ""
    text = msg.get("content") or ""
    if reasoning:
        content.append({"type": "thinking", "thinking": reasoning, "signature": _make_signature(body.get("model", ""), reasoning)})
        if text:
            content.append({"type": "text", "text": text})
    elif text:
        content.append({"type": "text", "text": text})
    for tc in msg.get("tool_calls") or []:
        f = tc.get("function") or {}
        try:
            inp = json.loads(f.get("arguments") or "{}")
        except Exception:
            inp = {}
        content.append({"type": "tool_use", "id": tc.get("id") or ("tool_" + os.urandom(4).hex()),
                        "name": _restore_mcp_name(f.get("name", "")), "input": inp})
    pe = resp.get("prompt_eval_count")
    ec = resp.get("eval_count")
    usage = resp.get("usage") or {}
    return {
        "id": "msg_" + os.urandom(8).hex(),
        "type": "message",
        "role": "assistant",
        "model": body.get("model"),
        "stop_reason": _map_stop_ollama(resp.get("done_reason")),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens") or pe or 0,
            "output_tokens": usage.get("completion_tokens") or ec or 0,
        },
        "content": content,
    }


def _resolve_ollama_think(settings: dict, project_path: str | None, sid: str | None) -> bool:
    """解析本次请求是否保留模型思考（thinking）块。

    默认取全局设置 ollama_think；若请求归属某个 app 会话（网关路由带 /gw/<pid>/<sid>），
    且该会话显式设置过 ollama_think，则按会话级值覆盖——实现「思考」的会话级开关，
    而非只能全局统一开关。会话未显式设置时回退全局默认。
    """
    think = bool(settings.get("ollama_think", True))
    if project_path and sid:
        try:
            from . import store as _store
            s = _store.read_session(project_path, sid)
            if s and s.get("ollama_think") is not None:
                think = bool(s["ollama_think"])
        except Exception:
            pass
    return think


async def handle_messages(req: Request, settings: dict, project_path: str | None = None, sid: str | None = None):
    """FastAPI 路由处理函数：POST /v1/messages 或 /gw/<pid>/<sid>/v1/messages。

    project_path / sid 由带会话路径的路由传入，用于按会话解析思考开关（会话级覆盖全局）。
    """
    body = await req.json()
    oa = anthropic_to_openai(body)
    url = _ollama_base_url(settings) + "/api/chat"

    auth = req.headers.get("x-api-key") or req.headers.get("authorization", "")
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = auth if auth.lower().startswith("bearer ") else "Bearer " + auth

    want_stream = bool(body.get("stream", True))

    # 走 Ollama 原生 /api/chat 端点（非 OpenAI 兼容端点）：只有原生端点接受 options.num_ctx，
    # 才能由本网关控制上下文长度（KV cache 上限），**不影响 Ollama GUI/全局配置**。实测 OpenAI
    # 兼容端点忽略 num_ctx/options.num_ctx，会把上下文拉满模型原生 128k（deepseek-r1:14b 单份
    # 吃掉 ~34GB）。默认 16384 对辩论场景足够，且把单份显存压到 ~12GB。
    num_ctx = int(settings.get("ollama_num_ctx") or 16384)
    ollama_body = {
        "model": oa.get("model"),
        "messages": _openai_messages_to_ollama(oa.get("messages")),
        "options": {"num_ctx": num_ctx},
        "stream": want_stream,
    }
    # 思考开关：ollama_think=False 时请求带 think:false（gemma 等推理模型支持的参数，
    # 实测生效：不再产出 thinking 块，输出从单轮数万 token 降到个位数）。默认 True 不传，
    # 保持对不支持 think 参数的模型（deepseek 等）无影响。
    # 优先取会话级开关（带 /gw/<pid>/<sid> 路径的路由会传入 project_path/sid），
    # 未设置则回退全局默认。
    if not _resolve_ollama_think(settings, project_path, sid):
        ollama_body["think"] = False
    if oa.get("tools"):
        # 真工具调用路径：把 anthropic_to_openai 过滤后的 end_discussion 工具定义透传给
        # Ollama 原生端点（其 tools 格式与 OpenAI 一致：{type:function, function:{name,
        # description, parameters}}）。漏传则 gemma 收不到工具定义、永不产出 tool_calls
        # （8e0eef0 改原生 /api/chat 时把请求体重写为手工 ollama_body，漏掉 tools，回归）。
        ollama_body["tools"] = oa["tools"]
    if oa.get("temperature") is not None:
        ollama_body["options"]["temperature"] = oa["temperature"]
    if oa.get("max_tokens") is not None:
        ollama_body["options"]["num_predict"] = oa["max_tokens"]

    if not want_stream:
        async with httpx.AsyncClient(timeout=600) as client:
            r = await client.post(url, json=ollama_body, headers=headers)
            r.raise_for_status()
            return JSONResponse(_non_stream_response_ollama(body, r.json()))

    async def gen():
        async with httpx.AsyncClient(timeout=600) as client:
            async with client.stream("POST", url, json=ollama_body, headers=headers) as r:
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
                    line = line.strip()
                    if not line:
                        continue
                    # Ollama 原生 /api/chat 流式为不带 data: 前缀的纯 JSON 行（OpenAI 兼容端点
                    # 才带 data: 前缀）。之前强制要求 data: 前缀会把原生端点整行跳过 → 空流
                    # （8e0eef0 的测试用带 data: 前缀的假数据，掩盖了这一真 bug）。兼容两种格式。
                    if line.startswith("data:"):
                        line = line[5:].strip()
                        if not line or line == "[DONE]":
                            continue
                    try:
                        chunk = json.loads(line)
                    except Exception:
                        continue
                    # Ollama 原生 /api/chat：增量在 chunk["message"]（content/thinking）；
                    # done 事件在顶层带 done/done_reason/prompt_eval_count/eval_count（无 usage 键）。
                    if chunk.get("done"):
                        pe = chunk.get("prompt_eval_count")
                        ec = chunk.get("eval_count")
                        if pe is not None or ec is not None:
                            translator.usage = {"prompt_tokens": pe or 0, "completion_tokens": ec or 0}
                        dr = chunk.get("done_reason")
                        if dr and not translator.stop_reason:
                            translator.stop_reason = _map_stop_ollama(dr)
                        continue
                    msg = chunk.get("message") or {}
                    delta = {
                        "content": msg.get("content") or "",
                        "reasoning": msg.get("thinking") or "",
                        "tool_calls": _ollama_tool_calls_to_openai(msg.get("tool_calls")),
                    }
                    if delta["content"] or delta["reasoning"] or delta["tool_calls"]:
                        got_any = True
                    for ev in translator.feed(delta):
                        yield _sse_event(ev)
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
