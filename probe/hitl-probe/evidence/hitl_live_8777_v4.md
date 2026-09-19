# HITL 留痕测试评估 · 去选项（自由文本回复）· 2026-09-19

## 本轮改动
- 前端 HITL 面板移除「选项列表让用户点选」（`hitlOptions` 容器与 `.hitl-opt` 按钮逻辑删除），只留问题 + 自由文本输入框。
- 提示词 `hitl_chat`/`hitl_debate`/`hitl_opening` 去掉 `options` 示例与「给 2-4 个具体选项」要求；MCP 工具 `request_human_input` schema 删除 `options` 字段。
- 后端对 `options` 保持兼容（有则透传、无则空），UI 不再渲染。
- 单元测试：前端 `test_frontend_render.js` 改为断言「选项容器已移除 + 自由文本提交」；全量 322 例通过。

## 留痕运行（session `s_2f8ead46`，项目 `pr_0335148e` 双壳 codex+claude 本地 gemma4:26b）
驱动器：`drive_hitl.py` 指向 8777，TASK 改为「调 request_human_input 问问题，用户会自由回复，无需提供候选项」。

结果：
- `human_input_required` ×2，触发者 = `Codex (本地 gemma)`、`Claude Code (本地 gemma)`；两事件 `options` 均为 `[]`（无选项列表下发前端）。
- 工具调用：`request_human_input` 出现 3 次——Codex ×2（`input.options=None`）、Claude ×1（`mcp__terminate-signal__request_human_input`，`input.options=None`）；另 Claude `end_discussion` ×1。
- 人工回答（自由文本「每周 3-4 次…我更倾向早上训练。」）由驱动器经 WS 提交成功；辩论续跑至 `run_end`，**0 错误/异常事件**。
- 会话已落入 留痕项目列表（`message_count=12`），符合留痕两处落盘要求。

## 与上一轮（s_b09223c3，带 options 任务）对比
| 项 | s_b09223c3（带 options） | s_2f8ead46（去 options） |
|---|---|---|
| Codex 调 `request_human_input` | 0 次（模型侧把 options 当不可见内建通道回避） | 2 次 |
| Claude 调 `request_human_input` | 1 次 | 1 次 |
| 下发前端 options | — | `[]` |
| 自由文本回答续跑 | 是 | 是 |
| 错误事件 | 0 | 0 |

**结论**：印证用户判断——「选项交互要训练模型、很不稳、codex 难触发」。去掉 `options` 后，核心 HITL（提问→暂停整场→用户自由回复→续跑）反而更稳：**Codex 本轮能正常触发 `request_human_input`**（上一轮完全不触发），且不再要求模型产出结构化候选项。

## 过程问题（非 HITL 代码 bug）
8777 重启后用默认 uvicorn，WS 默认 keepalive（ping_timeout=20s）会在 Codex 长生成轮触发驱动端「1011 keepalive ping timeout」假断线（服务端未崩溃，仍 running）。驱动器侧关掉客户端 ping（`ping_interval=0`）后跑通。属**服务端 keepalive 配置差异**（原 8777 实例更宽容），浏览器原生 WS 不受影响。若用户自行重启 8777，建议沿用原启动参数或加 `--ws-ping-timeout 300`。

## 证据
- 原始事件：`probe/hitl-probe/raw/hitl_live_8777_v4.events.jsonl` / `.log`
- 会话：`pr_0335148e` / `s_2f8ead46`
