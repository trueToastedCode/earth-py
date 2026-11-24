"""
Math.cos exactly as Node 20 (V8 11.3, the webapp's Docker image) computes it: V8's own port of
the original fdlibm (src/base/ieee754.cc: __ieee754_rem_pio2, __kernel_cos, __kernel_sin). It
differs from musl's fdlibm (fdlibm.py, used by the native converter) in the kernels and the
argument reduction. Used for the JavaScript parts of the export (tileset.json geometric errors,
location.json / README sizes). Range: |x| <= 2^19 * pi/2.
"""

import struct

_d = struct.Struct("<d")
_q = struct.Struct("<Q")


def _bits(x):
    return _q.unpack(_d.pack(x))[0]


def _from_bits(b):
    return _d.unpack(_q.pack(b & 0xFFFFFFFFFFFFFFFF))[0]


def _hi_signed(x):
    h = _bits(x) >> 32
    return h - 0x100000000 if h & 0x80000000 else h


_C1 = 4.16666666666666019037e-02
_C2 = -1.38888888888741095749e-03
_C3 = 2.48015872894767294178e-05
_C4 = -2.75573143513906633035e-07
_C5 = 2.08757232129817482790e-09
_C6 = -1.13596475577881948265e-11


def _kernel_cos(x, y):
    ix = _hi_signed(x) & 0x7FFFFFFF
    if ix < 0x3E400000:
        return 1.0
    z = x * x
    r = z * (_C1 + z * (_C2 + z * (_C3 + z * (_C4 + z * (_C5 + z * _C6)))))
    if ix < 0x3FD33333:
        return 1.0 - (0.5 * z - (z * r - x * y))
    if ix > 0x3FE90000:
        qx = 0.28125
    else:
        qx = _from_bits((ix - 0x00200000) << 32)
    iz = 0.5 * z - qx
    a = 1.0 - qx
    return a - (iz - (z * r - x * y))


_S1 = -1.66666666666666324348e-01
_S2 = 8.33333333332248946124e-03
_S3 = -1.98412698298579493134e-04
_S4 = 2.75573137070700676789e-06
_S5 = -2.50507602534068634195e-08
_S6 = 1.58969099521155010221e-10


def _kernel_sin(x, y, iy):
    ix = _hi_signed(x) & 0x7FFFFFFF
    if ix < 0x3E400000:
        return x
    z = x * x
    v = z * x
    r = _S2 + z * (_S3 + z * (_S4 + z * (_S5 + z * _S6)))
    if iy == 0:
        return x + v * (_S1 + z * r)
    return x - ((z * (0.5 * y - v * r) - y) - v * _S1)


_NPIO2_HW = (
    0x3FF921FB, 0x400921FB, 0x4012D97C, 0x401921FB, 0x401F6A7A, 0x4022D97C,
    0x4025FDBB, 0x402921FB, 0x402C463A, 0x402F6A7A, 0x4031475C, 0x4032D97C,
    0x40346B9C, 0x4035FDBB, 0x40378FDB, 0x403921FB, 0x403AB41B, 0x403C463A,
    0x403DD85A, 0x403F6A7A, 0x40407E4C, 0x4041475C, 0x4042106C, 0x4042D97C,
    0x4043A28C, 0x40446B9C, 0x404534AC, 0x4045FDBB, 0x4046C6CB, 0x40478FDB,
    0x404858EB, 0x404921FB,
)
_invpio2 = 6.36619772367581382433e-01
_pio2_1 = 1.57079632673412561417e+00
_pio2_1t = 6.07710050650619224932e-11
_pio2_2 = 6.07710050630396597660e-11
_pio2_2t = 2.02226624879595063154e-21
_pio2_3 = 2.02226624871116645580e-21
_pio2_3t = 8.47842766036889956997e-32


def _rem_pio2(x):
    hx = _hi_signed(x)
    ix = hx & 0x7FFFFFFF
    if ix <= 0x3FE921FB:
        return 0, x, 0.0
    if ix < 0x4002D97C:
        if hx > 0:
            z = x - _pio2_1
            if ix != 0x3FF921FB:
                y0 = z - _pio2_1t
                return 1, y0, (z - y0) - _pio2_1t
            z -= _pio2_2
            y0 = z - _pio2_2t
            return 1, y0, (z - y0) - _pio2_2t
        z = x + _pio2_1
        if ix != 0x3FF921FB:
            y0 = z + _pio2_1t
            return -1, y0, (z - y0) + _pio2_1t
        z += _pio2_2
        y0 = z + _pio2_2t
        return -1, y0, (z - y0) + _pio2_2t
    if ix <= 0x413921FB:
        t = abs(x)
        n = int(t * _invpio2 + 0.5)
        fn = float(n)
        r = t - fn * _pio2_1
        w = fn * _pio2_1t
        if n < 32 and ix != _NPIO2_HW[n - 1]:
            y0 = r - w
        else:
            j = ix >> 20
            y0 = r - w
            i = j - ((_bits(y0) >> 52) & 0x7FF)
            if i > 16:
                t = r
                w = fn * _pio2_2
                r = t - w
                w = fn * _pio2_2t - ((t - r) - w)
                y0 = r - w
                i = j - ((_bits(y0) >> 52) & 0x7FF)
                if i > 49:
                    t = r
                    w = fn * _pio2_3
                    r = t - w
                    w = fn * _pio2_3t - ((t - r) - w)
                    y0 = r - w
        y1 = (r - y0) - w
        if hx < 0:
            return -n, -y0, -y1
        return n, y0, y1
    if ix >= 0x7FF00000:
        return 0, x - x, x - x
    raise ValueError("argument too large for this cos port: %r" % x)


def cos(x):
    ix = _hi_signed(x) & 0x7FFFFFFF
    if ix <= 0x3FE921FB:
        return _kernel_cos(x, 0.0)
    if ix >= 0x7FF00000:
        return x - x
    n, y0, y1 = _rem_pio2(x)
    n &= 3
    if n == 0:
        return _kernel_cos(y0, y1)
    if n == 1:
        return -_kernel_sin(y0, y1, 1)
    if n == 2:
        return -_kernel_cos(y0, y1)
    return _kernel_sin(y0, y1, 1)
