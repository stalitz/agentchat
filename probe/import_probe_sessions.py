#!/usr/bin/env python3
"""把探针工作目录里的会话**导入到 app 项目**（让它出现在左栏项目列表的会话列表里）。

背景：探针跑在隔离实例上，用的是仓库内 `probe/**/work` 作为项目路径 + 独立 config.json，
因此会话只落在仓库里、**不会出现在用户 8777 的项目列表中**。本脚本把那些会话搬进真正的
「留痕」项目（如 agent探针(留痕)），并做两件必要改写：
  1. team_id 映射 → 目标项目里真实存在的 team（否则前端团队徽章解析不到）；
  2. 标题加可辨识前缀（否则一堆「probe：HITL 人工介入」分不清）。
同 id 已存在则跳过；导入前自动备份目标 sessions.json。

用法：
  .venv/bin/python probe/import_probe_sessions.py \
      --src probe/hitl-probe/work \
      --dst /Users/stalitz/www/aitest/agent-probes \
      --team-map tm_hitl=tm_a0242d79,tm_hitl_c=tm_a0242d79,tm_hitl_x=tm_c04bdb6d \
      --title-prefix "HITL 探针"
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="探针项目路径（含 .agentchat/）")
    ap.add_argument("--dst", required=True, help="目标项目路径（app 项目列表里的项目）")
    ap.add_argument("--team-map", default="", help="源 team_id=目标 team_id，逗号分隔")
    ap.add_argument("--title-prefix", default="", help="标题前缀（可辨识用）")
    ap.add_argument("--title-map", default="", help="逐条指定标题：sid=新标题，逗号分隔（优先于 --title-prefix）")
    ap.add_argument("--apply", action="store_true", help="真正写入（缺省只打印计划）")
    a = ap.parse_args()

    src = Path(a.src).expanduser().resolve()
    dst = Path(a.dst).expanduser().resolve()
    s_sj = src / ".agentchat" / "sessions.json"
    d_sj = dst / ".agentchat" / "sessions.json"
    if not s_sj.exists():
        print(f"源会话文件不存在: {s_sj}")
        return 1
    if not d_sj.exists():
        print(f"目标项目没有会话文件: {d_sj}")
        return 1

    def _pair(s):
        out = {}
        for kv in filter(None, s.split(",")):
            k, _, v = kv.partition("=")
            out[k.strip()] = v.strip()
        return out

    tmap = _pair(a.team_map)
    titles = _pair(a.title_map)

    src_sessions = json.loads(s_sj.read_text(encoding="utf-8"))
    dst_sessions = json.loads(d_sj.read_text(encoding="utf-8"))
    have = {s.get("id") for s in dst_sessions}

    plan = []
    for s in src_sessions:
        if s.get("id") in have:
            continue
        ns = dict(s)
        tid = ns.get("team_id")
        if tid in tmap:
            ns["team_id"] = tmap[tid]
        sid = ns.get("id")
        if sid in titles:
            ns["title"] = titles[sid]
        elif a.title_prefix:
            tail = (ns.get("updated_at") or "")[-5:] or (ns.get("created_at") or "")[-5:]
            ns["title"] = f"{a.title_prefix} · {ns.get('title', '').strip()[:24]} · {tail}"
        plan.append(ns)

    print(f"源 {len(src_sessions)} 条，目标已有 {len(dst_sessions)} 条，待导入 {len(plan)} 条")
    for s in plan:
        print(f"   + {s['id']}  team={s.get('team_id')}  msgs={len(s.get('messages') or [])}  "
              f"title={s['title']}")
    if not a.apply:
        print("\n（dry-run，加 --apply 才写入）")
        return 0

    bak = d_sj.with_suffix(f".json.bak{time.strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(d_sj, bak)
    print(f"\n已备份目标会话文件 → {bak}")

    # prompts / tools 侧文件一并搬（同名 sid，目标不存在才复制）
    for sub in ("prompts", "tools"):
        sd = src / ".agentchat" / sub
        dd = dst / ".agentchat" / sub
        if not sd.exists():
            continue
        dd.mkdir(parents=True, exist_ok=True)
        for s in plan:
            f = sd / f"{s['id']}.jsonl"
            if f.exists() and not (dd / f.name).exists():
                shutil.copy2(f, dd / f.name)
                print(f"   复制 {sub}/{f.name}")

    dst_sessions.extend(plan)
    d_sj.write_text(json.dumps(dst_sessions, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已写入 {len(plan)} 条 → {d_sj}（现共 {len(dst_sessions)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
