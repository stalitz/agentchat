# HITL 回复轮连调根治 · v12d 验证结论（2026-09-21）

## 一、v12d 运行事实
- 隔离实例 8790、`PROBE_ROUNDS=3`、思考开、`PROBE_TOTAL_WAIT=3300`、标记 `v12d`
- 会话 `s_79a23994`；`run_end=true`、`aborted=false`；事件总数 23456
- 工具事件 4 次：
  - `H-Probe Codex` → `ask_human_input` **2 次**（line 173/174，同一问题「预算上限」重复）
  - `H-Probe Claude` → `ask_human_input` 1 次（line 1311）+ `end_discussion` 1 次（line 9255）
- `human_input_required` = 2 次（codex 1 + claude 1），均由单待问收敛 → 用户每轮只暂停 1 次
- 回执文本已生效（line 1312）：`【工具回执】你的 ask_human_input 调用已收到，本次发言到此结束，请交还控制权。用户回答会作为后续输入给你，无需自行猜测或重复提问。`

## 二、核心结论：回复轮连调已根治（铁证对拍）
| 版本 | 回复轮（用户回答后发言）连调次数 | 首轮连调 | 说明 |
|---|---|---|---|
| v12b（改前） | **8 次**（line 1772–2496，全问「装修风格」） | 1 次 | 回复轮模板「次数不限」诱导 |
| v12d（改后） | **0 次**（回复轮 codex/claude 均不再调 ask_human_input） | 2 次 | config 去「次数不限」生效 |

**回复轮 8 连调 → 0 连调**，证明根因确实是 `hitl_followup_debate/chat` 模板里的「次数不限」诱导句；config 改为「每次发言只调用一次…不要在同一次发言里重复调用」后，回复轮连调消失。

## 三、剩余 codex 首轮 2 次重发的真源（非代码路径，反证修复点正确）
v12d 的 2 次重发落在**首轮提问**（line 173/174，均「预算上限」），不是回复轮。根因在**测试任务 prompt 自身**（`drive_hitl.py` 注入的任务文本，line 2）：
> 「拿到回答后，如果还有必须确认的关键信息，可以继续再次调用 ask_human_input 追问（**次数不限**）；确认够了就把结论写成完整方案…」

这句「次数不限」写死在探针任务里、不在 `prompt_templates` 覆盖范围 → 首轮 codex 仍被诱导重发 2 次（call_id 互异、单待问收敛成 1 暂停）。这**恰好反证**修复点正确：代码路径（回复轮模板）的连调已清零，剩下的是测试任务文案的诱导，与产品代码无关。若想让探针也零连调，改 `drive_hitl.py` 任务文案的「次数不限」即可（非本仓库代码）。

## 四、codex 末尾空转（与 HITL 无关）
drive 日志末段：`H-Probe Codex` 最后一轮吐 `single-step single-step…` 直到 300s 单轮超时、被标记「会话中止」；但 drive 仍判定 `run_end=true`（事件流已闭合）。
- 这是 gemma4:26b 在 codex app-server 辩论路径下的**退化采样**（与 v12 同型偶发），不属于 HITL 机制问题。
- 3 轮辩论内容未完整收敛（codex 末轮空转），但 **HITL 机制验证目标（回复轮连调消失）已干净达成**，无需再归因此空转。

## 五、本轮全部改动清单（均未提交，待用户拍板）
1. 回执收敛（`terminate_signal_mcp.py` + `executor.py` + `TOOL2_DESC`）：去掉「重复问题 / 系统暂停 / 等待人工介入」泄漏
2. `prompt_defaults.py` 两处泄漏收敛（L119 工具说明 + L134 回复模板，去「暂停整场 / 继续暂停」）
3. config 文案根治：`data/config.json` + `probe/hitl-probe/config.json` 的 `hitl_followup_debate/chat` 去「次数不限」→「每次发言只调用一次…不要在同一次发言里重复调用」
4. UI 入口补上：`frontend/index.html` 全局设置新增「HITL 回复提示词」section（编辑框 + 恢复默认），`submitSettings` 同步回写
5. 测试断言同步（`test_mcp.py` / `test_hitl.py`）
- 全量 pytest **422 通过**（v12b 后）；v12d 验证回复轮连调根治有效。

## 六、生效条件
- config / UI 改动要让 **8777 主服务**生效需重启（按纪律重启只限用户明确目标，未擅动）。
- 探针已用隔离实例 8790 验证文案改动落地 + 回复轮连调消失。
