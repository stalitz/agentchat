"""MCP stdio 服务：向 claude-code 暴露 `end_discussion(reason)` 工具。

背景（需求文档 FR5.3a / 开发文档 §6）：终止讨论的可靠机制——agent 通过**结构化工具调用**
表达终止意图，比纯字符串 TERMINATE 可靠（gate 结构性、不达标返回失败、agent 自行放弃）。

**判定数据来源**：本服务是无状态进程（每轮 claude 进程启动时被拉起一次），轮次/提议状态由
编排层（orchestrator）写在共享状态文件（环境变量 `TERMINATE_STATE_FILE` 指定，默认
`/tmp/agentchat_terminate_state.json`）：
```json
{ "round_i": 3, "agent_count": 2, "current_agent": "Claude Code (Gemma)",
  "proposed_by": "Claude Code (Gemma)", "reason": "双方同意结论X" }
```
编排层在每次启动 claude 子进程**之前**写该文件（app 单会话串行，无竞态）；`current_agent`
= 本轮发言的 agent（本进程对应的 claude 的调用者身份），`proposed_by` = 已提议终止的一方
（编排层在流式看到本轮 tool_use 事件后更新，下一轮对方读到）。

**判定规则（tools/call handler）**：
- `round_i < 2*agent_count - 1`（未完成两轮完整往返）→ `isError: true`：「终止被拒：至少需
  2 轮（双方各发言 2 次）」，agent 看到失败自行放弃调用、继续讨论；
- 达标且无提议 → 记提议（返回「已记录你的中止提议，等待对方确认」；**注意**：proposed_by
  由编排层在本轮流式看到 tool_use 事件后写入状态文件，下一轮对方读到）；
- 达标且 `proposed_by == current_agent`（调用者自己已提过）→ 仍等对方；
- 达标且 `proposed_by` 是对方（≠ current_agent）→ 确认达成（返回成功，讨论终止）。

**传输协议**：MCP stdio 用 LSP 风格消息（`Content-Length: N\\r\\n\\r\\n` + JSON body），
非裸 JSONL。纯 stdlib 实现，无第三方依赖。
"""
import json
import os
import sys
from pathlib import Path

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
SERVER_INFO = {"name": "terminate-signal", "version": "0.1.0"}


def _state_file() -> Path:
    p = os.environ.get("TERMINATE_STATE_FILE") or "/tmp/agentchat_terminate_state.json"
    return Path(p)


def _read_state() -> dict:
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _decide(state: dict, reason: str) -> tuple[bool, str]:
    """返回 (is_error, message)。调用者身份 = 状态文件的 current_agent（本轮 claude 的发起者）。"""
    round_i = int(state.get("round_i", 0) or 0)
    agent_count = max(1, int(state.get("agent_count", 1) or 1))
    floor = 2 * agent_count - 1  # 至少两轮完整往返（双方各发言 2 次）
    if round_i < floor:
        return True, (
            f"终止被拒：讨论尚未达到最少轮数（当前第 {round_i + 1} 轮，至少需 {floor + 1} 轮，"
            "即双方各发言 2 次）。请继续讨论。"
        )
    proposed_by = state.get("proposed_by")
    current_agent = state.get("current_agent") or ""
    if not proposed_by:
        # 编排层会在本轮流式收到 tool_use 后把 proposed_by 写入状态文件
        return False, f"已记录你的中止提议（理由：{reason}），等待对方确认后终止。"
    if proposed_by == current_agent:
        return False, f"你已提过中止提议（理由：{reason}），仍在等待对方确认。"
    # 对方已提议，本方确认 → 终止达成
    return False, "对方已确认你的中止提议，讨论终止。"


def _handle_call(params: dict) -> dict:
    args = params.get("arguments") or {}
    name = params.get("name", "")
    if name != TOOL_NAME:
        return {
            "content": [{"type": "text", "text": f"未知工具: {name}"}],
            "isError": True,
        }
    reason = str(args.get("reason", "") or "")
    is_error, msg = _decide(_read_state(), reason)
    return {"content": [{"type": "text", "text": msg}], "isError": is_error}


def _read_frame() -> str | None:
    """从 stdin 读一个 Content-Length 帧，返回 JSON 字符串；EOF 返回 None。"""
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None  # EOF
        if line in (b"\r\n", b"\n"):
            break
        try:
            key, _, value = line.decode("utf-8").partition(":")
            headers[key.strip().lower()] = value.strip()
        except Exception:
            pass
    length = int(headers.get("content-length", "0") or 0)
    if length <= 0:
        return ""
    body = sys.stdin.buffer.read(length)
    try:
        return body.decode("utf-8")
    except Exception:
        return ""


def _write_frame(obj: dict) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    out = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8") + body
    sys.stdout.buffer.write(out)
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
