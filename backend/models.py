from typing import Literal, Optional
from pydantic import BaseModel

AgentType = Literal["claude-code", "codex", "fake"]


class AgentConfig(BaseModel):
    id: str = ""
    name: str
    type: AgentType
    role: str = ""            # 角色 / system prompt
    model: Optional[str] = None
    cli_args: str = ""        # 额外 CLI 参数（空格分隔）
    # 仅 fake 类型使用：在第 N 轮（含）起回复末尾加 TERMINATE 触发收敛停。0=永不
    fake_terminate_round: int = 0
    # agent 级「本地模型网关」接入（走 Claude Code 外壳加载本地模型，如 Ollama 的 gemma/deepseek）。
    # 设置后，编排器在【拉起 claude 子进程时】注入 ANTHROPIC_BASE_URL 等环境变量，
    # 从而覆盖 Claude Code 自身全局配置，把该 agent 指向指定网关，绝不串到默认 deepseek。
    # 不设置则沿用 Claude Code 全局配置（即默认后端）。
    base_url: Optional[str] = None   # 网关地址，如 http://localhost:4000（litellm 等翻译网关，其后接 Ollama/本地模型）
    api_key: Optional[str] = None    # 网关所需 key（可为空；运行时补 dummy，网关侧放行即可）


class TeamConfig(BaseModel):
    id: str = ""
    name: str
    agent_ids: list[str] = []


class AppSettings(BaseModel):
    """App 全局配置（存 config.json 顶层 settings）。"""
    default_timeout: int = 300          # 单轮静默超时（秒），全局生效
    default_max_rounds: int = 12        # 辩论模式默认轮次上限，全局生效（composer 轮次输入默认值来源）
    default_model: str = ""             # claude-code 默认模型名（留空=自动探测 Claude Code 当前默认；用于展示，pin 后可覆盖）
    default_model_codex: str = ""       # codex 默认模型名（留空=自动探测 Codex 当前默认；用于展示，pin 后可覆盖）
    ollama_base_url: str = "http://localhost:11434"  # 内置网关转发目标（只读 Ollama 的 OpenAI 兼容端点，不改 Ollama 配置）


class ProjectConfig(BaseModel):
    id: str = ""
    name: str
    path: str                 # 项目目录
    team_ids: list[str] = []
