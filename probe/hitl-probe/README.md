# probe/hitl-probe · request_human_input（HITL 人工介入）可用性留痕（2026-09-18）

## 起因

用户会话 `s_317d5554「跳高训练计划」`（项目 `agent探针(留痕)`，team `探针Team（双壳 · 本地 gemma）`）：
议题是「想做一个跳高训练计划，请给多套方案让我看看」，用户希望 agent **调 `request_human_input`
回来问人**，但整场 6 轮 prompt、2 个 agent，**一次都没调用**（倒是 claude 两次调了 `end_discussion`）。

用户诉求：工具到底真可用还是假的？要求在项目内做留痕测试。

## 复现 / 取证方式（两层）

| 层 | 脚本 | 是否跑模型 | 产出 |
|---|---|---|---|
| 静态分层（调用链自底向上） | `probe_hitl_static.py` | 否 | `evidence/static.txt` |
| 端到端（隔离 app 实例 8790 真跑辩论） | `drive_hitl.py` + `config.json` | 是（**本机免费 gemma4:26b**，claude-code / codex 双 agent） | `raw/<tag>.events.jsonl`、`raw/<tag>.log`；`evidence/*_key_events.txt`、`evidence/*_tail.txt` |

重跑命令：

```bash
# ① 静态层（秒级、零 token）
.venv/bin/python probe/hitl-probe/probe_hitl_static.py

# ② 端到端（需先起隔离实例；会用本机 Ollama gemma4:26b 拉起真实 CLI）
AGENTCHAT_CONFIG=$PWD/probe/hitl-probe/config.json .venv/bin/python -m uvicorn backend.main:app \
    --host 127.0.0.1 --port 8790 --log-level warning &
AGENTCHAT_WS=ws://127.0.0.1:8790/ws AGENTCHAT_HTTP=http://127.0.0.1:8790 \
    .venv/bin/python probe/hitl-probe/drive_hitl.py
```

## 静态层结论（`evidence/static.txt`）

沿「模型 → 网关/CLI → 执行器 → 编排层 → 前端」逐层取证，**唯一断点在 codex 执行器上报环节**：

| 层 | 检查项 | 结果 |
|---|---|---|
| L1 | `terminate_signal_mcp.py` 的 `tools/list` 真的返回 `request_human_input`（含 schema），`tools/call` 正常回执 | ✅ |
| L2 | claude 路径：网关把该工具以**干净名**转发给本地模型（同时放行内置工具、剥交互型与第三方 MCP） | ✅ |
| L2b | 回程名还原：干净名 → `mcp__terminate-signal__request_human_input`（内置工具不被误加前缀） | ✅ |
| L3 | **codex 把该 MCP 调用上报给编排层**（`tool_use` 事件，HITL 状态机唯一输入） | ❌ |
| L4 | 编排层识别（`_is_human_input` 三种写法 + 文本 json 兜底） | ✅ |
| L5 | 每轮 prompt 都注入了 HITL 指令（真实会话 `s_317d5554` 的 6 条 prompt 全含） | ✅ |

### L3 断链细节（真 bug）

codex 侧两条输出通路都**只放行 `end_discussion`**：

- `backend/executor.py:801`（app-server 实时 `item/completed`）：
  `elif _is_mcp_tool_item(it) and "end_discussion" in str(it.get("tool") or ""):`
- `backend/executor.py:1485`（`_parse_codex_line`，exec / rollout 回放）：
  `if ev.get("type") == "item.completed" and "end_discussion" in str(item.get("tool") or ""):`

`request_human_input` 落到 `_is_mcp_tool_item` 命中但名字不匹配 → 掉进下一分支
`_codex_native_tool_use(it)`（只认 `command_execution` / `file_change`）→ 返回 None → **事件被吞**。
对照实验结果（同一脚本内 end_discussion 对照组能通过）：

```
app-server item/completed type=mcpToolCall    → tool_use=[]      ❌
app-server item/completed type=mcp_tool_call  → tool_use=[]      ❌
exec/rollout _parse_codex_line  mcpToolCall   → tool_use=[]      ❌
exec/rollout _parse_codex_line  mcp_tool_call → tool_use=[]      ❌
对照组 end_discussion（app-server）            → tool_use=[('tool_use', {...})] ✅
```

即：**codex 就算调了 `request_human_input`，编排层也收不到 ⇒ 不会暂停、前端不会弹人工介入卡片。**
（另有一条 `executor.py:971` 的 `item/tool/call` 分支已经兼容了两者，但那是罕见回退路径。）

## 端到端结论（隔离实例 8790，双壳分别单跑 chat 模式，本机 gemma4:26b）

任务统一为：「设计跳高训练计划。**第一步必须调用 `request_human_input`** 向用户提问每周能练几次
（候选 1-2 / 3-4 / 5 次以上），不要在正文里问」。

| 场景 | teams / agent | 结果 |
|---|---|---|
| claude 侧 | `tm_hitl_c` → H-Probe Claude（claude-code → 网关 8790 → Ollama gemma） | ✅ **真调通**：`tool_use mcp__terminate-signal__request_human_input` → `tool_result`（回执）→ 编排层广播 `human_input_required` → 人工作答 → 续跑下一轮；**一小时内连问两次都正常** |
| codex 侧 | `tm_hitl_x` → H-Probe Codex（codex app-server → 本机 Ollama 直连） | ❌ **没调**：全程无 tool_use，改成在正文里反问用户 |

证据：

- `evidence/claude_key_events.txt`：两次完整 HITL 往返（`tool_use` + 回执 + `human_input_required` + 续跑），
  第二次的 options 是模型自己换的问法（「allo」高水平阶段），说明它真在用这个工具而不是照抄模板。
- `evidence/codex_key_events.txt`：只有 `turn_start / turn_end`，无工具事件。
- `evidence/codex_tail.txt`（**关键自述**）——codex 的思考里写得很清楚：

  ```
  Wait, looking at the error: `request_user_input is unavailable in Default mode`.
  Since the tool is unavailable, I am stuck in a bit of a paradox.
      But I can still ask the question in the text and hope the user responds.
  ```

  即 **codex + 本机 Ollama provider 下，`terminate-signal` MCP 的工具在模型侧就不可见**
  （`codex mcp list -c mcp_servers.terminate-signal...` 显示 server 本身 `enabled`，
  所以不是注册失败，而是这条 provider 路径没把工具交给模型）。

### 汇总：HITL 这条链路到底通不通

```
claude-code → 网关 → gemma        ✅ 全通（工具可见、可调用、编排层暂停、人工回答后续跑）
codex(app-server) → Ollama gemma  ❌ 两个断点叠加：
                                     ① 模型侧看不到该工具（codex 自述 unavailable）
                                     ② 即使看得到，executor 也只放行 end_discussion（静态 L3），调用会被吞
```

### 「跳高训练计划」那场为什么没人问？

不是工具坏了 —— **claude 侧工具是通的**（本次探针直接证明了）。那场是 debate 模式，提示词把
HITL 表述成「当你**需要**用户从多个方案中选择时」才调用，而原议题「请给多套方案让我看看」
被两个 gemma 理解成「把多套方案摆出来即可」，于是双双跳过提问、直接开讲，最后按惯例收敛到
`end_discussion`。要提高命中率，得改的是**提示词强度**（把「需要」改成显式触发条件），
而不是工具链路。这一条属于产品决策，未改代码（本次只做留痕，不动实现）。

---

## 附：留痕会话的**气泡颜色**修复（2026-09-19）

用户反馈：导入的会话里「两个 codex 的会话不是绿色气泡」「有个 claude 好像在和 fake 对话」。查证如下。

### 根因（两件事）

1. **speaker 名对不上配置 → 颜色回落成 claude 蓝。**
   前端气泡颜色是 `cfg_agents_by_name(speaker)` **按名字**查 agent 的 `type`
   （`frontend/index.html` `renderHistoryEntry`），查不到就回落到 `claude-code`。
   探针会话的 speaker 是隔离实例里的名字（`H-Probe Codex` / `H-Probe Claude` / `Probe Claude` / `Probe Fake`），
   目标项目里根本没有这些 agent ⇒ **codex 显示成蓝气泡、fake 陪练也显示成蓝的**。
2. **「claude 和 fake 对话」不是 bug，是探针设计。**
   `s_3100f43b` 来自下午网关复现探针，其 team 就是 `probe（claude+gemma vs fake）`
   —— 用 fake 当陪练驱动辩论轮次（不烧 token），claude 是真跑的那个。
   只是它被归到了「双壳(Codex+Claude)」team 下，看起来才像错配。

### 修复

- `probe/import_probe_sessions.py`：新增 `--speaker-map` / `--sid-speaker-map` / `--only` / `--force`。
  导入时把 speaker 名改写成目标项目里**真实存在的 agent 名**，`agent_sessions` 键与
  prompts/tools 侧 jsonl 的 `agent` 字段同步改名（否则工具状态标签按名匹配不上）。
- `frontend/index.html`：名字查不到的回落从 `claude-code` 改为新增的中性灰 `unknown`
  （历史/实时流两处一致），**不再把未知发言者染成 claude 蓝**。
- 配置（`data/config.json`，不入 git）：新建 fake agent「Fake 陪练（调试 · 不烧 token）」+
  team「探针Team（调试 · Claude Code + Fake 陪练）」，把 `s_3100f43b` 归进去；
  5 条纯 claude 的网关复现会话归到「探针Team（单壳 · Claude Code 本地 gemma）」。

### 验证（真数据 + 真 DOM）

`verify_bubble_color.js`：拉 8777 的真实 `/api/config` 与真实会话，用 `frontend/index.html`
的真实渲染函数渲染，断言气泡颜色类。结果全绿：

| 会话 | 发言者 → 气泡类（实测） |
|---|---|
| s_0449ef1d / s_a3d8022f | Codex (本地 gemma) → **codex（绿 #e6f6ec）** |
| s_a3d8022f / s_3100f43b | Claude Code (本地 gemma) → claude-code（蓝） |
| s_54eb6e23 | Claude Code 单壳 (本地 gemma) → claude-code（蓝） |
| s_3100f43b | Fake 陪练（调试 · 不烧 token） → **fake（橙 #fff4e6）** |

回归测试：`tests/test_frontend_render.js` 新增 5 条断言（未知 speaker → unknown 灰、codex 绿、
实时流未带 atype 不为蓝）；全量 302 例 Python + 前端 jsdom 均通过。

运行：
```bash
NODE_PATH=<managed node workspace>/node_modules node probe/hitl-probe/verify_bubble_color.js
```
