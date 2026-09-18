#!/usr/bin/env python3
"""复现/验证「claude-code 被中断后，同 session 续轮变哑」的最小实验（留痕用）。

背景（2026-09-18 实测 s_967eab80）：
  app 每轮都新起 claude 进程（首次 `--session-id <uuid>`，后续 `--resume <uuid>`）。
  turn 级中止走 stdin `control_request{subtype:interrupt}` 帧（executor.interrupt）。
  实测：生成中途被中断后，续轮 claude 不再发 API 请求（transcript 出 <synthetic>、usage 全 0），
  直接合成 `No response requested.` / `API Error: ... empty or malformed response (HTTP 200)`。

本脚本按 app 的真实做法驱动 claude CLI（同样的帧格式、同样的参数），并落原生留痕：
  raw/<tag>.stdout.jsonl       claude stdout 原始行（turn1）
  raw/<tag>.turn2.stdout.jsonl 续轮 stdout 原始行
  raw/<tag>.turn1.events.txt   实验时间线
  raw/<tag>.transcript.jsonl   claude 原生 transcript（跑完从 CLAUDE_CONFIG_DIR 拷回）

用法:
  .venv/bin/python probe/claude-interrupt/run.py --mode stdin       --tag b_stdin
  .venv/bin/python probe/claude-interrupt/run.py --mode sigint      --tag c_sigint
  .venv/bin/python probe/claude-interrupt/run.py --mode repairexit  --tag d_repair
  .venv/bin/python probe/claude-interrupt/run.py --mode sameproc    --tag e_sameproc

mode:
  stdin       中断 = stdin control_request 帧（app 现状），续轮 = --resume
  sigint      中断 = SIGINT 给进程组（模拟真实终端 Ctrl+C），续轮 = --resume
  repairexit  中断 = stdin 帧；续轮前改写 transcript（摘掉中断/合成残留）后再 --resume
  sameproc    中断 = stdin 帧；续轮 = **同一个进程**继续喂 user 帧（真实终端 Ctrl+C 后继续输入）
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw"
CFG = HERE / "claude-cfg"          # 隔离 CLAUDE_CONFIG_DIR：transcript 落在项目内
MODEL = os.environ.get("PROBE_MODEL", "gemma4:26b")
BASE_URL = os.environ.get("PROBE_BASE_URL", "http://127.0.0.1:8778")

LONG_PROMPT = "请从 1 数到 300，每行一个数字，不要任何解释。"
SHORT_PROMPT = "用一句话回答：1+1 等于几？只回中文。"


def claude_env() -> dict:
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(CFG)
    env["ANTHROPIC_BASE_URL"] = BASE_URL
    env["ANTHROPIC_AUTH_TOKEN"] = "dummy"
    env["ANTHROPIC_API_KEY"] = ""
    return env


def user_frame(text: str) -> bytes:
    # 与 executor._user_frame 完全同形（stream-json NDJSON，结尾换行）
    return (json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }, ensure_ascii=False) + "\n").encode("utf-8")


def interrupt_frame() -> bytes:
    # 与 executor.AgentRuntime.interrupt 完全同形
    return (json.dumps({
        "type": "control_request", "request_id": "int-1",
        "request": {"subtype": "interrupt", "cancel_queued": True},
    }, ensure_ascii=False) + "\n").encode("utf-8")


class Turn:
    """驱动一次 claude 进程，按行收集 stdout，可中途写中断帧。"""

    def __init__(self, tag: str, resume_id: str | None):
        self.tag = tag
        self.lines: list[str] = []
        self.err: list[str] = []
        self.events: list[str] = []
        cmd = ["claude", "--output-format", "stream-json", "--input-format", "stream-json",
               "--verbose", "--include-partial-messages", "--dangerously-skip-permissions"]
        if resume_id:
            cmd += ["--resume", resume_id]
            self.session_id = resume_id
        else:
            self.session_id = str(uuid.uuid4())
            cmd += ["--session-id", self.session_id]
        cmd += ["--model", MODEL]
        self.cmd = cmd

    def _ev(self, s: str):
        line = f"[{time.strftime('%H:%M:%S')}] {s}"
        self.events.append(line)
        print("  " + line, flush=True)

    def _pump(self, stream, sink: list[str]):
        for raw in iter(stream.readline, b""):
            sink.append(raw.decode(errors="replace"))

    def start(self):
        self._ev(f"start: {' '.join(self.cmd)}")
        self.proc = subprocess.Popen(
            self.cmd, cwd=str(HERE), env=claude_env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.out_t = threading.Thread(target=self._pump, args=(self.proc.stdout, self.lines), daemon=True)
        self.err_t = threading.Thread(target=self._pump, args=(self.proc.stderr, self.err), daemon=True)
        self.out_t.start()
        self.err_t.start()
        self._ev(f"pid={self.proc.pid} session={self.session_id}")

    def send(self, data: bytes, what: str):
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
            self._ev(f"sent {what} ({len(data)}B)")
        except Exception as e:
            self._ev(f"send {what} FAILED: {e}")

    def saw(self, needle: str) -> bool:
        return any(needle in l for l in list(self.lines))

    def count_result(self) -> int:
        return sum(1 for l in list(self.lines) if '"type":"result"' in l)

    def wait_first_output(self, max_s: float) -> bool:
        """等到 claude 吐出第一个实质事件（说明 API 已往返、正在生成）。"""
        t0 = time.time()
        while time.time() - t0 < max_s:
            if self.saw('"type":"stream_event"') or self.saw('"type":"assistant"'):
                return True
            time.sleep(0.3)
        return False

    def wait_result_n(self, n: int, max_s: float) -> bool:
        t0 = time.time()
        while time.time() - t0 < max_s:
            if self.count_result() >= n:
                self._ev(f"result count = {self.count_result()}")
                return True
            if self.proc.poll() is not None:
                self._ev(f"process exited rc={self.proc.returncode}")
                return False
            time.sleep(0.3)
        self._ev(f"timeout waiting result #{n} (have {self.count_result()})")
        return False

    def interrupt(self, mode: str):
        if mode == "sigint":
            try:
                pgid = os.getpgid(self.proc.pid)
                os.killpg(pgid, signal.SIGINT)
                self._ev(f"sent SIGINT to pgid {pgid}")
            except Exception as e:
                self._ev(f"SIGINT FAILED: {e}")
        else:
            self.send(interrupt_frame(), "control_request{interrupt}")

    def drain(self, max_s: float, close_stdin: bool = True):
        """收尾：等进程退出；不退出则关 stdin，再不行整组杀。"""
        if self.proc.poll() is None and self.count_result() == 0:
            self.wait_result_n(1, max_s)
        try:
            if close_stdin and self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
                self._ev("closed stdin")
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
            self._ev(f"process exited rc={self.proc.returncode}")
        except subprocess.TimeoutExpired:
            self._ev("process still alive 10s after stdin close -> killpg")
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except Exception:
                pass
        self.out_t.join(timeout=5)
        self.err_t.join(timeout=5)

    def dump(self, suffix: str = ""):
        sfx = f".{suffix}" if suffix else ""
        (RAW / f"{self.tag}.stdout{sfx}.jsonl").write_text("".join(self.lines), encoding="utf-8")
        (RAW / f"{self.tag}.stderr{sfx}.txt").write_text("".join(self.err), encoding="utf-8")
        (RAW / f"{self.tag}.events{sfx}.txt").write_text("\n".join(self.events) + "\n", encoding="utf-8")


def transcript_paths(sid: str) -> list[Path]:
    root = CFG / "projects"
    return list(root.rglob(f"{sid}.jsonl")) if root.exists() else []


def lang_of(t: str) -> str:
    cn = sum(1 for c in t if "\u4e00" <= c <= "\u9fff")
    en = sum(1 for c in t if c.isalpha() and ord(c) < 128)
    return f"中{cn}/英{en}"


def summarize(lines: list[str], tag: str) -> dict:
    print(f"\n----- {tag} 摘要 -----", flush=True)
    stat = {"real_api": 0, "synthetic": 0, "results": [], "texts": []}
    for l in lines:
        try:
            d = json.loads(l)
        except Exception:
            continue
        t = d.get("type")
        if t == "assistant":
            m = d.get("message", {})
            u = m.get("usage") or {}
            mdl = m.get("model")
            txt = ""
            c = m.get("content")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "text":
                        txt += b.get("text", "")
                    elif isinstance(b, dict) and b.get("type") == "tool_use":
                        txt += f"[tool_use:{b.get('name')}]"
            if mdl == "<synthetic>":
                stat["synthetic"] += 1
            else:
                stat["real_api"] += 1
            if txt:
                stat["texts"].append(txt)
            print(f"  assistant model={mdl} in={u.get('input_tokens')} out={u.get('output_tokens')} ({lang_of(txt)}) text={txt[:70]!r}", flush=True)
        elif t == "result":
            stat["results"].append((d.get("subtype"), d.get("is_error"), str(d.get("result"))[:120]))
            print(f"  result subtype={d.get('subtype')} is_error={d.get('is_error')} result={str(d.get('result'))[:140]!r}", flush=True)
    return stat


def warmup():
    """预热 Ollama（26B 冷启动很慢），避免 turn1 还没开始生成就被中断。"""
    marker = RAW / ".warmup"
    if marker.exists() and time.time() - marker.stat().st_mtime < 240:
        print("warmup: skipped (recent)", flush=True)
        return
    print("warmup: 预热模型...", flush=True)
    t = Turn("warmup", None)
    t.start()
    t.send(user_frame("只回复两个字：收到"), "warmup frame")
    t.wait_result_n(1, 300)
    t.drain(20)
    t.dump()
    summarize(t.lines, "warmup")
    marker.write_text(str(time.time()), encoding="utf-8")


def find_interrupt_leftovers(p: Path) -> list[dict]:
    """列出 transcript 里的中断/合成残留条目（诊断用）。"""
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        m = d.get("message") or {}
        c = m.get("content")
        blob = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
        if m.get("role") == "user" and ("[Request interrupted by user" in blob
                                        or "No response requested" in blob
                                        or "empty or malformed" in blob):
            out.append({"uuid": d.get("uuid"), "role": "user", "text": str(blob)[:140]})
        elif m.get("role") == "assistant" and (m.get("model") == "<synthetic>"
                                               or "No response requested" in blob):
            out.append({"uuid": d.get("uuid"), "role": "assistant(synthetic)", "text": str(blob)[:140]})
    return out


def repair(p: Path) -> int:
    keep, dropped = [], []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except Exception:
            keep.append(line)
            continue
        m = d.get("message") or {}
        c = m.get("content")
        blob = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
        is_marker = (m.get("role") == "user" and "[Request interrupted by user" in blob)
        is_synth = (m.get("model") == "<synthetic>" or (m.get("role") == "assistant"
                                                        and "No response requested" in blob))
        if is_marker or is_synth:
            dropped.append(str(blob)[:90])
            continue
        keep.append(line)
    p.write_text("\n".join(keep) + "\n", encoding="utf-8")
    print(f"repair: dropped {len(dropped)} line(s): {dropped}", flush=True)
    return len(dropped)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="stdin", choices=["stdin", "sigint", "repairexit", "sameproc"])
    ap.add_argument("--tag", default=None)
    ap.add_argument("--turn1-wait", type=float, default=120.0, help="等首个输出的上限（秒）")
    ap.add_argument("--hold", type=float, default=4.0, help="首个输出后再等多久才中断（秒，制造「生成中途」）")
    ap.add_argument("--turn2-wait", type=float, default=240.0, help="续轮最长等待（秒）")
    ap.add_argument("--no-warmup", action="store_true")
    args = ap.parse_args()
    tag = args.tag or args.mode
    RAW.mkdir(parents=True, exist_ok=True)
    CFG.mkdir(parents=True, exist_ok=True)

    print(f"=== mode={args.mode} tag={tag} model={MODEL} base_url={BASE_URL} ===", flush=True)
    if not args.no_warmup:
        warmup()

    # ---------- turn 1：开始生成 → （生成中途）中断 ----------
    t1 = Turn(f"{tag}.turn1", resume_id=None)
    t1.start()
    t1.send(user_frame(LONG_PROMPT), "turn1 user frame")
    got = t1.wait_first_output(args.turn1_wait)
    t1._ev(f"first output seen={got}")
    if got:
        time.sleep(args.hold)
        t1._ev(f"held {args.hold}s (partial output accumulated)")
    t1.interrupt(args.mode)
    t1.wait_result_n(1, 90)
    t1.drain(20)
    t1.dump("turn1")
    s1 = summarize(t1.lines, "turn1")

    tid = t1.session_id
    print(f"\nsession id = {tid}", flush=True)
    tps = transcript_paths(tid)
    print(f"transcript: {[str(p) for p in tps]}", flush=True)
    if tps:
        left = find_interrupt_leftovers(tps[0])
        print(f"transcript 中断残留条目: {len(left)}", flush=True)
        for x in left:
            print(f"    {x['role']:22} uuid={x['uuid']} {x['text']!r}", flush=True)

    if args.mode == "repairexit" and tps:
        repair(tps[0])

    # ---------- turn 2 ----------
    if args.mode == "sameproc":
        # 同进程续轮：中断后往**同一个进程**的 stdin 再喂一条 user 帧（真实终端 Ctrl+C 后的行为）
        t1._ev(">>> same-process turn2: writing next user frame into the SAME stdin")
        t1.send(user_frame(SHORT_PROMPT), "same-proc turn2 user frame")
        t1.wait_result_n(t1.count_result() + 1, args.turn2_wait)
        s2 = summarize(t1.lines, "sameproc-turn2")
        t1.drain(20)
        t1.dump("sameproc")
        t2_lines = t1.lines
    else:
        t2 = Turn(f"{tag}.turn2", resume_id=tid)
        t2.start()
        t2.send(user_frame(SHORT_PROMPT), "turn2 user frame")
        t2.wait_result_n(1, args.turn2_wait)
        t2.drain(20)
        t2.dump("turn2")
        s2 = summarize(t2.lines, "turn2")
        t2_lines = t2.lines

    for p in transcript_paths(tid):
        dest = RAW / f"{tag}.transcript.jsonl"
        dest.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"transcript copied: {p} -> {dest}", flush=True)

    verdict = ("✅ 续轮正常（有真实 API 往返）" if s2.get("real_api")
               else "❌ 续轮变哑（无真实 API 往返 / 合成消息）")
    print(f"\n=== mode={args.mode} 判定：{verdict} ===", flush=True)
    print(f"    turn1: real_api={s1.get('real_api')} synthetic={s1.get('synthetic')} results={s1.get('results')}", flush=True)
    print(f"    turn2: real_api={s2.get('real_api')} synthetic={s2.get('synthetic')} results={s2.get('results')}", flush=True)
    print(f"    turn2 texts={[t[:60] for t in s2.get('texts', [])]}", flush=True)


if __name__ == "__main__":
    main()
