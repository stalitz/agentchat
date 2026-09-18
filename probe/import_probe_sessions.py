#!/usr/bin/env python3
"""把探针工作目录里的会话**导入到 app 项目**（让它出现在左栏项目列表的会话列表里）。

背景：探针跑在隔离实例上，用的是仓库内 `probe/**/work` 作为项目路径 + 独立 config.json，
因此会话只落在仓库里、**不会出现在用户 8777 的项目列表中**。本脚本把那些会话搬进真正的
「留痕」项目（如 agent探针(留痕)），并做三件必要改写：
  1. team_id 映射 → 目标项目里真实存在的 team（否则前端团队徽章解析不到）；
  2. 标题加可辨识前缀（否则一堆「probe：HITL 人工介入」分不清）；
  3. **发言者改名**（--speaker-map）→ 目标项目里真实存在的 agent 名。
     必须改：前端气泡颜色是 `cfg_agents_by_name(speaker)` 按**名字**查 agent.type，
     名字对不上就回落 claude-code（蓝色），codex 的绿气泡、fake 的橙气泡都会显示错。
     prompts/tools 侧 jsonl 里的 agent 字段同步改名（否则工具状态标签匹配不上）。
同 id 已存在则跳过（加 --force 覆盖回填）；导入前自动备份目标 sessions.json。

用法：
  .venv/bin/python probe/import_probe_sessions.py \
      --src probe/hitl-probe/work \
      --dst /Users/stalitz/www/aitest/agent-probes \
      --team-map tm_hitl=tm_a0242d79,tm_hitl_c=tm_a0242d79,tm_hitl_x=tm_c04bdb6d \
      --speaker-map "H-Probe Codex=Codex (本地 gemma),H-Probe Claude=Claude Code (本地 gemma)" \
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
    ap.add_argument("--speaker-map", default="",
                    help="发言者改名：原名=目标项目里的 agent 名，逗号分隔（决定气泡颜色，务必填）")
    ap.add_argument("--sid-speaker-map", default="",
                    help="按会话覆盖改名：sid:原名=新名，逗号分隔（优先于 --speaker-map）")
    ap.add_argument("--only", default="", help="只处理这些 sid，逗号分隔（分批处理不同 team 的会话用）")
    ap.add_argument("--force", action="store_true",
                    help="目标已存在同 id 会话时也覆盖回填（用于补做发言者改名 / 换 team）")
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
    smap = _pair(a.speaker_map)
    sid_smap = {}
    for kv in filter(None, a.sid_speaker_map.split(",")):
        sid, _, rest = kv.partition(":")
        sid_smap[sid.strip()] = _pair(rest)
    only = {x.strip() for x in filter(None, a.only.split(","))}

    def renamer(sid):
        per = sid_smap.get(sid, {})

        def rename(name):
            if not name:
                return name
            return per.get(name, smap.get(name, name))
        return rename

    def transform(s):
        ns = json.loads(json.dumps(s))  # 深拷贝，避免改到源
        sid = ns.get("id")
        rename = renamer(sid)
        tid = ns.get("team_id")
        if tid in tmap:
            ns["team_id"] = tmap[tid]
        sid = ns.get("id")
        if sid in titles:
            ns["title"] = titles[sid]
        elif a.title_prefix and not str(ns.get("title") or "").startswith(a.title_prefix):
            # 回填时不重复加前缀
            tail = (ns.get("updated_at") or "")[-5:] or (ns.get("created_at") or "")[-5:]
            ns["title"] = f"{a.title_prefix} · {ns.get('title', '').strip()[:24]} · {tail}"
        # 发言者改名：气泡颜色按名字查 agent.type，名字必须命中目标项目里的 agent
        for m in (ns.get("messages") or []):
            for k in ("agent", "speaker"):
                if k in m and isinstance(m[k], str):
                    m[k] = rename(m[k])
        if isinstance(ns.get("agent_sessions"), dict):
            ns["agent_sessions"] = {rename(k): v for k, v in ns["agent_sessions"].items()}
        return ns

    src_sessions = json.loads(s_sj.read_text(encoding="utf-8"))
    dst_sessions = json.loads(d_sj.read_text(encoding="utf-8"))
    have = {s.get("id") for s in dst_sessions}

    added, updated = [], []
    for s in src_sessions:
        sid = s.get("id")
        if only and sid not in only:
            continue
        if sid in have and not a.force:
            continue
        ns = transform(s)
        (updated if sid in have else added).append(ns)

    plan = added + updated
    print(f"源 {len(src_sessions)} 条，目标已有 {len(dst_sessions)} 条；"
          f"新增 {len(added)} 条，覆盖回填 {len(updated)} 条")
    for s in plan:
        who = sorted({(m.get("agent") or m.get("speaker")) for m in (s.get("messages") or [])
                      if (m.get("agent") or m.get("speaker"))})
        print(f"   {'~' if s['id'] in have else '+'} {s['id']}  team={s.get('team_id')}  "
              f"msgs={len(s.get('messages') or [])}  speakers={who}  title={s['title']}")
    if not a.apply:
        print("\n（dry-run，加 --apply 才写入）")
        return 0

    bak = d_sj.with_suffix(f".json.bak{time.strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(d_sj, bak)
    print(f"\n已备份目标会话文件 → {bak}")

    # prompts / tools 侧文件一并搬；--force 时覆盖，并把日志里的 agent 名同步改掉
    # （工具状态标签按 speaker 匹配 tools 日志的 agent 字段，名字不一致会导致标签不显示）
    if smap:
        for sub in ("prompts", "tools"):
            sd = src / ".agentchat" / sub
            dd = dst / ".agentchat" / sub
            if not sd.exists():
                continue
            dd.mkdir(parents=True, exist_ok=True)
            for s in plan:
                f = sd / f"{s['id']}.jsonl"
                if not f.exists():
                    continue
                rename = renamer(s.get("id"))
                out = []
                for line in f.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except ValueError:
                        out.append(line)
                        continue
                    if isinstance(o, dict) and isinstance(o.get("agent"), str):
                        o["agent"] = rename(o["agent"])
                    out.append(json.dumps(o, ensure_ascii=False))
                (dd / f.name).write_text("\n".join(out) + "\n", encoding="utf-8")
                print(f"   写入 {sub}/{f.name}")
    else:
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

    by_id = {s.get("id"): s for s in plan}
    dst_sessions = [by_id[s.get("id")] if s.get("id") in by_id else s for s in dst_sessions]
    for s in added:
        if s.get("id") not in {x.get("id") for x in dst_sessions}:
            dst_sessions.append(s)
    d_sj.write_text(json.dumps(dst_sessions, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已写入 新增 {len(added)} / 回填 {len(updated)} 条 → {d_sj}（现共 {len(dst_sessions)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
