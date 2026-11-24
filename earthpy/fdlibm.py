"""
sin, cos, asin, atan2 exactly as the Rust `libm` crate 0.2.16 computes them (a port of
musl / FreeBSD msun, i.e. Sun's fdlibm). The webapp's native converter uses that crate, so these
give bit-identical results; Python's math module uses the platform libm, which can differ in
the last bit. Only the argument range the converter needs (|x| < 2^20 * pi/2) is supported for
sin/cos; larger arguments raise ValueError.
"""

import math
import struct

_d = struct.Struct("<d")
_q = struct.Struct("<Q")


def _bits(x):
    return _q.unpack(_d.pack(x))[0]


def _from_bits(b):
    return _d.unpack(_q.pack(b & 0xFFFFFFFFFFFFFFFF))[0]


def _hi(x):
    return _q.unpack(_d.pack(x))[0] >> 32


# ------------------------------------------------------------------ kernels (k_sin.rs, k_cos.rs)

_S1 = -1.66666666666666324348e-01
_S2 = 8.33333333332248946124e-03
_S3 = -1.98412698298579493134e-04
_S4 = 2.75573137070700676789e-06
_S5 = -2.50507602534068634195e-08
_S6 = 1.58969099521155010221e-10


def _k_sin(x, y, iy):
    z = x * x
    w = z * z
    r = _S2 + z * (_S3 + z * _S4) + z * w * (_S5 + z * _S6)
    v = z * x
    if iy == 0:
        return x + v * (_S1 + z * r)
    return x - ((z * (0.5 * y - v * r) - y) - v * _S1)


_C1 = 4.16666666666666019037e-02
_C2 = -1.38888888888741095749e-03
_C3 = 2.48015872894767294178e-05
_C4 = -2.75573143513906633035e-07
_C5 = 2.08757232129817482790e-09
_C6 = -1.13596475577881948265e-11


def _k_cos(x, y):
    z = x * x
    w = z * z
    r = z * (_C1 + z * (_C2 + z * _C3)) + w * w * (_C4 + z * (_C5 + z * _C6))
    hz = 0.5 * z
    w = 1.0 - hz
    return w + (((1.0 - w) - hz) + (z * r - x * y))


# ------------------------------------------------------------------ rem_pio2.rs

_EPS = 2.2204460492503131e-16
_TO_INT = 1.5 / _EPS
_INV_PIO2 = 6.36619772367581382433e-01
_PIO2_1 = 1.57079632673412561417e+00
_PIO2_1T = 6.07710050650619224932e-11
_PIO2_2 = 6.07710050630396597660e-11
_PIO2_2T = 2.02226624879595063154e-21
_PIO2_3 = 2.02226624871116645580e-21
_PIO2_3T = 8.47842766036889956997e-32


def _medium(x, ix):
    tmp = x * _INV_PIO2 + _TO_INT
    f_n = tmp - _TO_INT
    n = int(f_n)
    r = x - f_n * _PIO2_1
    w = f_n * _PIO2_1T
    y0 = r - w
    ey = (_bits(y0) >> 52) & 0x7FF
    ex = ix >> 20
    if ex - ey > 16:
        t = r
        w = f_n * _PIO2_2
        r = t - w
        w = f_n * _PIO2_2T - ((t - r) - w)
        y0 = r - w
        ey = (_bits(y0) >> 52) & 0x7FF
        if ex - ey > 49:
            t = r
            w = f_n * _PIO2_3
            r = t - w
            w = f_n * _PIO2_3T - ((t - r) - w)
            y0 = r - w
    y1 = (r - y0) - w
    return n, y0, y1


def _rem_pio2(x):
    b = _bits(x)
    sign = b >> 63
    ix = (b >> 32) & 0x7FFFFFFF
    if ix <= 0x400F6A7A:
        if (ix & 0xFFFFF) == 0x921FB:
            return _medium(x, ix)
        if ix <= 0x4002D97C:
            if sign == 0:
                z = x - _PIO2_1
                y0 = z - _PIO2_1T
                return 1, y0, (z - y0) - _PIO2_1T
            z = x + _PIO2_1
            y0 = z + _PIO2_1T
            return -1, y0, (z - y0) + _PIO2_1T
        if sign == 0:
            z = x - 2.0 * _PIO2_1
            y0 = z - 2.0 * _PIO2_1T
            return 2, y0, (z - y0) - 2.0 * _PIO2_1T
        z = x + 2.0 * _PIO2_1
        y0 = z + 2.0 * _PIO2_1T
        return -2, y0, (z - y0) + 2.0 * _PIO2_1T
    if ix <= 0x401C463B:
        if ix <= 0x4015FDBC:
            if ix == 0x4012D97C:
                return _medium(x, ix)
            if sign == 0:
                z = x - 3.0 * _PIO2_1
                y0 = z - 3.0 * _PIO2_1T
                return 3, y0, (z - y0) - 3.0 * _PIO2_1T
            z = x + 3.0 * _PIO2_1
            y0 = z + 3.0 * _PIO2_1T
            return -3, y0, (z - y0) + 3.0 * _PIO2_1T
        if ix == 0x401921FB:
            return _medium(x, ix)
        if sign == 0:
            z = x - 4.0 * _PIO2_1
            y0 = z - 4.0 * _PIO2_1T
            return 4, y0, (z - y0) - 4.0 * _PIO2_1T
        z = x + 4.0 * _PIO2_1
        y0 = z + 4.0 * _PIO2_1T
        return -4, y0, (z - y0) + 4.0 * _PIO2_1T
    if ix < 0x413921FB:
        return _medium(x, ix)
    if ix >= 0x7FF00000:
        y0 = x - x
        return 0, y0, y0
    raise ValueError("argument too large for this sin/cos port: %r" % x)


# ------------------------------------------------------------------ sin.rs, cos.rs

def sin(x):
    ix = _hi(x) & 0x7FFFFFFF
    if ix <= 0x3FE921FB:
        if ix < 0x3E500000:
            return x
        return _k_sin(x, 0.0, 0)
    if ix >= 0x7FF00000:
        return x - x
    n, y0, y1 = _rem_pio2(x)
    n &= 3
    if n == 0:
        return _k_sin(y0, y1, 1)
    if n == 1:
        return _k_cos(y0, y1)
    if n == 2:
        return -_k_sin(y0, y1, 1)
    return -_k_cos(y0, y1)


def cos(x):
    ix = _hi(x) & 0x7FFFFFFF
    if ix <= 0x3FE921FB:
        if ix < 0x3E46A09E:
            return 1.0
        return _k_cos(x, 0.0)
    if ix >= 0x7FF00000:
        return x - x
    n, y0, y1 = _rem_pio2(x)
    n &= 3
    if n == 0:
        return _k_cos(y0, y1)
    if n == 1:
        return -_k_sin(y0, y1, 1)
    if n == 2:
        return -_k_cos(y0, y1)
    return _k_sin(y0, y1, 1)


# ------------------------------------------------------------------ asin.rs

_PIO2_HI = 1.57079632679489655800e+00
_PIO2_LO = 6.12323399573676603587e-17
_P_S0 = 1.66666666666666657415e-01
_P_S1 = -3.25565818622400915405e-01
_P_S2 = 2.01212532134862925881e-01
_P_S3 = -4.00555345006794114027e-02
_P_S4 = 7.91534994289814532176e-04
_P_S5 = 3.47933107596021167570e-05
_Q_S1 = -2.40339491173441421878e+00
_Q_S2 = 2.02094576023350569471e+00
_Q_S3 = -6.88283971605453293030e-01
_Q_S4 = 7.70381505559019352791e-02
_TINY_1 = _from_bits(0x3870000000000000)


def _comp_r(z):
    p = z * (_P_S0 + z * (_P_S1 + z * (_P_S2 + z * (_P_S3 + z * (_P_S4 + z * _P_S5)))))
    q = 1.0 + z * (_Q_S1 + z * (_Q_S2 + z * (_Q_S3 + z * _Q_S4)))
    return p / q


def asin(x):
    b = _bits(x)
    hx = b >> 32
    ix = hx & 0x7FFFFFFF
    if ix >= 0x3FF00000:
        lx = b & 0xFFFFFFFF
        if ((ix - 0x3FF00000) | lx) == 0:
            return x * _PIO2_HI + _TINY_1
        return float("nan")
    if ix < 0x3FE00000:
        if 0x00100000 <= ix < 0x3E500000:
            return x
        return x + x * _comp_r(x * x)
    z = (1.0 - abs(x)) * 0.5
    s = math.sqrt(z)  # correctly rounded, same as libm sqrt
    r = _comp_r(z)
    if ix >= 0x3FEF3333:
        x = _PIO2_HI - (2.0 * (s + s * r) - _PIO2_LO)
    else:
        f = _from_bits(_bits(s) & 0xFFFFFFFF00000000)
        c = (z - f * f) / (s + f)
        x = 0.5 * _PIO2_HI - (2.0 * s * r - (_PIO2_LO - 2.0 * c) - (0.5 * _PIO2_HI - 2.0 * f))
    return -x if hx >> 31 else x


# ------------------------------------------------------------------ atan.rs, atan2.rs

_ATANHI = (4.63647609000806093515e-01, 7.85398163397448278999e-01,
           9.82793723247329054082e-01, 1.57079632679489655800e+00)
_ATANLO = (2.26987774529616870924e-17, 3.06161699786838301793e-17,
           1.39033110312309984516e-17, 6.12323399573676603587e-17)
_AT = (3.33333333333329318027e-01, -1.99999999998764832476e-01, 1.42857142725034663711e-01,
       -1.11111104054623557880e-01, 9.09088713343650656196e-02, -7.69187620504482999495e-02,
       6.66107313738753120669e-02, -5.83357013379057348645e-02, 4.97687799461593236017e-02,
       -3.65315727442169155270e-02, 1.62858201153657823623e-02)
_TINY_120 = _from_bits(0x03800000)  # as in the Rust source (f64::from_bits(0x0380_0000))


def atan(x):
    ix = _hi(x)
    sign = ix >> 31
    ix &= 0x7FFFFFFF
    if ix >= 0x44100000:
        if x != x:
            return x
        z = _ATANHI[3] + _TINY_120
        return -z if sign else z
    if ix < 0x3FDC0000:
        if ix < 0x3E400000:
            return x
        id_ = -1
    else:
        x = abs(x)
        if ix < 0x3FF30000:
            if ix < 0x3FE60000:
                x = (2.0 * x - 1.0) / (2.0 + x)
                id_ = 0
            else:
                x = (x - 1.0) / (x + 1.0)
                id_ = 1
        elif ix < 0x40038000:
            x = (x - 1.5) / (1.0 + 1.5 * x)
            id_ = 2
        else:
            x = -1.0 / x
            id_ = 3
    z = x * x
    w = z * z
    s1 = z * (_AT[0] + w * (_AT[2] + w * (_AT[4] + w * (_AT[6] + w * (_AT[8] + w * _AT[10])))))
    s2 = w * (_AT[1] + w * (_AT[3] + w * (_AT[5] + w * (_AT[7] + w * _AT[9]))))
    if id_ < 0:
        return x - x * (s1 + s2)
    z = _ATANHI[id_] - (x * (s1 + s2) - _ATANLO[id_] - x)
    return -z if sign else z


_PI = 3.1415926535897931160E+00
_PI_LO = 1.2246467991473531772E-16


def atan2(y, x):
    if x != x or y != y:
        return x + y
    bx = _bits(x)
    by = _bits(y)
    ix = bx >> 32
    lx = bx & 0xFFFFFFFF
    iy = by >> 32
    ly = by & 0xFFFFFFFF
    if (((ix - 0x3FF00000) & 0xFFFFFFFF) | lx) == 0:
        return atan(y)
    m = ((iy >> 31) & 1) | ((ix >> 30) & 2)
    ix &= 0x7FFFFFFF
    iy &= 0x7FFFFFFF
    if (iy | ly) == 0:
        if m in (0, 1):
            return y
        return _PI if m == 2 else -_PI
    if (ix | lx) == 0:
        return -_PI / 2.0 if m & 1 else _PI / 2.0
    if ix == 0x7FF00000:
        if iy == 0x7FF00000:
            return (_PI / 4.0, -_PI / 4.0, 3.0 * _PI / 4.0, -3.0 * _PI / 4.0)[m]
        return (0.0, -0.0, _PI, -_PI)[m]
    if ((ix + (64 << 20)) & 0xFFFFFFFF) < iy or iy == 0x7FF00000:
        return -_PI / 2.0 if m & 1 else _PI / 2.0
    if (m & 2) and ((iy + (64 << 20)) & 0xFFFFFFFF) < ix:
        z = 0.0
    else:
        z = atan(abs(y / x))
    if m == 0:
        return z
    if m == 1:
        return -z
    if m == 2:
        return _PI - (z - _PI_LO)
    return (z - _PI_LO) - _PI


def hypot3(a, b, c):
    """V8's Math.hypot for three values (scaled, compensated sum), as the converter uses it."""
    mx = 0.0
    for v in (a, b, c):
        v = abs(v)
        if v == float("inf"):
            return float("inf")
        if v != v:
            return float("nan")
        if v > mx:
            mx = v
    if mx == 0.0:
        return 0.0
    s = 0.0
    comp = 0.0
    for v in (a, b, c):
        n = abs(v) / mx
        summand = n * n - comp
        pre = s + summand
        comp = (pre - s) - summand
        s = pre
    return math.sqrt(s) * mx
