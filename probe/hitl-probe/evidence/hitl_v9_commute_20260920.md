# v9 端到端留痕 · 通勤 90 分钟碎片化英语学习（2026-09-20）

> 跑在**去掉待问队列之后**的代码上（commit `532cfc1`），隔离实例 8790 重启后运行。
> 与 v8（改动前、队列版）对照。

## 运行条件

- 隔离实例 `127.0.0.1:8790`，配置 `probe/hitl-probe/config.json`，数据落 `probe/hitl-probe/work`。
- 会话 `s_6d125fe6`，team `tm_hitl`（双壳：H-Probe Codex + H-Probe Claude），`max_rounds=2`，会话级 think=on。
- 模型：本机 Ollama `gemma4:26b`（**免费**），未接远程付费模型。
- 议题：每天通勤单程 90 分钟，如何把碎片时间用成系统的英语学习。
- 原始事件：`probe/hitl-probe/raw/v9_commute.events.jsonl`（9494 条）；日志 `raw/v9_commute.log`。
- **正常跑到 `run_end`**（未超时）。

## 事件主干

| 序 | 事件 | 说明 |
|---|---|---|
| 1 | `turn_start/end` Codex | 184 字，直接给了初版思路，**未调工具提问** |
| 2 | `turn_start` Claude → `tool_use request_human_input` ×1 → `turn_end` | 405 字，**只调用一次**提问工具 |
| 3 | `human_input_required` → `human(人工回答)` | 问「目标 + 每天整块时间」，用户答「雅思 6.5 / 睡前 30 分钟」 |
| 4 | `turn_start/end` Claude | **1387 字**：拿到回答后的回复轮 = 归纳结论 ✅ |
| 5 | `turn_start/end` Codex | **1950 字**：收到 claude 的结论后继续 ✅ |
| 6 | `turn_start/end` Claude | **1420 字** ✅ |
| 7 | `system 会话结束` → `run_end` | 正常收敛 |

工具事件总数 **1**，`human_input_required` **1** 次 ⇒ 一次调用对应一次提问，没有重复提问、也没有排队遗留。

## 结论

1. **一问一答的来回结构成立**：`turn_end → human_input_required → human(人工回答) → turn_start` 严格交替，
   每条回答单独落盘为 `role=人工回答` 的 human 消息（前端右侧橙色气泡）。
2. **FR20.2（codex 正文兜底）继续生效**：Codex 第 4 轮 1950 字、Claude 回复轮 1387 字，结论能完整传下去。
3. **无回归**：去掉队列后链路照常收敛到 `run_end`（v8 是 600s 超时截断）。

## 诚实边界

本场**没有触发**「同一次发言里重复调用 `request_human_input`」（Codex 起问轮干脆没调工具），
所以 v9 **不能直接证明**「不再把同一个问题问两遍」——那一点的证据是：
① v8（改动前代码）实测 Codex/Claude 都在同一次发言里调了两次、队列版把用户问了两遍；
② 单测 `tests/test_hitl.py::TestChatOneAskOneReplyLoop::test_duplicate_asks_in_one_turn_keep_only_last`
（异常连问两次 → 只问最后一个、不排队）。
另需注意：**是否提问仍取决于模型**（本场 Codex 就没问），这不是链路问题。
