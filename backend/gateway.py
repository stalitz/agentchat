"""内置 Anthropic↔Ollama 翻译网关。

为什么需要它：Claude Code CLI 说 Anthropic Messages API 格式（system / tool_use /
tool_result），而本地 Ollama 说 Responses API 格式（/v1/responses）。两者协议不兼容。
把这一层翻译做进 app 本身，claude-code 类型的 agent 就能把 `base_url` 指向本网关
（如 http://127.0.0.1:8777），从而直连 Ollama 上已 pull 好的模型（gemma / deepseek 等），
**无需用户另起 litellm、也完全不改动 Ollama 自身配置**。

2026-09-18 改造（用户裁定方案 A）：本地模型统一走 Ollama 的 `/v1/responses`
（Responses API）而非旧的 `/api/chat`（chat completions）。原因：gemma 等本地模型在
Responses API 端点适配好（会重试工具、正确命名、像 codex 路径一样稳定），而在 /api/chat
端点表现差（猜错工具名、不重试、被英文 system 带偏）。codex 路径本就直连 /v1/responses，
现在 claude 路径对齐它，claude+gemma 不再「瘸」。翻译：anthropic_to_responses 把
Messages 请求转成 /v1/responses 请求（input 数组 + tools + reasoning 开关），
_ResponsesStreamAdapter 把 Responses 流式 SSE 翻回 Anthropic Messages SSE（thinking /
text / tool_use 块序、signature、stop_reason、usage）给 claude CLI。

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

# 本地模型经 Ollama（默认 127.0.0.1:11434），可由全局设置 ollama_base_url 或环境变量
# OLLAMA_BASE_URL 覆盖。网关把 Anthropic Messages 翻成 Ollama /v1/responses（Responses API），
# 由 Responses 请求体里的 options.num_ctx 控制上下文长度（KV cache 上限），不影响 Ollama GUI/全局设置。
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
        # 工具转发（2026-09-18 调整）：
        # ① 受控工具 end_discussion / ask_human_input（terminate-signal MCP server 下）始终转发（去前缀），
        #    由编排层驱动终止 / 人工介入状态机；
        # ② claude 内置工具（Bash/Read/Edit/Glob/Grep/Write/WebFetch/WebSearch/...）**不带 mcp__ 前缀**，
        #    由 claude CLI 本地执行——原样转发给本地模型，让其能看到并产出 tool_use
        #    （claude CLI 收到后匹配本地工具执行）。之前误剥全部非受控工具 → gemma 看不到工具定义只能猜名
        #    （实测猜 shell:execute 失败、claude 回 No such tool available）；
        # ③ 第三方 MCP 工具（带 mcp__ 前缀、非受控）仍剥，避免本地模型乱调产生副作用；
        # ④ 交互型工具（AskUserQuestion/EnterPlanMode/ExitPlanMode）本 app 无 TTY 通道会卡死，也剥
        #    （人工介入改用受控工具 ask_human_input）。
        keep = []
        for t in tools:
            name = t.get("name") or ""
            if any(k in name for k in _GUARDED_TOOL_NAMES):
                keep.append(t)            # 受控工具：始终转发
                continue
            if name.startswith("mcp__"):
                continue                  # 第三方 MCP 工具：剥
            if name in _CLAUDE_INTERACTIVE_TOOLS:
                continue                  # 交互工具无 TTY 通道：剥
            keep.append(t)                # claude 内置工具：放行
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


# claude 内置的交互型工具：本 app 用无 TTY 子进程拉起 claude，
# 这些工具需要真人实时回答 / 进入 plan 模式，无通道会卡死，必须剥（人工介入改用受控工具 ask_human_input）。
_CLAUDE_INTERACTIVE_TOOLS = frozenset({
    "AskUserQuestion", "EnterPlanMode", "ExitPlanMode",
})


def anthropic_to_responses(body: dict) -> dict:
    """把 Anthropic Messages 请求体翻译为 Ollama /v1/responses（Responses API）请求体。

    复用 anthropic_to_openai 的工具过滤 + 中文强制 system（LANG_MANDATE）注入，再把
    OpenAI 格式的 messages 转成 Responses API 的 `input` 数组。关键差异（已对 Ollama
    0.33.3 实测）：
    - system → 顶层 `instructions` 字段；
    - 历史里的 assistant tool_use → **独立顶层项** `{type:"function_call", call_id,
      name, arguments}`（Ollama 不允许把它嵌进 `role:assistant` 的 content，会 400）；
    - tool_result → 独立顶层项 `{type:"function_call_output", call_id, output}`；
    - tools 格式为扁平 `{type:"function", name, description, parameters}`（非 OpenAI 的
      嵌套 `function:{...}`）；
    - 广告给模型的工具名用干净名（去 mcp__ 前缀），与历史 function_call 名一致，
      回程由 _ResponsesStreamAdapter / _restore_mcp_name 还原 mcp__ 前缀给 claude。
    """
    oa = anthropic_to_openai(body)
    oa_messages = list(oa.get("messages") or [])
    instructions = None
    if oa_messages and oa_messages[0].get("role") == "system":
        instructions = oa_messages[0].get("content") or ""
        oa_messages = oa_messages[1:]

    input_items: list[dict] = []
    for m in oa_messages:
        role = m.get("role")
        content = m.get("content")
        if role == "tool":
            input_items.append({
                "type": "function_call_output",
                "call_id": m.get("tool_call_id"),
                "output": m.get("content") or "",
            })
            continue
        if role == "assistant":
            text = content if isinstance(content, str) else "".join(
                b.get("text", "") for b in (content or [])
                if isinstance(b, dict) and b.get("type") == "text")
            if text:
                input_items.append({"role": "assistant",
                                     "content": [{"type": "output_text", "text": text}]})
            for tc in (m.get("tool_calls") or []):
                f = tc.get("function") or {}
                args = f.get("arguments")
                if not isinstance(args, str):
                    args = json.dumps(args or {}, ensure_ascii=False)
                input_items.append({
                    "type": "function_call",
                    "call_id": tc.get("id"),
                    "name": _strip_mcp_prefix(f.get("name", "")),
                    "arguments": args,
                })
            continue
        # role == "user"
        if isinstance(content, str):
            input_items.append({"role": "user", "content": content})
        else:
            parts = [b.get("text", "") for b in (content or [])
                     if isinstance(b, dict) and b.get("type") == "text"]
            input_items.append({"role": "user", "content": "".join(parts)})

    resp: dict[str, Any] = {
        "model": oa.get("model"),
        "input": input_items,
        # Anthropic Messages 的 stream 缺省为 **false**（缺字段=非流式）。若这里按 True 兜底，
        # claude 的非流式回退请求（不带 stream）会被当成流式转给 Ollama，回包是 SSE、
        # 而 handle_messages 又走 r.json() 解析 → 必错。必须与 handle_messages 的
        # want_stream 用同一默认值（false）。
        "stream": bool(body.get("stream", False)),
    }
    if instructions:
        resp["instructions"] = instructions
    tools = oa.get("tools")
    if tools:
        resp["tools"] = [{
            "type": "function",
            "name": t.get("function", {}).get("name"),
            "description": t.get("function", {}).get("description", ""),
            "parameters": t.get("function", {}).get("parameters", {}),
        } for t in tools]
    if oa.get("temperature") is not None:
        resp["temperature"] = oa["temperature"]
    if oa.get("max_tokens") is not None:
        resp["max_output_tokens"] = oa["max_tokens"]
    return resp


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


# 受控工具名（terminate-signal MCP server 提供）。HITL 工具 2026-09-20 由 request_human_input
# 改名 ask_human_input（与原内建 request_user_input 撞名 → codex 归一化后被门禁），旧名作为
# 兼容别名继续认下：本地小模型可能沿用提示词/历史会话里的旧名产出 tool_use。
_GUARDED_TOOL_NAMES = ("end_discussion", "ask_human_input", "request_human_input")
# 旧名 → 现用名（MCP server 只注册了新名，还原时必须归一到新名才匹配得上）
_GUARDED_TOOL_ALIASES = {"request_human_input": "ask_human_input"}


def _restore_mcp_name(name: str) -> str:
    """end_discussion / ask_human_input（或带前缀变体）→ mcp__terminate-signal__<基名>。

    模型可能以干净名或带其他前缀的名调用，claude 侧只注册了 MCP 原名——
    翻译 tool_calls 时必须还原，否则 claude 报「No such tool available」。
    两个受控工具同属 terminate-signal server，故前缀统一为 mcp__terminate-signal__；
    旧名 request_human_input 归一到 ask_human_input（server 已不再注册旧名）。
    """
    if name and not name.startswith("mcp__"):
        base = name.split("__")[-1] if "__" in name else name
        base = base.split(":")[-1]  # 兼容 anthropic: 前缀变体
        if base in _GUARDED_TOOL_NAMES:
            base = _GUARDED_TOOL_ALIASES.get(base, base)
            return f"mcp__terminate-signal__{base}"
    return name


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
    """把 OpenAI 流式 chunk 增量翻译为 Anthropic SSE 事件序列（可单测的纯逻辑）。

    块序策略：**单一开块 + 顺序 index 分配**（`_open_idx` / `_next_idx`）。
    Anthropic 流式协议要求块严格串行（start → delta… → stop 配对，任一时刻只有一个块
    处于打开状态），而上游事件顺序**不保证「思考在前、正文在后」**：gemma4:26b 实测
    会先吐一小段正文（分隔符 "---"），随后才进入思考（reasoning 在 Ollama 侧是
    out-of-band 事件，网关只在 `reasoning_summary_text.done` 拿到全文），最后才吐正式
    答案。于是真实事件序是 text → thinking → text。

    因此**不能把块 index 写死**（旧实现 thinking=0 / text=1 并假设思考先到）：迟到的
    思考会被塞进已打开的 text 块（同一 index 先 text_delta 再 thinking_delta），claude
    SDK 判流非法、在收到该 delta 时掐断流并触发非流式回退 → 报
    「API Error: API returned an empty or malformed response (HTTP 200)」(2026-09-18
    抓包实证，见 probe/app-repro/raw/tap.jsonl)。改为按事件类型切块：正文 → 思考 →
    正文 会拿到 0/1/2 三个块，各自 start/stop 配对，index 连续无空洞。
    """

    def __init__(self, model: str = "", est_input_tokens: int = 0):
        self.model = model
        self.est_input_tokens = est_input_tokens
        self.started = False
        self.tool_blocks: dict[int, dict] = {}  # openai index -> {our, id, name}
        self.stop_reason = None
        self.usage: dict = {}
        self._reasoning_acc = ""   # 当前思考块的累积文本，用于生成 thinking 块签名
        # —— 动态块管理：同一时刻只有一个开块，index 按 0,1,2… 顺序分配 ——
        self._next_idx = 0
        self._open_idx: int | None = None
        self._open_type: str | None = None   # thinking / text / tool
        self._open_tool: int | None = None   # 打开中的工具块对应的上游 index

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

    def _alloc(self) -> int:
        """按顺序分配一个新的块 index（0,1,2…，无空洞）。"""
        idx = self._next_idx
        self._next_idx += 1
        return idx

    def _close_open(self, events: list[dict]):
        """收口当前打开的块（thinking 需补 signature_delta）。无开块时空操作。"""
        if self._open_idx is None:
            return
        if self._open_type == "thinking":
            # 原生 thinking 块收口：signature_delta + content_block_stop（claude-code 原样落盘）
            events.append({"type": "content_block_delta", "index": self._open_idx,
                           "delta": {"type": "signature_delta",
                                     "signature": _make_signature(self.model, self._reasoning_acc)}})
        events.append({"type": "content_block_stop", "index": self._open_idx})
        self._open_idx = None
        self._open_type = None
        self._open_tool = None

    def _open_block(self, events: list[dict], kind: str, content_block: dict) -> int:
        """先收口当前块，再分配新 index 并 content_block_start。"""
        self._close_open(events)
        idx = self._alloc()
        events.append({"type": "content_block_start", "index": idx,
                       "content_block": content_block})
        self._open_idx = idx
        self._open_type = kind
        return idx

    def feed(self, delta: dict) -> list[dict]:
        events: list[dict] = []
        if not self.started:
            events += self._start()
        # 思考模型（gemma 等）：reasoning 作为原生 Anthropic `thinking` 块（带 signature）。
        # 已开着思考块就续写；否则（思考迟到：正文块已开）**先收口当前块再新开**——绝不把
        # thinking_delta 写进正文块的 index（同号混排是非法流，claude 会掐断并回退）。
        reasoning = delta.get("reasoning") or delta.get("reasoning_content") or ""
        if reasoning:
            if self._open_type != "thinking":
                self._open_block(events, "thinking", {"type": "thinking", "thinking": ""})
                self._reasoning_acc = ""  # 新思考块重新累积签名语料
            events.append({"type": "content_block_delta", "index": self._open_idx,
                           "delta": {"type": "thinking_delta", "thinking": reasoning}})
            self._reasoning_acc += reasoning
        text = delta.get("content") or ""
        if text:
            # 正文：思考块（或已收口的正文块）开着时切新块。注意两种历史坑：
            # ① 纯 text（无 reasoning，如 gemma 对工具结果的短回复）必须留在 index 0，不得
            #    误发 signature_delta 把后续文本切走；② 正文 → 思考 → 正文 交替时，第三段
            #    正文必须新开块，不能接着往已 stop 的块里写 delta。
            if self._open_type != "text":
                self._open_block(events, "text", {"type": "text", "text": ""})
            events.append({"type": "content_block_delta", "index": self._open_idx,
                           "delta": {"type": "text_delta", "text": text}})
        for tc in delta.get("tool_calls") or []:
            key = tc.get("index", 0)
            func = tc.get("function") or {}
            if key not in self.tool_blocks:
                # 开工具块前必须先收口已开的 thinking / text 块（由 _open_block 完成）——
                # 块未 stop 就 start tool_use 违反块序，claude 判 malformed。三种场景：
                # ① thinking 开着（先 reasoning 后直接 tool_calls，无 text）；② 正文在
                # index 1（有 thinking）；③ **纯 text 总结 + tool_calls**（无 reasoning，
                # 「先总结发言再调用工具」两步表达引导下 gemma 常见，实测 s_8dac9d7c 触发）。
                tid = tc.get("id") or ("tool_" + str(key))
                tname = _restore_mcp_name(func.get("name", ""))
                our = self._open_block(events, "tool", {"type": "tool_use", "id": tid,
                                                        "name": tname})
                self.tool_blocks[key] = {"our": our, "id": tid, "name": tname}
                self._open_tool = key
            elif not (self._open_type == "tool" and self._open_tool == key):
                # 该工具块此前已被收口（异常顺序：工具参数流中间插入了别的块）——向已 stop
                # 的块写 delta 是非法 SSE；`function_call_arguments.done` 会在收尾时补全，
                # 故丢弃这段增量而不产出非法事件。
                continue
            if func.get("arguments"):
                events.append({"type": "content_block_delta", "index": self.tool_blocks[key]["our"],
                               "delta": {"type": "input_json_delta", "partial_json": func["arguments"]}})
        return events

    def finish(self) -> list[dict]:
        events: list[dict] = []
        if not self.started:
            events += self._start()
        # 收尾：收口唯一开着的块（thinking 补 signature_delta；已收口的块不会重复 stop）
        self._close_open(events)
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


def _non_stream_response_responses(body: dict, resp: dict) -> dict:
    """把 Ollama /v1/responses 非流式响应翻译为 Anthropic 非流式 Messages 响应。"""
    output = resp.get("output") or []
    content: list[dict] = []
    reasoning = ""
    for item in output:
        t = item.get("type")
        if t == "reasoning":
            for s in (item.get("summary") or []):
                reasoning += s.get("text", "")
        elif t == "message":
            for c in (item.get("content") or []):
                if c.get("type") == "output_text":
                    content.append({"type": "text", "text": c.get("text", "")})
        elif t == "function_call":
            try:
                inp = json.loads(item.get("arguments") or "{}")
            except Exception:
                inp = {}
            content.append({"type": "tool_use", "id": item.get("call_id") or item.get("id"),
                            "name": _restore_mcp_name(item.get("name", "")), "input": inp})
    final: list[dict] = []
    if reasoning:
        final.append({"type": "thinking", "thinking": reasoning,
                      "signature": _make_signature(body.get("model", ""), reasoning)})
    final += [c for c in content if c["type"] == "text"]
    final += [c for c in content if c["type"] == "tool_use"]
    usage = resp.get("usage") or {}
    return {
        "id": "msg_" + os.urandom(8).hex(),
        "type": "message", "role": "assistant", "model": body.get("model"),
        "stop_reason": "tool_use" if any(c["type"] == "tool_use" for c in final) else "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": usage.get("input_tokens", 0),
                  "output_tokens": usage.get("output_tokens", 0)},
        "content": final,
    }


class _ResponsesStreamAdapter:
    """把 Ollama /v1/responses 流式 SSE 事件翻译为 Anthropic Messages SSE 事件序列。

    复用 _AnthropicTranslator（原生 thinking 块带 signature、块序动态分配、stop_reason
    按是否产出 tool_use 决定）。Ollama 实测：reasoning 先在
    `reasoning_summary_text.delta` 逐片给出（网关忽略）、末尾 `reasoning_summary_text.done`
    给全文（网关据此产 thinking_delta）；text 在 `output_text.delta`；tool 参数在
    `function_call_arguments.delta`+`done`。
    """

    def __init__(self, model: str = "", est_input_tokens: int = 0):
        self.t = _AnthropicTranslator(model=model, est_input_tokens=est_input_tokens)
        self._tool_name: dict[str, str] = {}
        self._tool_callid: dict[str, str] = {}
        self._tool_seen: set[str] = set()
        self._usage: dict = {}

    def start(self) -> list[dict]:
        return self.t._start()

    def handle(self, ev: dict) -> list[dict]:
        out: list[dict] = []
        typ = ev.get("type")
        if typ == "response.output_item.added":
            item = ev.get("item") or {}
            if item.get("type") == "function_call":
                self._tool_name[item.get("id")] = item.get("name", "")
                self._tool_callid[item.get("id")] = item.get("call_id") or item.get("id")
        elif typ == "response.reasoning_summary_text.done":
            txt = ev.get("text") or ""
            if txt:
                out += self.t.feed({"reasoning": txt, "content": "", "tool_calls": None})
        elif typ == "response.output_text.delta":
            txt = ev.get("delta") or ""
            if txt:
                out += self.t.feed({"content": txt, "reasoning": "", "tool_calls": None})
        elif typ == "response.function_call_arguments.delta":
            iid = ev.get("item_id")
            piece = ev.get("delta") or ""
            if piece:
                name = self._tool_name.get(iid, "")
                cid = self._tool_callid.get(iid, iid)
                idx = ev.get("output_index", 0)
                out += self.t.feed({"content": "", "reasoning": "", "tool_calls": [
                    {"index": idx, "id": cid, "function": {"name": name, "arguments": piece}}]})
                self._tool_seen.add(iid)
        elif typ == "response.function_call_arguments.done":
            iid = ev.get("item_id")
            if iid not in self._tool_seen:
                args = ev.get("arguments") or ""
                if args:
                    name = self._tool_name.get(iid, "")
                    cid = self._tool_callid.get(iid, iid)
                    idx = ev.get("output_index", 0)
                    out += self.t.feed({"content": "", "reasoning": "", "tool_calls": [
                        {"index": idx, "id": cid, "function": {"name": name, "arguments": args}}]})
        elif typ == "response.completed":
            self._usage = (ev.get("response") or {}).get("usage") or {}
        return out

    def finish(self) -> list[dict]:
        if self._usage:
            self.t.usage = {
                "prompt_tokens": self._usage.get("input_tokens"),
                "completion_tokens": self._usage.get("output_tokens"),
            }
        return self.t.finish()


def _resolve_think(settings: dict, project_path: str | None, sid: str | None,
                  think_override: bool | None = None) -> bool:
    """解析本次请求是否保留模型思考（thinking）块。

    优先级（三级开关）：think_override（编排层已按 会话>agent>全局 算好的最终值）
    > 会话级（网关路由带 /gw/<pid>/<sid> 且会话显式设置过 think/ollama_think）
    > 全局（settings.think）。
    think_override 由编排层传入时直接采用（最权威），否则走会话>全局回退。
    """
    if think_override is not None:
        return bool(think_override)
    think = bool(settings.get("think", True))
    if project_path and sid:
        try:
            from . import store as _store
            s = _store.read_session(project_path, sid)
            if s:
                sv = s.get("think")
                if sv is None:
                    sv = s.get("ollama_think")  # 兼容 #104 旧字段
                if sv is not None:
                    think = bool(sv)
        except Exception:
            pass
    return think


_THINK_CAP_CACHE: dict[tuple[str, str], bool] = {}


async def _ollama_supports_thinking(settings: dict, model: str | None) -> bool:
    """该模型是否声明支持 thinking（Ollama `/api/show` 的 capabilities）。

    只用于决定「生效值 True 时要不要显式传 `think: true`」：不支持的模型保持旧行为（不传），
    免得给不认这个参数的模型添乱。结果按 (base_url, model) 进程内缓存（模型能力不会变）；
    查询失败一律视为「不支持」（绝不因能力探测失败让网关请求出错）。
    """
    if not model:
        return False
    base = _ollama_base_url(settings)
    key = (base, str(model))
    if key in _THINK_CAP_CACHE:
        return _THINK_CAP_CACHE[key]
    ok = False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(base + "/api/show", json={"model": model})
            if r.status_code < 400:
                caps = (r.json() or {}).get("capabilities") or []
                ok = "thinking" in caps
    except Exception:
        ok = False
    _THINK_CAP_CACHE[key] = ok
    return ok


async def handle_messages(req: Request, settings: dict, project_path: str | None = None, sid: str | None = None,
                        think_override: bool | None = None):
    """FastAPI 路由处理函数：POST /v1/messages 或 /gw/<pid>/<sid>/v1/messages。

    本地模型统一走 Ollama /v1/responses（Responses API），由 _ResponsesStreamAdapter 把
    流式 SSE 翻回 Anthropic Messages SSE。project_path / sid 由带会话路径的路由传入，
    用于按会话解析思考开关（会话级覆盖全局）。think_override 由编排层按
    会话>agent>全局 算好并随 URL 查询传入，存在时优先采用。
    """
    body = await req.json()
    resp_body = anthropic_to_responses(body)
    url = _ollama_base_url(settings) + "/v1/responses"

    auth = req.headers.get("x-api-key") or req.headers.get("authorization", "")
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = auth if auth.lower().startswith("bearer ") else "Bearer " + auth

    # Anthropic Messages 的 stream 缺省为 false（缺字段 = 非流式）。这里旧实现按 True 兜底，
    # 于是 claude SDK 的非流式回退请求（body 不带 stream）被当流式处理、回了 138KB SSE，
    # claude 拿 SSE 当 JSON 解析失败 → 报「API returned an empty or malformed response
    # (HTTP 200)」(2026-09-18 抓包实证)。缺省必须为 false。
    want_stream = bool(body.get("stream", False))

    # 上下文长度：Responses API 仍接受 Ollama 原生 options.num_ctx（实测 0.33.3 不 400），
    # 控制 KV cache 上限，避免把上下文拉满模型原生 128k（deepseek-r1:14b 单份吃掉 ~34GB）。
    # 默认 16384 对辩论场景足够，且把单份显存压到 ~12GB。
    num_ctx = int(settings.get("ollama_num_ctx") or 16384)
    resp_body["options"] = {"num_ctx": num_ctx}
    # 思考开关：生效值 False → reasoning.effort=none（不产思考块）；True 且模型支持 →
    # reasoning.effort=low（与 codex 路径一致，Ollama 默认也思考，显式 low 更稳）。
    # 优先级：think_override（编排层算好）> 会话级（带 /gw/<pid>/<sid> 路径）> 全局默认。
    if not _resolve_think(settings, project_path, sid, think_override):
        resp_body["reasoning"] = {"effort": "none"}
    elif await _ollama_supports_thinking(settings, resp_body.get("model")):
        resp_body["reasoning"] = {"effort": "low"}

    if not want_stream:
        async with httpx.AsyncClient(timeout=600) as client:
            r = await client.post(url, json=resp_body, headers=headers)
            if r.status_code >= 400:
                try:
                    em = (r.json() or {}).get("error", {}).get("message") or r.text
                except Exception:
                    em = r.text
                return JSONResponse(status_code=502,
                                    content={"error": {"type": "api_error",
                                            "message": "Ollama gateway error: " + str(em)[:500]}})
            return JSONResponse(_non_stream_response_responses(body, r.json()))

    async def gen():
        async with httpx.AsyncClient(timeout=600) as client:
            async with client.stream("POST", url, json=resp_body, headers=headers) as r:
                if r.status_code >= 400:
                    err = await r.aread()
                    try:
                        em = json.loads(err).get("error", {}).get("message") or err.decode("utf-8", "replace")
                    except Exception:
                        em = err.decode("utf-8", "replace")
                    yield _sse_event({"type": "error", "error": {"type": "api_error", "message": "Ollama gateway error: " + str(em)[:500]}})
                    return
                adapter = _ResponsesStreamAdapter(model=body.get("model", ""), est_input_tokens=_estimate_input_tokens(body))
                # 立即发 message_start（不等 Ollama 首个 chunk）：claude 进程 SDK 对「HTTP 200
                # 已建立但长时间无首个 SSE 事件」有内部超时（约 60s），gemma 冷启/慢 prefill 时
                # 首 chunk 可达 60s+，网关若等首 chunk 才发 message_start，claude 会误判
                # 「API Error: empty or malformed (HTTP 200)」并退出（实测复现）。
                # 先发 message_start 让 claude 确认流已开始；若最终真空流，下方空流检测仍会产出 SSE error。
                for ev in adapter.start():
                    yield _sse_event(ev)

                def dispatch(payload: str) -> list:
                    try:
                        ev = json.loads(payload)
                    except Exception:
                        return []
                    return [_sse_event(a) for a in adapter.handle(ev)]

                got_any = False  # 是否收到过任何内容增量（空流检测）
                async for line in r.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("event:"):
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                        if not line or line == "[DONE]":
                            continue
                    # 去 data: 前缀后的负载，或 Ollama /v1/responses 原生不带前缀的纯 JSON 行。
                    # 每个事件一行（Ollama 每行一个完整 JSON 对象），逐行解析；不跨行累积
                    # 成单一 payload（否则多事件被拼成非法 JSON 整体解析失败 → 空流误判）。
                    if line and line != "[DONE]":
                        for e in dispatch(line):
                            yield e
                            got_any = True
                if not got_any:
                    # HTTP 200 但空流/畸形流：claude-code 会把这种情况渲染成普通文本
                    # 「API Error: API returned an empty or malformed response ...」混进正文，
                    # 随后被编排器当正常发言喂给下一个 agent。这里主动产出 SSE error，
                    # 让 claude 走错误通道（result is_error），编排器据此中止会话而非继续讨论。
                    yield _sse_event({"type": "error", "error": {"type": "api_error",
                        "message": "Ollama gateway error: empty or malformed stream (HTTP 200), no content received"}})
                    return
                for ev in adapter.finish():
                    yield _sse_event(ev)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
