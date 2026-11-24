"""Command line: download a workflow's tiles and export them like the webapp's
"3D Tiles (one GLB per tile)" + "Small ZIP + script" export."""

import argparse
import os
import re
import sys
import threading
import time

from . import decode, net, pipeline, rocktree, workflow
from .library import Library

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_env():
    """Optional .env next to earth_py.py (KEY=value lines); real environment variables win."""
    try:
        with open(os.path.join(HERE, ".env"), "r", encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
                if not m or line.strip().startswith("#"):
                    continue
                if m.group(1) not in os.environ:
                    os.environ[m.group(1)] = re.sub(r"^(['\"])(.*)\1$", r"\2", m.group(2))
    except OSError:
        pass


def _num_env(name, default):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    try:
        x = float(v)
        if x < 0:
            raise ValueError
        return x
    except ValueError:
        print("Ignoring invalid %s=%s, using %s" % (name, v, default))
        return default


def _levels(paths):
    counts = {}
    for p in paths:
        counts[len(p)] = counts.get(len(p), 0) + 1
    return " · ".join("L%d ×%d" % (l, counts[l]) for l in sorted(counts))


def main(argv=None):
    _load_env()
    threads_env = int(_num_env("CONVERT_THREADS", 0))
    ap = argparse.ArgumentParser(
        prog="earth_py.py",
        description="Download the tiles of an Earth Tile Library workflow and export them as 3D Tiles 1.1 "
                    "(one GLB per tile), package \"Small ZIP + script\" - the same files the webapp exports.")
    ap.add_argument("workflow", help="workflow file exported from the webapp (*.workflow.json)")
    ap.add_argument("proxies", nargs="?", help="optional SOCKS5 proxy list (one per line: user:password@host:port or "
                                              "host:port), same format as the webapp's Proxy panel")
    ap.add_argument("--proxies", dest="proxies_opt", metavar="FILE", help="same as the second argument")
    ap.add_argument("--out", default=".", help="folder for the export ZIP (default: current folder)")
    ap.add_argument("--cache", default=os.path.join(HERE, "library"),
                    help="cache folder for raw tiles and GLBs (default: library/ next to earth_py.py)")
    ap.add_argument("--jobs", type=int, default=threads_env or os.cpu_count() or 1,
                    help="processes for converting tiles (default: CONVERT_THREADS or all cores)")
    ap.add_argument("--delay-ms", type=float, default=_num_env("DOWNLOAD_DELAY_MS", 100),
                    help="minimum time between two requests to Google, per proxy (default 100)")
    ap.add_argument("--burst", type=int, default=int(_num_env("DOWNLOAD_BURST", 1000)),
                    help="pause after this many requests, per proxy; 0 = never (default 1000)")
    ap.add_argument("--pause-s", type=float, default=_num_env("DOWNLOAD_PAUSE_S", 60),
                    help="length of that pause in seconds; also the longest backoff of a failing proxy (default 60)")
    ap.add_argument("--skip-missing", action="store_true",
                    help="export even if some tiles could not be downloaded (they are left out)")
    ap.add_argument("--fast-png", action="store_true",
                    help="compress textures with Python's zlib: much faster, same pixels, but the GLB files are "
                         "no longer byte-identical to the webapp's")
    args = ap.parse_args(argv)

    t_start = time.time()
    cancel = threading.Event()
    try:
        wf = workflow.load(args.workflow)
    except workflow.WorkflowError as e:
        print("Error:", e)
        return 2
    paths = wf["tiles"]
    if not paths:
        print("Error: the workflow has no tiles.")
        return 2
    print("Workflow: %s%s - %d tiles (%s)" % (wf["name"], (" " + wf["version"]) if wf["version"] else "",
                                              len(paths), _levels(paths)))

    limiter = net.RequestLimiter(delay_ms=args.delay_ms, burst=args.burst, pause_ms=args.pause_s * 1000)
    proxy_file = args.proxies_opt or args.proxies
    if proxy_file:
        try:
            with open(proxy_file, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as e:
            print("Error: could not read the proxy list:", e)
            return 2
        proxies, errors = net.parse_proxy_list(text)
        for er in errors[:20]:
            print("  proxy list line %d (%s): %s" % (er["line"], er["text"], er["error"]))
        if len(errors) > 20:
            print("  ... and %d more invalid lines" % (len(errors) - 20))
        if not proxies:
            print("Error:", "No valid proxy in the file." if errors else "The file has no proxies.")
            return 2
        limiter.set_exits(proxies)
        print("Proxies: %d SOCKS5 proxies; all requests go through them (your own connection is not used)."
              % len(proxies))
    else:
        print("Proxies: none, using the direct connection.")
    print("Request limits: %g ms between requests, pause of %g s after %d requests (per %s)"
          % (args.delay_ms, args.pause_s, args.burst, "proxy" if proxy_file else "connection"))

    lib = Library(args.cache)
    rt = rocktree.RockTree(limiter, cancel.is_set)
    try:
        # 1. download
        missing = [p for p in paths if not lib.has(p)]
        print("\n1/4 Download: %d of %d tiles are not in the cache yet" % (len(missing), len(paths)))
        failed = pipeline.download(paths, lib, rt, limiter, cancel) if missing else {}
        for p, e in list(failed.items())[:10]:
            print("  failed: %s (%s)" % (p, e))
        if len(failed) > 10:
            print("  ... and %d more" % (len(failed) - 10))

        # 2. tiles without textured geometry -> best backup
        no_geo = [p for p in paths if lib.has(p) and decode.has_textured_geometry(lib.read(p)) is False]
        print("\n2/4 Tiles without textured geometry: %d" % len(no_geo))
        if no_geo:
            st = pipeline.fix_no_geometry(no_geo, lib, rt, limiter, cancel)
            n = len(no_geo)
            tl = lambda k: "%d tile%s" % (k, "" if k == 1 else "s")
            print("  %d of %d have their best backup; downloaded %d ancestor tile%s."
                  % (st["best"], n, st["downloaded"], "" if st["downloaded"] == 1 else "s"))
            if st["closer_missing"]:
                print("  %s: Google doesn't have a closer ancestor, so they use a coarser one." % tl(st["closer_missing"]))
            if st["none"]:
                print("  %s: no usable ancestor at Google (left out of the export)." % tl(st["none"]))
            if st["failed"]:
                print("  %d failed (%s: %s); run again to retry." % (st["failed"], st["first_failure"][0], st["first_failure"][1]))
            for t in no_geo[:10]:
                a = st["backup"].get(t)
                if a:
                    print("    %s <- L%d %s" % (t, len(a), a))

        if cancel.is_set():
            return 130
        have = [p for p in paths if lib.has(p)]
        if len(have) < len(paths):
            n = len(paths) - len(have)
            if not args.skip_missing:
                print("\nError: %d tile%s could not be downloaded; nothing was exported. Run again to retry, "
                      "or use --skip-missing to export without them." % (n, "" if n == 1 else "s"))
                return 1
            print("\n  Exporting without %d tile%s that could not be downloaded (--skip-missing)." % (n, "" if n == 1 else "s"))

        # 3. convert
        print("\n3/4 Convert to GLB (%d processes%s)" % (max(1, args.jobs), ", fast PNG" if args.fast_png else ""))
        skipped, failures = pipeline.convert(have, lib, args.jobs, not args.fast_png, force=no_geo, cancel=cancel)
        if failures:
            for p, e in list(failures.items())[:10]:
                print("  failed: %s: %s" % (p, e))
            print("Error: %d tile%s could not be converted; nothing was exported." % (len(failures), "" if len(failures) == 1 else "s"))
            return 1
        print("  all %d tiles are in the GLB cache%s" % (len(have) - len(skipped),
                                                         (", %d left out" % len(skipped)) if skipped else ""))

        # 4. export
        print("\n4/4 Export: 3D Tiles 1.1, small ZIP + script")
        zip_path, exported = pipeline.export_small_zip(have, lib, args.out, skipped)
        if skipped:
            print("  " + pipeline._left_out_note(skipped))
        size = os.path.getsize(zip_path)
        print("  %s (%d tiles, %s)" % (zip_path, len(exported), "%.1f KB" % (size / 1024)))
        print("\nDone in %s. To fill tiles/ after unzipping, run in the unzipped folder:" % pipeline._fmt_dur(time.time() - t_start))
        project = os.path.dirname(lib.root) if os.path.basename(lib.root) == "library" else lib.glb_dir
        print("  ./fill-tiles.sh %s" % _quote(project))
        print("(The README in the ZIP names ~/Downloads/earth-rea, exactly like the webapp's export; use the path above.)")
        return 0
    except KeyboardInterrupt:
        cancel.set()
        print("\nCancelled. Tiles already downloaded or converted stay in the cache.")
        return 130


def _quote(p):
    home = os.path.expanduser("~")
    if p.startswith(home + os.sep):
        p = "~" + p[len(home):]
    return p if re.match(r"^[~\w./-]+$", p) else "'%s'" % p.replace("'", "'\\''")
