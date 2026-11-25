#!/usr/bin/env python3
"""Draw berth's 512 px icon (a boat moored at a pier) as a PNG with zlib only. Run: python3 tools/make_icon.py"""

import math
import os
import struct
import zlib

SIZE = 512
BG = (20, 52, 86)
WATER = (36, 92, 140)
PIER = (214, 176, 120)
HULL = (245, 245, 240)
MARK = (232, 96, 72)


def rounded(x, y, r=96):
    cx = min(max(x, r), SIZE - 1 - r)
    cy = min(max(y, r), SIZE - 1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def pixel(x, y):
    if not rounded(x, y):
        return (0, 0, 0, 0)
    color = BG
    wave = 330 + 8 * math.sin(x / 24.0)
    if y > wave:
        color = WATER
    # Pier deck and posts.
    if 300 <= y <= 324 and 60 <= x <= 452:
        color = PIER
    for px in (92, 256, 420):
        if px - 12 <= x <= px + 12 and 300 <= y <= 440:
            color = PIER
    # Hull: a trapezoid above the deck.
    if 220 <= y <= 290:
        inset = (y - 220) * 0.6
        if 140 + inset <= x <= 372 - inset:
            color = HULL
    # Cabin and flag (the claim).
    if 180 <= y < 220 and 200 <= x <= 300:
        color = HULL
    if 96 <= y < 180 and 246 <= x <= 254:
        color = HULL
    if 96 <= y < 136 and 254 < x <= 320 and (x - 254) <= (136 - y) * 1.65:
        color = MARK
    return color + (255,)


def chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def main():
    rows = bytearray()
    for y in range(SIZE):
        rows.append(0)
        for x in range(SIZE):
            rows.extend(pixel(x, y))
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b"")
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins", "berth",
                       ".claude-plugin", "icon.png")
    with open(out, "wb") as fh:
        fh.write(png)
    print("wrote %s (%d bytes)" % (out, len(png)))


if __name__ == "__main__":
    main()
