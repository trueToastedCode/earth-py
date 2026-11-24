"""3D Tiles 1.1 tileset from tile paths + per-tile metadata (port of re/exporter/lib/tileset.js)."""

import math

from . import fdlibm, v8math
from .octant import box_of_path, child_cells

RAD = math.pi / 180
R = 6371008.8


def _js_max(*vals):
    out = -math.inf
    for v in vals:
        if v != v:
            return math.nan
        if v > out:
            out = v
    return out


def _js_min(*vals):
    out = math.inf
    for v in vals:
        if v != v:
            return math.nan
        if v < out:
            out = v
    return out


def sort_paths(paths):
    return sorted(paths, key=lambda p: (len(p), p))


def build_hierarchy(paths):
    sel = set(paths)
    prefixes = set()
    for p in paths:
        for i in range(2, len(p)):
            prefixes.add(p[:i])
    memo = {}

    def covered(p):
        if p in memo:
            return memo[p]
        r = False
        if p in sel:
            r = True
        elif p in prefixes:
            box = box_of_path(p)
            r = bool(box) and all(covered(p + str(k)) or covered(p + str(k + 4)) for k, _ in child_cells(box))
        memo[p] = r
        return r

    def nearest(p):
        out = []
        box = box_of_path(p)
        if not box:
            return out
        for k, _ in child_cells(box):
            for kk in (k, k + 4):
                c = p + str(kk)
                if c in sel:
                    out.append(c)
                elif c in prefixes:
                    out.extend(nearest(c))
        return out

    children = {}
    has_parent = set()
    for p in paths:
        box = box_of_path(p)
        if not box or p not in prefixes:
            continue
        if not all(covered(p + str(k)) or covered(p + str(k + 4)) for k, _ in child_cells(box)):
            continue
        kids = nearest(p)
        if not kids:
            continue
        children[p] = kids
        has_parent.update(kids)
    return children, [p for p in paths if p not in has_parent]


def ground_size(p):
    b = box_of_path(p)
    lat = ((b["n"] + b["s"]) / 2) * RAD
    return _js_max((b["e"] - b["w"]) * RAD * R * v8math.cos(lat), (b["n"] - b["s"]) * RAD * R)


def _translation(t):
    return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, t[0], t[1], t[2], 1]


def build_tileset(paths, meta_of):
    paths = sort_paths(paths)
    meta = {}
    for p in paths:
        m = meta_of(p)
        meta[p] = {"center": [float(x) for x in m["center"]], "half": [float(x) for x in m["half"]],
                   "ge": ground_size(p) / (m.get("texWidth") or 256)}
    children, tops = build_hierarchy(paths)

    def tile(p, parent_center):
        m = meta[p]
        kids = children.get(p) or []
        c = m["center"]
        h = m["half"]
        t = {
            "transform": _translation([c[0] - parent_center[0], c[1] - parent_center[1], c[2] - parent_center[2]]),
            "boundingVolume": {"box": [0, 0, 0, h[0], 0, 0, 0, h[1], 0, 0, 0, h[2]]},
            "geometricError": m["ge"] if kids else 0,
            "refine": "REPLACE",
            "content": {"uri": "tiles/" + p + ".glb"},
        }
        if kids:
            t["children"] = [tile(k, c) for k in kids]
        return t

    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    for m in meta.values():
        for k in range(3):
            lo[k] = _js_min(lo[k], m["center"][k] - m["half"][k])
            hi[k] = _js_max(hi[k], m["center"][k] + m["half"][k])
    rc = [(lo[k] + hi[k]) / 2 for k in range(3)]
    root_ge = _js_max(fdlibm.hypot3(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]), *[meta[p]["ge"] for p in tops])
    return {
        "asset": {"version": "1.1", "generator": "earth-tile-library"},
        "geometricError": root_ge,
        "root": {
            "transform": _translation(rc),
            "boundingVolume": {"box": [0, 0, 0, (hi[0] - lo[0]) / 2, 0, 0, 0, (hi[1] - lo[1]) / 2, 0, 0, 0, (hi[2] - lo[2]) / 2]},
            "geometricError": root_ge,
            "refine": "ADD",
            "children": [tile(p, rc) for p in tops],
        },
    }
