"""JavaScript-compatible number and JSON formatting (Number#toString, JSON.stringify, toFixed, Math.round)."""

import json
import math
from decimal import Decimal, ROUND_HALF_UP


def js_num(x):
    """Number#toString / JSON.stringify text for a number (bools are not numbers here)."""
    if isinstance(x, int) and not isinstance(x, bool):
        return str(x)
    if x != x or x in (math.inf, -math.inf):
        return "null"
    if x == 0:
        return "0"
    r = repr(float(x))
    sign = ""
    if r[0] == "-":
        sign, r = "-", r[1:]
    if "e" in r:
        mant, e = r.split("e")
        e = int(e)
    else:
        mant, e = r, 0
    ip, _, fp = mant.partition(".")
    if fp == "0":
        fp = ""
    digits = ip + fp
    lead = len(digits) - len(digits.lstrip("0"))
    n = len(ip) + e - lead  # value = 0.DIGITS * 10^n
    digits = digits.strip("0")
    k = len(digits)
    if k <= n <= 21:
        s = digits + "0" * (n - k)
    elif 0 < n <= 21:
        s = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        s = "0." + "0" * (-n) + digits
    else:
        ex = n - 1
        s = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + ("+" if ex >= 0 else "-") + str(abs(ex))
    return sign + s


def js_round(x):
    """Math.round."""
    if x != x or x in (math.inf, -math.inf):
        return x
    r = math.floor(x)
    return float(r + 1 if x - r >= 0.5 else r)


def js_to_fixed(x, digits):
    """Number#toFixed (exact decimal value, ties rounded up in magnitude)."""
    if x != x:
        return "NaN"
    if abs(x) >= 1e21:
        return js_num(x)
    q = abs(Decimal(x)).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    return ("-" if x < 0 else "") + format(q, "f")


def _str(s):
    return json.dumps(s, ensure_ascii=False)


def stringify(v, indent=None):
    """JSON.stringify(v) or JSON.stringify(v, null, indent). Dicts keep insertion order."""
    if indent is None:
        return _compact(v)
    return _pretty(v, " " * indent if isinstance(indent, int) else indent, "")


def _compact(v):
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return js_num(v)
    if isinstance(v, str):
        return _str(v)
    if isinstance(v, dict):
        return "{" + ",".join(_str(str(k)) + ":" + _compact(x) for k, x in v.items()) + "}"
    return "[" + ",".join(_compact(x) for x in v) + "]"


def _pretty(v, ind, cur):
    if isinstance(v, dict):
        if not v:
            return "{}"
        nxt = cur + ind
        return "{\n" + ",\n".join(nxt + _str(str(k)) + ": " + _pretty(x, ind, nxt) for k, x in v.items()) + "\n" + cur + "}"
    if isinstance(v, (list, tuple)):
        if not v:
            return "[]"
        nxt = cur + ind
        return "[\n" + ",\n".join(nxt + _pretty(x, ind, nxt) for x in v) + "\n" + cur + "]"
    return _compact(v)
