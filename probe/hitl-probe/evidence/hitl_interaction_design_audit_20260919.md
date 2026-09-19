# HITL 交互设计核查：多次提问 / 用户回复带提示词 / 跨 agent 传递（2026-09-19 23:0x）

用户提出四条质疑，逐条以代码 + 真实 transcript/rollout 核实。**本文件只做核查，未改代码。**

用户原话要点：
1. codex 好像能在一次发言里多次问用户、用户也能多次回复，但记录好像只支持一次；
2. 这属于**工具交互**，用户回复是否还有必要带上提示词；
3. 发言交给 claude 时，是否应让 codex 根据用户回复**总结出结果**，而不是把 codex 与用户的交互原样发给 claude
   （反之亦然：codex 也不需要知道 claude 与用户的交互过程）；
4. 认为正是这个交互过程让 claude 以为需要一个 `<tool_code>` 来说明，而且该记号在交互时被原样喂回，
   强化了这个「记忆」，导致后面一直输出。

---

## 一、「单轮多次问人只支持一次」——**成立**（代码级）

两条 HITL 通路都只保留**一个**提问：

| 通路 | 代码位置 | 行为 |
|---|---|---|
| MCP 工具（claude，`mcp__terminate-signal__request_human_input`） | `orchestrator.py:719-729` | `if not self._human_input_request:` + `for tu in reversed(tool_uses)` + `break`——**同一轮多个调用只取最后一个**（注释亦写明「取最后一个调用（避免多调用时取错）」） |
| codex 原生（`item/tool/requestUserInput`） | `orchestrator.py:676-680` | `questions = req.get("questions") or []` → **`q0 = questions[0]`**，只问第一个 |

**跨轮次**多次提问是支持的：
- chat 模式：`while self._human_input_request:`（`:934`）循环暂停；
- debate 模式：`if self._human_input_request:` 后 `continue`（`:993-1000`，`round_i` 不变）→ 外层 while 再判，
  同一 agent 可再次触发暂停。

**实测**：`bee8165b`（chat 模式，app 会话 `s_54eb6e23`）transcript 里 claude **3 次**调用
`request_human_input`（问训练频率 / 水平阶段 / 专项方向），app 侧记录到 **2 条「人工回答」**
（第 3 次提问的答案未产生，探针已结束）⇒ 跨轮多次可用，「只支持一次」不成立于跨轮，**成立于单轮**。

## 二、「用户回复带上提示词」——**成立**（实证）

`49c97e36` 的 claude transcript 行 `[11]`（= 用户回答经 app 组装后的**完整输入**）：

```
你是：探针用 claude：经本 app 内置网关接本机 gemma，作为对照。。

人工介入 / 任务：
每周 3-4 次，每次 60-90 分钟。我更倾向早上训练。

【轮次门槛提醒】当前是第 1 轮发言…（略）
请基于以上继续推进。
【共识终止：先给出总结发言，再发起 end_discussion 调用】
… 仅当你的工具列表里确实没有该工具时，才输出以下格式的代码块（与调用等价）：
```json
{ "tool": "end_discussion", "reason": "…" }
```
【需要用户拍板时，必须调用 request_human_input 工具，不要在正文里反问】
… 仅当工具列表里确实没有它时，才输出下面这种多行 json 代码块：
```json
{ "tool": "request_human_input", "question": "…" }
```
```

⇒ **一句「每周 3-4 次」被 ~2000 字提示词包裹**，其中含**两段「用代码块代替工具调用」的兜底格式说明**。
组装点：`_build_prompt(agent, inject=answer)`（debate 模式 `:411+`；chat 模式 `:425-437`）——
HITL 回答走与常规轮次**完全相同**的 prompt 拼装，没有为「工具交互回复」做轻量通道。

## 三、「把 codex 与用户的交互发给 claude」——**不成立**（但存在反向缺陷）

`_others_since_last`（`:369-374`）：
```python
m for m in self.history[my_idx + 1:]
if m.get("speaker") not in (agent.name, "system", "human")   # ← human 被显式排除
```
⇒ 人工问答（`speaker="human"`）**不会**进「其他 agent 的发言」delta。
`_record_human`（`:548-552`）虽把回答落盘为一条 human 消息，但只用于落库/展示/任务背景定位。

**但存在真实的反向缺陷——信息不对称**：用户回答只发给**提问方**（`pending_inject = self._human_input_answer`
给 `current`，`:997`）。非提问方**永远看不到用户的原话**，只能从提问方的转述里揣测。
实证：`49c97e36` 行 `[7]` claude 写「`// 暂无需要执行的工具调用，等待 Codex 的进一步讨论或
用户对频率信息的补充。`」——它其实在**猜用户是否已回答**。

## 四、「交互过程导致 `<tool_code>` 并被原样喂回、强化」——**成立**（rollout 铁证）

`49c97e36`（claude）+ `rollout-2026-09-19T16-47-49-…01a0b8d9…jsonl`（codex，同一 app 会话 `s_2f8ead46`）交叉比对：

| 时序 | 侧 | 事件 |
|---|---|---|
| claude `[5]`→`[6]`→`[7]` | claude | 调 `request_human_input` → 拿到回执 → **写出第 1 条 `<tool_code>`**（`// 暂无需要执行的工具调用，等待 Codex…`） |
| claude `[11]`→`[12]` | claude | 收到**用户回答 + 整段提示词** → **写出第 2 条 `<tool_code>`**（`// 无需工具调用，继续与 Codex…`） |
| codex `[54]` | codex **输入** | 已含 claude 上面两条 `<tool_code>`（作为「其他 agent 的发言」传入） |
| codex `[56]/[57]` | codex **输出** | **照抄同格式**：`// 无需工具调用，继续完善训练计划的设计逻辑。` |
| claude `[17]` | claude **输入** | 收到 codex 那条含 `<tool_code>` 的发言 |
| claude `[21]` | claude 输出 | 再写第 4 条（`// 本次讨论已完成…`） |

⇒ **成因链修正**：不只是「单 agent 自我模仿」，而是**跨 agent 原样转述形成的传染回路**——
畸形记号被写进发言 → 对方的发言 delta 原样带上 → 对方照抄 → 又传回 → 逐轮强化。
**触发土壤**是 HITL 工具交互：两次起始都紧邻工具回执 / 「用户回答 + 工具指令块」，
模型被反复置于「表达工具决策」的语境，而 app 提示词恰恰教它「**用代码块代替工具调用**」。

**Codex 该场并未调用任何问人工具**（rollout 里 `request_user_input`/`requestUserInput`/`request_human_input` 均 0 命中）
⇒ 用户看到的「codex 与用户交互」实际是 **codex 在正文里提问**（其工具侧自述不可用，回落成正文提问），
正文提问会被对方 agent 接走回答、用户看不到。

---

## 结论：四条质疑的判定

| # | 用户判断 | 判定 | 依据 |
|---|---|---|---|
| 1 | 单轮多次问人只支持一次 | **成立** | `:719-729` 取最后一个；`:676-680` 取 `questions[0]`（跨轮次支持） |
| 2 | 用户回复还带整段提示词 | **成立** | transcript `[11]` 实证；`_build_prompt(inject=answer)` 无轻量通道 |
| 3 | 把 codex 与用户的交互发给 claude | **不成立**（human 已被排除），但**非提问方看不到回答**是真缺陷 | `:369-374`；`49c97e36 [7]` 的「等待用户补充」即揣测证据 |
| 4 | 交互过程导致 `<tool_code>` 并被原样喂回强化 | **成立** | 上述交叉时序表（claude 先造 → codex 照抄 → 传回强化） |

## 可选改法（**未实施，待用户定**）
- **A. HITL 回答走轻量 delta**：只发「用户回答：<text>」+ 一行极简提醒，去掉轮次门槛/终止/HITL 长指令；
- **B. 单轮多次提问**：MCP 通路改 `tool_uses` 全收集（队列化多问）、codex 原生改遍历 `questions` 全部；
- **C. 消除不对称**：让提问方在继续发言时**输出结构化结论**（如「用户回答要点：…」），
  再把该结论（而非原话）注入对方 delta；或干脆把「用户回答」也作为一条对全员可见的结构化消息；
- **D. 削减传播**：对「其他 agent 的发言」做畸形标记清洗（或至少不让 `<tool_code>` 这类未识别 XML 进入 delta）。
