"""极简折线图（复用 png.py 的编码器）。

不画文字——文字要靠字体文件，代价不值当；坐标含义写在网页的图注里。
"""

from __future__ import annotations

from . import png

BG = (255, 255, 255)
AXIS = (190, 210, 222)
GRID = (232, 240, 246)


def _y(v, lo, hi, top, height):
    if hi <= lo:
        return top + height // 2
    f = (v - lo) / (hi - lo)
    return int(top + (1 - f) * height)


def line_chart(width: int, height: int, series: list[dict],
               pad_l: int = 46, pad_r: int = 14, pad_t: int = 14, pad_b: int = 18,
               y_pad: float = 0.08) -> bytes:
    """series: [{"values": [...], "color": (r,g,b), "width": int, "dash": bool}, ...]

    多条线共享同一 y 轴范围；x 轴按索引均分。
    """
    c = png.Canvas(width, height, bg=BG)
    iw, ih = width - pad_l - pad_r, height - pad_t - pad_b
    vals = [v for s in series for v in s["values"] if v == v]
    if not vals:
        return c.to_png()
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    lo -= span * y_pad
    hi += span * y_pad

    # 网格
    for k in range(5):
        y = pad_t + int(ih * k / 4)
        for x in range(pad_l, pad_l + iw):
            c.set(x, y, GRID)
    # 轴
    for x in range(pad_l, pad_l + iw):
        c.set(x, pad_t + ih, AXIS)
    for y in range(pad_t, pad_t + ih + 1):
        c.set(pad_l, y, AXIS)

    n = max(len(s["values"]) for s in series)
    for s in series:
        vs = s["values"]
        w = int(s.get("width", 2))
        dash = bool(s.get("dash"))
        color = s["color"]
        prev = None
        for i, v in enumerate(vs):
            if v != v:
                prev = None
                continue
            x = pad_l + (int(i * (iw - 1) / max(1, n - 1)) if n > 1 else iw // 2)
            y = _y(v, lo, hi, pad_t, ih)
            if prev is not None and not (dash and i % 6 < 2):
                x0, y0 = prev
                steps = max(abs(x - x0), abs(y - y0), 1)
                for t in range(steps + 1):
                    xx = x0 + round((x - x0) * t / steps)
                    yy = y0 + round((y - y0) * t / steps)
                    for dx in range(w):
                        for dy in range(w):
                            c.set(xx + dx, yy + dy, color)
            prev = (x, y)
    return c.to_png()


def bar_chart(width: int, height: int, values: list[float],
              color=(11, 126, 168), pad_l: int = 30, pad_r: int = 12,
              pad_t: int = 12, pad_b: int = 16, base_color=(210, 226, 236)) -> bytes:
    """最简单的柱状图，x 按顺序均分。"""
    c = png.Canvas(width, height, bg=BG)
    iw, ih = width - pad_l - pad_r, height - pad_t - pad_b
    hi = max([v for v in values if v == v] or [1])
    n = len(values)
    if n == 0 or hi <= 0:
        return c.to_png()
    bw = max(2, iw // n - 3)
    for i, v in enumerate(values):
        x0 = pad_l + int(i * iw / n) + 2
        h = int(ih * (v / hi)) if v == v else 0
        for x in range(x0, min(x0 + bw, pad_l + iw)):
            for y in range(pad_t + ih - h, pad_t + ih):
                c.set(x, y, color)
        for x in range(x0, min(x0 + bw, pad_l + iw)):
            c.set(x, pad_t + ih, base_color)
    return c.to_png()
