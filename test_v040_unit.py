# -*- coding: utf-8 -*-
"""v0.4.0 单元测试：语法/风格模板/链接解析/合集识别/弹幕解析"""
import ast
import sys

sys.path.insert(0, r"C:\Users\Administrator\Desktop\SeeGlow\开源发布版")

# 1. 语法检查所有改动文件
for f in (r"seeglow\bilibili.py", r"seeglow\summarize.py", r"seeglow\pipeline.py",
          r"seeglow\web.py", r"..\订阅封装版\pro_routes.py"):
    ast.parse(open(f, encoding="utf-8").read())
    print("syntax OK:", f)

# HTML 基本完整性（括号配对；对话框数量随版本增加，只验证开闭配对）
html = open(r"seeglow\static\index.html", encoding="utf-8").read()
assert html.count("<dialog") == html.count("</dialog>") >= 3, (html.count("<dialog"), html.count("</dialog>"))
for fn in ("showColPicker", "runBatch", "uploadFile", "loadDanmaku", "exportOPML",
           "makeFlashcards", "openFeed", "checkFeed", "tsLinkify", "colSelectAll"):
    assert fn in html, f"缺函数 {fn}"
print("html functions OK")

# 2. 风格模板
from seeglow.summarize import STYLE_GUIDES, style_hint, SINGLE_PROMPT, REDUCE_PROMPT, AUDIO_SINGLE_PROMPT, REDUCE_AUDIO_PROMPT

assert {"general", "study", "tutorial", "meeting", "review"} <= set(STYLE_GUIDES), set(STYLE_GUIDES)
assert style_hint("general") == ""
assert "术语表" in style_hint("study") and "操作步骤" in style_hint("tutorial")
assert "待办" in style_hint("meeting") and "名场面" in style_hint("review")
for p in (SINGLE_PROMPT, REDUCE_PROMPT, AUDIO_SINGLE_PROMPT, REDUCE_AUDIO_PROMPT):
    p.format(meta="m", transcript="t", chunk="c", joined="j", style_hint="", title="x", start="0:00", end="1:00")
print("style prompts OK")

# 3. 收藏夹/合集 URL 解析
from seeglow import bilibili as B

assert B.parse_collection_url("https://www.bilibili.com/medialist/detail/ml1319100012") == {"kind": "fav", "media_id": 1319100012}
assert B.parse_collection_url("https://space.bilibili.com/123/favlist?fid=456") == {"kind": "fav", "media_id": 456}
assert B.parse_collection_url("https://space.bilibili.com/123/channel/collectiondetail?sid=789") == {"kind": "season", "sid": 789, "mid": 0}
assert B.parse_collection_url("https://www.bilibili.com/medialist/play/123/456") == {"kind": "season", "mid": 123, "sid": 456}
assert B.parse_collection_url("https://www.bilibili.com/video/BV1xx411c7mD") is None
print("collection url parse OK")

# 4. protobuf 弹幕解析（构造假数据：elem{progress=12000, content="测试"}）


def pb_varint(n):
    out = b""
    while True:
        x = n & 0x7F
        n >>= 7
        out += bytes([x | (0x80 if n else 0)])
        if not n:
            return out


def pb_bytes(field, b):
    return bytes([field << 3 | 2]) + pb_varint(len(b)) + b


def pb_str(field, s):
    return pb_bytes(field, s.encode())


def pb_int(field, n):
    return bytes([field << 3 | 0]) + pb_varint(n)


elem = pb_int(2, 12000) + pb_str(7, "测试弹幕")
msg = pb_bytes(1, elem)
parsed = B._pb_parse_danmaku(msg)
assert parsed == [(12.0, "测试弹幕")], parsed
print("danmaku pb parser OK:", parsed)

# 5. wbi 签名参数结构
p = B.wbi_sign({"mid": 123})
assert "wts" in p and len(p["w_rid"]) == 32
print("wbi sign OK")

print("\nUNIT TESTS PASSED")
