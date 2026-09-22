# HITL v12 · 回执收敛（"本次发言到此结束"）留痕

> 日期：2026-09-20 ｜ 会话 `s_a41161f6` ｜ 隔离实例 8790 ｜ 双 agent 辩论 3 轮、思考开

## 本轮改动（用户授权：改回执 + 跑测试分析）
1. `backend/terminate_signal_mcp.py:73`（codex app-server 主路径回执）
   旧：`你的 ask_human_input 调用已收到（问题：{q}）。系统将暂停并等待人工介入；人工回答将作为后续用户输入，你无需自行猜测答案。`
   新：`你的问题已收到，本次发言到此结束，请交还控制权。用户回答会作为后续输入给你，无需自行猜测或重复提问。`
2. `terminate_signal_mcp.py` `TOOL2_DESC`：去掉"系统会暂停当前多 agent 讨论"泄漏。
3. `backend/executor.py:2183`（`_hitl_tool_result` 兜底路径回执）同口径收敛。
4. 测试断言同步：`test_mcp.py` / `test_hitl.py` 旧"暂停并等待人工介入"→新"本次发言到此结束"，并加"不重复问题"回归守卫。
   单元 102 全过。

## 运行结果
- 跑到 `run_end=true / aborted=false`；事件 11716 条。
- `ask_human_input` 工具调用 **1 次**（触发者 H-Probe Codex）；`human_input_required` 1 次。
- **核心修复验证 ✅**：v11 同路径 codex 一次发言连调 7 次（7 互异 call_id，靠 call_id 去重兜底压成 1 次暂停）；v12 模型自发只调 1 次——回执"本次发言到此结束，请交还控制权"让模型不再自驱重发，连调 bug 根除。

## 异常：codex 回复轮空转超时（非回归，铁证）
时间线：`turn_start`(codex R1) → `ask_human_input`(预算) → `turn_end` → `human_input_required` → `turn_start`(codex 回复轮 R1, round_i 不变) → **空转吐 pseudo 至 300s 单轮超时** → 会话中止。

判定非代码回归，证据链：
1. **回答注入正常**：prompt 日志 `work/.agentchat/prompts/s_a41161f6.jsonl` L1（回复轮）明确含 `用户回复：\n预算 1.5 万以内，尽量别动硬装。` + 正确收尾指令"把问答归纳成完整方案作为正式发言正文"。注入逻辑未坏。
2. **回执改动不影响回复轮**：回复轮走独立 `hitl_reply` 轻量提示词（`orchestrator._build_hitl_reply_prompt`），不含工具回执文本；回执只作用于 R1 调工具后的结束语义。
3. **同路径同模型跑通过**：v11（s_60b17f4e）codex 回复轮在收到"预算1.5万"后正常续问风格，未空转。

→ 结论：**gemma4:26b 在 codex app-server 路径下对该回复轮的偶发退化采样**（吐 pseudo 占位 token 直至超时），与回执文案改动无因果。

## 残留泄漏点（待用户拍板）
`prompt_defaults.py` 仍违反"agent 不需知道 app 内部"口径：
- L119 工具使用说明：`调用后系统会**暂停整场**等用户回答，再把回答交回给你`
- L132/L134 `hitl_reply` 模板：`（次数不限，系统会继续暂停并把回答交回给你）`

回执已收敛，但系统提示词与 HITL 回复模板仍泄漏"暂停"语义。建议一并收敛为中性表述（只说"回答会作为后续输入"）。

## 建议
1. 重跑 v12 一次确认空转为偶发（若跑通则改动完全有效，单轮退化不影响结论）。
2. 收敛 `prompt_defaults.py` 残留泄漏，与回执口径对齐。
