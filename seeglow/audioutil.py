"""音频工具：解码、按静音切段、写 WAV。供多模态模型直听音频使用。

仅依赖 PyAV(av) 与 numpy，无需 ffmpeg 二进制、无需 faster-whisper。
"""

from __future__ import annotations

SR = 16000


def decode_audio(path):
    """任意音频/视频容器 → 16kHz 单声道 float32 numpy 数组。"""
    import av
    import numpy as np

    container = av.open(str(path))
    try:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise RuntimeError("音频解码失败：文件中没有音轨")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SR)
        pieces = []
        for packet in container.demux(stream):
            for frame in packet.decode():
                for conv in resampler.resample(frame):
                    pieces.append(conv.to_ndarray().ravel())
    finally:
        container.close()
    if not pieces:
        raise RuntimeError("音频解码失败：没有可用的音频数据")
    audio = np.concatenate(pieces).astype(np.float32) / 32768.0
    return audio


def split_chunks(audio, chunk_sec: int = 240):
    """把整段音频切成多块，尽量在最安静的位置下刀，避免切断词语。"""
    import numpy as np

    n = len(audio)
    size = chunk_sec * SR
    if n <= size * 1.3:
        return [audio]

    chunks = []
    pos = 0
    while pos < n:
        nxt = min(pos + size, n)
        if nxt < n:
            # 在目标切点 ±1 秒内找能量最低的 20ms 窗口作为切点
            win = SR // 50
            lo = max(pos + 10 * SR, nxt - SR)
            hi = min(nxt + SR, n - win)
            best_i, best_e = lo, None
            for i in range(lo, hi, win // 2):
                e = float(np.abs(np.asarray(audio[i : i + win])).mean())
                if best_e is None or e < best_e:
                    best_e, best_i = e, i
            nxt = min(best_i + win // 2, n)
        chunks.append(audio[pos:nxt])
        pos = nxt
    return chunks


def chunk_starts(chunks):
    """各段在整支视频中的起始秒数。"""
    starts = []
    acc = 0.0
    for ch in chunks:
        starts.append(acc)
        acc += len(ch) / SR
    return starts


def to_wav(chunk_ndarray, dest):
    import numpy as np
    import wave
    from pathlib import Path

    pcm = np.clip(chunk_ndarray, -1.0, 1.0)
    pcm = (pcm * 32767).astype("<i2").tobytes()
    with wave.open(str(Path(dest)), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)


def to_mp3(chunk_ndarray, dest, bit_rate=48000):
    """压缩为单声道 MP3：体积约为 WAV 的 1/20，上传更快；语音质量无感知损失。"""
    import av
    import numpy as np
    from pathlib import Path

    pcm = np.clip(chunk_ndarray, -1.0, 1.0)
    pcm = (pcm * 32767).astype("<i2")
    out = av.open(str(Path(dest)), "w")
    try:
        stream = out.add_stream("libmp3lame", rate=SR)
        stream.layout = "mono"
        stream.bit_rate = bit_rate
        resampler = av.AudioResampler(format="s16p", layout="mono", rate=SR)
        step = SR  # 每次喂 1 秒
        for offset in range(0, len(pcm), step):
            frame = av.AudioFrame.from_ndarray(
                pcm[offset : offset + step].reshape(1, -1), format="s16", layout="mono"
            )
            frame.rate = SR
            for f in resampler.resample(frame):
                for pkt in stream.encode(f):
                    out.mux(pkt)
        for pkt in stream.encode(None):
            out.mux(pkt)
    finally:
        out.close()


def export_chunk(chunk_ndarray, dir_path, name_base):
    """优先导出 MP3（小而快）；编码器不可用时回退 WAV。返回实际文件路径。"""
    import os
    from pathlib import Path

    mp3 = Path(dir_path) / f"{name_base}.mp3"
    try:
        to_mp3(chunk_ndarray, mp3)
        if mp3.exists() and mp3.stat().st_size > 0:
            return mp3
    except Exception:
        pass
    wav = Path(dir_path) / f"{name_base}.wav"
    to_wav(chunk_ndarray, wav)
    return wav


def cleanup(*paths):
    import os

    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


# ---------------- 画面理解：关键帧抽取 + 截图墙拼接 ----------------
# 思路：速成课/教程的画面特征是大段静止的 PPT、题目页，因此不做定时截屏，
# 而是按“画面变化检测”只保留换页后的关键帧，拼成一张截图墙随音频一起交给多模态模型。

FRAME_SAMPLE_STEP = 1.0       # 每隔约 1 秒做一次画面变化检测
SCENE_DIFF_THRESHOLD = 12     # 缩略图平均像素差（0~255）超过该值视为换页/切镜
GRID_COLS = 5                 # 截图墙每行帧数
CELL_W = 320                  # 单帧在截图墙中的宽度
CELL_H = 180                  # 单帧高度（B站基本为 16:9）
MAX_FRAMES_PER_CHUNK = 30     # 每个音频分段的截图墙上限，控制请求体积
CELL_BYTES_HINT = CELL_W * CELL_H * 3

# B站视频流有防盗链：走 ffmpeg 的 http 协议时必须带上 UA 与 Referer
_AV_OPEN_OPTS = {
    "rw_timeout": "30000000",  # 30s 无数据视为断流（微秒）
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "headers": "Referer: https://www.bilibili.com/\r\n",
}


def _to_cell_rgb(frame):
    """视频帧 → CELL_W×CELL_H 的 RGB numpy 数组（拉伸适配，仅用于给模型看）。"""
    return frame.reformat(CELL_W, CELL_H, format="rgb24").to_ndarray()


def _gray_diff(a, b) -> float:
    """两张同尺寸图的平均差异（按通道均值粗算，够用且省内存）。"""
    import numpy as np

    ga = a.mean(axis=2)[::4, ::4]
    gb = b.mean(axis=2)[::4, ::4]
    return float(np.abs(ga.astype(np.int16) - gb.astype(np.int16)).mean())


def collect_keyframes(video_src, chunk_starts, chunk_durs, max_per_chunk=MAX_FRAMES_PER_CHUNK,
                      stop_check=None):
    """流式读取视频源（URL 或本地文件），按画面变化挑选每个分段的关键帧。

    返回 {chunk_idx: [(时间秒, RGB数组), ...]}；本地纯音频文件会抛 RuntimeError。
    """
    import av
    from bisect import bisect_right

    n = len(chunk_starts)
    if not n:
        return {}
    total_end = chunk_starts[-1] + chunk_durs[-1]
    starts_list = list(chunk_starts)  # bisect 用，预转一次

    container = av.open(str(video_src), options=dict(_AV_OPEN_OPTS))
    kept = {i: [] for i in range(n)}
    try:
        stream = next((s for s in container.streams if s.type == "video"), None)
        if stream is None:
            raise RuntimeError("视频源中没有画面流")

        last_check_t = -1e9
        ref = None  # 上一个保留帧的降采样灰度，作为变化检测基准
        tb = stream.time_base or 1 / 1000
        done_reading = False
        for packet in container.demux(stream):
            if stop_check is not None and stop_check():
                raise RuntimeError("已取消")
            for frame in packet.decode():
                if frame.pts is None:
                    continue
                t = float(frame.pts * tb)
                if t > total_end + 2:
                    done_reading = True
                    break
                if t - last_check_t < FRAME_SAMPLE_STEP:
                    continue
                last_check_t = t

                ci = min(max(bisect_right(starts_list, t) - 1, 0), n - 1)
                bucket = kept[ci]
                if len(bucket) >= max_per_chunk:
                    continue

                cell = _to_cell_rgb(frame)
                is_first_of_chunk = not bucket and t >= chunk_starts[ci]
                changed = ref is None or _gray_diff(cell, ref) > SCENE_DIFF_THRESHOLD
                if not (changed or is_first_of_chunk):
                    continue
                bucket.append((t, cell))
                ref = cell
            if done_reading:
                break
    finally:
        container.close()
    return kept


def _write_grid_jpeg(cells, dest):
    """把若干 RGB 数组拼成网格 JPEG（用 PyAV 编码，不引入 Pillow）。"""
    import av
    import numpy as np
    from pathlib import Path

    dest = Path(dest)

    k = len(cells)
    cols = GRID_COLS
    rows = (k + cols - 1) // cols
    canvas = np.full((rows * CELL_H, cols * CELL_W, 3), 40, dtype=np.uint8)
    for i, arr in enumerate(cells):
        r, c = divmod(i, cols)
        h, w = arr.shape[:2]
        ch = min(h, CELL_H)
        cw = min(w, CELL_W)
        y0 = r * CELL_H + (CELL_H - ch) // 2
        x0 = c * CELL_W + (CELL_W - cw) // 2
        canvas[y0 : y0 + ch, x0 : x0 + cw] = arr[:ch, :cw]

    w, h = canvas.shape[1], canvas.shape[0]
    if w % 2:
        canvas = canvas[:, :-1]
    if h % 2:
        canvas = canvas[:-1, :]

    dest.parent.mkdir(parents=True, exist_ok=True)
    out = av.open(str(dest), "w")
    try:
        st = out.add_stream("mjpeg", rate=1)
        st.width = canvas.shape[1]
        st.height = canvas.shape[0]
        st.pix_fmt = "yuvj420p"
        frame = av.VideoFrame.from_ndarray(canvas, format="rgb24")
        for pkt in st.encode(frame):
            out.mux(pkt)
        for pkt in st.encode(None):
            out.mux(pkt)
    finally:
        out.close()


def build_frame_grids(video_src, chunk_starts, chunk_durs, dir_path, name_base,
                      max_per_chunk=MAX_FRAMES_PER_CHUNK, notice=None, stop_check=None):
    """抽取关键帧并按音频分段导出截图墙。

    返回 ({chunk_idx: (jpg路径, [帧时间秒])}, 所有生成文件路径列表)。
    """
    from pathlib import Path

    def say(msg):
        if notice:
            notice(msg)

    frames = collect_keyframes(
        video_src, chunk_starts, chunk_durs,
        max_per_chunk=max_per_chunk, stop_check=stop_check,
    )
    out, paths = {}, []
    total = 0
    for idx in sorted(frames):
        fl = frames[idx]
        if not fl:
            continue
        jpg = Path(dir_path) / f"{name_base}_c{idx}.jpg"
        _write_grid_jpeg([c for _, c in fl], jpg)
        paths.append(jpg)
        out[idx] = (jpg, [t for t, _ in fl])
        total += len(fl)
    say(f"共抽取 {total} 张关键帧（{len(out)} 个分段有画面）")
    return out, paths
