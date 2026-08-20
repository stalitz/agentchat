"""MCP stdio 服务：向 claude-code 暴露 `end_discussion(reason)` 工具。

背景（需求文档 FR5.3a / 开发文档 §6）：终止讨论的可靠机制——agent 通过**结构化工具调用**
表达终止意图，比纯字符串 TERMINATE 可靠。

**本服务是「无状态确认通道」**：`tools/call` 一律返回成功确认文本，**不做任何判定**——
最小轮数 gate 与两阶段确认全部由编排层（orchestrator）执行：其内存状态机已完整
（未达轮次→rejected、无提议→proposed、同方重复→repeat、对方→confirmed，每次调用落
工具日志 `tools/<sid>.jsonl`）。判定不落在本服务的理由：本服务由每个 claude 进程按项目
`.mcp.json` 拉起、无法得知当前会话/轮次；且多会话并发时共享状态文件会串场（早期版本曾读
共享文件 `TERMINATE_STATE_FILE` 做 gate，已在「多会话并行」改造中废除——编排层状态机是
权威，这里仅提供工具调用通道，让模型以结构化 `tool_use` 表达终止意图，而不是输出无法被
识别的文字）。

**传输协议**：MCP stdio 用 LSP 风格消息（`Content-Length: N\\r\\n\\r\\n` + JSON body），
非裸 JSONL。纯 stdlib 实现，无第三方依赖。
"""
import json
import sys

TOOL_NAME = "end_discussion"
TOOL_DESC = (
    "结束当前多 agent 讨论。只有当本场讨论已充分进行（至少两轮完整往返）且你真正认为"
    "双方已达成一致、可以收尾时才调用；否则不要调用。reason 请用一两句话说明达成的一致结论。"
)
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string", "description": "双方达成的一致结论（一两句话）"},
    },
    "required": ["reason"],
}

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "terminate-signal", "version": "0.2.0"}


def _handle_call(params: dict) -> dict:
    args = params.get("arguments") or {}
    name = params.get("name", "")
    if name != TOOL_NAME:
        return {
            "content": [{"type": "text", "text": f"未知工具: {name}"}],
            "isError": True,
        }
    reason = str(args.get("reason", "") or "")
    # 无状态确认：真实判定（轮次 gate / 两阶段确认）由编排层执行并落工具日志。
    # 回执刻意用强指令口吻：本地弱模型（gemma）易把「调用 end_discussion」误解为「对话结束」，
    # 需用明确中文命令它「继续、勿待命、勿输出英文」，否则会回退英文 standby 反射。
    # 注意「两个条件按顺序检查」的框定：gemma 曾把未生效归因于「对方未确认」，实际是被
    # 轮次门槛拒绝——必须点名「未达最小轮数是首要/最常见原因、与对方确认无关」。
    return {
        "content": [{"type": "text", "text": (
            f"【重要·讨论尚未结束】你的 end_discussion 调用已收到（理由：{reason}），"
            "但**终止尚未生效**。终止必须同时满足两个条件，且**按顺序检查**："
            "① 本场讨论已达最小轮数（双方各至少发言两次）；② 对方 agent 也调用 "
            "end_discussion 确认。**最常见的未生效原因是①未满足**——若你是在讨论早期"
            "发起调用，系统会直接拒绝，**原因是轮数不足，与对方是否确认无关**；"
            "只有轮数达标后，才会进入②等待对方确认。"
            "请用简体中文继续参与讨论、推进你的观点或反驳对方，不要进入待命/结束状态，"
            "也不要输出英文。"
        )}],
        "isError": False,
    }


def _read_frame() -> str | None:
    """从 stdin 读一帧 JSON 消息，返回 JSON 字符串；EOF 返回 None。

    支持两种传输（实测 claude-code 2.1.212 用 **JSONL**——每行一个 JSON，
    首字符即 `{`；标准 MCP 客户端用 Content-Length 帧）：
    - 首行以 `{` 开头 → JSONL，整行即一条消息；
    - 否则按 Content-Length 帧解析（header 行 + 空行 + body）。
    此前只支持 Content-Length 帧，导致 claude 的 JSONL 被当 header 行解析失败、
    表现为「server 从不通信」（实测多轮排查的根因）。
    """
    first = sys.stdin.buffer.readline()
    if not first:
        return None  # EOF
    if first.startswith(b"{"):
        # JSONL：整行即一条 JSON-RPC 消息
        return first.decode("utf-8", "replace")

    headers: dict[str, str] = {}

    def _parse_header(line: bytes) -> None:
        try:
            key, _, value = line.decode("utf-8").partition(":")
            headers[key.strip().lower()] = value.strip()
        except Exception:
            pass

    _parse_header(first)
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None  # EOF
        if line in (b"\r\n", b"\n"):
            break
        _parse_header(line)
    length = int(headers.get("content-length", "0") or 0)
    if length <= 0:
        return ""
    body = sys.stdin.buffer.read(length)
    try:
        return body.decode("utf-8")
    except Exception:
        return ""


def _write_frame(obj: dict) -> None:
    # JSONL 输出（claude-code 2.1.x 的 stdio MCP 读响应按行解析，与请求侧对称）；
    # 标准 MCP 客户端也接受裸 JSON 行（部分客户端按行容错）。
    body = json.dumps(obj, ensure_ascii=False)
    sys.stdout.buffer.write(body.encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def main() -> int:
    while True:
        raw = _read_frame()
        if raw is None:
            return 0
        if not raw.strip():
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        method = msg.get("method", "")
        msg_id = msg.get("id")
        # 通知类（notifications/initialized 等）：无 id，不响应
        if msg_id is None:
            continue
        if method == "initialize":
            _write_frame({
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                },
            })
        elif method == "tools/list":
            _write_frame({
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "tools": [{
                        "name": TOOL_NAME,
                        "description": TOOL_DESC,
                        "inputSchema": INPUT_SCHEMA,
                    }],
                },
            })
        elif method == "tools/call":
            _write_frame({"jsonrpc": "2.0", "id": msg_id, "result": _handle_call(msg.get("params") or {})})
        elif method == "ping":
            _write_frame({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        else:
            _write_frame({
                "jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            })


if __name__ == "__main__":
    sys.exit(main())
