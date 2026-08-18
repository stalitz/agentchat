"""为历史会话回填时间戳（ts），来源：claude CLI transcript 的 user/assistant 事件 timestamp。

claude 原始 transcript 每个事件都带 ISO 时间戳（如 `2026-08-17T08:11:26.627Z`），
agent-chat 的 sessions.json 在加 ts 落盘之前没有时间线。本工具按以下规则回填：

- **human 消息**：**只给「成功送达 claude 且有回复」的提问**回填——按内容匹配 transcript 的
  user 事件（编排器 prompt 含用户问题/任务/介入文本）；无回复的提问（claude 根本没收到，
  如 gemma 冷启/链路失败）不匹配，时间无从考证、保持无 ts（前端不显示时间）；
- **agent 回复**：按内容匹配该 agent transcript 的 assistant 回合（去 --- 后子串匹配，
  避免 gemma 模板答案重复时按顺序错位）；
- 无 transcript 对应（失败空跑等）的消息保持无 ts（前端不显示时间）。

用法：
    .venv/bin/python -m backend.backfill_timestamps <project_path> [--backup]
"""
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import executor, store  # noqa: E402


def _local(iso_utc: str) -> str:
    """'2026-08-17T08:11:26.627Z' → 本地 '2026-08-17 16:11:26'。"""
    try:
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _transcript_events(path: str) -> tuple[list, list]:
    """解析 transcript：返回 (user_events, reply_events)。

    - user_events: [(local_ts, prompt_text), ...]
    - reply_events: [{"ts": local_ts, "content": str, "thinking": str}, ...] —— 按 user 事件分界的每次
      assistant 回复（同一 user 后可能多个 assistant 事件，thinking/text 合并；取最后一条
      assistant 事件的 ts 作为回复完成时间）。
    """
    users, replies = [], []
    pending_reply = None  # 当前回复单元 {"ts": ..., "content": [...], "thinking": [...]}
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                o = json.loads(ln)
            except json.JSONDecodeError:
                continue
            t = o.get("type")
            if t == "user":
                if pending_reply:
                    replies.append({
                        "ts": pending_reply["ts"],
                        "content": "".join(pending_reply["content"]),
                        "thinking": "".join(pending_reply["thinking"]),
                    })
                    pending_reply = None
                c = (o.get("message") or {}).get("content")
                if isinstance(c, str) and c.strip():
                    users.append((_local(o.get("timestamp", "")), c))
                elif isinstance(c, list):
                    text = "".join(b.get("text", "") for b in c if isinstance(b, dict))
                    users.append((_local(o.get("timestamp", "")), text))
                continue
            if t != "assistant":
                continue
            if pending_reply is None:
                pending_reply = {"ts": _local(o.get("timestamp", "")), "content": [], "thinking": []}
            else:
                pending_reply["ts"] = _local(o.get("timestamp", ""))  # 取最后一条 assistant 的 ts
            msg = o.get("message", {})
            blocks = msg.get("content")
            if isinstance(blocks, str):
                pending_reply["content"].append(blocks)
            elif isinstance(blocks, list):
                for b in blocks:
                    if isinstance(b, dict) and b.get("type") == "text":
                        pending_reply["content"].append(b.get("text", ""))
                    elif isinstance(b, dict) and b.get("type") == "thinking":
                        pending_reply["thinking"].append(b.get("thinking", "") or b.get("text", ""))
    if pending_reply:
        replies.append({
            "ts": pending_reply["ts"],
            "content": "".join(pending_reply["content"]),
            "thinking": "".join(pending_reply["thinking"]),
        })
    return users, replies


def _norm(s: str) -> str:
    return (s or "").strip().lstrip("-").strip()


def _content_match(local: str, transcript: str) -> bool:
    """宽松内容匹配：去 --- 后任一方前 30 字出现在对方里（gemma 模板答案也适用）。"""
    l, t = _norm(local), _norm(transcript)
    if not l or not t:
        return False
    return t[:30] in l or l[:30] in t


def _has_reply(msgs: list, human_idx: int) -> bool:
    """该 human 提问之后、下一个提问之前，是否存在非空的 agent 回复。

    无回复的提问（claude 根本没收到，如 gemma 冷启/链路失败空跑）时间无从考证，
    不参与 transcript 时间回填。
    """
    for m in msgs[human_idx + 1:]:
        sp = m.get("speaker")
        if sp == "human":
            return False
        if sp and sp != "system" and (m.get("content") or "").strip():
            return True
    return False


def main(argv: list[str]) -> int:
    if not argv:
        print("用法: python -m backend.backfill_timestamps <project_path> [--backup]")
        return 1
    project_path = argv[0]
    backup = "--backup" in argv
    sessions = store._load_sessions(project_path)
    if not sessions:
        print(f"无会话: {project_path}")
        return 1
    if backup:
        f = store._sessions_file(project_path)
        bak = str(f) + ".bak-ts"
        shutil.copy2(f, bak)
        print(f"备份 -> {bak}")

    filled = 0
    for s in sessions:
        msgs = s.get("messages", [])
        agent_sessions = s.get("agent_sessions") or {}
        # 收集所有 agent 的 transcript 事件
        all_users: list = []
        agent_replies: dict[str, list] = {}
        for agent_name, cli_id in agent_sessions.items():
            p = executor._claude_transcript_path(cli_id, agent_name)
            if not p:
                continue
            users, replies = _transcript_events(p)
            all_users.extend(users)
            agent_replies[agent_name] = replies
        if not all_users and not agent_replies:
            continue
        # 先清空旧 ts，再按 transcript 重算（幂等；修正历史错配的 ts，如失败提问误占时间）
        for m in msgs:
            m.pop("ts", None)
        used_users = set()
        for i, m in enumerate(msgs):
            if m.get("ts"):
                continue
            sp = m.get("speaker")
            content = m.get("content") or ""
            if sp == "human":
                if not _has_reply(msgs, i):
                    continue  # 无回复的提问：claude 根本没收到，时间无从考证，保持无 ts
                for j, (ts, prompt) in enumerate(all_users):
                    if j in used_users:
                        continue
                    if content.strip() and content.strip() in prompt:
                        m["ts"] = ts
                        used_users.add(j)
                        filled += 1
                        break
            elif sp in agent_replies:
                for r in agent_replies[sp]:
                    if _content_match(content, r["content"]):
                        m["ts"] = r["ts"]
                        filled += 1
                        break
                    # 思考无正文的回复（gemma 只吐 thinking + '---' 被清洗）：用 thinking 匹配
                    if not content and not _norm(r["content"]) \
                            and m.get("thinking") and _content_match(m.get("thinking", ""), r.get("thinking", "")):
                        m["ts"] = r["ts"]
                        filled += 1
                        break
        if filled:
            s["updated_at"] = store._now()
    store._save_sessions(project_path, sessions)
    print(f"完成: 回填 {filled} 条消息时间戳")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
