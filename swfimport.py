#!/usr/bin/env python3
"""swfimport: build a brand-new MC3 (PS2) swf .pck from an ordinary Flash SWF.

    python3 swfimport.py ui.swf ui.pck --donor any_game.pck [--fit 640x480] [--drop-action getBytesTotal] [--sizes 240,400]

Supported: shapes (solid/gradient fills, curves tessellated, strokes converted to filled polygons), sprites and the root timeline
(place/move/remove/actions, rotation/scale, colour transforms, instance names), static text and edit text, fonts (DefineFont2/3:
glyph outlines are rasterised into the engine's 128x128 4bpp sheets at every size the movie uses).
Not supported yet (warned): bitmaps, buttons, sound/video, morphs, masks, radial gradients (approximated).
The donor pck (any original game pck) supplies header words and the page/font templates."""
import sys, struct, math, argparse
from rpck import *
import swfr, swfw as W, tess
from swf2pck import Writer, clamp
import pckfont
import swfbitmap as BM

SUPPORTED_OPS = {0x04, 0x05, 0x06, 0x07, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f, 0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x17, 0x18, 0x1c, 0x1d, 0x21, 0x26, 0x29, 0x30,
                 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x3c, 0x3f, 0x40, 0x41, 0x44, 0x47, 0x48, 0x49, 0x4c, 0x4d, 0x4e, 0x4f, 0x50, 0x51, 0x52, 0x60, 0x61, 0x62,
                 0x63, 0x64, 0x65, 0x67, 0x81, 0x88, 0x96, 0x99, 0x9d, 0x9e, 0x9f}


def ops_of(code):
    i = 0; out = []
    while i < len(code) and code[i]:
        op = code[i]; out.append((op, i))
        i += 1 if op < 0x80 else 3 + struct.unpack_from('<H', code, i + 1)[0]
    return out


# ------------------------------------------------------------------ strokes -> polygons
def stroke_tris(chain, width):
    hw = max(width, 20) / 2.0; tris = []
    for (x0, y0), (x1, y1) in zip(chain, chain[1:]):
        dx, dy = x1 - x0, y1 - y0; L = math.hypot(dx, dy)
        if L < 1e-6: continue
        nx, ny = -dy / L * hw, dx / L * hw
        q = [(x0 + nx, y0 + ny), (x1 + nx, y1 + ny), (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)]
        q = [(round(a), round(b)) for a, b in q]; tris.append(q)
    if hw >= 10:     # round joins/caps
        for (x, y) in chain:
            pts = [(round(x + hw * math.cos(k * math.pi / 3)), round(y + hw * math.sin(k * math.pi / 3))) for k in range(6)]
            tris.append(pts)
    return tris


def strokes_to_fills(sh):
    for g in sh['groups']:
        if tess.SIMPLIFY > 0 and not g.get('_simp'): g['edges'] = tess.simplify_edges(g['edges']); g['_simp'] = True
        out = []
        for li, ls in enumerate(g['lines'], 1):
            chain = []; tris = []
            for kind, pts, f0, f1, ln in g['edges']:
                if ln != li: continue
                ps = [pts[0]] + (pts[1:] if kind == 'l' else tess.flatten_q(*pts))
                if chain and chain[-1] == ps[0]: chain += ps[1:]
                else:
                    if len(chain) > 1: tris += stroke_tris(chain, ls['width'])
                    chain = list(ps)
            if len(chain) > 1: tris += stroke_tris(chain, ls['width'])
            if tris: out.append((ls['color'], tris))
        g['stroke_tris'] = out
    return sh


# ------------------------------------------------------------------ fonts
class SwfFontRaster:
    """rasterises DefineFont2/3 outlines for pckfont.build_sheet (same interface as pckfont.Raster)"""
    def __init__(s, font, size_twips, ss=4):
        s.f = font; s.px = size_twips / 20.0; s.U = 1024.0 / s.px; s.ss = ss
        s.k = s.px / float(font['em']); s.idx = {c: i for i, c in enumerate(font['codes'])}

    def advance_units(s, i):
        a = s.f.get('adv_units'); return a[i] if a else 0

    def glyph(s, code):
        from PIL import Image, ImageDraw, ImageChops
        i = s.idx.get(code)
        if i is None: return None, (0, 0, 0, 0, 0)
        adv = s.advance_units(i)
        polys = []
        for c in s.f['glyphs'][i]:
            pts = []; cur = (c[0][1], c[0][2]); pts.append(cur)
            for seg in c[1:]:
                if seg[0] == 'L': cur = (seg[1], seg[2]); pts.append(cur)
                else: pts += tess.flatten_q(cur, (seg[1], seg[2]), (seg[3], seg[4]))[:]; cur = (seg[3], seg[4])
            if len(pts) >= 3: polys.append([(x * s.k, y * s.k) for x, y in pts])
        if not polys: return None, (0, 0, 0, 0, adv)
        xs = [x for p in polys for x, y in p]; ys = [y for p in polys for x, y in p]
        x0, y0 = int(math.floor(min(xs))), int(math.floor(min(ys))); x1, y1 = int(math.ceil(max(xs))), int(math.ceil(max(ys)))
        w, h = max(1, x1 - x0), max(1, y1 - y0); ss = s.ss
        mask = Image.new('1', (w * ss, h * ss), 0)
        for p in polys:
            m = Image.new('1', mask.size, 0); ImageDraw.Draw(m).polygon([((x - x0) * ss, (y - y0) * ss) for x, y in p], fill=1); mask = ImageChops.logical_xor(mask, m)
        im = mask.convert('L').resize((w, h), Image.BOX)
        return im, (int(round(x0 * s.U)), int(round(y0 * s.U)), adv, int(round((y0 + h) * s.U)), adv)


def font_object(p, tpl, donor_hdr, font, sizes, log):
    codes = font['codes']; n = len(codes); fid = font['id']
    for sz in sizes: pass
    ptrs = []
    for sz in sizes:
        r = SwfFontRaster(font, sz)
        sheet, npg, _ = pckfont.build_sheet(p, tpl, r, codes); p.put(sheet + 8, '<hH', sz, n); ptrs.append(sheet)
        log('  font %d (%s): %.1fpx sheet -> %d page(s)' % (fid, font['name'], sz / 20.0, npg))
    em = font['em']; lay = font['layout']
    asc = int(round(lay['ascent'] * 1024.0 / em)) if lay else 920; ext = int(round((lay['descent'] + lay['leading']) * 1024.0 / em)) if lay else 260
    hdr = bytearray(donor_hdr[:0xD0]); hdr[0] = 5; struct.pack_into('<I', hdr, 4, VT[5]); struct.pack_into('<H', hdr, 8, n)
    glyphtab = p.alloc(b'\0' * (8 * n), 4); chars = p.alloc(struct.pack('<%dH' % n, *codes), 4)
    adv = font['adv_units']; p2 = p.alloc(struct.pack('<%dH' % n, *[clamp(v, 0, 65535) for v in adv]), 4)
    struct.pack_into('<III', hdr, 0xC, glyphtab, chars, p2)
    hdr[0x18:0x98] = bytes(0x80)
    for i, c in enumerate(codes):
        if 0 < c < 128: hdr[0x18 + c] = i
    struct.pack_into('<hhhh', hdr, 0x98, len(ptrs), clamp(asc, 0, 4096), clamp(ext, 0, 4096), clamp(ext, 0, 4096))
    hdr[0xA0:0xC0] = b'\xCD' * 0x20
    for k, sp in enumerate(ptrs): struct.pack_into('<I', hdr, 0xA0 + 4 * k, sp)
    struct.pack_into('<I', hdr, 0xC0, n)
    return p.alloc(bytes(hdr), 16)


# ------------------------------------------------------------------ text objects
def text_object(p, t, log):
    a, b, c, d, tx, ty = t['matrix']; rec = b''
    for r in t['records']:
        if r['color'] is not None: rec += struct.pack('<H', 2) + bytes(r['color'])
        if r['font'] is not None: rec += struct.pack('<HHH', 1, r['font'], r['size'])
        if r['x'] is not None: rec += struct.pack('<Hh', 4, r['x'])
        if r['y'] is not None: rec += struct.pack('<Hh', 3, r['y'])
        for k in range(0, len(r['glyphs']), 65535):
            g = r['glyphs']; rec += struct.pack('<HH', 5, len(g)) + b''.join(struct.pack('<HH', gi, adv & 0xFFFF) for gi, adv in g)
    rec += struct.pack('<H', 0)
    ra = p.alloc(rec, 4)
    return p.alloc(struct.pack('<B3xI6fI', 6, VT[6], a, b, c, d, tx / 20.0, ty / 20.0, ra) + b'\xCD' * 12, 16)


def edittext_object(p, e, fonts, log):
    text = (e['text'] or '').encode('latin-1', 'replace') + b'\0'; var = (e['var'] or '').encode('latin-1', 'replace') + b'\0'
    ta = p.alloc(text, 4); va = p.alloc(var, 4)
    x0, x1, y0, y1 = e['bounds']; size = e['size'] or 240; font = e['font'] if e['font'] in fonts else 0
    al = e['align'] if e['f2'] & 0x20 else 0
    obj = struct.pack('<B3xIIIHh4BHHiiiiB', 7, VT[7], ta, va, len(text), 0, *e['color'], font, size, x1 - x0, y1 - y0, x0, y0, al) + b'\xCD' * 3
    return p.alloc(obj, 16)



class ImportWriter(Writer):
    """Writer whose bitmap lookups come from the importer (SWF bitmap id -> object address, ORIGINAL size, UV scale of the padded texture)"""
    bm = {}
    def bitmap_info(s, bi):
        return s.bm.get(bi)


def parse_button2(pl):
    r = swfr.BR(pl); bid = r.u16(); r.u8(); aoff = r.u16(); recs = []
    while True:
        f = r.u8()
        if f == 0: break
        cid = r.u16(); depth = r.u16(); m = r.matrix(); cx = r.cxform(True)
        recs.append(dict(state=f & 0x0F, char=cid, depth=depth, matrix=m, cxform=cx))
    conds = []
    if aoff:
        while r.p < len(pl):
            start = r.p; sz = r.u16(); cond = r.u16(); end = len(pl) if sz == 0 else start + sz
            conds.append((cond, pl[r.p:end]))
            if sz == 0: break
            r.p = end
    return dict(id=bid, records=recs, conds=conds)


def parse_font1(pl, info):
    r = swfr.BR(pl); fid = r.u16(); base = r.p; first = r.u16(); n = first // 2
    offs = [first] + [r.u16() for _ in range(n - 1)]
    glyphs = [swfr.parse_glyph(pl, base + o) for o in offs]
    codes = None; name = 'font%d' % fid
    if info:
        icode, ipl = info; q = swfr.BR(ipl); q.u16(); nl = q.u8(); name = q.rd(nl).decode('latin-1'); fl = q.u8()
        if icode == 62: q.u8()
        wide = bool(fl & 1); codes = [(q.u16() if wide else q.u8()) for _ in range(n)]
    return dict(id=fid, name=name, n=n, glyphs=glyphs, codes=codes or [32 + i for i in range(n)], em=1024, layout=None, guessed=codes is None)


def text_to_shape(t, fonts):
    """static text -> vector SHAPE description (glyph outlines with advances, colour and the text matrix baked in).
    Used for text that the placements scale up a lot: the engine would stretch a bitmap font sheet, a vector stays sharp."""
    from pck2swf import reverse_contour
    fills = []; edges = []; x = y = 0; size = 240; font = None; color = (0, 0, 0, 255)
    a, b, c, d, tx, ty = t['matrix']
    def T(px, py): return (a * px + c * py + tx, b * px + d * py + ty)
    for r in t['records']:
        if r['font'] is not None: font = fonts.get(r['font'])
        if r['size']: size = r['size']
        if r['color'] is not None: color = r['color']
        if r['x'] is not None: x = r['x']
        if r['y'] is not None: y = r['y']
        key = tuple(color)
        if key not in [f['color'] for f in fills]: fills.append(dict(type=0, color=key))
        fi = [f['color'] for f in fills].index(key) + 1
        for gi, adv in r['glyphs']:
            if font is not None and gi < len(font['glyphs']):
                k = size / float(font['em']); conts = font['glyphs'][gi]
                def area(cn):
                    pts = [(q[-2], q[-1]) for q in cn]; return tess.area2(pts)
                if conts and area(max(conts, key=lambda cn: abs(area(cn)))) < 0: conts = [reverse_contour(cn) for cn in conts]
                for cn in conts:
                    cur = (cn[0][1], cn[0][2]); start = cur
                    for seg in cn[1:]:
                        if seg[0] == 'L':
                            nxt = (seg[1], seg[2]); edges.append(('l', [T(x + cur[0] * k, y + cur[1] * k), T(x + nxt[0] * k, y + nxt[1] * k)], 0, fi, 0)); cur = nxt
                        else:
                            ctl = (seg[1], seg[2]); nxt = (seg[3], seg[4])
                            edges.append(('q', [T(x + cur[0] * k, y + cur[1] * k), T(x + ctl[0] * k, y + ctl[1] * k), T(x + nxt[0] * k, y + nxt[1] * k)], 0, fi, 0)); cur = nxt
                    if cur != start: edges.append(('l', [T(x + cur[0] * k, y + cur[1] * k), T(x + start[0] * k, y + start[1] * k)], 0, fi, 0))
            x += adv
    return dict(id=t['id'], bounds=(0, 0, 0, 0), groups=[dict(fills=fills, lines=[], edges=edges)])


def strip_preloader(frames, where, log):
    """Flash preloader: frame script polling getBytesLoaded/getBytesTotal plus a later 'goto that frame; play' loop-back.
    Replace the loader by a jump straight to the frame it would jump to when loaded (keeping its trailing play/stop) and drop the loop-back.
    Returns True if the removed script stopped the _root timeline (the importer then adds a root stop so a start button still gates the show)."""
    root_stop = False
    for fi, f in enumerate(frames):
        for k, d in list(f):
            if k != 'action' or not (b'getBytesLoaded' in d or b'getBytesTotal' in d): continue
            ops = ops_of(d); target = None; tail = None
            for j, (op, pos) in enumerate(ops):
                if op == 0x81: target = struct.unpack_from('<H', d, pos + 3)[0]; tail = ops[j + 1][0] if j + 1 < len(ops) and ops[j + 1][0] in (0x06, 0x07) else None
            if b'_root\0' in d and b'stop\0' in d: root_stop = True
            new = b''
            if target is not None: new += b'\x81\x02\x00' + struct.pack('<H', target)
            new += bytes([tail if tail is not None else (0x06 if where == 'root' else 0x07)]) + b'\0'
            f[f.index((k, d))] = ('action', new)
            log('preloader: %s frame %d loader script replaced by %s' % (where, fi, 'goto %d' % target if target is not None else 'stop/play'))
            loop = b'\x81\x02\x00' + struct.pack('<H', fi) + b'\x06\x00'
            for fj in range(fi + 1, min(len(frames), fi + 4)):
                for k2, d2 in list(frames[fj]):
                    if k2 == 'action' and d2 == loop: frames[fj].remove((k2, d2)); log('preloader: %s frame %d loop-back removed' % (where, fj))
    return root_stop


# ------------------------------------------------------------------ build
def build(swf_bytes, donor_path, fit=None, drop_actions=(), extra_sizes=(), log=print, no_preloader=False, omit=(), bitmap_budget=600000, max_bitmap_dim=256, text_shape_scale=1.75, jpeg_tables_from=None):
    swf = swfr.read_swf(swf_bytes); ver = swf['version']
    donor = Pck(donor_path); tpl = pckfont.template(donor); tpl['next0'] = 0x245040   # global texture-list head: the first texture of every original file links to this constant
    dfont = next((donor.raw(a, 0xD0) for a in donor.ptrs if a and donor.u8(a) == 5 and sheets_ok(donor, a)), None)
    if dfont is None: raise SystemExit('the donor pck has no font with sheets (needed as a template)')
    # ---- skeleton
    hdr = bytes(donor.hdr); base = donor.base
    data = bytearray(b'\xCD' * 0x40)
    struct.pack_into('<B', data, 0, 0); struct.pack_into('<I', data, 4, VT[0]); struct.pack_into('<H', data, 8, 0)
    struct.pack_into('<II', data, 0xC, 0, base + 0x30); data[0x14] = 6; struct.pack_into('<4f', data, 0x18, 0.0, 0.0, 0.0, 0.0); struct.pack_into('<HH', data, 0x28, 0, 0)
    raw = bytearray(hdr); struct.pack_into('<IIII', raw, 0, base, donor.u1, donor.u2, len(data)); p = Pck(bytes(raw) + bytes(data))
    wr = ImportWriter(p); wr.bm = {}; warn = []
    def W_(m): warn.append(m); log('warning: ' + m)
    tags = swf['tags']
    defs = {}; fontinfo = {}; jtables = b''
    for code, pl in tags:
        if code in (2, 22, 32, 39, 48, 75, 10, 11, 33, 37, 34, 6, 20, 21, 35, 36): defs[struct.unpack_from('<H', pl)[0]] = (code, pl)
        elif code in (13, 62): fontinfo[struct.unpack_from('<H', pl)[0]] = (code, pl)
        elif code == 8: jtables = pl
        elif code == 7: W_('DefineButton (v1, id %d) not supported - skipped' % struct.unpack_from('<H', pl)[0])
        elif code in (14, 15, 17, 18, 19, 45): pass
        elif code in (46, 84): W_('morph shape not supported - skipped')
    if any(c in (14, 18, 45) for c, _ in tags): W_('sound/streaming audio is not converted')
    omit = set(omit); silent = set()
    if 'text' in omit:
        for i in [i for i, (c, _) in defs.items() if c in (48, 75, 10, 11, 33, 37)]: silent.add(i); del defs[i]
        log('omit: fonts, static text and edit text left out')
    # ---- fonts + text sizes
    fonts = {}
    for i, (code, pl) in defs.items():
        if code in (48, 75): fonts[i] = swfr.parse_font2(pl, code)
        elif code == 10:
            fonts[i] = parse_font1(pl, fontinfo.get(i))
            if fonts[i].get('guessed'): W_('font %d has no DefineFontInfo: character codes guessed' % i)
    sizes = {i: set(int(x) for x in extra_sizes) for i in fonts}
    texts = {i: swfr.parse_text(pl, code) for i, (code, pl) in defs.items() if code in (11, 33)}
    edits = {i: swfr.parse_edittext(pl) for i, (code, pl) in defs.items() if code == 37}
    # effective scale of every character: product of the placement scales along the sprite chain (root gets the --fit scale)
    fit_scale = 1.0
    if fit is not None:
        fr_ = swf['frame']; fit_scale = min(fit[0] / ((fr_[1] - fr_[0]) / 20.0), fit[1] / ((fr_[3] - fr_[2]) / 20.0))
    placements = {}      # parent id (0 = root) -> [(char, max scale)]
    def collect(tags, parent):
        for f in swfr.parse_timeline_ex(tags, ver)[0]:
            for k_, d_ in f:
                if k_ == 'place' and d_['char'] is not None:
                    m = d_['matrix']; sc = max(abs(m[0]), abs(m[3]), math.hypot(m[0], m[1]), math.hypot(m[2], m[3])) if m else 1.0
                    placements.setdefault(parent, []).append((d_['char'], sc))
    collect([(c, pl) for c, pl in tags if c in (26, 28, 1, 4, 5)], 0)
    for i_, (c_, pl_) in defs.items():
        if c_ == 39: collect(swfr.read_tags(pl_, 4), i_)
    users = {}
    for par, lst in placements.items():
        for ch, sc in lst: users.setdefault(ch, []).append((par, sc))
    memo = {}
    def eff(i, guard=()):
        if i in memo: return memo[i]
        if i in guard: return 1.0
        best = 1.0 if i not in users else 0.0
        for par, sc in users.get(i, []):
            best = max(best, sc * (fit_scale if par == 0 else eff(par, guard + (i,))))
        memo[i] = best; return best
    as_shape = {i for i, t in texts.items() if eff(i) > text_shape_scale}
    if as_shape: log('text: %d static text object(s) are scaled up more than %.2fx by their placements -> vector shapes (ids %s)' % (len(as_shape), text_shape_scale, sorted(as_shape)))
    for i, t in texts.items():
        if i in as_shape: continue
        for r in t['records']:
            if r['font'] in fonts and r['size']: sizes[r['font']].add(r['size'])
    for e in edits.values():
        if e['font'] in fonts and e['size']: sizes[e['font']].add(e['size'])
    # advances (1/1024 em)
    for fid, f in fonts.items():
        n = f['n']; adv = [0] * n
        if f['layout']: adv = [int(round(v * 1024.0 / f['em'])) for v in f['layout']['advance']]
        else:
            seen = {}
            for t in texts.values():
                for r in t['records']:
                    if r['font'] == fid and r['size']:
                        for gi, a in r['glyphs']: seen.setdefault(gi, []).append(a * 1024.0 / r['size'])
            for i in range(n):
                if i in seen: adv[i] = int(round(sum(seen[i]) / len(seen[i])))
                else:
                    xs = [seg[-2] for c in f['glyphs'][i] for seg in c]; adv[i] = int(round((max(xs) - 0 if xs else 400) * 1024.0 / f['em'] * 1.05))
        f['adv_units'] = adv
        if not sizes[fid]: sizes[fid].add(240)
    # dense object indices (no gaps: the engine walks the whole object list); fonts first so text/edit text can refer back to them
    kind = lambda i: defs[i][0]
    order = sorted(fonts) + [i for i in sorted(defs) if i not in fonts and kind(i) in (6, 20, 21, 35, 36)] + [i for i in sorted(defs) if i not in fonts and kind(i) not in (6, 20, 21, 35, 36, 34)] + [i for i in sorted(defs) if i not in fonts and kind(i) == 34]
    idmap = {old: new for new, old in enumerate(order, 1)}
    if any(o != n for o, n in idmap.items()): log('object ids renumbered densely (%d objects)' % len(idmap))
    for t in texts.values():
        for r in t['records']:
            if r['font'] is not None: r['font'] = idmap.get(r['font'], 0)
    mfonts = set(idmap[i] for i in fonts)
    objs = {}
    for fid, f in fonts.items(): objs[idmap[fid]] = font_object(p, tpl, dfont, f, sorted(sizes[fid]), log)
    if not jtables and any(c == 6 for c, _ in defs.values()):
        if jpeg_tables_from:
            src_swf = swfr.read_swf(open(jpeg_tables_from, 'rb').read()); jt_ = [pl for c, pl in src_swf['tags'] if c == 8]
            if jt_: jtables = jt_[0]; log('JPEGTables taken from %s (%d bytes)' % (jpeg_tables_from, len(jtables)))
            else: W_('%s has no JPEGTables either' % jpeg_tables_from)
        else: W_('DefineBits images need a JPEGTables tag, which this SWF does not have (an editor dropped it): they will decode as black. Pass --jpeg-tables original.swf')
    # ---- bitmaps: decode, downscale into power-of-two textures within the memory budget, quantise to 256 colours
    bmids = [i for i, (c, _) in defs.items() if c in (6, 20, 21, 35, 36)]
    if bmids:
        tpl_bm = BM.template_from(donor); imgs = {}
        for i in bmids:
            c, pl = defs[i]
            try: imgs[i] = BM.decode_bitmap(c, pl, jtables)
            except Exception as e: W_('bitmap %d could not be decoded (%r) - skipped' % (i, e))
        plan = BM.choose_sizes({i: im.size for i, im in imgs.items()}, bitmap_budget, max_bitmap_dim)
        chain = dict(last=tpl['next0'])
        for i in sorted(imgs):
            w, h, sw, sh = plan[i]; w0, h0 = imgs[i].size
            idx, pal = BM.quantize(imgs[i], w, h)                      # whole image stretched over the whole texture
            addr = BM.emit_bitmap(p, tpl_bm, w, h, idx, pal, chain, (w0, h0)); tpl['next0'] = chain['last']      # stored inverse size = SOURCE size
            wr.bm[i] = (addr, w0, h0, 1.0, 1.0); objs[idmap[i]] = addr
        tb = sum(plan[i][0] * plan[i][1] for i in plan)
        log('bitmaps: %d images (%.1f Mpixel) -> %d texture bytes in %d textures (budget %d)' % (len(imgs), sum(a * b for a, b in (im.size for im in imgs.values())) / 1e6, tb, len(plan), bitmap_budget))
    # ---- shapes
    for i, (code, pl) in defs.items():
        if code in (2, 22, 32):
            sh = strokes_to_fills(swfr.parse_shape_any(code, pl))
            objs[idmap[i]] = wr.shape(sh)
    text_mat = {}
    for i, t in texts.items():
        if i in as_shape:
            objs[idmap[i]] = wr.shape(strokes_to_fills(text_to_shape(t, {idmap[f_]: ft for f_, ft in fonts.items()}))); continue    # records already carry dense font ids
        m = t['matrix']
        if tuple(round(v, 6) for v in (m[0], m[1], m[2], m[3])) != (1.0, 0.0, 0.0, 1.0) or m[4] or m[5]: text_mat[i] = m
        t2 = dict(t); t2['matrix'] = (1.0, 0.0, 0.0, 1.0, 0, 0)     # the engine multiplies the text matrix AFTER the placement chain (swfTEXT::Draw): keep it identity
        objs[idmap[i]] = text_object(p, t2, log)
    edit_shift = {}
    for i, e in edits.items():
        e2 = dict(e); e2['font'] = idmap[e['font']] if e['font'] in fonts else None
        objs[idmap[i]] = edittext_object(p, e2, mfonts, log); edit_shift[i] = (e['bounds'][0] + 40, e['bounds'][2] + 40)
    # ---- timelines
    def fix_actions(code, labels, where):
        for pat in drop_actions:
            if pat.encode() in code: log('dropped action (%s) matching %r' % (where, pat)); return None
        ops = ops_of(code)
        for op, pos in ops:
            if op == 0x8C: pass
            if op not in SUPPORTED_OPS and op != 0x8C: W_('script in %s uses opcode %#x which the engine interpreter does not implement' % (where, op))
        if any(op == 0x8C for op, _ in ops):
            if any(op in (0x99, 0x9D) for op, _ in ops): W_('GotoLabel in a script with jumps cannot be rewritten (%s)' % where)
            else:
                out = bytearray(); i = 0
                while code[i]:
                    op = code[i]
                    if op < 0x80: out.append(op); i += 1; continue
                    n = struct.unpack_from('<H', code, i + 1)[0]; body = code[i + 3:i + 3 + n]
                    if op == 0x8C:
                        lab = body.split(b'\0')[0].decode('latin-1'); out += b'\x81\x02\x00' + struct.pack('<H', labels.get(lab, 0))
                    else: out += code[i:i + 3 + n]
                    i += 3 + n
                code = bytes(out) + b'\0'
        return code
    def conv_frames(frames, labels, where, root=False):
        out = []; state = {}; depth_char = {}      # depth -> last matrix/cxform ; depth -> character currently there (Flash keeps them when a character is replaced with move+character)
        for fr in frames:
            nf = []
            for k, d in fr:
                if k == 'action':
                    if 'actions' in omit: continue
                    c = fix_actions(d, labels, where)
                    if c is not None: nf.append(('action', c))
                    continue
                if k == 'remove': state.pop(d, None); depth_char.pop(d, None)
                if k == 'place':
                    d = dict(d); st = state.get(d['depth'])
                    if d['char'] is not None and st is not None:      # replace: unspecified matrix / colour transform are inherited
                        if d['matrix'] is None: d['matrix'] = st['matrix']
                        if d['cxform'] is None: d['cxform'] = st['cxform']
                    if d['char'] is not None and not d['move']: state[d['depth']] = dict(matrix=d['matrix'], cxform=d['cxform'])
                    elif st is not None:
                        if d['matrix'] is not None: st['matrix'] = d['matrix']
                        if d['cxform'] is not None: st['cxform'] = d['cxform']
                        if d['char'] is not None: pass
                    elif d['char'] is not None: state[d['depth']] = dict(matrix=d['matrix'], cxform=d['cxform'])
                    if k == 'place':
                        ch_ = d['char'] if d['char'] is not None else depth_char.get(d['depth'])
                        if d['char'] is not None: depth_char[d['depth']] = d['char']
                        if ch_ in text_mat and (d['matrix'] is not None or d['char'] is not None):
                            P = d['matrix'] or (1.0, 0.0, 0.0, 1.0, 0, 0); M = text_mat[ch_]
                            d['matrix'] = W.a_mul(P, M); d['matrix'] = d['matrix'][:4] + (round(d['matrix'][4]), round(d['matrix'][5]))
                    if d['char'] in edit_shift and d['char'] is not None and d['matrix'] is None: d['matrix'] = (1.0, 0.0, 0.0, 1.0, 0, 0)
                    if d['char'] in edit_shift and d['matrix']:
                        dx, dy = edit_shift[d['char']]; a, b, c, dd, tx, ty = d['matrix']; d['matrix'] = (a, b, c, dd, round(tx + a * dx + c * dy), round(ty + b * dx + dd * dy))
                    if d['flags'] & 0x40: W_('clip depth / mask in %s ignored (masks are not supported by the engine)' % where)
                    if d['char'] is not None:
                        if d['char'] not in idmap:
                            if d['char'] not in silent: W_('placement of unsupported character %d in %s dropped' % (d['char'], where))
                            continue
                        d['char'] = idmap[d['char']]
                    if root and fit and (d['matrix'] or d['char'] is not None):
                        s, ox, oy = fit; a, b, c, dd, tx, ty = d['matrix'] or (1.0, 0.0, 0.0, 1.0, 0, 0)
                        d['matrix'] = (a * s, b * s, c * s, dd * s, round(tx * s + ox), round(ty * s + oy))
                    nf.append(('place', d))
                else: nf.append((k, d))
            out.append(nf)
        return out
    need_root_stop = [False]
    for i, (code, pl) in defs.items():
        if code == 39:
            sid, nf, _ = swfr.parse_sprite(pl); frames, labels = swfr.parse_timeline_ex(swfr.read_tags(pl, 4), ver)
            if not frames: frames = [[]]
            if no_preloader and strip_preloader(frames, 'sprite %d' % i, log): need_root_stop[0] = True
            objs[idmap[i]] = wr.sprite(conv_frames(frames, labels, 'sprite %d' % i))
    # ---- buttons (after everything they point to)
    for i, (code, pl) in defs.items():
        if code != 34: continue
        bt = parse_button2(pl); recs = b''; ok = True
        for r in bt['records']:
            if r['char'] not in idmap or idmap[r['char']] not in objs: W_('button %d refers to unsupported character %d - record dropped' % (i, r['char'])); continue
            m = r['matrix']; mult, add = r['cxform']
            recs += struct.pack('<BBH6f', r['state'] & 0xF, 0xCD, r['depth'], m[0], m[1], m[2], m[3], m[4] / 20.0, m[5] / 20.0)
            recs += bytes([clamp(round(v / 4), 0, 255) for v in mult] + [clamp(v, -128, 127) & 255 for v in add]) + struct.pack('<I', objs[idmap[r['char']]])
        nrec = len(recs) // 0x28
        ra = p.alloc(recs, 4); cb = b''
        for cond, act in bt['conds']:
            code_a = fix_actions(act, {}, 'button %d' % i)
            cb += struct.pack('<HHI', cond, 0, wr.code(bytearray(code_a)) if code_a else 0)
        ca = p.alloc(cb, 4) if cb else 0
        objs[idmap[i]] = p.alloc(struct.pack('<B3xIBBBxII', 3, VT[3], 0, nrec, len(bt['conds']), ra, ca) + b'\xCD' * 12, 16)
    root_tags = [(c, pl) for c, pl in tags if c in (26, 28, 12, 1, 4, 5, 43)]
    rframes, rlabels = swfr.parse_timeline_ex(root_tags, ver)
    bg = 0xFF000000 | 0xFFFFFF
    for c, pl in tags:
        if c == 9: bg = struct.unpack('<I', bytes(pl[:3]) + b'\xff')[0]
    fr = swf['frame']; sw, sh_ = (fr[1] - fr[0]) / 20.0, (fr[3] - fr[2]) / 20.0
    if fit is not None:
        tw, th = fit; sc = min(tw / sw, th / sh_); fit = (sc, round((tw * 20 - sw * 20 * sc) / 2 - fr[0] * sc), round((th * 20 - sh_ * 20 * sc) / 2 - fr[2] * sc)); sw, sh_ = tw, th
        log('fit %dx%d -> %dx%d: scale %.4f' % ((fr[1] - fr[0]) / 20, (fr[3] - fr[2]) / 20, sw, sh_, fit[0]))
    if no_preloader:
        if strip_preloader(rframes, 'root', log): need_root_stop[0] = True
        if need_root_stop[0] and rframes and not any(k == 'action' and d[:2] == b'\x07\x00' for k, d in rframes[0]):
            rframes[0].append(('action', b'\x07\0')); log('preloader: root timeline stopped on frame 0 (the removed loader did that; a start button now advances it)')
    rconv = conv_frames(rframes, rlabels, 'root', root=True)
    fa, nfr = wr.timeline(rconv, bg)
    # ---- finalize root
    maxid = max(list(objs) + [0]); ptrs = [0] * (maxid + 1)
    for i, a in objs.items(): ptrs[i] = a
    la = p.alloc(struct.pack('<%dI' % len(ptrs), *ptrs), 4)
    p.put(p.base + 8, '<H', nfr); p.put(p.base + 0xC, '<II', fa, la); p.d[0x14] = 6
    p.put(p.base + 0x18, '<4f', 0.0, float(sw), 0.0, float(sh_)); p.put(p.base + 0x28, '<HH', swf['rate'], len(ptrs))
    p.reload_root()
    return p.tobytes(), warn


def sheets_ok(p, a):
    from sheetfont import sheets_of
    return bool(sheets_of(p, a))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('swf'); ap.add_argument('pck'); ap.add_argument('--donor', required=True, help='any original game .pck (header + font/page templates)')
    ap.add_argument('--fit', default='', help='fit the stage into WxH (e.g. 640x480), scaling/centering root content; default: keep the SWF size')
    ap.add_argument('--drop-action', action='append', default=[], help='drop DoAction scripts containing this text (e.g. getBytesTotal for preloaders)')
    ap.add_argument('--omit', default='', help='comma list for bisecting a crash: text (no fonts/text/edit text), actions (no scripts)')
    ap.add_argument('--bitmap-budget', type=int, default=600000, help='total texture bytes for all bitmaps (images are downscaled to fit; the game heap is small)')
    ap.add_argument('--max-bitmap-dim', type=int, default=256, choices=[64, 128, 256])
    ap.add_argument('--text-shape-scale', type=float, default=1.75, help='static text whose placements scale it up more than this becomes vector shapes instead of (bitmap font) text')
    ap.add_argument('--jpeg-tables', help='SWF to take the shared JPEGTables from when the input lost them (e.g. the unedited original)')
    ap.add_argument('--tolerance', type=float, default=4.0, help='curve flattening tolerance in twips (20 twips = 1 px); smaller = rounder curves, bigger file')
    ap.add_argument('--simplify', type=float, default=0.0, help='Douglas-Peucker tolerance in twips applied to all shape edge chains (20 twips = 1 px; try 10-20); lower poly, much smaller pck, faster; 0 = off')
    ap.add_argument('--min-area', type=float, default=0.0, help='drop fill loops smaller than this many square pixels (specks in traced art; try 1-4)')
    ap.add_argument('--no-preloader', action='store_true', help='remove a getBytesLoaded/getBytesTotal preloader loop from the root timeline')
    ap.add_argument('--sizes', default='', help='extra font sheet sizes in twips for every font')
    a = ap.parse_args()
    tess.TOL = a.tolerance; tess.SIMPLIFY = a.simplify; tess.MIN_AREA = a.min_area * 400.0
    fit = tuple(int(x) for x in a.fit.lower().split('x')) if a.fit else None
    out, warn = build(open(a.swf, 'rb').read(), a.donor, fit, a.drop_action, [x for x in a.sizes.split(',') if x], no_preloader=a.no_preloader, omit=[x for x in a.omit.split(',') if x], bitmap_budget=a.bitmap_budget, max_bitmap_dim=a.max_bitmap_dim, text_shape_scale=a.text_shape_scale, jpeg_tables_from=a.jpeg_tables)
    open(a.pck, 'wb').write(out); print('wrote %s (%d bytes)' % (a.pck, len(out)))

if __name__ == '__main__': main()
