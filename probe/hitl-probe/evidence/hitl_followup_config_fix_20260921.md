# HITL 回复轮连调根治 · config 文案改动 + UI 缺口诊断（2026-09-21）

## 一、根因回顾（已实证）
v12b 跑通完整 3 轮，但 codex 第 2 轮（用户回复后的发言，line 1772–2496）**同 turn 内连调 8 次** `ask_human_input`，全问「装修风格」只是 phrasing 变体。
第 1 轮（首问预算）只调 1 次。诱导源 = `hitl_followup_debate` 模板里的「**继续调用** ask_human_input 工具问即可（**次数不限**）」——
gemma 理解为可反复调，而回执是同步的、不回灌答案，没等到就换说法重发（call_id 互异，去重只挡同 id、挡不住变体）。

## 二、本次改动（用户授权「改」，只改 config、不动代码）
文件：`data/config.json`、`probe/hitl-probe/config.json` 的 `settings.prompt_templates.debate`

`hitl_followup_debate` 关键句：
- 旧：`**继续调用 ask_human_input 工具**问即可（次数不限，回答会作为后续输入给你），不要在正文里反问。`
- 新：`**继续调用 ask_human_input 工具**问即可（每次发言只调用一次，回答会作为后续输入给你），不要在正文里反问，也不要在同一次发言里重复调用。`

`hitl_followup_chat` 同样去「次数不限」，加「每次发言只调用一次」「不要在同一次发言里重复调用」。

验证：两 config 写入成功，`hitl_followup_debate != 代码默认`（覆盖生效）、含「每次发言只调用一次」、`次数不限` 已剔除。

## 三、UI 缺口诊断（用户报告「全局设置里找不到回复提示词」）
代码证据 `frontend/index.html` 全局设置面板 `openSettings()`（line 2104–2195）只渲染 **2 个** prompt_templates 字段：
- `gConsensus` → `prompt_templates.debate.consensus`（共识终止提示词，line 2162–2169）
- `gExtraRules` → `prompt_templates.debate.extra_rules`（附加规则提示词，line 2170–2177）

`submitSettings()`（line 2213–2224）只回写 `consensus` / `extra_rules` 两个 key，**没有** `hitl_followup_debate/chat`。
⇒ 配置层支持任意 `prompt_templates.debate` key 覆盖（orchestrator `self._pt` 合并），但 **UI 没给 `hitl_followup` 编辑入口**。

结论：用户在 UI 找不到是真实 UI 缺口，非操作问题。要让回复提示词在 UI 可改，需给 `openSettings`/`submitSettings` 补 `hitl_followup_debate`/`hitl_followup_chat` 两个编辑框（含「恢复默认」按钮，复用现有 `setTemplateDefault` 模式）。此属前端改动，待用户授权。

## 四、v12c 重跑结果（环境抖动污染，不可作为有效验证）
- v12c（s_0638df5c，隔离8790）`run_end=true` 但 **codex 回复轮报 `Reconnecting... 1/5` 连接错误，会话中止，未跑完3轮辩论**。
- 工具事件=7：codex `ask_human_input`×2（首轮同问题「预算上限」重发 line199/200，疑似链路抖动重发，非提示词诱导）、claude `mcp__terminate-signal__ask_human_input`×5（追问连调，被单待问收敛成2次 human_input_required）。
- `human_input_required`=3（codex1+claude2）。用户侧仍只暂停1次/轮。
- **结论**：因 codex 连接抖动导致 run 中止，无法干净验证「去次数不限」是否消除回复轮连调。需重跑 v12d（Ollama 稳定时）。

## 五、UI 入口补上（用户授权「补 UI 入口」）
`frontend/index.html` 三处改动（**串行 Edit**，并行会互相覆盖——本次初版4个并行 Edit 只有1个落盘，已串行重做）：
- `setTemplateDefault`(2199)：keys 字典扩展支持 `gHitlFollowDebate→hitl_followup_debate`、`gHitlFollowChat→hitl_followup_chat`。
- `openSettings` 渲染(2177后)：新增「HITL 回复提示词（用户回答后）」section，含两个 textarea（辩论/单聊）+ 恢复默认按钮，复用 `setTemplateDefault` 模式。
- `submitSettings`(2224后)：回写 `hitl_followup_debate/chat` 到 `prompt_templates.debate`（与默认一致即删除，避免垃圾配置）。
- 验证：grep 5 个标记均存在；`node --check` JS 语法 OK。
- 「恢复默认」依赖后端 `get_settings` 注入的 `prompt_template_defaults.debate`（store.py:107，代码内置默认），正确还原。

## 六、改动未提交
- config 文案（2 文件：去「次数不限」）+ 前端 UI 入口（3 处）+ 之前回执/prompt_defaults 收敛 + 测试断言，均未提交，待用户拍板。
- 主服务 8777 未重启到最新（含 UI 改动 + config 改动）：用户刷新界面即可加载新 index.html；config 改动需重启 8777 才对主服务生效（探针已用隔离实例验证）。
