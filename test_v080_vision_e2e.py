# -*- coding: utf-8 -*-
"""v0.8.0 端到端测试：mock B站接口与 LLM，走完整 pipeline。

覆盖：
1. vision 开启：音频段附带截图墙图片消息
2. 模型拒绝图片（400）→ 自动降级纯听，且图片被拒后不再重试
3. 总结中途点取消 → 秒级抛出「已取消」
4. 字幕路径的取消（此前完全没有检查点）
"""
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

TMP = Path(tempfile.gettempdir())


def make_test_media(path: Path, sec: float = 60, fps: int = 5, sr: int = 16000):
    """60s 测试媒体：每 10s 换一次亮度（6 幕），并带 440Hz 正弦音轨。"""
    import av

    w, h = 320, 180
    out = av.open(str(path), "w")
    vst = out.add_stream("mpeg4", rate=fps)
    vst.width, vst.height, vst.pix_fmt = w, h, "yuv420p"
    ast = out.add_stream("aac", rate=sr)
    ast.layout = "mono"
    rng = np.random.default_rng(3)
    total = int(sec * fps)
    try:
        for i in range(total):
            scene = int(i // (total / 6))
            bright = 60 + scene * 30
            arr = np.full((h, w, 3), (bright, bright, bright), dtype=np.int16)
            arr += rng.integers(-4, 5, size=arr.shape, dtype=np.int16)
            frame = av.VideoFrame.from_ndarray(np.clip(arr, 0, 255).astype(np.uint8), format="rgb24")
            for pkt in vst.encode(frame):
                out.mux(pkt)

        # 音轨：440Hz 正弦，每写 0.1s 编码一次
        step = int(sr * 0.1)
        t_off = 0
        while t_off < int(sec * sr):
            t = (np.arange(step) + t_off) / sr
            pcm = (np.sin(2 * np.pi * 440 * t) * 8000).astype("<i2").reshape(1, -1)
            aframe = av.AudioFrame.from_ndarray(pcm, format="s16", layout="mono")
            aframe.rate = sr
            aframe.pts = t_off
            for pkt in ast.encode(aframe):
                out.mux(pkt)
            t_off += step
        for pkt in ast.encode(None):
            out.mux(pkt)
        for pkt in vst.encode(None):
            out.mux(pkt)
    finally:
        out.close()
    return path


class MockLLM:
    """记录调用并按配置模拟行为（正常 / 拒图 / 慢速）。"""

    def __init__(self, model="mock-omni"):
        self.model = model
        self.calls = []           # 每次 user 消息的 content 类型摘要
        self.reject_images = False
        self.slow_sec = 0.0
        self._n = 0

    def chat(self, messages, max_retries=2):
        user = messages[-1]
        kinds = [p["type"] for p in user["content"]] if isinstance(user["content"], list) else ["text"]
        self.calls.append(kinds)
        if self.reject_images and any(k == "image_url" for k in kinds):
            raise __import__("seeglow.summarize", fromlist=["LLMError"]).LLMError(
                "API 返回 400: image not supported")
        if self.slow_sec:
            time.sleep(self.slow_sec)
        self._n += 1
        return f"## 模拟小结 {self._n}\n- [00:10] 要点一\n- 数据 42%"


def setup_mocks(media: Path, llm: MockLLM, vision_on: bool):
    from seeglow import bilibili, pipeline, summarize as sz_mod

    cfg_path = Path("config.json")
    backup = cfg_path.exists()
    if backup:
        saved = cfg_path.read_text(encoding="utf-8")
    cfg_path.write_text(json.dumps({
        "api_base": "http://mock", "api_key": "sk-mock", "model": "mock-omni",
        "sessdata": "", "vision": vision_on,
        "output_dir": str(TMP / "seeglow_e2e_out"),
    }), encoding="utf-8")

    bilibili.get_video_info = lambda bvid, sess="": {
        "bvid": bvid, "cid_first": 111, "title": "E2E测试视频", "desc": "",
        "owner": "tester", "owner_mid": 1, "duration": 60,
        "season": {"id": 0, "title": "", "mid": 0, "videos": []},
        "pages": [{"page": 1, "cid": 111, "part": ""}],
    }
    bilibili.get_subtitle_segments = lambda bvid, cid, sess="": (None, [])
    bilibili.get_audio_url = lambda bvid, cid, sess="": str(media)
    bilibili.get_video_url = lambda bvid, cid, sess="": str(media)

    def fake_download(url, dest, sess="", progress_cb=None, stop_check=None):
        shutil.copyfile(url, dest)
        return Path(dest)

    bilibili.download_audio = fake_download

    sz_mod.LLMClient = lambda *a, **k: llm
    return (cfg_path, saved if backup else None)


def restore(cfg_path, saved):
    if saved is not None:
        cfg_path.write_text(saved, encoding="utf-8")
    elif cfg_path.exists():
        cfg_path.unlink()


def test_vision_message():
    from seeglow import pipeline
    from seeglow.summarize import build_audio_message

    media = make_test_media(TMP / "seeglow_e2e_media.mp4")
    llm = MockLLM()
    ctx = setup_mocks(media, llm, vision_on=True)
    try:
        events = []
        r = pipeline.run_pipeline("BV1E2Etest00", {}, lambda d: events.append(d))
        assert r["output_file"], "未生成笔记"
        img_calls = [c for c in llm.calls if "image_url" in c]
        assert img_calls, f"vision 开启但没有图片消息：{llm.calls[:3]}"
        assert all("input_audio" in c or "audio_url" in c for c in llm.calls if len(c) > 1)
        # 音频+图片消息结构正确性（不真正发请求，只验结构）
        msg = build_audio_message("p", media, "input_audio", image_paths=[media])
        types = [p["type"] for p in msg["content"]]
        assert types == ["text", "input_audio", "image_url"], types
        assert msg["content"][2]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        print(f"[ok] vision 流程：{len(llm.calls)} 次调用，其中 {len(img_calls)} 次带图")
    finally:
        restore(*ctx)
        media.unlink(missing_ok=True)


def test_image_fallback():
    from seeglow import pipeline

    media = make_test_media(TMP / "seeglow_e2e_fb.mp4", sec=30)
    llm = MockLLM()
    llm.reject_images = True  # 模拟纯音频模型对图片报 400
    ctx = setup_mocks(media, llm, vision_on=True)
    try:
        r = pipeline.run_pipeline("BV1E2Etest01", {}, lambda d: None)
        assert r["output_file"]
        assert llm._no_images, "拒图后应设置 _no_images 标记"
        # 第一次尝试带图失败 → 立即无图重试成功；后续分段不再带图
        first = llm.calls[0]
        assert "image_url" in first, "第一次应尝试带图"
        later = [c for c in llm.calls[1:] if "image_url" in c]
        assert not later, f"拒图后不应再带图：{later[:2]}"
        print(f"[ok] 拒图降级：首调带图失败后全程纯听（共 {len(llm.calls)} 次调用）")
    finally:
        restore(*ctx)
        media.unlink(missing_ok=True)


def test_cancel_during_listen():
    from seeglow import pipeline

    # 600s → 切成 3 段并行直听，取消发生在多段等待期间
    media = make_test_media(TMP / "seeglow_e2e_cancel.mp4", sec=600)
    llm = MockLLM()
    llm.slow_sec = 3.0  # 每段调用挂 3 秒，模拟长 LLM 请求
    ctx = setup_mocks(media, llm, vision_on=False)
    stop = {"flag": False}
    try:
        t0 = {}

        def progress(d):
            t0.setdefault("start", time.time())
            # 首个进度回调后 1 秒点取消（模拟用户点击）
            if not stop["flag"] and time.time() - t0["start"] > 1.0:
                stop["flag"] = True

        begin = time.time()
        try:
            pipeline.run_pipeline("BV1E2Etest02", {}, progress, stop_check=lambda: stop["flag"])
            raise AssertionError("应已取消")
        except RuntimeError as e:
            assert "取消" in str(e), f"异常类型不对：{e}"
        elapsed = time.time() - begin
        assert elapsed < 8, f"取消生效太慢：{elapsed:.1f}s（等待慢段完整跑完了）"
        print(f"[ok] 直听中途取消：{elapsed:.1f}s 内生效")
    finally:
        restore(*ctx)
        media.unlink(missing_ok=True)


def test_cancel_during_subtitle_summary():
    """字幕路径（summarize_transcript）此前完全没有取消检查点。"""
    from seeglow import bilibili
    from seeglow.summarize import summarize_transcript

    media = make_test_media(TMP / "seeglow_e2e_sub.mp4", sec=10)
    llm = MockLLM()
    llm.slow_sec = 2.0
    ctx = setup_mocks(media, llm, vision_on=False)
    try:
        stop = {"flag": False}
        calls = {"n": 0}
        orig = llm.chat

        def chat(messages, max_retries=2):
            calls["n"] += 1
            if calls["n"] >= 2:
                stop["flag"] = True  # 第二段开始前取消
            return orig(messages, max_retries)

        llm.chat = chat
        transcript = "\n".join(f"[00:{i:02d}] 第{i}行内容" for i in range(50))
        try:
            summarize_transcript(transcript * 80, "meta", llm, stop_check=lambda: stop["flag"])
            raise AssertionError("应已取消")
        except RuntimeError as e:
            assert "取消" in str(e)
        print(f"[ok] 字幕总结中途取消（第 {calls['n']} 次调用后生效）")
    finally:
        restore(*ctx)
        media.unlink(missing_ok=True)


if __name__ == "__main__":
    test_vision_message()
    test_image_fallback()
    test_cancel_during_listen()
    test_cancel_during_subtitle_summary()
    print("\n端到端全部通过 ✓")
