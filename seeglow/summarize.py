"""大模型总结：OpenAI 兼容 API；支持文本转写稿总结与多模态模型直听音频。"""

from __future__ import annotations

import time
from pathlib import Path

import requests


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, api_base, api_key, model, temperature=0.3, timeout=300):
        self.api_base = (api_base or "").rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model = model or ""
        self.temperature = temperature
        self.timeout = timeout
        if not self.api_base:
            raise LLMError("请先在设置中配置 API 地址（如 https://api.siliconflow.cn/v1）")
        if not self.api_key:
            raise LLMError("请先在设置中配置 API Key")
        if not self.model:
            raise LLMError("请先在设置中配置模型名称")

        # 复用 HTTPS 连接，省去每次调用的 TLS 握手
        from requests.adapters import HTTPAdapter

        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=8)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def chat(self, messages, max_retries=2) -> str:
        url = self.api_base + "/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_err = None
        for attempt in range(max_retries + 1):
            try:
                r = self.session.post(url, json=payload, headers=headers, timeout=self.timeout)
                if r.status_code == 200:
                    content = r.json()["choices"][0]["message"]["content"]
                    if content and content.strip():
                        return content.strip()
                    last_err = LLMError("模型返回了空内容")
                else:
                    last_err = LLMError(f"API 返回 {r.status_code}: {r.text[:300]}")
                    if r.status_code in (400, 401, 403, 404):
                        # 客户端错误（如模型不支持音频输入）重试无意义，直接抛出
                        raise last_err
            except requests.RequestException as e:
                last_err = e
            if attempt < max_retries:
                time.sleep(2 * (attempt + 1))
        raise LLMError(f"调用大模型失败：{last_err}")


SYSTEM_PROMPT = (
    "你是「拾光 SeeGlow」的资深视频内容分析师，擅长把冗长的视频转写稿提炼成"
    "结构清晰、信息密度高的中文笔记。输出使用 GitHub 风格 Markdown，"
    "忠实于原片内容，不编造事实。直接输出正文，不要寒暄。"
)

CHUNK_CHARS = 3600

# 总结风格模板：附加到各总结 prompt 的【输出要求】前
STYLE_GUIDES = {
    "general": "",
    "study": (
        "【风格：学习笔记】\n"
        "- 「核心要点」突出知识点与结论，重要术语加粗\n"
        "- 额外增加一节「## 术语表」：3~8 个关键概念 + 一句话通俗解释\n"
        "- 时间线按知识模块组织，标注每个模块覆盖的时间范围\n"
    ),
    "tutorial": (
        "【风格：教程步骤】\n"
        "- 把可操作的内容整理为「## 操作步骤」：编号步骤，写明参数/路径/命令/前置条件\n"
        "- 额外增加「## 常见坑与提示」：视频中提到的坑、报错与解决办法\n"
    ),
    "meeting": (
        "【风格：会议纪要】\n"
        "- 要点按议题归纳，一个议题一个小节\n"
        "- 额外增加「## 结论与待办」：逐条写明 做什么 + 谁负责（未指明则写「主讲人/未指明」）\n"
    ),
    "review": (
        "【风格：轻松杂谈】\n"
        "- 语言轻快有梗，但事实必须忠于原片，不编造\n"
        "- 金句多摘 1~2 条；额外增加「## 名场面」：3~5 个值得一看的时间点（[mm:ss]）+ 一句话安利\n"
    ),
    "speed": (
        "【风格：⚡ 期末速成 · 速学模式】读者是要在几小时内自学完这份内容、准备考试的学生。"
        "总原则：**穷尽全部知识点，宁多勿漏**，同时让速学效率最大化。\n"
        "- 「## 必会知识点」：穷尽视频里出现的**所有**知识点/概念/公式/结论/案例，宁多勿漏；"
        "每条前用★标注考试重要度（★★★必会 / ★★常考 / ★了解），至少输出 6 条，每条用一两句话讲透，"
        "关键术语、数据、公式加粗\n"
        "- 「## 术语表」：所有专业术语 + 一句话通俗解释 +（如有）英文对照，覆盖必会知识点里的全部术语\n"
        "- 「## 干货时间线」：只保留知识内容段落，跳过寒暄、广告与铺垫，格式 `[mm:ss] 主题 —— 一句话结论`\n"
        "- 「## 高频考点预测」：基于内容推测最可能被出题的 3~6 个考点，"
        "每条注明可能的题型（选择/填空/简答/计算/论述）和考察角度\n"
        "- 「## 易错点与辨析」：容易混淆、容易理解错的地方，做对比辨析\n"
        "- 「## 自测八题」：8 道简答题，覆盖上面全部知识点；每题下一行用引用格式（> ）给出答案，方便先自测后对照\n"
        "- 「## 记忆锚点」：口诀/类比/联想/数字桩，帮助快速记住核心内容（如内容不适合则写知识框架串联）\n"
        "- 「## 思维导图」：用**缩进列表**输出全知识结构树，必须覆盖必会知识点中的每一项；"
        "每个节点 2~12 字，层级 2~4 层，格式如下（缩进用两个空格）：\n"
        "  - 中心主题（视频主题）\n"
        "    - 一级分支（知识模块）\n"
        "      - 子节点（具体知识点）\n"
        "- 长度上限放宽：为覆盖全部知识点，全文可到 2500 字，但每条仍须精炼、不注水"
    ),
}


def style_hint(style: str) -> str:
    return STYLE_GUIDES.get(style or "general", "")


def chunk_text(text: str, max_chars: int = CHUNK_CHARS):
    lines = text.splitlines()
    chunks, cur, size = [], [], 0
    for ln in lines:
        if size + len(ln) > max_chars and cur:
            chunks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(ln)
        size += len(ln) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


SINGLE_PROMPT = """请根据下面的B站视频转写稿，生成一份高质量中文总结笔记。

【视频信息】
{meta}

【转写稿】（方括号内为时间点）
{transcript}

{style_hint}
【输出要求】
使用以下 Markdown 结构，直接以 `## 一句话总结` 开头，不要输出一级标题。
若上方【风格】要求与下方结构冲突（章节名称、新增章节等），以风格要求为准，风格要求的新增章节必须输出：

## 一句话总结
用一句话概括这支视频讲了什么。

## 核心要点
- 用 5~10 条要点覆盖视频的主要论点/知识点，每条一行，重要概念加粗。

## 内容时间线
- 依据转写稿中的时间点列出 4~10 个章节，格式：`[mm:ss] 章节名 —— 一句话说明`。

## 金句摘录
- 摘录 2~4 句原文中有价值或有趣的表达。

## 适合谁看 & 结语
简短说明目标观众，并给出一句观看建议。"""

MAP_PROMPT = """以下是长视频的第 {i}/{n} 段转写稿（上下文可能被截断）。

【视频信息】
{meta}

【本段转写稿】
{chunk}

请提炼本段内容，直接输出 Markdown：
## 本段小结
- 3~6 条要点，保留关键数据、结论与时间点；如出现明显时间点，请在条目前标注 `[mm:ss]`。
不要寒暄，直接输出。"""

REDUCE_PROMPT = """下面是一支长视频各片段的小结，请整合成一份最终总结笔记。

【视频信息】
{meta}

【各段小结】
{joined}

{style_hint}
【输出要求】
若上方存在【风格】块：以风格要求的章节与结构为准，风格要求的新增章节必须全部输出，长度上限按风格要求放宽。
若无【风格】块：严格使用以下结构，直接以 `## 一句话总结` 开头：
## 一句话总结 / ## 核心要点 / ## 内容时间线 / ## 金句摘录 / ## 适合谁看 & 结语
要求：合并重复内容；时间线按时间顺序排列并保留 `[mm:ss]` 时间点。"""


def summarize_transcript(transcript_text: str, meta: str, client: LLMClient, progress_cb=None, style="general"):
    def report(p, msg=""):
        if progress_cb:
            progress_cb(min(max(p, 0.0), 1.0), msg)

    chunks = chunk_text(transcript_text)
    n = len(chunks)

    if n == 1:
        report(0.15, "正在生成总结…")
        md = client.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": SINGLE_PROMPT.format(
                    meta=meta, transcript=chunks[0], style_hint=style_hint(style))},
            ]
        )
        report(1.0, "总结完成")
        return md

    partials = []
    for i, ch in enumerate(chunks):
        report(i / n * 0.85, f"分段精读 {i + 1}/{n}…")
        part = client.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": MAP_PROMPT.format(i=i + 1, n=n, meta=meta, chunk=ch),
                },
            ]
        )
        partials.append(part)

    report(0.9, "汇总全文总结…")
    joined = "\n\n".join(f"【片段 {i + 1} 小结】\n{p}" for i, p in enumerate(partials))
    md = client.chat(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": REDUCE_PROMPT.format(
                meta=meta, joined=joined, style_hint=style_hint(style))},
        ]
    )
    report(1.0, "总结完成")
    return md


# ---------------- 单模型直听音频（多模态） ----------------

AUDIO_CHUNK_SEC = 240
AUDIO_CONCURRENCY = 6

AUDIO_MAP_PROMPT = """你是视频内容分析师。以下是视频《{title}》从第 {start} 到第 {end} 的音频片段，请直接聆听。

听完输出该片段的 Markdown 小结，格式如下：

## [mm:ss] 段落主题
- 3~6 条要点：保留关键数据、人名、结论与原话金句；重要时间点用 `[mm:ss]` 标注（相对整支视频，本段起点为 {start}）。

直接输出，不要寒暄。"""

AUDIO_SINGLE_PROMPT = """你是视频内容分析师。以下是视频《{title}》的完整音频（从 {start} 到 {end}），请直接聆听。

听完后生成一份高质量中文总结笔记，直接以 `## 一句话总结` 开头。若下方【风格】与结构要求冲突，以风格要求为准，风格要求的新增章节必须输出：

{style_hint}
## 一句话总结
用一句话概括这支视频讲了什么。

## 核心要点
- 用 5~10 条要点覆盖主要论点/知识点，重要概念加粗。

## 内容时间线
- 依据音频中的内容顺序列出 4~10 个章节，格式：`[mm:ss] 章节名 —— 一句话说明`。

## 金句摘录
- 摘录 2~4 句原话中有价值或有趣的表达。

## 适合谁看 & 结语
简短说明目标观众，并给出一句观看建议。"""

REDUCE_AUDIO_PROMPT = """下面是 AI 逐段聆听视频《{title}》音频后得到的分段小结，请整合成一份最终总结笔记。

【各段小结】
{joined}

{style_hint}
【输出要求】
若上方存在【风格】块：以风格要求的章节与结构为准，风格要求的新增章节必须全部输出，长度上限按风格要求放宽。
若无【风格】块：严格使用以下结构，直接以 `## 一句话总结` 开头：
## 一句话总结 / ## 核心要点 / ## 内容时间线 / ## 金句摘录 / ## 适合谁看 & 结语
要求：合并重复内容；时间线按时间顺序并保留时间点。"""


def _wav_b64(path) -> str:
    import base64

    return base64.b64encode(Path(path).read_bytes()).decode()


def build_audio_message(prompt: str, audio_path, style: str = "input_audio") -> dict:
    suffix = str(audio_path).lower().rsplit(".", 1)[-1]
    mime = "audio/mpeg" if suffix == "mp3" else "audio/wav"
    fmt = suffix if suffix in ("mp3", "wav") else "wav"
    b64 = _wav_b64(audio_path)
    if style == "audio_url":
        # 硅基流动等平台使用的 data-uri 格式
        content = [
            {"type": "text", "text": prompt},
            {"type": "audio_url", "audio_url": {"url": f"data:{mime};base64,{b64}"}},
        ]
    else:
        # OpenAI 标准格式
        content = [
            {"type": "text", "text": prompt},
            {"type": "input_audio", "input_audio": {"data": b64, "format": fmt}},
        ]
    return {"role": "user", "content": content}


def _listen_chunk(client, system_prompt, prompt, audio_path, preferred_style):
    """让模型听一段音频并返回小结；平台不接受 input_audio 时自动换 data-uri 格式。"""
    try:
        return (
            client.chat(
                [
                    {"role": "system", "content": system_prompt},
                    build_audio_message(prompt, audio_path, preferred_style),
                ]
            ),
            preferred_style,
        )
    except LLMError as e:
        if preferred_style == "input_audio" and "400" in str(e):
            return (
                client.chat(
                    [
                        {"role": "system", "content": system_prompt},
                        build_audio_message(prompt, audio_path, "audio_url"),
                    ]
                ),
                "audio_url",
            )
        raise


def summarize_audio_direct(audio_path, title, client: LLMClient, progress_cb=None, stop_check=None, notice_cb=None, style="general"):
    def notice(msg):
        if notice_cb:
            notice_cb(msg)

    def stopped():
        return stop_check is not None and stop_check()

    import os
    import tempfile

    from concurrent.futures import ThreadPoolExecutor, as_completed

    from .audioutil import SR, cleanup, chunk_starts, decode_audio, export_chunk, split_chunks
    from .bilibili import fmt_ts

    notice("正在解码音频…")
    audio = decode_audio(str(audio_path))
    chunks = split_chunks(audio, AUDIO_CHUNK_SEC)
    n = len(chunks)
    starts = chunk_starts(chunks)
    total_sec = starts[-1] + len(chunks[-1]) / SR if chunks else 0

    tmp_dir = Path(tempfile.gettempdir())
    name_base = f"seeglow_omni_{os.getpid()}"
    paths = []
    try:
        for i, ch in enumerate(chunks):
            paths.append(export_chunk(ch, tmp_dir, f"{name_base}_{i}"))
        sizes = sum(p.stat().st_size for p in paths)
        notice(
            f"音频 {total_sec:.0f}s → {len(paths)} 段（共 {sizes // 1024} KB），"
            + (f"{AUDIO_CONCURRENCY} 路并行直听…" if n > 1 else "AI 正在听…")
        )

        # 单段：一次调用直接产出最终总结（省去汇总步骤）
        if n == 1:
            if progress_cb:
                progress_cb(0.25, f"AI 正在听音频（{sizes // 1024} KB）…")
            md, _ = _listen_chunk(
                client,
                SYSTEM_PROMPT,
                AUDIO_SINGLE_PROMPT.format(
                    title=title, start=fmt_ts(starts[0]), end=fmt_ts(total_sec),
                    style_hint=style_hint(style),
                ),
                paths[0],
                "input_audio",
            )
            if progress_cb:
                progress_cb(1.0, "总结完成")
            ctx = [{
                "start": starts[0],
                "end": total_sec,
                "text": f"全片音频小结（{fmt_ts(starts[0])}-{fmt_ts(total_sec)}）\n{md}",
            }]
            return {"md": md, "ctx": ctx}

        # 多段：并行直听 → 汇总
        partials = [None] * n
        done = 0

        def job(idx):
            end_t = starts[idx] + len(chunks[idx]) / SR
            prompt = AUDIO_MAP_PROMPT.format(
                title=title, start=fmt_ts(starts[idx]), end=fmt_ts(end_t)
            )
            return _listen_chunk(client, SYSTEM_PROMPT, prompt, paths[idx], "input_audio")

        workers = min(AUDIO_CONCURRENCY, n)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(job, i): i for i in range(n)}
            for fu in as_completed(futures):
                idx = futures[fu]
                partials[idx], _style = fu.result()
                done += 1
                if stopped():
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise RuntimeError("已取消")
                if progress_cb:
                    progress_cb(done / n * 0.85, f"AI 并行直听中… {done}/{n}")

        if progress_cb:
            progress_cb(0.9, "汇总全文总结…")
        joined = "\n\n".join(partials)
        md = client.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": REDUCE_AUDIO_PROMPT.format(
                        title=title, joined=joined, style_hint=style_hint(style)),
                },
            ]
        )
        if progress_cb:
            progress_cb(1.0, "总结完成")
        ctx = [
            {"start": starts[i], "end": starts[i] + len(chunks[i]) / SR, "text": partials[i]}
            for i in range(n)
        ]
        return {"md": md, "ctx": ctx}
    finally:
        cleanup(*paths)
