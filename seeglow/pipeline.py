"""完整流水线：解析 → 字幕/AI直听 → 总结 → 落盘 Markdown。

v0.2.0 起支持B站多P视频：
- 单P模式：总结指定分P（URL 带 ?p= 或由前端/CLI 指定）
- 全部P模式：逐P处理，合并为一份带章节的笔记
"""

from __future__ import annotations

import datetime
import json
import tempfile
import time
from pathlib import Path

from . import bilibili, summarize as sz_mod
from .config import load_config


def _process_page(bvid, page_no, cid, part, title, owner, client, cfg, prog, stop_check, style="general"):
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
            style=style,
            stop_check=stop_check,
        )
        return {
            "src": "B站字幕",
            "md": md,
            "segs": len(segments),
            "ctx": [
                {"start": s["start"], "end": s["end"], "text": s["text"]} for s in segments
            ],
        }

    reason = "无可用字幕" + (f"（{sub_error}）" if sub_error else "")
    prog("download", 0.16, f"{reason}，下载音频中…")
    audio_url = bilibili.get_audio_url(bvid, cid, sess)
    tmp_audio = Path(tempfile.gettempdir()) / f"seeglow_{bvid}_p{page_no}_{time.time_ns()}.m4s"
    # 画面理解开关：开启时顺带取最低清视频流，供模型看题目/PPT
    video_src = None
    if str(cfg.get("vision") or "").lower() in ("1", "true", "on", "yes") or cfg.get("vision") is True:
        try:
            video_src = bilibili.get_video_url(bvid, cid, sess)
        except Exception:
            video_src = None  # 取不到画面流就纯听，不影响总结
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
        res = sz_mod.summarize_audio_direct(
            tmp_audio,
            listen_title,
            client,
            progress_cb=lambda p, msg="": prog("omni", 0.28 + p * 0.66, msg),
            stop_check=stop_check,
            notice_cb=lambda m: prog("omni", 0.3, m),
            style=style,
            video_src=video_src,
        )
        return {
            "src": f"AI直听·{client.model}",
            "md": res["md"],
            "segs": 0,
            "ctx": res["ctx"],
        }
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


def _base_cfg(cfg_override: dict | None = None) -> dict:
    """公共部署（BYOK）时用请求带来的凭据覆盖服务端 config.json。"""
    cfg = load_config()
    if cfg_override:
        for k in ("api_base", "api_key", "model", "temperature"):
            v = cfg_override.get(k)
            if isinstance(v, (int, float)) and k == "temperature":
                if 0 <= float(v) <= 1:
                    cfg[k] = float(v)
            elif isinstance(v, str) and v.strip():
                cfg[k] = v.strip()
    return cfg


def run_pipeline(url: str, options: dict | None, progress_cb, stop_check=None, cfg_override: dict | None = None) -> dict:
    cfg = _base_cfg(cfg_override)
    opts = options or {}
    style = opts.get("style") or "general"
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
            style=style,
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

    # 保存问答上下文（供 AI 问答使用）
    ctx_items = []
    for pg, r in results:
        pfx = f"P{pg['page']} {pg.get('part','')} · " if len(results) > 1 else ""
        for it in r.get("ctx", []):
            ctx_items.append({"start": it["start"], "end": it["end"], "text": f"{pfx}{it['text']}"})
    ctx_items.sort(key=lambda x: x["start"])
    (out_dir / f"{fname}.ctx.json").write_text(
        json.dumps(
            {"title": title, "url": f"https://www.bilibili.com/video/{bvid}", "items": ctx_items},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report("done", 100, "完成")
    first_pg = results[0][0]
    return {
        "title": title,
        "author": owner,
        "url": f"https://www.bilibili.com/video/{bvid}",
        "bvid": bvid,
        "cid": first_pg["cid"],          # 供前端拉取弹幕高能时间轴
        "page": first_pg["page"],
        "duration": bilibili.fmt_ts(info["duration"]),
        "duration_sec": info["duration"],
        "source": src_label,
        "pages_total": len(pages),
        "pages_done": [pg["page"] for pg, _ in results],
        "summary_md": summary_md,
        "output_file": fname,
    }


def run_file_pipeline(file_path, display_title: str, style: str, progress_cb, stop_check=None, cfg_override: dict | None = None) -> dict:
    """总结本地音视频文件：解码 → AI 直听 → 落盘 Markdown。"""
    from pathlib import Path

    cfg = _base_cfg(cfg_override)
    client = sz_mod.LLMClient(
        cfg.get("api_base"), cfg.get("api_key"), cfg.get("model"), cfg.get("temperature", 0.3)
    )

    def report(stage: str, percent: float, message: str = ""):
        progress_cb({"stage": stage, "percent": round(max(0.0, min(percent, 100.0)), 1), "message": message})

    title = display_title or Path(file_path).stem or "本地文件"
    report("omni", 5, "解码本地文件…")
    # 本地文件本身就是视频：开启画面理解时直接从文件抽帧
    vision_on = str(cfg.get("vision") or "").lower() in ("1", "true", "on", "yes") or cfg.get("vision") is True
    try:
        res = sz_mod.summarize_audio_direct(
            str(file_path), title, client,
            progress_cb=lambda p, msg="": report("omni", 10 + p * 84, msg),
            stop_check=stop_check,
            notice_cb=lambda m: report("omni", 12, m),
            style=style,
            video_src=(str(file_path) if vision_on else None),
        )
    except Exception as e:
        if "取消" in str(e):
            raise
        raise RuntimeError(f"AI 直听本地文件失败：{str(e)[:150]}") from e

    # 落盘
    report("save", 96, "保存结果…")
    out_dir = Path(cfg.get("output_dir"))
    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    today = f"{datetime.date.today():%Y%m%d}"
    fname = f"{today}-本地-{bilibili.safe_filename(title)}.md"
    header = (
        f"# {title}\n\n"
        f"> 来源：本地文件 · 由 [拾光 SeeGlow](https://github.com/Orisyan/seeglow) 生成于 {now}\n\n"
    )
    (out_dir / fname).write_text(header + res["md"] + "\n", encoding="utf-8")
    (out_dir / f"{fname}.ctx.json").write_text(
        json.dumps({"title": title, "url": "", "items": res["ctx"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    report("done", 100, "完成")
    return {
        "title": title,
        "author": "",
        "url": "",
        "bvid": "",
        "cid": 0,
        "duration": "",
        "source": f"本地文件·{client.model}",
        "summary_md": res["md"],
        "output_file": fname,
    }


def run_batch_pipeline(items: list, style: str, progress_cb, stop_check=None, cfg_override: dict | None = None) -> dict:
    """批量总结：items=[{bvid,title}]，逐个跑完整流水线，最后生成目录索引。

    单个视频失败不中断批次，记录错误继续。
    """
    n = len(items)
    done, failures, files = 0, [], []

    def report(stage, percent, message=""):
        progress_cb({"stage": stage, "percent": round(percent, 1), "message": message})

    for i, it in enumerate(items):
        if stop_check and stop_check():
            raise RuntimeError("已取消")
        base = i * (96.0 / max(n, 1))
        span = 96.0 / max(n, 1)

        def prog(d, _b=base, _s=span, _i=i, _n=n, _t=it.get("title", "")):
            report(
                d["stage"], _b + d["percent"] / 100.0 * _s * 0.98,
                f"({_i + 1}/{_n}) {_t[:24]} · {d['message']}",
            )

        try:
            r = run_pipeline(f"https://www.bilibili.com/video/{it['bvid']}",
                             {"style": style}, prog, stop_check,
                             cfg_override=cfg_override)
            files.append({"title": it.get("title") or r["title"], "file": r["output_file"],
                          "url": r["url"], "author": r.get("author", "")})
        except Exception as e:
            if "取消" in str(e):
                raise
            failures.append({"title": it.get("title", it["bvid"]), "error": str(e)[:120]})

    report("save", 97, "生成目录索引…")
    out_dir = Path(load_config()["output_dir"])
    today = f"{datetime.date.today():%Y%m%d}"
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    index_name = f"{today}-批量总结{n}支视频.md"
    lines = [f"# 批量总结 · {n} 支视频\n",
             f"> 由 [拾光 SeeGlow](https://github.com/Orisyan/seeglow) 生成于 {now} · 成功 {len(files)} 支"
             + (f" · 失败 {len(failures)} 支" if failures else "") + "\n"]
    for f in files:
        lines.append(f"- [{f['title']}]({f['url']})  \n  笔记：`拾光/{f['file']}`")
    if failures:
        lines.append("\n## 失败列表\n")
        lines += [f"- {x['title']}：{x['error']}" for x in failures]
    index_content = "\n".join(lines) + "\n"
    (out_dir / index_name).write_text(index_content, encoding="utf-8")

    report("done", 100, f"完成：成功 {len(files)} / {n}")
    return {
        "title": f"批量总结 · 成功 {len(files)}/{n} 支视频",
        "author": "",
        "url": "",
        "bvid": "",
        "cid": 0,
        "duration": "",
        "source": "批量模式",
        "summary_md": index_content,
        "output_file": index_name,
        "files": files,
        "failures": failures,
    }
