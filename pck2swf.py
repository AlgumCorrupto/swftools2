#!/usr/bin/env python3
"""pck2swf: convert a PS2 MC3/RAGE swf .pck into a standard SWF (v7).
Object index in the pck == CharacterId in the SWF.  The SWF embeds the source pck
(Metadata tag) so swf2pck can round-trip things that have no SWF equivalent."""
import sys, struct, json, zlib, base64, hashlib
from rpck import *
import swfw as W

def rgba(c): return bytes(c)

def payload_of(tg):
    return tg[6:] if (struct.unpack_from("<H", tg)[0] & 63) == 63 else tg[2:]

def aff(m6):  # engine float matrix (a,b,c,d,tx,ty) -> same tuple
    return tuple(m6)

from fonttex import FontTexGlyphs
from sheetfont import SheetGlyphs, sheets_of
# ---------------- glyph outlines (the pck stores none; borrow them from an open TTF, boxes if unavailable) ----------------
FONT_CANDIDATES = ['/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
                   '/usr/share/fonts/TTF/DejaVuSans.ttf', 'C:/Windows/Fonts/arial.ttf', '/Library/Fonts/Arial.ttf', '/System/Library/Fonts/Supplemental/Arial.ttf']

class GlyphSrc:
    EM = 1024
    def __init__(s, path=None):
        s.ok = False; s.cache = {}; s.path = None
        try:
            from fontTools.ttLib import TTFont
            import os
            for cand in ([path] if path else FONT_CANDIDATES):
                if cand and os.path.exists(cand): s.path = cand; break
            if not s.path: return
            s.f = TTFont(s.path, fontNumber=0); s.cmap = s.f.getBestCmap(); s.gs = s.f.getGlyphSet(); s.upem = s.f['head'].unitsPerEm
            s.hmtx = s.f['hmtx']; s.ok = True
            hh = s.f['hhea']; s.asc, s.desc = round(hh.ascent * s.EM / s.upem), round(-hh.descent * s.EM / s.upem)
        except Exception as e:
            s.ok = False
        if not s.ok: s.asc, s.desc = 900, 220

    def box(s):
        e = s.EM
        return [('M', 80, -700), ('L', 80, 0), ('L', 480, 0), ('L', 480, -700), ('L', 80, -700),
                ('M', 140, -640), ('L', 420, -640), ('L', 420, -60), ('L', 140, -60), ('L', 140, -640)], 560

    def glyph(s, code):
        """-> (segs, advance_em) ; segs oriented interior-on-right in y-down em units"""
        if code in s.cache: return s.cache[code]
        if code == 32 or code == 0xA0: r = ([], round(s.EM * 0.32))
        elif s.ok and code in s.cmap:
            from fontTools.pens.basePen import BasePen
            k = s.EM / s.upem; name = s.cmap[code]; contours = []; cur = []
            class P(BasePen):
                def _moveTo(self, p): nonlocal cur; cur = [('M', p[0] * k, -p[1] * k)]
                def _lineTo(self, p): cur.append(('L', p[0] * k, -p[1] * k))
                def _qCurveToOne(self, c, p): cur.append(('Q', c[0] * k, -c[1] * k, p[0] * k, -p[1] * k))
                def _curveToOne(self, c1, c2, p):   # cubic -> two quads (approximation)
                    x0, y0 = cur[-1][-2], cur[-1][-1]
                    mx, my = (c1[0] + c2[0]) / 2 * k, -(c1[1] + c2[1]) / 2 * k
                    cur.append(('Q', (x0 + 3 * c1[0] * k) / 4 if False else c1[0] * k, -c1[1] * k, mx, my)); cur.append(('Q', c2[0] * k, -c2[1] * k, p[0] * k, -p[1] * k))
                def _closePath(self): nonlocal cur; contours.append(cur); cur = []
                def _endPath(self): nonlocal cur; contours.append(cur); cur = []
            s.gs[name].draw(P(s.gs))
            def pts(c): return [(x[-2], x[-1]) for x in c]
            def area(c):
                ps = pts(c); return sum(ps[i][0] * ps[(i + 1) % len(ps)][1] - ps[(i + 1) % len(ps)][0] * ps[i][1] for i in range(len(ps)))
            big = max(contours, key=lambda c: abs(area(c))) if contours else None
            flip = big is not None and area(big) < 0
            segs = []
            for c in contours:
                if flip: c = reverse_contour(c)
                segs += [tuple([x[0]] + [round(v) for v in x[1:]]) for x in c]
                st = c[0]
                if (c[-1][-2], c[-1][-1]) != (st[1], st[2]): segs.append(('L', round(st[1]), round(st[2])))
            r = (segs, round(s.hmtx[name][0] * k))
        else: r = s.box()
        s.cache[code] = r; return r

def reverse_contour(c):
    """reverse a contour made of M/L/Q segments"""
    pts = [(c[0][1], c[0][2])]; segs = []
    for x in c[1:]:
        if x[0] == 'L': segs.append(('L', None)); pts.append((x[1], x[2]))
        else: segs.append(('Q', (x[1], x[2]))); pts.append((x[3], x[4]))
    out = [('M', pts[-1][0], pts[-1][1])]
    for i in range(len(segs) - 1, -1, -1):
        if segs[i][0] == 'L': out.append(('L', pts[i][0], pts[i][1]))
        else: out.append(('Q', segs[i][1][0], segs[i][1][1], pts[i][0], pts[i][1]))
    return out

def merge_polys(polys):
    """Union of non-overlapping polygons that share edges (the engine stores fills as triangle/quad fans): cancel the interior edges and return
    outline loops with the interior on the right (holes oppositely wound). Flash's shape model has one edge per boundary segment with the fill on
    one side only; paths with duplicated interior edges (what a naive polygon-per-path SWF has) export wrongly to FLA in JPEXS.
    Returns None when the result is not trustworthy (caller keeps the original polygons)."""
    from collections import defaultdict
    import tess
    polys = [list(p) for p in polys if len(p) >= 3]
    P = []
    for p in polys:
        a = tess.area2(p)
        if a == 0: continue
        P.append(p if a > 0 else p[::-1])
    if len(P) < 2: return None
    tot = sum(tess.area2(p) for p in P)
    if len(P) <= 600:   # repair T-junctions: split an edge at every vertex that lies exactly on it
        V = set(pt for p in P for pt in p); Vl = list(V)
        def split(a, b):
            xs, ys = sorted((a[0], b[0])), sorted((a[1], b[1])); mids = []
            for v in Vl:
                if v == a or v == b or not (xs[0] <= v[0] <= xs[1] and ys[0] <= v[1] <= ys[1]): continue
                if (b[0] - a[0]) * (v[1] - a[1]) - (b[1] - a[1]) * (v[0] - a[0]) == 0: mids.append(v)
            mids.sort(key=lambda v: (v[0] - a[0]) ** 2 + (v[1] - a[1]) ** 2)
            return [a] + mids + [b]
        Q = []
        for p in P:
            q = []
            for i in range(len(p)):
                seg = split(p[i], p[(i + 1) % len(p)]); q += seg[:-1]
            Q.append(q)
        P = Q
    edges = [(p[i], p[(i + 1) % len(p)]) for p in P for i in range(len(p))]
    loops = tess.stitch(edges)
    out = []
    for lp in loops:
        m = len(lp); lp = [lp[i] for i in range(m) if tess._cross(lp[i - 1], lp[i], lp[(i + 1) % m]) != 0]       # drop collinear vertices
        if len(lp) >= 3: out.append(lp)
    if not out or abs(sum(tess.area2(l) for l in out) - tot) > 0.005 * abs(tot) + 2: return None
    return out


class Conv:
    def __init__(s, p, preview=False, glyphs=None, fonttex_dir=None, fontmap=None):
        s.glyphs = glyphs or GlyphSrc(); s.fonttex_dir = fonttex_dir; s.fontmap = fontmap or {}; s.ftcache = {}; s.fontinfo = {}; s.p = p; s.sha = {}; s.preview = preview; s.over = {}; s.nextid = p.count; s.sans = None; s.clones = {}; s.cloneinfo = {}; s.wrappers = set(); s._allcode = None
        if preview: s.prepare_preview()

    # ---------- shapes ----------
    def fillstyle(s, f):
        p = s.p
        if f['grad'] and f['flag'] == 0x10:
            g = f['grad']; Gm = aff(g['matrix'])
            Ms = W.a_inv(Gm)
            nb = W.matrix(Ms[0], Ms[1], Ms[2], Ms[3], round(Ms[4]), round(Ms[5]))
            recs = b''.join(bytes([r]) + rgba(c) for r, c in zip(g['ratios'], g['colors']))
            return bytes([0x10]) + nb + bytes([len(g['ratios'])]) + recs
        if f['bitmap'] and f['flag'] in (0x40, 0x41):
            bi = s.idx(f['bitmap']); a = p.ptrs[bi]; bp = p.bitmap_parts(a); w, h = bp['tw'], bp['th']
            Me = aff(f['matrix'])                       # px -> UV
            F = W.a_mul((w, 0, 0, h, 0, 0), W.a_mul(Me, (1 / 20, 0, 0, 1 / 20, 0, 0)))  # twips -> fill space (SWF bitmap space = bitmap pixels)
            Ms = W.a_inv(F)
            nb = W.matrix(Ms[0], Ms[1], Ms[2], Ms[3], round(Ms[4]), round(Ms[5]))
            return bytes([f['flag']]) + struct.pack('<H', bi) + nb
        return b'\x00' + rgba(f['color'])

    def idx(s, addr):
        if not hasattr(s, '_inv'): s._inv = {a: i for i, a in enumerate(s.p.ptrs) if a}
        return s._inv[addr]

    def shape(s, i):
        p = s.p; sh = p.read_shape(p.ptrs[i])
        runs = []; cur = None; curs = None
        allpts = []
        for op, v in sh['ops']:
            if op == 4:
                cur = ('f', v); curs = None
            elif op == 5:
                cur = ('s', v)
            else:
                allpts += v
                if cur is None: cur = ('f', 1)
                if cur[0] == 'f':
                    key = ('f', cur[1])
                    if runs and runs[-1]['key'] == key and op == 2 and runs[-1]['closed']:
                        runs[-1]['polys'].append((v, True))
                    else:
                        f = sh['fills'][cur[1] - 1] if cur[1] - 1 < len(sh['fills']) else dict(flag=0, color=(255, 0, 255, 255), grad=None, bitmap=0, matrix=(1, 0, 0, 1, 0, 0))
                        runs.append(dict(key=key, closed=(op == 2), fill=s.fillstyle(f), line=None, polys=[(v, op == 2)]))
                else:
                    st = sh['strokes'][cur[1] - 1] if cur[1] - 1 < len(sh['strokes']) else dict(a=40, color=(255, 255, 255, 255))
                    ls = struct.pack('<H', max(1, st['a'])) + rgba(st['color'])
                    runs.append(dict(key=('s', cur[1]), closed=False, fill=None, line=ls, polys=[(v, False)]))
        for r in runs:       # merge fans of same-fill polygons into clean outlines (see merge_polys)
            if r['fill'] and r['closed'] and len(r['polys']) > 1:
                m = merge_polys([pts for pts, _ in r['polys']])
                if m is not None: r['polys'] = [(lp, True) for lp in m]; r['oriented'] = True
        if allpts:
            xs = [x for x, y in allpts]; ys = [y for x, y in allpts]
            bounds = W.rect(min(xs), max(xs), min(ys), max(ys))
        else: bounds = W.rect(0, 0, 0, 0)
        body = struct.pack('<H', i) + bounds + b'\x00\x00\x11' + W.shape_records(runs)   # no styles yet; NumFillBits = NumLineBits = 1
        return W.tag(32, body)

    # ---------- bitmaps ----------
    def bitmap(s, i):
        w, h, idx, pal, tt = bitmap_decode(s.p, s.p.ptrs[i])
        n = len(pal)
        ct = b''
        for r, g, b, a in pal:
            a8 = min(255, a * 2)
            ct += bytes([r * a8 // 255, g * a8 // 255, b * a8 // 255, a8])
        stride = (w + 3) & ~3
        rows = b''.join(idx[y * w:(y + 1) * w] + b'\x00' * (stride - w) for y in range(h))
        body = struct.pack('<HBHHB', i, 3, w, h, n - 1) + zlib.compress(ct + rows, 9)
        return W.tag(36, body)

    # ---------- commands ----------
    def pmatrix(s, m):
        sx, sy, r0, r1, tx, ty = m
        return W.matrix(sx / 256, r0 / 256, r1 / 256, sy / 256, tx, ty)   # a=sx b=rs0 c=rs1 d=sy

    def place(s, c):
        flags = 0; body = b''
        has_char = c['char'] != 0xFFFF
        if c['t'] == 1 and c['events']: flags |= 0x80
        if c['t'] == 1 and c['name'] is not None: flags |= 0x20
        if c['cxform']: flags |= 0x08
        if c['matrix']: flags |= 0x04
        if has_char: flags |= 0x02
        else: flags |= 0x01
        body += bytes([flags]) + struct.pack('<H', c['depth'])
        if has_char: body += struct.pack('<H', c['char'])
        if c['matrix']: body += s.pmatrix(c['matrix'])
        if c['cxform']:
            x = c['cxform']; 
            sg = lambda v: v - 256 if v > 127 else v
            mult = [sg(v) * 4 for v in x[:4]]
            add  = [sg(v) * 4 for v in x[4:]]
            body += W.cxform_alpha(mult, add)
        if flags & 0x20: body += c['name'].encode('latin-1') + b'\0'
        if flags & 0x80:
            allf = 0; recs = b''
            for fl, code, key in c['events']:
                allf |= fl; act = code[2:]
                kp = struct.pack('<B', key & 0xff) if fl & 0x20000 else b''
                recs += struct.pack('<II', fl, len(act) + len(kp)) + kp + act
            body += struct.pack('<HI', 0, allf) + recs + struct.pack('<I', 0)
        return W.tag(26, body)

    def timeline(s, frames):
        out = []; occ = set()
        for fr in frames:
            for c in fr:
                if c['t'] in (0, 1):
                    if c['char'] != 0xFFFF:       # SWF ignores a plain Place on an occupied depth: remove first
                        if c['depth'] in occ: out.append(W.tag(28, struct.pack('<H', c['depth'])))
                        occ.add(c['depth'])
                    out.append(s.place(c))
                elif c['t'] == 2: out.append(W.tag(28, struct.pack('<H', c['depth']))); occ.discard(c['depth'])
                elif c['t'] == 3: out.append(W.tag(12, c['code'][2:]))
            out.append(W.tag(1, b''))
        return out

    def frames(s, i):
        if i in s.over: return s.over[i]
        return s.p.read_sprite(s.p.ptrs[i])['frames']

    def otype(s, i):
        return 2 if i in s.over else s.p.otype(i)

    def sprite(s, i):
        fr = s.frames(i); sp = dict(nframes=len(fr), frames=fr); n = len(fr)
        inner = b''.join(s.timeline(sp['frames'])) + W.tag(0, b'')
        return W.tag(39, struct.pack('<HH', i, n) + inner), sp

    # ---------- text / font / edittext / button ----------
    def font_source(s, i, f, adv_tab):
        """pick the glyph source for swf font i: explicit --fontmap, else the font's own embedded bitmap sheet (what the engine
        draws), else best fonttex guess, else the TTF/box source"""
        if i not in s.fontmap:
            sg = SheetGlyphs(s.p, s.p.ptrs[i])
            if sg.ok:
                s.fontinfo[i] = 'embedded sheet %.0fpx (%d glyphs)' % (sg.size / 20, len(sg.recs)); return sg
        if not s.fonttex_dir: return s.glyphs
        from fonttex import FontTexGlyphs, read_fonttex
        import glob, os, statistics
        name = s.fontmap.get(i)
        if name is None:
            best = None
            for fn in sorted(glob.glob(os.path.join(s.fonttex_dir, '*.fonttex'))):
                nm = os.path.basename(fn)[:-8]
                if nm not in s.ftcache: s.ftcache[nm] = read_fonttex(fn)
                ft = s.ftcache[nm]; gm = {g['ch']: g for g in ft['glyphs']}
                chars = f['chars']; cov = sum(1 for c in chars if c in gm or c == 32) / max(1, len(chars))
                corr = 0.0
                if adv_tab:
                    xs = [a for c, a in zip(chars, adv_tab) if c in gm and c != 32 and gm[c]['w'] > 0]; ys = [gm[c]['w'] for c in chars if c in gm and c != 32 and gm[c]['w'] > 0]
                    if len(xs) >= 8:
                        mx, my = statistics.mean(xs), statistics.mean(ys); sx = sum((x - mx) ** 2 for x in xs) ** .5; sy = sum((y - my) ** 2 for y in ys) ** .5
                        corr = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy) if sx and sy else 0
                score = cov + corr
                if cov >= 0.85 and (best is None or score > best[0]): best = (score, nm)
            name = best[1] if best else None
        if not name: return s.glyphs
        em = None
        ft = s.ftcache.get(name) or read_fonttex(os.path.join(s.fonttex_dir, name + '.fonttex')); gm = {g['ch']: g for g in ft['glyphs']}
        # 1) best: the advances the text records actually use for this font (glyph advance / text size, in 1/1024 em)
        ks = []
        for oi, oa in enumerate(s.p.ptrs):
            if oa and s.p.u8(oa) == 6:
                size = None; cur = None
                for r in s.p.read_text(oa)['recs']:
                    if r[0] == 'font': cur, size = r[1], r[2]
                    elif r[0] == 'glyphs' and cur == i and size:
                        for gi, adv in r[1]:
                            c = f['chars'][gi] if gi < len(f['chars']) else None
                            g = gm.get(c)
                            if g and c != 32 and g['w'] > 0 and adv > 0: ks.append(adv * 1024.0 / size / (g['w'] + ft['spacing']))
        # 2) else the font's own advance table, 3) else the bitmap font's height
        if not ks and adv_tab:
            ks = [a / (gm[c]['w'] + ft['spacing']) for c, a in zip(f['chars'], adv_tab) if c in gm and c != 32 and gm[c]['w'] > 0]
        if ks: em = 1024.0 / statistics.median(ks)
        src = FontTexGlyphs(s.fonttex_dir, name, em)
        s.fontinfo[i] = name
        return src

    def font(s, i):
        a = s.p.ptrs[i]; f = s.p.read_font(a); n = f['n']; name = b'RpckFont%d' % i
        p2 = s.p.u32(a + 0x14)
        adv_tab = list(struct.unpack_from('<%dH' % n, s.p.d, p2 - s.p.base)) if p2 and s.p.inside(p2) else None
        src = s.font_source(i, f, adv_tab)
        if isinstance(src, SheetGlyphs) and not adv_tab: adv_tab = None
        shapes = []; adv = []; bounds = []
        for k, code in enumerate(f['chars']):
            r = src.glyph(code) if (src is s.glyphs or src.has(code) or code == 32) else None
            segs, ad = r if r else s.glyphs.glyph(code)
            adv.append(adv_tab[k] if adv_tab else ad)
            shapes.append(W.glyph_shape(segs))
            xs = [x[-2] for x in segs]; ys = [x[-1] for x in segs]
            bounds.append(W.rect(min(xs), max(xs), min(ys), max(ys)) if segs else W.rect(0, 0, 0, 0))
        flags = 0x80 | 0x08 | 0x04
        body = struct.pack('<HBBB', i, flags, 0, len(name)) + name + struct.pack('<H', n)
        base = 4 * n + 4; offs = []; pos = base
        for sh in shapes: offs.append(pos); pos += len(sh)
        body += b''.join(struct.pack('<I', o) for o in offs) + struct.pack('<I', pos) + b''.join(shapes)
        body += b''.join(struct.pack('<H', c) for c in f['chars'])
        fa = s.p.ptrs[i]; e_asc, e_extra = struct.unpack_from('<hh', s.p.d, fa + 0x9A - s.p.base)
        asc, desc = (e_asc, max(0, e_extra)) if 0 < e_asc <= 4096 and 0 <= e_extra <= 4096 else (src.asc, src.desc)
        body += struct.pack('<hhh', asc, desc, 0) + b''.join(struct.pack('<h', min(a_, 32767)) for a_ in adv) + b''.join(bounds) + struct.pack('<H', 0)
        return W.tag(48, body)

    def text(s, i):
        t = s.p.read_text(s.p.ptrs[i]); a, b_, c, d, tx, ty = t['matrix']
        recs = []; st = dict(font=None, size=0, color=None, x=None, y=None)
        maxg = 0; maxadv = 0; width = 0; size = 0
        for r in t['recs']:
            if r[0] == 'font': st['font'], st['size'] = r[1], r[2]; size = r[2]
            elif r[0] == 'color': st['color'] = r[1]
            elif r[0] == 'x': st['x'] = r[1]
            elif r[0] == 'y': st['y'] = r[1]
            else:
                g = r[1]
                for k in range(0, len(g), 255):
                    ch = g[k:k + 255]; recs.append((dict(st), ch)); st.update(font=None, color=None, x=None, y=None)
                    for gi, adv in ch: maxg = max(maxg, gi); maxadv = max(maxadv, abs(adv)); width += adv
        gb = max(1, maxg.bit_length()); ab = W.sbits(maxadv, -maxadv)
        out = b''
        for stt, ch in recs:
            fl = 0x80
            if stt['font'] is not None: fl |= 0x08
            if stt['color'] is not None: fl |= 0x04
            if stt['y'] is not None: fl |= 0x02
            if stt['x'] is not None: fl |= 0x01
            out += bytes([fl])
            if fl & 8: out += struct.pack('<H', stt['font'])
            if fl & 4: out += rgba(stt['color'])
            if fl & 1: out += struct.pack('<h', stt['x'])
            if fl & 2: out += struct.pack('<h', stt['y'])
            if fl & 8: out += struct.pack('<H', stt['size'])
            out += bytes([len(ch)])
            bw = W.Bits()
            for gi, adv in ch: bw.ub(gi, gb); bw.sb(adv, ab)
            out += bw.bytes()
        out += b'\x00'
        bounds = W.rect(0, max(width, 1), -size, size // 4)
        body = struct.pack('<H', i) + bounds + W.matrix(a, b_, c, d, round(tx * 20), round(ty * 20)) + bytes([gb, ab]) + out
        return W.tag(33, body)

    def edittext(s, i):
        e = s.p.read_edittext(s.p.ptrs[i])
        # swfEDITTEXT::Draw ignores the box offsets (+0x24/+0x28): text starts at the placement origin, first baseline at the font ascent.
        # Flash starts text 2px (40 twips) inside the box, so cancel that gutter.
        bounds = W.rect(-40, max(e['w'], 1) + 40, -40, max(e['h'], 1) + 40)
        fid = e['font'] if 0 < e['font'] < len(s.p.ptrs) and s.p.otype(e['font']) == 5 else 0
        f1 = 0x04 | 0x08 | (0x01 if fid else 0) | (0x80 if e['text'] else 0)      # HasTextColor | ReadOnly | HasFont | HasText
        f2 = 0x20 | 0x10 | (0x01 if fid else 0)                                   # HasLayout | NoSelect | UseOutlines
        body = struct.pack('<H', i) + bounds + bytes([f1, f2])
        if fid: body += struct.pack('<HH', fid, e['size'])
        body += rgba(e['color']) + bytes([min(e['align'], 3)]) + struct.pack('<HHHh', 0, 0, 0, 0)
        body += (e['key'] or '').encode('latin-1') + b'\0'
        if e['text']: body += e['text'].encode('latin-1', 'replace') + b'\0'
        return W.tag(37, body)

    def button(s, i):
        b = s.p.read_button(s.p.ptrs[i]); recs = b''
        for r in b['recs']:
            m = r['matrix']; x = r['cxform']
            recs += bytes([r['state'] & 0x0f]) + struct.pack('<HH', s.idx(r['target']), r['depth'])
            recs += W.matrix(m[0], m[1], m[2], m[3], round(m[4] * 20), round(m[5] * 20))
            recs += W.cxform_alpha([v * 4 for v in x[:4]], [v - 256 if v > 127 else v for v in x[4:]])
        recs += b'\x00'
        conds = b''
        for k, c in enumerate(b['conds']):
            cond, extra, cp = struct.unpack('<HHI', c['raw'][:8])
            act = s.p.read_code(cp)[2:]
            last = k == len(b['conds']) - 1
            ent = struct.pack('<H', cond) + act
            conds += struct.pack('<H', 0 if last else len(ent) + 2) + ent
        off = len(recs) + 2 if conds else 0
        body = struct.pack('<HBH', i, 0, off) + recs + conds
        return W.tag(34, body)

    # ---------- preview mode (not round-trippable) ----------
    # The engine treats a script's _x/_y as an offset from the placement matrix; Flash treats it as absolute.
    # Clips with scripts are parked off-screen by their placement and moved back by script (e.g. placed at x=+700px,
    # script sets _x=-700).  To get the intended look in a real player we put the placement offset inside a clone
    # of the clip (children shifted) and place the clone at the origin.
    def prepare_preview(s):
        p = s.p
        for i in range(1, p.count):
            if p.ptrs[i] and p.otype(i) == 2:
                s.over[i] = s.bake(p.read_sprite(p.ptrs[i])['frames'])

    def shift(s, frames, dx, dy):
        out = []
        for fr in frames:
            nf = []
            for c in fr:
                c = dict(c)
                if c['t'] in (0, 1) and (c['char'] != 0xFFFF or c['matrix']):
                    m = c['matrix'] or (256, 256, 0, 0, 0, 0)
                    c['matrix'] = (m[0], m[1], m[2], m[3], int(round(m[4] + dx)), int(round(m[5] + dy)))
                nf.append(c)
            out.append(nf)
        return out

    def needs_wrapper(s, c):
        """scaled/rotated placement whose clip is not addressed by name from other scripts"""
        sx, sy, r0, r1, tx, ty = c['matrix']
        if abs(sx - 256) <= 4 and abs(sy - 256) <= 4 and not r0 and not r1: return False
        return True

    @staticmethod
    def moves_xy(c):
        # only clips whose scripts assign/read _x/_y need the engine's offset semantics; clips that merely rotate/scale
        # (e.g. the spinning star) must keep their origin at the placement point or they would orbit
        return any(b'_x\0' in code or b'_y\0' in code for fl, code, k in c['events'])

    def bake(s, frames):
        out = []
        for fr in frames:
            nf = []
            for c in fr:
                c = dict(c)
                if c['t'] == 1 and c['events'] and c['matrix'] and c['char'] != 0xFFFF and s.otype(c['char']) == 2 and s.moves_xy(c) and s.needs_wrapper(c):
                    # engine: final = parent . placement . T(_x,_y) . R . S  (script offsets live in the clip's LOCAL space, so they are
                    # scaled by the placement).  Reproduce it with a wrapper that carries the placement; the script clip sits inside at identity.
                    nid = s.nextid; s.nextid += 1; s.wrappers.add(nid)
                    inner = dict(c); inner['matrix'] = None; inner['cxform'] = None; inner['depth'] = 1
                    s.over[nid] = [[inner]]
                    # the wrapper also carries the instance name so `_root.<name>._alpha = ...` style access from other scripts still hits it
                    # (alpha/visibility propagate to the inner script clip)
                    c = dict(t=1 if inner.get('name') is not None else 0, flags=c['flags'], depth=c['depth'], char=nid, matrix=c['matrix'], cxform=c['cxform'],
                             name=inner.get('name'), events=[], unk18=0)
                elif c['t'] == 1 and c['events'] and c['matrix'] and c['char'] != 0xFFFF and s.otype(c['char']) == 2 and (c['matrix'][4] or c['matrix'][5]) and s.moves_xy(c):
                    sx, sy, r0, r1, tx, ty = c['matrix']; a, d_, b, cc = sx / 256, sy / 256, r0 / 256, r1 / 256
                    det = a * d_ - b * cc
                    if abs(det) > 1e-9:
                        dx = (d_ * tx - cc * ty) / det; dy = (-b * tx + a * ty) / det
                        key = (c['char'], round(dx), round(dy))
                        if key not in s.clones:
                            nid = s.nextid; s.nextid += 1; s.clones[key] = nid
                            base = s.over.get(c['char']) or s.p.read_sprite(s.p.ptrs[c['char']])['frames']
                            s.over[nid] = s.bake(s.shift(base, key[1], key[2]))
                        s.cloneinfo.setdefault(s.clones[key], (c['char'], key[1], key[2], tx, ty, sx, sy, r0, r1))
                        c['char'] = s.clones[key]; c['matrix'] = (sx, sy, r0, r1, 0, 0)
                nf.append(c)
            out.append(nf)
        return out

    def text_edit(s, i):
        """static text -> read-only DefineEditText using a device font (glyph outlines are not in the pck)"""
        p = s.p; t = p.read_text(p.ptrs[i]); font = None; size = 240; color = (255, 255, 255, 255)
        x = 0; y = 0; lines = []; cur = ''; chars = None; width = 0
        for r in t['recs']:
            if r[0] == 'font':
                font, size = r[1], r[2]
                chars = p.read_font(p.ptrs[font])['chars'] if font < len(p.ptrs) and p.otype(font) == 5 else None
            elif r[0] == 'color': color = r[1]
            elif r[0] == 'x': x = r[1] if not lines and not cur else x
            elif r[0] == 'y':
                if cur: lines.append(cur); cur = ''
                y = r[1] if not lines else y
            else:
                for gi, adv in r[1]:
                    cur += chr(chars[gi]) if chars and gi < len(chars) and 32 <= chars[gi] < 0x3000 else '?'
                width = max(width, sum(a for _, a in r[1]))
        if cur: lines.append(cur)
        txt = '\r'.join(lines)
        bounds = W.rect(x - 40, x + width + 200, y - size, y + size * max(1, len(lines)))
        body = struct.pack('<H', i) + bounds + bytes([0x80 | 0x20 | 0x08 | 0x04 | 0x01, 0x10 | 0x04])
        body += struct.pack('<HH', s.sans, size) + rgba(color) + bytes([0]) + struct.pack('<HHHh', 0, 0, 0, 0) + b'\0' + txt.encode('latin-1', 'replace') + b'\0'
        return W.tag(37, body)

    # ---------- driver ----------
    def deps(s, i):
        p = s.p; t = s.otype(i); out = []
        a = p.ptrs[i] if i < len(p.ptrs) else 0
        if t == 1:
            for f in p.read_shape(a)['fills']:
                if f['bitmap'] and f['flag'] in (0x40, 0x41): out.append(s.idx(f['bitmap']))
        elif t == 2:
            for fr in s.frames(i):
                for c in fr:
                    if c['t'] in (0, 1) and c['char'] != 0xFFFF: out.append(c['char'])
        elif t == 6:
            out += [r[1] for r in p.read_text(a)['recs'] if r[0] == 'font']
        elif t == 3:
            out += [s.idx(r['target']) for r in p.read_button(a)['recs']]
        return out

    def convert(s, compress=False, embed=True, src_bytes=None):
        p = s.p; order = []; seen = set()
        root = [[p.read_cmd(c) for c in p.cmds(h)] for h in p.frames_of(p.frames, p.fc)]
        if s.preview: root = s.bake(root)      # may create clone sprites, so do it before building the order
        def visit(i):
            if i in seen: return
            seen.add(i)
            for d in s.deps(i):
                if (0 < d < len(p.ptrs) and p.ptrs[d]) or d in s.over: visit(d)
            order.append(i)
        for i in range(1, len(p.ptrs)):
            if p.ptrs[i]: visit(i)
        for i in sorted(s.over):
            visit(i)
        tags = []
        bg = p.raw(p.frames, 4)
        tags.append(W.tag(9, bg[:3]))
        meta_idx = len(tags); tags.append(b'')
        warn = []
        for i in order:
            t = s.otype(i)
            try:
                if t == 1: tg = s.shape(i)
                elif t == 4: tg = s.bitmap(i)
                elif t == 2: tg, _ = s.sprite(i)
                elif t == 5: tg = s.font(i)
                elif t == 6: tg = s.text(i)
                elif t == 7: tg = s.edittext(i)
                elif t == 3: tg = s.button(i)
                else: warn.append('object %d: unknown type %s' % (i, t)); continue
            except Exception as e:
                warn.append('object %d type %s: %r' % (i, t, e)); continue
            s.sha[i] = hashlib.sha1(payload_of(tg)).hexdigest(); tags.append(tg)
        rt = s.timeline(root)
        rsha = hashlib.sha1(b''.join(payload_of(t) for t in rt)).hexdigest()
        tags += rt
        if embed:
            meta = dict(v=1, sha=s.sha, root=rsha, order=order, preview=s.preview, sans=s.sans, wrappers=sorted(s.wrappers), clones={str(k): list(v) for k, v in s.cloneinfo.items()}, pck=base64.b64encode(zlib.compress(src_bytes or p.tobytes(), 9)).decode())
            blob = b'RPCK1:' + base64.b64encode(zlib.compress(json.dumps(meta).encode(), 9)) + b'\0'
            tags[meta_idx] = W.tag(77, blob)
        else: tags.pop(meta_idx)
        fr = W.rect(round(p.xmin * 20), round(p.xmax * 20), round(p.ymin * 20), round(p.ymax * 20))
        return W.swf_file(6, fr, p.rate88, p.fc, tags, compress), warn

def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('pck'); ap.add_argument('swf'); ap.add_argument('--zlib', action='store_true', help='compress (CWS)')
    ap.add_argument('--no-embed', action='store_true', help='do not embed the source pck in the SWF')
    ap.add_argument('--preview', action='store_true', help='viewer-friendly SWF (engine _x/_y offset semantics baked into clip copies; convertible back)')
    ap.add_argument('--fonttex', help='directory with <name>.fonttex and texture/<name>_NN.tex; real bitmap-font glyphs are traced to outlines')
    ap.add_argument('--fontmap', help='swf font object index -> fonttex name, e.g. 30=minidub,33=stencil (default: best guess, printed)')
    ap.add_argument('--font', help='TTF used for glyph outlines (the pck has none); default: a system DejaVu/Liberation/Arial. Needs fontTools; boxes otherwise')
    a = ap.parse_args()
    raw = open(a.pck, 'rb').read(); p = Pck(raw)
    gs = GlyphSrc(a.font)
    if not gs.ok: print('note: no usable TTF/fontTools - glyphs will be placeholder boxes')
    fm = dict((int(k), v) for k, v in (kv.split('=') for kv in a.fontmap.split(','))) if a.fontmap else {}
    cvx = Conv(p, a.preview, gs, a.fonttex, fm)
    out, warn = cvx.convert(a.zlib, not a.no_embed, raw)
    for fi, nm in sorted(cvx.fontinfo.items()): print('font %d -> %s' % (fi, nm))
    open(a.swf, 'wb').write(out)
    print('wrote %s (%d bytes, %d objects)' % (a.swf, len(out), p.count - 1))
    for w in warn: print('warning:', w)

if __name__ == '__main__': main()
