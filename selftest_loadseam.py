"""selftest_loadseam: the loading-screen artwork follows the polygons' screen layout (no seam where the two halves overlap).

    python3 selftest_loadseam.py

Needs no sample pcks: the geometry helpers are checked on a synthetic picture, and replace_art / decode_art are smoke-tested on the
pck embedded in examples/legal_pck_fixed.swf (both halves point at its 128x128 bitmap).
"""
import sys, os, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import numpy as np
from PIL import Image
import pckload as L, pckaudit, selftest_bitmaps as T
from rpck import Pck


def picture(W=512, H=256):
    """detailed everywhere (diagonal stripes + rings), so a shifted strip is easy to see"""
    y, x = np.mgrid[0:H, 0:W]
    r = 128 + 100 * np.sin(x / 3.1 + y / 5.3); g = 128 + 100 * np.sin(x / 7.7 - y / 2.9); b = 128 + 100 * np.cos(np.hypot(x - 256, y - 128) / 4.0)
    return Image.fromarray(np.stack([r, g, b, np.full_like(r, 255)], -1).clip(0, 255).astype(np.uint8), 'RGBA')


def art_dict(overlap):
    """450x265 px on screen, two polygons that overlap by `overlap` px (or leave a gap when negative)"""
    sw, sh = 450.0, 265.0; mid = sw / 2
    return dict(size=(512, 256), tex=(256, 256), screen=(sw, sh), origin=(0.0, 0.0), aspect=sw / sh, unsupported=None, top='right',
                boxes=((0.0, mid + overlap / 2, 0.0, sh), (mid - overlap / 2, sw, 0.0, sh)), overlap=overlap)


def seam_error(img, ref, cols):
    a = np.asarray(img.convert('RGB'), float)[:, cols[0]:cols[1]]; b = np.asarray(ref.convert('RGB'), float)[:, cols[0]:cols[1]]
    return np.abs(a - b).mean()


def main():
    src = picture()
    # 1) exact tiling: the helpers are the identity (no resampling at all)
    a = art_dict(0.0); bx = L.half_boxes(a)
    tex = L.halves_to_textures(src, bx, a['tex']); back = L.compose_halves([tex.crop((0, 0, 256, 256)), tex.crop((256, 0, 512, 256))], bx, a['size'])
    assert tex.tobytes() == src.tobytes() and back.tobytes() == src.tobytes(), 'tiling polygons must not resample'
    print('exact tiling: identity OK')
    # 2) overlapping polygons: the new mapping reproduces the picture, a fixed cut at x=256 shows the seam
    for ov in (4.0, 2.0, -0.0):
        a = art_dict(ov); bx = L.half_boxes(a)
        tex = L.halves_to_textures(src, bx, a['tex']); tiles = [tex.crop((0, 0, 256, 256)), tex.crop((256, 0, 512, 256))]
        new = L.compose_halves(tiles, bx, a['size'])
        old_tiles = [src.crop((0, 0, 256, 256)), src.crop((256, 0, 512, 256))]; old = L.compose_halves(old_tiles, bx, a['size'])
        near = (236, 276)                                                   # around the join
        e_new, e_old = seam_error(new, src, near), seam_error(old, src, near)
        print('overlap %+.1f px: error around the join: new mapping %.2f, fixed cut at 256 %.2f' % (ov, e_new, e_old))
        assert e_new < 10, 'overlapping polygons: new mapping drifts (%.2f)' % e_new
        if ov >= 2: assert e_old > 2 * e_new + 5, 'the fixed cut should show a seam here (%.2f vs %.2f)' % (e_old, e_new)
    # 3) the whole pipeline on a real pck structure (replace_art -> decode_art), audit clean, size unchanged
    raw = T.embedded_pck(); p = Pck(raw); bm = p.ptrs[5]
    for ov in (0.0, 5.0, -3.0):
        a = dict(art_dict(ov), left=bm, right=bm, tex=(128, 128), size=(256, 128)); ls = dict(art=a)
        p = Pck(raw)
        for fit in ('cover', 'stretch', 'contain'):
            out = L.replace_art(p, ls, picture(300, 200), fit, keep_mask=False, log=lambda m: None)
            assert out.size == (256, 128) and L.decode_art(p, ls).size == (256, 128)
        with tempfile.TemporaryDirectory() as tmp:
            f = os.path.join(tmp, 'x.pck'); open(f, 'wb').write(p.tobytes()); assert len(p.tobytes()) == len(raw) and not pckaudit.audit(f)[1]
    print('replace_art / decode_art on a pck: OK')
    # 4) the summary tells you about an overlap
    txt = L.summary(p, dict(art=art_dict(4.0), bar=None, text=None, tint=None), dict(bar=None, text=None, tint=None))
    assert 'overlap by 4.0 px' in txt and 'gap' in L.summary(p, dict(art=art_dict(-3.0), bar=None, text=None, tint=None), dict(bar=None, text=None, tint=None))
    print('ALL OK')


if __name__ == '__main__':
    main()
