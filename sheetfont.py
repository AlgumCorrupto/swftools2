"""Glyphs from the bitmap SHEETs embedded in swfFONT objects (what the engine itself draws via swfFONT::SHEET::DrawGlyph).

swfFONT: +0x98 i16 sheet count, +0xA0 sheet pointers.  SHEET: +0 atlas*, +4 records*, +8 i16 text size (twips), +0xA u16 n.
atlas*: +0 bmpInfo* (info+0x90 palette*, info+0x94 ps2Texture*), ps2Texture +8 pixels*, +0x2C w,h. 4bpp PSMT4 swizzled 128x128.
record (12 bytes): u8 x,y,w,h (rect in atlas)  i16 left, top, advance, bottom  (1/1024 em; y up is negative)."""
import struct
from rpck import unswizzle4, unswizzle8, unswizzle_palette
import tess


def sheets_of(p, a):
    cnt = struct.unpack_from('<h', p.d, a + 0x98 - p.base)[0]
    out = []
    for k in range(max(0, min(cnt, 12))):
        sp = p.u32(a + 0xA0 + 4 * k)
        if not p.inside(sp): continue
        size, n = struct.unpack_from('<hH', p.d, sp + 8 - p.base)
        out.append(dict(addr=sp, size=size, n=n))
    return out


class SheetGlyphs:
    EM = 1024

    def __init__(s, p, a, sheet=None):
        s.p = p; s.ok = False
        chars = p.read_font(a)['chars']; sh = sheets_of(p, a)
        if not sh: return
        sh = sheet if sheet is not None else max(sh, key=lambda x: x['size'])
        sp = sh['addr']; s.size = sh['size']; A = p.u32(sp); B = p.u32(sp + 4); k = max(1, p.u32(sp + 0x10))
        s.pages = []
        for pg in range(k):
            info = p.u32(A + 4 * pg); pal = p.u32(info + 0x90); tex = p.u32(info + 0x94); pix = p.u32(tex + 8)
            w, h = p.u16(tex + 0x2C), p.u16(tex + 0x2E); ncol = p.u16(pal + 2)
            cols = [tuple(p.raw(pal + 0x10 + 4 * q, 4)) for q in range(ncol)]
            if ncol == 16: idx = unswizzle4(p.raw(pix, w * h // 2), w, h)
            else:
                idx = unswizzle8(p.raw(pix, w * h), w, h); cols = unswizzle_palette(cols)
            s.pages.append((w, h, bytes(min(255, cols[v][3] * 2) for v in idx)))
        C = p.u32(sp + 0xC); explicit = list(p.raw(C, sh['n'])) if (k > 1 and p.inside(C)) else None
        s.recs = []; s.page_of = []; page = 0; prev = None
        for i in range(sh['n']):
            x, y, w, h, left, top, adv, bot = struct.unpack('<BBBBhhhh', p.raw(B + 12 * i, 12)); s.recs.append((x, y, w, h, left, top, adv, bot))
            if w and h:       # records are stored page after page; a page change shows as the rect row jumping back up
                if prev is not None and y < prev and page < k - 1: page += 1
                prev = y
            s.page_of.append(explicit[i] if explicit else page)
        s.map = {c: i for i, c in enumerate(chars) if i < len(s.recs)}
        s.U = s.EM * 20.0 / s.size      # em units per atlas pixel
        s.cache = {}; s.ok = True
        tops = [-r[5] for r in s.recs if r[2] and r[3]]; bots = [r[7] for r in s.recs if r[2] and r[3]]
        s.asc = max(tops) if tops else 900; s.desc = max(bots) if bots else 220

    def has(s, code): return code in s.map

    def glyph(s, code):
        if code in s.cache: return s.cache[code]
        i = s.map[code]; x0, y0, w, h, left, top, adv, bot = s.recs[i]
        pw, ph, alpha = s.pages[s.page_of[i]]
        def on(x, y): return 0 <= x < w and 0 <= y < h and alpha[(y0 + y) * pw + x0 + x] >= 96
        edges = []
        for y in range(h):
            for x in range(w):
                if on(x, y): edges += [((x, y), (x + 1, y)), ((x + 1, y), (x + 1, y + 1)), ((x + 1, y + 1), (x, y + 1)), ((x, y + 1), (x, y))]
        out = []
        for loop in tess.stitch(edges):
            m = len(loop)
            keep = [loop[k] for k in range(m) if (loop[k][0] - loop[k - 1][0]) * (loop[(k + 1) % m][1] - loop[k][1]) != (loop[k][1] - loop[k - 1][1]) * (loop[(k + 1) % m][0] - loop[k][0])]
            if len(keep) < 3: continue
            P = [(round(left + px * s.U), round(top + py * s.U)) for px, py in keep]
            out.append(('M',) + P[0]); out += [('L', qx, qy) for qx, qy in P[1:]] + [('L',) + P[0]]
        r = (out, adv); s.cache[code] = r; return r
