#!/usr/bin/env python3
"""selftest_anim.py bg_arcade.pck [bg_options.pck ...]
Replaces the background animation of each pck with a synthetic GIF in several modes (same length, GIF timing, longer, shorter, opacity)
and checks: pckaudit is clean, the animation reads back with the expected frame counts, the decoded frames match the source
(quantisation only), and pck2swf -> swf2pck reproduces the written pck byte for byte.  NOT a test in the game."""
import sys, os, math, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from PIL import Image, ImageDraw
import pckanim as A, pckaudit, pck2swf, swf2pck
from rpck import Pck


def make_gif(path, n=30, ms=100, size=(320, 180)):
    fr = []
    for k in range(n):
        im = Image.new('RGB', size, (10, 10, 40)); d = ImageDraw.Draw(im)
        for x in range(0, size[0], 8):
            c = int(127 + 120 * math.sin((x + k * 10) / 25)); d.rectangle([x, 0, x + 7, size[1]], fill=(c, 255 - c, 128))
        cx = int(30 + (size[0] - 60) * k / (n - 1)); d.ellipse([cx - 20, 70, cx + 20, 110], fill=(255, 255, 255))
        fr.append(im)
    fr[0].save(path, save_all=True, append_images=fr[1:], duration=ms, loop=0)


def psnr(a, b):
    a = np.asarray(a.convert('RGB'), float); b = np.asarray(b.convert('RGB'), float); m = ((a - b) ** 2).mean()
    return 99.0 if m == 0 else 10 * math.log10(255 ** 2 / m)


def main():
    if len(sys.argv) < 2: sys.exit(__doc__)
    tmp = tempfile.mkdtemp(prefix='anim_selftest_'); gif = os.path.join(tmp, 'test.gif'); make_gif(gif); bad = 0
    for src in sys.argv[1:]:
        p0 = Pck(src); a0 = A.pick(A.find_animations(p0)); old = a0['nframes']
        print('==', os.path.basename(src), A.describe(a0))
        for name, kw, want in (('same', dict(length='original'), old), ('timing', dict(length='timing', opacity=1.0), 45),
                               ('long', dict(length=old + 40), old + 40), ('short', dict(length=12, fit='stretch'), 12)):
            out = os.path.join(tmp, name + '.pck'); r = A.replace(src, out, a0['sprite'], gif, log=lambda m: None, **kw)
            _, errs = pckaudit.audit(out); p = Pck(out); a = A.pick(A.find_animations(p), a0['sprite'])
            fr = A.decode_frames(p, a); ref, durs = A.load_source(gif, a['tex'], a['aspect'], kw.get('fit', 'cover'), 15)
            ps = min(psnr(f, w) for f, w in zip(fr, A.resample(ref, durs, want)))
            conv = pck2swf.Conv(p, True, pck2swf.GlyphSrc(None), None, {}); swf, _ = conv.convert(False, True, open(out, 'rb').read())
            rt, changed, _ = swf2pck.convert(swf, None, log=lambda m: None)
            ok = (not errs and a['nframes'] == want and ps > 30 and rt == open(out, 'rb').read() and not changed)
            print('  %-7s frames %3d  size %8d (%+d)  audit problems %d  min PSNR %.1f dB  swf round trip %s  %s' % (
                name, a['nframes'], r['size'], r['delta'], len(errs), ps, 'identical' if rt == open(out, 'rb').read() else 'DIFFERENT', 'OK' if ok else 'FAIL'))
            bad += not ok
    print('FAILED' if bad else 'all OK'); sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
