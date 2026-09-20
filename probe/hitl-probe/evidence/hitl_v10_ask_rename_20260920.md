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
- **未跑到 `run_end`**：claude 后段生成慢，7 分钟探针窗口耗尽（非链路错误，见「诚实边界」）。

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

## 诚实边界

1. **未跑到 `run_end`**：探针 7 分钟窗口耗尽（claude 第 8 条后仍在生成），属耗时问题，
   不是链路问题——同场 HITL 三问三答与双方轮流发言均已完成。
2. **本场旧解析只多切 1 条**（5 vs 4），delta 远小于证据场 `s_a3d8022f`（旧 8 vs 真实 2）。
   即「旧代码在本场只会多 1 个幻影气泡」——真正证明修复幅度的是跨 25 会话的批量对照
   （改前 25/25 全都多切；改后 23/25 数量一致）。
3. **对账会覆盖实时思考**：codex 第 1 轮思考由实时 6668 字变为 transcript 的 4284 字
   （「冲突以 agent 为准」的既有语义，非本次改动引入）。
4. 第 3 序 codex 同轮重试 3 次工具、措辞略异，最终**只落最后一个问题**——这是
   「一次发言单待问」的预期行为，不是漏问。
