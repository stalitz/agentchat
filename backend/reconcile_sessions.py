"""对某项目全部会话重新执行 CLI transcript 对账，修复历史数据问题。

典型场景：
- claude-code 外壳曾把「一次回复」拆成多个独立 assistant 事件（thinking-only + text-only），
  旧解析把拆条各当一条回合、reconcile 后本地多出「思考空答案 + 无思考答案」的拆条残留；
  修复解析器后运行本工具，用合并后的回合重新对账、删除残留，历史回到与 CLI 一致。

用法：
    .venv/bin/python -m backend.reconcile_sessions <project_path> [--backup]

说明：无 transcript（离线 / fake agent）的会话保持本地不动；--backup 先备份 sessions.json。
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import executor, store  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv:
        print("用法: python -m backend.reconcile_sessions <project_path> [--backup]")
        return 1
    project_path = argv[0]
    backup = "--backup" in argv
    sessions = store._load_sessions(project_path)
    if not sessions:
        print(f"无会话: {project_path}")
        return 1
    if backup:
        f = store._sessions_file(project_path)
        bak = str(f) + ".bak-reconcile"
        shutil.copy2(f, bak)
        print(f"备份 -> {bak}")
    total = {"changed": 0, "appended": 0}
    for s in sessions:
        for agent_name, cli_id in (s.get("agent_sessions") or {}).items():
            cli_turns = executor.read_cli_transcript(cli_id, agent_name)
            if not cli_turns:
                print(f"  {s['id']} {agent_name}: 无 transcript，跳过")
                continue
            res = store.reconcile_agent_messages(project_path, s["id"], agent_name, cli_turns)
            print(f"  {s['id']} {agent_name}: changed={res['changed']} appended={res['appended']}")
            total["changed"] += res["changed"]
            total["appended"] += res["appended"]
    print(f"完成: changed={total['changed']} appended={total['appended']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
