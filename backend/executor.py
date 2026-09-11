import asyncio
import glob
import json
import os
import re
import shutil
import sys
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
    """stream-json 输入帧：把一轮 prompt 封装为 user 消息（SDK 同款格式）。"""
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
    }, ensure_ascii=False).encode("utf-8")


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

    def __init__(self, agent: AgentConfig, cli_session_id: str | None = None):
        self.agent = agent
        self.cli_session_id = cli_session_id  # None = 本 app 会话内该 agent 尚未开过 CLI 会话
        self._force_new_session = False  # 自愈：True 时下一轮用全新 id 注册（绕过已占用的旧锁）
        self._fake_history: list[str] = []
        self._section = None  # 当前文本分片归属：None / "think" / "ans"（跨 claude 多事件续接用）

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
            # claude 2.x 会把 ~/.claude/settings.json 的 env 合并进运行时并**覆盖**同名进程环境变量，
            # 仅靠子进程 env 注入 ANTHROPIC_BASE_URL 会被全局配置（如指向 deepseek）盖掉、per-agent 网关失效。
            # 故改用隔离配置目录：复制用户 settings（去插件以避免临时目录缺失插件文件报错）、
            # 改写 env.ANTHROPIC_BASE_URL 为目标网关，并用 CLAUDE_CONFIG_DIR 指向它——
            # 子进程只读这份隔离配置，完全不动用户全局 ~/.claude。
            env["CLAUDE_CONFIG_DIR"] = self._claude_config_dir()
            env["ANTHROPIC_BASE_URL"] = self.agent.base_url
            # claude 仍要求一个 key 头；网关侧放行任意值，故为空时补 dummy
            env["ANTHROPIC_API_KEY"] = self.agent.api_key or "local-gateway"
            # 让 claude 接受任意 model id（网关按此路由到具体本地模型），跳过内置校验
            if self.agent.model:
                env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = self.agent.model
                env["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"] = self.agent.model
        return env

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
        if not sfile.exists():
            src = Path.home() / ".claude" / "settings.json"
            d: dict = json.loads(src.read_text(encoding="utf-8")) if src.exists() else {}
            d.pop("enabledPlugins", None)  # 避免临时目录缺失插件文件导致加载报错
            d.setdefault("env", {})
            d["env"]["ANTHROPIC_BASE_URL"] = self.agent.base_url
            d["env"]["ANTHROPIC_API_KEY"] = self.agent.api_key or "local-gateway"
            if self.agent.model:
                d["env"]["ANTHROPIC_CUSTOM_MODEL_OPTION"] = self.agent.model
                d["env"]["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"] = self.agent.model
            # 允许项目级 .mcp.json 的 MCP 服务器自动加载（end_discussion 工具可达）。
            # 缺失实测（「夏朝存在吗」会话）：gemma 看不到工具、只能输出 json 文本兜底。
            d["enableAllProjectMcpServers"] = True
            sfile.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
        else:
            # 旧生成的隔离配置：补齐 enableAllProjectMcpServers（若缺）
            try:
                d = json.loads(sfile.read_text(encoding="utf-8"))
                if not d.get("enableAllProjectMcpServers"):
                    d["enableAllProjectMcpServers"] = True
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
        # codex
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
            yield kind, text


    async def _run_claude_once(self, prompt: str, cwd: str, timeout: int):
        cmd = self._claude_cmd()
        env = self._claude_env()
        # 确保项目 .mcp.json + 隔离配置目录的 trust/MCP 启用（end_discussion 工具可达）
        _ensure_project_mcp(cwd, self._claude_config_dir())
        self._section = None  # 新一轮：重置思考/答案归属状态
        async for kind, payload in _stream_subprocess(cmd, cwd, timeout, parse="claude", env=env,
                                                      runtime=self, stdin_data=_user_frame(prompt)):
            # text / thinking 整块回放为逐字流（claude 外壳对本地模型整块输出时的兜底）
            if kind in ("text", "thinking") and payload:
                async for seg in _replay(kind, payload):
                    yield seg
            else:
                yield kind, payload

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
        if self.agent.model:
            cmd += ["-m", self.agent.model]
        if self.agent.cli_args:
            cmd += self.agent.cli_args.split()
        if self.cli_session_id is not None:
            cmd += ["resume", self.cli_session_id]
        cmd += [prompt]
        return cmd

    async def _run_codex(self, prompt: str, cwd: str, timeout: int):
        cmd = self._codex_cmd(prompt)
        async for ev in _stream_subprocess(cmd, cwd, timeout, parse="codex", runtime=self):
            yield ev


# ---------------- 通用子进程流式读取 ----------------
async def _stream_subprocess(cmd: list, cwd: str, timeout: int, parse: str, runtime=None, env=None,
                             stdin_data: bytes | None = None):
    """启动子进程并按行产出 ("text", str) 或 ("error", str)。

    env：显式传入则完全覆盖子进程环境变量（用于 per-agent 网关隔离）；
    为 None 时继承当前进程环境（默认行为）。
    stdin_data：非空时写入子进程 stdin 后关闭（stream-json 输入帧用）。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, env=env,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:  # 命令不存在 / 权限问题
        yield ("error", f"启动失败: {e}")
        return
    assert proc.stdout is not None
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
            elif parse == "codex":
                for kind, payload in _parse_codex_line(text, runtime):
                    yield kind, payload
            else:
                if text.strip():
                    yield ("text", text)
        await proc.wait()
        if proc.returncode not in (0, None):
            err = ""
            if stderr_tail:
                err = b"".join(stderr_tail).decode(errors="replace")
            yield ("error", f"退出码 {proc.returncode}: {err[:800]}")
        if not drain_task.done():
            drain_task.cancel()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        if not drain_task.done():
            drain_task.cancel()
        yield ("error", f"单轮超时（>{timeout}s）已终止")
    except Exception as e:
        try:
            proc.kill()
        except Exception:
            pass
        if not drain_task.done():
            drain_task.cancel()
        yield ("error", f"执行异常: {e}")


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
    # 流式增量
    delta = ev.get("delta")
    if isinstance(delta, dict):
        # 思考增量（Anthropic thinking_delta，非网关路径）：独立产出 thinking 事件
        if isinstance(delta.get("thinking"), str) and delta["thinking"]:
            return [("thinking", delta["thinking"])]
        d = delta.get("text") or ""
        if d:
            return _classify_text(runtime, d)
    # assistant 整块：标准 thinking block（直连其他 client）或网关双 text 块（带哨兵）
    if t == "assistant":
        msg = ev.get("message") or {}
        out = []
        for item in (msg.get("content") or []):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "thinking" and item.get("thinking"):
                out.append(("thinking", item["thinking"]))
            elif item.get("type") == "text" and item.get("text"):
                out += _classify_text(runtime, item["text"])
            elif item.get("type") == "tool_use":
                # 结构化工具调用（如 end_discussion）：透传给编排层做终止状态机
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
    # MCP 工具调用（codex 0.146.1）：`item.completed` 里 item.type == "mcp_tool_call"，
    # 形如 {"server":"terminate-signal","tool":"end_discussion","arguments":{"reason":...}}。
    # 必须映射成与 claude 侧完全一致的 ("tool_use", {...})，否则编排层的终止状态机
    # 收不到 codex 的投票（表现为「codex 只会说话、不会投终止票」）。
    # 只认 item.completed：mcp_tool_call 会发 started + completed 两条，取 completed
    # 既保证「必有」、又不会重复产事件（agent_message 也只发 completed）。
    item = ev.get("item")
    if isinstance(item, dict) and item.get("type") == "mcp_tool_call":
        if ev.get("type") == "item.completed" and "end_discussion" in str(item.get("tool") or ""):
            args = item.get("arguments") or {}
            return [("tool_use", {
                "name": "end_discussion",
                "input": {"reason": str(args.get("reason", "") or "")},
            })]
        return []   # 其余 mcp 事件既不产文本、也不重复产 tool_use
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


def _parse_codex_transcript(path: str) -> list:
    """解析 codex JSONL（格式多变，尽力而为）：解析不出内容则返回空列表，对账时该 agent 跳过。"""
    import re as _re
    turns = []
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                o = json.loads(ln)
            except json.JSONDecodeError:
                continue
            content = None
            if isinstance(o.get("message"), dict) and o["message"].get("role") == "assistant":
                content = o["message"].get("content")
            elif o.get("role") == "assistant":
                content = o.get("content")
            if content is None:
                continue
            txt, thinking = "", ""
            if isinstance(content, str):
                txt = content
            elif isinstance(content, list):
                for b in content:
                    if isinstance(b, dict):
                        if b.get("type") in ("text",):
                            txt += b.get("text", "")
                        elif b.get("type") in ("thinking", "reasoning"):
                            thinking += (b.get("thinking") or b.get("text", ""))
            if txt.strip() or thinking.strip():
                c = _re.sub(r"^\s*---\s*", "", _re.sub(r"\s*---\s*$", "", _clean_sentinels(txt).strip())).strip()
                th = _clean_sentinels(thinking).strip()
                turns.append(_normalize_turn(c, th))
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
