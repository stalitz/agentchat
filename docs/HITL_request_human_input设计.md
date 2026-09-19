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

## 11. 2026-09-19 修订：不限次数提问 + 回复轮轻量提示词 + 交互过程不转述

### 11.1 触发（用户四条口径）

| # | 用户判断 | 核实结论 |
|---|---|---|
| ① | 同一轮 agent 应支持多次提问、用户多次回复，不限次数 | **成立**（旧实现同一次发言只取最后一个 `request_human_input` / codex 只取 `questions[0]`，其余静默丢弃） |
| ② | 回复时不需要带每轮那种完整提示词 | **成立**（用户回答走 `_build_prompt` 整轮注入，一句回答被 ~2000 字包住，含两段「用代码块代替工具调用」兜底说明） |
| ③ | 交给对方只需最后一次结论，不带与用户的来回交互 | **部分成立**（human 消息本就被 `_others_since_last` 排除；但 agent 在 HITL 中的**中间发言**仍会原样转述） |
| ④ | 是 HITL 交互让 claude 以为需要 `<tool_code>`，且原样喂回强化 | **修订后成立**（原「纯随机 + 单 agent 自我模仿」的结论不完整：codex rollout 逐行可见 claude 的记号被当作「对方发言」喂给 codex、codex 照抄、又被喂回 claude 的**跨 agent 传染回路**） |

### 11.2 改动

1. **一问一答来回（不限次数）——没有队列**——`Session._human_input_request` 就是那**一个**待问问题；
   `_collect_human_input_questions()` 一次发言只记一个（多次调用取最后一个、覆盖）；
   `_pause_for_human_input()` 问掉这一个、答完即清空并返回**这一条回答**；调用方拿到后立刻让同一 agent
   **再说一轮（新发言）**，由它决定再问还是收尾 ⇒ 循环继续，次数仍不限。
   **语义（用户口径 2026-09-20）**：一次发言最多提问一次，调了本次发言即结束；每次提问都是**独立发言**，
   不存在「同一次发言调 N 次」，因此**没有队列也不需要**。
   **修订沿革**：初版（2026-09-19）把同批提问连续问完、回答拼成「1) … 2) …」一次灌回，提示词表现为
   「用户一下发了两个回复」——来回结构丢失；随后又引入队列，仍会让上一轮遗留的问题在下一轮突然冒出来问，
   2026-09-20 一并去掉，`_join_answers()` 随之删除。
2. **回复轮轻量提示词**——`_run_turn(hitl_reply=True)` → `_build_hitl_reply_prompt()`，
   只含「身份 + `用户回复：<answer>` + `hitl_followup_chat/debate`」；
   引导语＝「还有问题继续调工具问（不限次数）；没问题了把这几轮问答归纳成完整结论／方案写进正式发言正文」，
   并说明「对方 agent 只能看到你的发言正文，看不到问答过程」。
3. **交互过程不转述（`interim`）**——回复轮中「又发起提问」的发言标 `interim`，
   `_others_since_last()` / `_format_conclusion()` 过滤；前端灰化 + 「交互过程（未转述给其他 agent）」角标。
4. **不清理 `<tool_code>`**——用户口径「不要找不到问题的原因就把问题先干掉」，保留以便暴露真问题。
   传染回路本身已由 ③ 部分缓解（这些条不再转述给对方），但**不做文本清洗**。

### 11.3 语义边界（有意为之）

- **起问那条发言不算 `interim`**：它含该 agent 对议题的正式观点（对话双方都需要），且只转述一次。
  被过滤的只是「回复用户之后又问了一次」的那些中间条——它们才是「来回交互过程」。
- **收尾那条发言不算 `interim`**：它承载归纳出的结论，必须转述给对方。
- 判据是「本轮是否为回复轮（`hitl_reply`）且**又**发起提问」，在落盘时计算（`_run_turn` 内），
  **不从 messages 事后推导**。

### 11.4 测试

- `test_hitl.py::TestHitlReplyPrompt`：轻量提示词不含轮次门槛/共识终止/完整 HITL 规则、含回答原文与续问引导；
  debate 版说明「对方只看得到发言正文」；同轮多提问全收（旧实现只取最后一个）；队列逐个问完返回全部回答；
  `interim` 不进对方 delta、不作结论来源。
- `test_hitl.py::TestChatMultiAskEndToEnd`：chat 连续两问两答 → 第三次给结论；
  回复轮 prompt 无整轮指令、起问轮仍是完整提示词；落盘 `interim` 标记正确（起问 false / 中间 true / 收尾 false）。
- `test_hitl.py::TestDebateMultiAskHandoff`：debate 端到端——轮到 B 时其 prompt 含 A 的起问观点与结论，
  **不含**交互过程发言、**不含**用户回答原文。
- `test_frontend_render.js`：历史 `interim` 打标 + 角标文案；实时 `turn_end.interim` 与历史渲染一致。
- `test_hitl.py::TestChatOneAskOneReplyLoop`（2026-09-19 晚新增）：同一次发言连问两次 → 逐个问、
  每条回复的 prompt 只含**自己那一条**回答（不含另一条、也不拼编号列表），验证来回结构。

## 12. 2026-09-20：codex 结论被吞（v7 留痕暴露，已修）

v7 端到端（隔离实例 8790，会话 `s_9df8d0e4`，议题「20㎡ 朝北客厅软装」）跑出四条 HITL 提问，
但落盘后 Codex 回复轮只有 `'---\n'`（4 字），而 rollout 里是 **512 字完整方案** ⇒ 交给 Claude 的
「其他 agent 的发言」为空，**结论永远传不过去**。

- **成因**：app-server 通路正文只认 `item/agentMessage/delta`；codex 常在回合末才整块落 rollout，
  `turn/completed → break` 一到就退出，整块 item（若到达）也没有对应分支接管 ⇒ 正文丢失。
  用户判断「你发给 claude 的话时把 codex 的方案给吞了，这是 bug」——**成立**。
- **修复**：见 开发文档「codex 正文兜底：结论不再被吞」小节（`executor._run_codex_appserver`：
  `item/completed` 的 `agentMessage` 分支 + `turn/completed` 前 rollout 正文对账）。
- **与 ④ 的关系**：这条丢失**不是** `<tool_code>` 清洗问题（从未做清洗），是实时流到落盘的缺口；
  修完才能真实验证 FR20.1 规则 3 在 codex 侧是否成立。
- 留痕：`probe/hitl-probe/evidence/hitl_v7_multiround_20260920.md`。
