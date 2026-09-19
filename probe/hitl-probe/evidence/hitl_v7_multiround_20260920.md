# HITL 多轮问答语义修正 · 端到端留痕 v7（2026-09-20）

验证 2026-09-19 晚的改动（commit `60ad54a`）：① 同轮多次提问不限次数；② 回复轮轻量提示词；
③ 交互过程不转述给对方；④ `<tool_code>` 不清洗。

**新议题（与历史「跳高训练计划」区分）**：20 平米朝北客厅的软装改造方案。

## 环境与命令

| 项 | 值 |
|---|---|
| 实例 | **隔离实例 8790**（`AGENTCHAT_CONFIG=probe/hitl-probe/config.json`，跑新代码） |
| 未动 8777 | 8777 上 PID 5028 仍是旧代码，按纪律未 kill |
| 模型 | 本机 Ollama gemma4:26b（**免费**），claude-code + codex 双壳 debate |
| 项目/team | `pr_hitl` / `tm_hitl`（H-Probe Codex + H-Probe Claude） |
| app 会话 | `s_9df8d0e4` |
| codex rollout | `~/.codex/sessions/2026/09/19/rollout-2026-09-19T23-55-01-01a0ba60-*.jsonl` |
| 原始事件 | `raw/hitl_v7_8790.events.jsonl`（11549 条）、`raw/hitl_v7_8790.log` |
| prompt 留痕 | `probe/hitl-probe/work/.agentchat/prompts/s_9df8d0e4.jsonl`（5 条） |

```bash
AGENTCHAT_CONFIG=$PWD/probe/hitl-probe/config.json \
  .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8790 --ws-ping-timeout 300
AGENTCHAT_WS=ws://127.0.0.1:8790/ws AGENTCHAT_HTTP=http://127.0.0.1:8790 \
  PROBE_PROJECT=pr_hitl PROBE_TEAM=tm_hitl PROBE_TAG=hitl_v7_8790 PROBE_ROUNDS=3 \
  PROBE_TOTAL_WAIT=480 .venv/bin/python probe/hitl-probe/drive_hitl.py
```

## 结果总览

| 验证项 | 结果 | 证据 |
|---|---|---|
| ① 同轮多次提问、不限次数 | ✅ **生效** | 4 次 `human_input_required`：Codex 一轮内 2 次、Claude 一轮内 2 次，队列逐个问完 |
| ② 回复轮轻量提示词 | ✅ **生效** | 回复轮 prompt 366 / 406 字（起问轮 2087 / 2218 字），**−82%**；「轮次门槛」「共识终止」「用代码块代替工具调用」全部消失 |
| ③ 用户回答不进对方 | ✅ **生效** | Claude 起问轮 prompt 中「1.5 万」=0 次、「猫」=0 次、「用户回复」=0 次 |
| ③b `interim` 过滤 | ⚠️ **本次未覆盖** | 未出现「回答后再次追问」的情形，故 `interim` 全程 False（0 条） |
| ④ `<tool_code>` | 未出现（0 次） | 事件流与 codex rollout 均 0；但本场未跑到 run_end，样本不完整 |
| ❗ 新发现 | **Codex 回复轮正文丢失** | rollout 有 512 字结论，app 只收到 4 字 `---`，Claude 收到空转述 |

## ① 多次提问：逐个问完（事件时序，raw 事件流）

```
turn_start  Codex
tool_use    Codex  request_human_input      ← 同轮第 1 个提问
tool_use    Codex  request_human_input      ← 同轮第 2 个提问（旧实现只取最后一个）
turn_end    Codex  interim=False
human_input_required  Codex  您的装修预算上限是多少？
human_input_required  Codex  您的装修预算上限是多少？   ← 两个都问了
turn_start  Codex …（拿到两条回答后续跑）
turn_start  Claude
tool_use    Claude mcp__terminate-signal__request_human_input (+tool_result)
tool_use    Claude mcp__terminate-signal__request_human_input (+tool_result)
human_input_required  Claude  …预算上限是多少呢？
human_input_required  Claude  …对客厅的装修风格有什么偏好吗？   ← 两个不同问题，都问了
```

旧实现：MCP 通路 `reversed(tool_uses) + break`（只取最后一个）、codex 只取 `questions[0]`。
现实现：`_human_input_queue` 全部入队 → `_pause_for_human_input()` 逐个暂停等人。
**两条回答都被汇总回给提问方**（见下方轻量 prompt 里的 `1) … 2) …`）。

## ② 回复轮 prompt：真实落盘对比

| # | round | agent | 长度 | 类型 | 含「轮次门槛/共识终止/用代码块」 |
|---|---|---|---|---|---|
| 0 | 0 | Codex | **2087** | 起问轮 | 是 |
| 1 | 0 | Codex | **366** | **回复轮** | **否** |
| 2 | 1 | Claude | **2218** | 起问轮 | 是 |
| 3 | 1 | Claude | **406** | **回复轮** | **否** |
| 4 | 2 | Codex | 3455 | 常规轮 | 是 |

回复轮 prompt 全文（`#1`，Codex）：

```
你是：探针用 codex：直连本机 Ollama 跑 gemma（免费）。。

用户回复：
1) 预算 1.5 万以内，尽量别动硬装。
2) 家里有一只猫，会抓沙发；朝北采光差，希望显得亮一点。

【用户已回答你的提问】
如果你还有需要用户拍板或澄清的问题，**继续调用 request_human_input 工具**问即可
（次数不限，系统会继续暂停并把回答交回给你），不要在正文里反问。
如果已经没有别的问题了，请**把刚才这几轮问答归纳成一个完整、可直接执行的结论或方案**，
并用它作为你本次的正式发言正文——**对方 agent 只能看到你的发言正文，看不到你和用户之间的
问答过程**，所以结论必须落在正文里，不要只说「已确认用户需求」这类空话……
```

## ③ 交接侧：Claude 起问轮 prompt 里有什么

```
'预算 1.5 万': 0 次      '猫': 0 次      '用户回复': 0 次
```
转述区：`其他 agent 的发言（自你上次发言之后）：H-Probe Codex（…）说：` ← **内容为空**（见下条 bug）。

## ❗ 新发现：Codex 回复轮正文丢失（真 bug，未修）

同一轮在两侧的记录不一致：

| 来源 | 该轮 Codex 正文 |
|---|---|
| codex rollout `[33]`（15:56:40.698Z） | **512 字**完整方案（预算 1.5 万 / 猫抓布 / 采光优化） |
| app 事件流 turn 1 | **4 字** `---\n`（仅 2 个 token 事件、1219 个 thinking 事件） |
| app 落盘 `sessions.json` round=4 | `content=''`，`blocks=[{text:"---\n"}, thinking…]` |

时间线对齐：`[28]` 回复轮 prompt 15:55:59 → `[33]` 512 字结论 15:56:40（41.6s 后）；
而 Claude 起问轮 prompt 正好生成于 **23:56:40**（= 15:56:40Z）⇒ **app 在结论落盘的同
一时刻就结束了 Codex 那一轮并切给 Claude**，结论没被读到，转述为空。

代码位置：codex app-server 通路正文走 `item/agentMessage/delta`（`executor.py:765-768`），
回合结束条件是 **`turn/completed` → `break`（`executor.py:840-842`）**。现象符合「第一个内部
turn（`---\n` + thinking）结束即 break，codex 自驱续跑的第二段正文未被读取」（与既有结论
「codex 有自驱续跑」一致）。Claude 侧不受影响（收尾轮 965 字正常落盘）。

影响：直接阻塞需求③ —— **Codex 侧的结论永远传不到对方 agent**。

## 观察项（非 bug）

- 同一轮里 Codex 的两次提问文本完全相同（`您的装修预算上限是多少？` ×2），用户因此被问了两遍、
  给出两条不同回答。属模型行为（gemma 重复输出），队列机制如实逐个问完。
- 本场未到 `run_end`：Codex 第 3 轮陷入退化循环（重复 `much (much)`），探针 480s 超时截断。
  gemma 长对话退化为既有现象，非本次改动引入。

## 待办

1. 修 Codex 回复轮正文丢失（`turn/completed` 后不立即 break / 或从 rollout 兜底补正文）——**未动代码，等用户定**。
2. `interim` 分支需一场「回答后再次追问」的会话才能覆盖。
