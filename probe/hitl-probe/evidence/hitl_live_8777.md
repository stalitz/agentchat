# HITL 实时测试 · 8777 留痕项目（2026-09-19 12:27）

## 环境
- 目标：真实 app `http://127.0.0.1:8777`（**新代码已加载**——PID 39596 启动 12:27:19，晚于 HITL 提交 9776a85/772da6f 的 12:18:36）。
- 项目：`pr_0335148e`（agent探针(留痕)），团队 `tm_a0242d79`（双壳 · 本地 gemma：Codex + Claude Code，均 gemma4:26b 免费本机模型）。
- 驱动：复用 `probe/hitl-probe/drive_hitl.py`，WS/HTTP 指向 8777，`PROBE_PROJECT/TEAM` 指向留痕项目与该双壳团队。
- 任务：设计跳高训练计划，第一步调用 `request_human_input` 向用户确认「每周能保证几次训练」。

## 结果
- 会话：`s_b09223c3`，**已确认落入 `pr_0335148e`**（项目会话总数 23，含本会话）。
- `human_input_required` 触发 **1 次**，触发者 = **Claude Code (本地 gemma)**，question = `你现在每周能保证几次训练？`。
  - 驱动侧提交人工回答 `每周 3-4 次，每次 60-90 分钟。`
  - 后端回显 `human` 消息并广播 `已提交人工回答，讨论将继续。`
  - Claude 续跑并在正文引用答案（token `每周…`），随后又起 3 个 turn。
- Claude 工具调用：`mcp__terminate-signal__request_human_input`（**MCP 路径正常**）。
- Codex 工具调用：9 次，均为 `exec_command` / `write_stdin`（自驱内部 turn），**0 次 `request_human_input`** → 印证 codex+Ollama Default mode 模型侧看不到该工具、退化到正文。
- 事件总数 4570；420s 超时内未收 `run_end`（codex 自驱多轮致事件量大），HITL 闭环已验证完毕。

## 结论
1. **MCP `request_human_input` 的 HITL 闭环在真实 app + 留痕项目端到端跑通**：暂停 → 人工回答 → 回灌 user 消息 → 续跑。无回归。
2. **新接的 codex 原生 `requestUserInput` 路径本次未被触发**：codex 在 Default mode 下不发该 ServerRequest（模型侧可见性限制，代码无解），故 live 未覆盖该分支；该分支由 `tests/test_hitl.py` 三层单元/集成测试覆盖。
3. 本次验证证明 commit 9776a85 的改动（删除静默空应答、转交编排层）**未破坏**现有 HITL 链路。

## 证据文件
- `probe/hitl-probe/raw/hitl_live_8777.events.jsonl`：WS 原始事件（4570 条）。
- `probe/hitl-probe/raw/hitl_live_8777.log`：运行日志（含 human_input_required 与回答回显）。
