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

    def test_handle_call_rejection_reason_unambiguous(self):
        # 被拒回执必须点名「轮次不足」是首要原因、并明确「与对方确认无关」——
        # gemma 曾把未生效归因于「对方未确认」，实际是未达最小轮数被拒（1+1=2 会话实测）
        resp = mcp._handle_call({
            "name": "end_discussion",
            "arguments": {"reason": "双方已达成一致"},
        })
        text = resp["content"][0]["text"]
        self.assertIn("终止尚未生效", text)
        self.assertIn("轮数不足", text, "必须点名被拒真实原因（未达最小轮数）")
        self.assertIn("与对方是否确认无关", text, "必须排除「对方未确认」的误归因")
        self.assertIn("简体中文继续参与讨论", text)
        self.assertFalse(resp.get("isError"), "回执本身不算错误")
        self.assertNotIn("The conversation has concluded", text, "不得出现英文待命语")


if __name__ == "__main__":
    unittest.main()
