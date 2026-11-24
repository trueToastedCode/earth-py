"""
zlib (deflate) level 6 compression that produces exactly the bytes of the webapp's native
converter: Rust flate2 1.1 `ZlibEncoder` (32 KiB output buffer) on the `zlib-rs` 0.6.8 backend,
i.e. zlib-ng's "medium" strategy. Pure Python port of the code paths level 6 uses:
deflate_medium, longest_match, fill_window, the trees and the flate2 write/finish loop
(the output buffer size matters: deflate_medium forgets its look-ahead match when it returns).
"""

import struct
import zlib

_u32 = struct.Struct("<I").unpack_from
_u64 = struct.Struct("<Q").unpack_from

W_BITS = 15
W_SIZE = 1 << W_BITS
W_MASK = W_SIZE - 1
WINDOW_SIZE = 2 * W_SIZE
HASH_SIZE = 65536
STD_MIN_MATCH = 3
STD_MAX_MATCH = 258
WANT_MIN_MATCH = 4
MIN_LOOKAHEAD = STD_MAX_MATCH + STD_MIN_MATCH + 1
MAX_DIST = W_SIZE - MIN_LOOKAHEAD
LIT_BUFSIZE = 1 << (8 + 6)
SYM_FLUSH = LIT_BUFSIZE - 1  # sym_buf.should_flush_block: filled == len - 3
# level 6: good_length 8, max_lazy 16, nice_length 128, max_chain 128, deflate_medium
GOOD_MATCH, MAX_LAZY, NICE_MATCH, MAX_CHAIN = 8, 16, 128, 128

L_CODES, D_CODES, BL_CODES = 286, 30, 19
HEAP_SIZE = 2 * L_CODES + 1
MAX_BITS, MAX_BL_BITS = 15, 7
END_BLOCK = 256
REP_3_6, REPZ_3_10, REPZ_11_138 = 16, 17, 18

EXTRA_LBITS = (0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0)
EXTRA_DBITS = (0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13)
EXTRA_BLBITS = (0,) * 16 + (2, 3, 7)
BL_ORDER = (16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15)

NO_FLUSH, FINISH = 0, 4
NEED_MORE, BLOCK_DONE, FINISH_STARTED, FINISH_DONE = 0, 1, 2, 3
OK, STREAM_END, BUF_ERROR = 0, 1, -5
ST_INIT, ST_BUSY, ST_FINISH = 1, 2, 3


def _rev(code, ln):
    r = 0
    for _ in range(ln):
        r = (r << 1) | (code & 1)
        code >>= 1
    return r


def _tables():
    length_code = [0] * 256
    base_length = [0] * 29
    length = 0
    for code in range(28):
        base_length[code] = length
        for _ in range(1 << EXTRA_LBITS[code]):
            length_code[length] = code
            length += 1
    length_code[length - 1] = 28
    base_length[28] = 255 - 3 + 1 - 1  # 255 = 258 - 3
    base_length[28] = 255
    dist_code = [0] * 512
    base_dist = [0] * 30
    dist = 0
    for code in range(16):
        base_dist[code] = dist
        for _ in range(1 << EXTRA_DBITS[code]):
            dist_code[dist] = code
            dist += 1
    dist >>= 7
    for code in range(16, 30):
        base_dist[code] = dist << 7
        for _ in range(1 << (EXTRA_DBITS[code] - 7)):
            dist_code[256 + dist] = code
            dist += 1
    # static literal/length tree
    slen = [8] * 144 + [9] * 112 + [7] * 24 + [8] * 8
    bl_count = [0] * (MAX_BITS + 1)
    for l in slen:
        bl_count[l] += 1
    nxt = [0] * (MAX_BITS + 1)
    code = 0
    for bits in range(1, MAX_BITS + 1):
        code = (code + bl_count[bits - 1]) << 1
        nxt[bits] = code
    scode = [0] * 288
    for n in range(288):
        l = slen[n]
        scode[n] = _rev(nxt[l], l)
        nxt[l] += 1
    sdcode = [_rev(n, 5) for n in range(D_CODES)]
    sdlen = [5] * D_CODES
    return length_code, base_length, dist_code, base_dist, slen, scode, sdlen, sdcode


LENGTH_CODE, BASE_LENGTH, DIST_CODE, BASE_DIST, SLTREE_LEN, SLTREE_CODE, SDTREE_LEN, SDTREE_CODE = _tables()


def _d_code(dist):
    return DIST_CODE[dist] if dist < 256 else DIST_CODE[256 + (dist >> 7)]


class _Tree:
    __slots__ = ("fc", "dl", "max_code", "stree_len", "extra", "base", "elems", "max_length")

    def __init__(self, size, stree_len, extra, base, elems, max_length):
        self.fc = [0] * size  # freq / code
        self.dl = [0] * size  # dad / len
        self.max_code = 0
        self.stree_len = stree_len
        self.extra = extra
        self.base = base
        self.elems = elems
        self.max_length = max_length


class Deflater:
    def __init__(self, data):
        self.inp = data
        self.next_in = 0
        self.avail_in = 0
        self.total_in = 0
        self.out = bytearray()
        self.avail_out = 0
        self.total_out = 0
        self.adler = 1
        self.window = bytearray(WINDOW_SIZE)
        self.prev = [0] * W_SIZE
        self.head = [0] * HASH_SIZE
        self.pending = bytearray()
        self.pend_pos = 0
        self.bi_buf = 0
        self.bi_valid = 0
        self.status = ST_INIT
        self.wrap = 1
        self.last_flush = -2
        self.strstart = 0
        self.block_start = 0
        self.lookahead = 0
        self.insert = 0
        self.match_start = 0
        self.prev_length = 0
        self.syms = []
        self.opt_len = 0
        self.static_len = 0
        self.l = _Tree(HEAP_SIZE, SLTREE_LEN, EXTRA_LBITS, 257, L_CODES, MAX_BITS)
        self.d = _Tree(2 * D_CODES + 1, SDTREE_LEN, EXTRA_DBITS, 0, D_CODES, MAX_BITS)
        self.bl = _Tree(2 * BL_CODES + 1, None, EXTRA_BLBITS, 0, BL_CODES, MAX_BL_BITS)
        self._init_block()

    # ----------------------------------------------------------- bit output

    def _send(self, val, ln):
        self.bi_buf |= val << self.bi_valid
        self.bi_valid += ln
        if self.bi_valid > 4096:
            self._flush_bits()

    def _flush_bits(self):
        n = self.bi_valid >> 3
        if n:
            self.pending += (self.bi_buf & ((1 << (8 * n)) - 1)).to_bytes(n, "little")
            self.bi_buf >>= 8 * n
            self.bi_valid -= 8 * n

    def _align(self):
        n = (self.bi_valid + 7) >> 3
        if n:
            self.pending += self.bi_buf.to_bytes(n, "little")
        self.bi_buf = 0
        self.bi_valid = 0

    def _flush_pending(self):
        self._flush_bits()
        avail = len(self.pending) - self.pend_pos
        ln = min(avail, self.avail_out)
        if ln == 0:
            return
        self.out += self.pending[self.pend_pos:self.pend_pos + ln]
        self.pend_pos += ln
        self.total_out += ln
        self.avail_out -= ln
        if self.pend_pos == len(self.pending):
            self.pending = bytearray()
            self.pend_pos = 0

    def _has_pending(self):
        return self.pend_pos < len(self.pending) or self.bi_valid >= 8

    # ----------------------------------------------------------- hashing

    def _quick_insert(self, s):
        w = self.window
        val = w[s] | (w[s + 1] << 8) | (w[s + 2] << 16) | (w[s + 3] << 24)
        hm = ((val * 2654435761) & 0xFFFFFFFF) >> 16
        head = self.head
        h = head[hm]
        if h != s:
            self.prev[s & W_MASK] = h
            head[hm] = s
        return h

    def _insert_string(self, s, count):
        w = self.window
        end = min(WINDOW_SIZE, s + count + 3) - 3
        head = self.head
        prev = self.prev
        for idx in range(s, end):
            val = _u32(w, idx)[0]
            hm = ((val * 2654435761) & 0xFFFFFFFF) >> 16
            h = head[hm]
            i16 = idx & 0xFFFF
            if h != i16:
                prev[i16 & W_MASK] = h
                head[hm] = i16

    # ----------------------------------------------------------- window

    def _read_buf(self, offset, size):
        ln = min(self.avail_in, size)
        if ln == 0:
            return 0
        chunk = self.inp[self.next_in:self.next_in + ln]
        self.window[offset:offset + ln] = chunk
        self.adler = zlib.adler32(chunk, self.adler)
        self.next_in += ln
        self.avail_in -= ln
        self.total_in += ln
        return ln

    def _fill_window(self):
        while True:
            more = WINDOW_SIZE - self.lookahead - self.strstart
            if self.strstart >= W_SIZE + MAX_DIST:
                w = self.window
                w[0:W_SIZE] = w[W_SIZE:2 * W_SIZE]
                if self.match_start >= W_SIZE:
                    self.match_start -= W_SIZE
                else:
                    self.match_start = 0
                    self.prev_length = 0
                self.strstart -= W_SIZE
                self.block_start -= W_SIZE
                self.insert = min(self.insert, self.strstart)
                self.head = [v - W_SIZE if v >= W_SIZE else 0 for v in self.head]
                self.prev = [v - W_SIZE if v >= W_SIZE else 0 for v in self.prev]
                more += W_SIZE
            if self.avail_in == 0:
                break
            n = self._read_buf(self.strstart + self.lookahead, more)
            self.lookahead += n
            if self.lookahead + self.insert >= STD_MIN_MATCH:
                string = self.strstart - self.insert
                if string >= 1:
                    self._quick_insert(string + 2 - STD_MIN_MATCH)
                count = self.insert
                if self.lookahead == 1:
                    count -= 1
                if count > 0:
                    self._insert_string(string, count)
                    self.insert -= count
            if not (self.lookahead < MIN_LOOKAHEAD and self.avail_in != 0):
                break

    # ----------------------------------------------------------- match finding

    def _longest_match(self, cur_match):
        w = self.window
        prev = self.prev
        match_start = self.match_start
        strstart = self.strstart
        lookahead = self.lookahead
        best_len = self.prev_length if self.prev_length > 0 else STD_MIN_MATCH - 1
        offset = best_len - 1
        if best_len >= 4:
            offset -= 2
            if best_len >= 8:
                offset -= 4
        chain = MAX_CHAIN
        if best_len >= GOOD_MATCH:
            chain >>= 2
        limit = strstart - MAX_DIST if strstart > MAX_DIST else 0
        scan_start = _u64(w, strstart)[0]
        scan_end = _u64(w, strstart + offset)[0]
        while True:
            if cur_match >= strstart:
                break
            ln = 0
            if best_len < 8:
                while True:
                    cmp = scan_start ^ _u64(w, cur_match)[0]
                    if cmp == 0:
                        break
                    cl = ((cmp & -cmp).bit_length() - 1) >> 3
                    if cl > best_len:
                        ln = cl
                        break
                    chain -= 1
                    if chain > 0:
                        cur_match = prev[cur_match & W_MASK]
                        if cur_match > limit:
                            continue
                    return best_len, match_start
            else:
                while True:
                    if _u64(w, cur_match + offset)[0] == scan_end and _u64(w, cur_match)[0] == scan_start:
                        break
                    chain -= 1
                    if chain > 0:
                        cur_match = prev[cur_match & W_MASK]
                        if cur_match > limit:
                            continue
                    return best_len, match_start
            if ln == 0:
                a = strstart + 2
                b = cur_match + 2
                if w[a:a + 256] == w[b:b + 256]:
                    ln = 258
                else:
                    i = 0
                    while w[a + i:a + i + 16] == w[b + i:b + i + 16]:
                        i += 16
                    while w[a + i] == w[b + i]:
                        i += 1
                    ln = i + 2
            if ln > best_len:
                match_start = cur_match
                if ln >= lookahead:
                    return lookahead, match_start
                best_len = ln
                if best_len >= NICE_MATCH:
                    return best_len, match_start
                offset = best_len - 1
                if best_len >= 4:
                    offset -= 2
                    if best_len >= 8:
                        offset -= 4
                scan_end = _u64(w, strstart + offset)[0]
            chain -= 1
            if chain > 0:
                cur_match = prev[cur_match & W_MASK]
                if cur_match > limit:
                    continue
            return best_len, match_start
        return best_len, match_start

    # ----------------------------------------------------------- symbols

    def _tally_lit(self, c):
        self.syms.append((0, c))
        self.l.fc[c] += 1
        return len(self.syms) == SYM_FLUSH

    def _tally_dist(self, dist, ln):
        self.syms.append((dist, ln))
        self.l.fc[LENGTH_CODE[ln] + 257] += 1
        self.d.fc[_d_code(dist - 1)] += 1
        return len(self.syms) == SYM_FLUSH

    # ----------------------------------------------------------- deflate_medium

    def _insert_match(self, ms, ml, ss, os_):
        if self.lookahead <= ml + WANT_MIN_MATCH:
            return
        if ml < WANT_MIN_MATCH:
            ss = (ss + 1) & 0xFFFF
            ml = (ml - 1) & 0xFFFF
            if ml > 0 and ss >= os_:
                if ((ss + ml) & 0xFFFF) > os_:
                    self._insert_string(ss, ml)
                else:
                    self._insert_string(ss, (os_ - ss + 1) & 0xFFFF)
            return
        if ml <= 16 * MAX_LAZY and self.lookahead >= WANT_MIN_MATCH:
            ml -= 1
            ss += 1
            if ss >= os_:
                if ss + ml > os_:
                    self._insert_string(ss, ml)
                else:
                    self._insert_string(ss, os_ - ss + 1)
            elif os_ < ss + ml:
                self._insert_string(os_, ss + ml - os_)
        else:
            ss += ml
            if ss >= STD_MIN_MATCH - 2:
                self._quick_insert(ss + 2 - STD_MIN_MATCH)

    def _medium(self, flush):
        cms = cml = css = cos_ = 0
        nms = nml = nss = nos = 0
        w = self.window
        while True:
            if self.lookahead < MIN_LOOKAHEAD:
                self._fill_window()
                if self.lookahead < MIN_LOOKAHEAD and flush == NO_FLUSH:
                    return NEED_MORE
                if self.lookahead == 0:
                    break
                nml = 0
            if nml > 0:
                cms, cml, css, cos_ = nms, nml, nss, nos
                nml = 0
            else:
                hash_head = 0
                if self.lookahead >= WANT_MIN_MATCH:
                    hash_head = self._quick_insert(self.strstart)
                css = self.strstart
                cos_ = css
                dist = self.strstart - hash_head
                if dist <= MAX_DIST and dist > 0 and hash_head != 0:
                    ml, ms = self._longest_match(hash_head)
                    self.match_start = ms
                    cml, cms = ml, ms
                    if cml < WANT_MIN_MATCH:
                        cml = 1
                    if cms >= css:
                        cml = 1
                else:
                    cms = 0
                    cml = 1
            self._insert_match(cms, cml, css, cos_)
            if self.lookahead > MIN_LOOKAHEAD and css + cml < WINDOW_SIZE - MIN_LOOKAHEAD:
                self.strstart = css + cml
                hash_head = self._quick_insert(self.strstart)
                nss = self.strstart
                nos = nss
                dist = self.strstart - hash_head
                if dist <= MAX_DIST and dist > 0 and hash_head != 0:
                    ml, ms = self._longest_match(hash_head)
                    self.match_start = ms
                    nml, nms = ml, ms
                    if nms >= nss:
                        nml = 1
                    if nml < WANT_MIN_MATCH:
                        nml = 1
                    elif cml > 1 and cml <= 1 + nms and cml <= 1 + nss \
                            and w[nms - cml + 1] == w[nss - cml + 1]:
                        # fizzle_matches
                        lim = nss - MAX_DIST if nss > MAX_DIST else 0
                        c_ml = cml
                        n_ss, n_ms, n_ml = nss, nms, nml
                        im = nms - 1
                        io = nss - 1
                        changed = 0
                        while True:
                            a = w[im] if im >= 0 else None
                            b = w[io] if io >= 0 else None
                            im -= 1
                            io -= 1
                            if a != b:
                                break
                            if c_ml < 1 or n_ss <= lim or n_ml >= 256 or n_ms <= 1:
                                break
                            n_ss -= 1
                            n_ms -= 1
                            n_ml += 1
                            c_ml -= 1
                            changed += 1
                        if changed and c_ml <= 1 and n_ml != 2:
                            cml = c_ml
                            nss, nms, nml, nos = n_ss, n_ms, n_ml, nos + 1
                else:
                    nms = 0
                    nml = 1
                self.strstart = css
            else:
                nml = 0
            # emit_match
            bflush = False
            if cml < WANT_MIN_MATCH:
                s0 = self.strstart
                for k in range(cml):
                    bflush |= self._tally_lit(w[s0 + k])
            else:
                bflush = self._tally_dist(css - cms, cml - STD_MIN_MATCH)
            self.lookahead -= cml
            self.strstart += cml
            if bflush:
                self._flush_block_only(False)
                if self.avail_out == 0:
                    return NEED_MORE
        self.insert = min(self.strstart, STD_MIN_MATCH - 1)
        if flush == FINISH:
            self._flush_block_only(True)
            if self.avail_out == 0:
                return FINISH_STARTED
            return FINISH_DONE
        if self.syms:
            self._flush_block_only(False)
            if self.avail_out == 0:
                return NEED_MORE
        return BLOCK_DONE

    # ----------------------------------------------------------- trees

    def _init_block(self):
        lf = self.l.fc
        for i in range(L_CODES):
            lf[i] = 0
        df = self.d.fc
        for i in range(D_CODES):
            df[i] = 0
        bf = self.bl.fc
        for i in range(BL_CODES):
            bf[i] = 0
        lf[END_BLOCK] = 1
        self.opt_len = 0
        self.static_len = 0
        self.syms = []

    def _build_tree(self, t):
        fc, dl = t.fc, t.dl
        stree = t.stree_len
        elems = t.elems
        heap = [0] * HEAP_SIZE
        depth = [0] * HEAP_SIZE
        heap_len = 0
        heap_max = HEAP_SIZE
        max_code = -1
        for n in range(elems):
            if fc[n] > 0:
                heap_len += 1
                heap[heap_len] = n
                max_code = n
                depth[n] = 0
            else:
                dl[n] = 0
        while heap_len < 2:
            heap_len += 1
            if max_code < 2:
                max_code += 1
                node = max_code
            else:
                node = 0
            heap[heap_len] = node
            fc[node] = 1
            depth[node] = 0
            self.opt_len -= 1
            if stree is not None:
                self.static_len -= stree[node]
        t.max_code = max_code

        def down(k):
            v = heap[k]
            vv = (fc[v] << 8) | depth[v]
            j = k << 1
            while j <= heap_len:
                jv = (fc[heap[j]] << 8) | depth[heap[j]]
                if j < heap_len:
                    j1 = (fc[heap[j + 1]] << 8) | depth[heap[j + 1]]
                    if j1 <= jv:
                        j += 1
                        jv = j1
                if vv <= jv:
                    break
                heap[k] = heap[j]
                k = j
                j <<= 1
            heap[k] = v

        for n in range(heap_len // 2, 0, -1):
            down(n)
        node = elems
        while True:
            n = heap[1]
            heap[1] = heap[heap_len]
            heap_len -= 1
            down(1)
            m = heap[1]
            heap_max -= 1
            heap[heap_max] = n
            heap_max -= 1
            heap[heap_max] = m
            fc[node] = (fc[n] + fc[m]) & 0xFFFF
            depth[node] = (max(depth[n], depth[m]) + 1) & 0xFF
            dl[n] = node
            dl[m] = node
            heap[1] = node
            node += 1
            down(1)
            if heap_len < 2:
                break
        heap_max -= 1
        heap[heap_max] = heap[1]
        # gen_bitlen
        extra, base, max_length = t.extra, t.base, t.max_length
        bl_count = [0] * (MAX_BITS + 1)
        dl[heap[heap_max]] = 0
        overflow = 0
        for h in range(heap_max + 1, HEAP_SIZE):
            n = heap[h]
            bits = dl[dl[n]] + 1
            if bits > max_length:
                bits = max_length
                overflow += 1
            dl[n] = bits
            if n > max_code:
                continue
            bl_count[bits] += 1
            xbits = extra[n - base] if n >= base else 0
            f = fc[n]
            self.opt_len += f * (bits + xbits)
            if stree is not None:
                self.static_len += f * (stree[n] + xbits)
        if overflow:
            while True:
                bits = max_length - 1
                while bl_count[bits] == 0:
                    bits -= 1
                bl_count[bits] -= 1
                bl_count[bits + 1] += 2
                bl_count[max_length] -= 1
                overflow -= 2
                if overflow <= 0:
                    break
            h = HEAP_SIZE
            for bits in range(max_length, 0, -1):
                n = bl_count[bits]
                while n != 0:
                    h -= 1
                    m = heap[h]
                    if m > max_code:
                        continue
                    if dl[m] != bits:
                        self.opt_len += (bits - dl[m]) * fc[m]
                        dl[m] = bits
                    n -= 1
        # gen_codes
        next_code = [0] * (MAX_BITS + 1)
        code = 0
        for bits in range(1, MAX_BITS + 1):
            code = ((code + bl_count[bits - 1]) << 1) & 0xFFFF
            next_code[bits] = code
        for n in range(max_code + 1):
            ln = dl[n]
            if ln == 0:
                continue
            fc[n] = _rev(next_code[ln], 16) >> (16 - ln)
            next_code[ln] = (next_code[ln] + 1) & 0xFFFF

    def _scan_tree(self, t, max_code):
        dl = t.dl
        bf = self.bl.fc
        prevlen = -1
        nextlen = dl[0]
        count = 0
        max_count, min_count = 7, 4
        if nextlen == 0:
            max_count, min_count = 138, 3
        dl[max_code + 1] = 0xFFFF
        for n in range(max_code + 1):
            curlen = nextlen
            nextlen = dl[n + 1]
            count += 1
            if count < max_count and curlen == nextlen:
                continue
            elif count < min_count:
                bf[curlen] += count
            elif curlen != 0:
                if curlen != prevlen:
                    bf[curlen] += 1
                bf[REP_3_6] += 1
            elif count <= 10:
                bf[REPZ_3_10] += 1
            else:
                bf[REPZ_11_138] += 1
            count = 0
            prevlen = curlen
            if nextlen == 0:
                max_count, min_count = 138, 3
            elif curlen == nextlen:
                max_count, min_count = 6, 3
            else:
                max_count, min_count = 7, 4

    def _send_tree(self, t, max_code):
        dl = t.dl
        bfc, bdl = self.bl.fc, self.bl.dl
        send = self._send
        prevlen = -1
        nextlen = dl[0]
        count = 0
        max_count, min_count = 7, 4
        if nextlen == 0:
            max_count, min_count = 138, 3
        for n in range(max_code + 1):
            curlen = nextlen
            nextlen = dl[n + 1]
            count += 1
            if count < max_count and curlen == nextlen:
                continue
            elif count < min_count:
                while True:
                    send(bfc[curlen], bdl[curlen])
                    count -= 1
                    if count == 0:
                        break
            elif curlen != 0:
                if curlen != prevlen:
                    send(bfc[curlen], bdl[curlen])
                    count -= 1
                send(bfc[REP_3_6], bdl[REP_3_6])
                send(count - 3, 2)
            elif count <= 10:
                send(bfc[REPZ_3_10], bdl[REPZ_3_10])
                send(count - 3, 3)
            else:
                send(bfc[REPZ_11_138], bdl[REPZ_11_138])
                send(count - 11, 7)
            count = 0
            prevlen = curlen
            if nextlen == 0:
                max_count, min_count = 138, 3
            elif curlen == nextlen:
                max_count, min_count = 6, 3
            else:
                max_count, min_count = 7, 4

    def _build_bl_tree(self):
        self._scan_tree(self.l, self.l.max_code)
        self._scan_tree(self.d, self.d.max_code)
        self._build_tree(self.bl)
        bdl = self.bl.dl
        max_blindex = BL_CODES - 1
        while max_blindex >= 3:
            if bdl[BL_ORDER[max_blindex]] != 0:
                break
            max_blindex -= 1
        self.opt_len += 3 * (max_blindex + 1) + 5 + 5 + 4
        return max_blindex

    def _compress_block(self, lcode, llen, dcode, dlen):
        buf = self.bi_buf
        valid = self.bi_valid
        out = self.pending
        for dist, lc in self.syms:
            if dist == 0:
                buf |= lcode[lc] << valid
                valid += llen[lc]
            else:
                code = LENGTH_CODE[lc]
                c = code + 257
                buf |= lcode[c] << valid
                valid += llen[c]
                e = EXTRA_LBITS[code]
                if e:
                    buf |= (lc - BASE_LENGTH[code]) << valid
                    valid += e
                dist -= 1
                dc = DIST_CODE[dist] if dist < 256 else DIST_CODE[256 + (dist >> 7)]
                buf |= dcode[dc] << valid
                valid += dlen[dc]
                e = EXTRA_DBITS[dc]
                if e:
                    buf |= (dist - BASE_DIST[dc]) << valid
                    valid += e
            if valid > 4096:
                n = valid >> 3
                out += (buf & ((1 << (8 * n)) - 1)).to_bytes(n, "little")
                buf >>= 8 * n
                valid -= 8 * n
        buf |= lcode[END_BLOCK] << valid
        valid += llen[END_BLOCK]
        self.bi_buf = buf
        self.bi_valid = valid

    def _stored_block(self, start, ln, last):
        self._send((0 << 1) | int(last), 3)
        self._align()
        self.pending += struct.pack("<HH", ln & 0xFFFF, (~ln) & 0xFFFF)
        if ln:
            self.pending += self.window[start:start + ln]

    def _flush_block(self, window_offset, stored_len, last):
        if not self.syms:
            opt_lenb = static_lenb = 0
            self.static_len = 7
            max_blindex = 0
        else:
            self._build_tree(self.l)
            self._build_tree(self.d)
            max_blindex = self._build_bl_tree()
            opt_lenb = (self.opt_len + 3 + 7) >> 3
            static_lenb = (self.static_len + 3 + 7) >> 3
            if static_lenb <= opt_lenb:
                opt_lenb = static_lenb
        if stored_len + 4 <= opt_lenb and window_offset is not None:
            self._stored_block(window_offset, stored_len, last)
        elif static_lenb == opt_lenb:
            self._send((1 << 1) | int(last), 3)
            self._compress_block(SLTREE_CODE, SLTREE_LEN, SDTREE_CODE, SDTREE_LEN)
        else:
            self._send((2 << 1) | int(last), 3)
            lcodes, dcodes, blcodes = self.l.max_code + 1, self.d.max_code + 1, max_blindex + 1
            self._send(lcodes - 257, 5)
            self._send(dcodes - 1, 5)
            self._send(blcodes - 4, 4)
            bdl = self.bl.dl
            for rank in range(blcodes):
                self._send(bdl[BL_ORDER[rank]], 3)
            self._send_tree(self.l, lcodes - 1)
            self._send_tree(self.d, dcodes - 1)
            self._compress_block(self.l.fc, self.l.dl, self.d.fc, self.d.dl)
        self._init_block()
        if last:
            self._align()

    def _flush_block_only(self, last):
        ws = self.block_start if self.block_start >= 0 else None
        self._flush_block(ws, (self.strstart - self.block_start) & 0xFFFFFFFF, last)
        self.block_start = self.strstart
        self._flush_pending()

    # ----------------------------------------------------------- deflate()

    def deflate(self, flush, avail_out):
        """One deflate() call: input is whatever is left; writes at most avail_out bytes."""
        self.avail_in = len(self.inp) - self.next_in
        self.avail_out = avail_out
        if self.status == ST_FINISH and flush != FINISH:
            raise ValueError("stream error")
        if self.avail_out == 0:
            return BUF_ERROR
        old_flush = self.last_flush
        self.last_flush = flush

        def rank(f):
            return f * 2 - (9 if f > 4 else 0)
        if self._has_pending():
            self._flush_pending()
            if self.avail_out == 0:
                self.last_flush = -1
                return OK
        elif self.avail_in == 0 and rank(flush) <= rank(old_flush) and flush != FINISH:
            return BUF_ERROR
        if self.status == ST_FINISH and self.avail_in != 0:
            return BUF_ERROR
        if self.status == ST_INIT:
            self.pending += b"\x78\x9c"  # zlib header, 32K window, level 6
            self.adler = 1
            self.status = ST_BUSY
            self._flush_pending()
            if self._has_pending():
                self.last_flush = -1
                return OK
        if self.avail_in != 0 or self.lookahead != 0 or (flush != NO_FLUSH and self.status != ST_FINISH):
            bstate = self._medium(flush)
            if bstate in (FINISH_STARTED, FINISH_DONE):
                self.status = ST_FINISH
            if bstate in (NEED_MORE, FINISH_STARTED):
                if self.avail_out == 0:
                    self.last_flush = -1
                return OK
        if flush != FINISH:
            return OK
        if self.wrap == 1:
            self.pending += struct.pack(">I", self.adler & 0xFFFFFFFF)
        self._flush_pending()
        if self.wrap > 0:
            self.wrap = -self.wrap
        return OK if self._has_pending() else STREAM_END


def compress(data, out_buf=32 * 1024):
    """flate2::write::ZlibEncoder::new(Vec, Compression::new(6)); write_all(data); finish()."""
    d = Deflater(bytes(data))
    while d.next_in < len(d.inp):
        d.deflate(NO_FLUSH, out_buf)
    while True:
        before = d.total_out
        st = d.deflate(FINISH, out_buf)
        if st == STREAM_END or d.total_out == before:
            break
    return bytes(d.out)
