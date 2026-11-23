#!/usr/bin/env python3
"""pckfont: replace the bitmap font sheets of a swf .pck font with glyphs rasterised from a TTF/OTF.

For each swfFONT object you name, every existing sheet (one per text size) is regenerated at the same pixel size
(textSize_twips / 20) - plus any extra sizes you ask for - packed into 128x128 4bpp PS2 pages with the font's own page template,
and appended to the pck (append-only; old data stays, pointers are updated).  The advance table (+0x14), the ascent fields
(+0x9A/+0x9C) and the advances/centering of static text that uses the font are updated as well.

    python3 pckfont.py in.pck out.pck --font 30=MyFont.ttf [--font 33=Other.ttf] [--add-size 1000] [--anchor auto]
    python3 pckfont.py in.pck --info
"""
import sys, struct, argparse, os
from rpck import *
from sheetfont import sheets_of, SheetGlyphs

PAGE_W = PAGE_H = 128
TEX_SZ, PIX_SZ, GAP, INFO_SZ, PAL_SZ = 0x60, 0x2000, 0x30, 0xA0, 0x50


def load_font(path, px):
    from PIL import ImageFont
    try: return ImageFont.truetype(path, float(px))
    except Exception: return ImageFont.truetype(path, max(1, int(round(px))))


class Raster:
    """glyph bitmaps + metrics (1/1024 em units) for one TTF at one pixel size.
    scale shrinks/grows the glyphs relative to the nominal em (the sheet size): bitmaps are drawn 1:1, so metrics are the
    scaled pixel metrics converted with the NOMINAL px/unit factor."""
    def __init__(s, path, size_twips, scale=1.0):
        s.px = size_twips / 20.0; s.scale = scale; s.font = load_font(path, s.px * scale); s.U = 1024.0 / s.px
        s.ref = load_font(path, 1024)

    def advance(s, ch):          # em units, size independent
        return int(round(s.ref.getlength(ch) * s.scale))

    def glyph(s, code):
        from PIL import Image, ImageDraw
        ch = chr(code)
        x0, y0, x1, y1 = s.font.getbbox(ch, anchor='ls')
        w, h = x1 - x0, y1 - y0
        adv = int(round(s.font.getlength(ch) * s.U))
        if w <= 0 or h <= 0 or code in (32, 0xA0): return None, (0, 0, 0, 0, adv)
        im = Image.new('L', (w, h), 0); ImageDraw.Draw(im).text((-x0, -y0), ch, font=s.font, fill=255, anchor='ls')
        left, top = int(round(x0 * s.U)), int(round(y0 * s.U))
        return im, (left, top, adv, int(round((y0 + h) * s.U)), adv)


def pack(glyph_imgs):
    """shelf-pack; -> list of (page, x, y) per glyph (None for empty) and page count"""
    pos = []; page = 0; x = y = 0; rowh = 0
    for im in glyph_imgs:
        if im is None: pos.append(None); continue
        w, h = im.size
        if w > PAGE_W or h > PAGE_H: raise ValueError('glyph %dx%d does not fit a 128x128 page (size too large for this font)' % (w, h))
        if x + w > PAGE_W: x = 0; y += rowh + 1; rowh = 0
        if y + h > PAGE_H: page += 1; x = y = 0; rowh = 0
        pos.append((page, x, y)); x += w + 1; rowh = max(rowh, h)
    return pos, page + 1


def template(p):
    """a page template (tex, info, pal bytes, palette alpha ramp) from any font sheet in the file"""
    for i, a in enumerate(p.ptrs):
        if a and p.u8(a) == 5:
            sh = sheets_of(p, a)
            if sh:
                A = p.u32(sh[0]['addr']); info = p.u32(A); tex = p.u32(info + 0x94); pal = p.u32(info + 0x90)
                return dict(tex=p.raw(tex, TEX_SZ), info=p.raw(info, INFO_SZ), pal=p.raw(pal, PAL_SZ), next0=p.u32(tex + 4),
                            alpha=[p.raw(pal + 0x10 + 4 * q, 4)[3] for q in range(16)])
    raise SystemExit('this pck has no font sheets to use as a page template')


def build_sheet(p, tpl, rast, chars):
    n = len(chars)
    imgs = []; metrics = []
    for c in chars:
        im, m = rast.glyph(c); imgs.append(im); metrics.append(m)
    pos, npages = pack(imgs)
    # alpha -> 4bit index through the template palette (ramp 0..0x80)
    pages = [bytearray(PAGE_W * PAGE_H) for _ in range(npages)]
    for im, ps in zip(imgs, pos):
        if im is None: continue
        pg, gx, gy = ps; w, h = im.size; data = im.tobytes()
        for yy in range(h):
            row = (gy + yy) * PAGE_W + gx
            for xx in range(w): pages[pg][row + xx] = min(15, (data[yy * w + xx] * 15 + 127) // 255)
    # records + page index array
    rec = b''; pidx = bytearray()
    for im, ps, m in zip(imgs, pos, metrics):
        left, top, adv, bottom, _ = m
        if im is None: rec += struct.pack('<BBBBhhhh', 0, 0, 0, 0, 0, 0, adv, 0); pidx.append(0)
        else:
            rec += struct.pack('<BBBBhhhh', ps[1], ps[2], im.size[0], im.size[1], left, top, adv, bottom); pidx.append(ps[0])
    Baddr = p.alloc(rec, 4)
    Caddr = p.alloc(bytes(pidx), 4) if npages > 1 else 0
    Aaddr = p.alloc(b'\0' * (4 * npages), 4)
    prev_tex = tpl['next0']
    for pg in range(npages):
        T = p.alloc(b'\xCD' * (TEX_SZ + PIX_SZ + GAP + INFO_SZ + PAL_SZ), 16)
        pix = T + TEX_SZ; info = pix + PIX_SZ + GAP; pal = info + INFO_SZ
        tex = bytearray(tpl['tex']); struct.pack_into('<II', tex, 4, prev_tex, pix)
        inf = bytearray(tpl['info']); struct.pack_into('<II', inf, 0x90, pal, T)
        p.d[T - p.base:T - p.base + TEX_SZ] = tex
        p.d[pix - p.base:pix - p.base + PIX_SZ] = swizzle4(bytes(pages[pg]), PAGE_W, PAGE_H)
        p.d[info - p.base:info - p.base + INFO_SZ] = inf
        p.d[pal - p.base:pal - p.base + PAL_SZ] = tpl['pal']
        p.put(Aaddr + 4 * pg, '<I', info); prev_tex = T
    tpl['next0'] = prev_tex
    sheet = p.alloc(struct.pack('<IIhHIi', Aaddr, Baddr, 0, n, Caddr, npages) + b'\xCD' * 12, 16)
    return sheet, npages, imgs


def auto_scale(p, a, ttf, chars, old_sheets, log):
    """scale so the new font's cap height (H, else 0) matches the old sheet's at the same size"""
    if not old_sheets: return 1.0
    sg = SheetGlyphs(p, a, old_sheets[0])
    for ref in ('H', '0', 'A', 'x'):
        c = ord(ref)
        if c in sg.map and sg.recs[sg.map[c]][3]:
            old_h = sg.recs[sg.map[c]][3]; nominal = sg.size / 20.0
            f = load_font(ttf, nominal); x0, y0, x1, y1 = f.getbbox(ref, anchor='ls'); new_h = y1 - y0
            if new_h > 0: sc = old_h / float(new_h); log('  auto scale %.3f (old %r height %dpx vs new %dpx at %.0fpx em)' % (sc, ref, old_h, new_h, nominal)); return sc
    return 1.0


def import_font(p, fi, ttf, extra_sizes=(), anchor='auto', retext=True, log=print, scale=None):
    a = p.ptrs[fi]
    if not a or p.u8(a) != 5: raise SystemExit('object %d is not a font' % fi)
    chars = p.read_font(a)['chars']; n = len(chars); tpl = template(p)
    old = sheets_of(p, a); sizes = [s['size'] for s in old] + [int(x) for x in extra_sizes if int(x) not in [s['size'] for s in old]]
    if not sizes: raise SystemExit('font %d has no sheets; pass --add-size' % fi)
    if len(sizes) > 12: raise SystemExit('a font holds at most 12 sheets')
    if scale is None: scale = auto_scale(p, a, ttf, chars, old[:1] and [old[0]] or [], log)
    new_ptrs = []
    for sz in sizes:
        r = Raster(ttf, sz, scale); sheet, npg, _ = build_sheet(p, tpl, r, chars)
        p.put(sheet + 8, '<hH', sz, n); new_ptrs.append(sheet)
        log('  font %d: %.1fpx sheet -> %d page(s)' % (fi, sz / 20.0, npg))
    for k, sp in enumerate(new_ptrs): p.put(a + 0xA0 + 4 * k, '<I', sp)
    p.put(a + 0x98, '<h', len(new_ptrs))
    # advances (1/1024 em) used by the engine's edit text
    ref = Raster(ttf, sizes[0], scale); adv = [ref.advance(chr(c)) for c in chars]
    p2 = p.u32(a + 0x14)
    if p.inside(p2): struct.pack_into('<%dH' % n, p.d, p2 - p.base, *[min(65535, max(0, v)) for v in adv])
    # ascent / line extra
    asc0, ext0 = struct.unpack_from('<hh', p.d, a + 0x9A - p.base)
    if 0 < asc0 <= 4096:
        asc, desc = ref.ref.getmetrics(); asc, desc = int(asc * scale), int(desc * scale); struct.pack_into('<hh', p.d, a + 0x9A - p.base, int(asc), int(desc))
    if retext: retext_font(p, fi, chars, adv, anchor, log)
    return sizes


def retext_font(p, fi, chars, adv, anchor, log):
    """static TEXT objects store per-glyph advances (twips) baked from the old font: recompute them, keep line anchors"""
    changed = 0
    for oi, oa in enumerate(p.ptrs):
        if not oa or p.u8(oa) != 6: continue
        q = p.u32(oa + 0x20); font = size = None; lines = []; cur = dict(xa=None, x=0)
        while True:
            t = p.u16(q)
            if t == 0: break
            if t == 1: font, size = p.u16(q + 2), p.u16(q + 4); q += 6
            elif t == 2: q += 6
            elif t == 3: cur['y'] = p.i16(q + 2); q += 4
            elif t == 4: cur['xa'] = q + 2; cur['x'] = p.i16(q + 2); q += 4
            else:
                n = p.u16(q + 2)
                if font == fi and size:
                    ents = [(q + 4 + 4 * k, p.u16(q + 4 + 4 * k), p.u16(q + 6 + 4 * k)) for k in range(n)]
                    lines.append(dict(xa=cur['xa'], x=cur['x'], size=size, ents=ents))
                q += 4 + 4 * n
        if not lines: continue
        for ln in lines:
            ln['oldW'] = sum(e[2] for e in ln['ents']); ln['newW'] = sum(int(round(adv[e[1]] * ln['size'] / 1024.0)) if e[1] < len(adv) else e[2] for e in ln['ents'])
        mode = anchor
        if mode == 'auto':
            if len(lines) < 2: mode = 'left'
            else:
                import statistics
                sp = {'left': statistics.pstdev([l['x'] for l in lines]), 'center': statistics.pstdev([l['x'] + l['oldW'] / 2 for l in lines]), 'right': statistics.pstdev([l['x'] + l['oldW'] for l in lines])}
                mode = min(sp, key=lambda k: (sp[k], k != 'left'))
        for ln in lines:
            for ea, gi, ad in ln['ents']:
                if gi < len(adv): p.put(ea + 2, '<H', min(65535, max(0, int(round(adv[gi] * ln['size'] / 1024.0)))))
            if ln['xa'] is not None:
                d = {'left': 0, 'center': (ln['oldW'] - ln['newW']) / 2.0, 'right': ln['oldW'] - ln['newW']}[mode]
                p.put(ln['xa'], '<h', max(-32768, min(32767, int(round(ln['x'] + d)))))
        changed += 1
    if changed: log('  font %d: re-laid-out %d static text object(s)' % (fi, changed))


def finish(p):
    p.reload_root()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pck'); ap.add_argument('out', nargs='?')
    ap.add_argument('--font', action='append', default=[], help='INDEX=font.ttf[@scale] (repeatable); INDEX is the pck font object index; scale 1.0 = em equals the text size, default: auto-match the old cap height')
    ap.add_argument('--add-size', default='', help='extra sheet sizes in twips (comma separated), e.g. 800,1200')
    ap.add_argument('--anchor', default='auto', choices=['auto', 'left', 'center', 'right'], help='how static text lines keep their position when widths change')
    ap.add_argument('--no-retext', action='store_true', help='do not touch static text advances')
    ap.add_argument('--info', action='store_true', help='list fonts and their sheets and exit')
    ap.add_argument('--export-png', help='write every sheet page of the (new) fonts to this directory')
    a = ap.parse_args()
    p = Pck(a.pck)
    if a.info:
        for i, o in enumerate(p.ptrs):
            if o and p.u8(o) == 5: print('font', i, 'glyphs', p.u16(o + 8), 'sheets (px):', [s['size'] / 20 for s in sheets_of(p, o)])
        return
    if not a.out or not a.font: ap.error('need out.pck and at least one --font INDEX=file.ttf')
    extra = [x for x in a.add_size.split(',') if x]
    for spec in a.font:
        idx, path = spec.split('=', 1); sc = None
        if '@' in path: path, sc = path.rsplit('@', 1); sc = float(sc)
        print('font', idx, '<-', path); import_font(p, int(idx), path, extra, a.anchor, not a.no_retext, scale=sc)
    open(a.out, 'wb').write(p.tobytes()); print('wrote', a.out, '(%d bytes)' % (len(p.d) + 0x80))
    if a.export_png:
        from PIL import Image
        os.makedirs(a.export_png, exist_ok=True)
        q = Pck(a.out)
        for spec in a.font:
            fi = int(spec.split('=')[0]); sg = SheetGlyphs(q, q.ptrs[fi])
            for k, (w, h, al) in enumerate(sg.pages):
                Image.frombytes('L', (w, h), al).save(os.path.join(a.export_png, 'font%d_page%d.png' % (fi, k)))
        print('exported pages to', a.export_png)

if __name__ == '__main__': main()
