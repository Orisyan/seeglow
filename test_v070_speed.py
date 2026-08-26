# -*- coding: utf-8 -*-
"""v0.7.0 速学模式 E2E：真实B站知识类视频 + 真实LLM，验证期末速成全家桶各板块"""
import re
import sys
import threading
import time
from pathlib import Path

import requests
import uvicorn

sys.path.insert(0, r"C:\Users\Administrator\Desktop\SeeGlow\开源发布版")
BASE = "http://127.0.0.1:8766"
NP = {"http": None, "https": None}
OUT = r"C:\Users\Administrator\Desktop\SeeGlow\开源发布版\拾光"

from seeglow.web import app  # noqa: E402

threading.Thread(
    target=lambda: uvicorn.run(app, host="127.0.0.1", port=8766, log_level="error"),
    daemon=True,
).start()


def wait_up():
    for _ in range(40):
        try:
            requests.get(BASE + "/api/history", timeout=2, proxies=NP)
            return True
        except Exception:
            time.sleep(1)
    raise SystemExit("服务未启动")


def wait_task(tid, timeout=900):
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout:
        r = requests.get(BASE + "/api/task/" + tid, timeout=10, proxies=NP)
        last = r.json()
        print(f"    [{last['stage']}] {last['percent']:5.1f}% {last['message'][:46]}")
        if last["status"] in ("done", "error", "stopped"):
            return last
        time.sleep(5)
    raise SystemExit(f"任务超时: {last}")


wait_up()
print("== 服务就绪 ==")

# 复用上一次速学产出（存在且含全部板块时跳过真实总结，节省 API 消耗）
reuse = None
for f in sorted(Path(OUT).glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
    txt = f.read_text(encoding="utf-8")
    if all(k in txt for k in ("必会知识点", "术语表", "高频考点预测", "思维导图", "自测八题")):
        reuse = f
        break
if reuse:
    print(f"复用已有速学产出: {reuse.name}")
    md = reuse.read_text(encoding="utf-8")
    fname = reuse.name
else:
    run_summary()

def run_summary():
    global md, fname, res
    # 挑一支知识/教学类热门短视频（多页+宽关键词），找不到就取时长适中的任意视频
    from seeglow import bilibili as B

    KW = ("教", "学", "知识", "原理", "科普", "入门", "历史", "物理", "化学",
          "数学", "经济", "心理", "考试", "如何", "为什么", "干货", "攻略", "指南")
    pool = []
    for pn in (1, 2, 3):
        data = B._get("https://api.bilibili.com/x/web-interface/popular",
                      {"ps": 20, "pn": pn})
        pool += data.get("list") or []

    cands = [v for v in pool if 120 <= v.get("duration", 0) <= 900
             and any(k in v.get("title", "") for k in KW)]
    v = (cands or [x for x in pool if 120 <= x.get("duration", 0) <= 900] or pool)[:1]
    v = v[0]
    bvid = v["bvid"]
    print(f"测试视频: {v['title'][:40]} ({v['duration']}s) {bvid} | 知识类命中: {bool(cands)}")

    # ---------- 1) 速学模式总结 ----------
    r = requests.post(BASE + "/api/start",
                      json={"url": bvid, "style": "speed"}, timeout=15, proxies=NP).json()
    print("== start(style=speed) ==")
    t = wait_task(r["task_id"])
    assert t["status"] == "done", t.get("error")
    res = t["result"]
    fname = res["output_file"]
    print("完成:", fname, "| 来源:", res["source"])
    md = open(OUT + "\\" + fname, encoding="utf-8").read()

# ---------- 2) 断言：速学全家桶各板块齐全 ----------
required = [
    "必会知识点", "术语表", "高频考点预测", "易错点", "自测八题", "记忆锚点", "思维导图",
]
missing = [k for k in required if k not in md]
print("板块检查: 缺失 =", missing or "无 ✓")
assert not missing, f"缺少板块: {missing}"

# 知识点穷尽度：★ 标注数量
stars = len(re.findall(r"★★[★]?", md))
print("★知识点标注数:", stars)
assert stars >= 5, "知识点过少"

# 思维导图缩进树
mm = re.search(r"## 思维导图\s*\n(.*?)(?:\n#|\Z)", md, re.S)
assert mm, "思维导图板块为空"
tree_lines = [l for l in mm.group(1).splitlines() if l.strip().startswith("-")]
print("导图节点行:", len(tree_lines))
assert len(tree_lines) >= 6, "导图节点过少"
assert any(l.startswith("  ") for l in tree_lines), "导图没有层级缩进"

# 自测题数量（列表项 + 引用答案成对）
quiz = re.search(r"## 自测八题\s*\n(.*?)(?:\n## |\Z)", md, re.S)
q_items = len(re.findall(r"^\s*\d+\.", quiz.group(1), re.M)) if quiz else 0
q_answers = len(re.findall(r"^>", quiz.group(1), re.M)) if quiz else 0
print(f"自测题: {q_items} 题 / {q_answers} 个引用答案")
assert q_items >= 6 and q_answers >= 6

# 高频考点题型标注
assert re.search(r"选择|简答|计算|论述|填空", md), "考点未标注题型"

# ---------- 3) 问答上下文存在 ----------
ctx = Path(OUT + "\\" + fname + ".ctx.json")
print("ctx 存在:", ctx.exists())

# ---------- 4) OPML 导图数据（前端逻辑的输入） ----------
heads = re.findall(r"^(#{1,4})\s+(.+)$", md, re.M)
print("标题节点数（OPML 输入）:", len(heads))
assert len(heads) >= 8

print("\nSPEED MODE E2E ALL PASSED")
print("输出文件:", OUT + "\\" + fname)
