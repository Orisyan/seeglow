# -*- coding: utf-8 -*-
"""v1.1.0 期末冲刺全链路测试（TestClient）：上传→docs.json 落盘→融合包/押题卷→ask 带资料上下文。"""
import io
import json as J
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TMP = Path(tempfile.gettempdir())

from fastapi.testclient import TestClient


def make_pptx(path: Path, tag: str):
    slide = ('<?xml version="1.0"?><p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
             'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree>{}</p:spTree></p:cSld></p:sld>')
    shape = '<p:sp><p:txBody><a:p><a:r><a:t>{}</a:t></a:r></a:p></p:txBody></p:sp>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("ppt/slides/slide1.xml",
                   slide.format(shape.format(f"{tag}重点：泰勒公式展开到三阶") + shape.format(f"考试必考，见{tag}第1页")))


def main():
    os.environ["SEEGLOW_AUTH_SECRET"] = "unit-test-secret"
    from seeglow import web, pro_web

    web.SITE_PAID_MODE = True
    outdir = TMP / "seeglow_study_test"
    outdir.mkdir(exist_ok=True)
    note_name = "20260827-测试视频.md"
    (outdir / note_name).write_text("# 测试视频\n\n## 核心要点\n- 泰勒公式 三阶展开", encoding="utf-8")
    (outdir / (note_name + ".ctx.json")).write_text(J.dumps({
        "title": "测试视频", "url": "", "items": [
            {"start": 60, "end": 120, "text": "这节讲泰勒公式怎么展开"}]}, ensure_ascii=False), encoding="utf-8")

    # 重定向输出目录与官方配置
    web.load_config = lambda: {"api_base": "http://official", "api_key": "sk-official",
                               "model": "m", "temperature": 0.3, "sessdata": "",
                               "output_dir": str(outdir), "vision": False}

    calls = []

    class FakeLLM:
        model = "fake"

        def __init__(self, *a, **k):
            pass

        def chat(self, messages):
            calls.append(messages[-1]["content"])
            if "逐字转写" in str(messages[-1]["content"])[:200]:
                return "Fake OCR transcript page"
            return "## 🎯 重点度调整说明\nok（来自 PPT 第1页）"

    from seeglow import summarize

    summarize.LLMClient = FakeLLM
    c = TestClient(web.app)

    # 激活票据（需先注册登录）
    r = c.post("/api/auth/register", json={"username": "studyuser", "password": "secret123"})
    sess = {"X-SeeGlow-Session": r.json()["token"]}
    code = pro_web.make_web_code("lifetime")
    d = c.post("/api/pro/code", json={"code": code}, headers=sess).json()
    assert d["ok"], d
    headers = {**sess, "X-SeeGlow-Auth": J.dumps({k: d[k] for k in ("kind", "uid", "expiry", "sig")})}

    # 1. 上传教师 PPT（清掉上次运行残留的同名记录）
    make_pptx(TMP / "t1.pptx", "老师强调")
    docs0 = web._load_docs(note_name)
    docs0["files"] = [x for x in docs0.get("files", []) if x["name"] != "期末重点PPT"]
    docs0["blocks"] = [b for b in docs0.get("blocks", []) if b.get("source") != "期末重点PPT"]
    web._save_docs(note_name, docs0)
    with open(TMP / "t1.pptx", "rb") as f:
        r = c.post("/api/upload_doc", files={"file": ("期末重点PPT.pptx", f)},
                   data={"note_file": note_name}, headers=headers)
    j = r.json()
    assert j.get("ok") and j["block_count"] >= 1 and j["chars"] > 20, j
    print(f"[ok] upload_doc：{j['block_count']} 块 {j['chars']} 字")

    docs = J.loads((outdir / (note_name + ".docs.json")).read_text(encoding="utf-8"))
    assert any("泰勒公式" in b["text"] for b in docs["blocks"])
    print("[ok] .docs.json 已落盘且包含 PPT 文本")

    # 2. 备考融合包
    r = c.post("/api/study_pack", json={"file": note_name, "mode": "fuse"}, headers=headers)
    d = r.json()
    assert d["ok"] and "对照表" in d["text"] or "重点度" in d["text"], d
    assert (outdir / d["output_file"]).exists()
    sent = calls[-1]
    assert "泰勒公式" in sent and "[期末重点PPT · PPT第1页]" in sent, sent[:300]
    print("[ok] 融合包生成，提示词含视频笔记+文档内容+来源标注")

    # 3. 押题卷
    r = c.post("/api/study_pack", json={"file": note_name, "mode": "exam", "count": 5}, headers=headers)
    d = r.json()
    assert d["ok"] and "押题卷" in d["output_file"], d
    print("[ok] 押题卷生成:", d["output_file"])

    # 4. ask 现在能命中资料条目
    import requests as rq

    class FakeAsk(rq.Response):
        pass

    # 直接用本地 LLMClient mock（ask 内部实例化）
    orig = summarize.LLMClient
    summarize.LLMClient = FakeLLM
    try:
        r = c.post("/api/ask", json={"file": note_name, "question": "往年题考过什么"}, headers=headers)
        assert r.status_code == 200, (r.status_code, r.text[:150])
        prompt = calls[-1]
        assert "[资料·期末重点PPT" in prompt, prompt[:400]
        print("[ok] ask() 的上下文已包含教师资料")
    finally:
        summarize.LLMClient = orig

    # 5. 未授权拦截（匿名 → 401 要求登录）
    r = c.post("/api/study_pack", json={"file": note_name, "mode": "fuse"})
    assert r.status_code == 401, r.status_code
    r = c.post("/api/upload_doc", files={"file": ("x.pptx", b"x")},
               data={"note_file": note_name})
    assert r.status_code == 401, r.status_code
    print("[ok] 未登录访问期末冲刺接口 → 401")

    # 6. 独立模式：不依赖任何视频笔记，直接传资料出知识梳理/押题卷
    calls.clear()
    make_pptx(TMP / "t2.pptx", "高数期末")
    with open(TMP / "t2.pptx", "rb") as f:
        r = c.post("/api/study_pack_standalone",
                   files=[("files", ("高数重点.pptx", f))],
                   data={"mode": "outline"}, headers=headers)
    j = r.json()
    assert r.status_code == 200 and j.get("ok"), j
    assert "知识梳理" in j["output_file"], j["output_file"]
    assert len(j["text"]) > 0 and "资料" in "".join(calls[-1]), j["text"][:100]
    sent = calls[-1]
    assert "高数重点" in sent and "知识梳理" in sent, sent[:300]
    out_note = outdir / j["output_file"]
    assert out_note.exists(), "独立笔记未落盘"
    dctx = J.loads((outdir / (j["output_file"] + ".ctx.json")).read_text(encoding="utf-8"))
    assert dctx["items"] == []  # 空 ctx，配合 docs 供 ask 使用
    print(f"[ok] standalone 知识梳理：{j['output_file']}")

    # 真 PDF（fitz 生成，文字层）
    import fitz

    pdfp = TMP / "t3.pdf"
    d3 = fitz.open()
    pg = d3.new_page()
    pg.insert_text((72, 72), "Past exam paper: Q1 calculus. Filler text to pass text-layer threshold easily.")
    pg.insert_text((72, 120), "More filler content so that page counts as text layer for the parser check.")
    d3.save(str(pdfp))
    d3.close()
    with open(pdfp, "rb") as f:
        r = c.post("/api/study_pack_standalone",
                   files=[("files", ("往年卷.pdf", f))],
                   data={"mode": "exam", "count": "6"}, headers=headers)
    j = r.json()
    assert r.status_code == 200 and j.get("ok") and "押题卷" in j["output_file"], j
    print("[ok] standalone 押题卷：", j["output_file"])

    # 7a. 损坏 PDF → 400 而非 500
    r = c.post("/api/study_pack_standalone",
               files=[("files", ("坏卷.pdf", io.BytesIO(b"%PDF-1.4 fake")))],
               data={"mode": "exam"}, headers=headers)
    assert r.status_code == 400, (r.status_code, r.text[:120])
    print("[ok] 损坏 PDF → 友好 400")

    # 7c. 双卷模式：综合题卷 + 标准模板卷 → 提示词分别进入 content/template 槽
    calls.clear()
    make_pptx(TMP / "t4.pptx", "综合题卷")      # 内容来源
    tpl_txt = TMP / "t5.txt"
    tpl_txt.write_text("标准模板：一、选择题10题×3分；二、填空题5题×4分；三、简答题4题×8分", encoding="utf-8")
    with open(TMP / "t4.pptx", "rb") as f, open(tpl_txt, "rb") as g:
        r = c.post("/api/study_pack_standalone",
                   files=[("files", ("综合题卷.pptx", f)),
                          ("template_files", ("标准模板卷.txt", g))],
                   data={"mode": "exam", "count": "19"}, headers=headers)
    j = r.json()
    assert r.status_code == 200 and j.get("ok"), j
    sent = calls[-1]
    assert "标准模板卷" in sent and "10题×3分" in sent, sent[:400]
    assert "综合题卷" in sent and "泰勒公式" in sent, sent[:400]
    assert "结构硬性对齐" in sent, sent[:400]
    # 有模板卷时：goal 明确“以模板卷为准”，不再强调总题数
    assert "以模板卷为准" in sent and "19 题" not in sent, sent[:400]
    print("[ok] 双卷出题：内容与结构分开注入，题量被忽略、以模板卷为准")

    # 7d. 无模板卷 → 使用 NO_TPL 结构规则，题量生效
    calls.clear()
    with open(TMP / "t4.pptx", "rb") as f:
        r = c.post("/api/study_pack_standalone",
                   files=[("files", ("综合题卷.pptx", f))],
                   data={"mode": "exam"}, headers=headers)
    sent = calls[-1]
    assert "模仿【综合题卷/资料】中试卷自身的结构" in sent, sent[:300]
    assert "8 题" in sent, sent[:300]
    print("[ok] 无模板卷 → 沿用综合题卷自身结构，题量生效")

    # 7e. docs.json 记录 role（服务端命名规则：<笔记全名>.docs.json）
    last_note = outdir / (j["output_file"] + ".docs.json")
    dctx = J.loads(last_note.read_text(encoding="utf-8"))
    assert all(b.get("role") == "content" for b in dctx["blocks"]), dctx["files"]
    print("[ok] blocks 携带 role 标记")

    # 7b. 空文件列表 → 400/422（仍需授权头先过门禁）
    r = c.post("/api/study_pack_standalone", files=[], data={"mode": "outline"}, headers=headers)
    assert r.status_code in (400, 422), r.status_code
    print("[ok] 空资料列表 →", r.status_code)

    print("\nSTUDY PACK E2E PASSED")


if __name__ == "__main__":
    main()
