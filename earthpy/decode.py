"""
Decode a stored tile (NodeData protobuf, see the webapp's re/proto/rocktree.proto) into meshes.
Port of the webapp's native decoder (re/native/src/decode.rs + pb.rs), itself a port of the parts
of Google's minified decoder that the GLB export uses.
"""

import math
import struct

from . import crn

_f32 = struct.Struct("<f")
_f64 = struct.Struct("<d")


def f32(x):
    """Round a double to float32 and back (Float32Array store / Rust `as f32`)."""
    try:
        return _f32.unpack(_f32.pack(x))[0]
    except OverflowError:
        return float("inf") if x > 0 else float("-inf")


def fdiv(a, b):
    """IEEE division (no ZeroDivisionError)."""
    if b == 0.0:
        if a != a or a == 0.0:
            return float("nan")
        neg = (a < 0) != (str(b)[0] == "-")
        return float("-inf") if neg else float("inf")
    return a / b


class DecodeError(Exception):
    pass


class Reader:
    __slots__ = ("data", "pos", "limit", "limits", "wire")

    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.limit = len(data)
        self.limits = []
        self.wire = 0

    def next_field(self):
        if self.pos < self.limit:
            tag = self.varint()
            self.wire = tag & 7
            return tag >> 3
        return 0

    def byte(self):
        p = self.pos
        if p >= len(self.data):
            raise DecodeError("unexpected end of tile data")
        self.pos = p + 1
        return self.data[p]

    def varint(self):
        data = self.data
        p = self.pos
        n = len(data)
        value = 0
        shift = 0
        while True:
            if p >= n:
                raise DecodeError("unexpected end of tile data")
            b = data[p]
            p += 1
            if shift < 64:
                value |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                self.pos = p
                return value & 0xFFFFFFFFFFFFFFFF

    def len(self):
        n = self.varint()
        if self.pos + n > len(self.data):
            raise DecodeError("field runs past the end of the tile data")
        return n

    def u16le(self):
        lo = self.byte()
        hi = self.byte()
        return (hi << 8) | lo

    def take(self, n):
        end = self.pos + n
        if end > len(self.data):
            raise DecodeError("unexpected end of tile data")
        s = self.data[self.pos:end]
        self.pos = end
        return s

    def skip(self, n):
        end = self.pos + n
        if end > len(self.data):
            raise DecodeError("unexpected end of tile data")
        self.pos = end

    def skip_field(self):
        w = self.wire
        if w == 0:
            self.varint()
        elif w == 1:
            self.skip(8)
        elif w == 2:
            self.skip(self.len())
        elif w == 5:
            self.skip(4)
        else:
            raise DecodeError("unsupported protobuf wire type %d" % w)

    def push(self, n):
        self.limits.append(self.limit)
        self.limit = self.pos + n

    def pop(self):
        if self.limits:
            self.limit = self.limits.pop()


class Texture:
    __slots__ = ("format", "bytes", "width", "height")


class Mesh:
    __slots__ = ("vertices", "indices", "texture", "uv_offset_and_scale", "layer_bounds",
                 "octant_counts", "normals", "raw_normals")

    def __init__(self):
        self.vertices = bytearray()
        self.indices = []
        self.texture = None
        self.uv_offset_and_scale = None
        self.layer_bounds = [0] * 10
        self.octant_counts = [0] * 72
        self.normals = b""
        self.raw_normals = None


class Node:
    __slots__ = ("matrix", "meshes")


def decode_node(data, textures=True):
    """Fd.prototype.Fa. textures=False skips the (slow) crunch decoding."""
    r = Reader(data)
    matrix = [0.0] * 16
    meshes = []
    normal_table = None
    while True:
        f = r.next_field()
        if f == 0:
            break
        if f == 1:
            n = r.len() // 8
            for i in range(n):
                v = _f64.unpack(r.take(8))[0]
                if i < 16:
                    matrix[i] = v
        elif f == 2:
            n = r.len()
            r.push(n)
            meshes.append(_decode_mesh(r, textures))
            r.pop()
        elif f == 8:
            normal_table = _decode_normal_table(r)
        else:
            r.skip_field()
    for m in meshes:
        _assign_normals(m, normal_table)
    node = Node()
    node.matrix = matrix
    node.meshes = meshes
    return node


def _decode_mesh(r, textures):
    m = Mesh()
    while True:
        f = r.next_field()
        if f == 0:
            break
        if f == 1:
            _decode_vertices(r, m)
        elif f == 3:
            _decode_indices(r, m)
        elif f == 6:
            n = r.len()
            r.push(n)
            m.texture = _decode_texture(r, textures)
            r.pop()
        elif f == 7:
            _decode_texture_coords(r, m)
        elif f == 8:
            _decode_layer_bounds(r, m)
        elif f == 10:
            n = r.len() // 4
            os_ = [0.0, 0.0, 0.0, 0.0]
            for i in range(n):
                v = _f32.unpack(r.take(4))[0]
                if i < 4:
                    os_[i] = v
            m.uv_offset_and_scale = os_
        elif f == 11:
            n = r.len()
            m.raw_normals = r.take(n)
        else:
            r.skip_field()
    os_ = m.uv_offset_and_scale
    if os_ is not None:
        os_[1] = f32(os_[1] - fdiv(1.0, os_[3]))
        os_[3] = -os_[3]
    return m


def _decode_vertices(r, m):
    n = r.len()
    src = r.take(n)
    count = n // 3
    v = bytearray(8 * count)
    for ch in range(3):
        s = src[ch * count:(ch + 1) * count]
        acc = 0
        o = ch
        for i in range(count):
            acc = (acc + s[i]) & 0xFF if i else s[0]
            v[o] = acc
            o += 8
    m.vertices = v


def _decode_indices(r, m):
    r.varint()
    count = r.varint()
    idx = []
    nxt = 0
    for _ in range(count):
        back = r.varint()
        idx.append((nxt - back) & 0xFFFF)
        if back == 0:
            nxt += 1
    m.indices = idx


def _decode_texture_coords(r, m):
    n = r.len()
    end = r.pos + n
    count = max(n - 4, 0) // 4
    umax = r.u16le()
    vmax = r.u16le()
    d = r.data[r.pos:end]
    u = v = 0
    um = umax + 1
    vm = vmax + 1
    vt = m.vertices
    lv = len(vt)
    for b in range(count):
        du = d[b] + (d[2 * count + b] << 8)
        dv = d[count + b] + (d[3 * count + b] << 8)
        u = (u + du) % um
        v = (v + dv) % vm
        o = 8 * b + 4
        if o + 4 <= lv:
            vt[o] = u & 0xFF
            vt[o + 1] = (u >> 8) & 0xFF
            vt[o + 2] = v & 0xFF
            vt[o + 3] = (v >> 8) & 0xFF
    if m.uv_offset_and_scale is None:
        m.uv_offset_and_scale = [0.5, 0.5, f32(1.0 / (umax + 1.0)), f32(1.0 / (vmax + 1.0))]
    r.pos = end


def _decode_layer_bounds(r, m):
    r.varint()
    count = r.varint()
    total = 0
    layer = 0
    lb = m.layer_bounds
    oc = m.octant_counts
    for i in range(count):
        if i % 8 == 0:
            if layer < 10:
                lb[layer] = total & 0xFFFFFFFF
            layer += 1
        c = r.varint()
        slot = 8 * (layer - 1) + (i & 7)
        if slot < 72:
            oc[slot] = c & 0xFFFFFFFF
        total += c
    while layer < 10:
        lb[layer] = total & 0xFFFFFFFF
        layer += 1


def _decode_texture(r, textures):
    fmt, width, height = 1, 256, 256
    data = None
    while True:
        f = r.next_field()
        if f == 0:
            break
        if f == 1:
            n = r.len()
            start = data[0] if data else r.pos  # like the JS: first offset, last length
            data = (start, n)
            r.skip(n)
        elif f == 2:
            fmt = r.varint() & 0xFFFFFFFF
        elif f == 3:
            width = r.varint() & 0xFFFFFFFF
        elif f == 4:
            height = r.varint() & 0xFFFFFFFF
        else:
            r.skip_field()
    start, n = data if data else (0, 0)
    if start + n > len(r.data):
        raise DecodeError("texture data out of range")
    src = r.data[start:start + n]
    t = Texture()
    t.format = fmt
    t.width = width
    t.height = height
    if fmt == 1:
        t.bytes = bytes(src)
    elif fmt == 6:
        t.bytes = crn.crn_to_dxt1(bytes(src)) if textures else None
    else:
        raise DecodeError("unknown textureFormat %d" % fmt)
    return t


def _expand_bits(c, d):
    if d <= 4:
        return (c << d) + (c & ((1 << d) - 1))
    if d <= 6:
        e = 8 - d
        s = c << d
        return s + (s >> e) + (s >> e >> e) + (s >> e >> e >> e)
    return -(c & 1)


def _js_min(c, d):
    return c if c < d else d


def _to_byte(x):
    if x != x or x == float("inf"):
        return 255
    if x == float("-inf"):
        return 0
    r = math.floor(x + 0.5)
    if 0.0 > r:
        r = 0.0
    return int(r) if r < 255.0 else 255


def _decode_normal_table(r):
    n = r.len()
    d = r.take(n)
    if len(d) < 3:
        return b""
    count = d[0] | (d[1] << 8)
    bits = d[2]
    d = d[3:]
    table = bytearray(3 * count)
    for i in range(count):
        if i >= len(d) or count + i >= len(d):
            break
        p = _expand_bits(d[i], bits)
        q = _expand_bits(d[count + i], bits)
        z0 = p / 255.0
        b0 = q / 255.0
        a, g = z0, b0
        c, rr = a + g, a - g
        sign = 1.0
        if not (0.5 <= c and 1.5 >= c and -0.5 <= rr and 0.5 >= rr):
            sign = -1.0
            if 0.5 >= c:
                a = 0.5 - b0
                g = 0.5 - z0
            elif 1.5 <= c:
                a = 1.5 - b0
                g = 1.5 - z0
            elif -0.5 >= rr:
                a = b0 - 0.5
                g = z0 + 0.5
            else:
                a = b0 + 0.5
                g = z0 - 0.5
            c = a + g
            rr = a - g
        z = _js_min(_js_min(2.0 * c - 1.0, 3.0 - 2.0 * c), _js_min(2.0 * rr + 1.0, 1.0 - 2.0 * rr)) * sign
        a = 2.0 * a - 1.0
        g = 2.0 * g - 1.0
        s = fdiv(127.0, math.sqrt(z * z + a * a + g * g))
        table[3 * i] = _to_byte(s * z + 127.0)
        table[3 * i + 1] = _to_byte(s * a + 127.0)
        table[3 * i + 2] = _to_byte(s * g + 127.0)
    return bytes(table)


def _assign_normals(m, table):
    raw = m.raw_normals
    m.raw_normals = None
    if raw is not None and table is not None:
        n = len(raw) // 2
        out = bytearray(4 * n)
        lt = len(table)
        for k in range(n):
            e = raw[k] | (raw[n + k] << 8)
            for c in range(3):
                j = 3 * e + c
                out[4 * k + c] = table[j] if j < lt else 0
        m.normals = bytes(out)
    else:
        n = len(m.vertices) // 8
        m.normals = b"\x7f\x7f\x7f\x00" * n


def has_meshes(node):
    """True when the node has textured meshes with vertices (the normal case)."""
    return any(len(m.vertices) >= 8 for m in node.meshes)


# ------------------------------------------------------------- quick check (tile-check.js)

def has_textured_geometry(buf):
    """Does a stored tile contain textured geometry? True / False, or None when unparseable."""
    try:
        found = [False]

        def outer(field, start, end):
            if field != 2:
                return

            def inner(f, s, e):
                if f == 1 and e - s >= 3:
                    found[0] = True
            _walk(buf, start, end, inner)
        _walk(buf, 0, len(buf), outer)
        return found[0]
    except Exception:
        return None


def _walk(buf, frm, to, fn):
    o = frm

    def varint():
        nonlocal o
        v = 0
        mul = 1
        while True:
            if o >= to:
                raise ValueError("truncated")
            b = buf[o]
            o += 1
            v += (b & 127) * mul
            mul *= 128
            if not b & 128:
                return v
    while o < to:
        tag = varint()
        field, wire = tag // 8, tag & 7
        if wire == 0:
            varint()
        elif wire == 1:
            o += 8
        elif wire == 5:
            o += 4
        elif wire == 2:
            ln = varint()
            if o + ln > to:
                raise ValueError("truncated")
            fn(field, o, o + ln)
            o += ln
        else:
            raise ValueError("unsupported wire type %d" % wire)
        if o > to:
            raise ValueError("truncated")
