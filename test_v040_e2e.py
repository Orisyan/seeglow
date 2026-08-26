# -*- coding: utf-8 -*-
"""v0.4.0 端到端测试（真实B站 + 真实LLM）：
1. parse：短视频识别 + 弹幕
2. start(style=study)：真实总结
3. 下载同视频音频 → start_file（本地文件直听）
4. start_batch：批量 2 支
5. flashcards：闪卡生成
"""
import sys
import time

import requests

sys.path.insert(0, r"C:\Users\Administrator\Desktop\SeeGlow\开源发布版")
BASE = "http://127.0.0.1:8766"
NP = {"http": None, "https": None}


def wait_up():
    for _ in range(40):
        try:
            requests.get(BASE + "/api/history", timeout=2, proxies=NP)
            return True
        except Exception:
            time.sleep(1)
    raise SystemExit("服务未启动")


def wait_task(tid, timeout=600):
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout:
        r = requests.get(BASE + "/api/task/" + tid, timeout=10, proxies=NP)
        last = r.json()
        print(f"    [{last['stage']}] {last['percent']:5.1f}% {last['message'][:50]}")
        if last["status"] in ("done", "error", "stopped"):
            return last
        time.sleep(5)
    raise SystemExit(f"任务超时: {last}")


wait_up()
print("== 服务就绪 ==")

# 挑一支最短的热门视频（<240s 保证单块直听）
from seeglow import bilibili as B

pop = B._get("https://api.bilibili.com/x/web-interface/popular", {"ps": 20})
short = min(pop["list"], key=lambda v: v["duration"])
bvid = short["bvid"]
print(f"测试视频: {short['title'][:36]} ({short['duration']}s) {bvid}")

# ---------- 1. parse + 弹幕 ----------
d = requests.post(BASE + "/api/parse", json={"url": bvid}, timeout=30, proxies=NP).json()
assert d["bvid"] == bvid, d
print("parse OK:", d["title"][:30], "| 分P:", len(d["pages"]), "| 合集:", bool(d.get("season")))

dm = requests.get(BASE + f"/api/danmaku?bvid={bvid}&cid={d['pages'][0]['cid']}&duration={d['duration']}",
                  timeout=60, proxies=NP).json()
print("danmaku:", "OK 共%d条 热点%s" % (dm.get("total", 0), dm.get("hot", [])[:2]) if dm.get("ok") else dm.get("msg"))

# ---------- 2. 真实总结（study 风格）----------
r = requests.post(BASE + "/api/start", json={"url": bvid, "style": "study"}, timeout=15, proxies=NP).json()
print("== start(study) ==")
t = wait_task(r["task_id"], 900)
assert t["status"] == "done", t.get("error")
res = t["result"]
print("总结完成:", res["output_file"], "| 来源:", res["source"])
md = open(r"C:\Users\Administrator\Desktop\SeeGlow\开源发布版\拾光" + "\\" + res["output_file"], encoding="utf-8").read()
has_terms = "术语表" in md
print("study 风格包含术语表:", has_terms, "| 含时间线:", "内容时间线" in md)

# ---------- 3. 本地文件：下载同视频音频 → start_file ----------
from pathlib import Path

audio_url = B.get_audio_url(bvid, d["pages"][0]["cid"])
tmp = Path(r"C:\Users\Administrator\Desktop\SeeGlow\开源发布版") / "local_test.m4s"
B.download_audio(audio_url, tmp)
print("本地音频:", tmp.stat().st_size // 1024, "KB")

with open(tmp, "rb") as f:
    r = requests.post(BASE + "/api/start_file", files={"file": ("学习方法测试.m4s", f)},
                      data={"style": "general"}, timeout=300, proxies=NP).json()
print("== start_file ==")
t = wait_task(r["task_id"], 900)
assert t["status"] == "done", t.get("error")
res2 = t["result"]
print("本地文件总结完成:", res2["output_file"], "| 来源:", res2["source"])
tmp.unlink()

# ---------- 4. 批量（挑2支最短的）----------
two = sorted(pop["list"], key=lambda v: v["duration"])[:2]
r = requests.post(BASE + "/api/start_batch",
                  json={"items": [{"bvid": v["bvid"], "title": v["title"]} for v in two],
                        "style": "general"}, timeout=15, proxies=NP).json()
print(f"== start_batch({r['count']}支) ==")
t = wait_task(r["task_id"], 1800)
assert t["status"] == "done", t.get("error")
res3 = t["result"]
print("批量完成:", res3["output_file"], "| 成功:", len(res3["files"]), "| 失败:", len(res3["failures"]))
for f in res3["files"]:
    print("   -", f["title"][:30])

# ---------- 5. 闪卡 ----------
r = requests.post(BASE + "/api/flashcards", json={"file": res["output_file"], "count": 6},
                  timeout=300, proxies=NP).json()
print("== flashcards ==")
print("生成", len(r["cards"]), "张；示例:", r["cards"][0]["q"][:40])
assert len(r["cards"]) >= 3

print("\nE2E ALL PASSED")
