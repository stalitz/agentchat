import json
import os
import uuid
from pathlib import Path
from typing import Optional

from .models import ProjectConfig, TeamConfig, AgentConfig

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
# 允许通过环境变量重定向配置文件（测试隔离用）；默认 data/config.json
CONFIG_FILE = Path(os.environ.get("AGENTCHAT_CONFIG", ROOT / "data" / "config.json"))


def _new_id(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex[:8]


# ---- 全局设置（settings，存 config.json 顶层）----

def _read_json_file(path: str):
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def detect_claude_default_model() -> str:
    """探测 Claude Code 当前生效的默认模型名（用于左栏/配置展示，避免只显示「默认」占位）。

    优先级（高→低）：
      1. 进程环境变量 ANTHROPIC_MODEL / ANTHROPIC_DEFAULT_MODEL（claude-code 直接读取）
      2. ~/.claude/settings.json 的 env 块（用户在 settings 里统一注入 ANTHROPIC_MODEL 等，
         实际会被注入为进程环境变量 —— 这是最常见的「默认模型」落点）
      3. 同上文件的顶层 model 字段
      4. ~/.claude.json 的顶层 model 字段
    都读不到则返回空串（前端回退「默认」）。
    注意：这只用于「展示」，不影响实际拉起 claude 时用的后端——claude 子进程仍按自身配置决议。
    """
    for env in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_MODEL"):
        v = os.environ.get(env)
        if v:
            return v
    for p in ("~/.claude/settings.json", "~/.config/claude/settings.json"):
        data = _read_json_file(p)
        if not isinstance(data, dict):
            continue
        env_block = data.get("env")
        if isinstance(env_block, dict):
            if env_block.get("ANTHROPIC_MODEL"):
                return env_block["ANTHROPIC_MODEL"]
            if env_block.get("ANTHROPIC_DEFAULT_MODEL"):
                return env_block["ANTHROPIC_DEFAULT_MODEL"]
        if data.get("model"):
            return data["model"]
    d = _read_json_file("~/.claude.json")
    if isinstance(d, dict) and d.get("model"):
        return d["model"]
    return ""


def detect_codex_default_model() -> str:
    """探测 Codex CLI 当前生效的默认模型名（与 detect_claude_default_model 对称，用于左栏/配置展示）。

    优先级：环境变量 CODEX_MODEL / OPENAI_MODEL / OPENAI_API_MODEL >
    ~/.codex/config.toml 的 model 字段。都读不到则返回空串（前端回退「默认」）。
    注意：仅用于「展示」，不影响实际拉起 codex 子进程时的后端决议。
    """
    for env in ("CODEX_MODEL", "OPENAI_MODEL", "OPENAI_API_MODEL"):
        v = os.environ.get(env)
        if v:
            return v
    try:
        import tomllib
        with open(os.path.expanduser("~/.codex/config.toml"), "rb") as f:
            data = tomllib.load(f)
        m = data.get("model") if isinstance(data, dict) else None
        if m:
            return m
    except Exception:
        pass
    return ""


def get_settings() -> dict:
    d = _load()
    s = d.get("settings") or {}
    # 兜底默认值，保证后端始终有数
    pinned = s.get("default_model", "")
    pinned_codex = s.get("default_model_codex", "")
    return {
        "default_timeout": s.get("default_timeout", 300),
        "default_max_rounds": s.get("default_max_rounds", 12),
        # 默认模型：用户显式 pin 优先；否则自动探测 Claude Code / Codex 当前默认模型
        "default_model": pinned if pinned else detect_claude_default_model(),
        "default_model_codex": pinned_codex if pinned_codex else detect_codex_default_model(),
        "ollama_base_url": s.get("ollama_base_url", "http://localhost:11434"),
    }


def update_settings(settings: dict) -> dict:
    d = _load()
    d["settings"] = settings
    _save(d)
    return d["settings"]


def _load() -> dict:
    if not CONFIG_FILE.exists():
        return {"projects": {}, "teams": {}, "agents": {}}
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def _save(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---- agents ----
def list_agents() -> dict:
    return _load()["agents"]


def get_agent(aid: str) -> Optional[dict]:
    return _load()["agents"].get(aid)


def upsert_agent(a: AgentConfig) -> AgentConfig:
    if not a.id:
        a.id = _new_id("ag")
    d = _load()
    d["agents"][a.id] = a.model_dump()
    _save(d)
    return a


def delete_agent(aid: str) -> None:
    d = _load()
    d["agents"].pop(aid, None)
    _save(d)


# ---- teams ----
def list_teams() -> dict:
    return _load()["teams"]


def get_team(tid: str) -> Optional[dict]:
    return _load()["teams"].get(tid)


def upsert_team(t: TeamConfig) -> TeamConfig:
    if not t.id:
        t.id = _new_id("tm")
    d = _load()
    d["teams"][t.id] = t.model_dump()
    _save(d)
    return t


def delete_team(tid: str) -> None:
    d = _load()
    d["teams"].pop(tid, None)
    _save(d)


# ---- projects ----
def list_projects() -> dict:
    return _load()["projects"]


def get_project(pid: str) -> Optional[dict]:
    return _load()["projects"].get(pid)


def upsert_project(p: ProjectConfig) -> ProjectConfig:
    if not p.id:
        p.id = _new_id("pr")
    d = _load()
    d["projects"][p.id] = p.model_dump()
    _save(d)
    return p


def delete_project(pid: str) -> None:
    d = _load()
    d["projects"].pop(pid, None)
    _save(d)


# ---- 会话（session）：每个项目下可有多个会话，各自独立历史与共识记忆 ----
# 存储结构：<project>/.agentchat/sessions.json  ->  list[{
#   "id": "s_xxxx", "title": "...", "created_at": "...", "updated_at": "...",
#   "messages": [{round, speaker, role, content}, ...],
#   "agent_sessions": {"A": "<cli_session_id>", "B": "<cli_session_id>"},  # 各 agent 的持续 CLI 会话 id（1:1 续接）
#   "conclusion": "..."   # 最近一次收敛的结论（仅供前端展示，不回灌 agent）
# }]
def memory_dir(project_path: str) -> Path:
    p = Path(project_path) / ".agentchat"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sessions_file(project_path: str) -> Path:
    return memory_dir(project_path) / "sessions.json"


def _load_sessions(project_path: str) -> list:
    f = _sessions_file(project_path)
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_sessions(project_path: str, sessions: list) -> None:
    _sessions_file(project_path).write_text(
        json.dumps(sessions, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _now() -> str:
    import time as _t
    return _t.strftime("%Y-%m-%d %H:%M:%S")


def list_sessions(project_path: str) -> list:
    """返回会话元数据列表（按 updated_at 倒序），含 message_count 与 preview。"""
    out = []
    for s in _load_sessions(project_path):
        msgs = s.get("messages", [])
        preview = (msgs[0].get("content") or "")[:80] if msgs else ""
        out.append({
            "id": s["id"],
            "title": s.get("title", "会话"),
            "team_id": s.get("team_id", ""),
            "created_at": s.get("created_at", ""),
            "updated_at": s.get("updated_at", ""),
            "message_count": len(msgs),
            "preview": preview,
        })
    out.sort(key=lambda x: x["updated_at"], reverse=True)
    return out


def read_session(project_path: str, sid: str) -> Optional[dict]:
    for s in _load_sessions(project_path):
        if s["id"] == sid:
            return s
    return None


def create_session(project_path: str, title: str = "新会话", team_id: str | None = None) -> dict:
    sessions = _load_sessions(project_path)
    sid = _new_id("s")
    now = _now()
    s = {
        "id": sid,
        "title": title or "新会话",
        "team_id": team_id or "",
        "created_at": now,
        "updated_at": now,
        "messages": [],
        "agent_sessions": {},
        "conclusion": "",
    }
    sessions.append(s)
    _save_sessions(project_path, sessions)
    return s


def append_message(project_path: str, sid: str, entry: dict) -> None:
    sessions = _load_sessions(project_path)
    for s in sessions:
        if s["id"] == sid:
            s.setdefault("messages", []).append(entry)
            s["updated_at"] = _now()
            break
    else:
        # 容错：会话不存在则按 sid 新建并追加
        now = _now()
        sessions.append({
            "id": sid, "title": "会话", "created_at": now, "updated_at": now,
            "messages": [entry], "agent_sessions": {}, "conclusion": "",
        })
    _save_sessions(project_path, sessions)


def get_agent_sessions(project_path: str, sid: str) -> dict:
    """返回该会话下各 agent 的持续 CLI 会话 id 映射：{agent_name: cli_session_id}。"""
    s = read_session(project_path, sid)
    return s.get("agent_sessions", {}) if s else {}


def reconcile_agent_messages(project_path: str, sid: str, agent_name: str, cli_turns: list) -> dict:
    """用 CLI transcript 的 agent 回合（cli_turns: 有序 [{content, thinking}]）覆盖本地该 agent 的消息。

    对齐规则（agent 为准）：按本地 `speaker==agent_name` 的顺序逐条对齐 CLI 回合——
    - CLI 第 k 条有内容：覆盖本地第 k 条的 content/thinking（冲突以 agent 为准）；
    - CLI 多出本地没有的回合：追加到末尾（崩写/掉轮恢复）；
    - CLI 回合少于本地：删除本地末尾多余的 agent 消息（claude 外壳曾把一次回复拆成多条
      assistant 事件，旧解析把拆条各当一条回合、对账后本地多出残留——修复解析器后按此收敛）。
    仅当 cli_turns 非空才落盘；cli_turns 为 None/空（找不到 transcript = 离线）则保持本地不动。
    """
    if not cli_turns:
        return {"changed": 0, "appended": 0, "skipped": True}
    sessions = _load_sessions(project_path)
    changed = 0
    appended = 0
    for s in sessions:
        if s["id"] != sid:
            continue
        msgs = s.setdefault("messages", [])
        idxs = [i for i, m in enumerate(msgs) if m.get("speaker") == agent_name]
        role = msgs[idxs[0]].get("role", "") if idxs else ""
        for k, turn in enumerate(cli_turns):
            new_content = turn.get("content", "") or ""
            new_think = turn.get("thinking", "") or ""
            if k < len(idxs):
                m = msgs[idxs[k]]
                if (m.get("content") or "") != new_content:
                    m["content"] = new_content
                    changed += 1
                if (m.get("thinking") or "") != new_think:
                    m["thinking"] = new_think
                    changed += 1
            else:
                msgs.append({
                    "round": len(msgs), "speaker": agent_name, "role": role,
                    "content": new_content, "thinking": new_think,
                })
                appended += 1
        # CLI 回合少于本地：删除末尾多余的本地消息（拆条残留收敛；reverse 保下标正确）
        if len(cli_turns) < len(idxs):
            for i in sorted(idxs[len(cli_turns):], reverse=True):
                msgs.pop(i)
                changed += 1
        if changed or appended:
            s["updated_at"] = _now()
            _save_sessions(project_path, sessions)
        return {"changed": changed, "appended": appended, "skipped": False}
    return {"changed": 0, "appended": 0, "skipped": True}


def set_agent_session_id(project_path: str, sid: str, agent_name: str, cli_session_id: str) -> None:
    """记录某 agent 在本 app 会话内的 CLI 会话 id（首次创建时写入，resume 后续不变）。"""
    sessions = _load_sessions(project_path)
    for s in sessions:
        if s["id"] == sid:
            s.setdefault("agent_sessions", {})[agent_name] = cli_session_id
            s["updated_at"] = _now()
            break
    else:
        now = _now()
        sessions.append({
            "id": sid, "title": "会话", "created_at": now, "updated_at": now,
            "messages": [], "agent_sessions": {agent_name: cli_session_id}, "conclusion": "",
        })
    _save_sessions(project_path, sessions)


def get_original_topic(project_path: str, sid: str) -> str:
    """最初议题 = 第一条 role=='任务' 的 human 消息内容（续开新议题时回传给首个 agent 锚定）。"""
    s = read_session(project_path, sid)
    if not s:
        return ""
    for m in s.get("messages", []):
        if m.get("speaker") == "human" and m.get("role") == "任务":
            return (m.get("content") or "").strip()
    return ""


def set_discussion_conclusion(project_path: str, sid: str, conclusion: str) -> None:
    """收敛时记录本轮结论（仅供前端「结论」展示；状态式 agent 自身已记住，不再回灌 prompt）。"""
    sessions = _load_sessions(project_path)
    for s in sessions:
        if s["id"] == sid:
            s["conclusion"] = conclusion
            break
    else:
        now = _now()
        sessions.append({
            "id": sid, "title": "会话", "created_at": now, "updated_at": now,
            "messages": [], "agent_sessions": {}, "conclusion": conclusion,
        })
    _save_sessions(project_path, sessions)


def get_discussion_context(project_path: str, sid: str) -> dict:
    """续开新议题所需的上下文：最初议题 + 上轮结论。"""
    s = read_session(project_path, sid)
    if not s:
        return {"original_topic": "", "conclusion": ""}
    return {
        "original_topic": get_original_topic(project_path, sid),
        "conclusion": s.get("conclusion", ""),
    }


def delete_session(project_path: str, sid: str) -> None:
    sessions = [s for s in _load_sessions(project_path) if s["id"] != sid]
    _save_sessions(project_path, sessions)


# ---- 提示词日志（prompt log）：记录每轮发给 agent 的完整提示语，便于排查 ----
# 存储结构：<project>/.agentchat/prompts/<sid>.jsonl，每行一条 {round, agent, role, first_turn, ts, prompt}
def prompt_log_dir(project_path: str) -> Path:
    p = memory_dir(project_path) / "prompts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def append_prompt_log(project_path: str, sid: str, entry: dict) -> None:
    entry = dict(entry)
    entry.setdefault("ts", _now())
    f = prompt_log_dir(project_path) / f"{sid}.jsonl"
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_prompt_log(project_path: str, sid: str) -> list:
    f = prompt_log_dir(project_path) / f"{sid}.jsonl"
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out
