"""rpck - reader/patcher for PS2 Midnight Club 3 / RAGE "swf" .pck resources.

A .pck is a 0x80-byte header followed by a load-in-place dump of C++ objects
(see NOTES.md).  Pointers are absolute: base_address + offset-in-data.
"""
import struct, os, contextlib

HDR = 0x80
OBJ_NAMES = {1: 'SHAPE', 2: 'SPRITE', 3: 'BUTTON', 4: 'BITMAP', 5: 'FONT', 6: 'TEXT', 7: 'EDITTEXT'}


def backup_name(path):
    """first free name of path + '.old', '.old2', '.old3' ..."""
    n, k = path + '.old', 1
    while os.path.lexists(n):
        k += 1; n = '%s.old%d' % (path, k)
    return n


@contextlib.contextmanager
def replacing(src, out):
    """Context for writing `out` from `src`.  Yields (path to read the source from, backup path or None).
    When `out` is the same file as `src` the original is first renamed to <out>.old (.old2 ... when that exists, so an earlier
    backup is never overwritten) and the yielded path points at that backup; the caller then writes `out` as usual.  If the body
    fails, the partial output is removed and the original is put back under its own name."""
    if not (src and os.path.exists(src) and os.path.exists(out) and os.path.samefile(src, out)):
        yield src, None
        return
    bak = backup_name(out)
    os.rename(out, bak)
    try:
        yield bak, bak
    except BaseException:
        if os.path.exists(out): os.remove(out)
        os.rename(bak, out)
        raise


# class vtable constants as stored in the (PS2, MC3) files; the loader overwrites them
VT = {0: 0x211a50, 1: 0x211c00, 2: 0x211c60, 3: 0x211cc0, 4: 0x211c90, 5: 0x211ae0, 6: 0x211ab0, 7: 0x211a80}
CMD_VT = {0: 0x211d30, 1: 0x211d10, 2: 0x211cf0, 3: 0x211d70}


class Pck:
    def __init__(s, src):
        b = open(src, 'rb').read() if isinstance(src, str) else bytes(src)
        s.hdr = bytearray(b[:HDR])
        s.base, s.u1, s.u2, size = struct.unpack_from('<IIII', b, 0)
        if size != len(b) - HDR:
            raise ValueError('header dataSize %#x != file payload %#x' % (size, len(b) - HDR))
        s.d = bytearray(b[HDR:])
        s.reload_root()

    # ---- root ----
    def reload_root(s):
        d = s.d
        s.fc, = struct.unpack_from('<H', d, 8)
        s.frames, s.lst = struct.unpack_from('<II', d, 0xc)
        s.fps_raw = d[0x14]
        s.xmin, s.xmax, s.ymin, s.ymax = struct.unpack_from('<4f', d, 0x18)  # 0x18,0x1c,0x20,0x24
        s.rate88, s.count = struct.unpack_from('<HH', d, 0x28)
        s.ptrs = list(struct.unpack_from('<%dI' % s.count, d, s.lst - s.base))

    def tobytes(s):
        hdr = bytearray(s.hdr)
        struct.pack_into('<IIII', hdr, 0, s.base, s.u1, s.u2, len(s.d))
        return bytes(hdr) + bytes(s.d)

    # ---- raw access (absolute addresses) ----
    def u8(s, a): return s.d[a - s.base]
    def u16(s, a): return struct.unpack_from('<H', s.d, a - s.base)[0]
    def i16(s, a): return struct.unpack_from('<h', s.d, a - s.base)[0]
    def u32(s, a): return struct.unpack_from('<I', s.d, a - s.base)[0]
    def f32(s, a): return struct.unpack_from('<f', s.d, a - s.base)[0]
    def raw(s, a, n): return bytes(s.d[a - s.base:a - s.base + n])
    def inside(s, a): return s.base <= a < s.base + len(s.d)
    def otype(s, i): return s.u8(s.ptrs[i]) if s.ptrs[i] else None

    # ---- allocation (append-only arena; new data goes after the old list) ----
    def alloc(s, data, align=4):
        while len(s.d) % align: s.d.append(0xCD)
        a = s.base + len(s.d)
        s.d += data
        return a

    def put(s, a, fmt, *v): struct.pack_into(fmt, s.d, a - s.base, *v)

    # ---- structures ----
    def frames_of(s, fa, n):
        return [s.u32(fa + 8 * k + 4) for k in range(n)]  # command-list heads (frame.null0 ignored)

    def cmds(s, head):
        out = []
        while head:
            out.append(head)
            head = s.u32(head + 4)
        return out

    def read_cmd(s, c):
        t, f1, us, f2 = s.raw(c, 4)
        o = {'addr': c, 't': t}
        if t in (0, 1):
            o.update(flags=f1, depth=s.u16(c + 0xc), char=s.u16(c + 0xe))
            mp, xp = s.u32(c + 0x10), s.u32(c + 0x14)
            o['matrix'] = s.read_pmatrix(mp, f1) if mp else None
            o['cxform'] = tuple(s.raw(xp, 8)) if xp else None
            if t == 1:
                o['unk18'] = s.u32(c + 0x18)
                n, arr, nm = s.u32(c + 0x1c), s.u32(c + 0x20), s.u32(c + 0x24)
                o['name'] = s.cstr(nm) if nm else None
                o['events'] = [(s.u32(arr + 12 * k), s.u32(arr + 12 * k + 4), s.u32(arr + 12 * k + 8)) for k in range(n)]
                o['events'] = [(fl, s.read_code(cp), x) for fl, cp, x in o['events']]
        elif t == 2:
            o['depth'] = s.u16(c + 2)
        elif t == 3:
            o['code'] = s.read_code(s.u32(c + 0xc))
        else:
            raise ValueError('unknown cmd type %d at %#x' % (t, c))
        return o

    def cstr(s, a):
        e = s.d.index(0, a - s.base)
        return bytes(s.d[a - s.base:e]).decode('latin-1')

    def read_pmatrix(s, a, f1):
        # packed: [sx sy (8.8)] [rs0 rs1 (8.8)] tx ty (twips, int16); flag bit0 = scale present, bit1 = rotation present
        # (flag1 == 2 = rotation without scale occurs in the ms_vp_* files; 0xCD = uninitialised, translation only)
        if f1 == 0xCD: hs = hr = 0
        elif f1 < 4: hs, hr = f1 & 1, f1 & 2
        else: raise ValueError('flag1 %#x' % f1)
        o = 0; sx = sy = 256; r0 = r1 = 0
        if hs: sx, sy = struct.unpack_from('<hh', s.d, a - s.base); o = 4
        if hr: r0, r1 = struct.unpack_from('<hh', s.d, a - s.base + o); o += 4
        tx, ty = struct.unpack_from('<hh', s.d, a - s.base + o)
        return (sx, sy, r0, r1, tx, ty)

    def code_len(s, a):
        """length of AVM1 block incl. the 2-byte prefix and the terminating 0"""
        i = a - s.base + 2; d = s.d
        while True:
            op = d[i]
            if op == 0: return i + 1 - (a - s.base)
            i += 1 if op < 0x80 else 3 + struct.unpack_from('<H', d, i + 1)[0]

    def read_code(s, a):
        n = s.code_len(a)
        return bytes(s.d[a - s.base:a - s.base + n])

    # shapes
    def read_shape(s, a):
        nf, ns = s.u16(a + 0x14), s.u16(a + 0x16)
        ft, st, dl = s.u32(a + 8), s.u32(a + 0xc), s.u32(a + 0x10)
        fills = []
        for k in range(nf):
            f = ft + 0x28 * k
            flag = s.u8(f)
            color = tuple(s.raw(f + 4, 4))
            m = struct.unpack_from('<6f', s.d, f + 8 - s.base)
            bm, g = s.u32(f + 0x20), s.u32(f + 0x24)
            gr = None
            if g:
                gm = struct.unpack_from('<6f', s.d, g - s.base)
                n = s.u16(g + 0x18); rp, cp = s.u32(g + 0x1c), s.u32(g + 0x20)
                gr = dict(matrix=gm, ratios=list(s.raw(rp, n)), colors=[tuple(s.raw(cp + 4 * j, 4)) for j in range(n)])
            fills.append(dict(flag=flag, color=color, matrix=m, bitmap=bm, grad=gr))
        strokes = [dict(a=s.u16(st + 8 * k), b=s.u16(st + 8 * k + 2), color=tuple(s.raw(st + 8 * k + 4, 4))) for k in range(ns)]
        ops = []; q = dl
        while True:
            op = s.u16(q)
            if op == 0: break
            if op in (4, 5):
                ops.append((op, s.u16(q + 2))); q += 4
            elif op in (1, 2):
                c = s.u16(q + 2)
                ops.append((op, [struct.unpack_from('<hh', s.d, q + 4 + 4 * k - s.base) for k in range(c)])); q += 4 + 4 * c
            else:
                raise ValueError('shape op %d at %#x' % (op, q))
        return dict(fills=fills, strokes=strokes, ops=ops)

    def read_sprite(s, a):
        n = s.u32(a + 0x10)
        assert s.u16(a + 8) == (n & 0xffff) or True
        return dict(nframes=n, frames=[[s.read_cmd(c) for c in s.cmds(h)] for h in s.frames_of(s.u32(a + 0xc), n)])

    def read_text(s, a):
        m = struct.unpack_from('<6f', s.d, a + 8 - s.base)
        q = s.u32(a + 0x20); recs = []
        while True:
            t = s.u16(q)
            if t == 0: break
            if t == 1: recs.append(('font', s.u16(q + 2), s.u16(q + 4))); q += 6
            elif t == 2: recs.append(('color', tuple(s.raw(q + 2, 4)))); q += 6
            elif t in (3, 4): recs.append(('y' if t == 3 else 'x', s.i16(q + 2))); q += 4
            elif t == 5:
                n = s.u16(q + 2)
                recs.append(('glyphs', [struct.unpack_from('<HH', s.d, q + 4 + 4 * k - s.base) for k in range(n)])); q += 4 + 4 * n
            else: raise ValueError('text rec %d' % t)
        return dict(matrix=m, recs=recs)

    def read_font(s, a):
        n = s.u16(a + 8)
        return dict(n=n, chars=list(struct.unpack_from('<%dH' % n, s.d, s.u32(a + 0x10) - s.base)))

    def read_edittext(s, a):
        return dict(color=tuple(s.raw(a + 0x14, 4)), font=s.u16(a + 0x18), size=s.u16(a + 0x1a),
                    w=struct.unpack_from('<i', s.d, a + 0x1c - s.base)[0], h=struct.unpack_from('<i', s.d, a + 0x20 - s.base)[0],
                    xo=struct.unpack_from('<i', s.d, a + 0x24 - s.base)[0], yo=struct.unpack_from('<i', s.d, a + 0x28 - s.base)[0],
                    align=s.u8(a + 0x2c), key=(s.cstr(s.u32(a + 0xc)) if s.inside(s.u32(a + 0xc)) else None),
                    text=(s.cstr(s.u32(a + 8)) if s.inside(s.u32(a + 8)) else ''), cap=s.u16(a + 0x10), eflags=s.u16(a + 0x12))

    def read_button(s, a):
        nrec, ncond = s.u8(a + 9), s.u8(a + 10)
        rp, cp = s.u32(a + 0xc), s.u32(a + 0x10)
        recs = []
        for k in range(nrec):
            r = rp + 0x28 * k
            recs.append(dict(state=s.u8(r), depth=s.u16(r + 2), matrix=struct.unpack_from('<6f', s.d, r + 4 - s.base),
                             cxform=tuple(s.raw(r + 0x1c, 8)), target=s.u32(r + 0x24), addr=r))
        conds = []
        for k in range(ncond):
            c = cp + 8 * k   # {u16 cond (SWF ButtonCondAction flags), u16 extra, u32 code ptr}
            conds.append(dict(addr=c, raw=s.raw(c, 8)))
        return dict(recs=recs, conds=conds)

    # bitmaps
    def bitmap_parts(s, a):
        info = s.u32(a + 8)
        tex = s.u32(info + 0x94)
        pal = s.u32(info + 0x90)
        return dict(w=s.u16(a + 0xc), h=s.u16(a + 0xe), info=info, tex=tex, pal=pal,
                    ttype=s.u8(tex + 0x30), tw=s.u16(tex + 0x2c), th=s.u16(tex + 0x2e),
                    pix=s.u32(tex + 8), ncol=s.u16(pal + 2))

# ------------- PS2 GS swizzles (algorithms from the RAGE-SWF-research repo) -------------
def unswizzle8(src, w, h):
    dst = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            bl = (y & ~0xF) * w + (x & ~0xF) * 2
            sw = (((y + 2) >> 2) & 1) * 4
            py = (((y & ~3) >> 1) + (y & 1)) & 7
            cl = py * w * 2 + ((x + sw) & 7) * 4
            bn = ((y >> 1) & 1) + ((x >> 2) & 2)
            dst[y * w + x] = src[bl + cl + bn]
    return bytes(dst)

def swizzle8(src, w, h):
    dst = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            bl = (y & ~0xF) * w + (x & ~0xF) * 2
            sw = (((y + 2) >> 2) & 1) * 4
            py = (((y & ~3) >> 1) + (y & 1)) & 7
            cl = py * w * 2 + ((x + sw) & 7) * 4
            bn = ((y >> 1) & 1) + ((x >> 2) & 2)
            dst[bl + cl + bn] = src[y * w + x]
    return bytes(dst)

def _pal_perm(n):
    out = []
    for part in range(n // 32):
        for block in range(2):
            for stripe in range(2):
                for color in range(8):
                    out.append(part * 32 + block * 8 + stripe * 16 + color)
    return out

def unswizzle_palette(cols):  # list of 256 rgba -> logical order
    return [cols[i] for i in _pal_perm(len(cols))]

def swizzle_palette(cols):
    out = [None] * len(cols)
    for k, i in enumerate(_pal_perm(len(cols))): out[i] = cols[k]
    return out


def bitmap_decode(p, a):
    """-> (w, h, indices(bytes, 1/px), palette[list of (r,g,b,a_ps2)], ttype)"""
    b = p.bitmap_parts(a); w, h = b['tw'], b['th']
    pal = [tuple(p.raw(b['pal'] + 0x10 + 4 * k, 4)) for k in range(b['ncol'])]
    if b['ttype'] == 6:       # 4bpp, linear, low nibble first
        n = w * h; src = p.raw(b['pix'], (n + 1) // 2); idx = bytearray(n)
        for i in range(n): idx[i] = (src[i >> 1] >> (4 * (i & 1))) & 15
        idx = bytes(idx)
    elif b['ttype'] in (1, 5):  # 8bpp; type 1: pixels swizzled, type 5: pixels linear. CLUT swizzled in both
        src = p.raw(b['pix'], w * h)
        idx = unswizzle8(src, w, h) if b['ttype'] == 1 else src
        pal = unswizzle_palette(pal)
    else:
        raise ValueError('texture type %d' % b['ttype'])
    return w, h, idx, pal, b['ttype']

def bitmap_encode_into(p, a, idx, pal):
    """patch indices + palette of an existing bitmap in place (same size/type)"""
    b = p.bitmap_parts(a); w, h = b['tw'], b['th']
    pal = list(pal) + [(0, 0, 0, 0)] * (b['ncol'] - len(pal))
    if len(pal) != b['ncol'] or len(idx) != w * h: raise ValueError('size mismatch')
    if b['ttype'] == 6:
        out = bytearray((w * h + 1) // 2)
        for i, v in enumerate(idx): out[i >> 1] |= (v & 15) << (4 * (i & 1))
        data = bytes(out)
    else:
        data = swizzle8(idx, w, h) if b['ttype'] == 1 else bytes(idx)
        pal = swizzle_palette(pal)
    p.d[b['pix'] - p.base:b['pix'] - p.base + len(data)] = data
    for k, c in enumerate(pal): p.d[b['pal'] + 0x10 + 4 * k - p.base:b['pal'] + 0x14 + 4 * k - p.base] = bytes(c)


def unswizzle4(src, w, h):
    """PS2 PSMT4 swizzled 4bpp (single 128x128 page, as used by the font sheets) -> one index per byte, row-major"""
    assert w == 128 and h == 128, 'only 128x128 4bpp sheets are supported'
    out = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            bl = ((x & ~0x1F) >> 1) * h + (y & ~0xF) * 2
            sw = (((y + 2) >> 2) & 1) * 4
            py = (((y & ~3) >> 1) + (y & 1)) & 7
            cl = py * h * 2 + ((x + sw) & 7) * 4
            bn = (x >> 3) & 3
            bs = (y >> 1) & 1
            out[y * w + x] = (src[bl + cl + bn] >> (4 * bs)) & 15
    return bytes(out)


def swizzle4(idx, w, h):
    """inverse of unswizzle4: one index per byte (row-major) -> PSMT4 swizzled bytes (128x128 page)"""
    assert w == 128 and h == 128
    out = bytearray(w * h // 2)
    for y in range(h):
        for x in range(w):
            bl = ((x & ~0x1F) >> 1) * h + (y & ~0xF) * 2
            sw = (((y + 2) >> 2) & 1) * 4
            py = (((y & ~3) >> 1) + (y & 1)) & 7
            cl = py * h * 2 + ((x + sw) & 7) * 4
            bn = (x >> 3) & 3
            bs = (y >> 1) & 1
            o = bl + cl + bn
            out[o] = (out[o] & ~(15 << (4 * bs))) | ((idx[y * w + x] & 15) << (4 * bs))
    return bytes(out)
