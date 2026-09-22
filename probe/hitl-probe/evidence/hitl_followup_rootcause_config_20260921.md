# HITL 回复轮连调根因 + 提示词配置抽取（2026-09-21）

> 会话 s_4871a922（v12b）实证 + 代码核对。结论：codex 回复轮连调根因在 `hitl_followup_debate` 提示词，已将回复提示词抽到全局配置（行为不变）。

## 一、根因（用户假设已证实）

**codex 每次多调都在「用户回复后的发言」（回复轮），不在首轮提问。**

| 模板 | 用于 | 关键措辞 | v12b 实测 |
|---|---|---|---|
| `hitl_debate`（prompt_defaults.py:109） | 第1轮首提问 | :119「调用后你的发言即**结束**」 | codex 调 **1 次** |
| `hitl_followup_debate`（prompt_defaults.py:131） | **回复轮**（拿到答案后） | :133「**继续调用 ask_human_input 工具**问即可（**次数不限**）」 | codex 连调 **8 次**（v12b:1772–2496 全在第2轮） |

v12b 的 8 次连调 100% 落在回复轮，`hitl_followup_debate:133` 的「继续调用…次数不限」是诱导源：gemma 理解为可反复调，回执同步、不回灌答案，没等到就换 phrasing 重发（call_id 互异，去重只挡同 id）。

## 二、B 方案澄清（非同步卡住）

回灌答案**已是现状**（v12b `s_4871a922.jsonl` L1 末尾实证含「用户回复：预算 1.5 万以内」）：agent 调工具→app 编排层暂停→用户答→答案作为**下一轮 `_run_turn` 输入**注入。agent 没同步卡住、没等。连调根因是回复轮提示词鼓励反复调，不是答案没回灌。

## 三、配置抽取（已做，零代码改动）

- `hitl_followup_debate` / `hitl_followup_chat` 本就在 `PROMPT_TEMPLATES_DEBATE`，orchestrator `self._pt` 已合并 `settings.prompt_templates.debate` 覆盖 → **代码本就支持 config 覆盖**，无需改 Python。
- 已把两提示词写入 `data/config.json` + `probe/hitl-probe/config.json` 的 `settings.prompt_templates.debate`，值**精确等于代码默认**（验证 `==default=True`），即现在可配置、但行为不变。
- 验证：`get_settings()` 能读到；JSON 合法；config 优先覆盖链路打通。

## 四、根治连调（待用户拍板，未改 wording）

在 config 的 `hitl_followup_debate` 把「次数不限」改为「一次只问一个最关键的问题，回答会作为后续输入」即可消除诱导——**只改 config，不动代码**。当前保留原 wording（用户要求先别直接改）。

## 五、pytest

全量 422 通过（config 改动不影响代码；上一轮回执+prompt收敛已验证）。
