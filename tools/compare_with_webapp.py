#!/usr/bin/env python3
"""
Compare earth-py's output with the Earth Tile Library webapp's.

  python3 tools/compare_with_webapp.py tiles --webapp ~/Downloads/earth-rea --workflow my.workflow.json
  python3 tools/compare_with_webapp.py export earth-3dtiles-xxxx.zip webapp-export.zip

`tiles` compares the GLB cache (earth-py's library/glb-v2 against the webapp's
webapp/data/library/glb-v2): structure, triangles and texture pixels must be identical, positions,
normals and texture coordinates may differ by floating point rounding (tolerances below).
`export` compares two exports (ZIPs or unzipped folders): the text files must be identical, the
JSON files equal up to the same rounding (location.json's "generated" time is ignored).
"""

import argparse
import json
import os
import struct
import sys
import zipfile
import zlib
from array import array

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TOL = {  # largest allowed absolute difference
    "POSITION": 1e-4,    # metres (tile-relative float32; one float32 step is ~1e-5 m at 200 m)
    "NORMAL": 1e-5,
    "TEXCOORD_0": 1e-6,
    "meta": 1e-6,        # metres, tile centre / half extent (ECEF doubles)
    "json_abs": 1e-6,    # numbers in tileset.json / location.json / footprint.geojson: absolute
    "json_rel": 1e-9,    # ... or relative difference (either is enough)
}


# ------------------------------------------------------------------ GLB

def parse_glb(b):
    jl = struct.unpack_from("<I", b, 12)[0]
    js = json.loads(b[20:20 + jl])
    return js, b[20 + jl + 8:]


def view(js, binb, i):
    v = js["bufferViews"][i]
    return binb[v["byteOffset"]:v["byteOffset"] + v["byteLength"]]


def accessor(js, binb, i):
    a = js["accessors"][i]
    t = {5126: "f", 5123: "H", 5125: "I"}[a["componentType"]]
    arr = array(t)
    arr.frombytes(view(js, binb, a["bufferView"]))
    return a, arr


def png_pixels(png):
    w, h = struct.unpack_from(">II", png, 16)
    o, idat = 8, b""
    while o < len(png):
        ln = struct.unpack_from(">I", png, o)[0]
        if png[o + 4:o + 8] == b"IDAT":
            idat += png[o + 8:o + 8 + ln]
        o += 12 + ln
    return w, h, zlib.decompress(idat)


def image(js, binb, prim):
    mat = js["materials"][prim["material"]]
    img = js["images"][js["textures"][mat["pbrMetallicRoughness"]["baseColorTexture"]["index"]]["source"]]
    data = view(js, binb, img["bufferView"])
    return img["mimeType"], (png_pixels(data) if img["mimeType"] == "image/png" else data)


def compare_glb(a_bytes, b_bytes, worst):
    """Problems (list of str) between two GLBs; updates worst[name] with the largest differences."""
    ja, ba = parse_glb(a_bytes)
    jb, bb = parse_glb(b_bytes)
    pa, pb = ja["meshes"][0]["primitives"], jb["meshes"][0]["primitives"]
    if len(pa) != len(pb):
        return ["%d vs %d primitives" % (len(pa), len(pb))]
    probs = []
    for k, (x, y) in enumerate(zip(pa, pb)):
        for name in ("POSITION", "NORMAL", "TEXCOORD_0"):
            ax, vx = accessor(ja, ba, x["attributes"][name])
            ay, vy = accessor(jb, bb, y["attributes"][name])
            if ax["count"] != ay["count"] or ax["type"] != ay["type"]:
                probs.append("primitive %d %s: count/type differ" % (k, name))
                continue
            d = max((abs(p - q) for p, q in zip(vx, vy) if not (p != p and q != q)), default=0.0)
            if name == "POSITION":
                for lim in ("min", "max"):
                    d = max([d] + [abs(p - q) for p, q in zip(ax[lim], ay[lim])])
            worst[name] = max(worst.get(name, 0.0), d)
            if d > TOL[name]:
                probs.append("primitive %d %s differs by %g" % (k, name, d))
        ix, vx = accessor(ja, ba, x["indices"])
        iy, vy = accessor(jb, bb, y["indices"])
        if ix["componentType"] != iy["componentType"] or list(vx) != list(vy):
            probs.append("primitive %d: indices differ" % k)
        if image(ja, ba, x) != image(jb, bb, y):
            probs.append("primitive %d: texture pixels differ" % k)
        if x["mode"] != y["mode"] or ja["materials"][x["material"]] != jb["materials"][y["material"]]:
            probs.append("primitive %d: material differs" % k)
    for key in ("asset", "extensionsUsed", "scene", "scenes", "nodes", "samplers"):
        if ja.get(key) != jb.get(key):
            probs.append("glTF %s differs" % key)
    return probs


def compare_meta(a, b, worst):
    if a.get("texWidth") != b.get("texWidth"):
        return ["texWidth %s vs %s" % (a.get("texWidth"), b.get("texWidth"))]
    d = max(abs(p - q) for p, q in zip(a["center"] + a["half"], b["center"] + b["half"]))
    worst["meta"] = max(worst.get("meta", 0.0), d)
    return ["centre/extent differ by %g m" % d] if d > TOL["meta"] else []


def cmd_tiles(args):
    web = os.path.join(os.path.expanduser(args.webapp), "webapp", "data", "library", "glb-v2")
    mine = os.path.join(os.path.expanduser(args.cache), "glb-v2")
    if args.workflow:
        with open(args.workflow, encoding="utf-8") as fh:
            tiles = json.load(fh)["tiles"]
    else:
        tiles = sorted(f[:-4] for f in os.listdir(mine) if f.endswith(".glb"))
    worst, bad, same_bytes, missing = {}, 0, 0, 0
    for p in tiles:
        fa, fb = os.path.join(mine, p + ".glb"), os.path.join(web, p + ".glb")
        if not (os.path.exists(fa) and os.path.exists(fb)):
            missing += 1
            print("  %s: not in both caches" % p)
            continue
        with open(fa, "rb") as f1, open(fb, "rb") as f2:
            a, b = f1.read(), f2.read()
        same_bytes += a == b
        probs = compare_glb(a, b, worst)
        with open(fa[:-4] + ".json") as ma, open(fb[:-4] + ".json") as mb:
            probs += compare_meta(json.load(ma), json.load(mb), worst)
        if probs:
            bad += 1
            print("  %s: %s" % (p, "; ".join(probs)))
    n = len(tiles) - missing
    print("%d tiles compared: %d equivalent, %d different, %d byte-identical%s" % (
        n, n - bad, bad, same_bytes, (", %d missing" % missing) if missing else ""))
    print("largest differences: " + ", ".join("%s %.3g" % kv for kv in sorted(worst.items())))
    return 1 if bad or missing else 0


# ------------------------------------------------------------------ export

def read_export(path):
    path = os.path.expanduser(path)
    if os.path.isdir(path):
        out = {}
        for f in os.listdir(path):
            full = os.path.join(path, f)
            if os.path.isfile(full) and not f.startswith("."):
                with open(full, "rb") as fh:
                    out[f] = fh.read()
        return out
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist() if not n.endswith("/")}


def json_diff(a, b, where, out, worst):
    if isinstance(a, dict) and isinstance(b, dict):
        if list(a) != list(b):
            out.append("%s: keys differ" % where)
            return
        for k in a:
            if where == "location.json" and k == "generated":
                continue
            json_diff(a[k], b[k], "%s.%s" % (where, k), out, worst)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append("%s: %d vs %d items" % (where, len(a), len(b)))
            return
        for i, (x, y) in enumerate(zip(a, b)):
            json_diff(x, y, "%s[%d]" % (where, i), out, worst)
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        d = abs(a - b)
        worst["json"] = max(worst.get("json", 0.0), d)
        if d > TOL["json_abs"] and d > TOL["json_rel"] * max(abs(a), abs(b)) and len(out) < 20:
            out.append("%s: %r vs %r" % (where, a, b))
    elif a != b and len(out) < 20:
        out.append("%s: %r vs %r" % (where, a, b))


def cmd_export(args):
    a, b = read_export(args.mine), read_export(args.reference)
    worst, bad = {}, 0
    if sorted(a) != sorted(b):
        print("  files differ: %s vs %s" % (sorted(a), sorted(b)))
        bad += 1
    for name in sorted(set(a) & set(b)):
        if name.endswith((".json", ".geojson")):
            out = []
            json_diff(json.loads(a[name]), json.loads(b[name]), name, out, worst)
            status = "identical" if a[name] == b[name] else "equivalent" if not out else "DIFFERENT"
        else:
            out = [] if a[name] == b[name] else ["text differs"]
            status = "identical" if not out else "DIFFERENT"
        bad += bool(out)
        print("  %-18s %s%s" % (name, status, ("\n    " + "\n    ".join(out)) if out else ""))
    if worst:
        print("largest number difference in the JSON files: %.3g" % worst["json"])
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    t = sub.add_parser("tiles", help="compare the GLB caches")
    t.add_argument("--webapp", required=True, help="the earth-rea project folder")
    t.add_argument("--cache", default=os.path.join(HERE, "library"), help="earth-py's cache (default: library/)")
    t.add_argument("--workflow", help="only the tiles of this workflow (default: every tile in earth-py's cache)")
    e = sub.add_parser("export", help="compare two exports")
    e.add_argument("mine", help="earth-py's export (ZIP or unzipped folder)")
    e.add_argument("reference", help="the webapp's export of the same workflow (ZIP or unzipped folder)")
    args = ap.parse_args()
    if not args.cmd:
        ap.error("choose 'tiles' or 'export'")
    sys.exit(cmd_tiles(args) if args.cmd == "tiles" else cmd_export(args))


if __name__ == "__main__":
    main()
