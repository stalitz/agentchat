import asyncio
import glob
import json
import os
import re
import shutil
import signal
import sys
import time
import uuid
from pathlib import Path

from .models import AgentConfig
from .gateway import THINK_SENT, ANSW_SENT


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in (name or "agent"))


_MCP_SERVER_CONFIG = {
    "mcpServers": {
        "terminate-signal": {
            "command": "/usr/local/bin/python3",
            "args": ["{root}/backend/terminate_signal_mcp.py"],
            "env": {"TERMINATE_STATE_FILE": "/tmp/agentchat_terminate_state.json"},
        }
    }
}

# claude 用的 MCP 服务脚本（纯 stdlib，无第三方依赖）。codex 侧复用同一个脚本，
# 见 AgentRuntime._codex_cmd 的 `-c mcp_servers.*` 注入。
_MCP_SCRIPT = str(Path(__file__).resolve().parent / "terminate_signal_mcp.py")


def _user_frame(prompt: str) -> bytes:
    """stream-json 输入帧：把一轮 prompt 封装为 user 消息（SDK 同款格式）。

    结尾必须带换行：stream-json 输入是**按行**解析的 NDJSON。原先因为写完就关 stdin，
    靠 EOF 断行才没暴露；现在 stdin 要保持打开（给中断帧留通道），缺换行会让 claude
    一直等这一行写完、首轮就卡死。SDK 自身也是 `JSON.stringify(msg) + "\\n"`。
    """
    return (json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
    }, ensure_ascii=False) + "\n").encode("utf-8")


def _ensure_project_mcp(cwd: str, config_dir: str | None = None) -> None:
    """确保项目目录存在 `.mcp.json`（claude 的项目级 MCP 配置来源）。

    claude-code 不读 settings.json 的 mcpServers（实测 2.1.212），只读项目级 `.mcp.json`
    与 `~/.claude.json`；end_discussion 工具依赖此文件被加载。已存在则不动（用户自定义优先）。

    config_dir：per-agent 隔离配置目录（CLAUDE_CONFIG_DIR）。claude 以 `CLAUDE_CONFIG_DIR`
    为准时，项目 trust 与 MCP 启用状态读**该目录下的 `.claude.json`**（而非 ~/.claude.json）；
    缺失时项目 .mcp.json 的 MCP 不会被加载（实测「夏朝存在吗」会话 gemma 看不到
    end_discussion 工具、只能输出 json 文本兜底）。故在此补齐：
    projects[realpath(cwd)].hasTrustDialogAccepted=true + enabledMcpjsonServers 含
    terminate-signal。
    """
    try:
        f = Path(cwd) / ".mcp.json"
        if not f.exists():
            root = Path(__file__).resolve().parent.parent
            cfg = json.loads(json.dumps(_MCP_SERVER_CONFIG))
            cfg["mcpServers"]["terminate-signal"]["args"][0] = str(
                root / "backend" / "terminate_signal_mcp.py")
            f.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        if config_dir:
            cf = Path(config_dir) / ".claude.json"
            d = json.loads(cf.read_text(encoding="utf-8")) if cf.exists() else {}
            projs = d.setdefault("projects", {})
            entry = projs.setdefault(os.path.realpath(cwd), {})
            entry["hasTrustDialogAccepted"] = True
            enabled = entry.setdefault("enabledMcpjsonServers", [])
            if "terminate-signal" not in enabled:
                enabled.append("terminate-signal")
            cf.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


_AS_STDERR_TAIL = 24  # 保留 app-server stderr 尾部行数（断流诊断用）


def _kill_proc_group(proc, grace: float = 0.6) -> None:
    """终止子进程及其**整个进程组**（子进程须以 `start_new_session=True` 启动）。

    为什么不能只 `proc.kill()`：`codex` 是两层进程 —— `node /usr/local/bin/codex`（壳）
    再 spawn 真正的原生 app-server 子进程。只杀壳会让原生子进程被 init 收养（PPID=1）
    继续存活，仍持有 codex thread 与 stdio 管道 → 进程泄漏；下次「继续」新起进程再
    resume 同一 thread 时出现「同一 thread 被两个进程持有」，是断流（连接断开）的隐患。
    （2026-09-14 实测：壳 49585 → 原生子 49586；只 kill 壳后 49586 仍存活；killpg 整组后归零。）

    `start_new_session=True` 让子进程自成进程组（pgid == 子进程 pid），故 killpg 不会误伤
    本服务自身；本函数再校验 `pgid != 自身 pgid` 作双保险。先 SIGTERM 给优雅退出机会
    （codex 借机落盘 rollout），超时再 SIGKILL 兜底；无法整组时退回单体 kill。
    """
    if proc is None or proc.returncode is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        pgid = None
    if pgid is not None and pgid != os.getpgid(0):  # 仅当子进程自成一组的才可整组杀
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception:
            pgid = None
        if pgid is not None:
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                if proc.returncode is not None:
                    return
                time.sleep(0.05)
            try:
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                pass
            return
    # 兜底：未自成进程组 / 无权限整组杀时，单体杀
    try:
        proc.kill()
    except Exception:
        pass


class AgentRuntime:
    """每个 agent 在某一 app 会话内的「持续 CLI 会话」运行时。

    设计前提（用户确认的架构约定）：app 会话 ↔ agent CLI 会话 **1:1、持续续接**。
    - 同一 app 会话里，无论多少轮 / 多少议题，两个 agent 各自保持**同一个** CLI 会话；
      编排器每轮只把「对手刚说的那一句 + 本轮任务」作为新的 user 消息续接，绝不重新搬运整段历史。
    - 只有**开新 app 会话**才新建 agent CLI 会话（新 cli_session_id）；点历史会话时，
      从 sessions.json 读回 cli_session_id 用 `--resume` 恢复对应 agent 历史会话。

    cli_session_id 续接机制（已对真实二进制核实）：
    - claude-code：我们自生成 uuid 并**始终**传 `--session-id <uuid>`（CLI 侧存在即 resume、不存在即新建），
      因此无需解析输出即可确定续接身份，确定式、零歧义。
    - codex：不支持自命名 session id，故首轮 `codex exec --json` 从 JSONL 抓取 `session_id`，
      后续 `codex exec --json resume <id> <prompt>` 续接。（首轮 session_id 抓取为尽力而为，
      依赖 codex 版本的事件字段，已在 `_parse_codex_line` 覆盖常见键；如某版本字段变更需现场核对。）
    - fake：纯内存模拟，维护 `_fake_history` 以忠实复现「有状态会话」（agent 自身记得前文）。
    """

    def __init__(self, agent: AgentConfig, cli_session_id: str | None = None, session_base_url: str | None = None,
                 think: bool | None = None):
        self.agent = agent
        self.cli_session_id = cli_session_id  # None = 本 app 会话内该 agent 尚未开过 CLI 会话
        # 会话级网关地址（含 /gw/<pid>/<sid>?think=… 路径）：编排层按会话拼好传入，
        # 据此让网关按会话/agent 读取思考开关。None 时回落到 agent.base_url（全局默认）。
        self.session_base_url = session_base_url
        # 三级「思考」开关的生效值（会话>agent>全局，编排层算好传入）。None=未指定（按开处理）。
        # 作用于：① codex → 关时注入 -c model_reasoning_effort=low（降/关 reasoning）
        #         ② 网关路径已由 session_base_url 的 ?think= 携带，此处不重复处理
        #         ③ 真实 claude 直连无干净开关（已知限制）
        self.think = think
        self._force_new_session = False  # 自愈：True 时下一轮用全新 id 注册（绕过已占用的旧锁）
        self._fake_history: list[str] = []
        self._section = None  # 当前文本分片归属：None / "think" / "ans"（跨 claude 多事件续接用）
        # 真流式（--include-partial-messages）：开了 partial 后，同一段内容会**既以
        # stream_event 的 delta 增量到达、又出现在最终那条整块 assistant 消息里**。
        # 这两个标志记录「本轮已经用 delta 吐过正文/思考」，据此在解析 assistant 整块时
        # 跳过重复内容（否则界面会显示两遍）。仅在 message_start 时复位。
        self._streamed_text = False
        self._streamed_think = False
        self._partial_messages = True  # 若该 claude 版本不认此 flag，自愈降级为 False（见 _run_claude）

        # codex app-server 持久连接（真流式）：一个 AgentRuntime 持有一条长连接，
        # 跨本会话内多轮复用同一 thread（无需每轮 resume），会话结束由 aclose() 回收。
        # 旧版本 codex 无 app-server 子命令 / 启动失败时，run() 自动回退 codex exec（见 _run_codex）。
        self._appserver_proc = None
        self._appserver_thread_id = None
        self._as_queue = None          # asyncio.Queue：读循环把通知/应答推入，轮生成器消费
        self._as_reader_task = None
        self._as_stderr_task = None
        self._as_req_id = 100          # JSON-RPC 请求 id 计数器（initialize/thread/turn 用）
        self._as_reasoning_seen = 0    # 已吐出的 rollout reasoning 条数（仅当无 live 增量时兜底用）
        self._as_stderr_tail = []      # app-server stderr 尾部（最多 _AS_STDERR_TAIL 行）：断流诊断用
        self._as_returncode = None     # app-server 退出码（读循环 EOF 时记录）

        # 「中止」目标定位（turn 级中断，与轮边界的暂停/停止互不干扰）：
        # - _live_proc：当前在途 turn 的子进程（claude / codex exec 兜底），中断时按后端
        #   分别走协议帧或整组硬停；turn 结束即注销，避免误伤下一轮。
        # - _as_turn_id：当前在途 turn 的 codex turn id（app-server 的 turn/interrupt 必需参数，
        #   来自 turn/start 应答或 turn/started 通知）。
        self._live_proc = None
        self._as_turn_id = None

    @property
    def is_fresh(self) -> bool:
        return self.cli_session_id is None

    # ---------------- 真实 CLI 会话（claude / codex） ----------------
    def _claude_env(self) -> dict:
        """构造拉起 claude 子进程用的环境变量。

        关键隔离保证：若本 agent 配置了 `base_url`，则**显式覆盖** ANTHROPIC_BASE_URL，
        使该 agent 的全部 API 调用（含工具往返）都走指定本地模型网关，
        绝不落到 Claude Code 全局配置里指向的默认后端（如 deepseek）。

        不修改 Claude Code 任何配置文件——仅作用于本次子进程 env。
        """
        env = dict(os.environ)
        # 单次响应 output token 上限（claude-code 默认 32000 偏小；gemma 等推理模型思考块
        # 疯狂吃 output token，象棋实测单轮 4 块 2.2 万字符直接爆表）。从全局配置读，可随时调。
        try:
            from .store import get_settings
            _mo = get_settings().get("claude_max_output_tokens")
            if _mo:
                env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(_mo)
        except Exception:
            pass
        if self.agent.base_url:
            # 会话级网关地址（含 /gw/<pid>/<sid> 路径）优先；否则回落 agent.base_url。
            # 两者都可能被 claude 2.x 的 settings.json env 合并覆盖，故下方把最终 base_url
            # 同步写进隔离配置目录的 settings.json env（见 _sync_claude_base_url），保证生效。
            base_url = self.session_base_url or self.agent.base_url
            # claude 2.x 会把 ~/.claude/settings.json 的 env 合并进运行时并**覆盖**同名进程环境变量，
            # 仅靠子进程 env 注入 ANTHROPIC_BASE_URL 会被全局配置（如指向 deepseek）盖掉、per-agent 网关失效。
            # 故改用隔离配置目录：复制用户 settings（去插件以避免临时目录缺失插件文件报错）、
            # 改写 env.ANTHROPIC_BASE_URL 为目标网关，并用 CLAUDE_CONFIG_DIR 指向它——
            # 子进程只读这份隔离配置，完全不动用户全局 ~/.claude。
            env["CLAUDE_CONFIG_DIR"] = self._claude_config_dir()
            env["ANTHROPIC_BASE_URL"] = base_url
            # claude 仍要求一个 key 头；网关侧放行任意值，故为空时补 dummy
            env["ANTHROPIC_API_KEY"] = self.agent.api_key or "local-gateway"
            # 让 claude 接受任意 model id（网关按此路由到具体本地模型），跳过内置校验
            if self.agent.model:
                env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = self.agent.model
                env["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"] = self.agent.model
            # 隔离配置目录的 settings.json env 会覆盖进程 env，必须把当前（会话级）base_url 写进去，
            # 否则会话路径 /gw/<pid>/<sid> 丢失、思考开关退化为全局。
            self._sync_claude_base_url(base_url)
        return env

    def _sync_claude_base_url(self, base_url: str) -> None:
        """把当前（会话级）base_url 写进隔离配置目录的 settings.json env.ANTHROPIC_BASE_URL。

        claude 2.x 合并 settings.json env 并覆盖进程 env，故仅设进程 env 不够——必须落到
        隔离配置目录文件。每次拉起子进程前调用，保证本会话的请求走正确的 /gw/<pid>/<sid> 路径。
        配置目录由 _claude_config_dir 创建（若不存在），这里只负责刷新已存在文件的 base_url。
        """
        try:
            root = Path.home() / ".cache" / "agentchat" / "claude-cfg" / _safe_name(self.agent.name)
            sfile = root / "settings.json"
            if not sfile.exists():
                return  # _claude_config_dir 创建时会写入正确的 base_url
            d = json.loads(sfile.read_text(encoding="utf-8"))
            d.setdefault("env", {})
            if d["env"].get("ANTHROPIC_BASE_URL") != base_url:
                d["env"]["ANTHROPIC_BASE_URL"] = base_url
                sfile.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _claude_config_dir(self) -> str | None:
        """为带 base_url 的 claude agent 生成隔离配置目录，使 per-agent 网关真正生效（详见 _claude_env）。

        认证沿用用户 settings 中的 ANTHROPIC_AUTH_TOKEN，网关侧放行任意 key；

        关键：隔离目录里的 `projects/` 软链到用户默认 `~/.claude/projects`，使 transcript 落在
        **「按项目目录」的标准位置**——这与用户的核心诉求一致：同一目录下、不管走 gemma 还是 deepseek，
        会话都能在 `cd <项目> && claude` 下直接看到并 `--resume` 恢复（跨模型 resume）。
        网关隔离（settings.json 的 ANTHROPIC_BASE_URL）保持不变，per-agent 路由互不影响。
        旧版把 projects 写成真实目录的，迁移其内容到标准位置后再替换为软链（历史会话不丢）。
        """
        if not self.agent.base_url:
            return None
        root = Path.home() / ".cache" / "agentchat" / "claude-cfg" / _safe_name(self.agent.name)
        root.mkdir(parents=True, exist_ok=True)
        sfile = root / "settings.json"
        # 隔离配置目录的 settings.json env.ANTHROPIC_BASE_URL：优先用会话级 base_url
        # （含 /gw/<pid>/<sid> 路径），否则回落 agent.base_url。进程 env 会被 claude 2.x
        # 合并 settings.json env 覆盖，故这里必须写正确地址，会话级思考开关才能生效。
        _cfg_base_url = self.session_base_url or self.agent.base_url
        if not sfile.exists():
            src = Path.home() / ".claude" / "settings.json"
            d: dict = json.loads(src.read_text(encoding="utf-8")) if src.exists() else {}
            d.pop("enabledPlugins", None)  # 避免临时目录缺失插件文件导致加载报错
            d.setdefault("env", {})
            d["env"]["ANTHROPIC_BASE_URL"] = _cfg_base_url
            d["env"]["ANTHROPIC_API_KEY"] = self.agent.api_key or "local-gateway"
            if self.agent.model:
                d["env"]["ANTHROPIC_CUSTOM_MODEL_OPTION"] = self.agent.model
                d["env"]["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"] = self.agent.model
            # 允许项目级 .mcp.json 的 MCP 服务器自动加载（end_discussion 工具可达）。
            # 缺失实测（「夏朝存在吗」会话）：gemma 看不到工具、只能输出 json 文本兜底。
            d["enableAllProjectMcpServers"] = True
            sfile.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
        else:
            # 旧生成的隔离配置：补齐 enableAllProjectMcpServers（若缺），并同步当前（会话级）
            # base_url——否则沿用旧文件里写死的地址，会话路径 /gw/<pid>/<sid> 丢失。
            try:
                d = json.loads(sfile.read_text(encoding="utf-8"))
                changed = False
                if not d.get("enableAllProjectMcpServers"):
                    d["enableAllProjectMcpServers"] = True
                    changed = True
                d.setdefault("env", {})
                if d["env"].get("ANTHROPIC_BASE_URL") != _cfg_base_url:
                    d["env"]["ANTHROPIC_BASE_URL"] = _cfg_base_url
                    changed = True
                if changed:
                    sfile.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        # projects/ 软链到标准 ~/.claude/projects：transcript 按目录汇聚，跨模型可 resume
        projects_link = root / "projects"
        target = Path.home() / ".claude" / "projects"
        target.mkdir(parents=True, exist_ok=True)
        if projects_link.is_symlink():
            pass  # 已是软链，无需处理
        elif projects_link.is_dir():
            # 迁移旧真实 projects：把会话文件逐项目合并进标准 ~/.claude/projects（同名跳过），
            # 旧 gemma 会话因此与裸 claude/deepseek 会话汇聚到同一目录，跨模型可 resume；再替换为软链
            for proj_dir in projects_link.iterdir():
                if not proj_dir.is_dir():
                    continue
                dest_proj = target / proj_dir.name
                dest_proj.mkdir(parents=True, exist_ok=True)
                for f in proj_dir.iterdir():
                    dest_f = dest_proj / f.name
                    if not dest_f.exists():
                        shutil.move(str(f), str(dest_f))
            shutil.rmtree(str(projects_link), ignore_errors=True)
            if not projects_link.exists():
                projects_link.symlink_to(target, target_is_directory=True)
        elif not projects_link.exists():
            projects_link.symlink_to(target, target_is_directory=True)
        return str(root)

    def _claude_cmd(self) -> list[str]:
        # stream-json 模式（2026-08-19，对齐 Claude Agent SDK / 实测结论）：
        # -p（--print 旧模式）在 2.1.212 下 MCP 客户端有 bug——spawn 项目级 .mcp.json 的
        # stdio server 后从不通信（30s EOF）；`--output-format stream-json --input-format
        # stream-json --verbose`（SDK 同款）下 MCP 正常加载、end_discussion 工具可达。
        # prompt 经 stdin 以 user 帧传入（见 _user_frame），不再作位置参数。
        cmd = ["claude", "--output-format", "stream-json", "--input-format", "stream-json",
               "--verbose", "--dangerously-skip-permissions"]
        # --include-partial-messages：**真流式**的关键开关（2026-09-11 实测）。
        # 不加时 claude 只发整块 assistant 消息（零 delta），界面上的"逐字效果"其实是
        # executor 侧 _replay 把整块文本演出来的；加上后才会发
        # `stream_event` → `content_block_delta`（text_delta / thinking_delta），
        # 正文与思考都是真增量。旧版本 claude 没有此 flag，_run_claude 会自愈降级。
        if self._partial_messages:
            cmd += ["--include-partial-messages"]
        if self.cli_session_id is None or self._force_new_session:
            # 本 app 会话中该 agent 首次启动（或自愈换新 id）：生成新 id 并注册为 claude 会话
            self.cli_session_id = str(uuid.uuid4())
            self._force_new_session = False
            cmd += ["--session-id", self.cli_session_id]
        else:
            # 续接（含历史会话恢复 / 同会话内后续轮）：claude 2.x 用 `--resume`，
            # 若复用 `--session-id` 会因「id 已占用」报 Session ID ... is already in use
            cmd += ["--resume", self.cli_session_id]
        if self.agent.model:
            cmd += ["--model", self.agent.model]
        if self.agent.cli_args:
            cmd += self.agent.cli_args.split()
        return cmd

    async def run(self, prompt: str, cwd: str, timeout: int, meta: dict | None = None):
        """异步生成器，逐块产出 (kind, text)。kind ∈ {"text", "error", "done"}。"""
        if self.agent.type == "fake":
            async for ev in self._run_fake(prompt, meta or {}):
                yield ev
            return
        if self.agent.type == "claude-code":
            async for ev in self._run_claude(prompt, cwd, timeout):
                yield ev
            return
        # codex：优先 app-server 真流式（JSON-RPC over stdio，逐 delta 增量）；
        # 握手/启动失败（旧版本无 app-server、进程崩溃等）自动回退 codex exec（兜底）。
        try:
            async for ev in self._run_codex_appserver(prompt, cwd, timeout):
                yield ev
        except _CodexAppServerUnavailable:
            async for ev in self._run_codex(prompt, cwd, timeout):
                yield ev

    # ---------------- fake / mock agent（调试用，不调用真实 CLI） ----------------
    async def _run_fake(self, prompt: str, meta: dict):
        """纯内存模拟 agent：流式吐出确定性文本，绝不调用外部进程。
        维护自身 _fake_history，模拟「有状态会话」——后续轮能感知前文已存在（但不回显进 prompt，
        因为编排器本就只发 delta；这里仅用于校验状态式链路）。
        """
        round_index = int(meta.get("round_index", 0))
        max_rounds = int(meta.get("max_rounds", 12))
        agent_count = int(meta.get("agent_count", 2)) or 2
        self._fake_history.append(prompt.replace("\n", " ")[:120])
        n_prev = len(self._fake_history) - 1
        note = f"（本 agent 持续会话已记住前 {n_prev} 次发言上下文）" if n_prev > 0 else ""
        # 口径（用户明确）：轮 = 回合 = 每个 agent 各发言一次算一轮。
        # max_rounds = 轮数上限（每轮内每个 agent 各发言一次），全局发言条数 = max_rounds × agent 数。
        # 本 agent 第 my_turn 次发言 = 第 my_turn 轮（每轮每 agent 各一次，两者同号）。
        my_turn = round_index // agent_count + 1
        lines = [
            f"[FAKE:{self.agent.name}] 我是「{self.agent.role or '未定义角色'}」，调试用假 agent。{note}",
            f"本轮：第 {my_turn} 轮 / 上限 {max_rounds} 轮。",
            "我的模拟建议：① 澄清需求边界；② 给出最小可行方案；③ 标注风险点。",
        ]
        # 注意：fake_terminate_round 语义 = 全局发言序号（round_index+1），测试精确控制时序用，勿改
        if self.agent.fake_terminate_round and (round_index + 1) >= self.agent.fake_terminate_round:
            # 收敛标记改为结构化工具调用（end_discussion）——编排层据此走两阶段终止状态机；
            # 不再输出 TERMINATE 文本（字符串不可靠）
            yield ("tool_use", {"name": "end_discussion",
                                "input": {"reason": f"双方已达成一致（fake 调试，第 {my_turn} 轮）"}})

        for ln in lines:
            for chunk in ln.split("。"):
                if chunk:
                    yield ("text", chunk + "。")
                    await asyncio.sleep(0.005)
            yield ("text", "\n")
        yield ("done", "")

    # ---------------- 真实 CLI 会话（claude / codex） ----------------
    async def _run_claude(self, prompt: str, cwd: str, timeout: int):
        async for kind, text in self._run_claude_once(prompt, cwd, timeout):
            if kind == "error" and _is_bad_session_id(text):
                # 自愈：该 session id 被占用（异常退出残留锁 / 旧进程未退出）或不存在（历史恢复读到坏 id），
                # 换新 id 重试一次。用 --session-id 全新注册以避开旧锁；orchestrator 会在本轮结束后持久化新 id。
                self._force_new_session = True
                async for k, t in self._run_claude_once(prompt, cwd, timeout):
                    yield k, t
                return
            if kind == "error" and self._partial_messages and _is_unknown_partial_flag(text):
                # 自愈：该 claude 版本没有 --include-partial-messages → 关掉它重试一次，
                # 退回「整块输出 + _replay 打字兜底」的旧行为（功能不受影响，只是非真流式）。
                # 此刻 claude 还没真正跑起来，故顺带换新 session id，避免拿未注册的 id 去 --resume。
                self._partial_messages = False
                self._force_new_session = True
                async for k, t in self._run_claude_once(prompt, cwd, timeout):
                    yield k, t
                return
            yield kind, text


    async def _run_claude_once(self, prompt: str, cwd: str, timeout: int):
        cmd = self._claude_cmd()
        env = self._claude_env()
        # 确保项目 .mcp.json + 隔离配置目录的 trust/MCP 启用（end_discussion 工具可达）
        _ensure_project_mcp(cwd, self._claude_config_dir())
        self._section = None  # 新一轮：重置思考/答案归属状态
        async for kind, payload in _stream_subprocess(cmd, cwd, timeout, parse="claude", env=env,
                                                      runtime=self, stdin_data=_user_frame(prompt),
                                                      # keep_stdin：保住 stdin 才能在 turn 中途
                                                      # 写 control_request 中断帧（中止功能，
                                                      # 与轮边界的暂停/停止互不干扰）
                                                      keep_stdin=True):
            # text / thinking 整块回放为逐字流（claude 外壳对本地模型整块输出时的兜底）
            if kind in ("text", "thinking") and payload:
                async for seg in _replay(kind, payload):
                    yield seg
            else:
                yield kind, payload

    def _codex_provider_overrides(self) -> list[str]:
        """codex agent 接「指定模型服务」的 `-c` 覆盖（一律命令行注入，不动用户 ~/.codex/config.toml）。

        codex 的默认 provider 来自用户全局配置（本机 config.toml 是 deepseek = 远程**付费**）。
        agent 配了 `base_url` 即表示「本 agent 走我指定的模型服务」，据此选 provider：
        - 指向本机 Ollama（11434）→ 用 codex **内置** provider `ollama`。codex-cli 0.146.1
          只支持 wire_api="responses"，而 Ollama 0.33.3 已支持 /v1/responses，故可直连（免费）。
        - 其它地址 → 注册自定义 provider `agentchat`，按 responses 协议接。内置 provider 名
          不可覆盖（实测 `model_providers.ollama.name` 被拒：reserved），故必须换名。
        未配 base_url → 不注入，沿用 codex 全局配置（既有行为不变）。
        """
        bu = (self.agent.base_url or "").strip()
        if not bu:
            return []
        if "11434" in bu and ("localhost" in bu or "127.0.0.1" in bu):
            return ["-c", 'model_provider="ollama"']
        base = bu.rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        return [
            "-c", 'model_provider="agentchat"',
            "-c", 'model_providers.agentchat.name="agentchat"',
            "-c", f'model_providers.agentchat.base_url="{base}"',
            "-c", 'model_providers.agentchat.wire_api="responses"',
        ]

    def _codex_cmd(self, prompt: str) -> list[str]:
        # --skip-git-repo-check（2026-09-11）：codex 默认拒绝在「非 git 仓库、且不在
        # ~/.codex/config.toml 受信项目列表（[projects."..."] trust_level = "trusted"）」的
        # 目录里运行，直接退出码 1：Not inside a trusted directory and
        # --skip-git-repo-check was not specified.（实测 /Users/stalitz/www/aitest/test3 必现）。
        # 本 app 的项目目录由用户任选、不可能都预先受信，故默认带上该 flag ——
        # 对应 claude 侧的 --dangerously-skip-permissions，codex 侧原先漏了这层豁免。
        cmd = ["codex", "exec", "--json", "--skip-git-repo-check"]
        # end_discussion 工具（FR5.3a）：复用 claude 侧同一个 MCP stdio 服务
        # （backend/terminate_signal_mcp.py，纯 stdlib）。claude 靠项目级 .mcp.json 注册，
        # codex 没有该项目级机制，改用 `-c key=value` 逐次注入配置覆盖（点号路径），
        # **不动用户 ~/.codex/config.toml**。三项缺一不可：
        #   1) command / args：拉起同一个「无状态确认通道」（判定仍由编排层做）；
        #   2) default_tools_approval_mode="approve"：codex 的 MCP 工具调用默认需要审批，
        #      而 `codex exec` 非交互、无审批人 → 自动返回 "user cancelled MCP tool call"，
        #      工具永远调不通（实测默认值与 "auto" 均被取消，只有 "approve" 能真正执行；
        #      合法值 auto/prompt/writes/approve 由 codex 对非法值的报错枚举得出）。
        # 取值用 TOML 字面字符串（单引号），路径含反斜杠也无需转义。
        cmd += ["-c", f"mcp_servers.terminate-signal.command='{sys.executable}'"]
        cmd += ["-c", f"mcp_servers.terminate-signal.args=['{_MCP_SCRIPT}']"]
        cmd += ["-c", 'mcp_servers.terminate-signal.default_tools_approval_mode="approve"']
        # 三级「思考」开关（生效值 False）：降 codex 的 reasoning effort，减少/关闭思考生成。
        # codex 无 --think 类开关，只能经 `-c` 配置覆盖（不动用户 ~/.codex/config.toml）。
        # 取最低档 "low"（best-effort：provider/模型不支持 reasoning effort 时可能无效）。
        if self.think is False:
            cmd += ["-c", 'model_reasoning_effort="low"']
        if self.agent.model:
            cmd += ["-m", self.agent.model]
        cmd += self._codex_provider_overrides()   # agent 配了 base_url → 指定 provider（见该方法）
        if self.agent.cli_args:
            cmd += self.agent.cli_args.split()
        if self.cli_session_id is not None:
            cmd += ["resume", self.cli_session_id]
        cmd += [prompt]
        return cmd

    def _codex_new_reasoning(self, seen: int) -> list[str]:
        """返回 rollout 里第 seen 条之后新增的 reasoning 文本；读不到就返回空（绝不影响主流程）。"""
        if not self.cli_session_id:
            return []
        try:
            p = _codex_transcript_path(self.cli_session_id)
            if not p:
                return []
            texts = _codex_reasoning_texts(p)
        except Exception:
            return []
        return texts[seen:] if len(texts) > seen else []

    async def _run_codex(self, prompt: str, cwd: str, timeout: int):
        cmd = self._codex_cmd(prompt)
        # codex 的 `--json` 事件流不含 reasoning（见 _codex_reasoning_texts 注释），而它的
        # **正文是整块**（item.completed/agent_message）在回合末才到达、reasoning 早已写进
        # rollout —— 所以在吐「正文 / 工具调用」之前，先把新增的 reasoning 作为 thinking
        # 吐出去，顺序天然对齐原生（思考在前、正文在后），不另造顺序逻辑。
        seen = 0
        async for ev in _stream_subprocess(cmd, cwd, timeout, parse="codex", runtime=self):
            if ev[0] in ("text", "tool_use"):
                fresh = self._codex_new_reasoning(seen)
                seen += len(fresh)
                for txt in fresh:
                    yield ("thinking", txt)
            yield ev
        # 回合收尾：只有推理、没有正文的情况（如只调了工具）也别漏
        for txt in self._codex_new_reasoning(seen):
            yield ("thinking", txt)


    # ---------------- codex app-server：真流式（JSON-RPC over stdio） ----------------

    async def _run_codex_appserver(self, prompt, cwd, timeout):
        """codex app-server 真流式：JSON-RPC over stdio，逐 delta 增量产出。

        与 `codex exec --json` 的根本区别：
        - exec 只在回合末发整块文本、reasoning 只落 rollout 文件（非真流式）；
        - app-server 通过 `item/agentMessage/delta` / `item/reasoning/textDelta` 实时增量推送，
          且 source=vscode 时会话在 ChatGPT 桌面端可见。

        连接模型（经真实二进制核实 codex-cli 0.146.1）：
        - 一条长连接跨本会话多轮复用（initialize → thread/start → 多次 turn/start）；
          同一 thread 在进程内持续，故跨轮记忆天然保留，**无需每轮 resume**。
        - 审批类 ServerRequest（执行命令/打补丁/权限/MCP 询问/动态工具）由读循环**就地自动批准**，
          否则 codex 会一直阻塞等应答、回合卡死。
        - `thread/resume` 在跨进程恢复时实测会静默断开（EOF），故只在「历史会话重开」时带超时尝试，
          失败则回落 thread/start 新建（历史经 rollout reconcile 仍可见，仅丢失模型 in-context 记忆）。
        - 握手阶段（initialize / thread 建立）若失败，抛 `_CodexAppServerUnavailable`，
          run() 据此回退 codex exec；握手成功后的单轮错误则按 ("error", ...) 产出，不回退。
        """
        await self._ensure_appserver(cwd)  # 握手；失败抛 _CodexAppServerUnavailable
        tid = self._appserver_thread_id
        # 清掉上一轮残留通知（status/tokenUsage 等），避免误判为新一轮事件
        await self._as_drain_stale()
        self._as_turn_id = None  # 上一轮 turn id 作废，避免中止动作打到已结束的 turn
        got_reasoning = False
        rid = self._next_id()
        self._as_send({"jsonrpc": "2.0", "id": rid, "method": "turn/start",
                       "params": {"threadId": tid, "input": [{"type": "text", "text": prompt}]}})
        try:
            while True:
                try:
                    o = await asyncio.wait_for(self._as_queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    # 单轮超时：杀掉持久进程，下一轮重建（与 exec 超时等价）
                    self._kill_appserver()
                    yield ("error", f"单轮超时（>{timeout}s）已终止")
                    return
                if o.get("_eof"):
                    # 给 stderr 排空任务一点时间收尾，尽量把崩溃原因一并带出（此前一律丢弃）。
                    await asyncio.sleep(0.15)
                    detail = self._as_eof_detail()
                    self._kill_appserver()  # 回收僵死进程与读循环，下一轮重建
                    yield ("error", "codex app-server 连接断开" + detail)
                    return
                if o.get("_tool_use") is not None:
                    yield ("tool_use", o["_tool_use"])
                    continue
                # turn/start 的应答：记录 turn id（turn/interrupt 必需参数）。
                # 应答与通知共用一个队列，必须在此消费掉，否则会被当成未知通知忽略。
                if o.get("id") == rid and isinstance(o.get("result"), dict):
                    _turn = (o["result"].get("turn") or {})
                    if _turn.get("id"):
                        self._as_turn_id = _turn["id"]
                    continue
                m = o.get("method")
                if m == "turn/started":
                    _turn = (o.get("params") or {}).get("turn") or {}
                    if _turn.get("id"):
                        self._as_turn_id = _turn["id"]
                elif m == "item/agentMessage/delta":
                    d = (o.get("params") or {}).get("delta") or ""
                    if d:
                        yield ("text", d)
                elif m == "item/reasoning/textDelta":
                    d = (o.get("params") or {}).get("delta") or ""
                    if d:
                        got_reasoning = True
                        yield ("thinking", d)
                elif m == "item/reasoning/summaryTextDelta":
                    d = (o.get("params") or {}).get("delta") or ""
                    if d:
                        got_reasoning = True
                        yield ("thinking", d)
                elif m == "item/completed":
                    it = (o.get("params") or {}).get("item") or {}
                    # app-server 的 MCP 调用条目名为 mcpToolCall（驼峰），见 _is_mcp_tool_item
                    if _is_mcp_tool_item(it) and "end_discussion" in str(it.get("tool") or ""):
                        yield ("tool_use", {"name": "end_discussion",
                                             "input": _codex_parse_args(it.get("arguments"))})
                    else:
                        # codex 原生工具（命令执行 / 文件改动）：原先一律忽略 ⇒ codex 调了自带
                        # 工具也不落盘、界面空白（2026-09-16 修）。只认 completed，避免
                        # started/completed 成对上报时重复产一次。
                        _tu = _codex_native_tool_use(it)
                        if _tu:
                            yield ("tool_use", _tu)
                            # 同一 item 带输出（aggregatedOutput / exitCode）⇒ 紧跟一条 tool_result，
                            # 否则 app-server 通路下工具「只有调用、没有结果」。
                            _tr = _codex_native_tool_result(it)
                            if _tr:
                                yield ("tool_result", _tr)
                elif m == "error":
                    params = o.get("params") or {}
                    yield ("error", str(params.get("message") or params.get("data") or params))
                    return
                elif m == "turn/completed":
                    self._as_turn_id = None
                    break
                # 其余通知（thread/status/changed、tokenUsage、mcpServer/* 等）忽略
        finally:
            pass
        # 回合结束：若本模型未发 reasoning 增量（如 deepseek 高 effort 不吐 reasoning delta），
        # 从 rollout 文件兜底补推理（仅补本轮新增，避免跨轮重复）。
        if not got_reasoning:
            try:
                fresh = self._codex_new_reasoning(self._as_reasoning_seen)
                self._as_reasoning_seen += len(fresh)
                for txt in fresh:
                    yield ("thinking", txt)
            except Exception:
                pass
        yield ("done", "")


    async def _ensure_appserver(self, cwd: str):
        """拉起（若尚未拉起）codex app-server 长连接并完成握手，确定本会话 thread id。

        失败（进程起不来 / initialize 无响应 / thread 建立失败）抛 _CodexAppServerUnavailable。
        """
        if self._appserver_proc is not None and self._appserver_proc.returncode is None:
            return
        cmd = ["codex", "app-server", "--listen", "stdio://"]
        # 复用 terminate-signal MCP（与 exec 同款），注入方式一致（`-c` 点号路径，不动用户 config.toml）。
        # 注意：app-server 不接受 `--skip-git-repo-check`（实测 rejected），但实测 thread/start
        # 在非 git/未受信目录也能直接成功，故无需该 flag。
        cmd += ["-c", f"mcp_servers.terminate-signal.command='{sys.executable}'"]
        cmd += ["-c", f"mcp_servers.terminate-signal.args=['{_MCP_SCRIPT}']"]
        cmd += ["-c", 'mcp_servers.terminate-signal.default_tools_approval_mode="approve"']
        # 三级「思考」开关（生效值 False）：降 reasoning effort（同 exec 路径，best-effort）。
        if self.think is False:
            cmd += ["-c", 'model_reasoning_effort="low"']
        if self.agent.model:
            # 注意：app-server 不接受 `-m` 参数（实测 "unexpected argument '-m'" 直接退出）；
            # 模型经 `-c` 配置覆盖设置，并在 thread/start 的 model 参数再确认（与已验证探针一致）。
            cmd += ["-c", f"model='{self.agent.model}'"]
        cmd += self._codex_provider_overrides()   # agent 配了 base_url → 指定 provider（见该方法）
        if self.agent.cli_args:
            cmd += self.agent.cli_args.split()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=cwd,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # 自成进程组：codex 是「node 壳 → 原生 app-server 子进程」两层，
                # 回收时须整组杀（见 _kill_proc_group），否则原生子进程会孤儿化。
                start_new_session=True,
            )
        except Exception as e:
            raise _CodexAppServerUnavailable(f"启动失败: {e}")
        self._appserver_proc = proc
        self._as_queue = asyncio.Queue()
        self._as_reader_task = asyncio.create_task(self._as_read_loop())
        self._as_stderr_task = asyncio.create_task(self._as_drain_stderr())
        # 1) initialize
        iid = self._next_id()
        self._as_send({"jsonrpc": "2.0", "id": iid, "method": "initialize",
                       "params": {"clientInfo": {"name": "agentchat", "version": "1.0"}}})
        try:
            init = await asyncio.wait_for(self._as_wait_response(iid), timeout=15)
        except (asyncio.TimeoutError, _CodexAppServerUnavailable):
            self._kill_appserver()
            raise _CodexAppServerUnavailable("initialize 无响应（可能 codex 版本不支持 app-server）")
        if "error" in init:
            self._kill_appserver()
            raise _CodexAppServerUnavailable(f"initialize 错误: {init.get('error')}")
        # 2) thread：有历史 cli_session_id 则尝试 resume（带超时兜底），否则新建
        try:
            tid = await self._as_ensure_thread(cwd)
        except _CodexAppServerUnavailable:
            self._kill_appserver()
            raise
        self._appserver_thread_id = tid
        self.cli_session_id = tid


    async def _as_ensure_thread(self, cwd: str) -> str:
        """建立本连接的 thread：优先 resume 历史 id，失败则新建。返回 thread id。"""
        if self.cli_session_id:
            rid = self._next_id()
            self._as_send({"jsonrpc": "2.0", "id": rid, "method": "thread/resume",
                           "params": {"threadId": self.cli_session_id}})
            try:
                resp = await asyncio.wait_for(self._as_wait_response(rid), timeout=10)
                if "result" in resp:
                    return (resp.get("result") or {}).get("thread", {}).get("id") or self.cli_session_id
            except (asyncio.TimeoutError, _CodexAppServerUnavailable):
                pass  # resume 不可靠 → 回落新建
        rid = self._next_id()
        params = {"cwd": cwd}
        if self.agent.model:
            params["model"] = self.agent.model
        self._as_send({"jsonrpc": "2.0", "id": rid, "method": "thread/start", "params": params})
        try:
            resp = await asyncio.wait_for(self._as_wait_response(rid), timeout=15)
        except (asyncio.TimeoutError, _CodexAppServerUnavailable):
            raise _CodexAppServerUnavailable("thread/start 无响应")
        if "error" in resp:
            raise _CodexAppServerUnavailable(f"thread/start 错误: {resp.get('error')}")
        tid = (resp.get("result") or {}).get("thread", {}).get("id")
        if not tid:
            raise _CodexAppServerUnavailable("thread/start 未返回 thread.id")
        return tid


    async def _as_wait_response(self, iid):
        """从队列消费，直到出现 id==iid 的应答（result/error）或 EOF。"""
        while True:
            o = await asyncio.wait_for(self._as_queue.get(), timeout=15)
            if o.get("_eof"):
                raise _CodexAppServerUnavailable("连接断开")
            if o.get("id") == iid and ("result" in o or "error" in o):
                return o
            # 其余通知（thread/started、status 等）暂存丢弃，不影响等待


    async def _as_read_loop(self):
        """后台读循环：解析 stdout 行，审批类请求就地自动批准，其余推入队列。"""
        proc = self._appserver_proc
        if proc is None:
            return
        stdout = proc.stdout
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    break
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                # 动态工具调用（含 end_discussion 经 client 路由的罕见情况）：先捕获投票，再自动应答
                if o.get("method") == "item/tool/call":
                    it = o.get("params") or {}
                    tool = str(it.get("tool") or "")
                    if "end_discussion" in tool:
                        try:
                            await self._as_queue.put(
                                {"_tool_use": {"name": "end_discussion",
                                              "input": _codex_parse_args(it.get("arguments"))}})
                        except Exception:
                            pass
                # 需客户端应答的 ServerRequest（审批类）：就地自动批准，不进队列
                if "method" in o and "id" in o and isinstance(o.get("id"), (str, int)):
                    if self._as_autoanswer(o["method"], o["id"], o.get("params") or {}):
                        continue
                try:
                    await self._as_queue.put(o)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            try:
                self._as_returncode = proc.returncode
            except Exception:
                pass
            try:
                await self._as_queue.put({"_eof": True})
            except Exception:
                pass


    async def _as_drain_stderr(self):
        """排空 app-server stderr（verbose 日志），避免管道撑满阻塞子进程。

        额外保留**尾部若干行**（`_AS_STDERR_TAIL`）：app-server 是崩溃/断流时才由它给出
        真实原因（如 sqlite 忙、panic、provider 报错），此前一律丢弃，导致线上「连接断开」
        只能看到一句无信息量的错误、无法定位。退出后在 EOF 错误里一并带出。
        """
        proc = self._appserver_proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                chunk = await proc.stderr.readline()
                if not chunk:
                    break
                try:
                    self._as_stderr_tail.append(chunk.decode(errors="replace").rstrip())
                except Exception:
                    pass
                if len(self._as_stderr_tail) > _AS_STDERR_TAIL:
                    del self._as_stderr_tail[0]
        except Exception:
            pass

    def _as_eof_detail(self) -> str:
        """断流时的补充诊断：退出码 + stderr 尾部（无信息时返回空串）。"""
        bits = []
        rc = self._as_returncode
        if rc is not None:
            bits.append(f"退出码 {rc}")
        tail = [ln for ln in self._as_stderr_tail if ln.strip()]
        if tail:
            bits.append("stderr 尾部: " + " | ".join(tail[-6:])[:600])
        return ("（" + "；".join(bits) + "）") if bits else ""


    async def _as_drain_stale(self):
        """清掉上一轮残留通知，避免误判为新一轮事件。"""
        if self._as_queue is None:
            return
        while True:
            try:
                await asyncio.wait_for(self._as_queue.get(), timeout=0.3)
            except (asyncio.TimeoutError, Exception):
                return


    def _as_autoanswer(self, method, rid, params: dict) -> bool:
        """审批类 ServerRequest 自动应答。返回 True 表示已处理（不进队列）。"""
        if method in ("execCommandApproval", "item/commandExecution/requestApproval"):
            self._as_send({"jsonrpc": "2.0", "id": rid, "result": {"decision": "accept"}})
            return True
        if method in ("applyPatchApproval", "item/fileChange/requestApproval"):
            self._as_send({"jsonrpc": "2.0", "id": rid, "result": {"decision": "approved"}})
            return True
        if method == "mcpServer/elicitation/request":
            self._as_send({"jsonrpc": "2.0", "id": rid, "result": {"action": "accept"}})
            return True
        if method == "item/tool/requestUserInput":
            self._as_send({"jsonrpc": "2.0", "id": rid, "result": {"answers": {}}})
            return True
        if method == "item/permissions/requestApproval":
            # 回授请求中声明的权限 profile（scope=session），等效放行本回合权限申请
            self._as_send({"jsonrpc": "2.0", "id": rid,
                           "result": {"permissions": params.get("permissions", {}), "scope": "session"}})
            return True
        if method == "item/tool/call":
            # 未注册的动态工具：返回空成功，避免阻塞（end_discussion 已在读循环捕获投票）
            self._as_send({"jsonrpc": "2.0", "id": rid,
                           "result": {"contentItems": [], "success": True}})
            return True
        return False


    def _as_send(self, obj: dict):
        """写一条 JSON-RPC 帧到 app-server stdin。"""
        if self._appserver_proc is None or self._appserver_proc.stdin is None:
            return
        try:
            self._appserver_proc.stdin.write(
                (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        except Exception:
            pass


    def _next_id(self):
        self._as_req_id += 1
        return self._as_req_id


    def _kill_appserver(self):
        """终止持久 app-server 进程与读循环（超时/错误/会话结束时回收）。

        必须**整组**回收：`codex` 是「node 壳 → 原生 app-server 子进程」两层，只杀壳会
        留下孤儿原生进程（PPID=1）继续持有 thread 与管道（见 `_kill_proc_group`）。
        """
        proc = self._appserver_proc
        if proc is not None:
            _kill_proc_group(proc)
            self._appserver_proc = None
        t = getattr(self, "_as_reader_task", None)
        if t is not None:
            t.cancel()
            self._as_reader_task = None
        st = getattr(self, "_as_stderr_task", None)
        if st is not None:
            st.cancel()
            self._as_stderr_task = None
        self._appserver_thread_id = None
        self._as_queue = None
        self._as_stderr_tail = []
        self._as_returncode = None


    def aclose(self):
        """会话结束回收 codex app-server 长连接（由编排层 Session.start() finally 调用）。"""
        self._kill_appserver()

    # ---------------- turn 级中断（「中止」功能） ----------------
    async def interrupt(self) -> str:
        """中断**当前在途 turn**，返回实际使用的通道（诊断用）。

        与轮边界的 pause/stop 是不同层的东西：pause/stop 只在下一位发言前生效（当前 turn 跑完
        才停），本方法直接掐断正在生成的输出。通道按「协议优先、硬停兜底」分级：

        - codex app-server：`turn/interrupt{threadId,turnId}` JSON-RPC（实测毫秒级生效），
          且长连接与 thread 都保留，续接能继续复用同一会话 —— 首选。
        - claude：往 stdin 写 `control_request{subtype:interrupt}`。claude 中止当前回合后照常
          发 result 收尾、进程正常退出（不留会话锁，续接走 `--resume`）。
        - 其余（codex exec 兜底路径 / 协议帧写失败）：`_kill_proc_group` 整组硬停。
        返回 "turn_interrupt" / "control_request" / "killpg" / "none"（无在途 turn）。
        """
        proc = self._appserver_proc
        if proc is not None and getattr(proc, "returncode", None) is None and self._as_turn_id:
            self._as_send({"jsonrpc": "2.0", "id": self._next_id(), "method": "turn/interrupt",
                           "params": {"threadId": self._appserver_thread_id,
                                      "turnId": self._as_turn_id}})
            return "turn_interrupt"
        live = self._live_proc
        if live is not None and getattr(live, "returncode", None) is None:
            if self.agent.type == "claude-code" and live.stdin is not None:
                try:
                    frame = json.dumps({
                        "type": "control_request", "request_id": "int-1",
                        "request": {"subtype": "interrupt", "cancel_queued": True},
                    }, ensure_ascii=False) + "\n"
                    live.stdin.write(frame.encode("utf-8"))
                    await live.stdin.drain()
                    return "control_request"
                except Exception:
                    pass
            _kill_proc_group(live)
            return "killpg"
        return "none"

    def hard_kill(self) -> None:
        """兜底硬停：中断帧发出后 turn 仍未收尾时，整组掐掉在途进程/连接（看门狗调用）。"""
        live = self._live_proc
        if live is not None and getattr(live, "returncode", None) is None:
            _kill_proc_group(live)
            self._live_proc = None
        if self._appserver_proc is not None:
            self._kill_appserver()


class _CodexAppServerUnavailable(Exception):
    """codex app-server 握手/启动失败：调用方应回退到 codex exec 兜底。"""
def _is_claude_turn_end(text: str) -> bool:
    """判断一行 stream-json 是否为「单轮结束」（claude 的 result 事件）。

    stdin 保持打开时（可中断模式，见 _stream_subprocess 的 keep_stdin）claude 收到 result 后
    不会自行退出——它在等后续消息，读循环会永远阻塞在下一次 readline，turn 永不结束。
    故必须据此主动收尾。正常完成与被打断（aborted）都以 result 收尾，只认 type 即可。
    """
    try:
        ev = json.loads(text)
    except Exception:
        return False
    return isinstance(ev, dict) and ev.get("type") == "result"


async def _stream_subprocess(cmd: list, cwd: str, timeout: int, parse: str, runtime=None, env=None,
                             stdin_data: bytes | None = None, keep_stdin: bool = False):
    """启动子进程并按行产出 ("text", str) 或 ("error", str)。

    env：显式传入则完全覆盖子进程环境变量（用于 per-agent 网关隔离）；
    为 None 时继承当前进程环境（默认行为）。
    stdin_data：非空时写入子进程 stdin 后关闭（stream-json 输入帧用）。
    keep_stdin：**保持 stdin 打开**（claude 用）。claude 的 turn 级中断只能靠往 stdin 写
      `control_request{subtype:interrupt}` 帧实现，关掉 stdin 就没通道了。代价是 claude 收到
      `result` 后不会自行退出（它在等后续消息），故此时由本函数**按 result 事件收尾**并主动
      关 stdin 让其正常退出（SIGKILL 会残留会话锁，见 _run_claude 自愈路径）。仅 claude 需要。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, env=env,
            stdin=asyncio.subprocess.PIPE if (stdin_data is not None or keep_stdin) else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            # 自成进程组：超时/异常时整组回收，避免 claude/codex 的子进程孤儿化
            # （见 _kill_proc_group）。
            start_new_session=True,
        )
    except Exception as e:  # 命令不存在 / 权限问题
        yield ("error", f"启动失败: {e}")
        return
    assert proc.stdout is not None
    # 登记「当前在途 turn 的进程」：编排层中止时据此定位目标（claude 写中断帧 / 兜底整组硬停）。
    if runtime is not None:
        runtime._live_proc = proc

    # --verbose 会把调试信息大量写入 stderr，不消费会撑满管道阻塞 claude 子进程；
    # 这里异步排空，仅保留尾部（错误报告时用）。
    stderr_tail: list[bytes] = []

    async def _drain_stderr():
        if proc.stderr is None:
            return
        try:
            while True:
                chunk = await proc.stderr.readline()
                if not chunk:
                    break
                stderr_tail.append(chunk)
                if len(stderr_tail) > 40:
                    stderr_tail.pop(0)
        except Exception:
            pass

    drain_task = asyncio.create_task(_drain_stderr())
    if stdin_data is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_data)
            await proc.stdin.drain()
        except Exception:
            pass
        finally:
            if not keep_stdin:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
    try:
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
            if not line:
                break
            text = line.decode(errors="replace")
            if parse == "claude":
                for kind, payload in _parse_claude_line(text, runtime):
                    yield kind, payload
                # stdin 保持着时 claude 不会在 result 后退出，必须按 result 事件收尾，
                # 否则本生成器永远阻塞在下一次 readline（turn 永不结束）。
                if keep_stdin and _is_claude_turn_end(text):
                    break
            elif parse == "codex":
                for kind, payload in _parse_codex_line(text, runtime):
                    yield kind, payload
            else:
                if text.strip():
                    yield ("text", text)
        if keep_stdin and proc.returncode is None:
            # 主动收尾：关 stdin 让 claude 正常退出（不 SIGKILL，避免残留会话锁）
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                _kill_proc_group(proc)
                await proc.wait()
        else:
            await proc.wait()
        if proc.returncode not in (0, None):
            err = ""
            if stderr_tail:
                err = b"".join(stderr_tail).decode(errors="replace")
            yield ("error", f"退出码 {proc.returncode}: {err[:800]}")
        if not drain_task.done():
            drain_task.cancel()
    except asyncio.TimeoutError:
        _kill_proc_group(proc)
        await proc.wait()
        if not drain_task.done():
            drain_task.cancel()
        yield ("error", f"单轮超时（>{timeout}s）已终止")
    except Exception as e:
        _kill_proc_group(proc)
        if not drain_task.done():
            drain_task.cancel()
        yield ("error", f"执行异常: {e}")
    finally:
        # 注销在途进程登记（本轮已结束，避免中止动作误伤下一轮）
        if runtime is not None and runtime._live_proc is proc:
            runtime._live_proc = None


_REPLAY_CHUNK = 3
_REPLAY_DELAY = 0.012


async def _replay(kind: str, text: str):
    """把整块文本回放为逐字流：claude 外壳对本地模型整块输出时的兜底，模拟打字效果。
    短片段（≤40 字）直接透传，避免给本就接近流式的增量加无意义延迟。"""
    if not text:
        return
    if len(text) <= 40:
        yield (kind, text)
        return
    for i in range(0, len(text), _REPLAY_CHUNK):
        yield (kind, text[i:i + _REPLAY_CHUNK])
        await asyncio.sleep(_REPLAY_DELAY)


def _classify_text(runtime: "AgentRuntime | None", text: str) -> list:
    """把一段文本按哨兵归属到「思考」或「答案」。

    网关在 reasoning 块首个分片前缀 \\x01THINK\\x01、answer 块首个分片前缀 \\x01ANSW\\x01。
    claude-code 外壳行为不稳定：可能把两个块拆成多个独立 assistant 事件，也可能把 reasoning+answer
    合并进同一个文本块（此时哨兵出现在文本中部、前面可能还有 gemma 自带的分隔符如 `---`）。
    故这里**扫描**文本中的哨兵位置并据此切分，而非仅看开头：
      - 出现 THINK：其之后的内容（到 ANSW 之前 / 结尾）归「思考」，THINK 之前的残留当作分隔符丢弃；
      - 出现 ANSW：其之后的内容归「答案」；
      - 既无 THINK 也无 ANSW：按当前 section 续接，或默认「答案」（非思考模型）。
    哨兵是纯文本，claude 原样透传。
    """
    if not text:
        return []
    out = []
    ti = text.find(THINK_SENT)
    ai = text.find(ANSW_SENT)
    if ti == -1 and ai == -1:
        # 无哨兵：续接段或默认答案
        if runtime is not None and runtime._section == "think":
            out.append(("thinking", text))
        else:
            if runtime is not None:
                runtime._section = "ans"
            out.append(("text", text))
        return out
    if ti == -1:
        # 只有 ANSW（非思考模型：answer 块被 claude 拆/合并，哨兵前的内容也是答案）→ 去哨兵整段当答案
        cleaned = text[:ai] + text[ai + len(ANSW_SENT):]
        if runtime is not None:
            runtime._section = "ans"
        if cleaned:
            out.append(("text", cleaned))
        return out
    # 有 THINK 哨兵
    if runtime is not None:
        runtime._section = "think"
    think_text = text[ti + len(THINK_SENT): ai if (ai != -1 and ai > ti) else len(text)]
    if think_text:
        out.append(("thinking", think_text))
    if ai != -1 and ai > ti:
        ans_text = text[ai + len(ANSW_SENT):]
        if runtime is not None:
            runtime._section = "ans"
        if ans_text:
            out.append(("text", ans_text))
    return out


def _parse_claude_line(text: str, runtime: "AgentRuntime | None" = None):
    try:
        ev = json.loads(text)
    except json.JSONDecodeError:
        if text.strip():
            return _classify_text(runtime, text)
        return []
    t = ev.get("type")
    # 真流式：`--include-partial-messages` 把原始 API 流事件包在
    # `{"type":"stream_event","event":{...}}` 里（event 才是 content_block_delta 等）。
    # 这里就地解包，使下面的 delta / 各分支逻辑对「包与不包」两种形态都成立。
    if t == "stream_event" and isinstance(ev.get("event"), dict):
        ev = ev["event"]
        t = ev.get("type")
        # message_start：一条新的 assistant 消息开始，复位去重标志
        if t == "message_start" and runtime is not None:
            runtime._streamed_text = False
            runtime._streamed_think = False
    # 流式增量
    delta = ev.get("delta")
    if isinstance(delta, dict):
        # 思考增量（Anthropic thinking_delta，非网关路径）：独立产出 thinking 事件
        if isinstance(delta.get("thinking"), str) and delta["thinking"]:
            if runtime is not None:
                runtime._streamed_think = True
            return [("thinking", delta["thinking"])]
        d = delta.get("text") or ""
        if d:
            if runtime is not None:
                runtime._streamed_text = True
            return _classify_text(runtime, d)
        # 其余 delta（input_json_delta / signature_delta）不出事件：tool_use 由下面
        # 整块 assistant 消息统一给出，避免半截 JSON 流到编排层
        return []
    # assistant 整块：标准 thinking block（直连其他 client）或网关双 text 块（带哨兵）
    if t == "assistant":
        msg = ev.get("message") or {}
        out = []
        for item in (msg.get("content") or []):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "thinking" and item.get("thinking"):
                # 已经用 delta 真流式吐过思考 → 跳过，避免显示两遍
                if runtime is not None and runtime._streamed_think:
                    continue
                out.append(("thinking", item["thinking"]))
            elif item.get("type") == "text" and item.get("text"):
                if runtime is not None and runtime._streamed_text:
                    continue
                out += _classify_text(runtime, item["text"])
            elif item.get("type") == "tool_use":
                # 结构化工具调用（如 end_discussion）：透传给编排层做终止状态机。
                # 即使在 partial 模式下也**必须**从整块消息取（delta 只有半截 JSON）。
                out.append(("tool_use", {
                    "name": item.get("name", ""),
                    "input": item.get("input", {}) or {},
                }))
        if out:
            return out
    # user 事件：可能是 tool_result（工具执行失败后的结果回传）。claude-code 把失败结果
    # 以 user.message.content[].{type:tool_result, is_error:true} 发出，必须透传给编排层，
    # 否则「No such tool available」这类错误会被完全吞掉。
    if t == "user":
        msg = ev.get("message") or {}
        out = []
        for item in (msg.get("content") or []):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_result":
                err = item.get("content", "") or ""
                # 回执 content 可能是 [{type:text,text}] 列表（claude 包法），或纯字符串，
                # 统一展平为字符串，否则前端渲染成 [object Object]。
                if isinstance(err, list):
                    err = "".join(
                        b.get("text", "") for b in err
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                elif not isinstance(err, str):
                    err = str(err)
                if isinstance(err, str):
                    err = err.replace("<tool_use_error>", "").replace("</tool_use_error>", "").strip()
                # 成功与失败的工具回执都按时间线产出（不再丢弃成功结果）：
                # 编排层据此把工具结果块插到对应工具调用之后，实时可见、无需刷新。
                if err:
                    out.append(("tool_result", {"is_error": bool(item.get("is_error")), "content": err}))
        return out
    # result 事件：失败透出错误；成功不重复产出（文本已由 assistant 事件给出）
    if t == "result" and (ev.get("is_error") or ev.get("subtype") == "error"):
        return [("error", ev.get("result") or "执行失败")]
    return []


def _parse_codex_line(text: str, runtime: "AgentRuntime | None"):
    """解析 codex --json 的 JSONL 事件：尽力抓取 session_id（仅首次），并产出文本 delta。"""
    try:
        ev = json.loads(text)
    except json.JSONDecodeError:
        if text.strip():
            return [("text", text)]
        return []
    # 抓取 session_id（codex 首轮输出里携带；resume 后 runtime 已持有，这里忽略）
    if runtime is not None and runtime.cli_session_id is None:
        sid = (ev.get("session_id")
               or (ev.get("session") or {}).get("id")
               or ev.get("thread_id"))
        if sid:
            runtime.cli_session_id = sid
    # MCP 工具调用：`item.completed` 里 item.type == "mcp_tool_call"（exec / rollout）或
    # "mcpToolCall"（app-server），形如 {"server":"terminate-signal","tool":"end_discussion",
    # "arguments":{"reason":...}}。命名兼容见 _is_mcp_tool_item。
    # 必须映射成与 claude 侧完全一致的 ("tool_use", {...})，否则编排层的终止状态机
    # 收不到 codex 的投票（表现为「codex 只会说话、不会投终止票」）。
    # 只认 item.completed：MCP 调用会发 started + completed 两条，取 completed
    # 既保证「必有」、又不会重复产事件（agent_message 也只发 completed）。
    item = ev.get("item")
    if _is_mcp_tool_item(item):
        if ev.get("type") == "item.completed" and "end_discussion" in str(item.get("tool") or ""):
            args = item.get("arguments") or {}
            return [("tool_use", {
                "name": "end_discussion",
                "input": {"reason": str(args.get("reason", "") or "")},
            })]
        return []   # 其余 mcp 事件既不产文本、也不重复产 tool_use
    if ev.get("type") == "item.completed":
        # codex 原生工具（shell / 打补丁）：此前整条忽略 ⇒ 调了也不显示。只认 completed，
        # 因为 started/completed 成对上报，取 completed 既保证「必有」又不重复产事件。
        # completed 条目本身已带输出（aggregated_output / exit_code）⇒ 同一处补发 tool_result，
        # 否则界面只有「调用了什么」、看不到「执行结果」。
        tu = _codex_native_tool_use(item)
        if tu:
            out = [("tool_use", tu)]
            tr = _codex_native_tool_result(item)
            if tr:
                out.append(("tool_result", tr))
            return out
    t = _codex_text(ev)
    if t:
        return [("text", t)]
    return []


def _codex_text(ev: dict) -> str:
    """尽力从 codex 事件中提取文本。codex JSON 事件结构随版本变化，这里覆盖常见字段。"""
    if isinstance(ev.get("text"), str) and ev["text"]:
        return ev["text"]
    msg = ev.get("message") or ev.get("msg")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str) and content:
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    t = item.get("text")
                    if isinstance(t, str) and t:
                        parts.append(t)
            if parts:
                return "".join(parts)
    delta = ev.get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("text"), str) and delta["text"]:
        return delta["text"]
    # codex 0.146.1 的真实结构（2026-09-11 实测 codex exec --json）：
    #   {"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"..."}}
    # 文本嵌在 item.text 里、不在顶层——原先没这条分支，会导致 codex 跑成功但界面收不到
    # 任何输出（表现为"codex 没反应"）。item 也可能是 reasoning / command_execution，
    # 这里只取带 text 的（agent_message 即正式发言）。
    item = ev.get("item")
    if isinstance(item, dict):
        it = item.get("text")
        if isinstance(it, str) and it:
            return it
    return ""


def _is_unknown_partial_flag(text: str) -> bool:
    """该版本 claude 不认 `--include-partial-messages` 时的报错特征（用于自愈降级）。"""
    t = (text or "").lower()
    return "include-partial-messages" in t and (
        "unknown option" in t or "unknown argument" in t or "unrecognized" in t or "invalid option" in t
    )


def _is_bad_session_id(text: str) -> bool:
    """claude 续接失败的可自愈错误：session id 仍被占用、或历史恢复读到不存在的 session。"""
    t = text.lower()
    return (
        "already in use" in t
        or "no conversation found" in t
        or ("session" in t and ("not found" in t or "not exist" in t or "unknown" in t))
    )


# ---------------- CLI transcript 读取（打开会话时以 agent 历史覆盖本地） ----------------
def _clean_sentinels(text: str) -> str:
    """剥离网关透传的思考/答案哨兵（纯文本，claude 原样透传，落盘 transcript 里可能残留）。"""
    if not text:
        return text
    return text.replace(THINK_SENT, "").replace(ANSW_SENT, "")


def _claude_transcript_path(cli_session_id: str, agent_name: str):
    """按文件名（= CLI session id）在 claude 两个可能的 projects 目录下定位 transcript JSONL。"""
    name = _safe_name(agent_name)
    bases = [
        Path.home() / ".cache" / "agentchat" / "claude-cfg" / name / "projects",
        Path.home() / ".claude" / "projects",
    ]
    for base in bases:
        hits = glob.glob(str(base / "**" / f"{cli_session_id}.jsonl"), recursive=True)
        if hits:
            return hits[0]
    return None


def _codex_transcript_path(cli_session_id: str):
    bases = [Path.home() / ".codex" / "sessions", Path.home() / ".codex"]
    for base in bases:
        hits = glob.glob(str(base / "**" / f"*{cli_session_id}*.jsonl"), recursive=True)
        if hits:
            return hits[0]
    return None


def _normalize_turn(content: str, thinking: str, tool_calls=None, tool_results=None) -> dict:
    """归一化一次 assistant 回合的 content / thinking / tool_calls / tool_results。

    原则（用户明确）：思考就是思考，绝不因为正文为空就把思考提升为正文——
    那样会造成「同一段内容既当思考又当正文」的重复显示。
    - 同时有 thinking + answer：双块都保留（thinking 折叠、answer 展开）；
    - 只有 thinking、answer 为空（如 gemma 把结论也吐在 reasoning 流里）：
      content 保持为空，thinking 保留原样；前端正文为空时显示
      「（本轮未输出独立结论，见上方思考过程）」占位，不造假正文。
    - tool_calls / tool_results：同一 agent 回合内（含多次工具调用、失败、重试、
      最终结论）合并为「一个气泡」的元数据，前端在同一气泡内以可折叠子块渲染
      （对齐 WorkBuddy / claude.ai：工具调用失败不新开 message，后续说明仍在同气泡）。
    """
    content = (content or "").strip()
    thinking = (thinking or "").strip()
    return {
        "content": content,
        "thinking": thinking,
        "tool_calls": tool_calls or [],
        "tool_results": tool_results or [],
    }


def _normalize_claude_blocks(seq: list) -> list:
    """把按原生 type 收集的有序块列表归一化为时间线 blocks。

    输入块结构直接对齐 claude 原生 content（type ∈ thinking/text/tool_use/tool_result）：
    - thinking: {type:"thinking", thinking}
    - text:     {type:"text", text}
    - tool_use: {type:"tool_use", name, input}
    - tool_result: {type:"tool_result", is_error, content}
    这里**不重新发明结构**，只做一项必要清洗：合并相邻同类型（thinking/text）块，
    避免 claude 把一次推理拆成多个块时 UI 出现一堆零散折叠。tool_result 的 content
    已在解析期展平为字符串（见 _extract_tool_results），此处不再处理。
    """
    out: list[dict] = []
    for b in seq:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if out and out[-1].get("type") == bt and bt in ("thinking", "text"):
            if bt == "thinking":
                out[-1]["thinking"] = (out[-1].get("thinking") or "") + (b.get("thinking") or "")
            else:
                out[-1]["text"] = (out[-1].get("text") or "") + (b.get("text") or "")
        else:
            out.append(dict(b))
    return out


def _parse_claude_transcript(path: str) -> list:
    """解析 claude JSONL：返回有序 [{content, thinking, tool_calls, tool_results}]，每个 = 一个 agent 气泡。

    气泡粒度 = **一个 agent 回合**（对齐 WorkBuddy / claude.ai）：同一回合内「思考 → 工具调用 →
    工具结果（失败）→ 重试/最终结论」合并为**一个气泡**，工具调用与失败作可折叠子块内联，
    不新开 message。判定规则：
    - `user` 事件若含 `tool_result` 块（工具回执，用户方）：拼入当前气泡的 tool_results，**不 flush**，
      故 tool_result 之后 assistant 的续写仍属同一气泡（工具失败后换工具/直接说明都在同气泡）。
    - `user` 事件含真实文本（下一轮 prompt / 人工输入）：收口当前气泡，开新单元。
    - `assistant` 事件：thinking/text 照旧归集；`tool_use` 块收集进 tool_calls（之前被忽略，
      导致「Claude 发出的调用输出」在 UI 消失——本次修复）。
    - claude-code 把「一次回复（原生 thinking + text）」拆成多个 assistant 事件（中间无 user 事件），
      仍按 user 分界合并，不拆开。
    - 网关 agent（gemma 等）text 块带 `\x01THINK\x01`/`\x01ANSW\x01` 哨兵，按 _classify_text 切分。
    """
    import re as _re
    turns = []
    pending = None  # 当前气泡单元：{"think": [...], "ans": [], "tc": [], "tr": [], "ts": None}

    def _extract_tool_results(msg):
        out = []
        for item in (msg.get("content") or []):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_result":
                err = item.get("content", "") or ""
                # claude 把 tool_result 的 content 包成 [{type:text, text:...}] 列表，
                # 必须展平为字符串，否则原样落盘后前端 textContent 渲染成 [object Object]
                if isinstance(err, list):
                    err = "".join(
                        b.get("text", "") for b in err
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                elif not isinstance(err, str):
                    err = str(err)
                if isinstance(err, str):
                    err = err.replace("<tool_use_error>", "").replace("</tool_use_error>", "").strip()
                out.append({"is_error": bool(item.get("is_error")), "content": err})
        return out

    def _has_real_text(msg):
        c = msg.get("content")
        if isinstance(c, str):
            return bool(c.strip())
        if isinstance(c, list):
            for item in c:
                if isinstance(item, dict) and item.get("type") == "text" and (item.get("text") or "").strip():
                    return True
        return False

    def flush():
        nonlocal pending
        if pending is None:
            return
        content = _re.sub(r"^\s*---\s*", "", _re.sub(r"\s*---\s*$", "", _clean_sentinels("\n\n".join(pending["ans"]).strip()))).strip()
        thinking = _clean_sentinels("\n\n".join(pending["think"])).strip()
        tc = pending["tc"]
        tr = pending["tr"]
        # 气泡非空条件：正文 / 思考 / 工具调用 / 工具结果 任一存在（纯工具失败轮也有 tc+tr，须保留）
        if content or thinking or tc or tr:
            t = _normalize_turn(content, thinking, tc, tr)
            # 时间线 blocks 直接复用 claude 原生 content 的顺序与 type（不另造顺序逻辑）
            t["blocks"] = _normalize_claude_blocks(pending.get("seq") or [])
            if pending.get("ts"):
                t["ts"] = pending["ts"]
            turns.append(t)
        pending = None

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
            ev_ts = o.get("timestamp")
            if t == "user":
                msg = o.get("message", {})
                trs = _extract_tool_results(msg)
                if trs:
                    if pending is None:
                        pending = {"think": [], "ans": [], "tc": [], "tr": [], "seq": [], "ts": ev_ts}
                    pending["tr"].extend(trs)
                    for tr in trs:
                        # 工具回执按原生 content 顺序进入时间线（type 对齐 claude）
                        pending["seq"].append({"type": "tool_result", "is_error": tr["is_error"], "content": tr["content"]})
                    # 工具回执为「用户方回传」，不新开气泡；但若同消息还带真实文本
                    # （下一轮 prompt），则收口当前气泡、开新单元
                    if _has_real_text(msg):
                        flush()
                        pending = {"think": [], "ans": [], "tc": [], "tr": [], "seq": [], "ts": ev_ts}
                    continue
                # 纯真实文本 user 事件：收口当前气泡，开新单元
                flush()
                pending = {"think": [], "ans": [], "tc": [], "tr": [], "seq": [], "ts": ev_ts}
                continue
            if t != "assistant":
                continue
            if pending is None:
                pending = {"think": [], "ans": [], "tc": [], "tr": [], "seq": [], "ts": ev_ts}  # 容错：无 user 前置
            msg = o.get("message", {})
            blocks = msg.get("content")
            if not isinstance(blocks, list):
                if isinstance(blocks, str) and blocks.strip():
                    pending["ans"].append(_clean_sentinels(blocks).strip())
                continue
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "thinking":
                    txt = b.get("thinking") or b.get("text", "")
                    pending["think"].append(txt)
                    # 按原生 type 进入时间线（thinking 块字段即原生 thinking）
                    pending["seq"].append({"type": "thinking", "thinking": txt})
                elif bt == "text":
                    t2 = b.get("text", "")
                    if THINK_SENT in t2 or ANSW_SENT in t2:
                        for kind, txt in _classify_text(None, t2):
                            if kind == "thinking":
                                pending["think"].append(txt)
                                pending["seq"].append({"type": "thinking", "thinking": txt})
                            else:
                                pending["ans"].append(txt)
                                pending["seq"].append({"type": "text", "text": txt})
                    else:
                        pending["ans"].append(t2)
                        pending["seq"].append({"type": "text", "text": t2})
                elif bt == "tool_use":
                    # 结构化工具调用（如 end_discussion）：收集进 tool_calls，UI 同气泡内渲染
                    # （之前漏解析，导致 Claude 发出的调用输出/reason 参数在界面消失）
                    tc = {"name": b.get("name", ""), "input": b.get("input", {}) or {}}
                    pending["tc"].append(tc)
                    # 按原生 type 进入时间线（name/input 即原生字段）
                    pending["seq"].append({"type": "tool_use", "name": tc["name"], "input": tc["input"]})
    flush()  # 文件尾收口最后一条气泡
    return turns


def _codex_iter_payloads(path: str):
    """逐行读 codex rollout，产出 (payload, 原始行对象)。

    codex 0.146.1 的 rollout（`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`）外层是
    `{"type": "response_item"|"event_msg"|..., "payload": {...}}`，**真实内容全在 payload 下**。
    旧实现直接读顶层的 `o["message"]` / `o["role"]`（chat-completions 风格），对这种格式
    解析出 0 个回合 —— 导致 codex agent 的历史重建（reconcile）完全失效。
    """
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                o = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if isinstance(o, dict):
                # 新格式（0.146.1）内容在 payload 下；老格式（早期版本）把 message 放在顶层、
                # 无 payload 键 —— 此时退回整行对象，交给下面的形态判定兼容处理。
                yield (o.get("payload") or o), o


def _codex_content_text(content) -> str:
    """从 codex 的 content 里取正文文本（str，或 [{type: text|output_text, text}]）。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") in ("text", "output_text"):
                t = b.get("text")
                if isinstance(t, str) and t.strip():
                    parts.append(t)
        return "\n".join(parts).strip()
    return ""


def _codex_assistant_text(pl: dict) -> str:
    """从各个 codex 版本的 payload 形态里取 assistant 正文。

    - 新（0.146.1）：`response_item/message`，role=assistant，content[].type=output_text；
      另有 `event_msg/agent_message`（message 为字符串）内容**与之重复**。
    - 旧（早期版本）：`event_msg` + 顶层/ payload 里的 `message{role:assistant, content[]}`。
    重复由调用方按「文本去重」处理，不在这里判类型，以免某个版本只发其中一种时漏读。
    """
    msg = pl.get("message")
    # 只认 agent_message —— `event_msg/user_message` 也是 message 字符串（那是**用户提示词**，
    # 不是 assistant 正文）；漏掉这个类型判断会把提示词混进正文。
    if isinstance(msg, str) and msg.strip() and pl.get("type") == "agent_message":
        return msg.strip()
    if isinstance(msg, dict) and msg.get("role") == "assistant":
        return _codex_content_text(msg.get("content"))
    if pl.get("role") == "assistant":
        return _codex_content_text(pl.get("content"))
    return ""


def _codex_reasoning_texts(path: str) -> list[str]:
    """按文件顺序取出 rollout 里全部 reasoning 明文（用于展示 codex 的「思考」）。

    codex `exec --json` 的事件流**不含 reasoning**（2026-09-11 实测：只有 thread.started /
    turn.started / item.completed / turn.completed，推理只以 `usage.reasoning_output_tokens`
    计数出现；`hide_agent_reasoning=false` + `show_raw_agent_reasoning=true` 也不会输出）。
    但 rollout 里写明文：`response_item` → payload.type == "reasoning" →
    content[].text（type == "reasoning_text"；summary 为空、encrypted_content 只是短 id）。
    """
    out: list[str] = []
    try:
        for pl, _raw in _codex_iter_payloads(path):
            if pl.get("type") != "reasoning":
                continue
            for b in (pl.get("content") or []):
                if isinstance(b, dict) and b.get("type") == "reasoning_text":
                    t = (b.get("text") or "").strip()
                    if t:
                        out.append(t)
    except OSError:
        pass
    return out


# codex 的 MCP 工具调用 item 名：**同一份 codex、两条输出通路的命名不同**——
#   - app-server（JSON-RPC 通知，本 app 默认走这条）：`item.type == "mcpToolCall"`（驼峰）
#   - exec（`codex exec --json`）与 rollout 文件：`item.type == "mcp_tool_call"`（下划线）
# 只判一种写法会导致另一条通路彻底漏掉 end_discussion 投票（实测 2026-09-15：app-server
# 发 camelCase，判的是 snake_case → codex 的终止票实时全丢，辩论只能靠轮次耗尽收尾）。
# 判定集中在此处，两条通路共用，避免再次分叉。
_MCP_TOOL_ITEM_TYPES = ("mcpToolCall", "mcp_tool_call")


def _is_mcp_tool_item(item) -> bool:
    """item 是否为 codex 的 MCP 工具调用条目（兼容驼峰/下划线两种命名）。"""
    return isinstance(item, dict) and item.get("type") in _MCP_TOOL_ITEM_TYPES


def _codex_parse_args(raw) -> dict:
    """codex function_call 的 arguments 是 JSON 字符串（如 '{"reason": "..."}'）。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            d = json.loads(raw)
            return d if isinstance(d, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


# codex **原生**（非 MCP）工具条目 → 统一显示名。与 _MCP_TOOL_ITEM_TYPES 同源问题：同一份
# codex，两条通路的命名不一样，故键一律按小写归一比较、只在此处列全，两条通路共用本表。
# 2026-09-16 实测（codex-cli 0.146.1）：
#   - `codex exec --json`：`item.type="command_execution"`，带 command / aggregated_output / exit_code；
#   - rollout 文件：`response_item.payload.type="function_call"`、`name="exec_command"`、
#     `arguments='{"cmd":"echo codex-tool-test"}'`，紧随一条 `function_call_output`；
#   - app-server：审批通知走 `method="item/commandExecution/requestApproval"`（驼峰）。
_CODEX_NATIVE_TOOLS = {
    "command_execution": "exec_command",   # exec / rollout 命名
    "commandexecution": "exec_command",    # app-server 命名（驼峰）
    "file_change": "apply_patch",
    "filechange": "apply_patch",           # app-server 命名（驼峰）
}


def _codex_native_tool_use(item) -> dict | None:
    """codex 原生工具条目 → 与 claude 同构的 tool_use（{name, input}）；非工具条目返回 None。

    这些条目此前被整条忽略 ⇒ codex 明明调了自带工具（shell / 打补丁），界面里却什么都不显示
    （2026-09-16 test5 实测：claude 调 Bash 有痕、codex 侧一律空白）。此处只产出 name/input
    两个字段，前端是通用渲染（不认工具名）⇒ 无需第二套 schema，前端零改动即可显示。
    """
    if not isinstance(item, dict):
        return None
    name = _CODEX_NATIVE_TOOLS.get(str(item.get("type") or "").lower())
    if not name:
        return None
    if name == "exec_command":
        cmd = item.get("command") or item.get("cmd")
        if not cmd:
            # rollout 的入参在 arguments（JSON 字符串）里，形如 {"cmd": "..."}
            cmd = _codex_parse_args(item.get("arguments")).get("cmd")
        inp: dict = {"command": cmd or ""}
        if item.get("exit_code") is not None:
            inp["exit_code"] = item.get("exit_code")
        return {"name": name, "input": inp}
    return {"name": name, "input": {"patch": item.get("patch") or item.get("changes") or ""}}


# codex 原生工具**输出**的字段名（三条通路各异，统一在此兜底）：
#   - exec --json：item.aggregated_output / exit_code（snake）
#   - rollout：`function_call_output` 的 output（字符串，内含 exit code 文本）
#   - app-server：item.aggregatedOutput / exitCode（camel）
_CODEX_TOOL_OUTPUT_FIELDS = ("aggregated_output", "aggregatedOutput", "formatted_output",
                             "formattedOutput", "output", "stdout")


def _codex_tool_output(item) -> tuple:
    """codex 原生工具条目 → (输出文本, 是否失败)。无输出返回 ("", False)。

    与 claude 侧 `_extract_tool_results` 对位：claude 的 tool_result 由 CLI 直接给出
    content/is_error，codex 没有统一字段，故这里按通路兜字段名 + 从输出文本里抠 exit code。
    """
    if not isinstance(item, dict):
        return "", False
    raw = None
    for k in _CODEX_TOOL_OUTPUT_FIELDS:
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            raw = v
            break
        if isinstance(v, list) and v:
            raw = v
            break
    if isinstance(raw, list):
        raw = "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in raw)
    full = _clean_sentinels(str(raw or "")).strip()
    text = full
    # rollout 的 output 头带执行元信息（Chunk ID / Wall time / Process exited with code N /
    # Original token count），界面上是噪声：剥到 `Output:` 之后只留真正的命令输出。
    if "\nOutput:\n" in text:
        text = text.split("\nOutput:\n", 1)[1].strip()
    code = item.get("exit_code")
    if code is None:
        code = item.get("exitCode")
    if code is None:
        # 退出码只在回执头里（`Process exited with code N`）⇒ 必须从**剥离前**的全文里抠
        m = re.search(r"(?:exited with code|exit code)\s*(-?\d+)", full)
        code = int(m.group(1)) if m else None
    return text, (isinstance(code, int) and code != 0)


def _codex_native_tool_result(item) -> dict | None:
    """codex 原生工具条目 → 与 claude 同构的 tool_result；无输出且未失败时返回 None。

    此前三条通路**都只产 tool_use、不产 tool_result** ⇒ codex 界面里工具「有调用、没结果」，
    而 claude 侧正常（2026-09-16 用户实测：codex 执行 shell 后看不到输出）。编排层与前端
    本就支持 tool_result（orchestrator._run_turn / index.html makeToolResultBlock），无需改动。
    """
    if not _codex_native_tool_use(item):
        return None
    text, is_err = _codex_tool_output(item)
    if not text:
        if not is_err:
            return None
        text = "（退出码非 0，且无输出）"
    return {"is_error": is_err, "content": text}


def _parse_codex_transcript(path: str) -> list:
    """解析 codex rollout 为一个 agent 的回合序列（供对账覆盖本地历史）。

    把 codex 的原生 item 映射成**与 claude 同一套块类型**、复用 `_normalize_claude_blocks`
    产出时间线 blocks，前后端不需要第二套 schema。
    codex 0.146.1 实测 payload 形态：
    - `message`（role=user）→ 一次新回合的起点（收口上一回合）
    - `reasoning` → content[].type=reasoning_text：**思考**
    - `message`（role=assistant）→ content[].type=output_text：**正文**
    - `function_call` → name / arguments（JSON 字符串）
    - `event_msg/agent_message` 与 assistant message **内容重复**，一律不取（避免双计）；
      `event_msg/task_complete` 同理不取。
    """
    turns: list = []
    pending = None

    def flush():
        nonlocal pending
        if pending is None:
            return
        content = _clean_sentinels("\n\n".join(pending["ans"]).strip()).strip()
        thinking = _clean_sentinels("\n\n".join(pending["think"])).strip()
        tc = pending["tc"]
        if content or thinking or tc or pending["trs"]:
            t = _normalize_turn(content, thinking, tc, pending["trs"])
            t["blocks"] = _normalize_claude_blocks(pending["seq"])
            if pending.get("ts"):
                t["ts"] = pending["ts"]
            turns.append(t)
        pending = None

    def _open(ts):
        nonlocal pending
        if pending is None:
            pending = {"think": [], "ans": [], "tc": [], "seq": [], "seen": set(), "ts": ts,
                       "trs": [], "calls": {}}

    for pl, raw in _codex_iter_payloads(path):
        pt = pl.get("type")
        ts = raw.get("timestamp")
        if pt == "message" and pl.get("role") == "user":
            flush()
            _open(ts)
            continue
        if pt == "reasoning":
            _open(ts)
            for b in (pl.get("content") or []):
                if isinstance(b, dict) and b.get("type") == "reasoning_text" and (b.get("text") or "").strip():
                    pending["think"].append(b["text"])
                    pending["seq"].append({"type": "thinking", "thinking": b["text"]})
            continue
        # assistant 正文：兼容多种版本形态；event_msg/agent_message 与 response_item 的
        # assistant message **内容重复**，故按文本去重（否则正文会被拼两遍）。
        txt = _codex_assistant_text(pl)
        if txt:
            _open(ts)
            if txt not in pending["seen"]:
                pending["seen"].add(txt)
                pending["ans"].append(txt)
                pending["seq"].append({"type": "text", "text": txt})
            continue
        if pt == "function_call":
            name = pl.get("name") or ""
            if not name:
                continue
            _open(ts)
            # end_discussion（MCP 终止票）与 codex 原生工具（exec_command / apply_patch 等）
            # 共用同一套 tool_use 结构。原生工具也必须在此认下来，否则重开历史会话时，
            # 实时落盘的工具块会被 CLI 侧覆盖抹掉（store._merge_tool_blocks：CLI 侧一旦
            # 有工具块就整体采信，不再并回本地）。
            tc = {"name": name, "input": _codex_parse_args(pl.get("arguments"))}
            pending["tc"].append(tc)
            pending["seq"].append({"type": "tool_use", "name": tc["name"], "input": tc["input"]})
            # 记下 call_id → 该 tool_use 在 seq 中的下标，供紧随的 function_call_output 归位
            if pl.get("call_id"):
                pending["calls"][pl["call_id"]] = len(pending["seq"]) - 1
            continue
        if pt == "function_call_output":
            # 工具回执（rollout 通路）：此前**整条被忽略** ⇒ codex 工具在界面里只有调用、
            # 没有结果（claude 侧有 tool_result，两边不一致）。按 call_id 贴到对应 tool_use
            # 之后；缺 call_id 时退回顺序追加（时间线本就紧邻）。
            _open(ts)
            text, is_err = _codex_tool_output(pl)
            if not text and not is_err:
                continue
            if not text:
                text = "（退出码非 0，且无输出）"
            tr = {"is_error": is_err, "content": text}
            pending["trs"].append(tr)
            blk = {"type": "tool_result", "is_error": is_err, "content": text}
            at = pending["calls"].get(pl.get("call_id")) if pl.get("call_id") else None
            if isinstance(at, int) and 0 <= at < len(pending["seq"]):
                pending["seq"].insert(at + 1, blk)
                # 插入后其后所有下标右移，同步修正已记录的 call_id → 下标映射
                for k, v in list(pending["calls"].items()):
                    if v > at:
                        pending["calls"][k] = v + 1
            else:
                pending["seq"].append(blk)
            continue
    flush()
    return turns


def read_cli_transcript(cli_session_id: str, agent_name: str):
    """打开会话时，从 CLI 会话 transcript 读取该 agent 的完整回合序列。

    返回 None 表示找不到 transcript（agent 不可连 / 全新会话）→ 调用方应保持本地不动（离线回退）；
    返回 [] 表示找到文件但无 assistant 回合；返回有序列表表示可据以覆盖本地。
    """
    if not cli_session_id:
        return None
    p = _claude_transcript_path(cli_session_id, agent_name)
    if p:
        return _parse_claude_transcript(p)
    p = _codex_transcript_path(cli_session_id)
    if p:
        return _parse_codex_transcript(p)
    return None
