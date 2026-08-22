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
