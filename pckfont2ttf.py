#!/usr/bin/env python3
"""pckfont2ttf: export the bitmap font of a swf .pck as a TrueType font (installable / usable in Macromedia Flash).

    python3 pckfont2ttf.py legal.pck out_dir                # every font object in the pck
    python3 pckfont2ttf.py legal.pck out_dir --font 9       # one font object
    python3 pckfont2ttf.py legal.pck --info

The pck stores each font as bitmap sheets (one per text size) plus a character table and glyph metrics. The pixels of the LARGEST sheet are
traced into vector outlines (pixel edges merged into contours), the advance widths come from the font's own table (else from the sheet
records) and the ascent/descent from the font object. Units per em = 1024 = the engine's own glyph units, so metrics are exact.
The result looks like the bitmap font scaled up: crisp at the sheet size or integer multiples of it, stair-stepped when scaled a lot."""
import sys, os, struct, argparse
from rpck import *
from sheetfont import SheetGlyphs, sheets_of


def glyph_contours(sg, code):
    """-> (list of contours [(x,y)...] in y-UP em units with TrueType winding, advance)"""
    segs, adv = sg.glyph(code)
    contours = []; cur = []
    for s in segs:
        if s[0] == 'M':
            if len(cur) >= 3: contours.append(cur)
            cur = [(s[1], -s[2])]
        else: cur.append((s[1], -s[2]))
    if len(cur) >= 3: contours.append(cur)
    out = []
    for c in contours:
        if c[-1] == c[0]: c = c[:-1]
        if len(c) >= 3: out.append(c)
    def area(c): return sum(c[i][0] * c[(i + 1) % len(c)][1] - c[(i + 1) % len(c)][0] * c[i][1] for i in range(len(c))) / 2.0
    if out:
        big = max(out, key=lambda c: abs(area(c)))
        if area(big) > 0: out = [c[::-1] for c in out]       # TrueType: outer contours clockwise (negative area) in y-up
    return out, adv


def text_advances(p, fi):
    """glyph index -> median advance (1/1024 em) used by the pck's static text for font fi (the engine takes spaces etc. from there)"""
    import statistics
    seen = {}
    for oi, oa in enumerate(p.ptrs):
        if not oa or p.u8(oa) != 6: continue
        font = size = None
        for r in p.read_text(oa)['recs']:
            if r[0] == 'font': font, size = r[1], r[2]
            elif r[0] == 'glyphs' and font == fi and size:
                for gi, adv in r[1]:
                    if adv: seen.setdefault(gi, []).append(adv * 1024.0 / size)
    return {gi: int(round(statistics.median(v))) for gi, v in seen.items()}


def build_ttf(p, fi, path, family=None, style='Regular', log=print):
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    a = p.ptrs[fi]; chars = p.read_font(a)['chars']; n = len(chars)
    shs = sheets_of(p, a)
    if not shs: raise SystemExit('font %d has no bitmap sheets (nothing to trace)' % fi)
    sg = SheetGlyphs(p, a, max(shs, key=lambda s: s['size']))
    p2 = p.u32(a + 0x14); adv_tab = list(struct.unpack_from('<%dH' % n, p.d, p2 - p.base)) if p.inside(p2) else None
    tadv = text_advances(p, fi)
    asc0, ext0 = struct.unpack_from('<hh', p.d, a + 0x9A - p.base)
    tops = [-r[5] for r in sg.recs if r[2] and r[3]]; bots = [r[7] for r in sg.recs if r[2] and r[3]]
    asc = asc0 if 0 < asc0 <= 4096 else (max(tops) if tops else 900)
    desc = max(ext0, max(bots) if bots else 0) if 0 <= ext0 <= 4096 else (max(bots) if bots else 250)
    desc = max(desc, 1)
    order = ['.notdef']; names = {}; cmap = {}; glyphs = {}; metrics = {}
    pen = TTGlyphPen(None); glyphs['.notdef'] = pen.glyph(); metrics['.notdef'] = (int(0.6 * 1024), 0)
    for gi, code in enumerate(chars):
        if code in cmap: continue
        name = 'uni%04X' % code if code > 0x7F or code < 0x21 else {0x21: 'exclam'}.get(code, 'g%d' % code)
        name = 'uni%04X' % code
        contours, adv = glyph_contours(sg, code) if sg.has(code) else ([], 0)
        if adv_tab and gi < len(adv_tab) and adv_tab[gi]: adv = adv_tab[gi]
        elif not adv and gi in tadv: adv = tadv[gi]                       # space etc.: width only exists in the text records
        if not adv and code in (32, 0xA0): adv = int(0.3 * 1024)
        pen = TTGlyphPen(None)
        for c in contours:
            pen.moveTo(c[0])
            for pt in c[1:]: pen.lineTo(pt)
            pen.closePath()
        g = pen.glyph(); glyphs[name] = g
        xmin = min((x for c in contours for x, y in c), default=0)
        metrics[name] = (max(0, int(adv)), int(xmin)); cmap[code] = name; order.append(name)
    fam = family or 'MC3 Font %d' % fi
    fb = FontBuilder(1024, isTTF=True)
    fb.setupGlyphOrder(order); fb.setupCharacterMap(cmap)
    fb.setupGlyf(glyphs); fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=int(asc), descent=-int(desc))
    fb.setupNameTable({'familyName': fam, 'styleName': style, 'psName': (fam + '-' + style).replace(' ', ''), 'uniqueFontIdentifier': fam + ' ' + style + ' (traced from pck bitmap sheet)'})
    fb.setupOS2(sTypoAscender=int(asc), sTypoDescender=-int(desc), usWinAscent=int(asc), usWinDescent=int(desc), sxHeight=int(asc * 0.5), sCapHeight=int(asc * 0.7), fsType=0)
    fb.setupPost(); fb.setupDummyDSIG() if False else None
    fb.save(path)
    log('font %d -> %s  (%d glyphs, sheet %.0fpx, ascent %d descent %d)' % (fi, path, len(order), sg.size / 20.0, asc, desc))
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pck'); ap.add_argument('outdir', nargs='?', default='.')
    ap.add_argument('--font', type=int, action='append', help='font object index (default: all fonts)')
    ap.add_argument('--family', help='font family name (default: "<pck name> Font <index>")')
    ap.add_argument('--info', action='store_true')
    a = ap.parse_args(); p = Pck(open(a.pck, 'rb').read()); base = os.path.splitext(os.path.basename(a.pck))[0]
    fonts = [i for i, o in enumerate(p.ptrs) if o and p.u8(o) == 5]
    if a.info:
        for i in fonts: print('font', i, 'glyphs', p.u16(p.ptrs[i] + 8), 'sheets (px):', [s['size'] / 20 for s in sheets_of(p, p.ptrs[i])])
        return
    os.makedirs(a.outdir, exist_ok=True)
    for i in (a.font or fonts):
        fam = a.family or '%s Font %d' % (base, i)
        build_ttf(p, i, os.path.join(a.outdir, '%s_font%d.ttf' % (base, i)), fam)

if __name__ == '__main__': main()
