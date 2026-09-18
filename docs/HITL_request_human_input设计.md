# HITL：request_human_input（agent 提问 → 暂停等人）

> 配套需求：在辩论/对话中，当某个 agent 需要用户从多个方案中选择、或需澄清任务时，
> 它调用 `request_human_input` 工具，编排器**暂停整场**、把问题推到前端、等用户回答后
> 作为下一条 delta 注入并续跑。解决「agent 反问被另一个 agent 接走、结果出人意料」的问题。

## 1. 背景与问题

- 薄编排器只做「路由 delta + 检测终止」，不知道 agent 在「问人」还是「对对方发言」。
- debate 模式每轮只把**对手上一句 + 本轮任务**作为 user delta 续接（`orchestrator._build_prompt`），
  所以 agent A 的「方案 A 还是 B？」文本 → 变成 agent B 的「对方发言」→ B 自己选了答案，人不在环内。
- claude 的 `AskUserQuestion` 工具被网关剥掉（只透 `end_discussion`，`gateway.anthropic_to_openai`），
  claude 只能以纯文本反问；codex 的 `requestUserInput` 被 `_as_autoanswer` 自动空答。两条路都没把问题递到人。

## 2. 设计目标

- agent 用**结构化工具调用**显式表达「需要人工介入」——与 `end_discussion` 同构（都是给薄编排器的硬信号）。
- 编排器收到信号后**暂停整场**（debate 两 agent 都停；chat 单 agent 本轮停），问题推前端，等用户答。
- 用户回答作为下一条 delta 注入：debate 下由「发起提问的 agent」续跑（不增轮次）；chat 下单 agent 续跑。
- 复用现成通道：`inject_queue`（人工介入）、`paused/resume_event`（轮边界暂停），不另造暂停机制。

## 3. 工具契约

共用现有 `terminate-signal` MCP server（`backend/terminate_signal_mcp.py`，claude 经项目级
`.mcp.json`、codex 经 `-c mcp_servers.terminate-signal.*` 注册，已自动暴露该 server 的所有工具）。
新增工具：

```
request_human_input(question: str, options: list[str] = [])
```

- `question`：要问用户的问题（必填）。
- `options`：可选选项列表；前端渲染成可点选按钮，点选后填入输入框（用户仍可改/自定义）。
- 回执：MCP server 一律返回「已收到，等待人工介入」中性成功文本（**不做判定**，判定在编排层）。
  与 `end_discussion` 同源：本 server 无状态、多会话并发共享状态会串场，故只提供工具调用通道。

## 4. 两条引擎接入

### 4.1 claude（stream-json）
- 网关 `anthropic_to_openai` 的工具过滤：从「只留含 `end_discussion` 的」扩展为「只留
  `end_discussion` 或 `request_human_input` 的」（按去前缀后的基名判定），其余内置工具仍不转发。
- `_restore_mcp_name`：基名为 `request_human_input` 时还原为 `mcp__terminate-signal__request_human_input`
  （与 end_discussion 同 server，故前缀同为 `terminate-signal`）。
- claude 调该 MCP 工具 → 产出 `tool_use` 块 → executor 透传为 `("tool_use", {name, input})`
  → 编排层按名识别（无需回 tool_result：用户回答作为下一轮新 user 消息续接）。

### 4.2 codex（app-server 主路径 + exec 兜底）
- 复用 `terminate-signal` server（`-c` 注册已含 `default_tools_approval_mode="approve"`），新工具自动可见。
- `_as_read_loop`（`item/tool/call` 捕获段，现仅捕 `end_discussion`）：新增捕获
  `request_human_input` → 将 `{"_tool_use": {name, input}}` 推入 `_as_queue`。
- 该 `item/tool/call` 仍走现有 `_as_autoanswer` 的 `item/tool/call` 空成功应答 → codex 本轮正常结束；
  编排层消费队列里的 `_tool_use` 后暂停。exec 兜底路径同理（MCP 工具被批准执行，回合末落 `tool_use`）。

## 5. 编排暂停/续跑

### 5.1 检测（orchestrator._run_turn）
- 与 `end_discussion` 检测并列：新增 `_is_human_input(name)`（`request_human_input` 在基名或带 mcp 前缀）。
- 流结束后扫 `tool_uses`，若存在 `request_human_input`，设 `self._human_input_request = {agent, question, options}`。

### 5.2 暂停（_debate / _chat_turn）
- `_debate`：在 `_run_turn` 之后、`round_i += 1` 之前插入判断：
  - 若 `_human_input_request` 非空 → 调 `_handle_human_input_pause(agent)`：
    - 广播 `human_input_required`（agent 名 + question + options）给前端；
    - `await self.human_input_event.wait()`（复用 Session 暴露的 `provide_human_input` 置位）；
    - 记录用户回答为 `human` 消息（role「人工回答」）；
    - **不增 `round_i`**，`pending_inject = 回答`，`continue` → 同 agent 续跑（回答作为下一条 delta）。
- `_chat_turn`：同上，暂停后重跑 `self.agents[0]`（inject=回答）。
- 中止/停止的既有语义不受影响（暂停在轮边界生效，与 `paused` 同层）。

### 5.3 续跑投递
- 用户回答经 `inject_queue` 同款语义落盘（role「人工回答」），并作为下一轮 `_build_prompt` 的
  `inject` 拼接（`人工介入 / 任务：<回答>`），对发起提问的 agent 可见。

## 6. 前端交互

- 新增「需要你介入」面板：监听 `human_input_required` 广播，展示
  - 提问 agent 名、question 文本；
  - options 以按钮渲染，点击填入输入框（可编辑/留空自定义）；
  - 文本输入 + 提交。
- 提交 → WebSocket `{action:"human_input", session_id, text}` → 后端 `Session.provide_human_input(text)`
  → 置 `human_input_event`、存回答。复用现有 人工介入 的广播/渲染风格。

## 7. prompt 提示（让 agent 在「该问」时主动调工具）

- debate 系统提示 + gateway 系统提示追加一句（中文）：
  「当你需要用户从多个方案中选择、或需澄清任务时，**调用 `request_human_input` 工具**而非在文本里
  反问——本场是多人辩论，对方 agent 会替你回答你的反问，导致结果偏离你的意图。」
- 同时说明 `options` 参数用法（列出候选项让用户点选）。

## 8. 测试点

- `test_gateway`：过滤同时保留 `end_discussion` 与 `request_human_input`；`_restore_mcp_name` 两者都还原。
- `test_orchestrator`：`_is_human_input` 识别带/不带前缀名；`_run_turn` 检测到工具调用后置
  `_human_input_request`；`_debate` 暂停后由同 agent 续跑且不增轮次。
- `test_codex_appserver` / `test_codex_parse`：`item/tool/call` 含 `request_human_input` 被捕获为 `_tool_use`。
- `test_frontend_render.js`（jsdom）：`human_input_required` 渲染出问题 + 选项按钮。
- `test_executor`：`.mcp.json` 的 `terminate-signal` server 暴露新工具（脚本含该工具即可，注册代码不变）。

## 9. 风险与限制

- **exec 兜底路径的 HITL 真实性**：codex exec 非交互，MCP 工具被批准执行后即返回，回合末才落
  `tool_use`；编排层仍会暂停等人，但「暂停时机」在回合末而非调用点。可接受（弱模型少用）。
- **弱模型（gemma/r1）可能不调工具**：依赖 prompt 提示；若仍以文本反问，行为同现状（对方接走），
  不回归、不阻塞。
- **暂停期间前端状态**：与现有 `paused` 一致，暂停时不消耗轮次预算，续跑接着算。
- **多会话并发**：编排层状态机 per-session（Session 实例隔离），互不串场。

## 10. 2026-09-19 修订：触发条件改硬条件 + codex 上报不再被吞

### 10.1 prompt 由「软条件」改为「硬条件」（§7 替换）

旧措辞「当你**需要**用户从多个方案中选择时」实测无效——跳高训练计划那场两个 gemma 都理解成
「把方案摆出来即可」，改为在正文里反问；而辩论里正文反问会被**对方 agent** 接过去回答，用户
根本看不到（界面上像 agent 在等回复，实际是自说自话：双壳探针 `s_a3d8022f` 中 codex 在一次
prompt 内自驱 6 个内部 turn，连问三次「你选哪个」，无人回应）。

现改为三层，落在 `prompt_defaults.py` 的三个可配置槽位（可被
`settings.prompt_templates.debate.<slot>` 覆盖）：

| 槽位 | 作用 |
|---|---|
| `hitl_chat` | 人机对话版通用规则 |
| `hitl_debate` | 辩论版通用规则（多一条：正文反问会被对方 agent 接走） |
| `hitl_opening` | 「先问后答」强指令（带 `<task>` 占位符） |

通用规则要点：① 列举**硬触发条件**（结论取决于用户选择 / 关键信息缺失 / 议题含「多套方案让我选」
类措辞）；② 明令**禁止正文反问**并说明后果；③ 给出无工具时的多行 json 兜底写法；④ 说明调用后
系统会暂停、回答会交回，**不要**输出「等你回复」之类待命文字。

`hitl_opening` 的注入条件（`orchestrator`）：议题命中 `_HITL_TASK_HINTS`
（`_task_needs_human_choice`）、本场尚未问过（`_hitl_asked`）、且是首个发言者（debate `round_i==0` /
chat 首轮）⇒ 要求先调工具问清决定性选择（给 2-4 个选项），拿到回答再给方案。

### 10.2 codex 上报断链修复（§8 测试点补充）

两条 codex 上报通路原先都只放行 `end_discussion`：

- app-server 实时 `item/completed`（executor `_run_codex_appserver`）
- `codex exec --json` / rollout 的 `item.completed`（`_parse_codex_line`）

写法均为 `... and "end_discussion" in str(tool)`，`request_human_input` 掉进 else 被
`_codex_native_tool_use` 判 `None` 吞掉 ⇒ codex 即便真调了，编排层也收不到、不会暂停。
现统一走 `_guarded_mcp_tool(item)`（与 `_is_mcp_tool_item` 同处），认干净名与 `mcp__…__` 前缀名，
产出干净名给编排层；非受控 MCP 调用仍不产事件。

**与网关无关**：两个工具同属 `terminate-signal` MCP server，codex 直连 MCP，不走网关；
end_discussion 走得通 ⇒ request_human_input 只需放开上报过滤。

### 10.3 遗留（未做，等定）

- codex + Ollama provider 下模型侧曾自述 `request_user_input is unavailable in Default mode`，
  需再跑一次端到端确认修完是否真能调通。
- codex 内建通道 `item/tool/requestUserInput` 目前被 `_as_autoanswer` 静默空应答
  （`{"answers": {}}`）；该通道模型侧总是可见，可考虑接进 HITL，但涉及 app-server 应答时序，未改。
