"""托盘图标绘制（PIL 生成，无需外部图标文件）。"""
from __future__ import annotations

from PIL import Image, ImageDraw

COLORS = {
    "on": (34, 197, 94),      # 绿：已连接
    "off": (148, 163, 184),   # 灰：未接管
    "busy": (245, 158, 11),   # 橙：启动/切换中
    "error": (239, 68, 68),   # 红：内核异常
}

_cache: dict[tuple[str, int], Image.Image] = {}


def make_icon(state: str = "on", size: int = 64) -> Image.Image:
    key = (state, size)
    if key in _cache:
        return _cache[key]

    color = COLORS.get(state, COLORS["off"])
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = max(1, size // 16)
    d.ellipse((pad, pad, size - pad, size - pad), fill=color)

    # 中间画一个 V（VPN）
    w = max(2, int(size * 0.11))
    x1, y1 = size * 0.29, size * 0.30
    x2, y2 = size * 0.50, size * 0.70
    x3, y3 = size * 0.72, size * 0.30
    d.line([(x1, y1), (x2, y2)], fill=(255, 255, 255, 255), width=w, joint="curve")
    d.line([(x2, y2), (x3, y3)], fill=(255, 255, 255, 255), width=w, joint="curve")

    _cache[key] = img
    return img
