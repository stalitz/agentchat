# v11 端到端留痕：两处缺陷修复后 · 双壳讨论 3 轮（2026-09-20）

> 本次要回答三件事：① 缺陷修复是否真生效；② 两个 agent 讨论 3 轮能否**完整跑完**；
> ③ 修复后幻影气泡 / 重复调用是否还出现。
> 会话 `s_60b17f4e`（隔离实例 8790），23:46:01 → 23:57:15，**11 分 14 秒**。

## 运行条件

| 项 | 值 |
|---|---|
| 实例 | 8790（`AGENTCHAT_CONFIG=probe/hitl-probe/config.json`），不碰 8777 |
| 模式 | debate 双壳 · H-Probe Codex + H-Probe Claude · 全本机免费 gemma4:26b |
| 轮次 | `PROBE_ROUNDS=3` → 系统广播「会话开始 · agent 数=2 · **上限 3 轮**」 |
| 思考 | 会话级 `think=on`（脚本固定开启；配置优先级 会话>agent，覆盖 agent 级 `think:false`） |
| 客户端窗口 | `PROBE_TOTAL_WAIT=3300`（v10 是 420 ⇒ 客户端先断、服务端还在跑） |
| 原始留痕 | `raw/v11_fix_round3.events.jsonl`（17390 条）、`raw/v11_fix_round3.log`、`raw/uvicorn8790_v11.log`（**空，无服务端异常**） |

## 结论一句话

**完整跑到 `run_end`（aborted=false），3 轮讨论走满，无一次执行异常。**
两项修复单测有覆盖；本场未再出现 v10 的中止错误，也未出现幻影气泡。

## 一、是否跑完（v10 的最大疑问）

```
run_end | {"mode": "debate", "aborted": false}
system  | {"msg": "会话结束"}
```

| v10（修复前） | v11（修复后） |
|---|---|
| 第 4 个发言位被中止：`Separator is not found...` 系统错误 | **全程无 error / 无 system 错误** |
| 客户端 420s 窗口先断，服务端又跑 34 分钟 | 客户端窗口 3300s，单次 11 分 14 秒跑完，`run_end=True` |

轮次结构（9 个 turn = 6 个正式发言位 + 3 个 HITL 回复轮，与 `max_rounds=3 × 2 agent` 完全吻合）：

| # | agent | thinking 事件 | interim | 工具调用 |
|---|---|---|---|---|
| 0 | Codex（轮 1 立论） | 1346 | false | 1 × ask_human_input |
| 1 | Codex（HITL 回复轮） | 1243 | **true** | **6 × ask_human_input** |
| 2 | Codex（同一 turn 位收尾） | 639 | false | 0 |
| 3 | Claude（轮 1） | 5102 | false | 3 × ask_human_input |
| 4 | Claude（HITL 回复轮） | 1605 | false | 0 |
| 5 | Codex（轮 2） | 950 | false | 0 |
| 6 | Claude（轮 2） | 1970 | false | 1 × end_discussion |
| 7 | Codex（轮 3） | 0 | false | 0 |
| 8 | Claude（轮 3 总结） | 920 | false | 0 |

> `round_i` 不因 HITL 回复轮递增（既有语义）——所以 3 轮 = 6 个发言位，符合配置。

## 二、缺陷①：受控工具重复上报

**本场结论：app 记录的 7 次 codex 调用，对应 rollout 里 7 个互异 `call_id`，一次不多一次不少——
即「重复上报」在本场并不存在，重复的是模型自己的行为。**

rollout（`~/.codex/sessions/2026/09/20/rollout-2026-09-20T23-46-01-01a0bf7e-….jsonl`）取证：

| rollout 条目 | 条数 | call_id 是否互异 |
|---|---|---|
| `function_call`（name=ask_human_input） | 7 | ✅ 7 个互异（`call_spidl0fe / xqq0dwe3 / lrmijawe / 5z2w55ga / y6q1fei4 / ig3ll41y / 2dptvwmk`） |
| `mcp_tool_call_end` | 7 | — |
| `function_call_output` | 7 | — |

app 落盘 codex `tool_calls` 合计 = **7**，与 rollout **1:1 相等** ⇒ 既没多记（去重没有误伤真实调用），
也没漏记。修复（按 item id 去重）在这条路径上是**净防御**：有同 id 重复上报时才生效
（`tests/test_codex_appserver.py` 两例覆盖：同 id 只计一次 / 不同 id 各自计数）。

**顺带修正上一轮归因**：v10 我说「一轮 3 次调用，部分是重复上报」——按本场 rollout 证据，
**codex 是真的发起了 7 次调用**（7 个互异 call_id），属于模型自驱重试；app 侧不存在幽灵重复。
用户实际仍只被问 **2 次**（编排层「单待问 · 取最后」把同轮多次调用收敛成 1 次暂停）。

为什么模型会连调多次：**HITL 工具回执只回「已收到」，不把用户答案回灌给模型**；
模型拿不到答案，就按提示词「还有必须确认的信息可以继续追问」继续重试。
证据：第 2 轮那 6 次调用分布在 `[1613][1614][1819][1820][1821][2047]` 多个相隔推理段之后。

## 三、缺陷②：CLI 读流上限

本场 **未触发**（claude 最长一轮 thinking 事件 5102 条、落盘 25505 字符，但单帧 JSON 没超过 64KiB；
服务端日志为空、无任何 error）。因此这条修复本场无法用端到端证据证伪，
由单元测试兜：`tests/test_executor.py::TestLongLineReading`

- 小 limit（128B）StreamReader 喂 5000B 单行 → `_readline_resilient` **不抛异常**，且**下一行能继续正常读出**；
- 真子进程吐 **300KB 单行** → `_stream_subprocess` 无 error、内容不截断。

实现坑（已写进注释）：CPython `StreamReader.readline()` 超限时**先 `self._buffer.clear()` 再抛
`ValueError`** ⇒ 该行内容事后**救不回来**，所以「异常后分片重读」的方案无效；
最终方案是**抬限为主（32MiB）+ 跳过该行降级兜底（只求不崩）**。

## 四、幻影气泡 / 对账

| 项 | 值 |
|---|---|
| prompt 日志（= app 实际调用 CLI 次数） | 9 条：Codex 5 + Claude 4 |
| 落盘消息 | 14 条 = human 4 + **Codex 5 + Claude 4** + system 1 |
| 1 发言 = 1 气泡 | ✅ Codex 5 turn = 5 条；Claude 4 turn = 4 条 |
| rollout `event_msg/user_message` | **5** == 本地 codex 消息 **5**（新回合边界正确） |
| reconcile 跑一次 | cli_turns 5/4 == 本地 5/4 → appended **0**，条数 14→14 ✅ |
| reconcile 幂等（再跑） | changed **0**，仍 14 条 ✅ |

思考产出：Codex 5 条中 4 条带 thinking（第 5 条为短认同发言，0）；Claude 4 条**全部**带 thinking。

## 五、诚实边界

1. **缺陷②本场未被端到端触发**——本场没有出现超 64KiB 的 stdout 单行，所以「不再中止」只有
   单测支撑 + `run_end=True` 的反证（无异常发生）。要真触发需要更长的 claude 轮（v10 是跑到 437KB transcript）。
2. **讨论内容的正确性不在本次验证范围**（gemma 26B 的方案质量），本证据只验证链路与机制。
3. codex 一轮发 7 次调用属于**模型行为**，不是链路缺陷；若要减少无效重试，
   应改「工具回执把用户答案回灌给模型」或收紧提示词——尚未改动，等用户拍板。
