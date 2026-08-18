import asyncio
import unittest

from tests.base import models, orch, store
from backend.orchestrator import _clean_answer


class Recorder:
    def __init__(self):
        self.events = []
        self.history_path = None
    async def __call__(self, ev):
        self.events.append(ev)
    def types(self):
        return [e.get("type") for e in self.events]


class StubRuntime:
    """测试用桩：模拟真实 agent CLI _runtime，分别吐 thinking 与 text。"""
    def __init__(self, chunks):
        self.cli_session_id = None
        self._chunks = chunks
    async def run(self, prompt, project_path, timeout, meta=None):
        for kind, text in self._chunks:
            yield (kind, text)


def make_session(agents, project_path, session_id, max_rounds=6, timeout=300, recorder=None):
    rec = recorder or Recorder()
    s = orch.Session(project_path, session_id, agents, max_rounds, rec, timeout=timeout)
    s._rec = rec
    return s, rec


class TestOrchestrator(unittest.TestCase):
    def setUp(self):
        self.proj = "/tmp/agentchat_test_proj_" + str(id(self))
        self.sid = store.create_session(self.proj, "测试会话")["id"]  # 建 .agentchat 目录 + 会话

    def run_session(self, s, task="任务", timeout=5):
        asyncio.run(asyncio.wait_for(s.start(task), timeout))

    def test_clean_answer_strips_gemma_separator(self):
        # gemma 等推理模型在 reasoning/answer 间输出 '---' 分隔符；
        # 清洗后应去掉首尾 --- 且不影响中间正文
        self.assertEqual(_clean_answer("---"), "")
        self.assertEqual(_clean_answer("  ---  "), "")
        self.assertEqual(_clean_answer("---\n\n我是 Gemma，一个本地模型。"), "我是 Gemma，一个本地模型。")
        self.assertEqual(_clean_answer("猫和狗\n---"), "猫和狗")
        self.assertEqual(_clean_answer("--- 答案 ---"), "答案")
        self.assertEqual(_clean_answer("正常无分隔符文本"), "正常无分隔符文本")
        # 中间的正文 --- 不误删
        self.assertEqual(_clean_answer("先说结论 --- 然后解释"), "先说结论 --- 然后解释")

    def test_thinking_persisted_in_history(self):
        # 思考必须随答案一起落盘，刷新后仍可见；且思考块与结论块内容不重复
        a1 = models.AgentConfig(name="A", type="fake")
        s, rec = make_session([a1], self.proj, self.sid)
        s.runtimes["A"] = StubRuntime([("thinking", "让我想想这个问题的思路。"), ("text", "这是最终答案。")])
        asyncio.run(s._run_turn(a1, 0, None))
        msg = store.read_session(self.proj, self.sid)["messages"][-1]
        self.assertEqual(msg["content"], "这是最终答案。")
        self.assertEqual(msg["thinking"], "让我想想这个问题的思路。")

    def test_thinking_fallback_no_duplicate(self):
        # 该轮只有 thinking、无答案文本时：正文 content 保持为空，thinking 单独落盘，
        # 避免正文气泡重复渲染思考内容
        a1 = models.AgentConfig(name="A", type="fake")
        s, rec = make_session([a1], self.proj, self.sid)
        s.runtimes["A"] = StubRuntime([("thinking", "只有思考没有答案")])
        asyncio.run(s._run_turn(a1, 0, None))
        msg = store.read_session(self.proj, self.sid)["messages"][-1]
        self.assertEqual(msg["content"], "")
        self.assertEqual(msg["thinking"], "只有思考没有答案")

    def test_session_uses_global_timeout(self):
        a1 = models.AgentConfig(name="A", type="fake")
        s, rec = make_session([a1], self.proj, self.sid, timeout=123)
        self.assertEqual(s.timeout, 123)

    def test_alternating_turns(self):
        a1 = models.AgentConfig(name="A", type="fake")
        a2 = models.AgentConfig(name="B", type="fake")
        s, rec = make_session([a1, a2], self.proj, self.sid, max_rounds=4)
        self.run_session(s)
        starts = [e["agent"] for e in rec.events if e.get("type") == "turn_start"]
        # max_rounds=4 即每个 agent 发言 4 次 → 共 8 次（A,B 交替两轮往返各两次）
        self.assertEqual(starts, ["A", "B", "A", "B", "A", "B", "A", "B"])

    def test_max_rounds_stops(self):
        a1 = models.AgentConfig(name="A", type="fake")
        a2 = models.AgentConfig(name="B", type="fake")
        s, rec = make_session([a1, a2], self.proj, self.sid, max_rounds=3)
        self.run_session(s)
        self.assertIn("会话结束", [e.get("msg") for e in rec.events if e.get("type") == "system"])
        # max_rounds=3 即每个 agent 发言 3 次 → 上限 6 次发言；不超过该上限
        turns = sum(1 for e in rec.events if e.get("type") == "turn_start")
        self.assertLessEqual(turns, 6)

    def test_terminate_converges(self):
        a1 = models.AgentConfig(name="A", type="fake", fake_terminate_round=2)
        a2 = models.AgentConfig(name="B", type="fake", fake_terminate_round=1)
        s, rec = make_session([a1, a2], self.proj, self.sid, max_rounds=10)
        self.run_session(s)
        self.assertTrue(any("收敛" in (e.get("msg") or "") for e in rec.events if e.get("type") == "system"))

    def test_terminate_floor_requires_two_full_rounds(self):
        # 最小轮数保护：N 个 agent 时 TERMINATE 仅在 round_i >= 2*N-1 后生效。
        # 2 agent → 阈值 3：第一个 agent 在 round 0 吐 TERMINATE 必须被忽略，
        # 辩论继续到至少两轮完整往返（A,B,A,B）后才可能收敛。
        a1 = models.AgentConfig(name="A", type="fake", fake_terminate_round=1)  # A 每轮都 TERMINATE
        a2 = models.AgentConfig(name="B", type="fake")
        # max_rounds=2 即每个 agent 发言 2 次（A,B,A,B 共 4 次）；收敛门槛 round_i>=3 在最后一轮才到，
        # 故 A 的 TERMINATE 全程被忽略、跑满两轮完整往返且不收敛。
        s, rec = make_session([a1, a2], self.proj, self.sid, max_rounds=2)
        self.run_session(s, task="需求讨论")
        starts = [e["agent"] for e in rec.events if e.get("type") == "turn_start"]
        self.assertIn("B", starts, "首轮 A 的 TERMINATE 应被忽略，B 仍要发言")
        self.assertEqual(starts, ["A", "B", "A", "B"], "阈值前的 TERMINATE 不应腰斩，应跑满两轮完整往返")
        sys = " ".join(e.get("msg", "") for e in rec.events if e.get("type") == "system")
        self.assertNotIn("收敛", sys, "未达阈值不应收敛，max_rounds=2 应严格跑满两轮")

    def test_stop_midway(self):
        a1 = models.AgentConfig(name="A", type="fake")
        a2 = models.AgentConfig(name="B", type="fake")
        s, rec = make_session([a1, a2], self.proj, self.sid, max_rounds=50)
        async def drive():
            task = asyncio.create_task(s.start("长任务"))
            await asyncio.sleep(0.15)
            s.stop()
            await task
        asyncio.run(drive())
        self.assertIn("会话结束", [e.get("msg") for e in rec.events if e.get("type") == "system"])
        turns = sum(1 for e in rec.events if e.get("type") == "turn_start")
        self.assertLess(turns, 50)

    def test_inject_inserts_human_turn(self):
        a1 = models.AgentConfig(name="A", type="fake")
        a2 = models.AgentConfig(name="B", type="fake")
        s, rec = make_session([a1, a2], self.proj, self.sid, max_rounds=10)
        async def drive():
            task = asyncio.create_task(s.start("初始任务"))
            await asyncio.sleep(0.15)
            await s.inject("人工：改需求为 X")
            await task
        asyncio.run(drive())
        self.assertTrue(any(e.get("type") == "human" for e in rec.events))

    def test_history_persisted(self):
        a1 = models.AgentConfig(name="A", type="fake")
        s, rec = make_session([a1], self.proj, self.sid, max_rounds=2)
        self.run_session(s)
        sess = store.read_session(self.proj, self.sid)
        hist = sess["messages"]
        # 用户消息也要入库（否则刷新后只剩 agent 的回复，看不到自己说了啥）
        self.assertEqual(hist[0]["speaker"], "human")
        self.assertEqual(hist[0]["content"], "任务")
        self.assertEqual(hist[1]["speaker"], "A")

    def test_agent_session_persisted_on_first_turn(self):
        # 真实 agent 首次开 CLI 会话后，cli_session_id 应落盘到 agent_sessions，
        # 以便点历史会话时用 --resume 恢复对应 agent 会话（1:1 持续会话）。
        # fake 不持 cli_session_id，这里手动注入以校验持久化链路。
        a1 = models.AgentConfig(name="A", type="fake")
        s, rec = make_session([a1], self.proj, self.sid, max_rounds=2)
        s.runtimes["A"].cli_session_id = "cli-sess-A-001"
        self.run_session(s)
        self.assertEqual(store.get_agent_sessions(self.proj, self.sid).get("A"), "cli-sess-A-001")

    def test_single_agent_mode_caps_at_one_round(self):
        # 单 agent 时轮次不生效：无论 max_rounds 设多大，只跑一次完整回答
        a1 = models.AgentConfig(name="A", type="fake")
        s, rec = make_session([a1], self.proj, self.sid, max_rounds=5)
        self.run_session(s)
        turns = [e for e in rec.events if e.get("type") == "turn_start"]
        self.assertEqual(len(turns), 1, "单 agent 模式应只跑一轮")
        sys_msgs = [e.get("msg", "") for e in rec.events if e.get("type") == "system"]
        self.assertTrue(any("单 agent 模式" in m for m in sys_msgs),
                        "应广播单 agent 模式提示")

    # ---- 单 agent = 对话模式 ----
    def test_single_agent_is_chat_mode(self):
        a1 = models.AgentConfig(name="A", type="fake")
        s, _ = make_session([a1], self.proj, self.sid, max_rounds=5)
        self.assertEqual(s.mode, "chat")

    def test_chat_mode_echoes_user_message(self):
        # 对话模式：用户消息要广播 human 事件（前端才有「我说的话」气泡）
        a1 = models.AgentConfig(name="A", type="fake")
        s, rec = make_session([a1], self.proj, self.sid)
        self.run_session(s, task="你好")
        humans = [e for e in rec.events if e.get("type") == "human"]
        self.assertEqual(len(humans), 1)
        self.assertEqual(humans[0]["text"], "你好")
        self.assertEqual(humans[0]["role"], "用户")

    def test_chat_mode_emits_run_end(self):
        a1 = models.AgentConfig(name="A", type="fake")
        s, rec = make_session([a1], self.proj, self.sid)
        self.run_session(s)
        ends = [e for e in rec.events if e.get("type") == "run_end"]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]["mode"], "chat")
        # 对话模式不该播「会话结束」——每条消息都是一轮对话，会话没结束
        self.assertNotIn("会话结束", [e.get("msg") for e in rec.events if e.get("type") == "system"])

    def test_chat_mode_sends_only_delta(self):
        # 状态式 agent：编排器只把用户本轮新消息作为 delta 发给 agent，
        # 不把「第一句」重新塞进 prompt（agent 自身 CLI 会话已持有完整历史）
        a1 = models.AgentConfig(name="A", type="fake")
        s1, _ = make_session([a1], self.proj, self.sid)
        self.run_session(s1, task="第一句")
        s2, _ = make_session([a1], self.proj, self.sid)
        self.assertGreaterEqual(len(s2.history), 2)
        prompt = s2._build_prompt(a1, "第二句")
        self.assertIn("第二句", prompt)
        self.assertNotIn("第一句", prompt, "状态式设计下不应把历史重新搬运进 prompt（token 浪费）")
        self.assertNotIn("end_discussion", prompt.replace("不要调用 end_discussion 工具", ""))

    def test_debate_mode_records_task_as_human(self):
        a1 = models.AgentConfig(name="A", type="fake")
        a2 = models.AgentConfig(name="B", type="fake")
        s, rec = make_session([a1, a2], self.proj, self.sid, max_rounds=2)
        self.run_session(s, task="设计登录")
        hist = store.read_session(self.proj, self.sid)["messages"]
        self.assertEqual(hist[0]["speaker"], "human")
        self.assertEqual(hist[0]["role"], "任务")

    def test_pause_actually_blocks(self):
        # 历史 bug：pause 用 Event.set()+wait()，已 set 时 wait 立即返回 -> 暂停形同虚设
        a1 = models.AgentConfig(name="A", type="fake")
        a2 = models.AgentConfig(name="B", type="fake")
        s, rec = make_session([a1, a2], self.proj, self.sid, max_rounds=50)

        async def drive():
            task = asyncio.create_task(s.start("长任务"))
            await asyncio.sleep(0.1)
            s.pause()
            await asyncio.sleep(0.05)
            n1 = sum(1 for e in rec.events if e.get("type") == "turn_start")
            await asyncio.sleep(0.25)
            n2 = sum(1 for e in rec.events if e.get("type") == "turn_start")
            s.stop()
            await task
            return n1, n2

        n1, n2 = asyncio.run(drive())
        self.assertLessEqual(n2 - n1, 1, "暂停后不应继续开新轮（允许当前轮跑完）")
        self.assertIn("已暂停", [e.get("msg") for e in rec.events if e.get("type") == "system"])

    # ---- 共识终止条件（辩论每轮提示词均含，且受最小轮数保护） ----
    def test_debate_prompt_includes_precise_consensus_condition(self):
        # 辩论模式提示词必须含「共识终止的精确条件」（后面的 agent 接受前面结论并总结才终止），
        # 且不再有旧的「首轮判定不可讨论就 TERMINATE」诱导指令。
        a1 = models.AgentConfig(name="A", type="fake")
        a2 = models.AgentConfig(name="B", type="fake")
        s, _ = make_session([a1, a2], self.proj, self.sid)
        p = s._build_prompt(a1, "设计登录")
        self.assertIn("end_discussion", p)
        self.assertIn("两轮完整往返", p)
        self.assertNotIn("首次发言特别要求", p, "已移除首轮「不可讨论就终止」诱导指令")
        self.assertNotIn("是否适合展开多 agent 讨论", p)

    def test_round0_terminate_is_ignored_before_floor(self):
        # 共识终止有最小轮数保护：至少两轮完整往返（2*N-1）后 TERMINATE 才生效。
        # 首轮（round 0）agent 吐 TERMINATE 不再直接终止，辩论应继续、第二个 agent 也会发言。
        a1 = models.AgentConfig(name="A", type="fake", fake_terminate_round=1)
        a2 = models.AgentConfig(name="B", type="fake")
        s, rec = make_session([a1, a2], self.proj, self.sid, max_rounds=10)
        self.run_session(s, task="聊聊天气")
        starts = [e["agent"] for e in rec.events if e.get("type") == "turn_start"]
        self.assertIn("B", starts, "首轮 TERMINATE 被忽略后，第二个 agent 应继续发言")
        sys_msgs = " ".join(e.get("msg", "") for e in rec.events if e.get("type") == "system")
        self.assertNotIn("不可讨论", sys_msgs, "已移除「首轮判定不可讨论即终止」机制")

    def test_system_messages_persisted(self):
        # 终止 / 结束类系统消息必须落库（speaker=system），否则辩论模式 run_end 重载后会消失、刷新即丢失
        a1 = models.AgentConfig(name="A", type="fake", fake_terminate_round=2)
        a2 = models.AgentConfig(name="B", type="fake", fake_terminate_round=1)
        s, rec = make_session([a1, a2], self.proj, self.sid, max_rounds=5)
        self.run_session(s, task="需求讨论")
        # 内存 history 末条应为系统消息
        self.assertEqual(s.history[-1]["speaker"], "system", "末条应为系统消息（会话结束）")
        # 磁盘持久化后也应可见（前端 run_end 重载读取的就是它）
        on_disk = store.read_session(self.proj, self.sid)
        msgs = on_disk["messages"]
        self.assertTrue(any(m["speaker"] == "system" for m in msgs), "磁盘消息应包含 system 条目")
        self.assertEqual(msgs[-1]["speaker"], "system", "磁盘末条也应为系统消息")
        self.assertIn("会话结束", [m["content"] for m in msgs if m["speaker"] == "system"])

    def test_consensus_condition_in_prompt_every_turn(self):
        # 共识终止的精确条件在每轮辩论提示词中都下发；旧的「首轮判定不可讨论」诱导指令已移除。
        a1 = models.AgentConfig(name="A", type="fake")
        a2 = models.AgentConfig(name="B", type="fake")
        s, _ = make_session([a1, a2], self.proj, self.sid)
        p = s._build_prompt(a1, "设计登录")
        self.assertIn("end_discussion", p)
        self.assertNotIn("首次发言特别要求", p)
        self.assertNotIn("是否适合展开多 agent 讨论", p)

    def test_conclusion_recorded_on_convergence(self):
        # 收敛（end_discussion 两阶段：B 提议 + A 确认）时应把 reason 作结论存盘，供续开新议题回传
        a1 = models.AgentConfig(name="A", type="fake", fake_terminate_round=5)  # A 第 5 轮（round4）发
        a2 = models.AgentConfig(name="B", type="fake", fake_terminate_round=4)  # B 第 4 轮（round3）提议
        s, rec = make_session([a1, a2], self.proj, self.sid, max_rounds=10)
        self.run_session(s, task="设计登录")
        ctx = store.get_discussion_context(self.proj, self.sid)
        self.assertTrue(ctx["conclusion"], "收敛后应记录结论")
        self.assertEqual(ctx["original_topic"], "设计登录", "应记录最初议题")
        sys = [e.get("msg", "") for e in rec.events if e.get("type") == "system"]
        self.assertTrue(any("结论" in m for m in sys), "收敛系统消息应带结论")
        self.assertTrue(any("end_discussion" in m for m in sys), "收敛系统消息应注明 end_discussion 达成")

    def test_end_discussion_two_phase_requires_confirmation(self):
        # 两阶段确认：只有一方提议（无对方确认）不收敛；双方都发才收敛
        # 轮次：round0 A、round1 B、round2 A、round3 B（B 提议，≥floor=3）、round4 A（确认）
        a1 = models.AgentConfig(name="A", type="fake", fake_terminate_round=5)
        a2 = models.AgentConfig(name="B", type="fake", fake_terminate_round=4)
        s, rec = make_session([a1, a2], self.proj, self.sid, max_rounds=10)
        self.run_session(s, task="讨论方案")
        starts = [e["agent"] for e in rec.events if e.get("type") == "turn_start"]
        # B 在 round3 提议后，A 在 round4 确认 → 共 5 次发言后收敛
        self.assertEqual(starts, ["A", "B", "A", "B", "A"], "提议后需对方确认才收敛")
        sys = " ".join(e.get("msg", "") for e in rec.events if e.get("type") == "system")
        self.assertIn("end_discussion", sys)

    def test_end_discussion_proposal_alone_does_not_converge(self):
        # 单方提议（对方从不调用）：不收敛，继续讨论到上限
        a1 = models.AgentConfig(name="A", type="fake")  # A 从不发
        a2 = models.AgentConfig(name="B", type="fake", fake_terminate_round=4)  # B round3 起每轮提议
        s, rec = make_session([a1, a2], self.proj, self.sid, max_rounds=3)
        self.run_session(s, task="讨论方案")
        starts = [e["agent"] for e in rec.events if e.get("type") == "turn_start"]
        # max_rounds=3 → 每 agent 3 次 = 6 次发言，跑满不收敛
        self.assertEqual(starts, ["A", "B", "A", "B", "A", "B"], "单方提议无确认应跑满上限")
        sys = " ".join(e.get("msg", "") for e in rec.events if e.get("type") == "system")
        self.assertNotIn("end_discussion", sys)
        self.assertIn("会话结束", sys)

    def test_new_topic_prompt_does_not_reinject_context(self):
        # 状态式 agent：续开新议题时不再手动回传「上轮结论 + 最初议题」
        # （agent 自身 CLI 会话已记住历次讨论）；辩论提示词含共识条件但不含旧上下文块。
        a1 = models.AgentConfig(name="A", type="fake", fake_terminate_round=5)
        a2 = models.AgentConfig(name="B", type="fake", fake_terminate_round=4)
        s1, _ = make_session([a1, a2], self.proj, self.sid, max_rounds=10)
        self.run_session(s1, task="设计登录")
        ctx1 = store.get_discussion_context(self.proj, self.sid)
        self.assertTrue(ctx1["conclusion"])
        s2, _ = make_session([a1, a2], self.proj, self.sid, max_rounds=10)
        p = s2._build_prompt(a1, "新议题：支付集成")
        self.assertIn("end_discussion", p, "新议题提示词应含共识条件")
        self.assertNotIn("上一轮讨论", p, "状态式设计下不手动回传上轮结论块（agent 自身会话已记住）")
        self.assertNotIn(ctx1["conclusion"], p, "不应把上轮结论文本重新塞进 prompt（token 浪费）")
        self.assertNotIn("设计登录", p, "不应把最初议题重新塞进 prompt（agent 自身会话已记住）")

    def test_multi_agent_uses_max_rounds(self):
        # 多 agent 仍按 max_rounds 交替
        a1 = models.AgentConfig(name="A", type="fake")
        a2 = models.AgentConfig(name="B", type="fake")
        s, rec = make_session([a1, a2], self.proj, self.sid, max_rounds=2)
        self.run_session(s)
        # max_rounds=2 即每个 agent 发言 2 次 → 共 4 次发言（A,B 交替两轮往返）
        turns = [e for e in rec.events if e.get("type") == "turn_start"]
        self.assertEqual(len(turns), 4, "双 agent 应严格跑满每个 agent 2 轮（共 4 次发言）")

    def test_prompt_log_recorded(self):
        # 每轮发给 agent 的完整提示词应落库；辩论提示词含「共识终止的精确条件」
        a1 = models.AgentConfig(name="A", type="fake", role="架构师")
        a2 = models.AgentConfig(name="B", type="fake", role="审查者")
        s, rec = make_session([a1, a2], self.proj, self.sid, max_rounds=2)
        self.run_session(s, task="评审这个设计")
        log = store.read_prompt_log(self.proj, self.sid)
        self.assertTrue(log, "应记录到提示词日志")
        # 条数 = 发言次数；max_rounds=2 即每个 agent 发言 2 次 → 共 4 条（A,B,A,B）
        self.assertEqual(len(log), 4)
        first = log[0]
        self.assertTrue(first.get("first_turn"), "首轮 first_turn 标志应为 True")
        self.assertIn("end_discussion", first["prompt"], "辩论首轮提示词应含共识条件")
        self.assertIn("评审这个设计", first["prompt"], "提示词应含任务文本")
        self.assertEqual(first["agent"], "A")
        # first_turn 标志仅标记每场讨论的开启轮（用于记录/排查），共识条件在所有轮都下发
        self.assertFalse(log[1].get("first_turn"))


    def test_agent_error_aborts_debate(self):
        # agent 执行失败（error 事件）：不得把错误当议题继续讨论，会话直接中止；
        # 失败轮不落 agent 发言（避免错误文本/空回答进历史），落 system 错误消息
        a1 = models.AgentConfig(name="A", type="fake")
        a2 = models.AgentConfig(name="B", type="fake")
        s, rec = make_session([a1, a2], self.proj, self.sid, max_rounds=10)
        s.runtimes["A"] = StubRuntime([("error", "Ollama 挂了")])
        self.run_session(s, task="设计登录")
        starts = [e["agent"] for e in rec.events if e.get("type") == "turn_start"]
        self.assertEqual(starts, ["A"], "A 失败后不应继续讨论（B 不应发言）")
        msgs = store.read_session(self.proj, self.sid)["messages"]
        a_msgs = [m for m in msgs if m["speaker"] == "A"]
        self.assertEqual(a_msgs, [], "失败轮不应把错误当 A 的发言落盘")
        self.assertTrue(any(m["speaker"] == "system" and "执行出错" in m["content"] for m in msgs),
                        "应落 system 错误消息说明原因")

    def test_claude_error_text_treated_as_failure(self):
        # claude 外壳把 API 失败渲染成普通文本（如 API Error: ...）：编排器必须识别并中止，
        # 不能把它当正常发言落盘、再喂给下一个 agent（历史 bug：两 agent 基于报错信息讨论）
        a1 = models.AgentConfig(name="A", type="fake")
        a2 = models.AgentConfig(name="B", type="fake")
        s, rec = make_session([a1, a2], self.proj, self.sid, max_rounds=10)
        s.runtimes["A"] = StubRuntime([
            ("text", "API Error: API returned an empty or malformed response (HTTP 200)")])
        self.run_session(s, task="男人和女人谁更聪明")
        starts = [e["agent"] for e in rec.events if e.get("type") == "turn_start"]
        self.assertEqual(starts, ["A"], "错误文本轮后不应继续讨论")
        msgs = store.read_session(self.proj, self.sid)["messages"]
        a_msgs = [m for m in msgs if m["speaker"] == "A"]
        self.assertEqual(a_msgs, [], "错误文本不得作为 A 的正常发言落盘")
        self.assertTrue(any(m["speaker"] == "system" and "执行出错" in m["content"] for m in msgs))

    def test_agent_error_chat_mode_records_system(self):
        # 单 agent 对话模式：执行失败同样不落 agent 发言、落 system 错误消息
        a1 = models.AgentConfig(name="A", type="fake")
        s, rec = make_session([a1], self.proj, self.sid)
        s.runtimes["A"] = StubRuntime([("error", "超时")])
        self.run_session(s, task="你好")
        msgs = store.read_session(self.proj, self.sid)["messages"]
        self.assertTrue(any(m["speaker"] == "system" and "执行出错" in m["content"] for m in msgs))
        self.assertEqual([m for m in msgs if m["speaker"] == "A"], [])


if __name__ == "__main__":
    unittest.main()
