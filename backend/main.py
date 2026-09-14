import asyncio
import json
import uuid
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import FileResponse

from . import store
from . import executor
from .models import AgentConfig, TeamConfig, ProjectConfig, AppSettings
from .orchestrator import Session, TONE_STYLES
from . import gateway

app = FastAPI(title="Agent CLI Orchestrator")
ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"


@app.middleware("http")
async def _rewrite_claude_tool_roundtrip_url(request: Request, call_next):
    """兜底 claude-code 2.1.212 的工具往返 URL bug：模型发出 tool_use 后，claude 的续接请求
    （把 tool_result 回传）会把 ANTHROPIC_BASE_URL 整体编码进 path——实测形如
    `POST /http%3A//127.0.0.1%3A8777/v1/messages?beta=true` → 本服务 404，导致工具调用
    永远拿不到结果（claude 报「There's an issue with the selected model」退出）。
    识别该模式并改写回真实的 path，保证工具往返走通（网关对 tool_result 处理本就正常）。

    会话级思考开关把 sid 编进了 base_url 路径（如 `/gw/<pid>/<sid>/v1/messages`），
    工具往返的编码 URL 形如 `/http%3A//127.0.0.1%3A8777/gw/<pid>/<sid>/v1/messages?beta=true`，
    这里用 urlparse 提取其中的 path 部分（保留 /gw/... 前缀）再改写，确保会话路径不丢。
    """
    from urllib.parse import unquote, urlparse

    path = request.url.path
    if path.startswith("/http%3A") or path.startswith("/http:"):
        decoded = unquote(path)
        parsed = urlparse(decoded)
        if parsed.path and "/v1/messages" in parsed.path:
            request.scope["path"] = parsed.path
            request.scope["raw_path"] = parsed.path.encode()
    return await call_next(request)

clients: set[WebSocket] = set()
# 会话注册表：sid -> Session。多会话可并行——每个会话独立 running/stop/pause/inject，
# 只锁自己，互不干扰（事件均带 sid，前端按会话归属渲染）。
sessions: dict[str, Session] = {}

ACTION_CN = {"stop": "停止", "pause": "暂停", "resume": "继续", "inject": "介入"}


async def broadcast(event: dict):
    data = json.dumps(event, ensure_ascii=False)
    for ws in list(clients):
        try:
            await ws.send_text(data)
        except Exception:
            clients.discard(ws)


# ---------------- 配置 CRUD ----------------
@app.get("/api/config")
async def get_config():
    return {
        "projects": list(store.list_projects().values()),
        "teams": list(store.list_teams().values()),
        "agents": list(store.list_agents().values()),
        "settings": store.get_settings(),
    }


@app.post("/api/settings")
async def save_settings(s: AppSettings):
    store.update_settings(s.model_dump())
    return {"ok": True, "settings": store.get_settings()}


# ---------------- 目录浏览（项目目录选择器） ----------------
@app.get("/api/fs")
async def list_dir(path: str = ""):
    root = Path(path).expanduser() if path else Path.home()
    if not root.exists() or not root.is_dir():
        raise HTTPException(status_code=400, detail="无效目录：" + str(root))
    dirs = []
    for e in sorted(root.iterdir()):
        try:
            if e.is_dir() and not e.name.startswith("."):
                dirs.append({"name": e.name, "path": str(e)})
        except PermissionError:
            continue
    parent = str(root.parent) if root != root.parent else None
    return {"path": str(root), "parent": parent, "dirs": dirs}


@app.post("/api/agents")
async def post_agent(a: AgentConfig):
    if not a.id:
        a.id = "ag_" + uuid.uuid4().hex[:8]
    return store.upsert_agent(a).model_dump()


@app.delete("/api/agents/{aid}")
async def del_agent(aid: str):
    store.delete_agent(aid)
    return {"ok": True}


@app.post("/api/teams")
async def post_team(t: TeamConfig):
    if not t.id:
        t.id = "tm_" + uuid.uuid4().hex[:8]
    return store.upsert_team(t).model_dump()


@app.delete("/api/teams/{tid}")
async def del_team(tid: str):
    store.delete_team(tid)
    return {"ok": True}


@app.post("/api/projects")
async def post_project(p: ProjectConfig):
    if not p.id:
        p.id = "pr_" + uuid.uuid4().hex[:8]
    return store.upsert_project(p).model_dump()


@app.delete("/api/projects/{pid}")
async def del_project(pid: str):
    store.delete_project(pid)
    return {"ok": True}


# ---------------- 会话控制（走 WS） ----------------
async def handle_start(msg: dict):
    sid = msg.get("session_id")
    # 只锁本会话：同一会话正在运行时拒绝重复发起；其他会话不受影响（可并行）
    if sid and sessions.get(sid) and sessions[sid].running:
        await broadcast({"type": "error", "text": "该会话正在运行，请等待本轮结束或点「停止」后再发。"})
        return
    proj = store.get_project(msg["project_id"])
    team = store.get_team(msg["team_id"])
    if not proj or not team:
        await broadcast({"type": "error", "text": "项目或 Team 不存在"})
        return
    agents = []
    for aid in team["agent_ids"]:
        ad = store.get_agent(aid)
        if ad:
            agents.append(AgentConfig(**ad))
    if len(agents) < 1:
        await broadcast({"type": "error", "text": "Team 内无可用的 agent"})
        return
    # 空议题防御：辩论/对话模式都不允许空内容发起，避免 agent 空跑一轮
    # （前端已对空输入禁用按钮，这里再做一层服务端拦截）
    task = (msg.get("task") or "").strip()
    if not task:
        await broadcast({"type": "error", "text": "请输入议题/消息后再发起（空内容不会让 agent 空跑）。"})
        return
    # 轮次是「这场讨论」的属性，由发起讨论时配置，不取 agent 上的值
    # 前端 composer 默认会带上全局设置的 default_max_rounds；此处兜底也用全局默认，保证一致
    settings = store.get_settings()
    max_rounds = int(msg.get("max_rounds", settings.get("default_max_rounds", 12)))
    # 单轮超时取全局设置（已不在 agent 上）
    timeout = settings.get("default_timeout", 300)
    # 讨论调性（可选）：发起时选定，注入每轮 prompt；持久化到会话（历史续接保持同调）
    tone = (msg.get("tone") or "").strip()
    if tone and tone not in TONE_STYLES:
        tone = ""  # 未知调名忽略（防脏数据）
    if not sid:
        title = (msg.get("task") or "").strip()[:24] or "新会话"
        sid = store.create_session(proj["path"], title, msg.get("team_id"))["id"]
    if tone:
        store.set_session_tone(proj["path"], sid, tone)
    # 持久化发起/续跑实际选用的 Team：首轮若用户在 composer 切了 Team（或「新建会话」弹窗选了
    # 非默认 Team）必须落库，否则 stored team_id 停留在创建默认 teams[0]，重开会话会被静默降级成
    # 2 agent（实测五子棋会话：首跑 3g 但 stored=2g，续跑无裁判）。每次发起都按当前选择写回。
    store.update_session_team(proj["path"], sid, msg.get("team_id"))
    sess = Session(proj["path"], sid, agents, max_rounds, broadcast, timeout=timeout, tone=tone,
                   project_id=proj["id"])
    sessions[sid] = sess  # 注册；结束后保留（running=False，可继续/复用）
    asyncio.create_task(sess.start(msg.get("task", "")))


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            action = msg.get("action")
            if action == "start":
                await handle_start(msg)
            elif action in ("stop", "pause", "resume", "inject"):
                # 按 session_id 路由到对应会话（只操作目标会话，其他会话并行不受影响）
                sid = msg.get("session_id")
                s = sessions.get(sid) if sid else None
                if not (s and s.running):
                    await broadcast({
                        "type": "error",
                        "text": f"当前没有正在运行的会话，「{ACTION_CN.get(action, action)}」无效。"
                                "请先点「开始讨论」/「发送」。",
                    })
                elif s.mode == "chat":
                    await broadcast({
                        "type": "error",
                        "text": f"单 agent 对话模式下「{ACTION_CN.get(action, action)}」不适用——"
                                "直接在输入框发下一条消息即可。",
                    })
                elif action == "stop":
                    s.stop()
                elif action == "pause":
                    s.pause()
                elif action == "resume":
                    s.resume()
                elif action == "inject":
                    text = (msg.get("text") or "").strip()
                    if not text:
                        await broadcast({"type": "error", "text": "介入内容为空。"})
                    else:
                        await s.inject(text)
                        await broadcast({"type": "system", "msg": "已排队人工介入，将在本轮结束后插入。"})
            elif action == "config" and msg.get("op") == "reload":
                await ws.send_text(json.dumps(await get_config(), ensure_ascii=False))
    except WebSocketDisconnect:
        clients.discard(ws)


# ---------------- 会话（session）管理 ----------------
@app.get("/api/sessions/{pid}")
async def list_sessions(pid: str):
    proj = store.get_project(pid)
    if not proj:
        return {"sessions": []}
    return {"sessions": store.list_sessions(proj["path"])}


@app.post("/api/sessions/{pid}")
async def create_session(pid: str, body: dict | None = None):
    proj = store.get_project(pid)
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")
    title = (body or {}).get("title") or "新会话"
    team_id = (body or {}).get("team_id")
    s = store.create_session(proj["path"], title, team_id)
    return {"session": s}


@app.get("/api/session/{pid}/{sid}")
async def get_session(pid: str, sid: str):
    proj = store.get_project(pid)
    if not proj:
        return {"messages": [], "agent_sessions": {}}
    s = store.read_session(proj["path"], sid)
    if not s:
        return {"messages": [], "agent_sessions": {}, "title": "", "exists": False}
    # 打开会话时：若 agent CLI 会话 transcript 可达，用 agent 历史覆盖本地（冲突以 agent 为准）；
    # 找不到 transcript（agent 不可连 / 全新会话）则保持本地不动，作为离线回退。
    _reconcile_with_cli(proj["path"], sid, s.get("agent_sessions", {}))
    s = store.read_session(proj["path"], sid)  # 重新读回对账后的最新消息
    # 会话级「思考」开关的当前生效值：会话显式设置过用会话值，否则回退全局默认
    # （前端据此初始化开关状态；切换时写入显式值）。
    think = s.get("ollama_think")
    if think is None:
        think = bool((store.get_settings()).get("ollama_think", True))
    return {
        "id": s["id"], "title": s.get("title", "会话"),
        "team_id": s.get("team_id", ""),
        "messages": s.get("messages", []),
        "agent_sessions": s.get("agent_sessions", {}),
        "think": think,
        "exists": True,
    }


def _reconcile_with_cli(project_path: str, sid: str, agent_sessions: dict) -> dict:
    """对每个持有 CLI 会话 id 的 agent，读取其 transcript 并与本地对账覆盖。返回各 agent 变更汇总。"""
    summary = {}
    for agent_name, cli_session_id in (agent_sessions or {}).items():
        cli_turns = executor.read_cli_transcript(cli_session_id, agent_name)
        if not cli_turns:  # None(无 transcript/离线) 或 [] (空文件) → 不动本地
            continue
        res = store.reconcile_agent_messages(project_path, sid, agent_name, cli_turns)
        if res.get("changed") or res.get("appended"):
            summary[agent_name] = res
    return summary


@app.post("/api/session/{pid}/{sid}/reconcile")
async def reconcile_session(pid: str, sid: str):
    """手动触发：用各 agent 的 CLI transcript 重新覆盖本地对话。"""
    proj = store.get_project(pid)
    if not proj:
        return {"ok": False, "error": "project not found"}
    s = store.read_session(proj["path"], sid)
    if not s:
        return {"ok": False, "error": "session not found"}
    summary = _reconcile_with_cli(proj["path"], sid, s.get("agent_sessions", {}))
    return {"ok": True, "summary": summary}


@app.get("/api/session/{pid}/{sid}/prompts")
async def get_prompt_log(pid: str, sid: str):
    """返回该会话的提示词日志（每轮发给 agent 的完整 prompt），按记录顺序。"""
    proj = store.get_project(pid)
    if not proj:
        return {"prompts": []}
    return {"prompts": store.read_prompt_log(proj["path"], sid)}


@app.get("/api/session/{pid}/{sid}/tools")
async def get_tool_log(pid: str, sid: str):
    """返回该会话的工具调用日志（end_discussion 每次调用：rejected/proposed/repeat/confirmed）。"""
    proj = store.get_project(pid)
    if not proj:
        return {"tools": []}
    return {"tools": store.read_tool_log(proj["path"], sid)}


@app.post("/api/session/{pid}/{sid}/title")
async def rename_session(pid: str, sid: str, body: dict | None = None):
    """修改会话标题（行内编辑保存）。"""
    proj = store.get_project(pid)
    if not proj:
        return {"ok": False, "error": "项目不存在"}
    title = (body or {}).get("title", "")
    if not (title or "").strip():
        return {"ok": False, "error": "标题不能为空"}
    if store.set_session_title(proj["path"], sid, title):
        return {"ok": True, "title": (title or "").strip()[:60]}
    return {"ok": False, "error": "会话不存在"}


@app.post("/api/session/{pid}/{sid}/think")
async def set_session_think(pid: str, sid: str, body: dict | None = None):
    """会话级「思考」开关：写入会话 ollama_think，覆盖全局默认。"""
    proj = store.get_project(pid)
    if not proj:
        return {"ok": False, "error": "项目不存在"}
    think = bool((body or {}).get("think", True))
    if store.set_session_think(proj["path"], sid, think):
        return {"ok": True, "think": think}
    return {"ok": False, "error": "会话不存在"}


# 全局会话列表（跨项目扁平化），供前端左侧「会话」主导航使用；
# 每条附带 project_id / project_name / team_id / team_name，按 updated_at 倒序。
@app.get("/api/sessions")
async def list_all_sessions():
    out = []
    for pid, p in store.list_projects().items():
        for s in store.list_sessions(p["path"]):
            item = dict(s)
            item["project_id"] = pid
            item["project_name"] = p.get("name", "")
            tid = s.get("team_id", "")
            tm = store.get_team(tid) if tid else None
            item["team_name"] = (tm or {}).get("name", "")
            out.append(item)
    out.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return {"sessions": out}


@app.delete("/api/session/{pid}/{sid}")
async def del_session(pid: str, sid: str):
    proj = store.get_project(pid)
    if not proj:
        return {"ok": True}
    store.delete_session(proj["path"], sid)
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(
        FRONTEND / "index.html",
        headers={"Cache-Control": "no-store"},
    )


# ---------------- 内置 Anthropic↔Ollama 翻译网关 ----------------
# claude-code 类型 agent 把 base_url 指向本后端（如 http://127.0.0.1:8777），
# 即可直连 Ollama 上已 pull 的本地模型（gemma/deepseek 等），无需另起 litellm、不改 Ollama 配置。
@app.post("/v1/messages")
async def anthropic_messages(req: Request):
    return await gateway.handle_messages(req, store.get_settings())


# 会话级思考开关：把 sid 编进 base_url 路径（/gw/<pid>/<sid>/v1/messages），
# 网关据此按会话读取 ollama_think 覆盖全局默认。claude-code 的 ANTHROPIC_BASE_URL
# 由编排层按会话拼成此路径（见 orchestrator.Session._gw_url）。
@app.post("/gw/{pid}/{sid}/v1/messages")
async def anthropic_messages_session(req: Request, pid: str, sid: str):
    proj = store.get_project(pid)
    project_path = proj["path"] if proj else pid
    return await gateway.handle_messages(req, store.get_settings(), project_path=project_path, sid=sid)
