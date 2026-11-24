"""
PlanetoidMetadata / BulkMetadata decoding, reproducing what Google's minified decoder in the
webapp (re/exporter/lib/decode-resource.js, command 0: rc.prototype.Nc) produces, quirks
included (it reads some fields regardless of their wire type, and out-of-range bytes read as 0).
Only the parts the tile lookup uses are kept: childIndices, epoch, bulkMetadataEpoch, flags,
imageryEpochArray, textureFormatArray and the defaults.
"""

import struct

_f32 = struct.Struct("<f")
_f64 = struct.Struct("<d")


def _i32(v):
    v = int(v) & 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def _u32(v):
    try:
        return int(v) & 0xFFFFFFFF
    except (ValueError, OverflowError):
        return 0  # NaN / Infinity store as 0 in a Uint32Array


def _u8(v):
    try:
        return int(v) & 0xFF
    except (ValueError, OverflowError):
        return 0


def _i16(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


class _JsReader:
    """The decoder's protobuf reader (qc): reads past the end give `undefined` (0 in bit maths)."""

    def __init__(self, data):
        self.b = data
        self.n = len(data)
        self.a = 0
        self.c = len(data)
        self.h = []
        self.o = 0

    def _byte(self):
        i = self.a
        self.a = i + 1
        if isinstance(i, int) and 0 <= i < self.n:
            return self.b[i]
        return None

    def D(self):
        if self.a < self.c:
            v = self.sa()
            self.o = _i32(v) & 7 if v == v else 0
            return _i32(v) >> 3 if v == v else 0
        return 0

    def O(self, c):
        self.h.append(self.c)
        self.c = c

    def N(self):
        if not self.h:
            return False
        self.c = self.h.pop()
        return True

    def sa(self):
        c = 0.0
        d = 1.0
        while True:
            e = self._byte()
            c += float((127 & e) if e is not None else 0) * d
            d *= 128.0
            if e is None or not (e & 128):
                break
        return int(c) if c == c and abs(c) != float("inf") and c == int(c) and abs(c) < 2 ** 63 else c

    f = sa

    def jd(self):
        while True:
            c = self._byte()
            if c is None or not (c & 128):
                return

    def hd(self):
        self.a = self.sa() + self.a

    def B(self):
        o = self.o
        if o == 0:
            self.jd()
        elif o == 1:
            self.a += 8
        elif o == 2:
            self.hd()
        elif o == 5:
            self.a += 4
        else:
            return False
        return True

    def _check(self, k):
        a = self.a
        if not isinstance(a, int) or a < 0 or a + k > self.n:
            raise ValueError("RangeError: offset is outside the bounds of the DataView")

    def float32(self):
        self._check(4)
        v = _f32.unpack_from(self.b, self.a)[0]
        self.a += 4
        return v

    def float64(self):
        self._check(8)
        v = _f64.unpack_from(self.b, self.a)[0]
        self.a += 8
        return v

    def Ia(self):
        e = self._byte()
        return e

    def Ka(self):
        c = self._byte() or 0
        c |= (self._byte() or 0) << 8
        return c

    def ia(self):
        c = self._byte() or 0
        return ((self._byte() or 0) << 8) | c

    def cd(self):
        ln = self._byte()
        if ln is None:
            return ""
        end = self.a + ln
        out = []
        while self.a < end:
            e = self._byte()
            if e is None:
                e = 0
            if e < 128:
                out.append(chr(e))
            elif not e < 192:
                if e < 224:
                    h = self._byte() or 0
                    out.append(chr(((e & 31) << 6) | (h & 63)))
                elif e < 240:
                    h = self._byte() or 0
                    k = self._byte() or 0
                    out.append(chr(((e & 15) << 12) | ((h & 63) << 6) | (k & 63)))
        return "".join(out)

    def reset(self):
        self.a = 0
        self.o = 0
        self.c = self.n
        self.h = []


def _sc(c):
    c = _i32(c) if c == c else 0
    for e in (6, 1):
        if c & (1 << (e - 1)):
            return e
    return 6


class Bulk:
    __slots__ = ("childIndices", "epoch", "bulkMetadataEpoch", "flags", "imageryEpochArray",
                 "textureFormatArray", "defaultImageryEpoch", "defaultTextureFormat", "headNodePath")


def decode_bulk(data):
    """Decode a PlanetoidMetadata or BulkMetadata response (command 0)."""
    r = _JsReader(bytes(data))
    blk = Bulk()
    blk.headNodePath = ""
    blk.defaultImageryEpoch = 0
    blk.defaultTextureFormat = 6
    head_epoch = 0
    paths = []

    def bd():
        e = ""
        while True:
            d = r.D()
            if not d:
                break
            if d == 1:
                v = r.f()
                pf = _i32(v) if v == v else 0
                n = (pf & 3) + 1
                v2 = (pf >> 2) & ((1 << (3 * n)) - 1)
                e = "".join(str((v2 >> (3 * k)) & 7) for k in range(n))
            else:
                r.B()
        return e

    def vc():
        nonlocal head_epoch
        while True:
            e = r.D()
            if not e:
                break
            if e == 1:
                blk.headNodePath = r.cd()
            elif e == 2:
                head_epoch = r.f()
            else:
                r.B()

    while True:
        d = r.D()
        if not d:
            break
        if d == 1:
            n = r.f()
            r.O(r.a + n)
            paths.append(bd())
            r.N()
        elif d == 2:
            n = r.f()
            r.O(r.a + n)
            vc()
            r.N()
        elif d == 3:
            r.f()
            r.float64()
            r.float64()
            r.float64()
        elif d == 4:
            r.f()
            r.float32()
            r.float32()
            r.float32()
            r.float32()
        elif d == 5:
            blk.defaultImageryEpoch = r.f()
        elif d == 6:
            blk.defaultTextureFormat = _sc(r.f())
        elif d == 7:
            r.f()
        elif d == 8:
            _sc(r.f())
        else:
            r.B()
    r.reset()
    order = sorted(paths, key=lambda p: (len(p), p))
    index_of = {}
    for k, p in enumerate(order):
        index_of[p] = k
    k = len(paths)
    epoch = [0] * k
    bme = [0] * k
    flags = [0] * k
    img = [0] * k
    tex = [0] * k
    seen_img = False
    seen_tex = False
    short = 0
    i = 0
    while True:
        d = r.D()
        if not d:
            break
        if d == 1:
            n = r.f()
            r.O(r.a + n)
            path = paths[i] if i < len(paths) else None
            i += 1
            A = index_of.get(path)
            img[A] = _u32(blk.defaultImageryEpoch)
            tex[A] = _u8(blk.defaultTextureFormat)
            kf = 0
            z = head_epoch
            bb = head_epoch
            while True:
                h = r.D()
                if not h:
                    break
                if h == 1:
                    kf = r.f()
                elif h == 3:
                    r.f()
                    for _ in range(3):
                        r.Ka()
                    for _ in range(3):
                        r.Ia()
                    for _ in range(3):
                        r.ia()
                elif h == 4:
                    r.float32()
                elif h == 2:
                    z = r.f()
                elif h == 5:
                    bb = r.f()
                elif h == 7:
                    seen_img = True
                    img[A] = _u32(r.f())
                elif h == 8:
                    seen_tex = True
                    tex[A] = _u8(_sc(r.f()))
                elif h == 9:
                    r.f()
                elif h == 10:
                    _sc(r.f())
                else:
                    r.B()
            c = len(path)
            if c < 4:
                short += 1
            epoch[A] = _u32(z)
            bme[A] = _u32(bb)
            kv = _i32(kf) if kf == kf else 0
            flags[A] = _u8(kv >> ((2 + 3 * c) & 31))
            r.N()
        else:
            r.B()
    ci = [0] * (8 * (short + 1))

    def ab(c, d):
        if len(c) == 4:
            return
        for e in range(8):
            hp = c + str(e)
            kk = index_of.get(hp)
            if kk is not None:
                ab(hp, kk)
            else:
                kk = -1
            idx = 8 * (d + 1) + e
            if 0 <= idx < len(ci):
                ci[idx] = _i16(kk)
    ab("", -1)
    blk.childIndices = ci
    blk.epoch = epoch
    blk.bulkMetadataEpoch = bme
    blk.flags = flags
    blk.imageryEpochArray = img if seen_img else None
    blk.textureFormatArray = tex if seen_tex else None
    return blk


# ------------------------------------------------------------------ utils.js bulk helpers

def _at(arr, i):
    if i is None or arr is None or not (0 <= i < len(arr)):
        return None
    return arr[i]


def has_node_at_index(bulk, index):
    f = _at(bulk.flags, index)
    return not ((f or 0) & 8)


def has_bulk_metadata_at_index(bulk, index):
    f = _at(bulk.flags, index)
    return bool((f or 0) & 4)


def get_index_by_path(bulk, path):
    c = -1
    n = len(path)
    if n == 0:
        return -1
    ci = bulk.childIndices
    for f in range((n - 1) - ((n - 1) % 4), n):
        if c is None:
            return None
        c = _at(ci, 8 * (c + 1) + (ord(path[f]) - 48))
    return c
