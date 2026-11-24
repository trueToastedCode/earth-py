"""
Crunched DXT1 (.crn, Google's texture format 6) -> plain DXT1 blocks, in pure Python.
A port of the DXT1 path of Binomial's public-domain crunch decoder (crn_decomp.h), which the
webapp's native converter compiles in; output is the same: level 0, rows of 8-byte blocks packed.
"""

import struct

_MAX_SYMS = 8192
_MAX_CODE = 16
_MAX_TABLE_BITS = 11
_MOST_PROBABLE = (17, 18, 19, 20, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15, 16)
_DXT1_FROM_LINEAR = (0, 2, 3, 1)
_CHUNK_TILES = ((0, 0, 0, 0), (0, 0, 1, 1), (0, 1, 0, 1), (0, 0, 1, 2), (1, 2, 0, 0),
                (0, 1, 0, 2), (1, 0, 2, 0), (0, 1, 2, 3))
_CHUNK_NUM_TILES = (1, 2, 2, 3, 3, 3, 3, 4)
_HEADER_SIZE = 74  # sizeof(crn_header)


class CrnError(Exception):
    pass


def _ceil_log2i(v):
    l = 0
    x = v
    while x > 1:
        x >>= 1
        l += 1
    if l != 32 and v > (1 << l):
        l += 1
    return l


class Model:
    """static_huffman_data_model + prefix_coding::decoder_tables."""
    __slots__ = ("total_syms", "code_sizes", "table_bits", "table_max_code", "start_len",
                 "max_codes", "val_ptrs", "lookup", "sorted")

    def prepare(self, code_sizes):
        num_syms = len(code_sizes)
        self.code_sizes = code_sizes
        self.total_syms = num_syms
        table_bits = min(1 + _ceil_log2i(num_syms), _MAX_TABLE_BITS) if num_syms > 16 else 0
        if not num_syms:
            raise CrnError("empty huffman table")
        num_codes = [0] * (_MAX_CODE + 1)
        for c in code_sizes:
            if c:
                num_codes[c] += 1
        max_codes = [0] * (_MAX_CODE + 1)
        val_ptrs = [0] * (_MAX_CODE + 1)
        min_codes = [0] * _MAX_CODE
        sorted_pos = [0] * (_MAX_CODE + 1)
        cur = 0
        used = 0
        min_size = 0xFFFFFFFF
        max_size = 0
        for i in range(1, _MAX_CODE + 1):
            n = num_codes[i]
            if n:
                min_size = min(min_size, i)
                max_size = max(max_size, i)
                min_codes[i - 1] = cur
                mc = cur + n - 1
                max_codes[i - 1] = 1 + ((mc << (16 - i)) | ((1 << (16 - i)) - 1))
                val_ptrs[i - 1] = used
                sorted_pos[i] = used
                cur += n
                used += n
            cur <<= 1
        order = [0] * used
        for i, c in enumerate(code_sizes):
            if c:
                order[sorted_pos[c]] = i
                sorted_pos[c] += 1
        if table_bits <= min_size:
            table_bits = 0
        lookup = None
        if table_bits:
            lookup = [0xFFFFFFFF] * (1 << table_bits)
            for size in range(1, table_bits + 1):
                if not num_codes[size]:
                    continue
                fill = table_bits - size
                k = max_codes[size - 1]
                max_code = 0xFFFFFFFF if not k else (k - 1) >> (16 - size)
                mn = min_codes[size - 1]
                vp = val_ptrs[size - 1]
                for code in range(mn, max_code + 1):
                    v = order[vp + code - mn] | (size << 16)
                    base = code << fill
                    for j in range(1 << fill):
                        lookup[base + j] = v
        for i in range(_MAX_CODE):
            val_ptrs[i] -= min_codes[i]
        table_max_code = 0
        start_len = min_size
        if table_bits:
            found = False
            for i in range(table_bits, 0, -1):
                if num_codes[i]:
                    table_max_code = max_codes[i - 1]
                    found = True
                    break
            if found:
                start_len = table_bits + 1
                for j in range(table_bits + 1, max_size + 1):
                    if num_codes[j]:
                        start_len = j
                        break
        max_codes[_MAX_CODE] = 0xFFFFFFFF
        val_ptrs[_MAX_CODE] = 0xFFFFF
        self.table_bits = table_bits
        self.table_max_code = table_max_code
        self.start_len = start_len
        self.max_codes = max_codes
        self.val_ptrs = val_ptrs
        self.lookup = lookup
        self.sorted = order
        return self


class Codec:
    """symbol_codec (decoding side)."""
    __slots__ = ("buf", "p", "end", "bit_buf", "bit_count")

    def start(self, data, ofs, size):
        if not size:
            raise CrnError("empty stream")
        self.buf = data
        self.p = ofs
        self.end = min(ofs + size, len(data))
        self.bit_buf = 0
        self.bit_count = 0

    def get_bits(self, n):
        while self.bit_count < n:
            c = 0
            if self.p < self.end:
                c = self.buf[self.p]
                self.p += 1
            self.bit_count += 8
            self.bit_buf |= c << (32 - self.bit_count)
        r = self.bit_buf >> (32 - n)
        self.bit_buf = (self.bit_buf << n) & 0xFFFFFFFF
        self.bit_count -= n
        return r

    def decode_bits(self, n):
        if not n:
            return 0
        if n > 16:
            a = self.get_bits(n - 16)
            return (a << 16) | self.get_bits(16)
        return self.get_bits(n)

    def decode(self, m):
        bc = self.bit_count
        if bc < 24:
            p = self.p
            end = self.end
            buf = self.buf
            if bc < 16:
                c0 = c1 = 0
                if p < end:
                    c0 = buf[p]
                    p += 1
                if p < end:
                    c1 = buf[p]
                    p += 1
                self.p = p
                bc += 16
                self.bit_buf |= ((c0 << 8) | c1) << (32 - bc)
            else:
                c = 0
                if p < end:
                    c = buf[p]
                    self.p = p + 1
                bc += 8
                self.bit_buf |= c << (32 - bc)
        bb = self.bit_buf
        k = (bb >> 16) + 1
        if k <= m.table_max_code:
            t = m.lookup[bb >> (32 - m.table_bits)]
            sym = t & 0xFFFF
            ln = t >> 16
        else:
            ln = m.start_len
            mc = m.max_codes
            while k > mc[ln - 1]:
                ln += 1
            vp = (m.val_ptrs[ln - 1] + (bb >> (32 - ln))) & 0xFFFFFFFF
            if vp >= m.total_syms:
                self.bit_count = bc
                return 0
            sym = m.sorted[vp]
        self.bit_buf = (bb << ln) & 0xFFFFFFFF
        self.bit_count = bc - ln
        return sym

    def receive_model(self):
        total = self.decode_bits(14)  # total_bits(cMaxSupportedSyms)
        if not total:
            return None
        sizes = [0] * total
        n_send = self.decode_bits(5)
        if n_send < 1 or n_send > 21:
            raise CrnError("bad huffman table")
        cl = [0] * 21
        for i in range(n_send):
            cl[_MOST_PROBABLE[i]] = self.decode_bits(3)
        dm = Model().prepare(cl)
        ofs = 0
        while ofs < total:
            rem = total - ofs
            code = self.decode(dm)
            if code <= 16:
                sizes[ofs] = code
                ofs += 1
            elif code == 17:
                ln = self.decode_bits(3) + 3
                if ln > rem:
                    raise CrnError("bad huffman table")
                ofs += ln
            elif code == 18:
                ln = self.decode_bits(7) + 11
                if ln > rem:
                    raise CrnError("bad huffman table")
                ofs += ln
            elif code in (19, 20):
                ln = self.decode_bits(2) + 3 if code == 19 else self.decode_bits(6) + 7
                if not ofs or ln > rem:
                    raise CrnError("bad huffman table")
                prev = sizes[ofs - 1]
                if not prev:
                    raise CrnError("bad huffman table")
                for _ in range(ln):
                    sizes[ofs] = prev
                    ofs += 1
            else:
                raise CrnError("bad huffman table")
        if ofs != total:
            raise CrnError("bad huffman table")
        return Model().prepare(sizes)


def _be(d, o, n):
    v = 0
    for i in range(n):
        v = (v << 8) | d[o + i]
    return v


def crn_info(src):
    """(width, height) from the .crn header, or None when it is not a valid crunched texture."""
    if len(src) < _HEADER_SIZE or _be(src, 0, 2) != 0x4878:
        return None
    if _be(src, 2, 2) < _HEADER_SIZE or len(src) < _be(src, 6, 4):
        return None
    return _be(src, 12, 2), _be(src, 14, 2)


def crn_to_dxt1(src):
    """Unpack a crunched texture into DXT1 blocks (8 bytes per 4x4 block, rows packed)."""
    info = crn_info(src) if src else None
    if not info:
        raise CrnError("invalid crunched texture")
    w, h = info
    levels = src[16]
    faces = src[17]
    fmt = src[18]
    ce_ofs, ce_size, ce_num = _be(src, 33, 3), _be(src, 36, 3), _be(src, 39, 2)
    cs_ofs, cs_size, cs_num = _be(src, 41, 3), _be(src, 44, 3), _be(src, 47, 2)
    ae_num = _be(src, 55, 2)
    tables_size, tables_ofs = _be(src, 65, 2), _be(src, 67, 3)
    if fmt != 0 or ae_num or faces != 1 or not ce_num:
        raise CrnError("could not decode crunched texture (unsupported crn format)")
    codec = Codec()
    # init_tables
    codec.start(src, tables_ofs, tables_size)
    chunk_dm = codec.receive_model()
    endpoint_dm = codec.receive_model()
    selector_dm = codec.receive_model()
    # decode_color_endpoints
    codec.start(src, ce_ofs, ce_size)
    dm0 = codec.receive_model()
    dm1 = codec.receive_model()
    endpoints = [0] * ce_num
    a = b = c = d = e = f = 0
    dec = codec.decode
    for i in range(ce_num):
        a = (a + dec(dm0)) & 31
        b = (b + dec(dm1)) & 63
        c = (c + dec(dm0)) & 31
        d = (d + dec(dm0)) & 31
        e = (e + dec(dm1)) & 63
        f = (f + dec(dm0)) & 31
        endpoints[i] = (c | (b << 5) | (a << 11) | (f << 16) | (e << 21) | (d << 27)) & 0xFFFFFFFF
    # decode_color_selectors
    codec.start(src, cs_ofs, cs_size)
    sdm = codec.receive_model()
    delta0 = [(i % 7) - 3 for i in range(49)]
    delta1 = [(i // 7) - 3 for i in range(49)]
    cur = [0] * 16
    selectors = [0] * cs_num
    fl = _DXT1_FROM_LINEAR
    for i in range(cs_num):
        for j in range(8):
            sym = dec(sdm)
            cur[j * 2] = (delta0[sym] + cur[j * 2]) & 3
            cur[j * 2 + 1] = (delta1[sym] + cur[j * 2 + 1]) & 3
        v = 0
        for k in range(16):
            v |= fl[cur[k]] << (2 * k)
        selectors[i] = v
    # unpack level 0
    level0 = _be(src, 70, 4)
    nxt = _be(src, 74, 4) if levels > 1 else len(src)
    blocks_x = (w + 3) >> 2
    blocks_y = (h + 3) >> 2
    pitch = blocks_x * 8
    out = bytearray(pitch * blocks_y)
    chunks_x = (blocks_x + 1) >> 1
    chunks_y = (blocks_y + 1) >> 1
    codec.start(src, level0, nxt - level0)
    pack = struct.Struct("<II").pack_into
    n_end = len(endpoints)
    n_sel = len(selectors)
    ce_bits = 1
    pe = 0
    ps = 0
    eps = [0, 0, 0, 0]
    for y in range(chunks_y):
        row = y * 2 * pitch
        xs = range(chunks_x) if not y & 1 else range(chunks_x - 1, -1, -1)
        skip_bottom = (y == chunks_y - 1) and (blocks_y & 1)
        for x in xs:
            if ce_bits == 1:
                ce_bits = dec(chunk_dm) | 512
            enc = ce_bits & 7
            ce_bits >>= 3
            for i in range(_CHUNK_NUM_TILES[enc]):
                pe = (pe + dec(endpoint_dm)) & 0xFFFFFFFF
                v = (pe - n_end) & 0xFFFFFFFF
                if not v & 0x80000000:
                    pe = v
                eps[i] = endpoints[pe]
            tiles = _CHUNK_TILES[enc]
            skip_right = (blocks_x & 1) and (x == chunks_x - 1)
            blk = row + x * 16
            if not skip_bottom and not skip_right:
                for t, o in ((0, blk), (1, blk + 8), (2, blk + pitch), (3, blk + pitch + 8)):
                    ps = (ps + dec(selector_dm)) & 0xFFFFFFFF
                    v = (ps - n_sel) & 0xFFFFFFFF
                    if not v & 0x80000000:
                        ps = v
                    pack(out, o, eps[tiles[t]], selectors[ps])
            else:
                for by in (0, 1):
                    for bx in (0, 1):
                        ps = (ps + dec(selector_dm)) & 0xFFFFFFFF
                        v = (ps - n_sel) & 0xFFFFFFFF
                        if not v & 0x80000000:
                            ps = v
                        if not ((bx and skip_right) or (by and skip_bottom)):
                            pack(out, blk + pitch * by + bx * 8, eps[tiles[bx + by * 2]], selectors[ps])
    return bytes(out)
