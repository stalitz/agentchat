"""terminate_signal_mcp 帧解析测试（JSONL + Content-Length 双格式）。"""
import io
import json
import unittest
from unittest import mock

from backend import terminate_signal_mcp as mcp


class FakeStdin:
    """模拟 sys.stdin：提供 .buffer（BytesIO）。"""
    def __init__(self, data: bytes):
        self.buffer = io.BytesIO(data)


class TestReadFrame(unittest.TestCase):
    def test_jsonl_line(self):
        # claude-code 2.1.x 的 stdio MCP 用 JSONL：每行一个 JSON（实测根因——
        # 之前只支持 Content-Length 帧，JSONL 被当 header 行解析失败）
        line = '{"jsonrpc": "2.0", "method": "initialize", "id": 0}\n'
        with mock.patch("sys.stdin", FakeStdin(line.encode())):
            raw = mcp._read_frame()
        self.assertEqual(json.loads(raw), json.loads(line))

    def test_content_length_frame(self):
        # 标准 MCP 客户端：Content-Length 帧（header + 空行 + body）仍支持
        body = '{"jsonrpc": "2.0", "id": 1, "result": {}}'
        frame = f"Content-Length: {len(body)}\r\n\r\n{body}".encode()
        with mock.patch("sys.stdin", FakeStdin(frame)):
            raw = mcp._read_frame()
        self.assertEqual(json.loads(raw), json.loads(body))

    def test_eof_returns_none(self):
        with mock.patch("sys.stdin", FakeStdin(b"")):
            self.assertIsNone(mcp._read_frame())

    def test_write_frame_is_jsonl(self):
        # 响应与请求对称：JSONL（每行一个 JSON），claude 按行读
        out = io.BytesIO()
        with mock.patch("sys.stdout", io.TextIOWrapper(out)):
            pass  # 占位：_write_frame 直接用 sys.stdout.buffer
        with mock.patch("sys.stdout", FakeStdin(b"")):  # 复用 FakeStdin 仅取 buffer 容器
            pass
        # 直接测：_write_frame 写 sys.stdout.buffer
        buf = io.BytesIO()
        with mock.patch("sys.stdout") as m_stdout:
            m_stdout.buffer = buf
            mcp._write_frame({"jsonrpc": "2.0", "id": 1, "result": {}})
        data = buf.getvalue().decode()
        self.assertTrue(data.startswith("{"), "响应应为 JSONL（裸 JSON 行）")
        self.assertNotIn("Content-Length", data, "不应再输出 Content-Length 帧头")
        self.assertTrue(data.endswith("\n"))

    def test_handle_call_receipt_neutral_not_presuming_verdict(self):
        # 回执必须**中性**、不预设判定结果：MCP 是无状态确认通道，不知道编排层会判
        # rejected/proposed/confirmed；旧文案断言「终止尚未生效/轮数不足」会在 confirmed
        # （成功终止）场景误导模型——gemma 把自己成功的确认读成「被拒」（「资本主义会
        # 灭亡吗」实测：confirmed 已收敛，gemma 却回「因轮次限制拒绝了我们的终止请求」）。
        resp = mcp._handle_call({
            "name": "end_discussion",
            "arguments": {"reason": "双方已达成一致"},
        })
        text = resp["content"][0]["text"]
        self.assertIn("工具回执", text)
        self.assertIn("调用已收到", text)
        self.assertIn("终止是否生效由系统判定", text, "判定权归编排层，回执不得越权断言")
        self.assertIn("简体中文继续参与讨论", text, "强指令仍须保留（防英文待命）")
        # 不得预设具体判定结果（无论 rejected / confirmed 都用同一份回执）
        self.assertNotIn("终止尚未生效", text, "confirmed 场景会误导（成功确认被读成被拒）")
        self.assertNotIn("轮数不足", text, "不得断言具体被拒原因（由编排层下一轮注入点名）")
        self.assertNotIn("对方是否确认无关", text)
        self.assertFalse(resp.get("isError"), "回执本身不算错误")
        self.assertNotIn("The conversation has concluded", text, "不得出现英文待命语")


class TestHitlToolNaming(unittest.TestCase):
    """HITL 工具命名（2026-09-20 由 request_human_input 改名为 ask_human_input）。

    改名的原因：旧名与 codex 内建的 `request_user_input` 太接近，codex 会把调用归一化到
    内建工具，被 headless「Default mode」门禁拒掉（实测 v9 通勤场工具完全不可用）。
    锁定两件事：① 对外**只暴露新名**；② 旧名仍**能调通**（历史会话 / 提示词残留）。
    """

    def _tools_list(self):
        resp = None
        def _cap(frame):
            nonlocal resp
            resp = frame
            raise SystemExit  # 抓一帧即退出循环
        with mock.patch.object(mcp, "_write_frame", _cap), \
                mock.patch.object(mcp, "_read_frame",
                                  return_value='{"jsonrpc":"2.0","id":1,"method":"tools/list"}'):
            try:
                mcp.main()
            except SystemExit:
                pass
        return {t["name"]: t for t in resp["result"]["tools"]}

    def test_exposed_name_is_new_only(self):
        tools = self._tools_list()
        self.assertIn("ask_human_input", tools, "必须暴露新名")
        self.assertNotIn("request_human_input", tools, "旧名不得再暴露给模型")
        self.assertIn("end_discussion", tools)
        # 参数 schema 仍是标准 MCP inputSchema（question: string）——与 codex 内建
        # request_user_input 的对象数组参数不同，也是防误调的一环
        schema = tools["ask_human_input"]["inputSchema"]
        self.assertEqual(schema["properties"]["question"]["type"], "string")
        self.assertEqual(schema["required"], ["question"])

    def test_legacy_name_still_accepted(self):
        for name in ("ask_human_input", "request_human_input"):
            resp = mcp._handle_call({"name": name, "arguments": {"question": "选哪个？"}})
            self.assertFalse(resp.get("isError"), f"{name} 应能调通")
            text = resp["content"][0]["text"]
            self.assertIn("选哪个？", text)
            self.assertIn("暂停并等待人工介入", text)
        # 回执里点名的工具名统一用新名（不再向模型强化旧名）
        resp = mcp._handle_call({"name": "request_human_input", "arguments": {"question": "q"}})
        self.assertIn("ask_human_input", resp["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
