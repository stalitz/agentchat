# 真实会话分析：3岁男孩的追高（s_c38dbc61）

- 时间：2026-09-21 15:02–15:36（14 条消息，run 到「会话结束」）
- 位置：app 项目 `agent-probes`（team `tm_a0242d79`），agent = `Codex (本地 gemma)` / `Claude Code (本地 gemma)`，`think=true`
- 模型：本机 Ollama `gemma4:26b`（免费），codex 走 app-server、claude 走 8777 网关
- 结论：**讨论正常收敛**（Codex 提议被轮次 gate 拒 → Claude 提议 → Codex 确认 2/2 通过）

## 证据源

| 类型 | 路径 |
|---|---|
| 落盘消息 | `agent-probes/.agentchat/sessions.json`（sid `s_c38dbc61`） |
| 每轮完整 prompt | `agent-probes/.agentchat/prompts/s_c38dbc61.jsonl`（8 条） |
| 终止工具日志 | `agent-probes/.agentchat/tools/s_c38dbc61.jsonl` |
| **codex 原始输出** | `~/.codex/sessions/2026/09/21/rollout-2026-09-21T15-04-07-01a0c2c7-…jsonl` |

## 一、codex 连调（用户观察①，成立）

rollout `function_call` 逐轮统计（同一 rollout 含 5 个 codex turn）：

| codex turn | 对应 app 消息 | `ask_human_input` | `end_discussion` |
|---|---|---|---|
| 1（首轮，用 `hitl_debate`+`hitl_opening`） | msg[1] | **2**（同问身高体重父母身高，英文两措辞） | 0 |
| 2（回复轮，用 `hitl_followup_debate`） | msg[3] | **3**（同问父母身高，中文三措辞） | 0 |
| 3（正文输出） | msg[5] | 0 | 0 |
| 4（终止轮） | msg[9] | 0 | **0（未真调）** |
| 5（确认轮） | msg[11] | 0 | **0（未真调）** |

- 用户侧无感：3 次连调被**单待问语义**收敛成 1 次 `human_input_required`。
- **根因不变**：工具回执是同步的、不回灌答案；模型没等到答案就换措辞重发（`call_id` 互异，去重挡同 id 挡不住变体）。

### 关键纠正：我上轮「去『次数不限』→ 回复轮连调清零」不成立

- 本场 L1 prompt（15:20:35）**已含**新措辞「每次发言只调用一次…不要在**同一次发言里重复调用**」→ codex 该回复轮仍 **3 连调**。
- 回查 v12d（12:13 rollout `…01a0c22a-ec2a-…`）：ask_human_input 共 2 次，**全在 turn 1**，turn 2（回复轮）0 次 —— 不是措辞生效，而是**该轮没有要问的东西**。
- 对照 v12b（改前，10:32 rollout `…01a0c1ce-c929-…`）：turn 1 = 1 次、**turn 2（回复轮）= 7 次**。

⇒ 结论：**提示词措辞不能保证「一次发言只调一次」**；v12d 的「清零」是样本假象（回复轮恰好无追问）。

## 二、codex 终止轮「没调用工具、用文字」（用户观察②，成立但非 app bug）

**rollout 铁证**：turn 4 / turn 5 **零 `function_call`**，只有 `agent_message`（正文），末尾带

    ```json
    {"tool": "end_discussion", "reason": "…"}
    ```

**app 侧对应**：msg[9] / msg[11] 的 `tool_calls` 有 `end_discussion`，但 `tool_results` **为空**、`blocks` **只有 `[{type:text}]`** —— 说明这条 tool_call **不是** codex 的真工具调用，而是 app 兜底解析器 `_extract_text_tool_call`（`orchestrator.py:104`）从正文 ```json 文本里解析出来的。

**功能未受损**：兜底解析让终止状态机照常工作（tools 日志：msg_round 9 `rejected`(floor=3) → msg_round 10 Claude `proposed` → msg_round 11 Codex `confirmed`）。

**为何不是「工具缺失」**：
- 同一 app 的 v12b（10:32 rollout）turn 5 codex **真调过** `end_discussion`（rollout 里 1 条 `function_call`）→ 工具确实在 codex 工具列表里。
- MCP server（`terminate_signal_mcp.py:188-199`）`tools/list` 同时暴露 `end_discussion` + `ask_human_input`；codex 侧以 `-c mcp_servers.terminate-signal.*` 注入（`executor.py:971-976`），无工具白名单过滤。

⇒ 判定：**gemma4:26b 的格式漂移**（该轮选择把调用写成正文 ```json，而非发 function_call）。且**非 claude 专属**：claude 在 v12c（msg[9]）也有「只有文本、无真调用」的先例；v12b（msg[12]）则是 claude「真调用 + 正文文本」双表达。

**唯一实际副作用**：正文里那段 ```json 会原样渲染在气泡里（用户看到「像在用文字调用」），且无从区分是真调用还是兜底解析。

## 三、附带摩擦

- codex 首轮提问用了**英文**（L0 prompt 无语言约束、agent 描述也未要求中文）→ 用户被迫追加「请全程使用中文, 包括向我提问」。

## 四、`【本场必须先问后答】`（用户观察③）

**机制**（两处代码）：

- 触发判据：`orchestrator.py:76-87` `_HITL_TASK_HINTS` 关键词表（含「多套/给我方案/帮我选/推荐哪个/适合我/options…」）× `_task_needs_human_choice()`
- 注入点：`orchestrator.py:458`（chat）/ `566`（debate，`round_i==0` 且本场未问过）
- 文案：`prompt_defaults.py:164` `hitl_opening`，措辞是**强制**（「必须先调用 ask_human_input」）
- 消解：真正问过一次后 `_hitl_asked=True`（`orchestrator.py:349`），后续不再注入

**本场命中原因**：任务句「如果有多套方案也可以征求我的意见」含「多套」→ 命中。但该句是**条件性许可**（「如果…也可以」），被判成「明显需要用户拍板」。

**设计动因**（代码注释 `orchestrator.py:73-75`，2026-09-19 实测）：议题写「请给多套方案让我选」时，两个 gemma 都直接摆方案、不问用户 → 加了强指令；词表**刻意放宽**，取舍是「漏判代价 > 误判代价」。

**问题（用户口径成立）**：
1. 是否该问是**语义判断**，现在交给关键词表 —— 任务条件完整时（如「给我方案：Java 17 迁移步骤」）会被迫先问一个无意义问题，白耗一轮。
2. 它**先于内容**强制，agent 连「我已有足够信息」都无法表达。
3. 与通用规则重复：`【需要用户拍板时…】` 已给出三条判据（含「关键信息缺失且无法推定」），本条把它降级成了硬触发。

**可配置性（实测）**：`_render_slot` 为 `self._pt.get(slot) or PROMPT_TEMPLATES_DEBATE[slot]`（`orchestrator.py:367`）——
- 改**文案**：config `settings.prompt_templates.debate.hitl_opening` 覆盖即可，**无需改代码**；
- 设成**空串无法关闭**（`or` 会回退内置默认）；要真正关掉/改判据，需改代码。
