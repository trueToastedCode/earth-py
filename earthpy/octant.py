"""Octant-path geometry (port of re/exporter/lib/octant-geometry.js)."""

ROOTS = (
    ("02", {"n": 0, "s": -90, "w": -180, "e": -90}),
    ("03", {"n": 0, "s": -90, "w": -90, "e": 0}),
    ("12", {"n": 0, "s": -90, "w": 0, "e": 90}),
    ("13", {"n": 0, "s": -90, "w": 90, "e": 180}),
    ("20", {"n": 90, "s": 0, "w": -180, "e": -90}),
    ("21", {"n": 90, "s": 0, "w": -90, "e": 0}),
    ("30", {"n": 90, "s": 0, "w": 0, "e": 90}),
    ("31", {"n": 90, "s": 0, "w": 90, "e": 180}),
)
_ROOT = dict(ROOTS)


def child_cells(box):
    """Every child cell of a box as [(key, box)]. Cells touching a pole are not split east/west."""
    n, s, w, e = box["n"], box["s"], box["w"], box["e"]
    mid_lat = (n + s) / 2
    mid_lon = (w + e) / 2
    out = []
    for y in (0, 1):
        cn = mid_lat if y == 0 else n
        cs = s if y == 0 else mid_lat
        if cn == 90 or cs == -90:
            out.append((y * 2, {"n": cn, "s": cs, "w": w, "e": e}))
        else:
            out.append((y * 2, {"n": cn, "s": cs, "w": w, "e": mid_lon}))
            out.append((y * 2 + 1, {"n": cn, "s": cs, "w": mid_lon, "e": e}))
    return out


_box_cache = {}


def box_of_path(path):
    """{n, s, w, e} of a node path, or None for an invalid path."""
    b = _box_cache.get(path)
    if b is not None or path in _box_cache:
        return b
    root = _ROOT.get(path[:2])
    if root is None or len(path) < 2:
        _box_cache[path] = None
        return None
    box = root
    for ch in path[2:]:
        key = int(ch) & 3
        kids = child_cells(box)
        k = next((c for c in kids if c[0] == key), None) or next((c for c in kids if c[0] == (key & 2)), None)
        if k is None:
            box = None
            break
        box = k[1]
    _box_cache[path] = box
    return box
