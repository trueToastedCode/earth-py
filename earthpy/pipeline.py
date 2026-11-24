"""
The steps of the Python port, each a port of the webapp's job of the same purpose:
download (server.js runDownload), no-geometry fix (runFix, "Auto fix"), conversion
(glb-cache.js ensureMany with the native converter) and export (export_3dtiles.js + runExport's
"Small ZIP + script" packaging).
"""

import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor, as_completed

import json

from . import decode, glb, location, tileset
from .library import Library, write_atomic
from .octant import box_of_path

DOWNLOAD_CONCURRENCY = 4  # tiles in flight per exit (proxy or direct); the limiter paces the requests
MAX_THREADS = 400
TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "fill-tiles.sh")


class Progress:
    def __init__(self, label, total, extra=lambda: ""):
        self.label = label
        self.total = total
        self.extra = extra
        self.t0 = time.time()
        self.last = 0.0
        self.lock = threading.Lock()

    def show(self, done, force=False, note=""):
        now = time.time()
        with self.lock:
            if not force and now - self.last < 2.0:
                return
            self.last = now
        el = now - self.t0
        eta = ""
        if 0 < done < self.total:
            eta = ", about %s left" % _fmt_dur(el / done * (self.total - done))
        ex = self.extra()
        print("  %s: %d / %d%s%s%s" % (self.label, done, self.total, eta, (" · " + ex) if ex else "",
                                      (" · " + note) if note else ""), flush=True)

    def heartbeat(self, get_done, every=10.0):
        """Print the status every `every` seconds while nothing finishes (slow proxies, pauses)."""
        stop = threading.Event()

        def loop():
            while not stop.wait(1.0):
                if time.time() - self.last >= every:
                    self.show(get_done(), force=True)
        threading.Thread(target=loop, daemon=True).start()
        return stop


def _fmt_dur(s):
    s = int(round(s))
    if s < 60:
        return "%d s" % s
    if s < 3600:
        return "%d min %02d s" % (s // 60, s % 60)
    return "%d h %02d min" % (s // 3600, (s % 3600) // 60)


def _run_threads(workers, fn, items, cancel):
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        for f in [ex.submit(fn, x) for x in items]:
            f.result()
    except BaseException:
        cancel.set()  # waiting requests stop; requests in flight end with their timeout
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    ex.shutdown()


def _err(e):
    return str(e) or e.__class__.__name__


# ------------------------------------------------------------------ download (runDownload)

def download(paths, lib, rt, limiter, cancel):
    """Download tiles that are not in the cache. Returns {path: error} for the failed ones."""
    todo = [p for p in paths if not lib.has(p)]
    failed = {}
    if not todo:
        return failed
    done = [len(paths) - len(todo)]
    prog = Progress("Downloading", len(paths), limiter.status_line)
    lock = threading.Lock()

    def one(p):
        if cancel.is_set():
            return
        try:
            loc = rt.locate(p)
            if not loc:
                raise ValueError("tile does not exist")
            raw = rt.get_node_raw(p, *loc)
            if not raw:
                raise ValueError("empty response")
            lib.add(p, raw)
        except Exception as e:
            if cancel.is_set():
                return
            with lock:
                failed[p] = _err(e)
        with lock:
            done[0] += 1
            d = done[0]
        prog.show(d)

    workers = min(MAX_THREADS, DOWNLOAD_CONCURRENCY * max(1, limiter.size()), len(todo))
    prog.last = time.time()
    hb = prog.heartbeat(lambda: done[0])
    try:
        _run_threads(workers, one, todo, cancel)
    finally:
        hb.set()
    prog.show(done[0], force=True, note="%d failed" % len(failed) if failed else "")
    return failed


# ------------------------------------------------------------------ no-geometry fix (runFix)

def fix_no_geometry(targets, lib, rt, limiter, cancel):
    """For tiles without textured geometry: walk from the parent up to level 2; ancestors not in the
    cache are looked up at Google and downloaded, until one has geometry for the tile (its best
    backup). Returns a summary dict (same counts as the webapp's Auto fix note)."""
    fetched = {}
    nodes = {}
    lock = threading.Lock()
    st = {"downloaded": 0, "best": 0, "closer_missing": 0, "none": 0, "failed": 0, "first_failure": None,
          "backup": {}}

    def ensure(a):
        if lib.has(a):
            return True
        with lock:
            fut = fetched.get(a)
            owner = fut is None
            if owner:
                fut = fetched[a] = Future()
        if not owner:
            return fut.result()
        try:
            loc = rt.locate(a)
            ok = False
            if loc:
                raw = rt.get_node_raw(a, *loc)
                if raw:
                    lib.add(a, raw)
                    with lock:
                        st["downloaded"] += 1
                    ok = True
            fut.set_result(ok)
            return ok
        except BaseException as e:
            fut.set_exception(e)
            raise

    def load(a):
        with lock:
            fut = nodes.get(a)
            owner = fut is None
            if owner:
                fut = nodes[a] = Future()
        if not owner:
            return fut.result()
        node = None
        raw = lib.read(a)
        if raw is not None:
            try:
                node = decode.decode_node(raw, textures=False)
            except Exception:
                node = None
        fut.set_result(node)
        return node

    prog = Progress("Checking ancestors", len(targets), limiter.status_line)
    finished = [0]

    def fix_one(t):
        try:
            _fix_one(t)
        finally:
            with lock:
                finished[0] += 1
                d = finished[0]
            prog.show(d)

    def _fix_one(t):
        box = box_of_path(t)
        missing = False
        a = t[:-1]
        while len(a) >= 2:
            if cancel.is_set():
                return
            try:
                have = ensure(a)
            except Exception as e:
                with lock:
                    st["failed"] += 1
                    if not st["first_failure"]:
                        st["first_failure"] = (a, _err(e))
                return
            if not have:
                missing = True
                a = a[:-1]
                continue
            n = load(a)
            if n is not None and glb.ancestor_pieces(n, a, t, box):
                with lock:
                    st["closer_missing" if missing else "best"] += 1
                    st["backup"][t] = a
                return
            a = a[:-1]
        with lock:
            st["none"] += 1

    workers = min(MAX_THREADS, DOWNLOAD_CONCURRENCY * max(1, limiter.size()), max(1, len(targets)))
    prog.last = time.time()
    hb = prog.heartbeat(lambda: finished[0])
    try:
        _run_threads(workers, fix_one, targets, cancel)
    finally:
        hb.set()
    prog.show(finished[0], force=True)
    return st


# ------------------------------------------------------------------ conversion (glb-cache.js)

def _convert_worker(job):
    cache_root, p = job
    try:
        lib = Library(cache_root)
        raw = lib.read(p)
        if raw is None:
            return p, "missing", "Tile %s is no longer in the library" % p
        r = glb.convert_raw_with(raw, p, lib.read)
        if isinstance(r, str):
            for f in (lib.glb_file(p), lib.meta_file(p)):
                try:
                    os.remove(f)
                except FileNotFoundError:
                    pass
            return p, "skipped", r
        write_atomic(lib.glb_file(p), r.glb)
        write_atomic(lib.meta_file(p), r.meta_json().encode())  # meta last: marks the entry complete
        return p, "converted", None
    except Exception as e:
        return p, "failed", _err(e)


def convert(paths, lib, jobs, force=(), cancel=None):
    """Make sure every tile is in the GLB cache. Returns (skipped [(path, reason)], failures {path: error})."""
    force = set(force)
    todo = [p for p in paths if p in force or not lib.glb_cached(p)]
    skipped, failures = [], {}
    if not todo:
        return skipped, failures
    prog = Progress("Converting", len(paths))
    done = len(paths) - len(todo)
    prog.show(done, force=True)
    jobs = max(1, min(jobs, len(todo)))
    if jobs == 1:
        results = (_convert_worker((lib.root, p)) for p in todo)
        for p, status, info in results:
            done += 1
            if status == "skipped":
                skipped.append((p, info))
            elif status != "converted":
                failures[p] = info
            prog.show(done)
            if cancel is not None and cancel.is_set():
                break
    else:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            futs = [ex.submit(_convert_worker, (lib.root, p)) for p in todo]
            try:
                for f in as_completed(futs):
                    p, status, info = f.result()
                    done += 1
                    if status == "skipped":
                        skipped.append((p, info))
                    elif status != "converted":
                        failures[p] = info
                    prog.show(done)
            except KeyboardInterrupt:
                for f in futs:
                    f.cancel()
                raise
    prog.show(done, force=True)
    order = {p: i for i, p in enumerate(tileset.sort_paths(paths))}
    skipped.sort(key=lambda x: order[x[0]])
    return skipped, failures


# ------------------------------------------------------------------ export (export_3dtiles.js + runExport)

def _left_out_note(skipped):
    n = len(skipped)
    return ("%d tile%s left out (no textured geometry and no usable ancestor tile in the library; "
            "try Auto fix in the Library panel). See README.txt." % (n, "" if n == 1 else "s"))


def export_small_zip(paths, lib, out_dir, skipped):
    """3D Tiles 1.1 export, package "Small ZIP + script" (everything except the tile GLBs, plus
    fill-tiles.sh). `paths` in selection order. Returns (zip path, tiles in the export)."""
    job_id = secrets.token_hex(4)
    left = {p for p, _ in skipped}
    work = tempfile.mkdtemp(prefix="earth-py-%s-" % job_id)
    try:
        out = os.path.join(work, "model")
        os.makedirs(out)
        sorted_paths = [p for p in tileset.sort_paths(paths) if p not in left]
        if not sorted_paths:
            raise RuntimeError("Every tile was left out: they have no textured geometry and no usable ancestor tile in the library.")
        metas = {}
        for p in sorted_paths:
            with open(lib.meta_file(p), "r", encoding="utf-8") as fh:
                metas[p] = json.load(fh)
        ts = tileset.build_tileset(sorted_paths, lambda p: metas[p])
        location._write(os.path.join(out, "tileset.json"), json.dumps(ts, separators=(",", ":"), ensure_ascii=False))
        location.write_location_info(out, sorted_paths, extra={
            "format": "3D Tiles 1.1 (one GLB per tile)",
            "coordinates": "WGS84 ECEF metres (tileset transforms); Google sphere positions re-projected onto WGS84.",
        }, readme_files=[
            "tileset.json       3D Tiles 1.1 tileset (open in CesiumJS or any 3D Tiles viewer)",
            "tiles/<path>.glb   one self-contained glTF per tile",
        ])
        readme = os.path.join(out, "README.txt")
        if skipped:
            n = len(skipped)
            location.append(readme, "\n".join([
                "",
                "LEFT OUT: %d selected tile%s" % (n, "" if n == 1 else "s"),
                "These tiles contain no textured geometry. Such a tile is normally replaced by the matching",
                "part of its nearest ancestor tile in the library (parent, grandparent, ...); none was usable:",
            ] + ["  %s  %s" % (p, r) for p, r in skipped] + [""]))
        # addFillScript (server.js)
        job_paths = [p for p in paths if p not in left]
        location._write(os.path.join(out, "tiles.txt"), "\n".join(job_paths) + "\n")
        with open(TEMPLATE, "r", encoding="utf-8") as fh:
            script = fh.read().replace("__CACHE_NAME__", os.path.basename(lib.glb_dir))
        sp = os.path.join(out, "fill-tiles.sh")
        location._write(sp, script)
        os.chmod(sp, 0o755)
        location.append(readme, "\n".join([
            "",
            "THIS ZIP DOES NOT CONTAIN THE TILES",
            "This is the small version of the export. The tile files (tiles/<path>.glb) are taken from",
            "the tile cache of your Earth Tile Library project by running, in this folder:",
            "",
            "  ./fill-tiles.sh ~/Downloads/earth-rea           (copies; clones on APFS, so no extra space)",
            "  ./fill-tiles.sh --link ~/Downloads/earth-rea    (symlinks into the cache instead)",
            "",
            "Use the path of your earth-rea folder. If the script is not executable after unzipping,",
            "run it as: bash fill-tiles.sh <project folder>",
            "The tiles must still be in the cache: if they were deleted from the library or the cache",
            "was cleared, export the area again first. tiles.txt lists the tiles this export needs.",
            "",
        ]))
        os.makedirs(out_dir, exist_ok=True)
        zip_path = os.path.join(os.path.abspath(out_dir), "earth-3dtiles-%s.zip" % job_id)
        _zip(out, sorted(os.listdir(out)), zip_path)
        return zip_path, job_paths
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _zip(src_dir, names, zip_path):
    """Like the webapp: Info-ZIP `zip -q -0 <zip> -@` (store only); zipfile when zip isn't installed."""
    zip_bin = "/usr/bin/zip" if os.path.exists("/usr/bin/zip") else shutil.which("zip")
    if zip_bin:
        r = subprocess.run([zip_bin, "-q", "-0", zip_path, "-@"], cwd=src_dir,
                           input=("\n".join(names) + "\n").encode(), capture_output=True)
        if r.returncode == 0:
            return
        sys.stderr.write("zip failed (%s), using Python's zipfile\n" % r.stderr.decode(errors="replace").strip())
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
        for n in names:
            z.write(os.path.join(src_dir, n), n)
