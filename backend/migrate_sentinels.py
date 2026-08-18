#!/usr/bin/env python3
"""一次性迁移工具：把旧网关哨兵方案的 transcript 还原为原生 claude 格式。

旧方案把思考/答案作为带 \x01THINK\x01 / \x01ANSW\x01 哨兵的纯 text 块写入 transcript，
导致原生 claude / 外部工具看到 ---\x01THINK\x01 乱码。本工具：

  1. 按哨兵把每个 text 块切分为 thinking / answer 片段；
  2. 同一轮（连续 assistant 行）合并成一个 message，重排为
     原生 {type:thinking, thinking, signature} 块 + {type:text} 块；
  3. 去掉 --- 噪音分隔符。

特性：
  - 默认 --dry-run：只打印将要做的改动，不写任何文件；
  - 非 dry-run 会先对每个改动文件做 .bak 备份，再写回；
  - 幂等：文件已无哨兵且无 --- 噪音则跳过（不会重复处理）。

用法：
  .venv/bin/python backend/migrate_sentinels.py --dry-run
  .venv/bin/python backend/migrate_sentinels.py            # 正式迁移（先备份）
"""
import argparse
import base64
import json
import os
import re
import shutil
import sys

THINK = "\x01THINK\x01"
ANSW = "\x01ANSW\x01"
SENT_RE = re.compile("\x01(THINK|ANSW)\x01")

# 占位签名：与 backend/gateway.py::_make_signature 保持一致（resume 时网关翻译给
# Ollama 会忽略 signature，不做校验），仅用于让原生 claude 能存储/回传 thinking 块。
def make_signature(model: str, reasoning: str) -> str:
    payload = json.dumps({"m": model, "r": reasoning}, ensure_ascii=False).encode("utf-8")
    return base64.b64encode(payload).decode("ascii")


def split_segments(text: str):
    """把带哨兵的文本切成有序片段列表 [(kind, body), ...]，kind ∈ THINK/ANSW/raw。"""
    segs = SENT_RE.split(text)
    out = []
    # segs[0] = 首个哨兵前的文本；之后 (marker, body) 交替
    pre = segs[0] if segs else ""
    if pre.strip():
        out.append(("raw", pre))
    for i in range(1, len(segs) - 1, 2):
        marker = segs[i]
        body = segs[i + 1]
        out.append((marker, body))
    return out


def classify_blocks(blocks):
    """blocks: 一个 message 的 content 数组（list[dict]）。返回 (thinking_text, answer_text)。"""
    think_parts = []
    ans_parts = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t != "text":
            # 非 text 块（tool_use 等）原样保留需求不在此工具范围，跳过
            continue
        txt = b.get("text", "") or ""
        segs = split_segments(txt)
        for kind, body in segs:
            body = body.strip()
            if not body:
                continue
            # 去掉仅由 '-' 与空白组成的噪音片段（旧数据的 --- 分隔符）
            if body.strip("-") == "":
                continue
            if kind == "THINK":
                think_parts.append(body)
            elif kind == "ANSW":
                ans_parts.append(body)
            else:  # raw：哨兵前无标记文本，归为 answer（默认正文）
                ans_parts.append(body)
    return "\n\n".join(think_parts).strip(), "\n\n".join(ans_parts).strip()


def rebuild_content(thinking_text, answer_text, model):
    content = []
    if thinking_text:
        content.append({
            "type": "thinking",
            "thinking": thinking_text,
            "signature": make_signature(model, thinking_text),
        })
    if answer_text:
        content.append({"type": "text", "text": answer_text})
    return content


def migrate_file(path, dry_run, model="gemma4:26b"):
    """返回 (changed: bool, stats: dict)。dry_run 时不写文件。"""
    lines = [ln for ln in open(path, encoding="utf-8") if ln.strip()]
    out_lines = []
    cur_seg = None          # 累积中的连续 assistant 段：{"msg": dict, "blocks": [...]}
    stats = {"assistant_lines": 0, "merged_msgs": 0, "thinking_blocks": 0,
             "text_blocks": 0, "sentinel_hits": 0, "dash_hits": 0}

    def flush(seg):
        if seg is None:
            return
        thinking_text, answer_text = classify_blocks(seg["blocks"])
        if not thinking_text and not answer_text:
            # 整段都是 --- 等噪音，丢弃，绝不原样保留含哨兵的脏块
            return
        stats["merged_msgs"] += 1
        content = rebuild_content(thinking_text, answer_text, model)
        new_msg = dict(seg["msg"])
        # 关键：transcript 结构是 message.content，必须写到这一层（不能只写顶层 content）
        msg = dict(new_msg.get("message", {}))
        msg["content"] = content
        new_msg["message"] = msg
        for b in content:
            if b["type"] == "thinking":
                stats["thinking_blocks"] += 1
            elif b["type"] == "text":
                stats["text_blocks"] += 1
        out_lines.append(json.dumps(new_msg, ensure_ascii=False))

    changed_any = False
    for ln in lines:
        try:
            o = json.loads(ln)
        except Exception:
            out_lines.append(ln.rstrip("\n"))
            continue
        if o.get("type") == "assistant":
            stats["assistant_lines"] += 1
            blocks = o.get("message", {}).get("content") or []
            # 检测本段是否含哨兵/---
            dirty = False
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "text":
                    txt = b.get("text", "") or ""
                    if THINK in txt or ANSW in txt:
                        dirty = True
                        stats["sentinel_hits"] += 1
                    if txt.strip().startswith("---") or txt.strip().endswith("---") or txt.strip() == "---":
                        stats["dash_hits"] += 1
            if dirty:
                changed_any = True
                if cur_seg is None:
                    cur_seg = {"msg": o, "blocks": []}
                cur_seg["blocks"].extend(blocks)
            else:
                # 干净块：若正在累积段，先 flush，再原样输出本行
                if cur_seg is not None:
                    flush(cur_seg)
                    cur_seg = None
                out_lines.append(ln.rstrip("\n"))
        else:
            if cur_seg is not None:
                flush(cur_seg)
                cur_seg = None
            out_lines.append(ln.rstrip("\n"))

    if cur_seg is not None:
        flush(cur_seg)

    if not changed_any:
        return False, stats

    if dry_run:
        return True, stats

    # 正式迁移：先备份
    bak = path + ".bak"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    with open(path, "w", encoding="utf-8") as f:
        for ol in out_lines:
            f.write(ol + "\n")
    return True, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写文件")
    ap.add_argument("--dir", default=os.path.expanduser("~/.claude/projects/-Users-stalitz-www-DEMO"),
                    help="目标项目 transcript 目录")
    ap.add_argument("--model", default="gemma4:26b")
    args = ap.parse_args()

    files = sorted(os.path.join(args.dir, f) for f in os.listdir(args.dir) if f.endswith(".jsonl"))
    total_changed = 0
    for f in files:
        changed, stats = migrate_file(f, args.dry_run, args.model)
        if changed:
            total_changed += 1
            mode = "DRY-RUN(不改)" if args.dry_run else "已迁移"
            print(f"[{mode}] {os.path.basename(f)}")
            print(f"    assistant行={stats['assistant_lines']} 合并为={stats['merged_msgs']} 个message | "
                  f"thinking块={stats['thinking_blocks']} text块={stats['text_blocks']} | "
                  f"哨兵命中={stats['sentinel_hits']} ---命中={stats['dash_hits']}")
    if total_changed == 0:
        print("无脏文件，全部已是原生格式 ✓")
    else:
        print(f"\n共 {total_changed} 个文件需迁移" + ("（dry-run，未落盘）" if args.dry_run else "（已备份 .bak 并落盘）"))


if __name__ == "__main__":
    main()
