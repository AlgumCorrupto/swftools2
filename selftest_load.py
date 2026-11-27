#!/usr/bin/env python3
"""selftest_load.py loading_generic_a.pck [loading_network_s10.pck ...]
For every loading-screen pck: structure found; writing the pck's own colours back is a byte-exact no-op; recolouring and artwork replacement
(shared / separate palette, dithered) keep the file size, pass pckaudit, keep the original transparent corners, give no colour step at the
seam with a shared palette, and survive pck2swf -> swf2pck byte for byte.  NOT a test in the game."""
import sys, os, math, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from PIL import Image, ImageDraw
import pckload as L, pckaudit, pck2swf, swf2pck
from rpck import Pck


def make_art(path):
    im = Image.new('RGB', (1280, 720)); d = ImageDraw.Draw(im)
    for x in range(0, 1280, 4):
        for y in range(0, 720, 2): d.rectangle([x, y, x + 3, y + 1], fill=(int(255 * x / 1280), int(255 * y / 720), int(127 + 127 * math.sin((x + y) / 90))))
    d.ellipse([440, 160, 840, 560], outline=(255, 255, 255), width=12); im.save(path)


def main():
    if len(sys.argv) < 2: sys.exit(__doc__)
    tmp = tempfile.mkdtemp(prefix='load_selftest_'); art = os.path.join(tmp, 'art.png'); make_art(art); bad = 0
    quiet = lambda m: None
    def check(name, cond, extra=''):
        nonlocal bad
        print('  %-44s %s %s' % (name, 'ok' if cond else 'FAIL', extra)); bad += not cond
    for src in sys.argv[1:]:
        p0 = Pck(src); ls0 = L.find_loadscreen(p0); cols = L.read_colors(p0, ls0); print('==', os.path.basename(src)); print('  ' + L.summary(p0, ls0).replace('\n', '\n  '))
        check('structure found', bool(ls0['art'] and ls0['bar'] and ls0['text'] and ls0['tint']))
        out = os.path.join(tmp, 'same.pck'); L.edit(src, out, bar=cols['bar'], text=cols['text'], tint=cols['tint'], log=quiet)
        check('own colours written back = identical file', open(out, 'rb').read() == open(src, 'rb').read())
        out = os.path.join(tmp, 'accent.pck'); L.edit(src, out, accent='#20a0c0', log=quiet)
        p = Pck(out); c = L.read_colors(p, L.find_loadscreen(p)); _, errs = pckaudit.audit(out)
        check('accent recolour', c['bar'] == (32, 160, 192) and c['text'] == (32, 160, 192) and not errs, 'tint #%02x%02x%02x' % c['tint'])
        mask0 = np.asarray(L.decode_art(p0, ls0))[:, :, 3] < 64
        for pal, dith in (('shared', False), ('separate', False), ('shared', True)):
            out = os.path.join(tmp, 'art_%s_%s.pck' % (pal, dith)); r = L.edit(src, out, image=art, palette=pal, dither=dith, log=quiet)
            p = Pck(out); ls = L.find_loadscreen(p); im = L.decode_art(p, ls); a = np.asarray(im.convert('RGB'), float)
            _, errs = pckaudit.audit(out)
            seam = np.abs(a[:, 255] - a[:, 256]).mean(); nb = np.abs(a[:, 254] - a[:, 255]).mean()
            conv = pck2swf.Conv(p, True, pck2swf.GlyphSrc(None), None, {}); swf, _ = conv.convert(False, True, open(out, 'rb').read())
            rt, changed, _ = swf2pck.convert(swf, None, log=lambda m: None)
            ok = (r['delta'] == 0 and not errs and ((np.asarray(im)[:, :, 3] < 64) != mask0).mean() < 0.003 and rt == open(out, 'rb').read() and not changed
                  and (pal != 'shared' or dith or seam < max(4.0, 3 * nb)))
            check('art %s palette%s' % (pal, ' + dither' if dith else ''), ok, 'size %+d, audit %d, seam %.1f (neighbour %.1f)' % (r['delta'], len(errs), seam, nb))
        for img in (art, None):          # frame over a new artwork, and over the current artwork
            out = os.path.join(tmp, 'frame_%s.pck' % ('new' if img else 'cur')); r = L.edit(src, out, image=img, frame=L.DEFAULT_FRAME, log=quiet)
            p = Pck(out); ls = L.find_loadscreen(p); im = np.asarray(L.decode_art(p, ls).convert('RGB'), float); _, errs = pckaudit.audit(out)
            edge = im[60:180, 5:6].mean(axis=(0, 1)); inner = im[60:180, 40:44].mean(axis=(0, 1))     # the frame's left line is ~#757375 at x ~5
            check('frame over %s artwork' % ('new' if img else 'current'), r['delta'] == 0 and not errs and abs(edge - 112).max() < 40 and not (abs(inner - 112).max() < 8),
                  'size %+d, audit %d, edge %s' % (r['delta'], len(errs), edge.round()))
    print('FAILED' if bad else 'all OK'); sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
