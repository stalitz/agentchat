# `<tool_code>` 原生可见性核查（2026-09-19）

## 问题
`tool_plan`（模型写的 `<tool_code> // … </tool_code>` 规划块）在**原生 Claude Code 界面**可见吗？测试的是哪个会话？用户想在 claude 界面看实际效果。

## 方法
1. 全量 grep `~/.claude/projects/**/*.jsonl`，找含 `<tool_code>` 的 transcript。
2. 解析命中 transcript 的结构（块类型 / role）。
3. 在 claude-code 二进制（`~/.local/share/claude/versions/2.1.212`，Mach-O 244MB）里搜
   `tool_code` / `<tool_code>` / `toolCode`，并用 `tool_use`/`tool_result`/`content_block_delta`
   做「二进制可搜性」对照。
4. 查 app 存储（`<project>/.agentchat/sessions.json`）是否含该串。

## 结果

### 1. 全库唯一命中 transcript
`~/.claude/projects/-Users-stalitz-www-aitest-agent-probes/49c97e36-a916-489b-a136-19c5e84a9e9e.jsonl`
（cwd `/Users/stalitz/www/aitest/agent-probes`，model `gemma4:26b`，28 行）

4 行含 `<tool_code>`，**全部内嵌在 `message.content[].text`（`block.type='text'`）**，不是独立块：

| 行 | role | tool_code 内文 |
|---|---|---|
| 7 | assistant | `// 暂无需要执行的工具调用，等待 Codex 的进一步讨论或用户对频率信息的补充。` |
| 12 | assistant | `// 无需工具调用，继续与 Codex 进行逻辑构建。` |
| 17 | user | `// 无需工具调用，继续完善训练计划的设计逻辑。` ← 用户引用的正是这条 |
| 21 | assistant | `// 本次讨论已完成，无需进一步调用工具。` |

### 2. claude-code 二进制无任何 `tool_code` 逻辑
| 搜索串 | 命中数 |
|---|---|
| `tool_use` | 654 |
| `tool_result` | 485 |
| `content_block_delta` | 32 |
| `stream-json` | 34 |
| **`tool_code`** | **0** |
| **`<tool_code>`** | **0** |
| **`toolCode`** | **0** |

⇒ claude-code **既不解析、也不渲染、更不「剥除」** `<tool_code>`；它只是**模型输出的普通文本**。
不存在「原生工具面板」这种渲染。

### 3. 它是 gemma 的行为，与 CLI 无关
app 存储 `s_2f8ead46`（12 消息）中 **4 条**含 `<tool_code>`，且 **Claude 与 Codex 两个 agent 都写了**
（Claude round 4/6/8、Codex round 7），一律存为 `text` 块内纯文本。
⇒ app 前端把 `text` 块当正文渲染，所以 **app 界面原样显示了 `<tool_code>…</tool_code>` 原文**。

## 更正此前结论（重要）
- ❌「原生 Claude Code 把 `<tool_code>` 渲染为工具面板」→ 无据，原生无任何 tool_code 逻辑。
- ❌「claude-code 在 stream-json 把 comment-only `<tool_code>` 从实时流剥除」→ 不成立；先前是**跨会话比较**得出的误推。
- ❌「app 存储 / 事件流 0 命中」→ 现状 app 存储有 4 条（应为之后 reconcile 回填或会话混淆），以现状为准。
- 推论：`backend/executor.py` 的 `t == "tool_code"` 分支是**死代码**（claude 从不发该类型消息）；
  `_classify_text` 里对 `<tool_code>` 的文本抽取才是有效路径。

## 会话与打开方式
- app 会话：`s_2f8ead46`（「probe：HITL 人工介入」），cwd `/Users/stalitz/www/aitest/agent-probes`
- Claude CLI 会话：`49c97e36-a916-489b-a136-19c5e84a9e9e`
- Codex 会话：`01a0b8d9-b27f-7882-9332-857d9c60ca9c`

```bash
cd /Users/stalitz/www/aitest/agent-probes
claude --resume 49c97e36-a916-489b-a136-19c5e84a9e9e
```

## 「是不是走了网关才变纯文本？」——续查（21:16）
用户假设：网关把 tool call 降级成了纯文本；原生 Ollama 也许有工具调用显示。静态核查：

1. **Ollama 的工具记号不是 `<tool_code>`**。`/Applications/Ollama.app/Contents/Resources/ollama`（0.34.2）里
   工具相关记号命中：`<tool_call>`=30、`<tool_response>`=5、`<tools>`=8、`<available_tools>`=2 …，
   **`tool_code` / `<tool_code>` = 0**。gemma4:26b 的 Modelfile 是 `TEMPLATE {{ .Prompt }}` +
   `RENDERER gemma4` + `PARSER gemma4`——Ollama 内建渲染/解析，但认的是 `<tool_call>` 一族，**不认 `<tool_code>`**。
2. **网关不注入、且会转发 tools**（`backend/gateway.py`：`anthropic_to_responses` 的 `tools` 透传，
   line 164–188/277–284；line 57「不再转发」的注释只针对旧的 `anthropic_to_openai`/OpenAI Chat 路径）。
3. **反证**：round 7 那条 `<tool_code>` 由 **Codex 写**，而 codex 是**直连 Ollama、不经网关**——同样落成纯文本。

⇒ **「网关把它变成纯文本」不成立**：`<tool_code>` 不是这条链上任何一环（ollama / claude / codex / 网关）
认可的工具格式，原生 Ollama 也不会把它解析成 tool call。
**但有你直觉的内核**：当模型**真的**发起工具调用时，Ollama 的 gemma4 parser 会把 `<tool_call>` 解析成结构化
`tool_calls`（或由客户端还原成原生 tool_use），那是**另一种**形态；你看到的 `<tool_code> // 注释` 是模型的
「无需工具」占位文本，无人识别 ⇒ 只能原样当文本。

### 待做的决定性实验（需用户同意——会调模型，本机免费 gemma）
不用网关、不用 claude/codex，直接打 Ollama，看原生行为：
```bash
# A. /api/chat 声明 tools + 诱导调用 → 看返回 message.tool_calls（结构化）还是 content 里的 <tool_code> 文本
curl -s http://localhost:11434/api/chat -d '{"model":"gemma4:26b","stream":false,
 "messages":[{"role":"user","content":"请调用 get_time 工具告诉我现在几点"}],
 "tools":[{"type":"function","function":{"name":"get_time","description":"获取当前时间","parameters":{"type":"object","properties":{}}}}]}'
# B. /v1/responses（claude/codex 实际走的端点）同题对比
```
预期：若原生工具有效 → `tool_calls` 结构化；若模型仍写 `<tool_code>` → 则确证其为模型自身习惯、与网关无关。

## 「是不是那次改动导致 claude 开始吐 `<tool_code>`？」——时序核查（22:1x）

用户观察：改前测 claude 几十次从未出现；「给 codex 改到能真正调用」后，claude 连续几个发言都输出它。

### 1. 作者归属（先纠正我自己的错）
`49c97e36` 逐条核对（`type`/`role`）——**4 处里 3 处是 Claude 自己生成的**（assistant 侧），
只有 1 处（行 17）是 **Codex 的输出被当作输入喂给 Claude**（user 侧）：

| 行 | 侧 | 作者 | 内文 |
|---|---|---|---|
| 7 | assistant | **Claude** | `// 暂无需要执行的工具调用，等待 Codex…` |
| 12 | assistant | **Claude** | `// 无需工具调用，继续与 Codex 进行逻辑构建。` |
| 17 | user（输入） | Codex | `// 无需工具调用，继续完善训练计划的设计逻辑。` |
| 21 | assistant | **Claude** | `// 本次讨论已完成，无需进一步调用工具。` |

⇒ 「是 codex 干的」**错**；但也不能据此说改动导致（见下）。**触发点具体**：行 5 调
`request_human_input` → 行 6 tool_result → 行 7 立刻吐 `<tool_code>`；行 19 调 `end_discussion`
→ 行 20 tool_result → 行 21 又吐一个。即**「真工具调用拿到结果后，紧跟一条 `<tool_code>` 占位」**。

### 2. 与那次改动无关——三条独立证据
1. **代码没碰 claude 路径**：`9efcf7a`（13:58「codex 不卡死」）的 executor 改动全在
   `item/tool/call` / `item/tool/requestUserInput` / 新增 `_hitl_tool_result` 这些 **codex 侧**分支；
   orchestrator 改的是 human_input 分支。claude（经网关）一行未动 ⇒ 结构上不可能改变其输出。
   （同批 `9776a85` 12:18 同样只在 codex 侧。）
2. **同码 5 分钟之差**：`a4c959ad`（09-19 **16:47**）与 `49c97e36`（**16:52**）跑同一份代码，
   16:47 拿到**逐字节相同**的 request_human_input 回执却 **0 命中**，16:52 有 3 处。
3. **回执相同、结果不同**：`request_human_input` 的 tool_result 文本在 6+ 个会话里**完全一致**
   （`18880a96` 12:38 / `5d22d6a8` 19:50 / `bee8165b`×3 / `de063076` / `a4c959ad`）→ 全部 0 命中；
   且改动**之后**的 19:41、19:50 两场同样 0。⇒ 不是「改动一开就一直出现」。

### 3. 量级：这是低概率采样事件
全库 gemma/claude 会话统计（遍历 `~/.claude/projects/**/*.jsonl`）：

| 指标 | 值 |
|---|---|
| assistant 文本块总数 | 384 |
| 紧跟在 tool_result 之后的 | 26 |
| 其中含 `<tool_code>` | **2**（均在 `49c97e36`）→ **7.7%** |
| 全库总出现次数 | **3** / 384 ≈ **0.8%** |
| 含该串的 gemma 会话 | **1** / 51 场 |

**为什么感觉「一改就连续出现」**：① 起始是低概率采样命中；② 一旦命中，模型在**同一上下文里看到
自己的输出** → 后续发言自我模仿（该场连续 3 次），故表现为「连续几个发言都是它」。
「连续」是自我强化，「起始」是随机。

### 4. 随机性的来源
全库**未设任何采样参数**：`backend/*.py` 无 `temperature`/`top_p`/`options`，`data/config.json` 与
agent-probes 的 agent 配置均无，`ollama show gemma4:26b --modelfile` 亦无 `PARAMETER` 行
⇒ 走 Ollama 默认 **temperature=0.8**，采样本身随机。

### 5. 模型为什么会写这个记号
app 自己的 prompt 里就有「**用代码块代替工具调用**」的兜底指示（多场 prompt 一致）：
> 仅当你的工具列表里确实没有该工具时，才输出以下格式的代码块（与调用等价）：
> ```json
> { "tool": "end_discussion", "reason": "..." }
> ```
gemma 把这条「用标记表达工具决策」泛化成了自造的 `<tool_code>` 标签。它不是任何真实工具格式
（Ollama=`<tool_call>`、claude 二进制 0 命中），因此无人识别，只能原样当文本。

## 待定
原生 TUI 的实际渲染形态（是否把未知 XML 标签当 HTML 吞掉，只留 `//` 注释）以用户打开为准；
随后再定 app 侧显示策略（保持 tool_plan 面板 / 按原生原样文本 / 隐藏）；原生 Ollama 实验待用户点头后跑。

