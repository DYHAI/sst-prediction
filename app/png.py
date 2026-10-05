"""极简 PNG 编码器（纯标准库）。

只需要 truecolor 8bit 这一种格式，够画海温图和色标。
"""

from __future__ import annotations

import struct
import zlib


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


class Canvas:
    """一张 RGB 画布，行优先。"""

    def __init__(self, width: int, height: int, bg=(255, 255, 255)):
        self.w = width
        self.h = height
        self.px = bytearray(bytes(bg) * (width * height))

    def set(self, x: int, y: int, color) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            self.px[i:i + 3] = bytes(color)

    def rect(self, x0: int, y0: int, x1: int, y1: int, color) -> None:
        for y in range(max(0, y0), min(self.h, y1)):
            for x in range(max(0, x0), min(self.w, x1)):
                self.set(x, y, color)

    def outline(self, x0: int, y0: int, x1: int, y1: int, color, width: int = 1) -> None:
        for t in range(width):
            for x in range(x0, x1):
                self.set(x, y0 + t, color)
                self.set(x, y1 - 1 - t, color)
            for y in range(y0, y1):
                self.set(x0 + t, y, color)
                self.set(x1 - 1 - t, y, color)

    def to_png(self) -> bytes:
        raw = bytearray()
        stride = self.w * 3
        for y in range(self.h):
            raw.append(0)  # filter type 0
            raw += self.px[y * stride:(y + 1) * stride]
        ihdr = struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0)
        return (b"\x89PNG\r\n\x1a\n"
                + _chunk(b"IHDR", ihdr)
                + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
                + _chunk(b"IEND", b""))


# ---------------------------------------------------------------- 调色板

_SST_STOPS = [
    (0.00, (12, 44, 110)),
    (0.22, (0, 118, 190)),
    (0.45, (30, 190, 200)),
    (0.63, (140, 220, 140)),
    (0.78, (250, 215, 90)),
    (0.90, (240, 140, 55)),
    (1.00, (185, 30, 45)),
]

_DIFF_STOPS = [
    (0.00, (40, 60, 170)),
    (0.28, (120, 170, 235)),
    (0.50, (250, 250, 250)),
    (0.72, (245, 165, 120)),
    (1.00, (175, 25, 35)),
]


def _ramp(stops, t: float):
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    for i in range(len(stops) - 1):
        a, ca = stops[i]
        b, cb = stops[i + 1]
        if t <= b or i == len(stops) - 2:
            f = 0.0 if b == a else (t - a) / (b - a)
            f = 0.0 if f < 0 else (1.0 if f > 1 else f)
            return tuple(int(ca[k] + (cb[k] - ca[k]) * f) for k in range(3))
    return stops[-1][1]


def sst_color(v: float, lo: float, hi: float):
    return _ramp(_SST_STOPS, (v - lo) / (hi - lo) if hi > lo else 0.5)


def diff_color(v: float, lim: float):
    return _ramp(_DIFF_STOPS, 0.5 + 0.5 * (v / lim if lim else 0.0))


LAND = (226, 233, 239)
