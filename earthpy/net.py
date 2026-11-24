"""
HTTP(S) requests to Google, optionally through SOCKS5 proxies, paced by a request limiter.
Ports of the webapp's webapp/lib/proxies.js (proxy list format) and webapp/lib/limiter.js
(pacing, burst pause, per-exit backoff). Standard library only.
"""

import http.client
import ipaddress
import re
import socket
import ssl
import struct
import threading
import time
from urllib.parse import urlsplit

MAX_PROXIES = 1000


# ------------------------------------------------------------------ proxy list (proxies.js)

def parse_proxy_list(text):
    """Returns (proxies, errors): proxies [{host, port, user, pass}], errors [{line, text, error}]."""
    proxies, errors, seen = [], [], set()
    for i, raw in enumerate(re.split(r"\r?\n", str(text or ""))):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        r = _parse_line(line)
        if "error" in r:
            errors.append({"line": i + 1, "text": _mask(line), "error": r["error"]})
            continue
        key = "%s@%s:%s" % (r["user"] or "", r["host"], r["port"])
        if key in seen:
            continue
        seen.add(key)
        if len(proxies) >= MAX_PROXIES:
            errors.append({"line": i + 1, "text": _mask(line), "error": "more than %d proxies" % MAX_PROXIES})
            continue
        proxies.append(r)
    return proxies, errors


def _parse_line(line):
    s = line
    m = re.match(r"^([a-z0-9]+)://", s, re.I)
    if m:
        if not re.match(r"^socks5h?$", m.group(1), re.I):
            return {"error": "only SOCKS5 proxies are supported (got %s://)" % m.group(1)}
        s = s[m.end():]
    user = passwd = ""
    at = s.rfind("@")
    if at >= 0:
        cred, s = s[:at], s[at + 1:]
        colon = cred.find(":")
        user = cred[:colon] if colon >= 0 else cred
        passwd = cred[colon + 1:] if colon >= 0 else ""
        if not user:
            return {"error": "user name is empty"}
        if len(user) > 255 or len(passwd) > 255:
            return {"error": "user name or password is longer than 255 characters"}
    m = re.match(r"^\[([0-9a-f:.]+)\]:(\d+)/?$", s, re.I) or re.match(r"^([^:\s/]+):(\d+)/?$", s)
    if not m:
        return {"error": "expected host:port"}
    port = int(m.group(2))
    if not 1 <= port <= 65535:
        return {"error": "invalid port %s" % m.group(2)}
    return {"host": m.group(1), "port": port, "user": user, "pass": passwd}


def proxy_label(p):
    host = "[%s]" % p["host"] if ":" in p["host"] else p["host"]
    return ("%s@" % p["user"] if p["user"] else "") + "%s:%d" % (host, p["port"])


def _mask(line):
    at = line.rfind("@")
    if at < 0:
        return line
    cred = line[:at]
    start = cred.find("://") + 3 if "://" in cred else 0
    colon = cred.find(":", start)
    return cred[:colon] + ":***" + line[at:] if colon >= 0 else line


# ------------------------------------------------------------------ SOCKS5 + HTTPS

class HttpError(Exception):
    def __init__(self, status, url):
        Exception.__init__(self, "HTTP status code %d for %s" % (status, url))
        self.status = status


class Cancelled(Exception):
    cancelled = True


def _recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("SOCKS5 proxy closed the connection")
        buf += chunk
    return buf


_SOCKS_ERRORS = {1: "general failure", 2: "connection not allowed", 3: "network unreachable",
                 4: "host unreachable", 5: "connection refused", 6: "TTL expired",
                 7: "command not supported", 8: "address type not supported"}


def socks5_connect(proxy, host, port, timeout=20.0):
    """Open a TCP connection to host:port through a SOCKS5 proxy (host names resolved by the proxy)."""
    s = socket.create_connection((proxy["host"], proxy["port"]), timeout=timeout)
    try:
        s.settimeout(timeout)
        if proxy["user"]:
            s.sendall(b"\x05\x02\x00\x02")
        else:
            s.sendall(b"\x05\x01\x00")
        ver, method = _recv_exact(s, 2)
        if ver != 5:
            raise ConnectionError("not a SOCKS5 proxy")
        if method == 2:
            u = proxy["user"].encode()
            pw = proxy["pass"].encode()
            s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(pw)]) + pw)
            if _recv_exact(s, 2)[1] != 0:
                raise ConnectionError("SOCKS5 authentication failed")
        elif method != 0:
            raise ConnectionError("SOCKS5 proxy wants an unsupported authentication method")
        try:
            ip = ipaddress.ip_address(host)
            addr = (b"\x01" if ip.version == 4 else b"\x04") + ip.packed
        except ValueError:
            hb = host.encode("idna")
            addr = b"\x03" + bytes([len(hb)]) + hb
        s.sendall(b"\x05\x01\x00" + addr + struct.pack(">H", port))
        ver, rep, _, atyp = _recv_exact(s, 4)
        if rep != 0:
            raise ConnectionError("SOCKS5 connect failed: %s" % _SOCKS_ERRORS.get(rep, "error %d" % rep))
        if atyp == 1:
            _recv_exact(s, 4)
        elif atyp == 4:
            _recv_exact(s, 16)
        elif atyp == 3:
            _recv_exact(s, _recv_exact(s, 1)[0])
        _recv_exact(s, 2)
        return s
    except BaseException:
        s.close()
        raise


_ssl_ctx = ssl.create_default_context()


class _Conn(http.client.HTTPSConnection):
    def __init__(self, host, proxy, timeout):
        http.client.HTTPSConnection.__init__(self, host, 443, timeout=timeout, context=_ssl_ctx)
        self._proxy = proxy

    def connect(self):
        if self._proxy is None:
            http.client.HTTPSConnection.connect(self)
            return
        sock = socks5_connect(self._proxy, self.host, self.port, 20.0)
        sock.settimeout(self.timeout)
        self.sock = _ssl_ctx.wrap_socket(sock, server_hostname=self.host)


class Exit:
    def __init__(self, ident, label, proxy=None, direct=False):
        self.id = ident
        self.label = label
        self.proxy = proxy
        self.direct = direct
        self.next_free = 0.0
        self.count = 0
        self.paused_until = 0.0
        self.blocked_until = 0.0
        self.streak = 0
        self.in_flight = 0
        self.last_error = ""
        self.requests = self.ok = self.failed = self.pauses = 0
        self._idle = []
        self._lock = threading.Lock()

    def ready_at(self):
        return max(self.next_free, self.paused_until, self.blocked_until)

    def fetch(self, url, timeout=60.0):
        """One GET request (keep-alive connections are reused per exit)."""
        u = urlsplit(url)
        path = u.path + ("?" + u.query if u.query else "")
        for attempt in (0, 1):
            with self._lock:
                conn = self._idle.pop() if self._idle else None
            reused = conn is not None
            if conn is None:
                conn = _Conn(u.hostname, self.proxy, timeout)
            try:
                conn.request("GET", path, headers={"Connection": "keep-alive"})
                resp = conn.getresponse()
                body = resp.read()
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError,
                    http.client.BadStatusLine) as e:
                conn.close()
                if reused and attempt == 0:
                    continue  # a stale kept-alive connection; retry once on a fresh one
                raise ConnectionError(str(e) or e.__class__.__name__)
            except BaseException:
                conn.close()
                raise
            if resp.status != 200:
                conn.close()
                raise HttpError(resp.status, url)
            if resp.will_close:
                conn.close()
            else:
                with self._lock:
                    if len(self._idle) < 32:
                        self._idle.append(conn)
                    else:
                        conn.close()
            return body
        raise ConnectionError("connection failed")


# ------------------------------------------------------------------ limiter (limiter.js)

def classify(err):
    """Who is to blame for a failed request: 'exit', 'both', 'request' or 'cancel'."""
    if isinstance(err, Cancelled):
        return "cancel"
    status = getattr(err, "status", None)
    if not status:
        return "exit"
    if status in (429, 403):
        return "exit"
    if status >= 500:
        return "both"
    return "request"


class RequestLimiter:
    """Paces every request over the exits (proxies, or the direct connection when there are none).
    Per exit: delay between request starts, pause after `burst` requests, and a backoff of 1 s,
    2 s, 4 s, ... (at most max(pause, 1 s)) after a failure caused by the exit. A request runs on
    the exit that is free first, moves on to the next free exit after an exit failure, waits for as
    long as no exit is free, and gives up only for request errors (404 etc.) or after
    max_request_failures server errors."""

    def __init__(self, delay_ms=100, burst=1000, pause_ms=60000, first_retry_ms=1000,
                 max_request_failures=5, log=print):
        self.delay = max(0, delay_ms) / 1000.0
        self.burst = max(0, int(burst))
        self.pause = max(0, pause_ms) / 1000.0
        self.first_retry = first_retry_ms / 1000.0
        self.max_backoff = max(self.pause, self.first_retry)
        self.max_request_failures = max_request_failures
        self.log = log
        self.lock = threading.Lock()
        self.requests = self.retries = self.failures = 0
        self.set_exits([])

    def set_exits(self, proxies):
        if proxies:
            self.exits = [Exit(proxy_label(p), proxy_label(p), proxy=p) for p in proxies]
        else:
            self.exits = [Exit("direct", "direct connection", direct=True)]

    def size(self):
        return len(self.exits)

    def run(self, fn, is_cancelled=lambda: False):
        server_errors = 0
        while True:
            if is_cancelled():
                raise Cancelled("cancelled")
            with self.lock:
                now = time.time()
                best = None
                for e in self.exits:
                    if best is None or e.ready_at() < best.ready_at():
                        best = e
                at = best.ready_at()
                if at > now:
                    wait = min(at - now, 1.0)
                    best = None
                else:
                    e = best
                    e.next_free = now + self.delay
                    e.count += 1
                    if self.burst and e.count >= self.burst:
                        e.count = 0
                        e.paused_until = now + self.pause
                        e.pauses += 1
                        self.log("Request limiter: %s made %d requests, pausing %d s"
                                 % (e.label, self.burst, round(self.pause)))
                    e.in_flight += 1
                    e.requests += 1
                    self.requests += 1
            if best is None:
                time.sleep(wait)
                continue
            e = best
            try:
                result = fn(e)
                with self.lock:
                    e.streak = 0
                    e.ok += 1
                return result
            except Exception as err:
                blame = classify(err)
                if blame == "cancel":
                    raise
                with self.lock:
                    if blame == "request":
                        e.streak = 0
                        self.failures += 1
                        raise
                    e.streak += 1
                    e.failed += 1
                    e.blocked_until = time.time() + min(self.first_retry * 2 ** (e.streak - 1), self.max_backoff)
                    e.last_error = str(err)[:300]
                    self.retries += 1
                    if blame == "both":
                        server_errors += 1
                        if server_errors >= self.max_request_failures:
                            self.failures += 1
                            raise
            finally:
                with self.lock:
                    e.in_flight -= 1

    def get(self, url, is_cancelled=lambda: False):
        return self.run(lambda e: e.fetch(url), is_cancelled)

    def status_line(self):
        now = time.time()
        with self.lock:
            ready = sum(1 for e in self.exits if e.ready_at() <= now or (e.blocked_until <= now and e.paused_until <= now))
            failing = sum(1 for e in self.exits if e.blocked_until > now)
            paused = sum(1 for e in self.exits if e.paused_until > now and e.blocked_until <= now)
        if len(self.exits) == 1 and self.exits[0].direct:
            return "direct connection" + (" (backing off)" if failing else " (paused)" if paused else "")
        return "%d proxies: %d ready, %d failing, %d paused" % (len(self.exits), ready, failing, paused)
