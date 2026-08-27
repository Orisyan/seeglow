# -*- coding: utf-8 -*-
"""期末资料解析：教师 PPT / 往年卷 → 统一 doc blocks，供融合提示词与问答使用。

支持格式与策略：
  .pptx / .docx   —— zip + ElementTree 直接抽 XML 文本（零第三方依赖）
  .pdf            —— PyMuPDF：先试文字层；每页几乎无文字则视为扫描件，
                     渲染页面图交给多模态模型转写（复用画面理解能力）
  .txt / .md      —— 原样读取

解析结果为 blocks 列表：
  [{"kind": "slide"|"page"|"para", "page": n, "text": ..., "source": 文件名}]
落盘 <笔记名>.docs.json，独立于字幕时间轴的 ctx.json。
"""

from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

MAX_BLOCKS_TEXT = 60000       # 单文档截断上限（字符）
MAX_PDF_PAGES = 80            # 扫描 PDF 最大页数（转写成本控制）
PAGE_CHARS_SCANNED = 60       # 文字版判定阈值：单页文字少于该值视为扫描页

_NS_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_NS_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _clean(text: str) -> str:
    return re.sub(r"[ \t\u3000]+", " ", (text or "").replace("\x0b", " ").strip())


def _detect_kind(name: str) -> str | None:
    ext = name.lower().rsplit(".", 1)[-1]
    return {"pptx": "pptx", "docx": "docx", "pdf": "pdf",
            "txt": "txt", "md": "txt"}.get(ext)


# ---------------- pptx / docx（OOXML 零依赖） ----------------

def _parse_pptx(path: Path) -> list[dict]:
    """按幻灯片顺序抽取文字。表格单元格、组合形状里的文本都能拿到。"""
    out = []
    with zipfile.ZipFile(path) as z:
        slides = sorted(
            [n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)],
            key=lambda n: int(re.search(r"(\d+)", n).group(1)),
        )
        for idx, name in enumerate(slides, 1):
            root = ET.fromstring(z.read(name))
            texts = [_clean(t.text or "") for t in root.iter(f"{_NS_A}t") if _clean(t.text or "")]
            if texts:
                out.append({"kind": "slide", "page": idx, "text": "\n".join(texts),
                            "source": path.name})
    return out


def _parse_docx(path: Path) -> list[dict]:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    paras = []
    for p in root.iter(f"{_NS_W}p"):
        text = _clean("".join(t.text or "" for t in p.iter(f"{_NS_W}t")))
        if text:
            paras.append(text)
    # 按 ~800 字切段，便于模型分块
    blocks, buf = [], ""
    for t in paras:
        if len(buf) + len(t) > 800 and buf:
            blocks.append(buf)
            buf = ""
        buf = f"{buf}\n{t}".strip()
    if buf:
        blocks.append(buf)
    src = path.name
    return [{"kind": "para", "page": i + 1, "text": b, "source": src}
            for i, b in enumerate(blocks)]


def parse_ooxml(path: Path) -> list[dict]:
    kind = _detect_kind(str(path))
    if kind == "pptx":
        return _parse_pptx(path)
    if kind == "docx":
        return _parse_docx(path)
    raise ValueError(f"不支持的 OOXML 类型：{path}")


# ---------------- pdf（PyMuPDF；缺失时给出明确指引） ----------------

def parse_pdf(path: Path, vision_transcribe=None, notice=None):
    """返回 (blocks, ocr_used)。vision_transcribe(list[(页码,PNG字节)], 文件名)->[str] 由调用方注入。"""
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz
        except ImportError:
            raise RuntimeError(
                "PDF 支持需要安装 PyMuPDF：pip install PyMuPDF（或仅用 PPTX/DOCX/图片）")

    def say(m):
        if notice:
            notice(m)

    doc = fitz.open(str(path))
    try:
        total_pages = min(len(doc), MAX_PDF_PAGES)
        truncated = len(doc) > MAX_PDF_PAGES
        text_blocks, scan_jobs = [], []
        for i in range(total_pages):
            page = doc[i]
            txt = _clean(page.get_text())
            if len(txt) >= PAGE_CHARS_SCANNED:
                text_blocks.append({"kind": "page", "page": i + 1, "text": txt,
                                    "source": path.name})
            else:
                pix = page.get_pixmap(dpi=110)
                scan_jobs.append((i + 1, pix.tobytes("png")))

        ocr_used = False
        if scan_jobs:
            say(f"{len(scan_jobs)} 页是扫描图片，交给 AI 视觉转写…")
            if vision_transcribe is None:
                raise RuntimeError("扫描版 PDF 需要视觉模型转写，但当前环境未提供该能力")
            for page_no, text in zip([j[0] for j in scan_jobs],
                                     vision_transcribe(scan_jobs, path.name)):
                text = _clean(text)
                if text:
                    text_blocks.append({"kind": "page", "page": page_no,
                                        "text": f"[扫描识别] {text}", "source": path.name})
                    ocr_used = True
            if not any(b["kind"] == "page" and b["text"].startswith("[扫描识别]") for b in text_blocks):
                pass

        text_blocks.sort(key=lambda b: b["page"])
        if truncated:
            text_blocks.append({"kind": "note", "page": 0,
                                "text": f"[注意] 原文件共 {len(doc)} 页，仅纳入前 {total_pages} 页。",
                                "source": path.name})
        return text_blocks, ocr_used
    finally:
        doc.close()


# ---------------- 入口 ----------------

def load_document(path: str | Path, vision_transcribe=None, notice=None) -> list[dict]:
    """任意支持的文件 → doc blocks。超长时整体截断到 MAX_BLOCKS_TEXT 字符。"""
    p = Path(path)
    kind = _detect_kind(str(p)) or ("img" if p.suffix.lower().lstrip(".") in
                                    {"png", "jpg", "jpeg", "webp"} else None)
    if kind == "txt":
        raw = p.read_text(encoding="utf-8", errors="ignore")
        blocks = [{"kind": "para", "page": i + 1, "text": b, "source": p.name}
                  for i, b in enumerate(_split_paras(raw))]
    elif kind == "pptx":
        blocks = _parse_pptx(p)
    elif kind == "docx":
        blocks = _parse_docx(p)
    elif kind == "pdf":
        blocks, _ocr = parse_pdf(p, vision_transcribe=vision_transcribe, notice=notice)
    elif kind == "img":
        if vision_transcribe is None:
            raise RuntimeError("图片类试卷需要视觉模型转写能力")
        texts = vision_transcribe([(1, p.read_bytes())], p.name)
        blocks = [{"kind": "page", "page": 1,
                   "text": "[扫描识别] " + _clean(t), "source": p.name}
                  for t in texts if _clean(t)]
    else:
        raise ValueError(f"暂不支持该资料格式：{p.suffix}（支持 pptx/docx/pdf/txt/md/图片）")

    # 总量截断
    total, kept = 0, []
    for b in blocks:
        if total + len(b["text"]) > MAX_BLOCKS_TEXT:
            b = {**b, "text": b["text"][: max(MAX_BLOCKS_TEXT - total, 0)]}
            kept.append(b)
            break
        total += len(b["text"])
        kept.append(b)
    if not kept:
        raise ValueError("未能从该文件提取出任何文字（可能是空文件或纯图片型 PPT）")
    return kept


def _split_paras(raw: str, size: int = 800) -> list[str]:
    lines = [l.strip() for l in raw.splitlines()]
    out, buf = [], ""
    for ln in lines:
        if len(buf) + len(ln) > size and buf:
            out.append(buf)
            buf = ""
        buf = f"{buf}\n{ln}".strip()
    if buf:
        out.append(buf)
    return out


def render_blocks(blocks: list[dict], budget: int = 30000) -> str:
    """把多个文件的 blocks 拼成给模型的上下文文本（带来源标注）。"""
    parts, used = [], 0
    cur_src = None
    for b in blocks:
        tag = {"slide": f"PPT第{b['page']}页",
               "page": f"第{b['page']}页",
               "para": f"段落{b['page']}",
               "note": "说明"}.get(b.get("kind", ""), "")
        line_prefix = f"[{b['source']} · {tag}]" if tag else f"[{b['source']}]"
        piece = f"{line_prefix}\n{b['text']}"
        if used + len(piece) > budget:
            break
        parts.append(piece)
        used += len(piece)
    return "\n\n".join(parts)


def allowed_ext(suffix: str) -> bool:
    return suffix.lower().lstrip(".") in {
        "pptx", "docx", "pdf", "txt", "md", "png", "jpg", "jpeg", "webp"}
