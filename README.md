# earth-py

A Python 3 port of one job of the Earth Tile Library webapp (`earth-rea`): take a workflow
export (`*.workflow.json`), download its tiles, and export them as **3D Tiles 1.1 - one GLB per
tile**, package **Small ZIP + script**. The output is the same as the webapp's:

- the files in the ZIP (`tileset.json`, `location.json`, `footprint.geojson`, `README.txt`,
  `tiles.txt`, `fill-tiles.sh`) are byte-identical to a webapp export of the same selection;
  only the `generated` time stamp in `location.json` differs, as it does between two webapp exports.
- the GLB files that `fill-tiles.sh` copies into `tiles/` are byte-identical to the webapp's GLB
  cache (geometry, normals, UVs, texture PNGs, JSON, metadata).

Standard library only (Python 3.7+). It doesn't use a native converter: the tile decoder, the
crunch texture decoder, the trigonometry and even the PNG compression are ports in plain Python.

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
The README inside the ZIP names `~/Downloads/earth-rea`, word for word like the webapp's. When the
tiles come from earth-py, pass the earth-py folder instead.

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
| `--fast-png` | off | stdlib zlib for textures: several times faster conversion, same pixels, but GLBs no longer byte-identical |

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

## Speed

On a 4-core Mac, the 185-tile Paris workflow takes about 1 min 40 s from an empty cache:
about 25 s for the downloads and about 75 s for the conversion. The PNG compression is about
two thirds of the conversion time: a pure Python port of zlib-rs, needed for identical bytes.
`--fast-png` cuts most of that.

## How exactness is achieved

The webapp in Docker converts with its native Rust module (`re/native`). The JavaScript parts
(tileset, location info) run on Node 20.

| Part | Port of |
|---|---|
| `earthpy/decode.py` | `re/native/src/decode.rs` + `pb.rs` (NodeData protobuf) |
| `earthpy/crn.py` | crunch `crn_decomp.h` DXT1 path (texture format 6) |
| `earthpy/fdlibm.py` | Rust `libm` 0.2.16 `sin`/`cos`/`asin`/`atan2` (what the converter uses), bit-exact |
| `earthpy/v8math.py` | V8 11.3's `Math.cos` (fdlibm port in `ieee754.cc`), bit-exact |
| `earthpy/zrs.py` | `zlib-rs` 0.6.8 level 6 (`deflate_medium`) driven like flate2's `ZlibEncoder` |
| `earthpy/png.py`, `glb.py` | `texture.rs`, `glb.rs` (incl. ancestor cut / clip) |
| `earthpy/bulk.py` | Google's minified bulk decoder (`decode-resource.js`, command 0), quirks included |
| `earthpy/rocktree.py` | `utils.js`, `locate()` in `convert-lat-long-to-octant.js` |
| `earthpy/net.py` | `webapp/lib/proxies.js`, `webapp/lib/limiter.js` + a small SOCKS5 client |
| `earthpy/tileset.py`, `location.py` | `tileset.js`, `location-info.js` (JS number formatting in `jsfmt.py`) |
| `earthpy/pipeline.py` | `runDownload`, `runFix`, `export_3dtiles.js`, `addFillScript` |
| `earthpy/templates/fill-tiles.sh` | copied unchanged from the webapp |

These were checked against the webapp:

- 300,000 random arguments against the Rust `libm` crate; 200,000 against Node 20's `Math.cos`
  and `Math.hypot`.
- 61 texture streams from the webapp's GLB cache.
- 185 Paris tiles plus 13 tiles with mixed levels (L17–L20), 10 of them without geometry.
  Raw downloads, GLBs and metadata are identical, and the export files match a webapp export of
  the same workflow.
- Two clip cases checked against the native converter.

The data belongs to Google; check Google's terms before using or sharing it.
