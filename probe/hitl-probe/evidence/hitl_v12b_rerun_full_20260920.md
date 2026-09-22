# HITL v12b 重跑留痕（回执+prompt收敛后，跑通完整 3 轮）

> 日期：2026-09-20｜SESSION `s_4871a922`｜隔离实例 8790｜gemma4:26b（本机免费）
> 改动范围：回执两处（`terminate_signal_mcp.py:73` + `executor.py:2183`）+ 工具描述 + `prompt_defaults.py:119/134` 收敛 + 2 条测试断言
> 目的：确认 v12 空转是偶发、并验证回执收敛是否根除 codex 连调

## 一、结论先行（纠正 v12 的误判）

**v12（上一轮）的「codex 只调 1 次」是空转假象，不是回执生效。**

v12 那次：codex 第 1 轮问预算 → 回复轮（第 2 轮）空转吐 `pseudo` 超时中止，**根本没走到第 2 轮连调环节**，所以只统计到 1 次调用。
v12b 真正跑完 3 轮 → codex 第 2 轮**连调 8 次** `ask_human_input`。

⇒ **回执收敛 + prompt 收敛没有根除 codex 连调**（v11=7 次，v12b=8 次，持平甚至略增）。连调是 gemma4:26b 在 codex app-server 路径下的固有自驱重试行为。

## 二、v12b 完整事件时间线（铁证，含行号）

| 行号 | 事件 | 说明 |
|---|---|---|
| 3 | turn_start codex | 第1轮发言 |
| 215 | tool_use ask_human_input（问预算） | **1 次** |
| 1552 | turn_end codex | |
| 1553 | human_input_required（codex·预算） | **暂停①** |
| 1556 | turn_start codex（回复轮，拿到「预算1.5万」） | |
| 1772/1773/2019/2020/2433/2434/2494/2496 | **8 次 tool_use ask_human_input**（全问「装修风格/功能需求」的不同 phrasing 变体） | 同一 turn 内连调 |
| 2496 | turn_end codex interim=true | |
| 2497 | human_input_required（codex·风格，**只 1 次**） | **暂停②** |
| 2500–4396 | turn_start/end codex（拿风格回答，正常发言） | |
| 4397–5293 | claude 发言 → 调 ask_human_input（问预算，走 `mcp__terminate-signal`） | **1 次** |
| 5294 | tool_result：「本次发言到此结束，请交还控制权…」 | 新回执生效 ✅ |
| 8821 | human_input_required（claude·预算） | **暂停③** |
| 8824–14185 | claude/codex 各 2 轮正常发言 | |
| 14004 | codex end_discussion | 收敛 |
| 14188 | run_end（aborted=false） | **跑通** |

## 三、关键事实

1. **3 轮辩论跑通**：run_end=true/aborted=false；结论「北向客厅软装改造方案达成一致，预算 1.5 万以内」。
2. **codex 连调未消除**：第 2 轮同一 turn 内连调 **8 次** ask_human_input（v11=7）。诱因不变 = 回执不回灌答案，模型预期答案内联回来、没等到就换 phrasing 重发。
3. **用户侧每轮只暂停 1 次**：靠**单待问语义**（同轮多次工具调用收敛为 1 待问、取最后，`_human_input_request`），8 次 → 1 次 `human_input_required`。call_id 去重（缺陷①）是防原生重复上报的额外保险。
4. **claude 路径干净**：只调 1 次，收到新回执「本次发言到此结束」正确。
5. **回执/prompt 收敛的价值（真实有效部分）**：符合用户「agent 不需知 app 内部」口径——去掉了「系统暂停整场/继续暂停/重复问题」的泄漏；claude 收到的回执已是中性契约。但未触及连调根因。

## 四、连调根因与根治选项（待用户拍板）

连调根因 = **工具回执是同步的、不携带答案**，模型拿不到答案自驱重试。
- 选项 A（现状）：靠单待问语义 + call_id 去重兜底，用户无感（每轮只停 1 次）；代价 = 模型侧浪费 8 次工具调用（token/延迟）。
- 选项 B（根治）：让 HITL 回复轮把用户答案**回灌进回执**（或回执后追加一条 tool_result 携带答案），模型拿到答案即止；但需解决「回执同步 vs 答案异步」的架构衔接。

## 五、pytest 状态

全量 422 通过（回执+prompt 收敛未破任何断言）。
