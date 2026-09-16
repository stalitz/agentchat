import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import ProjectConfig, TeamConfig, AgentConfig
from .prompt_defaults import PROMPT_TEMPLATES_DEBATE

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
        "ollama_num_ctx": int(s.get("ollama_num_ctx", 16384)),
        # 兼容 #104 旧字段 ollama_think（旧 config.json 只写了它）
        "think": bool(s.get("think", s.get("ollama_think", True))),
        "claude_max_output_tokens": int(s.get("claude_max_output_tokens", 64000)),
        "prompt_templates": s.get("prompt_templates") or {},
        # 提示词默认模板（供前端「显示默认 / 恢复默认」）：与后端实际生效的缺省值同源
        "prompt_template_defaults": {"debate": dict(PROMPT_TEMPLATES_DEBATE)},
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


def _claude_ts_to_local(iso: str):
    """claude transcript 的 UTC ISO 时间戳 -> 本地 `%Y-%m-%d %H:%M:%S`，与 messages.ts 同格式。

    reconcile 从 CLI transcript 取回 agent 回合时，回合自带 claude 原生 timestamp（UTC ISO）。
    转成本地格式回填 messages.ts，使重开会话带真实时间，且与时序排序（按 ts 字符串比较）一致。
    解析失败返回 None（调用方保持原 ts 不动）。"""
    if not iso:
        return None
    try:
        s = iso.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _reorder_by_ts(msgs: list) -> bool:
    """按 ts 稳定排序（无 ts 排末尾，保相对序），并重排 round=下标。返回是否发生位置/round 变化。

    治本修复：API-error 时 orchestrator 不落 agent 最后一轮，reconcile 把缺失轮 append 到数组末尾
    （落在「会话结束」系统消息之后）造成错序；按 claude 时间戳把缺失轮归位到正确时序。"""
    ordered = sorted(msgs, key=lambda m: (0 if m.get("ts") else 1, m.get("ts") or ""))
    changed = any(ordered[i] is not msgs[i] for i in range(len(ordered)))
    for i, m in enumerate(ordered):
        if m.get("round") != i:
            m["round"] = i
            changed = True
    msgs[:] = ordered
    return changed



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


def update_session_team(project_path: str, sid: str, team_id: str) -> None:
    """持久化会话实际选用的 Team。

    发起/续跑时用户可能在 composer 切换 Team（或「新建会话」弹窗选了非默认 Team），该选择
    必须落库——否则 stored team_id 停留在创建默认 teams[0]，重开会话会被静默降级成 2 agent
    （实测五子棋会话：首跑 3g 但 stored=2g，续跑无裁判）。每次发起都按当前选择写回。"""
    sessions = _load_sessions(project_path)
    for s in sessions:
        if s["id"] == sid:
            s["team_id"] = team_id or ""
            s["updated_at"] = _now()
            break
    else:
        return
    _save_sessions(project_path, sessions)


def append_message(project_path: str, sid: str, entry: dict) -> None:
    sessions = _load_sessions(project_path)
    # 每条消息落盘时自动带时间戳（ts，本地时间）——前端时间线展示；历史数据由
    # backend/backfill_timestamps.py 从 claude transcript 回填。
    entry = dict(entry)
    entry.setdefault("ts", _now())
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


def _similar_content(a: str, b: str) -> bool:
    """宽松内容相似：去 '---' 分隔符噪声后，任一方前 30 字出现在对方里。

    用于数量不一致时把 CLI 回合匹配到本地同内容条目（gemma 模板答案重复也能正确配对，
    避免按顺序覆盖把 CLI 回合塞进无关空位）。"""
    a = (a or "").strip().lstrip("-").strip()
    b = (b or "").strip().lstrip("-").strip()
    if not a or not b:
        return False
    return b[:30] in a or a[:30] in b


# claude 外壳把 API 失败 / resume 异常渲染成普通文本（与 orchestrator._looks_like_claude_error 同源）；
# 这些不是有效发言，reconcile 对账时应直接剔除（否则错误文本会从 transcript 被带回/追加为发言）。
_CLAUDE_ERROR_PREFIXES = ("API Error:", "No response requested.", "Request Failed:")


def _is_claude_error_text(text: str) -> bool:
    t = (text or "").strip()
    return any(t.startswith(p) for p in _CLAUDE_ERROR_PREFIXES)


_TOOL_BLOCK_TYPES = ("tool_use", "tool_result")


def _merge_tool_blocks(new_blocks: list, old_blocks: list) -> list:
    """CLI transcript 缺工具块时，把本地已记录的工具块按相对位置并回新 blocks。

    仅在「本地有工具块、CLI 侧一个都没有」时触发；CLI 侧带工具块则以 CLI 为准（避免重复）。
    插入点取本地「首个工具块之前的非工具块数量」在新序列中的同位——覆盖 claude 的常见形态
    （思考 → 正文 → 文本兜底调用）。
    """
    if not new_blocks or not old_blocks:
        return new_blocks
    if any(b.get("type") in _TOOL_BLOCK_TYPES for b in new_blocks):
        return new_blocks
    tools = [b for b in old_blocks if b.get("type") in _TOOL_BLOCK_TYPES]
    if not tools:
        return new_blocks
    lead = 0
    for b in old_blocks:
        if b.get("type") in _TOOL_BLOCK_TYPES:
            break
        lead += 1
    pos = min(lead, len(new_blocks))
    return list(new_blocks[:pos]) + tools + list(new_blocks[pos:])


def _merge_tool_info(msg: dict, turn: dict) -> bool:
    """把 CLI 回合的工具信息并入本地消息，返回是否发生变更。

    **空值 ≠「该轮确实没调用工具」，故不做空值覆盖**：
    - CLI 回合带 tool_calls / tool_results → 以 CLI 为准（覆盖）；
    - CLI 回合为空 → 保留本地已有值。claude 走「prompt 文本 json」兜底解析出的 tool_use
      本就不在它自己的 transcript 里（模型把调用写进了正文），覆盖会把这条留痕抹掉
      （实测 s_f85043cf：claude 的终止提议在气泡里查不到工具块）。
    - blocks：CLI 侧无工具块时按相对位置并回本地工具块，保证气泡内可见。
    """
    changed = False
    new_tc = turn.get("tool_calls") or []
    new_tr = turn.get("tool_results") or []
    if new_tc and (msg.get("tool_calls") or []) != new_tc:
        msg["tool_calls"] = new_tc
        changed = True
    if new_tr and (msg.get("tool_results") or []) != new_tr:
        msg["tool_results"] = new_tr
        changed = True
    merged = _merge_tool_blocks(turn.get("blocks") or [], msg.get("blocks") or [])
    if (msg.get("blocks") or []) != merged:
        msg["blocks"] = merged
        changed = True
    return changed


def reconcile_agent_messages(project_path: str, sid: str, agent_name: str, cli_turns: list) -> dict:
    """用 CLI transcript 的 agent 回合（cli_turns: 有序 [{content, thinking}]）覆盖本地该 agent 的消息。

    对齐规则（agent 为准）：
    - **数量一致**（CLI 回合数 == 本地该 agent 消息数）：按顺序覆盖 content/thinking（冲突以 agent 为准）；
    - **数量不一致**：**严禁顺序覆盖**（会把 CLI 回合复制进本地无关空位造成重复——实测 s_2289e1cf
      cli 3 条 vs 本地 7 条，顺序覆盖后 T1/T2/T3 各出现 2 次）。改为**内容匹配覆盖**：CLI 回合只覆盖
      本地**内容相同**的条目；CLI 多出本地没有的（且本地无匹配）→ 追加（崩写/掉轮恢复）；
      CLI 少于本地时，未匹配的 CLI 回合跳过、本地多余条目仅删「think-only 残留」（content 空、
      thinking 非空），**有 content 的条目一律保留**（transcript 可能因 auto-compact / 截断而不完整，
      盲删会丢真实历史）。
    仅当 cli_turns 非空才落盘；cli_turns 为 None/空（找不到 transcript = 离线）则保持本地不动。
    """
    if not cli_turns:
        return {"changed": 0, "appended": 0, "skipped": True}
    # 剔除「错误文本」回合（API Error / No response requested. / Request Failed:）——
    # 它们是 claude 外壳渲染的失败输出，不是有效发言，不得被对账回本地当发言
    # （s_54d1232e 曾因 transcript 含错误文本而被带回）。
    # 但**不静默丢弃**：整条丢掉后会话会变成「只有提问、没有任何回应」的空白，看不出是运行失败
    # 还是漏落。改为转成可见的系统留痕（err_turns → 下方落盘段写 speaker="system"）。
    err_turns = [t for t in cli_turns if _is_claude_error_text(t.get("content", ""))]
    cli_turns = [t for t in cli_turns if not _is_claude_error_text(t.get("content", ""))]
    sessions = _load_sessions(project_path)
    changed = 0
    appended = 0
    for s in sessions:
        if s["id"] != sid:
            continue
        msgs = s.setdefault("messages", [])
        idxs = [i for i, m in enumerate(msgs) if m.get("speaker") == agent_name]
        role = msgs[idxs[0]].get("role", "") if idxs else ""
        # 被中止（interrupted）的发言是「作废但留痕」的记录：覆盖时只改内容/思考/工具/时间，
        # 不碰 interrupted 标记本身（下面各处赋值均按字段写，天然保留）。
        # 但**禁止在存在中止记录时追加**：中止的那一轮在 CLI transcript 里可能是半截内容
        # （与我们留的半截不完全一致 → 内容匹配不上），照常追加会凭空多出一个重复气泡。
        has_interrupted = any(m.get("interrupted") for m in msgs if m.get("speaker") == agent_name)
        if not cli_turns:
            # 该 agent 的 CLI 记录里**全是报错回合**（无有效发言）：只落系统留痕，
            # 不动本地已记录的发言（原语义：CLI 侧为空则保持本地不动）。
            pass
        elif len(cli_turns) == len(idxs):
            # 数量一致：按顺序覆盖（原语义，以 agent 为准修正内容/思考）
            for k, turn in enumerate(cli_turns):
                new_content = turn.get("content", "") or ""
                new_think = turn.get("thinking", "") or ""
                m = msgs[idxs[k]]
                if (m.get("content") or "") != new_content:
                    m["content"] = new_content
                    changed += 1
                if (m.get("thinking") or "") != new_think:
                    m["thinking"] = new_think
                    changed += 1
                # 工具信息**不做空值覆盖**（见 _merge_tool_info 说明）：claude 走「prompt 文本
                # json」兜底解析出的 tool_use 不在它自己的 transcript 里，用空值覆盖会把这条
                # 留痕连同气泡内的工具块一起抹掉（实测 s_f85043cf：claude 的终止提议气泡里查不到）。
                if _merge_tool_info(m, turn):
                    changed += 1
                # 回填 claude 真实时间（仅补缺失，避免每次重开改写已有本地 ts）
                new_ts = _claude_ts_to_local(turn.get("ts"))
                if new_ts and not m.get("ts"):
                    m["ts"] = new_ts
                    changed += 1
        else:
            # 数量不一致：严禁顺序覆盖（会把 CLI 回合复制进本地无关空位造成重复——
            # s_2289e1cf 实测 cli 3 vs 本地 7，顺序覆盖后 T1/T2/T3 各出现 2 次）。
            # 只做内容匹配覆盖；CLI 多出且本地无匹配才追加；未匹配的 CLI 回合跳过。
            matched: set = set()
            for turn in cli_turns:
                new_content = turn.get("content", "") or ""
                new_think = turn.get("thinking", "") or ""
                hit = None
                if new_content:
                    for j in idxs:
                        if j in matched:
                            continue
                        if _similar_content(msgs[j].get("content") or "", new_content):
                            hit = j
                            break
                if hit is None:
                    if len(cli_turns) > len(idxs) and not has_interrupted:
                        msgs.append({
                            "round": len(msgs), "speaker": agent_name, "role": role,
                            "content": new_content, "thinking": new_think,
                            "tool_calls": turn.get("tool_calls", []) or [],
                            "tool_results": turn.get("tool_results", []) or [],
                            "ts": _claude_ts_to_local(turn.get("ts")),
                        })
                        appended += 1
                    continue
                m = msgs[hit]
                if (m.get("content") or "") != new_content:
                    m["content"] = new_content
                    changed += 1
                if (m.get("thinking") or "") != new_think:
                    m["thinking"] = new_think
                    changed += 1
                # 工具信息不做空值覆盖（同上分支，见 _merge_tool_info）
                if _merge_tool_info(m, turn):
                    changed += 1
                # 回填 claude 真实时间（仅补缺失）
                new_ts = _claude_ts_to_local(turn.get("ts"))
                if new_ts and not m.get("ts"):
                    m["ts"] = new_ts
                    changed += 1
                matched.add(hit)
            # CLI 回合少于本地：删除「未被匹配的 think-only 残留」（content 空、thinking 非空）——
            # 拆条修复后本地多出的"思考空答案"一半。有 content 的条目是真实历史，
            # 可能因 transcript 不完整（auto-compact/截断）而比 cli 多，绝不盲删（reverse 保下标正确）。
            if len(cli_turns) < len(idxs):
                unmatched = [i for i in idxs if i not in matched]
                for i in sorted(unmatched, reverse=True):
                    m = msgs[i]
                    # 中止记录不删：它是「中间出过错」的留痕（用户明确要求落盘可见）
                    if m.get("interrupted"):
                        continue
                    if (m.get("thinking") or "") and not (m.get("content") or ""):
                        msgs.pop(i)
                        changed += 1
        # CLI 报错回合 → 可见的系统留痕（**幂等**：每次都按当前 CLI 记录重建，先清掉上次派生的）。
        # 会话因此显示「xx 本次运行失败（CLI 报错…）」而不是留白；错误原文保留，便于定位
        # 是 API 限额 / 网络 / 代理问题（如 "API returned an empty or malformed response"）。
        old_err = [(m.get("content"), m.get("ts")) for m in msgs if m.get("sys_error_of")]
        msgs[:] = [m for m in msgs if not m.get("sys_error_of")]
        new_err = [{
            "round": len(msgs),
            "speaker": "system",
            "role": "系统",
            "content": (f"{agent_name} 本次运行失败（CLI 报错，无有效输出）：\n\n"
                        f"{(t.get('content') or '').strip()}"),
            "ts": _claude_ts_to_local(t.get("ts")),
            "sys_error_of": agent_name,   # 派生标记：重跑对账时据此清理重建，避免重复堆叠
        } for t in err_turns]
        if [(m["content"], m["ts"]) for m in new_err] != old_err:
            changed += 1
        msgs.extend(new_err)
        # 治本：按 ts 稳定排序兜底，归位 API-error 缺失轮（reconcile 曾 append 到「会话结束」之后）
        if _reorder_by_ts(msgs):
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


def set_pending_turn(project_path: str, sid: str, info: dict) -> None:
    """记录「待续接」的发言：某 agent 的一次发言被中止，等用户点「继续」时由它重新发言。

    info = {"agent": 名称, "round_i": 该次发言的轮次下标}。存 session 级字段（而非从
    messages 推导）：一轮的 round_i 会因偶发空轮（未落盘的 agent 发言）与「有效发言条数」
    发生偏移，推导会把续接指到错误的 agent 上；记录实测值才是确定式的。
    """
    sessions = _load_sessions(project_path)
    for s in sessions:
        if s["id"] == sid:
            s["pending_turn"] = dict(info or {})
            s["updated_at"] = _now()
            break
    else:
        return
    _save_sessions(project_path, sessions)


def get_pending_turn(project_path: str, sid: str) -> Optional[dict]:
    """读「待续接」记录；无则返回 None（前端据此决定主按钮是否显示「继续」）。"""
    s = read_session(project_path, sid)
    info = (s or {}).get("pending_turn") or {}
    return info if info.get("agent") else None


def clear_pending_turn(project_path: str, sid: str) -> None:
    """清除待续接记录（被一次有效发言兑现，或被新议题顶掉）。幂等：无记录时不动。"""
    sessions = _load_sessions(project_path)
    for s in sessions:
        if s["id"] == sid:
            if s.pop("pending_turn", None) is None:
                return
            s["updated_at"] = _now()
            break
    else:
        return
    _save_sessions(project_path, sessions)


def set_session_tone(project_path: str, sid: str, tone: str) -> None:
    """记录会话的讨论调性（发起讨论时选定；历史会话续接保持同调）。"""
    sessions = _load_sessions(project_path)
    for s in sessions:
        if s["id"] == sid:
            s["tone"] = tone
            s["updated_at"] = _now()
            break
    _save_sessions(project_path, sessions)


def set_session_think(project_path: str, sid: str, think: bool) -> bool:
    """记录会话级「思考」开关（覆盖全局默认，优先级高于 agent 级与全局）；返回是否找到该会话。

    全局开关（AppSettings.think）作用于全部 agent；agent 级（AgentConfig.think）覆盖全局；
    会话级最高。Ollama 经本网关时网关据此带 think:false；codex 降 model_reasoning_effort；
    真实 claude 直连无干净开关（仅经网关路由的 Ollama 真正生效）。"""
    sessions = _load_sessions(project_path)
    found = False
    for s in sessions:
        if s["id"] == sid:
            s["think"] = bool(think)
            s["updated_at"] = _now()
            found = True
            break
    if found:
        _save_sessions(project_path, sessions)
    return found


def resolve_think(agent: "AgentConfig", session: dict, settings: dict) -> bool:
    """解析某 agent 在某会话下的「思考」生效值（三级：会话 > agent > 全局）。

    - 全局：settings.get("think", True)
    - agent 级：agent.think（None=继承全局）
    - 会话级：session 的 "think"（兼容旧字段 "ollama_think"），None=未显式设置则回退
    优先级：会话级 > agent 级 > 全局（最具体者胜）。"""
    app = bool((settings or {}).get("think", True))
    ag = (agent or {}).get("think") if isinstance(agent, dict) else getattr(agent, "think", None)
    agent_val = None if ag is None else bool(ag)
    sess_raw = (session or {}).get("think")
    if sess_raw is None:
        sess_raw = (session or {}).get("ollama_think")  # 兼容 #104 旧字段
    sess_val = None if sess_raw is None else bool(sess_raw)
    if sess_val is not None:
        return sess_val
    if agent_val is not None:
        return agent_val
    return app


def set_session_title(project_path: str, sid: str, title: str) -> bool:
    """修改会话标题；返回是否找到该会话。"""
    title = (title or "").strip()
    if not title:
        return False
    sessions = _load_sessions(project_path)
    found = False
    for s in sessions:
        if s["id"] == sid:
            s["title"] = title[:60]  # 防超长标题撑爆 UI
            s["updated_at"] = _now()
            found = True
            break
    if found:
        _save_sessions(project_path, sessions)
    return found


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


# ---- 工具调用日志（tool log）：记录每次 end_discussion 调用（无论被拒/提议/确认），便于排查 ----
# 存储结构：<project>/.agentchat/tools/<sid>.jsonl，每行一条
# {ts, agent, round_i, action(rejected/proposed/repeat/confirmed), reason, msg}
def tool_log_dir(project_path: str) -> Path:
    p = memory_dir(project_path) / "tools"
    p.mkdir(parents=True, exist_ok=True)
    return p


def append_tool_log(project_path: str, sid: str, entry: dict) -> None:
    entry = dict(entry)
    entry.setdefault("ts", _now())
    f = tool_log_dir(project_path) / f"{sid}.jsonl"
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_tool_log(project_path: str, sid: str) -> list:
    f = tool_log_dir(project_path) / f"{sid}.jsonl"
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
