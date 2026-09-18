#!/usr/bin/env python3
"""在隔离的 app 实例里真跑「辩论 → 中止 → 继续」，复现/验证 claude 中断续接。

与 probe/claude-interrupt/run.py 的区别：这里走**完整编排层**（Session._debate + abort +
resume_turn），而不是直接驱动 claude 进程。用来验证「app 编排层在 abort 后立刻 resume 是否
造成 claude 续轮失败」。

留痕（全在项目内）：
  raw/app_repro.events.jsonl   WebSocket 收到的全部事件（原始）
  work/.agentchat/sessions.json  app 原生会话存储（编排层视角）
  claude transcript             app 给 claude 用的隔离配置目录（projects 软链到 ~/.claude/projects）
用法：AGENTCHAT_WS=ws://127.0.0.1:8779/ws .venv/bin/python probe/app-repro/drive.py
"""
import asyncio
import json
import os
import time
from pathlib import Path

import httpx
import websockets

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw"
RAW.mkdir(parents=True, exist_ok=True)
URL = os.environ.get("AGENTCHAT_WS", "ws://127.0.0.1:8779/ws")
HTTP_BASE = os.environ.get("AGENTCHAT_HTTP", "http://127.0.0.1:8779")
PROJ = os.environ.get("PROBE_PROJECT", "pr_probe")
TEAM = os.environ.get("PROBE_TEAM", "tm_probe")
TASK = os.environ.get("PROBE_TASK", "请从 1 数到 300，每行一个数字，不要任何解释。")
ABORT_AFTER_TEXTS = int(os.environ.get("PROBE_ABORT_AFTER_TEXTS", "3"))
TOTAL_WAIT = float(os.environ.get("PROBE_TOTAL_WAIT", "420"))


def brief(ev: dict) -> str:
    t = ev.get("type")
    bits = [str(t)]
    if ev.get("agent"):
        bits.append(str(ev["agent"]))
    if ev.get("msg"):
        bits.append(str(ev["msg"])[:70])
    if ev.get("text"):
        bits.append(str(ev["text"])[:70].replace("\n", "⏎"))
    if ev.get("interrupted"):
        bits.append("interrupted=True")
    return " | ".join(bits)


async def main():
    log_path = RAW / "app_repro.events.jsonl"
    log = log_path.open("w", encoding="utf-8")
    # 先经 REST 建会话拿到 sid（前端就是这么做的：createSessionOnServer → WS start 带 session_id）。
    # 不发 start 由服务端代建的话，sid 不会回传给客户端 → abort/resume 永远发不出去
    # （2026-09-18 实测：sid 恒 None，整个「中止→继续」路径没被跑到）。
    async with httpx.AsyncClient(base_url=HTTP_BASE, timeout=30) as hc:
        r = await hc.post(f"/api/sessions/{PROJ}", json={"title": "probe：中止→继续", "team_id": TEAM})
        r.raise_for_status()
        sid = (r.json().get("session") or {}).get("id")
    print(f"created session sid={sid}", flush=True)
    async with websockets.connect(URL, max_size=None, ping_interval=20) as ws:
        print(f"connected {URL}", flush=True)
        await ws.send(json.dumps({"action": "start", "project_id": PROJ, "team_id": TEAM,
                                  "session_id": sid, "max_rounds": 2, "task": TASK}, ensure_ascii=False))
        print(f"sent start: {TASK}", flush=True)
        texts_seen = 0
        texts_at_resume = None
        new_turn_texts = 0
        resume_ok = False
        abort_sent = False
        abort_done = False
        resume_attempts = 0
        last_resume_at = 0.0
        attack_at = None
        last_ev: dict = {}
        t0 = time.time()
        while time.time() - t0 < TOTAL_WAIT:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                raw = None
            except Exception as e:
                print("ws closed:", e, flush=True)
                break
            if raw is not None:
                try:
                    ev = json.loads(raw)
                except Exception:
                    ev = None
                if ev is not None:
                    ev["_t"] = time.strftime("%H:%M:%S")
                    last_ev = ev
                    log.write(json.dumps(ev, ensure_ascii=False) + "\n")
                    log.flush()
                    print("  " + ev["_t"] + " " + brief(ev), flush=True)
                    if ev.get("session_id"):
                        sid = ev["session_id"]
                    # 计数 claude 的文本增量（以 agent 名含 claude 且 text 非空为准）
                    ag = str(ev.get("agent") or "")
                    if "claude" in ag.lower() and ev.get("text"):
                        texts_seen += 1
                        if resume_ok:
                            new_turn_texts += 1
                    # 续接被接受 = 中止之后又出现新的 turn_start（此后才开始数「续接回合」的文本）
                    if resume_attempts and not resume_ok and ev.get("type") == "turn_start":
                        resume_ok = True
                        new_turn_texts = 0
                        print("  >>> 续接已被接受：新 turn_start", flush=True)

            # 动作检查放在「收到事件」之外：会话被中止后 run_end 就位、后续不再有事件，
            # 若只在事件分支里检查，resume_turn 永远发不出去（2026-09-18 实测踩到）。
            if not abort_sent and texts_seen >= ABORT_AFTER_TEXTS and sid:
                abort_sent = True
                attack_at = time.time()
                print(f"  >>> 已见 {texts_seen} 条 claude 文本，发 abort（sid={sid}）", flush=True)
                await ws.send(json.dumps({"action": "abort", "session_id": sid}, ensure_ascii=False))

            # 「继续」只在**中止真正落地后**才发（真实用户也是等界面提示出现再点继续）。
            # 实测：abort 的 ack（control_request）到强制终止要 ~12s，这期间发 resume 会被
            # 服务端以「该会话正在运行，无法继续」拒掉。故等 turn_end(interrupted) 后重试。
            if abort_sent and not abort_done and last_ev.get("type") == "turn_end" \
                    and last_ev.get("interrupted"):
                abort_done = True
            if abort_sent and abort_done and resume_attempts < 5 \
                    and time.time() - last_resume_at > 3.0:
                resume_attempts += 1
                last_resume_at = time.time()
                if texts_at_resume is None:
                    texts_at_resume = texts_seen
                print(f"  >>> 发 resume_turn（第 {resume_attempts} 次，模拟点「继续」）", flush=True)
                await ws.send(json.dumps({"action": "resume_turn", "session_id": sid,
                                          "project_id": PROJ}, ensure_ascii=False))

            # 续接回合里又看到 claude 的 2 条新文本 → 再等 20s 收尾就结束
            if resume_ok and new_turn_texts >= 2:
                await asyncio.sleep(20)
                break
        log.close()
        print(f"session_id = {sid}", flush=True)
        print(f"事件已存: {log_path}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
