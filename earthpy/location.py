"""location.json, footprint.geojson and README.txt (port of re/exporter/lib/location-info.js)."""

import math
import os
from datetime import datetime, timezone

from . import jsfmt, v8math
from .octant import box_of_path
from .tileset import _js_max, _js_min


def iso_now():
    """new Date().toISOString()"""
    d = datetime.now(timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (d.microsecond // 1000)


def write_location_info(out_dir, paths, extra=None, readme_files=()):
    tiles = []
    for p in paths:
        b = box_of_path(p)
        if b:
            tiles.append({"path": p, "level": len(p), "box": b})
    b = {
        "north": _js_max(*[t["box"]["n"] for t in tiles]),
        "south": _js_min(*[t["box"]["s"] for t in tiles]),
        "east": _js_max(*[t["box"]["e"] for t in tiles]),
        "west": _js_min(*[t["box"]["w"] for t in tiles]),
    }
    center = {"lat": (b["north"] + b["south"]) / 2, "lon": (b["east"] + b["west"]) / 2}
    counts = {}
    for t in tiles:
        counts[t["level"]] = counts.get(t["level"], 0) + 1
    levels = {str(l): counts[l] for l in sorted(counts)}  # integer keys: ascending, like a JS object
    R = 6371008.8
    rad = math.pi / 180
    height_m = (b["north"] - b["south"]) * rad * R
    width_m = (b["east"] - b["west"]) * rad * R * v8math.cos(center["lat"] * rad)

    info = {
        "description": "Area covered by this model: the union of the listed tiles (lat/lon rectangles, WGS84 degrees).",
        "bounds": b,
        "center": center,
        "approxSizeMeters": {"width": jsfmt.js_round(width_m), "height": jsfmt.js_round(height_m)},
        "tileCount": len(tiles),
        "tilesPerLevel": levels,
        "generated": iso_now(),
        "coordinates": "Model vertices are Earth-centred globe coordinates (not local); recentre them in your 3D tool.",
        "tiles": [{"path": t["path"], "level": t["level"], "north": t["box"]["n"], "south": t["box"]["s"],
                   "east": t["box"]["e"], "west": t["box"]["w"]} for t in tiles],
    }
    for k, v in (extra or {}).items():  # Object.assign: existing keys keep their place
        info[k] = v
    _write(os.path.join(out_dir, "location.json"), jsfmt.stringify(info, 2))

    geo = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"path": t["path"], "level": t["level"]},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[t["box"]["w"], t["box"]["s"]], [t["box"]["e"], t["box"]["s"]],
                                 [t["box"]["e"], t["box"]["n"]], [t["box"]["w"], t["box"]["n"]],
                                 [t["box"]["w"], t["box"]["s"]]]],
            },
        } for t in tiles],
    }
    _write(os.path.join(out_dir, "footprint.geojson"), jsfmt.stringify(geo))

    def f(n):
        return jsfmt.js_to_fixed(n, 6)
    n = jsfmt.js_num
    lines = [
        "Earth model export",
        "",
        "Covers:   lat %s to %s, lon %s to %s  (WGS84)" % (f(b["south"]), f(b["north"]), f(b["west"]), f(b["east"])),
        "Centre:   %s, %s" % (f(center["lat"]), f(center["lon"])),
        "Size:     about %s m x %s m" % (n(jsfmt.js_round(width_m)), n(jsfmt.js_round(height_m))),
        "Tiles:    %d (%s)" % (len(tiles), ", ".join("L%s x%d" % (l, levels[l]) for l in levels)),
        "Map link: https://www.openstreetmap.org/?mlat=%s&mlon=%s#map=16/%s/%s"
        % (f(center["lat"]), f(center["lon"]), f(center["lat"]), f(center["lon"])),
        "",
        "location.json      exact bounds, centre and every tile",
        "footprint.geojson  tile outlines, open in QGIS / geojson.io",
    ] + list(readme_files) + [""]
    _write(os.path.join(out_dir, "README.txt"), "\n".join(lines))


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def append(path, text):
    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write(text)
