#!/usr/bin/env python3
"""端到端探针：在隔离 app 实例里真跑一场辩论，验证 ask_human_input 是否能被 agent 调起、
编排层是否暂停等人、人工回答后能否续跑。

会拉起真实 CLI（claude-code / codex）+ 本机免费模型 gemma4:26b（Ollama），
**不接任何远程付费模型**。隔离实例，不碰用户 8777 上的数据与会话。

用法：
  AGENTCHAT_WS=ws://127.0.0.1:8790/ws AGENTCHAT_HTTP=http://127.0.0.1:8790 \
    .venv/bin/python probe/hitl-probe/drive_hitl.py

留痕：
  probe/hitl-probe/raw/e2e.events.jsonl   WebSocket 原始事件
  probe/hitl-probe/evidence/e2e.txt      本次运行摘要
  probe/hitl-probe/work/.agentchat/...   app 原生会话/工具/prompt 存储
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
import websockets

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw"
EV = HERE / "evidence"
RAW.mkdir(parents=True, exist_ok=True)
EV.mkdir(parents=True, exist_ok=True)

URL = os.environ.get("AGENTCHAT_WS", "ws://127.0.0.1:8790/ws")
HTTP_BASE = os.environ.get("AGENTCHAT_HTTP", "http://127.0.0.1:8790")
PROJ = os.environ.get("PROBE_PROJECT", "pr_hitl")
TEAM = os.environ.get("PROBE_TEAM", "tm_hitl")
TOTAL_WAIT = float(os.environ.get("PROBE_TOTAL_WAIT", "420"))
def _load_answers() -> list[str]:
    """按收到提问的顺序逐个提交的自然语言回答（验证「不限次数」：每次调工具就走一次回复）。

    环境变量 PROBE_ANSWERS 为 JSON 数组；最后一条会被重复使用，并且语义上引导 agent
    收尾（「没有补充了，请总结结论交给对方」）。
    """
    raw = os.environ.get("PROBE_ANSWERS")
    if raw:
        try:
            v = json.loads(raw)
            if isinstance(v, list) and v:
                return [str(x) for x in v]
        except ValueError:
            pass
    return [
        "预算 1.5 万以内，尽量别动硬装。",
        "家里有一只猫，会抓沙发；朝北采光差，希望显得亮一点。",
        "就这些了，没有别的补充。请把这几轮问答归纳成一份完整的客厅改造方案，交给另一位讨论者。",
    ]


ANSWERS = _load_answers()
ANSWER = ANSWERS[0]
TASK = os.environ.get("PROBE_TASK", (
    "我们要一起给用户家的客厅（20 平米、朝北、采光差）做一份软装改造方案。"
    "第一步：请你调用 ask_human_input 工具向用户（人）提问，问题写在 question 里，"
    "先问清楚预算上限。记住：必须在工具里问，不要在正文里问对方 agent；"
    "用户会用自然语言自由回复，你无需提供候选项。"
    "拿到回答后，如果还有必须确认的关键信息，可以继续再次调用 ask_human_input 追问（次数不限）；"
    "确认够了就把结论写成完整方案，交给另一位讨论者继续。"
))


def brief(ev: dict) -> str:
    t = ev.get("type")
    bits = [str(t)]
    for k in ("agent", "name", "msg", "question", "tool", "text"):
        v = ev.get(k)
        if v:
            bits.append(f"{k}={str(v)[:80]}")
    return " | ".join(bits)


async def main() -> int:
    events: list[dict] = []
    log_lines: list[str] = []

    def echo(s: str) -> None:
        print(s, flush=True)
        log_lines.append(s)

    async with httpx.AsyncClient(base_url=HTTP_BASE, timeout=30) as hc:
        r = await hc.post(f"/api/sessions/{PROJ}",
                          json={"title": "probe：HITL 人工介入", "team_id": TEAM})
        r.raise_for_status()
        sid = (r.json().get("session") or {}).get("id")
        # 等价 UI 勾选「思考」：会话级开启 think（覆盖全局默认，确保探针跑带思考）
        try:
            await hc.post(f"/api/session/{PROJ}/{sid}/think", json={"think": True})
            echo("think = on (会话级)")
        except Exception as e:
            echo(f"think 开启失败（非致命）：{e}")
    echo(f"session = {sid}")

    hitl_seen: list[dict] = []
    tool_uses: list[dict] = []
    interim_turns: list[dict] = []
    answered = False
    finished = False
    n_answered = 0

    async with websockets.connect(URL, max_size=None, ping_interval=0, ping_timeout=600) as ws:
        await ws.send(json.dumps({"action": "start", "project_id": PROJ, "team_id": TEAM,
                                  "session_id": sid, "max_rounds": int(os.environ.get("PROBE_ROUNDS", "2")),
                                  "task": TASK}, ensure_ascii=False))
        echo(f"start: {TASK}")
        t0 = time.time()
        try:
            while time.time() - t0 < TOTAL_WAIT:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    if finished:
                        break
                    continue
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                events.append(ev)
                echo(brief(ev))
                t = ev.get("type")
                if t == "human_input_required":
                    hitl_seen.append(ev)
                    echo(f"  >>> HITL 请求 #{len(hitl_seen)}（agent={ev.get('agent')}）: {ev.get('question')}")
                    # 按序发下一条回答；回答用完后重复最后一条（语义上引导收尾）
                    ans = ANSWERS[min(n_answered, len(ANSWERS) - 1)]
                    n_answered += 1
                    await asyncio.sleep(0.5)
                    await ws.send(json.dumps({"action": "human_input", "text": ans,
                                              "session_id": sid}, ensure_ascii=False))
                    answered = True
                    echo(f"  >>> 已提交第 {n_answered} 条人工回答：{ans}")
                elif t in ("tool_use", "tool"):
                    tool_uses.append(ev)
                elif t == "turn_end" and ev.get("interim"):
                    # 交互过程发言：app 侧标 interim，不转述给其他 agent
                    interim_turns.append(ev)
                    echo(f"  >>> interim（交互过程，不转述）agent={ev.get('agent')}")
                elif t == "run_end":
                    finished = True
                    await asyncio.sleep(1.0)
                if finished and (time.time() - t0) > TOTAL_WAIT * 0.5:
                    break
        finally:
            # 先落盘再汇总：任何异常都不丢原始事件
            tag = os.environ.get("PROBE_TAG", "e2e")
            (RAW / f"{tag}.events.jsonl").write_text(
                "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
                encoding="utf-8")
            try:
                await ws.close()
            except Exception:
                pass

    echo("")
    echo("=" * 70)
    echo("摘要")
    echo("=" * 70)
    echo(f"事件总数 = {len(events)}")
    echo(f"human_input_required 次数 = {len(hitl_seen)}  触发者 = {[h.get('agent') for h in hitl_seen]}")
    for i, h in enumerate(hitl_seen, 1):
        echo(f"  问 {i}: {h.get('question')}")
    echo(f"interim（交互过程，未转述给对方）轮数 = {len(interim_turns)}"
         + (f"  {[t.get('agent') for t in interim_turns]}" if interim_turns else ""))
    echo(f"工具事件 = {len(tool_uses)}")
    for t in tool_uses:
        echo(f"  {brief(t)}")
    echo(f"提交了 {n_answered} 条人工回答（answered={answered}）；是否跑到 run_end = {finished}")
    if not hitl_seen:
        echo("结论：本场没有任何 agent 调用 ask_human_input（工具没有被触发）。")
    # 完整运行日志体量大（数千条 token 事件）→ 放 raw/（不入库，见 probe/.gitignore）；
    # evidence/ 只留裁剪后的关键片段，由 README 引用。
    tag = os.environ.get("PROBE_TAG", "e2e")
    (RAW / f"{tag}.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
