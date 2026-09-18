# probe/ · claude+gemma「empty or malformed (HTTP 200)」复现 / 定位 / 修复验证（2026-09-18）

## 结论（TL;DR）

用户看到的 `Probe Claude 执行出错，会话中止：API Error: API returned an empty or malformed response (HTTP 200)`
**不是中断造成的、也与「要不要换新会话」无关**（同 session `--resume` 续接本身正常）。它是**网关两个 bug 叠加**，
2026-09-18 已修（`backend/gateway.py`），本目录保留定位证据与修复前后的对照复现。

### Bug 1 · 思考块与正文块混在同一个 content block（根因）
`_ResponsesStreamAdapter` / `_AnthropicTranslator` 在**正文块（index 0, type=text）已经开始之后**，
把迟到的 Ollama `response.reasoning_summary_text.done`（思考全文）当成
`content_block_delta{index:0, delta:{type:"thinking_delta"}}` 发出去 —— 同一个 index 里既有
`text_delta` 又有 `thinking_delta`。Anthropic 流式协议不允许这种切换，claude-code SDK 判定流非法
**提前中断该流**（实测 6.6s / 1901 字节处结束），随即触发 SDK 的 **非流式回退**。

为什么思考会「迟到」？Ollama 的真实事件序（curl 实测 `raw/ollama_event_order.sse`）：

```
response.output_item.added(item=reasoning)
response.reasoning_summary_text.delta × 153      ← 网关忽略（未处理该事件）
response.reasoning_summary_text.done             ← 全文一次性给出 → 网关发 thinking_delta
response.output_item.done(item=reasoning)
```

而 app 场（带 `---` 分隔符的 prompt）的真实序是 **正文 → 思考 → 正文**：gemma 先吐 `---\n` 正文，
再进思考（期间 153 个 delta 被忽略、6 秒），最后才吐正式答案。于是 `thinking_delta` 落在了已经打开的
text 块 index 上。抓包原文（`raw/tap.before_fix.jsonl`）：

```
content_block_start  {"index":0,"content_block":{"type":"text","text":""}}
content_block_delta  {"index":0,"delta":{"type":"text_delta","text":"---"}}
content_block_delta  {"index":0,"delta":{"type":"thinking_delta","thinking":"The user wants me to count..."}}   ← 非法
```

### Bug 2 · 非流式请求被误当流式（致命放大器）
`handle_messages` 里 `want_stream = bool(body.get("stream", True))`（`anthropic_to_responses` 同款）——
**默认值写反了**。Anthropic Messages API 中 `stream` **缺省即 false**；claude 的**非流式回退**请求
**不带该字段**，于是被网关当成流式，回了 `text/event-stream`（138KB SSE）。claude 用 JSON 解析 SSE →
`cuu(data)` 判定失败 → 抛 `API returned an empty or malformed response (HTTP 200)`（该报错串来自 claude
二进制的**非流式回退**分支，不是网关写的）。

- 实测（修复前）：`POST /v1/messages`（body 无 `stream`）→ `content-type: text/event-stream` ❌
                  `POST /v1/messages`（body 带 `stream:false`）→ `content-type: application/json` ✅
- 也因此，Bug 1 一旦触发回退，回退**必定失败**，用户看到的就是那句错误。

## 修复（`backend/gateway.py`）

1. **翻译层改成「单一开块 + 顺序 index 分配」**：`_open_idx` / `_open_type` / `_next_idx`，任何类型切换
   都先 `content_block_stop` 旧块再 `content_block_start` 新块。`正文 → 思考 → 正文` 得到 index 0/1/2
   三个块，各自配对，index 连续无空洞（不再假设「思考必在前」）。
2. **`stream` 缺省取 false**：`handle_messages` 的 `want_stream` 与 `anthropic_to_responses` 的
   `resp["stream"]` 同步改为 `bool(body.get("stream", False))`（两者必须一致，否则非流式请求会给 Ollama
   带 `stream=true`，回 SSE、`r.json()` 解析失败）。

## 修复前 / 后对照（app 内真跑，隔离实例 8779）

| | 修复前 `raw/app_repro2.before_fix.log` + `raw/tap.before_fix.jsonl` | 修复后 `raw/app_repro.after_fix.log` + `raw/tap.after_fix_noabort.jsonl` |
|---|---|---|
| `/v1/messages` 请求 | 2 次：第 1 次 `stream=true`，第 2 次**无 `stream` 字段**（SDK 非流式回退） | 2 次，**均 `stream=true`**（无需回退） |
| 第 1 个响应 | 1901B 后**被 claude 掐断**（thinking 混排进 text 块） | 正常收流：`text(0) → thinking(1) → text(2) → message_stop` |
| 第 2 个响应 | 138KB **SSE** 回给一个非流式请求 → claude 解析失败 | —（未发生） |
| 块序校验（脚本逐事件回放） | — | **违规 0**（无「start 未 stop / delta 跨块 / stop 错块」） |
| SSE error 事件 | 有（claude 侧报 empty or malformed） | **无** |
| 会话结果 | claude 报错中止 | 正常跑完 2 轮辩论（`turn_end` → 第 2 轮 fake → `会话结束` / `run_end`） |

## 中止 → 继续 的验证（用户明确要求「会话得保持，不要开新会话」）

`raw/app_repro.after_fix_abort3.log` + `raw/tap.jsonl`：

- 探针先经 REST 建会话拿 sid，再 WS `start`；claude 生成中出现 3 条文本后发 `abort`。
- abort 通道：`control_request`（stdin 帧）→ 2.5s 看门狗未收尾 → 强制终止进程（退出码 143）；
  落盘 `turn_end interrupted=True` + 系统消息「本次发言已被中止（内容作废，不计入轮次）」。
- 等中止真正落地后再发 `resume_turn` → **被接受**：「继续讨论：由 Probe Claude 重新发言」+ 新 `turn_start`。
- **同一个 cli session 被复用**：`sessions.json` 的 `agent_sessions = {Probe Claude: 912d02d1-…}`，
  transcript 里 `[Request interrupted by user]` 之后是新的 `user` 提示（「你上一次的发言被用户中止了…」），
  续接请求 `--resume 912d02d1-…`；续接回合的 SSE 块序同样合法、无回退、无 error。
- 探针的时间坑（已修进 `drive.py`）：abort 的 ack 到强制终止要 ~12s，**这期间发 resume 会被服务端以
  「该会话正在运行，无法继续」拒掉**；且动作检查必须放在「收到事件」之外（run_end 后不再有事件）。

## 实验矩阵（全部在项目内留痕）

| 实验 | 命令/mode | 结果 | 留痕 |
|---|---|---|---|
| B 未开始输出即中断 → resume | `claude-interrupt/run.py --mode stdin --tag b_stdin` | ✅ 续轮正常 | `claude-interrupt/raw/b_stdin.*` |
| B2 生成中途中断 → resume | `--mode stdin --tag b2_stdin` | ✅ 续轮正常（回复「1+1等于2。」） | `claude-interrupt/raw/b2_stdin.*` |
| C 同上，打 **8777**（用户实际实例） | `PROBE_BASE_URL=…:8777 --tag c_8777` | ✅ 续轮正常 | `claude-interrupt/raw/c_8777.*` |
| app 真跑：辩论（修复前） | `app-repro/drive.py`（隔离实例 8779） | ❌ 复现用户现象 | `app-repro/raw/app_repro2.before_fix.log` |
| app 真跑 + 抓包（修复前） | 同上，agent base_url → `tap.py`(8780) | ❌ 复现并抓到两个 bug | `app-repro/raw/tap.before_fix.jsonl` |
| S1 复刻 app 调用形态（短 prompt） | `app-repro/single.py --tag s1_full` | ✅ 正常 | `app-repro/raw/s1_full.*` |
| S4 用 app 场**原样 prompt**直跑 | `--tag s4_appprompt --prompt-file raw/app_prompt.txt` | ✅ 正常（5 分钟一直在数数） | `app-repro/raw/s4_appprompt.*` |
| Ollama 原始事件序 | `curl 11434/v1/responses`（思考开着，流式） | 153×`reasoning_summary_text.delta` → `…done` | `app-repro/raw/ollama_event_order.sse` |
| app 真跑（**修复后**） | `app-repro/drive.py` | ✅ 正常跑完，无回退、块序违规 0 | `app-repro/raw/app_repro.after_fix.log`、`tap.after_fix_noabort.jsonl` |
| app 真跑 中止→继续（**修复后**） | 同上（探针修好 sid/时序后） | ✅ 同 session 续接、续接回合正常流式 | `app-repro/raw/app_repro.after_fix_abort3.log`、`tap.jsonl` |

> 关键：**直跑（不经过 app 编排）复现不出来**，因为 claude 只在「流式被判非法」时才走非流式回退；
> 直跑那几次 gemma 没吐「正文在思考之前」的 `---`，块序列干净。必须有**思考块混排**才会踩到 Bug 2。

## 怎么重跑

```bash
# 1) 一个隔离的 app 实例（独立 config，不碰 data/config.json）
AGENTCHAT_CONFIG=$PWD/probe/app-repro/config.json \
  .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8779

# 2)（可选）抓包代理：claude → 8780 → 8779，原始字节落 raw/tap.jsonl
.venv/bin/python probe/app-repro/tap.py        # 并把 agent.base_url 指向 http://127.0.0.1:8780

# 3) 用 WS 真跑「建会话 → 辩论 → 中止 → 等落地 → 继续」，事件落 raw/app_repro.events.jsonl
AGENTCHAT_WS=ws://127.0.0.1:8779/ws .venv/bin/python probe/app-repro/drive.py

# 4) 只跑一轮 claude（复刻 executor 的 cmd/env），用于二分
.venv/bin/python probe/app-repro/single.py --tag s4_appprompt --prompt-file probe/app-repro/raw/app_prompt.txt

# 5) 直接驱动 claude CLI 复现「中断 → resume」（不经 app 编排）
.venv/bin/python probe/claude-interrupt/run.py --mode stdin --tag b2_stdin   # / sigint / repairexit / sameproc
```

## 留痕文件说明

**入库的精简证据（`evidence/`）**
- `evidence/before_fix_illegal_blocks.txt`：修复前抓包节选 —— ① index 0 先 `text_delta` 后 `thinking_delta`（非法）；
  ② 非流式回退请求（body 无 `stream`）收到 `content-type: text/event-stream` 的 138KB SSE。
- `evidence/ollama_event_order.txt`：Ollama `/v1/responses` 原生事件序（折叠）—— 证明「reasoning 先逐片 delta
  （网关忽略）、末尾 `.done` 才给全文」，故思考在网关侧必然是**迟到的大块**。

**脚本**
- `claude-interrupt/run.py`：直接驱动 claude CLI（复刻 executor 的帧格式/参数）。mode:
  `stdin`（app 现状的中断帧）/ `sigint`（模拟终端 Ctrl+C）/ `repairexit`（改写 transcript 后续接）/ `sameproc`（同进程续轮）。
- `app-repro/drive.py`：走完整编排层（REST 建会话 → WS start / abort / resume_turn）。
- `app-repro/single.py`：按 app 的 cmd+env 跑一轮（二分用）。
- `app-repro/tap.py`：透明抓包代理（不改 app 代码），把请求/响应原始字节写成 `raw/tap.jsonl`。

**原始运行产物（`**/raw/`，已 gitignore，仅本地保留，可随时按上面命令重跑）**
- `app-repro/raw/tap.before_fix.jsonl`：**修复前最关键的证据**（非法块序 + 非流式请求收到 SSE 全量）。
- `app-repro/raw/tap.after_fix_noabort.jsonl`、`tap.jsonl`：修复后两次真跑的抓包（块序违规 0）。
- `app-repro/raw/ollama_event_order.sse`：Ollama 原生事件序原始流。
- `app-repro/raw/app_repro*.log` / `*.events.jsonl`：三次 app 真跑的 WS 事件与时间线（before_fix / after_fix / after_fix_abort*）。
- `app-repro/raw/app_prompt.txt`：app 场 claude 实际收到的 1336 字符 prompt 原文。
- `app-repro/raw/s4_appprompt.*`、`s1_full.*`：直跑对照（stdout + transcript + 事件）。
- `claude-interrupt/raw/*`：claude 原生输出与 transcript、实验时间线（含 `b2_stdin`、`c_8777` 两个「续轮正常」对照）。
- `app-repro/work/.agentchat/`：app 原生会话存储（编排层视角，含 `agent_sessions` 复用证据）。

> 注：`app-repro/config.json` 的 agent `base_url` 会被改成 `http://127.0.0.1:8780`（抓包用），
> 重跑不带抓包时改回 `http://127.0.0.1:8779`。
