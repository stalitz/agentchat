# v10 端到端留痕 · 客厅软装改造（2026-09-20 下午）

> 跑在 **HITL 工具改名 + reconcile 幻影气泡修复之后**的代码上（`dbcc64f` / `7046262`）。
> 目的：验证 ① 改名 `ask_human_input` 后 codex 是否真能调起工具；② 新会话经 reconcile
> 会不会再长出幻影气泡。

## 运行条件

- 隔离实例 `127.0.0.1:8790`，配置 `probe/hitl-probe/config.json`，数据落 `probe/hitl-probe/work`。
  用户侧 8777 已于 16:10 重启（同源码），本次探针**不碰 8777 的数据**。
- 会话 `s_13b91c5e`，team `tm_hitl`（双壳：H-Probe Codex + H-Probe Claude），`max_rounds=2`，会话级 think=on。
- 模型：本机 Ollama `gemma4:26b`（**免费**），未接远程付费模型。
- 议题：20 平米朝北客厅的软装改造，任务里显式要求先用 `ask_human_input` 问预算。
- 原始事件：`probe/hitl-probe/raw/v10_ask_rename.events.jsonl`（9590 条）；日志 `raw/v10_ask_rename.log`。
- **未跑到 `run_end`**：探针 420s 窗口到期后客户端先撤，服务端 run 又跑 34 分钟，最终因
  claude 轮 stdout 单行超流上限而中止（真因见下「停跑真因」，非链路错误）。

## 事件主干

| 序 | 事件 | 说明 |
|---|---|---|
| 1 | `human(任务)` → `turn_start` Codex → `tool_use ask_human_input` → `turn_end(interim=False)` | 问预算上限，**工具调用成功** |
| 2 | `human_input_required` → `human(人工回答)` | 答「预算 1.5 万以内，尽量别动硬装」 |
| 3 | `turn_start` Codex → `tool_use ask_human_input` ×3（同轮重试）→ `turn_end(interim=True)` | 追问风格；**只产生 1 次暂停**（取最后一个待问） |
| 4 | `human_input_required` → `human(人工回答)` | 答「有猫、朝北采光差、要显亮」 |
| 5 | `turn_start/end` Codex | 归纳需求，结论交给对方 |
| 6 | `turn_start` Claude → `tool_use mcp__terminate-signal__ask_human_input` → `turn_end` | claude 走网关通道提问 |
| 7 | `human_input_required` → `human(人工回答)` → claude 出方案 → codex 收尾 | — |

工具事件 **5** 条（codex 4 + claude 1），`human_input_required` **3** 次 ⇒ 3 问 3 暂停，无重复暂停、无排队遗留。

## 验证结果

### ① 改名生效（codex 不再撞门禁）

- 新会话 5 条 prompt，**全部只含新名 `ask_human_input`**（旧名 0 处）：codex 3 条（6/1/1 处）、claude 2 条（6/1 处）。
- codex 的 4 次工具调用**全部成功产成 `tool_use` 事件**（`name=ask_human_input`），
  全程**没有** `unavailable in Default mode` 之类门禁报错。
- claude 侧回程名还原为 `mcp__terminate-signal__ask_human_input`，照常触发 HITL。

### ② 无幻影气泡（reconcile 修复）

| 项 | 值 |
|---|---|
| 本地消息（实时落盘） | 10 条 = human 4 / Codex 4 / Claude 2 |
| codex 新解析 `cli_turns` | **4** == 本地 4 ✅（旧口径会切 5；`event_msg/user_message` = 4） |
| claude `cli_turns` | **2** == 本地 2 ✅ |
| 跑 reconcile 后 | **10 → 10 条**，`appended=0`（codex `changed=9` / claude `changed=3`，只做内容覆盖） |
| 二次 reconcile（幂等） | `changed=0, appended=0`，条数不变 ✅ |
| B 兜底（伪造 codex 6 回合 > 本地 4） | `appended=0`，codex 仍 4 条 ✅ 追加被拦 |

气泡↔发言一一对应（codex 4 次发言 = 4 条消息，思考长度分别非零），**未出现多气泡**。

## 停跑真因（2026-09-20 16:53 复盘，更正上文的粗略说法）

会话最后两笔写入在 **16:53:42**，是服务端（非探针）写的：

```
[10] system  H-Probe Claude 执行出错，会话中止：执行异常: Separator is not found, and chunk exceed the limit
[11] system  会话结束
```

时间线（prompt 日志 7 条 + 会话消息）：

| 时刻 | 事件 |
|---|---|
| 16:12:07 | 探针 start；prompt[0] codex（slot 0） |
| 16:13:20 / 16:14:04 | prompt[1]/[2] codex 追问轮（HITL 回复轮，**不占轮次**） |
| 16:14:55 / 16:17:24 | prompt[3]/[4] claude（slot 1 及其回复轮） |
| 16:18:05 | prompt[5] codex（slot 2），16:19:29 落盘 |
| 16:19:07 | **探针 420s 窗口到期，客户端关 socket**（服务端 run 不受影响，继续跑） |
| 16:19:29 | prompt[6] claude（**slot 3 = 最后一个允许的发言位**） |
| 16:53:42 | claude 轮报流读取错 → 会话中止 → 会话结束 |

- **不是轮次上限**：`max_rounds=2` 语义 = 每 agent 发言 2 次（循环上限 `max_rounds × agent 数` = 4 个发言位），
  本场 slot 0/1/2 完成、slot 3 在跑（即便成功，跑完也到上限收尾）。全场从未有过「3 轮」的配置。
- **不是探针掐断**：探针只发 `start` / `human_input`，没有发 `stop`/`abort`/`pause`
  （这些是显式 action，见 `main.py:300-345`）。窗口到期只影响「谁在收听」，服务端 run 又跑了 34 分钟。
- **真因**：读 **claude CLI stdout** 的单行超过 asyncio StreamReader 上限（默认 64KB）→
  `LimitOverrunError`（`backend/executor.py:1372` 的 `proc.stdout.readline()`）。claude 这轮
  跑了约 34 分钟、transcript 涨到 437KB 才触发。**待修，未授权不动。**

## 另一处观察：「一次发言调 3 次工具」的成因

第 3 序那轮（claude 前）codex **同一轮里发起了 3 次 `ask_human_input`**，落盘为 3 条 tool_call
（其中第 1、3 条问题措辞不同，第 2、3 条逐字相同）：

- **模型自驱追问**：该通道工具只回「已收到」回执、**不把用户答案回灌**，模型拿不到答案，
  便按提示词「还有必须确认的信息可以继续追问」继续追问；事件里第 3 次调用出现在
  **93 段推理之后**（新的模型步），模型自己的推理还写着「The tool call is done. The turn is over.」。
- **代码不对称（值得修）**：codex app-server 的受控 MCP 分支**不按 `call_id` 去重**
  （`executor.py:881-885`），而原生工具分支有去重（`:896-903`）。重复上报会多记一次调用。
- **用户侧只看到 1 问**：编排层「单待问·取最后」把同轮多次调用收敛为 **1 次暂停、1 个问题**，
  这正是「无队列」的预期行为。

## 诚实边界

1. **上轮把停跑归因为「探针 7 分钟窗口耗尽」只对了一半**：窗口到期只停了客户端收听；
   服务端 run 又跑了 34 分钟，最终因上面的流读取错误中止。**我 16:22 执行的那次 kill
   也没有立即生效**（会话日志证明该进程活到 16:53:42），当时回「已关闭」不准确；现在 8790 已无监听。
2. **本场旧解析只多切 1 条**（5 vs 4），delta 远小于证据场 `s_a3d8022f`（旧 8 vs 真实 2）。
   即「旧代码在本场只会多 1 个幻影气泡」——真正证明修复幅度的是跨 25 会话的批量对照
   （改前 25/25 全都多切；改后 23/25 数量一致）。
3. **对账会覆盖实时思考**：codex 第 1 轮思考由实时 6668 字变为 transcript 的 4284 字
   （「冲突以 agent 为准」的既有语义，非本次改动引入）。
4. 第 3 序 codex 同轮重试 3 次工具、措辞略异，最终**只落最后一个问题**——这是
   「一次发言单待问」的预期行为，不是漏问。
