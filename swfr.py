"""Minimal SWF reader for the subset used by swf2pck."""
import struct, zlib

class BR:
    def __init__(s, b, pos=0): s.b = b; s.p = pos; s.n = 0; s.cur = 0
    def ub(s, nb):
        v = 0
        for _ in range(nb):
            if s.n == 0: s.cur = s.b[s.p]; s.p += 1; s.n = 8
            s.n -= 1; v = (v << 1) | ((s.cur >> s.n) & 1)
        return v
    def sb(s, nb):
        v = s.ub(nb)
        return v - (1 << nb) if nb and v >> (nb - 1) else v
    def align(s): s.n = 0
    def u8(s): s.align(); v = s.b[s.p]; s.p += 1; return v
    def u16(s): s.align(); v = struct.unpack_from('<H', s.b, s.p)[0]; s.p += 2; return v
    def i16(s): s.align(); v = struct.unpack_from('<h', s.b, s.p)[0]; s.p += 2; return v
    def u32(s): s.align(); v = struct.unpack_from('<I', s.b, s.p)[0]; s.p += 4; return v
    def rd(s, n): s.align(); v = s.b[s.p:s.p + n]; s.p += n; return bytes(v)
    def cstr(s): s.align(); e = s.b.index(0, s.p); v = s.b[s.p:e].decode('latin-1'); s.p = e + 1; return v
    def rect(s):
        nb = s.ub(5); r = tuple(s.sb(nb) for _ in range(4)); s.align(); return r
    def matrix(s):
        a = d = 1.0; b = c = 0.0
        if s.ub(1): nb = s.ub(5); a = s.sb(nb) / 65536; d = s.sb(nb) / 65536
        if s.ub(1): nb = s.ub(5); b = s.sb(nb) / 65536; c = s.sb(nb) / 65536
        nb = s.ub(5); tx = s.sb(nb); ty = s.sb(nb); s.align()
        return (a, b, c, d, tx, ty)
    def cxform(s, alpha=True):
        has_add, has_mult = s.ub(1), s.ub(1); nb = s.ub(4)
        mult = [256, 256, 256, 256]; add = [0, 0, 0, 0]
        if has_mult: mult = [s.sb(nb) for _ in range(4 if alpha else 3)] + ([256] if not alpha else [])
        if has_add: add = [s.sb(nb) for _ in range(4 if alpha else 3)] + ([0] if not alpha else [])
        s.align(); return mult, add

def read_swf(data):
    sig = data[:3]; ver = data[3]
    body = data[8:] if sig == b'FWS' else zlib.decompress(data[8:])
    if sig not in (b'FWS', b'CWS'): raise ValueError('not an SWF (ZWS/LZMA not supported)')
    r = BR(body); frame = r.rect(); rate = r.u16(); nfr = r.u16()
    return dict(version=ver, frame=frame, rate=rate, nframes=nfr, tags=read_tags(body, r.p))

def read_tags(b, p, end=None):
    end = len(b) if end is None else end; out = []
    while p < end:
        h = struct.unpack_from('<H', b, p)[0]; p += 2; code, ln = h >> 6, h & 63
        if ln == 63: ln = struct.unpack_from('<I', b, p)[0]; p += 4
        out.append((code, bytes(b[p:p + ln]))); p += ln
        if code == 0: break
    return out

def parse_cxform_swf(r): return r.cxform(True)

def parse_place(pl):
    r = BR(pl); fl = r.u8(); o = dict(flags=fl, depth=r.u16(), char=None, matrix=None, cxform=None, name=None, events=None, move=bool(fl & 1))
    if fl & 2: o['char'] = r.u16()
    if fl & 4: o['matrix'] = r.matrix()
    if fl & 8: o['cxform'] = r.cxform(True)
    if fl & 16: r.u16()
    if fl & 32: o['name'] = r.cstr()
    if fl & 64: r.u16()
    if fl & 128:
        r.u16(); r.u32(); ev = []
        while True:
            f = r.u32()
            if f == 0: break
            ln = r.u32(); body = r.rd(ln); key = 0
            if f & 0x20000: key = body[0]; body = body[1:]
            ev.append((f, body, key))
        o['events'] = ev
    return o

def parse_timeline(tags):
    """-> list of frames, each a list of ('place',d)/('remove',depth)/('action',bytes)"""
    frames = []; cur = []
    for code, pl in tags:
        if code == 26: cur.append(('place', parse_place(pl)))
        elif code == 28: cur.append(('remove', struct.unpack_from('<H', pl)[0]))
        elif code == 12: cur.append(('action', pl))
        elif code == 1: frames.append(cur); cur = []
    if cur: frames.append(cur)
    return frames

def parse_sprite(pl):
    sid, nf = struct.unpack_from('<HH', pl); tags = read_tags(pl, 4)
    return sid, nf, parse_timeline(tags)

def parse_bitmap(pl):
    cid, fmt, w, h = struct.unpack_from('<HBHH', pl)
    if fmt == 3:
        n = pl[7] + 1; raw = zlib.decompress(pl[8:]); ct = raw[:4 * n]; stride = (w + 3) & ~3
        idx = b''.join(raw[4 * n + y * stride:4 * n + y * stride + w] for y in range(h))
        pal = []
        for k in range(n):
            r, g, b, a = ct[4 * k:4 * k + 4]
            if a: r, g, b = min(255, round(r * 255 / a)), min(255, round(g * 255 / a)), min(255, round(b * 255 / a))
            pal.append((r, g, b, a))
        return dict(id=cid, w=w, h=h, idx=idx, pal=pal)
    if fmt == 5:
        raw = zlib.decompress(pl[7:]); px = []
        for k in range(w * h):
            a, r, g, b = raw[4 * k:4 * k + 4]
            if a: r, g, b = min(255, round(r * 255 / a)), min(255, round(g * 255 / a)), min(255, round(b * 255 / a))
            px.append((r, g, b, a))
        return dict(id=cid, w=w, h=h, px=px)
    raise ValueError('unsupported lossless format %d' % fmt)

def _fillstyles(r, shape3=True):
    n = r.u8()
    if n == 255: n = r.u16()
    out = []
    for _ in range(n):
        t = r.u8(); f = dict(type=t)
        if t == 0: f['color'] = tuple(r.rd(4))
        elif t in (0x10, 0x12, 0x13):
            f['matrix'] = r.matrix(); ng = r.u8() & 15
            f['stops'] = [(r.u8(), tuple(r.rd(4))) for _ in range(ng)]
            if t == 0x13: r.i16()
        elif t in (0x40, 0x41, 0x42, 0x43):
            f['bitmap'] = r.u16(); f['matrix'] = r.matrix()
        else: raise ValueError('fill type %#x' % t)
        out.append(f)
    return out

def _linestyles(r):
    n = r.u8()
    if n == 255: n = r.u16()
    return [dict(width=r.u16(), color=tuple(r.rd(4))) for _ in range(n)]

def parse_shape3(pl):
    """-> dict(id, groups=[dict(fills, lines, edges=[(kind, pts, f0, f1, ln)])]) with absolute coords; curves kept"""
    r = BR(pl); sid = r.u16(); bounds = r.rect()
    fills = _fillstyles(r); lines = _linestyles(r)
    nfb = r.ub(4); nlb = r.ub(4)
    groups = []; cur = dict(fills=fills, lines=lines, edges=[]); f0 = f1 = ln = 0; x = y = 0
    while True:
        if r.ub(1) == 0:
            fl = r.ub(5)
            if fl == 0: break
            if fl & 1:
                nb = r.ub(5); x = r.sb(nb); y = r.sb(nb)
            if fl & 2: f0 = r.ub(nfb)
            if fl & 4: f1 = r.ub(nfb)
            if fl & 8: ln = r.ub(nlb)
            if fl & 16:
                r.align(); groups.append(cur)
                fills = _fillstyles(r); lines = _linestyles(r); nfb = r.ub(4); nlb = r.ub(4)
                cur = dict(fills=fills, lines=lines, edges=[]); f0 = f1 = ln = 0
        else:
            if r.ub(1):  # straight
                nb = r.ub(4) + 2
                if r.ub(1): dx = r.sb(nb); dy = r.sb(nb)
                elif r.ub(1): dx = 0; dy = r.sb(nb)
                else: dx = r.sb(nb); dy = 0
                cur['edges'].append(('l', [(x, y), (x + dx, y + dy)], f0, f1, ln)); x += dx; y += dy
            else:
                nb = r.ub(4) + 2; cx = x + r.sb(nb); cy = y + r.sb(nb); ax = cx + r.sb(nb); ay = cy + r.sb(nb)
                cur['edges'].append(('q', [(x, y), (cx, cy), (ax, ay)], f0, f1, ln)); x, y = ax, ay
    groups.append(cur)
    return dict(id=sid, bounds=bounds, groups=groups)


# ---------------- additional parsers used by swfimport ----------------
def _fs(r, code):
    """fill/line style arrays for DefineShape (2: RGB, no 0xFF extension) / Shape2 (22: RGB) / Shape3 (32: RGBA)"""
    alpha = code == 32
    def col(): return tuple(r.rd(4)) if alpha else tuple(r.rd(3)) + (255,)
    n = r.u8()
    if n == 255 and code != 2: n = r.u16()
    fills = []
    for _ in range(n):
        t = r.u8(); f = dict(type=t)
        if t == 0: f['color'] = col()
        elif t in (0x10, 0x12):
            f['matrix'] = r.matrix(); ng = r.u8() & 15; f['stops'] = [(r.u8(), col()) for _ in range(ng)]
        elif t in (0x40, 0x41, 0x42, 0x43): f['bitmap'] = r.u16(); f['matrix'] = r.matrix()
        else: raise ValueError('fill type %#x' % t)
        fills.append(f)
    n = r.u8()
    if n == 255 and code != 2: n = r.u16()
    lines = [dict(width=r.u16(), color=col()) for _ in range(n)]
    return fills, lines

def parse_shape_any(code, pl):
    """DefineShape / 2 / 3 -> same structure as parse_shape3 (RGBA colours)"""
    r = BR(pl); sid = r.u16(); bounds = r.rect()
    fills, lines = _fs(r, code); nfb = r.ub(4); nlb = r.ub(4)
    groups = []; cur = dict(fills=fills, lines=lines, edges=[]); f0 = f1 = ln = 0; x = y = 0
    while True:
        if r.ub(1) == 0:
            fl = r.ub(5)
            if fl == 0: break
            if fl & 1: nb = r.ub(5); x = r.sb(nb); y = r.sb(nb)
            if fl & 2: f0 = r.ub(nfb)
            if fl & 4: f1 = r.ub(nfb)
            if fl & 8: ln = r.ub(nlb)
            if fl & 16:
                r.align(); groups.append(cur); fills, lines = _fs(r, code); nfb = r.ub(4); nlb = r.ub(4)
                cur = dict(fills=fills, lines=lines, edges=[]); f0 = f1 = ln = 0
        elif r.ub(1):
            nb = r.ub(4) + 2
            if r.ub(1): dx = r.sb(nb); dy = r.sb(nb)
            elif r.ub(1): dx = 0; dy = r.sb(nb)
            else: dx = r.sb(nb); dy = 0
            cur['edges'].append(('l', [(x, y), (x + dx, y + dy)], f0, f1, ln)); x += dx; y += dy
        else:
            nb = r.ub(4) + 2; cx = x + r.sb(nb); cy = y + r.sb(nb); ax = cx + r.sb(nb); ay = cy + r.sb(nb)
            cur['edges'].append(('q', [(x, y), (cx, cy), (ax, ay)], f0, f1, ln)); x, y = ax, ay
    groups.append(cur)
    return dict(id=sid, bounds=bounds, groups=groups)

def parse_glyph(pl, off):
    """one font glyph SHAPE at pl[off:] -> list of contours of ('M',x,y)/('L',x,y)/('Q',cx,cy,x,y) in glyph units"""
    r = BR(pl, off); nfb = r.ub(4); nlb = r.ub(4); x = y = 0; contours = []; cur = None
    while True:
        if r.ub(1) == 0:
            fl = r.ub(5)
            if fl == 0: break
            if fl & 1:
                nb = r.ub(5); x = r.sb(nb); y = r.sb(nb)
                if cur: contours.append(cur)
                cur = [('M', x, y)]
            if fl & 2: r.ub(nfb)
            if fl & 4: r.ub(nfb)
            if fl & 8: r.ub(nlb)
            if fl & 16: raise ValueError('new styles in glyph')
        elif r.ub(1):
            nb = r.ub(4) + 2
            if r.ub(1): dx = r.sb(nb); dy = r.sb(nb)
            elif r.ub(1): dx = 0; dy = r.sb(nb)
            else: dx = r.sb(nb); dy = 0
            x += dx; y += dy; cur.append(('L', x, y))
        else:
            nb = r.ub(4) + 2; cx = x + r.sb(nb); cy = y + r.sb(nb); ax = cx + r.sb(nb); ay = cy + r.sb(nb)
            cur.append(('Q', cx, cy, ax, ay)); x, y = ax, ay
    if cur: contours.append(cur)
    return contours

def parse_font2(pl, tag=48):
    r = BR(pl); fid = r.u16(); fl = r.u8(); r.u8(); name = r.rd(r.u8()).decode('latin-1'); n = r.u16()
    wide_off, wide_codes, has_layout = bool(fl & 8), bool(fl & 4), bool(fl & 0x80)
    base = r.p; offs = [(r.u32() if wide_off else r.u16()) for _ in range(n)]; cto = r.u32() if wide_off else r.u16()
    glyphs = [parse_glyph(pl, base + o) for o in offs]
    r.p = base + cto; codes = [(r.u16() if wide_codes else r.u8()) for _ in range(n)]
    out = dict(id=fid, name=name, n=n, glyphs=glyphs, codes=codes, em=20480 if tag == 75 else 1024, layout=None)
    if has_layout:
        asc, desc, lead = r.i16(), r.i16(), r.i16(); adv = [r.i16() for _ in range(n)]
        out['layout'] = dict(ascent=asc, descent=desc, leading=lead, advance=adv)
    return out

def parse_text(pl, code=11):
    r = BR(pl); cid = r.u16(); bounds = r.rect(); m = r.matrix(); gb = r.u8(); ab = r.u8(); recs = []
    while True:
        fl = r.u8()
        if fl == 0: break
        rec = dict(font=None, size=None, color=None, x=None, y=None)
        if fl & 8: rec['font'] = r.u16()
        if fl & 4: rec['color'] = tuple(r.rd(4)) if code == 33 else tuple(r.rd(3)) + (255,)
        if fl & 1: rec['x'] = r.i16()
        if fl & 2: rec['y'] = r.i16()
        if fl & 8: rec['size'] = r.u16()
        n = r.u8(); rec['glyphs'] = [(r.ub(gb), r.sb(ab)) for _ in range(n)]; r.align(); recs.append(rec)
    return dict(id=cid, bounds=bounds, matrix=m, records=recs)

def parse_edittext(pl):
    r = BR(pl); cid = r.u16(); bounds = r.rect(); f1 = r.u8(); f2 = r.u8(); o = dict(id=cid, bounds=bounds, f1=f1, f2=f2, font=None, size=None, color=(0, 0, 0, 255), align=0, text='', var='', maxlen=0)
    if f1 & 1: o['font'] = r.u16(); o['size'] = r.u16()
    if f2 & 0x80: r.cstr()
    if f1 & 4: o['color'] = tuple(r.rd(4))
    if f1 & 2: o['maxlen'] = r.u16()
    if f2 & 0x20: o['align'] = r.u8(); r.u16(); r.u16(); r.u16(); r.i16()
    o['var'] = r.cstr()
    if f1 & 0x80: o['text'] = r.cstr()
    return o

def parse_timeline_ex(tags, version=6):
    """like parse_timeline but also handles legacy PlaceObject/RemoveObject and returns frame labels"""
    frames = []; cur = []; labels = {}
    for code, pl in tags:
        if code == 26: cur.append(('place', parse_place(pl)))
        elif code == 4:
            r = BR(pl); cid = r.u16(); depth = r.u16(); m = r.matrix(); cx = r.cxform(False) if r.p < len(pl) else None
            cur.append(('place', dict(flags=0x06 | (8 if cx else 0), depth=depth, char=cid, matrix=m, cxform=cx, name=None, events=None, move=False)))
        elif code in (28, 5): cur.append(('remove', struct.unpack_from('<H', pl, 0 if code == 28 else 2)[0]))
        elif code == 12: cur.append(('action', pl))
        elif code == 43: labels[pl.split(b'\0')[0].decode('latin-1')] = len(frames)
        elif code == 1: frames.append(cur); cur = []
    if cur: frames.append(cur)
    return frames, labels
