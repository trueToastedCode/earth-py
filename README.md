# earth-py

A Python 3 port of one job of the Earth Tile Library webapp (`earth-rea`): take a workflow
export (`*.workflow.json`), download its tiles, and export them as **3D Tiles 1.1 - one GLB per
tile**, package **Small ZIP + script**. The result is equivalent to the webapp's:

- **Raw downloads**: byte-identical (same requests, same responses).
- **GLBs**: the same structure, the same triangles (indices), normals, texture coordinates and
  texture pixels.
  - Positions and tile centres can differ in the last bit, because Python's `math` module rounds
    a little differently from the webapp's Rust/V8 maths. That is at most one float32 step,
    measured up to 0.008 mm.
  - The PNG textures are compressed with Python's `zlib`: the same pixels, but different (and
    ~13 % smaller) bytes.
- **Export**:
  - `README.txt`, `tiles.txt`, `fill-tiles.sh` and `footprint.geojson` are identical to the
    webapp's.
  - `tileset.json` and `location.json` hold the same structure and values, apart from those
    nanometre-scale differences and number formatting (`5.0` vs `5`).

Standard library only, Python 3.8+. No native code. Google's tile format and the crunch
texture decoder are implemented in plain Python.

## Use

```bash
python3 earth_py.py ~/Downloads/paris-python-0.1-python.workflow.json
python3 earth_py.py my.workflow.json proxies.txt            # with SOCKS5 proxies
python3 earth_py.py my.workflow.json --proxies proxies.txt --out ~/Desktop/exports
```

Then unzip the export and fill `tiles/` from this folder's cache (the command is printed at the end):

```bash
cd earth-3dtiles-xxxxxxxx && ./fill-tiles.sh ~/Desktop/earth-py
```

`fill-tiles.sh` is the webapp's script unchanged. It finds the cache in `<folder>/library/glb-v2`,
and uses APFS clones on macOS (instant, no extra space). `--link` creates symlinks instead.
The README inside the ZIP names `~/Downloads/earth-rea` like the webapp's. When the tiles come
from earth-py, pass the earth-py folder instead.

## What it does

1. **Download**: every workflow tile that isn't in the cache yet. The steps are the same as the
   webapp's: bulk-metadata walk, then the NodeData request with the same epochs and texture format.
   The raw responses are stored unconverted in `library/tiles/<path>.raw`.
2. **Tiles without textured geometry**: they are detected right after the download. For each
   one it does the same as the webapp's *Auto fix*: walk from the parent up to level 2, look up
   the ancestors that aren't in the cache at Google, and download them until one has geometry
   there (its best backup).
3. **Convert**: each tile becomes `library/glb-v2/<path>.glb` + `<path>.json`, using all cores.
   A tile without geometry gets the matching part of its nearest usable ancestor: the quarter of
   the parent, or clipped exactly to the tile's lat/lon box when the ancestor is further up. A
   tile with no usable ancestor is left out, and listed in the export's README, as in the webapp.
4. **Export**: `tileset.json` keeps the same mixed-level hierarchy as the webapp. A tile is nested
   over finer tiles only when they cover it fully. The rest of the package is the same as the
   webapp's "Small ZIP + script".

Already downloaded or converted tiles are reused, so running again is fast.

## Options

| Option | Default | Meaning |
|---|---|---|
| `PROXIES` / `--proxies FILE` | none | SOCKS5 list in the webapp's format: one per line, `user:password@host:port`, `host:port` or `socks5://...`; `#` comments |
| `--out DIR` | `.` | where the ZIP goes (`earth-3dtiles-<id>.zip`) |
| `--cache DIR` | `./library` | raw tile and GLB cache |
| `--jobs N` | all cores (`CONVERT_THREADS`) | conversion processes |
| `--delay-ms`, `--burst`, `--pause-s` | 100, 1000, 60 (`DOWNLOAD_DELAY_MS`, `DOWNLOAD_BURST`, `DOWNLOAD_PAUSE_S`) | request limits, per proxy |
| `--skip-missing` | off | export even if some tiles couldn't be downloaded |

The environment variables, or a `.env` file next to `earth_py.py`, work as in the webapp.

## Proxies and request limits

The behaviour is the same as the webapp's limiter (`webapp/lib/limiter.js`):

- Every request goes through it: metadata and tile data.
- With proxies, requests are spread over all of them and your own connection is never used.
- Each proxy keeps its own pace and burst pause.
- A failing proxy (no connection, timeout, SOCKS error, 403/429, 5xx) gets a 1 s, 2 s, 4 s, ...
  break (at most the pause length), and the request moves on to the next free proxy.
- A request only fails for good when Google says the tile doesn't exist, or after 5 server errors.
- The timeouts are the same as the webapp's: 20 s for the SOCKS connection, 60 s per request.
  Dead proxies in a list therefore slow things down.

One deliberate difference: in the webapp, `getNodeRaw` in `re/exporter/lib/utils.js` calls the
plain `getUrl` instead of the configured one. Tile data (NodeData) downloads therefore skip the
limiter and the proxies, and go out directly. Only the metadata uses the proxies. earth-py sends
both through the proxies, as the webapp's README describes. The downloaded bytes are the same
either way.

## Open files limit (important with many proxies)

Every network connection counts as an open file. The most earth-py has open at once is about:

    30 + min(400, 4 × proxies, tiles to download) + 32 × proxies

With the direct connection that is about 70; with 95 proxies it is about 3,450.
macOS allows only 256 per Terminal session by default. Above that, downloads fail with
`Errno 24 Too many open files`.

Before a run with proxies, raise the limit in the same Terminal window (it lasts until you
close it):

```bash
ulimit -n 8192
python3 earth_py.py my.workflow.json proxies.txt
```

Pick a value above the number from the formula. `ulimit -n` alone shows the current limit.

## Speed

On a 4-core Mac, the 185-tile Paris workflow takes about 45 s from an empty cache: about 25 s for
the downloads and about 15 s for the conversion. With a filled cache it takes under a second.

## Comparing with the webapp

`tools/compare_with_webapp.py` checks earth-py's output against the webapp's.

`tiles` compares the GLB caches:

```bash
python3 tools/compare_with_webapp.py tiles --webapp ~/Downloads/earth-rea --workflow my.workflow.json
```

Structure, indices, materials and texture pixels must be identical. Positions may differ by up to
0.1 mm, normals by up to 1e-5, texture coordinates by up to 1e-6 and tile centres by up to 1 µm.
The tool prints the largest differences it found.

`export` compares two exports (ZIPs or unzipped folders):

```bash
python3 tools/compare_with_webapp.py export earth-3dtiles-xxxx.zip webapp-export.zip
```

The text files must be identical. The numbers in the JSON files may differ by at most 1 µm (or
1e-9 relative), and the `generated` time in `location.json` is ignored.

Results for the Paris workflow (185 tiles) and a test with 13 tiles across L17–L20, 10 of them
without geometry:

- **Triangles, normals, texture coordinates and pixels**: all identical.
- **Positions**: at most 0.008 mm apart (one float32 step).
- **Tile centres**: at most 2 nm apart.
- **Clipping from higher ancestors**: 12 cases checked against the webapp's native converter,
  with identical triangles.

In theory, a vertex lying exactly on a tile edge could fall on the other side of the clip because
of the last-bit rounding. That would make the cut differ by a nanometre-thin sliver. It didn't
happen in the cases tested.

## Layout

| File | What it is |
|---|---|
| `earth_py.py`, `earthpy/cli.py` | command line |
| `earthpy/pipeline.py` | download, no-geometry fix, conversion, export (`runDownload`, `runFix`, `export_3dtiles.js`, `addFillScript`) |
| `earthpy/decode.py` | NodeData protobuf decoder (port of `re/native/src/decode.rs`) |
| `earthpy/crn.py` | crunch texture decoder, DXT1 path (port of `crn_decomp.h`) |
| `earthpy/png.py`, `earthpy/glb.py` | DXT1 -> PNG, meshes -> GLB incl. ancestor cut / clip (`texture.rs`, `glb.rs`) |
| `earthpy/bulk.py` | bulk metadata decoder, reproducing Google's minified decoder (`decode-resource.js`) |
| `earthpy/rocktree.py` | tile lookup and URLs (`utils.js`, `locate()`) |
| `earthpy/net.py` | SOCKS5 client, proxy list (`proxies.js`), request limiter (`limiter.js`) |
| `earthpy/tileset.py`, `earthpy/location.py` | `tileset.json`, `location.json`, `footprint.geojson`, `README.txt` |
| `earthpy/templates/fill-tiles.sh` | copied unchanged from the webapp |
| `tools/compare_with_webapp.py` | comparison with the webapp's output |

The data belongs to Google; check Google's terms before using or sharing it.
