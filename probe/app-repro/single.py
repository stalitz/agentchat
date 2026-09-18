#!/usr/bin/env python3
"""按 app 的调用形态跑**一轮** claude（复刻 executor._claude_cmd / _claude_env），二分定位失败原因。

app 场（8779）实测：claude 第 1 轮就失败 —— 只收到 `---` 然后报
`API Error: API returned an empty or malformed response (HTTP 200)`，而 Ollama 两次都 200。
与我的裸直跑（probe/claude-interrupt/run.py，成功）相比，app 多了：隔离 CLAUDE_CONFIG_DIR
（含用户全局 settings 的 env，尤其 CLAUDE_CODE_EFFORT_LEVEL=max）、cwd 下的 .mcp.json
（end_discussion 工具）、CLAUDE_CODE_MAX_OUTPUT_TOKENS=64000、/gw 路径。

用法：
  .venv/bin/python probe/app-repro/single.py --tag s1_full            # 全复刻 app
  .venv/bin/python probe/app-repro/single.py --tag s2_clean_cfg --clean-cfg   # 换干净配置目录
  .venv/bin/python probe/app-repro/single.py --tag s3_no_effort --env CLAUDE_CODE_EFFORT_LEVEL=low
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw"
RAW.mkdir(parents=True, exist_ok=True)
MODEL = os.environ.get("PROBE_MODEL", "gemma4:26b")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--cwd", default=str(HERE / "work"))
    ap.add_argument("--cfg-dir", default=os.path.expanduser("~/.cache/agentchat/claude-cfg/Probe_Claude"))
    ap.add_argument("--clean-cfg", action="store_true", help="用本目录下的干净 claude-cfg（不含 EFFORT_LEVEL）")
    ap.add_argument("--prompt", default="只回复两个字：收到")
    ap.add_argument("--prompt-file", default=None, help="从文件读 prompt（复刻 app 场完整 prompt 用）")
    ap.add_argument("--base-url", default="http://127.0.0.1:8779")
    ap.add_argument("--env", action="append", default=[], help="额外 env，KEY=VAL，可重复")
    ap.add_argument("--max-output-tokens", default="64000")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--wait", type=float, default=300)
    args = ap.parse_args()

    if args.prompt_file:
        args.prompt = Path(args.prompt_file).read_text(encoding="utf-8")

    cwd = args.cwd
    if args.clean_cfg:
        cfg = str(HERE / "claude-cfg")
        Path(cfg).mkdir(parents=True, exist_ok=True)
    else:
        cfg = args.cfg_dir

    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = cfg
    env["ANTHROPIC_BASE_URL"] = args.base_url
    env["ANTHROPIC_API_KEY"] = "local-gateway"
    env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = args.model
    env["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"] = args.model
    if args.max_output_tokens:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = args.max_output_tokens
    for kv in args.env:
        k, _, v = kv.partition("=")
        env[k] = v

    sid = str(uuid.uuid4())
    cmd = ["claude", "--output-format", "stream-json", "--input-format", "stream-json",
           "--verbose", "--include-partial-messages", "--dangerously-skip-permissions",
           "--session-id", sid, "--model", args.model]
    ev = []
    ev.append(f"[{time.strftime('%H:%M:%S')}] cwd={cwd} cfg={cfg}")
    ev.append(f"[{time.strftime('%H:%M:%S')}] cmd={' '.join(cmd)}")
    ev.append(f"[{time.strftime('%H:%M:%S')}] prompt={args.prompt!r}")
    for k in ("CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "ANTHROPIC_BASE_URL"):
        ev.append(f"[{time.strftime('%H:%M:%S')}] env {k}={env.get(k)}")
    mcp = Path(cwd) / ".mcp.json"
    ev.append(f"[{time.strftime('%H:%M:%S')}] mcp.json present={mcp.exists()}")
    for e in ev:
        print(e, flush=True)

    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    lines, errs = [], []

    def pump(stream, sink):
        for raw in iter(stream.readline, b""):
            sink.append(raw.decode(errors="replace"))

    t_out = threading.Thread(target=pump, args=(proc.stdout, lines), daemon=True)
    t_err = threading.Thread(target=pump, args=(proc.stderr, errs), daemon=True)
    t_out.start()
    t_err.start()

    frame = (json.dumps({"type": "user",
                         "message": {"role": "user", "content": [{"type": "text", "text": args.prompt}]}},
                        ensure_ascii=False) + "\n").encode("utf-8")
    proc.stdin.write(frame)
    proc.stdin.flush()

    t0 = time.time()
    while time.time() - t0 < args.wait:
        if any('"type":"result"' in l for l in list(lines)):
            break
        if proc.poll() is not None:
            break
        time.sleep(0.3)
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

    (RAW / f"{args.tag}.stdout.jsonl").write_text("".join(lines), encoding="utf-8")
    (RAW / f"{args.tag}.stderr.txt").write_text("".join(errs), encoding="utf-8")
    (RAW / f"{args.tag}.events.txt").write_text("\n".join(ev) + "\n", encoding="utf-8")

    print("\n----- 结果 -----", flush=True)
    real = synth = 0
    for l in lines:
        try:
            d = json.loads(l)
        except Exception:
            continue
        if d.get("type") == "assistant":
            m = d.get("message") or {}
            u = m.get("usage") or {}
            txt = ""
            c = m.get("content")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "text":
                        txt += b.get("text", "")
            if m.get("model") == "<synthetic>":
                synth += 1
            else:
                real += 1
            print(f"  assistant model={m.get('model')} in={u.get('input_tokens')} out={u.get('output_tokens')} text={txt[:80]!r}", flush=True)
        elif d.get("type") == "result":
            print(f"  result subtype={d.get('subtype')} is_error={d.get('is_error')} result={str(d.get('result'))[:150]!r}", flush=True)
    verdict = "✅ 正常" if (real and not synth) else "❌ 失败/异常"
    print(f"\n=== tag={args.tag} 判定：{verdict} (real={real} synthetic={synth}) ===", flush=True)


if __name__ == "__main__":
    main()
