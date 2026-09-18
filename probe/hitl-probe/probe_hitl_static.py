"""request_human_input（HITL 人工介入）可用性分层探针 —— 静态层，不跑模型、不烧 token。

目的：用户实测「跳高训练计划」会话里没有一个 agent 调用 request_human_input，
需要确认「工具是否真可用」。本脚本沿调用链自底向上逐层取证：

  L1 MCP 服务层   terminate_signal_mcp.py 是否真的提供 request_human_input（tools/list + tools/call）
  L2 网关可见性   claude 路径：Anthropic tools → Ollama tools，该工具是否被转发（干净名）
  L2b 名还原      模型以干净名调用时，回程是否还原成 mcp__terminate-signal__request_human_input
  L3 codex 上报   codex 两条通路（app-server 实时 / exec+rollout）是否把该 MCP 调用
                  产成 ("tool_use", {...}) 事件上报编排层 —— 编排层的 HITL 分支只认这个事件
  L4 编排层识别   _is_human_input 是否识别（含 MCP 原名）＋文本 json 兜底是否可用
  L5 提示词注入   真实会话 s_317d5554 的 prompt log 里是否每轮都注入了 HITL 指令

输出：probe/hitl-probe/evidence/static.txt（stdout 同步打印）。
"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend import executor, gateway, orchestrator  # noqa: E402
from backend.executor import AgentRuntime  # noqa: E402
from backend.models import AgentConfig  # noqa: E402

OUT = Path(__file__).resolve().parent / "evidence"
OUT.mkdir(parents=True, exist_ok=True)
_lines: list[str] = []


def log(s: str = "") -> None:
    print(s)
    _lines.append(s)


def rule(title: str) -> None:
    log()
    log("=" * 78)
    log(title)
    log("=" * 78)


# --------------------------------------------------------------------------
# L1 · MCP 服务层：terminate_signal_mcp.py 真握手
# --------------------------------------------------------------------------
def l1_mcp_server() -> bool:
    rule("L1 · MCP 服务层（terminate_signal_mcp.py，claude 经项目 .mcp.json 拉起 / codex 经 -c mcp_servers.* 拉起）")
    script = str(ROOT / "backend" / "terminate_signal_mcp.py")
    # claude-code 2.1.x 的 stdio MCP 走 JSONL（每行一条 JSON-RPC），与生产一致
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "request_human_input",
                    "arguments": {"question": "用哪套方案？", "options": ["入门", "强化"]}}},
    ]
    payload = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in reqs)
    p = subprocess.run([sys.executable, script], input=payload, capture_output=True,
                       text=True, timeout=30)
    resp = [json.loads(x) for x in p.stdout.splitlines() if x.strip().startswith("{")]
    tools = (resp[1].get("result") or {}).get("tools") if len(resp) > 1 else []
    names = [t.get("name") for t in tools or []]
    log(f"tools/list 返回 ({len(names)}): {names}")
    ok_list = "request_human_input" in names
    log(f"  → 工具定义存在: {ok_list}")
    if ok_list:
        sch = [t for t in tools if t.get("name") == "request_human_input"][0]
        log(f"  → schema: {json.dumps(sch, ensure_ascii=False)}")
    call = (resp[2].get("result") or {}) if len(resp) > 2 else {}
    txt = "".join(c.get("text", "") for c in call.get("content") or [])
    log(f"tools/call 回执: {txt}")
    log(f"  → isError={call.get('isError')}")
    ok = ok_list and call.get("isError") is False
    log(f"【L1 结论】MCP server 提供并正常回执 request_human_input: {ok}")
    return ok


# --------------------------------------------------------------------------
# L2 · claude 路径：网关是否把该工具转发给模型
# --------------------------------------------------------------------------
def _fake_claude_tools() -> list[dict]:
    """claude CLI 真实请求里的 tools 片段（受控 MCP + 内置工具 + 第三方 MCP + 交互型）。"""
    return [
        {"name": "mcp__terminate-signal__end_discussion", "description": "结束讨论",
         "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}},
                          "required": ["reason"]}},
        {"name": "mcp__terminate-signal__request_human_input", "description": "请求人工介入",
         "input_schema": {"type": "object", "properties": {"question": {"type": "string"}},
                          "required": ["question"]}},
        {"name": "Bash", "description": "执行命令", "input_schema": {"type": "object"}},
        {"name": "Read", "description": "读文件", "input_schema": {"type": "object"}},
        {"name": "AskUserQuestion", "description": "交互型", "input_schema": {"type": "object"}},
        {"name": "mcp__other-server__whatever", "description": "第三方 MCP",
         "input_schema": {"type": "object"}},
    ]


def l2_gateway_visibility() -> bool:
    rule("L2 · claude 路径：网关工具转发（Anthropic tools → Ollama /v1/responses tools）")
    body = {
        "model": "gemma4:26b",
        "system": "你是探针。",
        "messages": [{"role": "user", "content": "问用户选哪套"}],
        "tools": _fake_claude_tools(),
        "stream": True,
        "max_tokens": 512,
    }
    oa = gateway.anthropic_to_responses(body)
    names = []
    for t in oa.get("tools") or []:
        names.append(t.get("name") if "name" in t else (t.get("function") or {}).get("name"))
    log(f"转发给本地模型的工具名: {names}")
    ok_rhi = "request_human_input" in names
    ok_ed = "end_discussion" in names
    ok_builtin = "Bash" in names and "Read" in names
    ok_strip = "AskUserQuestion" not in names and "mcp__other-server__whatever" not in names
    log(f"  → request_human_input 转发: {ok_rhi}")
    log(f"  → end_discussion 转发:      {ok_ed}")
    log(f"  → claude 内置工具放行:       {ok_builtin}")
    log(f"  → 交互型/第三方 MCP 已剥:    {ok_strip}")
    log(f"  → 干净名（去 mcp__ 前缀）:   {not any(n and n.startswith('mcp__') for n in names)}")
    ok = ok_rhi and ok_ed and ok_builtin and ok_strip
    log(f"【L2 结论】本地模型能看到 request_human_input: {ok}")
    return ok


def l2b_name_restore() -> bool:
    rule("L2b · 回程名还原：模型以干净名调用 → 是否还原成 claude 注册的 MCP 原名")
    cases = ["request_human_input", "anthropic:request_human_input",
             "end_discussion", "Bash", "Read"]
    ok = True
    for n in cases:
        restored = gateway._restore_mcp_name(n)
        log(f"  {n:35s} → {restored}")
    r1 = gateway._restore_mcp_name("request_human_input") == "mcp__terminate-signal__request_human_input"
    r2 = gateway._restore_mcp_name("end_discussion") == "mcp__terminate-signal__end_discussion"
    r3 = gateway._restore_mcp_name("Bash") == "Bash"
    ok = r1 and r2 and r3
    log(f"【L2b 结论】受控工具名还原正确（内置工具不被加前缀）: {ok}")
    return ok


# --------------------------------------------------------------------------
# L3 · codex 上报：两条通路是否把 request_human_input 上报给编排层
# --------------------------------------------------------------------------
def _mcp_item(itype: str, tool: str, args: dict) -> dict:
    return {"type": itype, "id": "call_9", "server": "terminate-signal",
            "tool": tool, "status": "completed", "arguments": args}


async def _as_collect(notifications) -> list:
    """驱动 app-server 主循环（跳握手），喂通知收集事件 —— 与 tests/test_codex_appserver.py 同法。"""
    rt = AgentRuntime(AgentConfig(name="C", type="codex"))

    async def _noop_ensure(cwd):
        return None

    async def _noop_drain():
        return None

    rt._ensure_appserver = _noop_ensure
    rt._as_drain_stale = _noop_drain
    rt._appserver_thread_id = "th_1"
    rt._as_queue = asyncio.Queue()
    for o in notifications:
        await rt._as_queue.put(o)
    return [ev async for ev in rt._run_codex_appserver("hi", "/tmp", 5)]


async def l3_codex_report() -> bool:
    rule("L3 · codex 上报：MCP 调用是否被转成 tool_use 事件给编排层（HITL 状态机唯一的输入）")
    args = {"question": "你每周能练几次？", "options": ["1-2 次", "3-4 次"]}
    results = {}

    # app-server 实时通路（本 app 默认路径）
    for itype in ("mcpToolCall", "mcp_tool_call"):
        evs = await _as_collect([
            {"method": "item/completed", "params": {"item": _mcp_item(itype, "request_human_input", args)}},
            {"method": "turn/completed", "params": {}},
        ])
        tools = [e for e in evs if e[0] == "tool_use"]
        results[f"appserver/{itype}"] = tools
        log(f"app-server 实时 item/completed type={itype:14s} → tool_use={tools}")

    # exec / rollout 通路
    for itype in ("mcpToolCall", "mcp_tool_call"):
        ev = {"type": "item.completed", "item": _mcp_item(itype, "request_human_input", args)}
        got = executor._parse_codex_line(json.dumps(ev), None)
        tools = [e for e in got if e[0] == "tool_use"]
        results[f"exec/{itype}"] = tools
        log(f"exec/rollout _parse_codex_line type={itype:14s} → tool_use={tools}")

    # 对照：同一位置的 end_discussion（已知可用）
    evs = await _as_collect([
        {"method": "item/completed",
         "params": {"item": _mcp_item("mcpToolCall", "end_discussion", {"reason": "一致"})}},
        {"method": "turn/completed", "params": {}},
    ])
    ed_tools = [e for e in evs if e[0] == "tool_use"]
    log(f"对照组 end_discussion(app-server)                   → tool_use={ed_tools}")
    ed_ok = len(ed_tools) == 1

    rhi_ok = any(len(v) == 1 for k, v in results.items())
    log()
    log(f"  → end_discussion 能被上报: {ed_ok}（对照组，正常）")
    log(f"  → request_human_input 能被上报: {rhi_ok}")
    if ed_ok and not rhi_ok:
        log("  ⚠️ 同一分支只放行 end_discussion：codex 调了 request_human_input 也会被吞掉，")
        log("     编排层收不到 tool_use ⇒ 不会暂停、前端也不会弹人工介入。这是**真的断了**，不是模型不调。")
    return rhi_ok


# --------------------------------------------------------------------------
# L4 · 编排层识别
# --------------------------------------------------------------------------
def l4_orchestrator() -> bool:
    rule("L4 · 编排层： EVENT 到了之后能否被识别为人工介入（含文本 json 兜底）")
    checks = {
        "干净名": "request_human_input",
        "MCP 原名": "mcp__terminate-signal__request_human_input",
        "anthropic: 变体": "anthropic:request_human_input",
    }
    ok = True
    for label, name in checks.items():
        r = orchestrator._is_human_input(name)
        log(f"  _is_human_input({name!r}) = {r}   [{label}]")
        ok = ok and r
    txt = '```json\n{"tool":"request_human_input","question":"你选哪套？","options":["A","B"]}\n```'
    parsed = orchestrator._extract_text_tool_call(txt)
    log(f"  文本 json 兜底 _extract_text_tool_call → {parsed}")
    ok = ok and bool(parsed) and parsed.get("name") == "request_human_input"
    log(f"【L4 结论】编排层能识别 request_human_input（含文本兜底）: {ok}")
    return ok


# --------------------------------------------------------------------------
# L5 · 提示词注入（真实会话 prompt log）
# --------------------------------------------------------------------------
def l5_prompt_injection(session: str) -> bool:
    rule("L5 · 提示词注入：真实会话发给 agent 的 prompt 里是否带了 HITL 指令")
    p = Path("/Users/stalitz/www/aitest/agent-probes/.agentchat/prompts") / f"{session}.jsonl"
    if not p.exists():
        log(f"  prompt log 不存在：{p}（跳过）")
        return True
    total = 0
    hit = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        o = json.loads(line)
        prompt = o.get("prompt") or ""
        total += 1
        if "request_human_input" in prompt:
            hit += 1
    log(f"  {p.name}: 共 {total} 条 prompt，含 request_human_input 指令 {hit} 条")
    ok = total > 0 and hit == total
    log(f"【L5 结论】每轮都注入 HITL 指令: {ok}")
    return ok


async def main() -> int:
    r1 = l1_mcp_server()
    r2 = l2_gateway_visibility()
    r2b = l2b_name_restore()
    r3 = await l3_codex_report()
    r4 = l4_orchestrator()
    r5 = l5_prompt_injection("s_317d5554")

    rule("总表")
    table = [
        ("L1 MCP server 提供工具 + 正常回执", r1),
        ("L2 网关把工具转发给本地模型（claude 路径）", r2),
        ("L2b 回程名还原（干净名 → MCP 原名）", r2b),
        ("L3 codex 把该 MCP 调用上报编排层", r3),
        ("L4 编排层识别（含文本 json 兜底）", r4),
        ("L5 每轮 prompt 注入 HITL 指令", r5),
    ]
    for name, ok in table:
        log(f"  {'✅' if ok else '❌'}  {name}")
    (OUT / "static.txt").write_text("\n".join(_lines) + "\n", encoding="utf-8")
    log()
    log(f"证据文件：{OUT / 'static.txt'}")
    return 0 if all(ok for _, ok in table) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
