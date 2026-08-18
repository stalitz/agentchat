"""测试公共基础：必须在 import backend 之前设置 AGENTCHAT_CONFIG，
把存储重定向到临时文件，避免污染真实 data/config.json。
所有测试模块第一行都 from tests.base import * """
import os
import tempfile
import uuid
import pathlib

TMP = pathlib.Path(tempfile.mkdtemp(prefix="agentchat_test_"))
CONFIG_PATH = TMP / "config.json"
os.environ["AGENTCHAT_CONFIG"] = str(CONFIG_PATH)

import backend.store as store          # noqa: E402  (env 已在上方设置)
import backend.main as main             # noqa: E402
import backend.models as models         # noqa: E402
import backend.orchestrator as orch     # noqa: E402
import backend.executor as executor     # noqa: E402

def fresh_config():
    """每个用例用独立配置文件，避免用例间互相污染。"""
    p = TMP / (uuid.uuid4().hex + ".json")
    store.CONFIG_FILE = p
    return p

# 测试用 Agent 工厂（max_rounds / timeout 已移出 agent，不再设）
def make_agent(name="A", atype="fake", role="测试角色",
               fake_terminate_round=0):
    return models.AgentConfig(
        id="", name=name, type=atype, role=role,
        fake_terminate_round=fake_terminate_round,
    )

def make_project_dir(tag="proj"):
    p = TMP / f"proj_{tag}"
    p.mkdir(parents=True, exist_ok=True)
    return str(p)
