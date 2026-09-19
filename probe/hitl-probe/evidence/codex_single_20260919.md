# codex 单壳端到端复跑确认 + 静默空应答行为（2026-09-19）

## 0. 复跑命令与产物

```bash
# 隔离实例（端口 8793，不碰用户 8777；本机免费 gemma4:26b，单壳 codex）
AGENTCHAT_CONFIG=$PWD/probe/hitl-probe/config.json \
  .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8793 --log-level warning &
AGENTCHAT_WS=ws://127.0.0.1:8793/ws AGENTCHAT_HTTP=http://127.0.0.1:8793 \
  PROBE_PROJECT=pr_hitl PROBE_TEAM=tm_hitl_x PROBE_TAG=codex_single_20260919 \
  PROBE_ROUNDS=2 PROBE_TOTAL_WAIT=300 \
  PROBE_TASK="...请调用 request_human_input 工具来问；如果工具确实不可用，也要在正文里清楚地把这个问题问出来..." \
  .venv/bin/python probe/hitl-probe/drive_hitl.py
```

产物：`raw/codex_single_20260919.events.jsonl`、`raw/codex_single_20260919.log`、
会话 `work/.agentchat/prompts/s_1c9dcdc8.jsonl`。

## 1. 复跑结论：codex 仍调不起 request_human_input（与 2026-09-18 一致）

| 项 | 本次实测 |
|---|---|
| 会话 | `s_1c9dcdc8`（单壳 codex + 本地 gemma） |
| prompt 是否注入 HITL 硬指令 | ✅ 含「必须调用 request_human_input」「禁止用正文反问」（`s_1c9dcdc8.jsonl`） |
| `tool_use` 事件数 | **0** |
| `human_input_required` 次数 | **0** |
| codex 实际行为 | 在**正文**里反问「你现在每周能保证几次训练？」+ 候选 1-2/3-4/5 次以上 |

**根因 = 模型侧工具可见性，不是提示词、也不是 executor 上报。**
- executor 上报断链（只放行 end_discussion）已在 commit 3900936 修好（L3 ❌→✅）；
  但**该修复必要但不充分**——codex + Ollama provider 这条路径下，`terminate-signal` MCP 的
  `request_human_input` 在模型侧根本不可见（2026-09-18 自述 `request_user_input is unavailable in Default mode`），
  所以 codex 压根不会发起调用，上报修复无的放矢。
- 提示词硬条件也已就位（prompt_defaults.hitl_chat），本次 prompt 确实注入了，仍无效 → 排除提示词。

## 2. 静默空应答 `{"answers": {}}` 在终端 vs 在本 app 的行为

### 代码路径（`backend/executor.py`）

codex 原生「问用户」通道 = `item/tool/requestUserInput`（JSON-RPC ServerRequest）。
读循环（~:988）：

```python
if "method" in o and "id" in o and isinstance(o.get("id"), (str, int)):
    if self._as_autoanswer(o["method"], o["id"], o.get("params") or {}):
        continue                                   # ← 命中即跳过 _as_queue，编排层收不到
```

`_as_autoanswer`（:1067）：

```python
if method == "item/tool/requestUserInput":
    self._as_send({"jsonrpc": "2.0", "id": rid, "result": {"answers": {}}})  # 立即空应答
    return True                                    # ← True 让上面 continue
```

### 行为对比

| 场景 | `requestUserInput` 是否真的等用户 |
|---|---|
| **codex 原生终端 / TUI**（独立 `codex` CLI，交互模式） | ✅ **真等**：codex 暂停运行、弹出输入框等真人打字，回车后才继续。这就是 `requestUserInput` 的设计目的。 |
| **本 app（app-server）** | ❌ **不真等**：`_as_autoanswer` 立即回 `{"answers": {}}` 并 `return True`，读循环 `continue`，**不进 `_as_queue`** ⇒ 编排层永远收不到、不暂停、不广播 `human_input_required`。codex 拿到**空答案 map** 继续跑，等于「用户没答 / 答了空」→ 模型自说自话。 |

即：在本 app 里 `{"answers": {}}` 是**静默假确认（自动空应答短路）**，不是真的等用户确认。

### 本次 run 是否触发该分支

**未触发。** codex 在 Default mode 下根本没发 `requestUserInput`（直接正文反问），
`raw/codex_single_20260919.events.jsonl` 中无 `requestUserInput` / `"answers"` / `human_input_required`。
该 `_as_autoanswer` 分支是**潜在断点**：一旦 codex 真发原生 `requestUserInput`，本 app 会用空答案把它糊弄过去，
而不是暂停等人。

## 3. 当前 codex 在两条 HITL 通道上都不给真介入

| 通道 | 现状 | 卡在哪 |
|---|---|---|
| MCP `request_human_input`（我们做的工具） | 编排层已能暂停（claude 已验证）；codex 调不到 | **模型侧**：codex+Ollama 看不到该工具（可见性） |
| 原生 `requestUserInput`（codex 内建） | 本 app 静默空应答、不暂停 | **app 侧**：`_as_autoanswer` 短路；且 Default mode 下 codex 不发 |

## 4. 待决策（未改代码，仅留痕）

若要让 codex 原生通道也变成真 HITL，应把 `item/tool/requestUserInput` 接到
`_pause_for_human_input`（与 MCP 工具同等待遇），而非在 `_as_autoanswer` 里空应答。
是否接、何时接由用户定（属待定项 ②）。
