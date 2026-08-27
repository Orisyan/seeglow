# -*- coding: utf-8 -*-
"""v0.8.0 单元测试：画面理解（关键帧抽取 / 截图墙）+ 取消检查点。

运行：python test_v080_vision_unit.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

TMP = Path(tempfile.gettempdir())


def make_test_video(path: Path, sec: float = 6.0, fps: int = 10):
    """合成测试视频：前半段纯红、后半段纯蓝（各带轻微噪点防止编码器全黑帧优化）。"""
    import av

    w, h = 320, 180
    out = av.open(str(path), "w")
    st = out.add_stream("mpeg4", rate=fps)
    st.width, st.height, st.pix_fmt = w, h, "yuv420p"
    rng = np.random.default_rng(7)
    try:
        for i in range(int(sec * fps)):
            # 前半亮红（灰度≈130）、后半暗蓝（灰度≈70）：灰度差必须可检测
            base = (230, 60, 60) if i < sec * fps / 2 else (30, 30, 150)
            arr = np.full((h, w, 3), base, dtype=np.int16)
            arr += rng.integers(-6, 7, size=arr.shape, dtype=np.int16)
            frame_arr = np.clip(arr, 0, 255).astype(np.uint8)
            frame = av.VideoFrame.from_ndarray(frame_arr, format="rgb24")
            for pkt in st.encode(frame):
                out.mux(pkt)
        for pkt in st.encode(None):
            out.mux(pkt)
    finally:
        out.close()
    return path


def test_collect_and_grid():
    from seeglow.audioutil import build_frame_grids

    vid = make_test_video(TMP / "seeglow_test_vis.mp4")
    # 单段：整支视频一个分段，应至少抽出 2 个关键帧（红、蓝两幕）
    grids, paths = build_frame_grids(str(vid), [0.0], [6.0], TMP, "seeglow_t_single")
    assert grids, "未抽取到任何关键帧"
    idxs = list(grids.keys())
    assert idxs == [0], f"分段索引错误：{idxs}"
    gpath, times = grids[0]
    assert gpath.exists() and gpath.stat().st_size > 5000, "截图墙文件异常"
    assert times == sorted(times), "帧时间点应有序"
    assert any(t >= 2.5 for t in times), "后段（蓝色场景）没有对应关键帧"
    print(f"[ok] 单段抽取 {len(times)} 帧，截图墙 {gpath.stat().st_size // 1024} KB")

    # 两段：每段至少各 1 帧
    grids2, paths2 = build_frame_grids(str(vid), [0.0, 3.0], [3.0, 3.0], TMP, "seeglow_t_dual")
    assert set(grids2.keys()) == {0, 1}, f"应两个分段都有画面：{set(grids2.keys())}"
    print(f"[ok] 双段各自抽帧：{[(len(v[1])) for v in grids2.values()]}")

    for p in paths + paths2 + [vid]:
        p.unlink(missing_ok=True)


def test_cancel():
    import threading
    from seeglow.audioutil import collect_keyframes

    vid = make_test_video(TMP / "seeglow_test_cancel.mp4", sec=4)
    fired = threading.Event()

    def stop():
        return True  # 立即要求取消

    try:
        collect_keyframes(str(vid), [0.0], [4.0], stop_check=stop)
        raise AssertionError("应抛出已取消")
    except RuntimeError as e:
        assert "取消" in str(e), f"错误信息不对：{e}"
        print("[ok] 抽帧过程响应取消")
    finally:
        vid.unlink(missing_ok=True)


def test_summarize_import():
    """冒烟：确认 summarize/pipeline 模块可导入且新签名存在。"""
    import inspect

    from seeglow import summarize as sz
    sig = inspect.signature(sz.summarize_transcript)
    assert "stop_check" in sig.parameters
    sig2 = inspect.signature(sz.summarize_audio_direct)
    assert "video_src" in sig2.parameters
    note = sz.frame_grid_note(3, [10, 20.5, 125])
    assert "[00:20]" in note.replace("20分30秒", "") and "第3帧" in note
    print("[ok] summarize 新参数与截图墙说明正常")


if __name__ == "__main__":
    test_collect_and_grid()
    test_cancel()
    test_summarize_import()
    print("\n全部通过 ✓")
