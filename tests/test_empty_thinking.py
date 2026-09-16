"""空思考块（`thinking:""`）清洗回归（2026-09-16，用户报「思考块内容 0 字需要跳过」）。

claude transcript 里**存在 `thinking:""` 的回合**（模型没吐推理），解析后原样进 blocks，
界面出现「💭 xx 的思考过程 0 字」的空折叠块（无信息量）。修法两层：
- 解析侧：`_normalize_claude_blocks` 统一丢弃空白 thinking 块；`_parse_claude_transcript`
  的原生 thinking 块与网关哨兵切分两处也不再收空文本。
- 前端：`renderHistoryEntry` 两条渲染路径（blocks / 无 blocks 回退）先 trim 再渲染
  ⇒ 已落盘的旧数据无需清洗即可正常显示（见 tests/test_frontend_render.js）。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import executor


def test_normalize_claude_blocks_drops_empty_thinking():
    out = executor._normalize_claude_blocks([
        {"type": "thinking", "thinking": ""},
        {"type": "thinking", "thinking": "   "},
        {"type": "text", "text": "正文"},
    ])
    assert out == [{"type": "text", "text": "正文"}], out


def test_claude_transcript_skips_empty_thinking_block():
    proj = tempfile.mkdtemp(prefix="agentchat_emptyth_", dir="/tmp")
    p = os.path.join(proj, "t.jsonl")
    events = [
        {"type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "你好"}]}},
        {"type": "assistant", "timestamp": "2026-01-01T00:00:01Z",
         "message": {"role": "assistant", "content": [
             {"type": "thinking", "thinking": ""},
             {"type": "text", "text": "回答"},
         ]}},
    ]
    with open(p, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")

    turns = executor._parse_claude_transcript(p)
    assert len(turns) == 1, turns
    t = turns[0]
    assert t["content"] == "回答"
    assert t["thinking"] == ""           # 空推理不落 thinking
    assert [b["type"] for b in t["blocks"]] == ["text"], t["blocks"]
