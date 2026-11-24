"""
Decoded node -> binary glTF (.glb) for 3D Tiles, byte-identical to the webapp's native converter
(re/native/src/glb.rs, itself a port of re/exporter/lib/glb-writer.js). Includes the fallback for
tiles without textured geometry: geometry cut from the nearest usable ancestor tile.
"""

import math
import struct
from array import array

from . import decode, fdlibm, png
from .jsfmt import js_num
from .octant import box_of_path

GLB_VERSION = 2
SPHERE_RADIUS = 6371010.0
WGS84_A = 6378137.0
WGS84_E2 = 0.0066943799901413165
DEG = 180.0 / math.pi
SKIP_NO_ANCESTOR = "no textured geometry, and no usable ancestor tile in the library"

_asin, _atan2, _sin, _cos, _hypot3 = fdlibm.asin, fdlibm.atan2, fdlibm.sin, fdlibm.cos, fdlibm.hypot3
_sqrt = math.sqrt
_NAN = float("nan")


class Tile:
    __slots__ = ("glb", "center", "half", "tex_width")

    def meta_json(self):
        return '{"center":%s,"half":%s,"texWidth":%d}' % (_arr(self.center), _arr(self.half), self.tex_width)


def _arr(v):
    return "[" + ",".join(js_num(x) for x in v) + "]"


def sphere_to_wgs84(x, y, z):
    r = _sqrt(x * x + y * y + z * z)
    lat = _asin(z / r) if r else _asin(_NAN)
    lon = _atan2(y, x)
    h = r - SPHERE_RADIUS
    sl = _sin(lat)
    cl = _cos(lat)
    n = WGS84_A / _sqrt(1.0 - WGS84_E2 * sl * sl)
    return ((n + h) * cl * _cos(lon), (n + h) * cl * _sin(lon), (n * (1.0 - WGS84_E2) + h) * sl)


def strip_to_triangles(mesh):
    idx = mesh.indices
    out = []
    stop = mesh.layer_bounds[3]
    for i in range(max(len(idx) - 2, 0)):
        if i == stop:
            break
        a, b, c = idx[i], idx[i + 1], idx[i + 2]
        if a == b or a == c or b == c:
            continue
        if i & 1:
            out += (a, c, b)
        else:
            out += (a, b, c)
    return out


class Piece:
    __slots__ = ("mesh", "g", "ng", "uv", "tris")

    def n(self):
        return len(self.g) // 3


def mesh_piece(m, mesh, verts, tris):
    n = len(verts)
    g = [0.0] * (n * 3)
    ng = [0.0] * (n * 3)
    uv = [0.0] * (n * 2)
    nb = mesh.normals
    lnb = len(nb)
    vt = mesh.vertices
    os_ = mesh.uv_offset_and_scale
    m0, m1, m2, m4, m5, m6, m8, m9, m10, m12, m13, m14 = (m[0], m[1], m[2], m[4], m[5], m[6], m[8], m[9],
                                                          m[10], m[12], m[13], m[14])
    for j, i in enumerate(verts):
        b8 = i * 8
        x, y, z = float(vt[b8]), float(vt[b8 + 1]), float(vt[b8 + 2])
        j3 = j * 3
        g[j3] = x * m0 + y * m4 + z * m8 + m12
        g[j3 + 1] = x * m1 + y * m5 + z * m9 + m13
        g[j3 + 2] = x * m2 + y * m6 + z * m10 + m14
        b4 = i * 4
        nx = nb[b4] - 127.0 if b4 < lnb else _NAN
        ny = nb[b4 + 1] - 127.0 if b4 + 1 < lnb else _NAN
        nz = nb[b4 + 2] - 127.0 if b4 + 2 < lnb else _NAN
        gx = nx * m0 + ny * m4 + nz * m8
        gy = nx * m1 + ny * m5 + nz * m9
        gz = nx * m2 + ny * m6 + nz * m10
        h = _hypot3(gx, gy, gz)
        ln = 1.0 if (h == 0.0 or h != h) else h
        ng[j3] = gx / ln
        ng[j3 + 1] = gy / ln
        ng[j3 + 2] = gz / ln
        if os_ is not None:
            u = vt[b8 + 5] * 256.0 + vt[b8 + 4]
            v = vt[b8 + 7] * 256.0 + vt[b8 + 6]
            uv[j * 2] = (u + os_[0]) * os_[2]
            uv[j * 2 + 1] = 1.0 - (v + os_[1]) * os_[3]
    p = Piece()
    p.mesh, p.g, p.ng, p.uv, p.tris = mesh, g, ng, uv, tris
    return p


def has_meshes(node):
    return decode.has_meshes(node)


def node_to_glb(node, exact_png=True):
    m = node.matrix
    pieces = [mesh_piece(m, mesh, range(len(mesh.vertices) // 8), strip_to_triangles(mesh)) for mesh in node.meshes]
    return build_glb(pieces, exact_png)


def _texture_image(mesh, exact_png):
    tex = mesh.texture
    if tex is None:
        raise ValueError("mesh has no texture")
    if tex.format == 1:
        return "image/jpeg", tex.bytes, tex.width
    if tex.format == 6:
        return "image/png", png.png_from_dxt1(tex.bytes, tex.width, tex.height, exact_png), tex.width
    raise ValueError("unknown textureFormat %d" % tex.format)


def build_glb(pieces, exact_png=True):
    inf = float("inf")
    mn = [inf, inf, inf]
    mx = [-inf, -inf, -inf]
    ecefs = []
    for pc in pieces:
        g = pc.g
        e = [0.0] * len(g)
        for j in range(0, len(g), 3):
            p = sphere_to_wgs84(g[j], g[j + 1], g[j + 2])
            for k in range(3):
                v = p[k]
                e[j + k] = v
                if v < mn[k]:
                    mn[k] = v
                if v > mx[k]:
                    mx[k] = v
        ecefs.append(e)
    if not (mn[0] == mn[0] and abs(mn[0]) != inf):
        raise ValueError("node has no vertices")
    center = [(mn[k] + mx[k]) / 2.0 for k in range(3)]
    half = [(mx[k] - mn[k]) / 2.0 for k in range(3)]

    data = bytearray()
    views = []

    def add(b, target=None):
        pad = (4 - len(data) % 4) % 4
        data.extend(b"\x00" * pad)
        v = '{"buffer":0,"byteOffset":%d,"byteLength":%d' % (len(data), len(b))
        if target is not None:
            v += ',"target":%d' % target
        views.append(v + "}")
        data.extend(b)
        return len(views) - 1

    accessors, images, textures, materials, primitives = [], [], [], [], []
    max_tex_w = 0
    cx, cy, cz = center
    for pc, e in zip(pieces, ecefs):
        n = len(e) // 3
        pos = [0.0] * (n * 3)
        lo = [inf, inf, inf]
        hi = [-inf, -inf, -inf]
        for j in range(0, n * 3, 3):
            x = e[j] - cx
            y = e[j + 1] - cy
            z = e[j + 2] - cz
            ny = -y
            pos[j] = x
            pos[j + 1] = z
            pos[j + 2] = ny
            if x < lo[0]:
                lo[0] = x
            if x > hi[0]:
                hi[0] = x
            if z < lo[1]:
                lo[1] = z
            if z > hi[1]:
                hi[1] = z
            if ny < lo[2]:
                lo[2] = ny
            if ny > hi[2]:
                hi[2] = ny
        ng = pc.ng
        nrm = [0.0] * (n * 3)
        for j in range(0, n * 3, 3):
            nrm[j] = ng[j]
            nrm[j + 1] = ng[j + 2]
            nrm[j + 2] = -ng[j + 1]
        tris = pc.tris
        use32 = n > 65535
        idx_bytes = array("I" if use32 else "H", [t & (0xFFFFFFFF if use32 else 0xFFFF) for t in tris]).tobytes()
        v_pos = add(array("f", pos).tobytes(), 34962)
        accessors.append('{"bufferView":%d,"componentType":5126,"count":%d,"type":"VEC3","min":%s,"max":%s}'
                         % (v_pos, n, _arr(lo), _arr(hi)))
        acc_pos = len(accessors) - 1
        v_nrm = add(array("f", nrm).tobytes(), 34962)
        accessors.append('{"bufferView":%d,"componentType":5126,"count":%d,"type":"VEC3"}' % (v_nrm, n))
        acc_nrm = len(accessors) - 1
        v_uv = add(array("f", pc.uv).tobytes(), 34962)
        accessors.append('{"bufferView":%d,"componentType":5126,"count":%d,"type":"VEC2"}' % (v_uv, n))
        acc_uv = len(accessors) - 1
        v_idx = add(idx_bytes, 34963)
        accessors.append('{"bufferView":%d,"componentType":%d,"count":%d,"type":"SCALAR"}'
                         % (v_idx, 5125 if use32 else 5123, len(tris)))
        acc_idx = len(accessors) - 1
        mime, img, width = _texture_image(pc.mesh, exact_png)
        max_tex_w = max(max_tex_w, width)
        v_img = add(img)
        images.append('{"bufferView":%d,"mimeType":"%s"}' % (v_img, mime))
        textures.append('{"sampler":0,"source":%d}' % (len(images) - 1))
        materials.append('{"pbrMetallicRoughness":{"baseColorTexture":{"index":%d},"metallicFactor":0,'
                         '"roughnessFactor":1},"doubleSided":true,"extensions":{"KHR_materials_unlit":{}}}'
                         % (len(textures) - 1))
        primitives.append('{"attributes":{"POSITION":%d,"NORMAL":%d,"TEXCOORD_0":%d},"indices":%d,"material":%d,"mode":4}'
                          % (acc_pos, acc_nrm, acc_uv, acc_idx, len(materials) - 1))
    while len(data) % 4:
        data.append(0)
    js = ('{"asset":{"version":"2.0","generator":"earth-tile-library"},"extensionsUsed":["KHR_materials_unlit"],'
          '"scene":0,"scenes":[{"nodes":[0]}],"nodes":[{"mesh":0}],"meshes":[{"primitives":[%s]}],'
          '"materials":[%s],"textures":[%s],"images":[%s],'
          '"samplers":[{"magFilter":9729,"minFilter":9987,"wrapS":33071,"wrapT":33071}],'
          '"accessors":[%s],"bufferViews":[%s],"buffers":[{"byteLength":%d}]}'
          % (",".join(primitives), ",".join(materials), ",".join(textures), ",".join(images),
             ",".join(accessors), ",".join(views), len(data))).encode("utf8")
    js += b" " * ((4 - len(js) % 4) % 4)
    total = 12 + 8 + len(js) + 8 + len(data)
    glb = b"glTF" + struct.pack("<II", 2, total) + struct.pack("<I", len(js)) + b"JSON" + js \
        + struct.pack("<I", len(data)) + b"BIN\x00" + bytes(data)
    t = Tile()
    t.glb, t.center, t.half, t.tex_width = glb, center, half, (256 if max_tex_w == 0 else max_tex_w)
    return t


# ------------------------------------------------------------------ ancestor fallback

def _strip_range(indices, frm, to):
    out = []
    end = min(to, len(indices)) - 2
    for k in range(frm, end):
        a, b, c = indices[k], indices[k + 1], indices[k + 2]
        if a == b or a == c or b == c:
            continue
        if k & 1:
            out += (a, c, b)
        else:
            out += (a, b, c)
    return out


def _quarter_triangles(mesh, quarter):
    oc, lb = mesh.octant_counts, mesh.layer_bounds
    out = []
    for layer in range(3):
        start = lb[layer]
        for o in range(8):
            count = oc[layer * 8 + o]
            if (o & 3) == quarter and count > 0:
                out += _strip_range(mesh.indices, start, start + count)
            start += count
    return out


def _compact(m, mesh, raw):
    mp = {}
    verts = []
    tris = []
    for i in raw:
        j = mp.get(i)
        if j is None:
            j = len(verts)
            mp[i] = j
            verts.append(i)
        tris.append(j)
    return mesh_piece(m, mesh, verts, tris)


def _clip_to_box(pc, b):
    n = pc.n()
    g, ng, uv = list(pc.g), list(pc.ng), list(pc.uv)
    vlat = [0.0] * n
    vlon = [0.0] * n
    pg = pc.g
    for j in range(n):
        x, y, z = pg[j * 3], pg[j * 3 + 1], pg[j * 3 + 2]
        vlat[j] = _asin(z / _sqrt(x * x + y * y + z * z)) * DEG
        vlon[j] = _atan2(y, x) * DEG
    bw, be, bs, bn = b["w"], b["e"], b["s"], b["n"]

    def inside(la, lo):
        return lo >= bw and lo <= be and la >= bs and la <= bn

    edges = (lambda k: vlon[k] - bw, lambda k: be - vlon[k], lambda k: vlat[k] - bs, lambda k: bn - vlat[k])
    tris = []
    pt = pc.tris
    for t in range(0, len(pt) - len(pt) % 3, 3):
        a, bb, c = pt[t], pt[t + 1], pt[t + 2]
        if inside(vlat[a], vlon[a]) and inside(vlat[bb], vlon[bb]) and inside(vlat[c], vlon[c]):
            tris += (a, bb, c)
            continue
        poly = [a, bb, c]
        for f in edges:
            out = []
            ln = len(poly)
            for i in range(ln):
                p, q = poly[i], poly[(i + 1) % ln]
                fp, fq = f(p), f(q)
                if fp >= 0.0:
                    out.append(p)
                if (fp >= 0.0) != (fq >= 0.0):
                    tt = fp / (fp - fq)
                    k = len(vlat)
                    for cc in range(3):
                        g.append(g[p * 3 + cc] + tt * (g[q * 3 + cc] - g[p * 3 + cc]))
                    nx = ng[p * 3] + tt * (ng[q * 3] - ng[p * 3])
                    ny = ng[p * 3 + 1] + tt * (ng[q * 3 + 1] - ng[p * 3 + 1])
                    nz = ng[p * 3 + 2] + tt * (ng[q * 3 + 2] - ng[p * 3 + 2])
                    l = _sqrt(nx * nx + ny * ny + nz * nz)
                    ln2 = 1.0 if (l == 0.0 or l != l) else l
                    ng += (nx / ln2, ny / ln2, nz / ln2)
                    for cc in range(2):
                        uv.append(uv[p * 2 + cc] + tt * (uv[q * 2 + cc] - uv[p * 2 + cc]))
                    vlat.append(vlat[p] + tt * (vlat[q] - vlat[p]))
                    vlon.append(vlon[p] + tt * (vlon[q] - vlon[p]))
                    out.append(k)
            poly = out
            if len(poly) < 3:
                break
        for i in range(1, max(len(poly) - 1, 1)):
            tris += (poly[0], poly[i], poly[i + 1])
    if not tris:
        return None
    mp = {}
    og, ong, ouv, otris = [], [], [], []
    for k in tris:
        j = mp.get(k)
        if j is None:
            j = len(mp)
            mp[k] = j
            og += g[k * 3:k * 3 + 3]
            ong += ng[k * 3:k * 3 + 3]
            ouv += uv[k * 2:k * 2 + 2]
        otris.append(j)
    out = Piece()
    out.mesh, out.g, out.ng, out.uv, out.tris = pc.mesh, og, ong, ouv, otris
    return out


def ancestor_pieces(ancestor, ancestor_path, tile_path, box):
    """Pieces of `ancestor` that cover `tile_path`; None when it has nothing there."""
    if not decode.has_meshes(ancestor):
        return None
    quarter = (ord(tile_path[len(ancestor_path)]) - 48) & 3
    direct = len(tile_path) == len(ancestor_path) + 1
    m = ancestor.matrix
    pieces = []
    for mesh in ancestor.meshes:
        if len(mesh.vertices) < 8:
            continue
        raw = _quarter_triangles(mesh, quarter)
        if not raw:
            continue
        pc = _compact(m, mesh, raw)
        if not direct:
            pc = _clip_to_box(pc, box) if box is not None else None
        if pc is not None:
            pieces.append(pc)
    return pieces or None


def cut_from_ancestors(tile_path, box, load, exact_png=True):
    """(Tile, ancestor path) from the nearest usable ancestor; load(path) -> raw bytes or None."""
    a = tile_path[:-1]
    while len(a) >= 2:
        raw = load(a)
        if raw is not None:
            try:
                node = decode.decode_node(raw)
            except Exception as e:
                raise ValueError("tile %s: %s" % (a, e))
            pieces = ancestor_pieces(node, a, tile_path, box)
            if pieces:
                return build_glb(pieces, exact_png), a
        a = a[:-1]
    return None


def convert_raw_with(raw, path, load, exact_png=True):
    """Convert a stored tile. Returns Tile, or a str (the reason) when the tile is left out."""
    node = decode.decode_node(raw)
    if decode.has_meshes(node):
        return node_to_glb(node, exact_png)
    box = box_of_path(path)
    if box is None:
        raise ValueError("invalid tile path")
    r = cut_from_ancestors(path, box, load, exact_png)
    return r[0] if r else SKIP_NO_ANCESTOR
