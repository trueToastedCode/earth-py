#!/usr/bin/env python3
"""Strict, parallel SOCKS5 proxy test (standard library only, Python 3.7+).

A proxy ONLY counts as working if ALL of the following are true:

  1. The SOCKS5 handshake works (without login, or with user:pass if given).
  2. A real HTTPS request gets through the proxy (CONNECT + TLS).
     The certificate is verified normally -> proxies that tamper with TLS
     (MITM) or only return junk/ad pages are dropped.
  3. The response is HTTP 200 and the body is exactly one valid IP address
     (echo service) -> no hijacking, no HTML pages.
  4. This is repeated over several rounds (--rounds) against different,
     independent services (--min-success rounds must succeed).
  5. The exit IP is the same in all rounds (no rotating/flickering)
     and is NOT your own IP (no pass-through/transparent proxy).
  6. The median response time is below --max-latency.

Output:
  working_socks5.txt            addresses only (ip:port or user:pass@ip:port)
  working_socks5_analysis.csv   details per working proxy

Input (optional): JSON of the proxifly list OR a text file with one line per
         proxy (ip:port, socks5://ip:port, user:pass@ip:port).
         Without a file, the current proxifly SOCKS5 list is loaded automatically
         (or from --url, if given).

Usage:
  python3 check_socks5.py
  python3 check_socks5.py data.json
  python3 check_socks5.py --url https://example.com/list.txt
  python3 check_socks5.py --workers 300 --rounds 3 --timeout 10
"""
import argparse
import concurrent.futures as cf
import ipaddress
import json
import re
import socket
import ssl
import statistics
import struct
import sys
import time
import urllib.request
from urllib.parse import unquote, urlsplit

DEFAULT_LIST_URL = ("https://raw.githubusercontent.com/proxifly/free-proxy-list/"
                    "main/proxies/protocols/socks5/data.json")

DEFAULT_TARGETS = [
    "https://api.ipify.org/",
    "https://icanhazip.com/",
    "https://checkip.amazonaws.com/",
]


# --------------------------------------------------------------------------
# Input
# --------------------------------------------------------------------------
def fetch_text(url, timeout=30):
    """Load the list over HTTP(S) (github.com/.../blob/... is rewritten to raw)."""
    m = re.match(r"https?://github\.com/([^/]+/[^/]+)/blob/(.+)", url)
    if m:
        url = "https://raw.githubusercontent.com/%s/%s" % m.groups()
    req = urllib.request.Request(url, headers={"User-Agent": "check_socks5/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def load_entries(path=None, url=None):
    if path:
        text = open(path, encoding="utf-8").read()
    else:
        text = fetch_text(url or DEFAULT_LIST_URL)
    entries, seen = [], set()

    def add(host, port, user=None, pw=None, country=None, anonymity=None):
        key = (host, port, user)
        if key in seen:
            return
        seen.add(key)
        entries.append(dict(host=host, port=port, user=user, pw=pw,
                            country=country, anonymity=anonymity))

    if text.lstrip().startswith("["):
        for e in json.loads(text):
            add(e["ip"], int(e["port"]),
                country=(e.get("geolocation") or {}).get("country"),
                anonymity=e.get("anonymity"))
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            line = re.sub(r"^socks5h?://", "", line)
            user = pw = None
            if "@" in line:
                cred, line = line.rsplit("@", 1)
                user, _, pw = cred.partition(":")
                user, pw = unquote(user), unquote(pw)
            host, _, port = line.rpartition(":")
            if host and port.isdigit():
                add(host, int(port), user, pw)
    return entries


# --------------------------------------------------------------------------
# Low-level helpers (blocking sockets, deadline-based)
# --------------------------------------------------------------------------
def _left(deadline):
    rest = deadline - time.monotonic()
    if rest <= 0:
        raise socket.timeout("deadline")
    return rest


def recv_exact(sock, n, deadline):
    buf = b""
    while len(buf) < n:
        sock.settimeout(_left(deadline))
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return buf


def socks5_connect(sock, entry, host, port, deadline):
    """SOCKS5 handshake (+ optional RFC1929 login) and CONNECT to host:port."""
    has_creds = entry["user"] is not None
    sock.settimeout(_left(deadline))
    sock.sendall(b"\x05\x02\x00\x02" if has_creds else b"\x05\x01\x00")
    ver, method = recv_exact(sock, 2, deadline)
    if ver != 5:
        raise ValueError("not SOCKS5")
    if method == 0xFF:
        raise ValueError("no matching auth method")
    if method == 2:
        if not has_creds:
            raise ValueError("login required")
        u, p = entry["user"].encode(), (entry["pw"] or "").encode()
        sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
        if recv_exact(sock, 2, deadline)[1] != 0:
            raise ValueError("login rejected")
    elif method != 0:
        raise ValueError("unknown method")

    h = host.encode("idna")
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(h)]) + h + struct.pack(">H", port))
    head = recv_exact(sock, 4, deadline)
    if head[0] != 5 or head[1] != 0:
        raise ValueError("CONNECT rejected (rep=%d)" % head[1])
    atyp = head[3]
    if atyp == 1:
        recv_exact(sock, 6, deadline)
    elif atyp == 4:
        recv_exact(sock, 18, deadline)
    elif atyp == 3:
        n = recv_exact(sock, 1, deadline)[0]
        recv_exact(sock, n + 2, deadline)
    else:
        raise ValueError("invalid response")


def https_get_ip(sock, target, ctx, deadline):
    """TLS (with certificate verification) + GET; expects 200 and exactly one IP in the body."""
    host = target.hostname
    sock.settimeout(_left(deadline))
    tls = ctx.wrap_socket(sock, server_hostname=host)
    try:
        req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: curl/8.4.0\r\n"
               "Accept: */*\r\nConnection: close\r\n\r\n" % (target.path or "/", host))
        tls.settimeout(_left(deadline))
        tls.sendall(req.encode())
        buf = b""
        while len(buf) < 16384:
            tls.settimeout(_left(deadline))
            try:
                chunk = tls.recv(4096)
            except (ssl.SSLError, ConnectionError):
                if buf:
                    break
                raise
            if not chunk:
                break
            buf += chunk
    finally:
        try:
            tls.close()
        except Exception:
            pass

    head, sep, body = buf.partition(b"\r\n\r\n")
    if not sep:
        raise ValueError("no HTTP response")
    m = re.match(rb"HTTP/1\.[01] (\d{3})", head)
    if not m or m.group(1) != b"200":
        raise ValueError("HTTP status != 200")
    text = body.decode("ascii", "replace")
    if len(text.strip()) > 200:
        raise ValueError("body too large (hijacking?)")
    ips = set()
    for cand in re.findall(r"(?:\d{1,3}\.){3}\d{1,3}|[0-9a-fA-F]*:[0-9a-fA-F:]+", text):
        try:
            ips.add(str(ipaddress.ip_address(cand)))
        except ValueError:
            pass
    if len(ips) != 1:
        raise ValueError("body is not a single IP")
    return ips.pop()


def direct_ip(targets, ctx, timeout):
    """Find your own public IP (without a proxy)."""
    for t in targets:
        try:
            deadline = time.monotonic() + timeout
            s = socket.create_connection((t.hostname, t.port or 443), timeout=timeout)
            try:
                return https_get_ip(s, t, ctx, deadline)
            finally:
                s.close()
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------
# Checking one proxy
# --------------------------------------------------------------------------
def one_round(entry, target, ctx, timeout):
    t0 = time.monotonic()
    deadline = t0 + timeout
    sock = socket.create_connection((entry["host"], entry["port"]), timeout=timeout)
    try:
        connect_ms = int((time.monotonic() - t0) * 1000)
        socks5_connect(sock, entry, target.hostname, target.port or 443, deadline)
        ip = https_get_ip(sock, target, ctx, deadline)
        return ip, int((time.monotonic() - t0) * 1000), connect_ms
    finally:
        try:
            sock.close()
        except Exception:
            pass


def check_proxy(entry, targets, ctx, args, own_ip):
    results = []  # (ip, total_ms, connect_ms)
    fails = 0
    for i in range(args.rounds):
        try:
            results.append(one_round(entry, targets[i % len(targets)], ctx, args.timeout))
        except Exception:
            fails += 1
            if i == 0:                      # round 1 must succeed
                return None
            if args.rounds - fails < args.min_success:
                return None
        if i < args.rounds - 1:
            time.sleep(0.2)
    if len(results) < args.min_success:
        return None

    ips = {r[0] for r in results}
    if len(ips) != 1 and not args.allow_rotating:
        return None                         # exit IP flickers -> unreliable
    exit_ip = results[0][0]
    if own_ip and own_ip in ips:
        return None                         # passes your real IP through
    lat = [r[1] for r in results]
    med = int(statistics.median(lat))
    if med > args.max_latency:
        return None
    return {
        "host": entry["host"], "port": entry["port"],
        "user": entry["user"], "pw": entry["pw"],
        "ok_rounds": len(results), "rounds": args.rounds,
        "median_ms": med, "max_ms": max(lat),
        "connect_ms": int(statistics.median(r[2] for r in results)),
        "exit_ip": exit_ip, "exit_ips": len(ips),
        "country": entry["country"], "anonymity": entry["anonymity"],
    }


def address(r):
    base = "%s:%d" % (r["host"], r["port"])
    return "%s:%s@%s" % (r["user"], r["pw"], base) if r["user"] is not None else base


# --------------------------------------------------------------------------
def raise_fd_limit(wanted):
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        new = min(hard, max(soft, wanted)) if hard != resource.RLIM_INFINITY else max(soft, wanted)
        if new > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (new, hard))
        return new
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("file", nargs="?", default=None,
                    help="optional: local JSON/text file (default: load the list online)")
    ap.add_argument("--url", default=DEFAULT_LIST_URL,
                    help="URL of the proxy list if no file is given (default: proxifly socks5)")
    ap.add_argument("--workers", type=int, default=300, help="proxies tested in parallel (default 300)")
    ap.add_argument("--timeout", type=float, default=10, help="seconds per round (default 10)")
    ap.add_argument("--rounds", type=int, default=3, help="test rounds per proxy (default 3)")
    ap.add_argument("--min-success", type=int, default=None,
                    help="this many rounds must succeed (default: all)")
    ap.add_argument("--max-latency", type=int, default=5000,
                    help="max. median response time in ms (default 5000)")
    ap.add_argument("--allow-rotating", action="store_true",
                    help="don't drop proxies whose exit IP changes")
    ap.add_argument("--targets", default=",".join(DEFAULT_TARGETS),
                    help="comma-separated HTTPS URLs that return an IP as text")
    ap.add_argument("--cafile", default=None, help="custom CA (for tests only)")
    ap.add_argument("--own-ip", default=None, help="give your own IP instead of detecting it")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="working_socks5.txt", help="file with addresses only")
    ap.add_argument("--analysis", default="working_socks5_analysis.csv", help="file with the analysis")
    args = ap.parse_args()
    if args.min_success is None:
        args.min_success = args.rounds
    args.min_success = max(1, min(args.min_success, args.rounds))

    targets = [urlsplit(t.strip()) for t in args.targets.split(",") if t.strip()]
    ctx = ssl.create_default_context(cafile=args.cafile)

    limit = raise_fd_limit(args.workers * 2 + 100)
    if limit and args.workers * 2 + 100 > limit:
        args.workers = max(10, (limit - 100) // 2)
        print("Note: file limit %d -> workers reduced to %d" % (limit, args.workers))

    try:
        entries = load_entries(args.file, args.url)
    except Exception as e:
        sys.exit("Could not load the proxy list (%s): %s" % (args.file or args.url, e))
    print("List: %s (%d entries)" % (args.file or args.url, len(entries)))
    if args.limit:
        entries = entries[: args.limit]

    own_ip = args.own_ip or direct_ip(targets, ctx, 8)
    print("Own IP: %s" % (own_ip or "unknown (pass-through check off)"))
    print("Testing %d proxies | %d parallel | %d rounds (min. %d ok) | timeout %ss | max %d ms"
          % (len(entries), args.workers, args.rounds, args.min_success, args.timeout, args.max_latency),
          flush=True)

    ok, done, t0 = [], 0, time.monotonic()
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(check_proxy, e, targets, ctx, args, own_ip) for e in entries]
        try:
            for f in cf.as_completed(futs):
                done += 1
                try:
                    r = f.result()
                except Exception:
                    r = None
                if r:
                    ok.append(r)
                if done % 250 == 0:
                    print("  %d/%d checked, %d working" % (done, len(entries), len(ok)), flush=True)
        except KeyboardInterrupt:
            print("\nCancelled - saving the hits so far ...")
            for f in futs:
                f.cancel()

    ok.sort(key=lambda r: r["median_ms"])
    with open(args.out, "w") as f:
        for r in ok:
            f.write(address(r) + "\n")
    with open(args.analysis, "w", encoding="utf-8") as f:
        f.write("address;ok_rounds;median_ms;max_ms;connect_ms;exit_ip;country;anonymity;auth\n")
        for r in ok:
            f.write("%s;%d/%d;%d;%d;%d;%s;%s;%s;%s\n" % (
                address(r), r["ok_rounds"], r["rounds"], r["median_ms"], r["max_ms"],
                r["connect_ms"], r["exit_ip"], r["country"] or "", r["anonymity"] or "",
                "yes" if r["user"] is not None else "no"))
    print("\nDone in %.0fs: %d/%d working" % (time.monotonic() - t0, len(ok), len(entries)))
    print("  Addresses: %s\n  Analysis:  %s" % (args.out, args.analysis))
    for r in ok[:10]:
        print("  %-28s median %5d ms  exit=%s  %s" % (address(r), r["median_ms"], r["exit_ip"], r["country"] or ""))


if __name__ == "__main__":
    main()
