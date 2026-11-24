"""DXT1 -> RGB -> PNG, same pixels as the webapp's converter (png.js / texture.rs)."""

import math
import struct
import zlib


def _rnd(x):
    return int(math.floor(x + 0.5))


_R5 = [_rnd(i * 255.0 / 31.0) for i in range(32)]
_G6 = [_rnd(i * 255.0 / 63.0) for i in range(64)]


def _rgb565(c):
    return (_R5[(c >> 11) & 31], _G6[(c >> 5) & 63], _R5[c & 31])


_pal_cache = {}


def _palette(c0, c1):
    key = (c0 << 16) | c1
    p = _pal_cache.get(key)
    if p is None:
        a = _rgb565(c0)
        b = _rgb565(c1)
        if c0 > c1:
            p2 = bytes(_rnd((2.0 * a[i] + b[i]) / 3.0) for i in range(3))
            p3 = bytes(_rnd((a[i] + 2.0 * b[i]) / 3.0) for i in range(3))
        else:
            p2 = bytes(_rnd((a[i] + b[i]) / 2.0) for i in range(3))
            p3 = b"\x00\x00\x00"
        p = (bytes(a), bytes(b), p2, p3)
        if len(_pal_cache) > 200000:
            _pal_cache.clear()
        _pal_cache[key] = p
    return p


def dxt1_to_rgb_rows(data, w, h):
    """DXT1 -> list of h RGB rows (top-down block order as stored; missing blocks stay black)."""
    rows = [bytearray(w * 3) for _ in range(h)]
    bw = (w + 3) // 4
    bh = (h + 3) // 4
    n = len(data)
    for by in range(bh):
        for bx in range(bw):
            o = (by * bw + bx) * 8
            if o + 8 > n:
                continue
            c0 = data[o] | (data[o + 1] << 8)
            c1 = data[o + 2] | (data[o + 3] << 8)
            pal = _palette(c0, c1)
            bits = data[o + 4] | (data[o + 5] << 8) | (data[o + 6] << 16) | (data[o + 7] << 24)
            x0 = bx * 4
            for py in range(4):
                y = by * 4 + py
                if y >= h:
                    break
                row = rows[y]
                sh = 8 * py
                for px in range(4):
                    x = x0 + px
                    if x >= w:
                        break
                    d = x * 3
                    row[d:d + 3] = pal[(bits >> (sh + 2 * px)) & 3]
    return rows


def _chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def png_from_dxt1(data, w, h):
    """PNG (RGB, 8 bit, no filters, zlib level 6) of a DXT1 texture, rows flipped (atlases are
    bottom row first). Same pixels as the webapp's; the compressed bytes differ (Python's zlib
    instead of zlib-rs)."""
    rows = dxt1_to_rgb_rows(data, w, h)
    raw = bytearray()
    for y in range(h - 1, -1, -1):
        raw.append(0)
        raw += rows[y]
    idat = zlib.compress(bytes(raw), 6)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")
