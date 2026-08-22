"""完整流水线：解析 → 字幕/转写 → 总结 → 落盘 Markdown。"""

from __future__ import annotations

import datetime
import tempfile
import time
from pathlib import Path

from . import bilibili, summarize as sz_mod
from .config import load_config


def run_pipeline(url: str, options: dict | None, progress_cb, stop_check=None) -> dict:
    cfg = load_config()
    opts = options or {}

    def report(stage: str, percent: float, message: str = ""):
        progress_cb(
            {
                "stage": stage,
                "percent": round(max(0.0, min(percent, 100.0)), 1),
                "message": message,
            }
        )

    report("parse", 2, "解析链接…")
    bvid, page = bilibili.parse_bvid(url)

    report("info", 6, "获取视频信息…")
    info = bilibili.get_video_info(bvid, cfg.get("sessdata", ""))
    cid = bilibili.get_cid(info, page)
    title = info["title"]
    page_part = next((p["part"] for p in info["pages"] if p["page"] == page and p.get("part")), "")
    video_url = f"https://www.bilibili.com/video/{bvid}?p={page}"

    # 并行预取：B站字幕 与 音频流地址 同时请求（无字幕视频可省一次网络往返）
    from concurrent.futures import ThreadPoolExecutor

    segments, sub_lans = None, []
    sub_error = None
    audio_url = None
    report("subtitle", 12, "尝试获取B站字幕…")
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_sub = ex.submit(bilibili.get_subtitle_segments, bvid, cid, cfg.get("sessdata", ""))
        f_url = ex.submit(bilibili.get_audio_url, bvid, cid, cfg.get("sessdata", ""))
        try:
            segments, sub_lans = f_sub.result()
        except Exception as e:
            sub_error = str(e)
        if not segments:
            try:
                audio_url = f_url.result()
            except Exception:
                audio_url = None

    client = sz_mod.LLMClient(
        cfg.get("api_base"), cfg.get("api_key"), cfg.get("model"), cfg.get("temperature", 0.3)
    )
    meta = f"- 标题：{title}\n- UP主：{info['owner']}\n- 链接：{video_url}"
    if page_part:
        meta += f"\n- 分P：{page_part}"

    transcript_text = None
    summary_md = None
    src_label = ""
    try:
        if segments:
            # 第一优先级：B站字幕 → 文本总结
            src_label = "B站字幕"
            report("summarize", 58, "准备调用大模型…")
            summary_md = sz_mod.summarize_transcript(
                bilibili.segments_to_text(segments),
                meta,
                client,
                progress_cb=lambda p, msg="": report("summarize", 58 + p * 37, msg),
            )
        else:
            reason = "无可用字幕" + (f"（{sub_error}）" if sub_error else "")
            report("download", 20, f"{reason}，下载音频中…")
            if audio_url is None:
                audio_url = bilibili.get_audio_url(bvid, cid, cfg.get("sessdata", ""))
            tmp_audio = Path(tempfile.gettempdir()) / f"seeglow_{bvid}_{int(time.time())}.m4s"
            bilibili.download_audio(
                audio_url,
                tmp_audio,
                cfg.get("sessdata", ""),
                progress_cb=lambda p: report("download", 20 + p * 8, f"下载音频 {int(p * 100)}%"),
                stop_check=stop_check,
            )

            # 无字幕 → AI 直接听音频写总结
            try:
                report("omni", 28, f"让 {client.model} 直接听音频…")
                summary_md = sz_mod.summarize_audio_direct(
                    tmp_audio,
                    title,
                    client,
                    progress_cb=lambda p, msg="": report("omni", 28 + p * 62, msg),
                    stop_check=stop_check,
                    notice_cb=lambda m: report("omni", 30, m),
                )
                src_label = f"AI直听·{client.model}"
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

    report("save", 96, "保存结果…")
    out_dir = Path(cfg.get("output_dir"))
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{datetime.date.today():%Y%m%d}-{bilibili.safe_filename(title)}.md"
    out_path = out_dir / fname

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    header = (
        f"# {title}\n\n"
        f"> 来源：[{video_url}]({video_url})  \n"
        f"> UP主：{info['owner']}  \n"
        f"> 时长：{bilibili.fmt_ts(info['duration'])} · 内容来源：{src_label} · "
        f"由 [拾光 SeeGlow](https://github.com/seeglow) 生成于 {now}\n\n"
    )
    footer = (
        ""
        if transcript_text is None
        else f"\n\n---\n\n<details>\n<summary>附：完整逐字稿</summary>\n\n{transcript_text}\n\n</details>\n"
    )
    out_path.write_text(header + summary_md + footer, encoding="utf-8")

    report("done", 100, "完成")
    return {
        "title": title,
        "author": info["owner"],
        "url": video_url,
        "duration": bilibili.fmt_ts(info["duration"]),
        "source": src_label,
        "segments": len(segments) if segments else 0,
        "summary_md": summary_md,
        "output_file": fname,
    }
