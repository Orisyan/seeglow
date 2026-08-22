# -*- coding: utf-8 -*-
"""生成拾光 SeeGlow 应用图标 icon.ico（与前端 logo 同款：渐变圆角块 + 白色日出）"""
import math

from PIL import Image, ImageDraw

S = 256  # 主画布尺寸
TOP = (245, 158, 11)    # #f59e0b
BOT = (249, 115, 22)    # #f97316


def gradient_tile(size: int) -> Image.Image:
    """带对角渐变的圆角方块"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    n = size - 1
    for y in range(size):
        for x in range(size):
            t = (x / n * 0.45 + y / n * 0.55)  # 对角渐变权重
            r = int(TOP[0] + (BOT[0] - TOP[0]) * t)
            g = int(TOP[1] + (BOT[1] - TOP[1]) * t)
            b = int(TOP[2] + (BOT[2] - TOP[2]) * t)
            px[x, y] = (r, g, b, 255)

    # 圆角遮罩
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.28), fill=255)
    img.putalpha(mask)
    return img


def draw_sun(img: Image.Image):
    """在 256 画布上绘制白色日出图形（对应前端 SVG 坐标 × 7.1）"""
    k = S / 36.0
    d = ImageDraw.Draw(img)
    w = max(int(2.4 * k), 3)

    def P(x, y):
        return (x * k, y * k)

    # 太阳半圆（上半弧）
    cx, cy, r = 18 * k, 21 * k, 7 * k
    bbox = [cx - r, cy - r, cx + r, cy + r]
    d.arc(bbox, start=180, end=360, fill="white", width=w)

    # 地平线
    d.line([P(10.5, 25), P(25.5, 25)], fill="white", width=w)

    # 三道光芒：中、左斜、右斜
    d.line([P(18, 8.5), P(18, 11.5)], fill="white", width=w)
    d.line([P(9.5, 12.5), P(11.6, 14.6)], fill="white", width=max(w - 1, 2))
    d.line([P(26.5, 12.5), P(24.4, 14.6)], fill="white", width=max(w - 1, 2))


def main():
    base = gradient_tile(S)
    draw_sun(base)
    base.save("icon_preview.png")

    base.save(
        "icon.ico",
        sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)],
    )
    print("icon.ico / icon_preview.png 已生成")


if __name__ == "__main__":
    main()
