"""完整流水线：解析 → 字幕/AI直听 → 总结 → 落盘 Markdown。

v0.2.0 起支持B站多P视频：
- 单P模式：总结指定分P（URL 带 ?p= 或由前端/CLI 指定）
- 全部P模式：逐P处理，合并为一份带章节的笔记
"""

from __future__ import annotations

import datetime
import tempfile
import time
from pathlib import Path

from . import bilibili, summarize as sz_mod
from .config import load_config


def _process_page(bvid, page_no, cid, part, title, owner, client, cfg, prog, stop_check):
    """处理单个分P，返回 {"src", "md", "segs"}。prog(stage, frac 0~1, msg)。"""
    sess = cfg.get("sessdata", "")

    segments = None
    sub_error = ""
    prog("subtitle", 0.02, "尝试获取B站字幕…")
    try:
        segments, _lan = bilibili.get_subtitle_segments(bvid, cid, sess)
    except Exception as e:
        sub_error = str(e)

    if segments:
        prog("subtitle", 0.12, f"已获取B站字幕（{len(segments)} 条）")
        meta = f"- 标题：{title}\n- UP主：{owner}"
        if part:
            meta += f"\n- 分P：P{page_no} {part}"
        prog("summarize", 0.55, "准备调用大模型…")
        md = sz_mod.summarize_transcript(
            bilibili.segments_to_text(segments),
            meta,
            client,
            progress_cb=lambda p, msg="": prog("summarize", 0.55 + p * 0.42, msg),
        )
        return {"src": "B站字幕", "md": md, "segs": len(segments)}

    reason = "无可用字幕" + (f"（{sub_error}）" if sub_error else "")
    prog("download", 0.16, f"{reason}，下载音频中…")
    audio_url = bilibili.get_audio_url(bvid, cid, sess)
    tmp_audio = Path(tempfile.gettempdir()) / f"seeglow_{bvid}_p{page_no}_{int(time.time())}.m4s"
    try:
        bilibili.download_audio(
            audio_url,
            tmp_audio,
            sess,
            progress_cb=lambda p: prog("download", 0.16 + p * 0.09, f"下载音频 {int(p * 100)}%"),
            stop_check=stop_check,
        )
        prog("omni", 0.26, f"让 {client.model} 直接听音频…")
        listen_title = title + (f"（P{page_no}）" if part else "")
        md = sz_mod.summarize_audio_direct(
            tmp_audio,
            listen_title,
            client,
            progress_cb=lambda p, msg="": prog("omni", 0.28 + p * 0.66, msg),
            stop_check=stop_check,
            notice_cb=lambda m: prog("omni", 0.3, m),
        )
        return {"src": f"AI直听·{client.model}", "md": md, "segs": 0}
    except Exception as e:
        if "取消" in str(e):
            raise
        raise RuntimeError(
            f"AI 直听音频失败：{str(e)[:150]}。"
            f"请在设置中确认模型支持音频输入（推荐 Qwen3-Omni），"
            f"或配置 SESSDATA 以使用B站字幕。"
        ) from e
    finally:
        if tmp_audio and tmp_audio.exists():
            try:
                tmp_audio.unlink()
            except OSError:
                pass


def run_pipeline(url: str, options: dict | None, progress_cb, stop_check=None) -> dict:
    cfg = load_config()
    opts = options or {}
    client = sz_mod.LLMClient(
        cfg.get("api_base"), cfg.get("api_key"), cfg.get("model"), cfg.get("temperature", 0.3)
    )

    def report(stage: str, percent: float, message: str = ""):
        progress_cb(
            {
                "stage": stage,
                "percent": round(max(0.0, min(percent, 100.0)), 1),
                "message": message,
            }
        )

    report("parse", 2, "解析链接…")
    bvid, url_page = bilibili.parse_bvid(url)

    report("info", 6, "获取视频信息…")
    info = bilibili.get_video_info(bvid, cfg.get("sessdata", ""))
    pages = info["pages"] or [{"page": 1, "cid": info["cid_first"], "part": ""}]
    title = info["title"]
    owner = info["owner"]

    all_pages = bool(opts.get("all_pages")) and len(pages) > 1
    if all_pages:
        target_pages = pages
    else:
        page_no = int(opts.get("page") or url_page or 1)
        match = next((p for p in pages if p["page"] == page_no), None)
        target_pages = [match or pages[0]]

    n = len(target_pages)
    results = []
    for idx, pg in enumerate(target_pages):
        base = 8 + idx * (86.0 / n)
        span = 86.0 / n

        def prog(stage, frac, msg="", _b=base, _s=span):
            report(stage, _b + max(0.0, min(frac, 1.0)) * _s * 0.97, msg)

        r = _process_page(
            bvid=bvid,
            page_no=pg["page"],
            cid=pg["cid"],
            part=pg.get("part", ""),
            title=title,
            owner=owner,
            client=client,
            cfg=cfg,
            prog=prog,
            stop_check=stop_check,
        )
        results.append((pg, r))

    # ---------- 组装 Markdown ----------
    report("save", 96, "保存结果…")
    out_dir = Path(cfg.get("output_dir"))
    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    today = f"{datetime.date.today():%Y%m%d}"

    if all_pages:
        src_set = sorted({r["src"] for _, r in results})
        src_label = f"全部{n}个分P · " + "+".join(src_set)[:60]
        video_url_base = f"https://www.bilibili.com/video/{bvid}"
        header = (
            f"# {title}\n\n"
            f"> 来源：[https://www.bilibili.com/video/{bvid}]({video_url_base})  \n"
            f"> UP主：{owner} · 共 {n} 个分P  \n"
            f"> 内容来源：{src_label} · 由 [拾光 SeeGlow](https://github.com/Orisyan/seeglow) 生成于 {now}\n"
        )
        body_parts = [header]
        for pg, r in results:
            part_name = pg.get("part") or f"P{pg['page']}"
            body_parts.append(f"\n---\n\n## P{pg['page']} · {part_name}\n\n{r['md']}\n")
        content = "".join(body_parts)
        summary_md = "\n\n".join(r["md"] for _, r in results)
        fname = f"{today}-{bilibili.safe_filename(title)}_全P.md"
    else:
        pg, r = results[0]
        page_no = pg["page"]
        video_url = f"https://www.bilibili.com/video/{bvid}?p={page_no}"
        src_label = r["src"]
        part_note = f" · 分P：P{page_no} {pg['part']}" if (pg.get("part") and len(pages) > 1) else ""
        header = (
            f"# {title}{part_note}\n\n"
            f"> 来源：[{video_url}]({video_url})  \n"
            f"> UP主：{owner}  \n"
            f"> 时长：{bilibili.fmt_ts(info['duration'])} · 内容来源：{src_label} · "
            f"由 [拾光 SeeGlow](https://github.com/Orisyan/seeglow) 生成于 {now}\n\n"
        )
        content = header + r["md"] + "\n"
        summary_md = r["md"]
        suffix = f"_P{page_no}" if len(pages) > 1 else ""
        fname = f"{today}-{bilibili.safe_filename(title)}{suffix}.md"

    out_path = out_dir / fname
    out_path.write_text(content, encoding="utf-8")

    report("done", 100, "完成")
    return {
        "title": title,
        "author": owner,
        "url": f"https://www.bilibili.com/video/{bvid}",
        "duration": bilibili.fmt_ts(info["duration"]),
        "source": src_label,
        "pages_total": len(pages),
        "pages_done": [pg["page"] for pg, _ in results],
        "summary_md": summary_md,
        "output_file": fname,
    }
