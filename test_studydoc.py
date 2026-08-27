# -*- coding: utf-8 -*-
"""验证 studydoc 解析：程序化构造 pptx/docx/txt/pdf（文字版+扫描版）。"""
import os
import sys
import zipfile
from pathlib import Path

import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
TMP = Path(tempfile.gettempdir())

from seeglow.studydoc import load_document, render_blocks, allowed_ext


def make_pptx(path: Path):
    slide_xml = (
        '<?xml version="1.0"?><p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<p:cSld><p:spTree>{shapes}</p:spTree></p:cSld></p:sld>'
    )
    shape = ('<p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        for i in range(1, 4):
            texts = f"第{i}页 标题" + f"考点{'一二三'[i-1]}"
            body = shape.format(text=texts) + shape.format(text=f"知识点 K{i}：example {i}")
            z.writestr(f"ppt/slides/slide{i}.xml", slide_xml.format(shapes=body))
    return path


def make_docx(path: Path):
    w = '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{}</w:body></w:document>'
    para = '<w:p><w:r><w:t>{}</w:t></w:r></w:p>'
    paras = "".join(para.format("往年真题卷段落 " + "长文本内容占位。" * 40) for _ in range(3))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", w.format(paras))
    return path


def make_pdf_text(path: Path):
    import fitz

    doc = fitz.open()
    for i in range(2):
        page = doc.new_page()
        page.insert_text((72, 72), f"Sample exam page {i+1}: Question about calculus K{i+1}.")
        page.insert_text((72, 120), "Filler text so this counts as a text layer page, "
                                   "with plenty of characters to exceed the scanned threshold easily.")
    doc.save(str(path))
    doc.close()
    return path


def main():
    assert allowed_ext(".pptx") and allowed_ext(".pdf") and not allowed_ext(".exe")

    pptx = make_pptx(TMP / "sg_test.pptx")
    blocks = load_document(pptx)
    assert len(blocks) == 3 and blocks[0]["kind"] == "slide" and "K1" in blocks[0]["text"], blocks[:2]
    print(f"[ok] pptx 解析 {len(blocks)} 页")

    docx = make_docx(TMP / "sg_test.docx")
    db = load_document(docx)
    assert db and all("真题卷段落" in b["text"] or b["kind"] == "para" for b in db)
    print(f"[ok] docx 解析 {len(db)} 段")

    pdf = make_pdf_text(TMP / "sg_test.pdf")
    pb, ocr = load_document(pdf), None
    assert len(pb) == 2 and pb[0]["kind"] == "page" and "calculus" in pb[0]["text"]
    print(f"[ok] 文字版 PDF 解析 {len(pb)} 页（未触发 OCR）")

    # 扫描 PDF：空白页 → 应走 vision 转写桩
    import fitz

    scan = TMP / "sg_scan.pdf"
    d2 = fitz.open()
    p2 = d2.new_page()  # 空页 → 无文字层
    d2.save(str(scan))
    d2.close()

    def fake_vision(jobs, src):
        return [f"Fake OCR of page {n}: quiz item Q{n}" for n, _ in jobs]

    sb, used = None, None
    from seeglow import studydoc

    sb_blocks, ocr_used = studydoc.parse_pdf(scan, vision_transcribe=fake_vision)
    assert sb_blocks and sb_blocks[0]["text"].startswith("[扫描识别]") and "Q1" in sb_blocks[0]["text"]
    print(f"[ok] 扫描 PDF 走视觉转写（ocr_used={ocr_used}）")

    txt = TMP / "sg_test.txt"
    txt.write_text("\n".join(f"line {i}" for i in range(50)), encoding="utf-8")
    tb = load_document(txt)
    assert tb, "txt 失败"
    rendered = render_blocks(blocks)
    assert "PPT第1页" in rendered
    print("[ok] txt 与 render_blocks 正常")

    for f in (pptx, docx, pdf, scan, txt):
        f.unlink(missing_ok=True)
    print("\nSTUDYDOC TESTS PASSED")


if __name__ == "__main__":
    main()
