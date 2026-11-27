#!/usr/bin/env python3
"""pckbitmaps: view, export and replace the bitmap objects (type 4) of a .pck.

    python3 pckbitmaps.py X.pck list
    python3 pckbitmaps.py X.pck export INDEX out.png [--raw] [--aspect auto|source]
    python3 pckbitmaps.py X.pck export-all FOLDER [--raw] [--aspect auto|source]
    python3 pckbitmaps.py X.pck replace INDEX my.png out.pck [--fit cover|contain|stretch] [--dither] [--aspect auto|source]
        (out.pck may be X.pck itself: the original is then kept as X.pck.old)

Facts this relies on (see NOTES.md, "Texture convention"):
* A bitmap is a palettised PS2 texture (8bpp swizzled/linear, or 4bpp) whose size is a power of two.
* The object also stores 1/sourceSize (+0x10, two floats).  When the original image was not a power of two, the WHOLE
  image was stretched over the WHOLE texture (425x125 logo -> 512x128 texture).  The *true* aspect ratio of a bitmap is
  therefore the source size, not the texture size, and that is what the viewer shows / what export restores.
* The aspect a bitmap is DRAWN with can differ from its source size (a 64x64 animation frame stretched over the screen, a half of a
  stitched artwork ...).  The shape that uses the bitmap says how: its bitmap fill matrix maps the unit UV square back to a
  parallelogram in shape space, and every placement matrix up the sprite chain scales/rotates that again.  `screen_aspect` is
  that final width/height.  It is the reference whenever it is known and unambiguous (all uses of the bitmap agree within 3%);
  otherwise the stored source size is used (`--aspect source` forces that).
* Replacement patches pixels + palette in place (rpck.bitmap_encode_into), so the file size never changes and the
  texture size / source size stay as they were.  A new image is fitted to the source aspect ratio, then stretched over the
  texture exactly like the originals.
"""
import os, sys, struct, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rpck
from rpck import Pck

try:
    import numpy as np
except ImportError:                                   # optional: only makes decoding of big pcks faster
    np = None

FITS = ('cover', 'contain', 'stretch')
ASPECTS = ('auto', 'source')                       # reference aspect: as drawn in the game when known (auto) / stored source size
AGREE = 1.03                                      # all uses of one bitmap must agree within 3% for the drawn aspect to be trusted
_PERM = {}


# ------------------------------------------------------------------------------------------------ fast decoding
def _unswizzle8_fast(src, w, h):
    """numpy version of rpck.unswizzle8 (same formula, vectorised, permutation cached per size)."""
    perm = _PERM.get((w, h))
    if perm is None:
        y, x = np.mgrid[0:h, 0:w]
        bl = (y & ~0xF) * w + (x & ~0xF) * 2
        sw = (((y + 2) >> 2) & 1) * 4
        py = (((y & ~3) >> 1) + (y & 1)) & 7
        cl = py * w * 2 + ((x + sw) & 7) * 4
        bn = ((y >> 1) & 1) + ((x >> 2) & 2)
        perm = _PERM[(w, h)] = (bl + cl + bn).reshape(-1)
    return np.frombuffer(src, dtype=np.uint8)[perm].tobytes()


def _decode_indices(p, a):
    """-> (w, h, indices (1 byte per pixel, row-major), palette [(r, g, b, ps2 alpha)], ttype); same result as rpck.bitmap_decode."""
    b = p.bitmap_parts(a); w, h = b['tw'], b['th']
    if w <= 0 or h <= 0 or w > 4096 or h > 4096:
        raise ValueError('implausible texture size %dx%d' % (w, h))
    pal = [tuple(p.raw(b['pal'] + 0x10 + 4 * k, 4)) for k in range(b['ncol'])]
    if b['ttype'] == 6:
        n = w * h; src = p.raw(b['pix'], (n + 1) // 2)
        if np is not None:
            arr = np.frombuffer(src, dtype=np.uint8)
            idx = np.stack([arr & 15, arr >> 4], axis=1).reshape(-1)[:n].tobytes()
        else:
            ib = bytearray(n)
            for i in range(n): ib[i] = (src[i >> 1] >> (4 * (i & 1))) & 15
            idx = bytes(ib)
    elif b['ttype'] in (1, 5):
        src = p.raw(b['pix'], w * h)
        if b['ttype'] == 1: idx = _unswizzle8_fast(src, w, h) if np is not None else rpck.unswizzle8(src, w, h)
        else: idx = src
        pal = rpck.unswizzle_palette(pal)
    else:
        raise ValueError('unsupported texture type %d' % b['ttype'])
    return w, h, idx, pal, b['ttype']


def _render(idx, pal, w, h):
    """indices + palette (ps2 alpha 0..128, 128 = opaque) -> RGBA PIL image"""
    from PIL import Image
    pal = list(pal) + [(0, 0, 0, 0)] * (256 - len(pal))
    im = Image.frombytes('P', (w, h), bytes(idx))
    flat = []
    for r, g, bl, _ in pal[:256]: flat += [r, g, bl]
    im.putpalette(flat)
    rgb = im.convert('RGB')
    a = Image.frombytes('L', (w, h), bytes(idx)).point([min(255, c[3] * 2) for c in pal[:256]])
    rgb.putalpha(a)
    return rgb


# ------------------------------------------------------------------------------------------------ fitting / quantising
def fit_image(im, size, aspect, fit):
    """RGBA image -> RGBA `size` (texture sized) image.  The game stretches the texture over an area of aspect `aspect`
    (= source width / height), so 'cover' crops the image to that aspect first, 'contain' pads it with TRANSPARENT bars,
    'stretch' squeezes the whole image in (distorts when the aspects differ)."""
    from PIL import Image
    im = im.convert('RGBA'); w, h = im.size
    if fit == 'stretch' or aspect <= 0 or abs(w / h - aspect) < 0.5 / max(w, h):
        return im.resize(size, Image.LANCZOS)
    if fit == 'cover':
        if w / h > aspect: nw, nh = max(1, round(h * aspect)), h
        else: nw, nh = w, max(1, round(w / aspect))
        x0, y0 = (w - nw) // 2, (h - nh) // 2
        return im.crop((x0, y0, x0 + nw, y0 + nh)).resize(size, Image.LANCZOS)
    if fit == 'contain':
        if w / h > aspect: nw, nh = w, max(1, round(w / aspect))
        else: nw, nh = max(1, round(h * aspect)), h
        canvas = Image.new('RGBA', (nw, nh), (0, 0, 0, 0)); canvas.paste(im, ((nw - w) // 2, (nh - h) // 2))
        return canvas.resize(size, Image.LANCZOS)
    raise ValueError("fit must be one of %s" % ', '.join(FITS))


def quantize(im, ncol, dither=False):
    """RGBA texture image -> (indices, palette of ncol (r, g, b, ps2 alpha))"""
    from PIL import Image
    a = im.getchannel('A')
    if a.getextrema()[0] < 250:                       # transparency: the octree quantiser keeps the alpha channel
        q = im.quantize(colors=ncol, method=Image.Quantize.FASTOCTREE)
        pal = q.getpalette()[:3 * ncol]; pal += [0] * (3 * ncol - len(pal)); idx = q.tobytes(); acc = {}
        for i, av in zip(idx, a.tobytes()): acc.setdefault(i, []).append(av)
        cols = []
        for i in range(ncol):
            v = acc.get(i); al = round(sum(v) / len(v)) if v else 0
            cols.append((pal[3 * i], pal[3 * i + 1], pal[3 * i + 2], min(128, round(al / 2))))
        return bytes(min(i, ncol - 1) for i in idx), cols
    rgb = im.convert('RGB')
    q = rgb.quantize(colors=ncol, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    pal = q.getpalette()[:3 * ncol]; pal += [0] * (3 * ncol - len(pal)); idx = q.tobytes()
    if dither:                                        # Pillow ignores `dither` unless a palette is supplied -> second pass
        padded = pal + pal[3 * (ncol - 1):3 * ncol] * (256 - ncol)
        pimg = Image.new('P', (1, 1)); pimg.putpalette(padded)
        idx = bytes(min(i, ncol - 1) for i in rgb.quantize(palette=pimg, dither=Image.Dither.FLOYDSTEINBERG).tobytes())
    return bytes(idx), [(pal[3 * i], pal[3 * i + 1], pal[3 * i + 2], 128) for i in range(ncol)]


def load_image(src):
    """path / PIL image -> RGBA PIL image (first frame of animated files)"""
    from PIL import Image
    if isinstance(src, Image.Image): return src.convert('RGBA')
    from PIL import ImageOps
    with Image.open(src) as im:
        if getattr(im, 'is_animated', False): im.seek(0)
        try: im = ImageOps.exif_transpose(im)         # phone photos: honour the rotation flag
        except Exception: pass
        return im.convert('RGBA')


def _lin(m):
    """packed placement matrix (sx, sy, rs0, rs1, tx, ty; 8.8) -> linear part (a, b, c, d) with x' = a x + c y, y' = b x + d y"""
    if not m: return (1.0, 0.0, 0.0, 1.0)
    return (m[0] / 256.0, m[2] / 256.0, m[3] / 256.0, m[1] / 256.0)


def _mul(m2, m1):
    """linear part m1 applied first, then m2"""
    a2, b2, c2, d2 = m2; a1, b1, c1, d1 = m1
    return (a2 * a1 + c2 * b1, b2 * a1 + d2 * b1, a2 * c1 + c2 * d1, b2 * c1 + d2 * d1)


def _apply(m, v):
    return (m[0] * v[0] + m[2] * v[1], m[1] * v[0] + m[3] * v[1])


def _display_size(d, aspect):
    """size of the picture shown for a bitmap whose reference aspect is `aspect`: the stored source size when the aspect is
    that of the source, else the smallest size of that aspect that does not throw texture detail away"""
    if aspect <= 0 or abs(aspect / d['aspect'] - 1) < 0.01: return d['src_w'], d['src_h']
    tw, th = d['tex_w'], d['tex_h']
    a, b = (tw, max(1, round(tw / aspect))), (max(1, round(th * aspect)), th)
    return a if a[0] * a[1] >= b[0] * b[1] else b


# ------------------------------------------------------------------------------------------------ the bitmap set
class BitmapSet:
    """All bitmaps of one pck, with in-memory replacement (nothing touches the file until save())."""

    def __init__(self, src):
        self.p = Pck(src)
        self.path = src if isinstance(src, str) else None
        self.indices = [i for i in range(1, self.p.count) if self.p.ptrs[i] and self.p.otype(i) == 4]
        self.snaps = {}                               # object index -> (pixel bytes, palette bytes) before the first replacement
        self._geo = None                              # bitmap index -> [{'aspect', 'placed', 'shape'}], built on first use
        users = {}
        for i in self.indices:
            try: users.setdefault(self.p.bitmap_parts(self.p.ptrs[i])['pix'], []).append(i)
            except Exception: pass
        self._shared = {i: [j for j in v if j != i] for v in users.values() for i in v if len(v) > 1}

    def clone(self):
        """an independent reader of the current state (for a worker thread)"""
        return BitmapSet(self.p.tobytes())

    # ---- how the bitmap is drawn (shape fill matrix x placement chain) ----
    def _placements(self):
        """object index -> [(owner sprite index, 0 = root; Place/PlaceClip command address)] (new placements only, not moves)"""
        p = self.p; out = {}

        def walk(owner, fa, n):
            for h in p.frames_of(fa, n):
                seen = set()
                while h and h not in seen:
                    seen.add(h)
                    if p.u8(h) in (0, 1):
                        c = p.u16(h + 0xE)
                        if c != 0xFFFF: out.setdefault(c, []).append((owner, h))
                    h = p.u32(h + 4)
        walk(0, p.frames, p.fc)
        for i in range(1, p.count):
            a = p.ptrs[i]
            if a and p.u8(a) == 2: walk(i, p.u32(a + 0xC), p.u32(a + 0x10))
        return out

    def _geometry(self):
        if self._geo is not None: return self._geo
        p = self.p; geo = {}
        byaddr = {p.ptrs[i]: i for i in self.indices}
        try: plc = self._placements()
        except Exception: plc = {}
        memo = {}

        def chains(obj, depth=0):                     # every placement matrix chain of an object, composed down to the stage
            if obj in memo: return memo[obj]
            res = []
            for owner, cmd in plc.get(obj, [])[:64]:
                try: m = _lin(p.read_pmatrix(p.u32(cmd + 0x10), p.u8(cmd + 1)) if p.u32(cmd + 0x10) else None)
                except Exception: m = _lin(None)
                up = chains(owner, depth + 1) if owner and depth < 8 else []
                res += [_mul(pm, m) for pm in up] if up else [m]
                if len(res) >= 256: break
            memo[obj] = res = res[:256]
            return res
        for si in range(1, p.count):
            a = p.ptrs[si]
            if not a or p.otype(si) != 1: continue
            try:
                ft = p.u32(a + 8)
                for k in range(p.u16(a + 0x14)):
                    f = ft + 0x28 * k
                    if p.u8(f) not in (0x40, 0x41): continue
                    bi = byaddr.get(p.u32(f + 0x20))
                    if bi is None: continue
                    A, B, C, D = struct.unpack_from('<4f', p.d, f + 8 - p.base)
                    det = A * D - B * C
                    if not all(map(math.isfinite, (A, B, C, D))) or abs(det) < 1e-12: continue
                    wv, hv = (D / det, -B / det), (-C / det, A / det)    # the unit UV square back in shape space (px)
                    ms = chains(si)
                    for m in (ms or [(1.0, 0.0, 0.0, 1.0)]):
                        w, h = math.hypot(*_apply(m, wv)), math.hypot(*_apply(m, hv))
                        if w > 1e-9 and h > 1e-9:
                            geo.setdefault(bi, []).append(dict(aspect=w / h, placed=bool(ms), shape=si))
            except Exception:
                continue
        self._geo = geo
        return geo

    def screen(self, i):
        """-> dict(aspect or None, basis 'placement' / 'shape' / 'none', note): the aspect bitmap `i` is drawn with"""
        uses = self._geometry().get(i, [])
        placed = [u for u in uses if u['placed']]
        basis = 'placement' if placed else 'shape' if uses else 'none'
        uses = placed or uses
        if not uses: return dict(aspect=None, basis='none', note='no shape uses this bitmap')
        v = sorted(u['aspect'] for u in uses)
        if v[-1] / v[0] > AGREE:
            return dict(aspect=None, basis=basis, note='used with different proportions (%s)' % ', '.join('%.2f' % x for x in sorted(set(round(x, 2) for x in v))))
        return dict(aspect=v[len(v) // 2], basis=basis, note='')

    def reference_aspect(self, d, reference='auto'):
        """the aspect new images are fitted to / exports are restored to"""
        if reference not in ASPECTS: raise ValueError('aspect must be one of %s' % ', '.join(ASPECTS))
        sa = d.get('screen_aspect')
        return sa if reference == 'auto' and sa else d['aspect']

    # ---- info ----
    def info(self, i):
        p = self.p; a = p.ptrs[i]
        d = dict(index=i, ok=True, error='', shared_with=self._shared.get(i, []), modified=i in self.snaps)
        try:
            b = p.bitmap_parts(a)
            tw, th = b['tw'], b['th']
            sw, sh = tw, th
            inv = struct.unpack_from('<ff', p.d, a + 0x10 - p.base)
            if inv[0] > 0 and inv[1] > 0:
                x, y = round(1 / inv[0]), round(1 / inv[1])
                if 1 <= x <= 4096 and 1 <= y <= 4096: sw, sh = x, y
            if b['ttype'] not in (1, 5, 6): raise ValueError('unsupported texture type %d' % b['ttype'])
            d.update(tex_w=tw, tex_h=th, src_w=sw, src_h=sh, ttype=b['ttype'], ncol=b['ncol'], bpp=4 if b['ttype'] == 6 else 8,
                     aspect=sw / sh, resized=(sw, sh) != (tw, th))
            sc = self.screen(i)
            d.update(screen_aspect=sc['aspect'], screen_basis=sc['basis'], screen_note=sc['note'])
            ra = self.reference_aspect(d)
            d.update(auto_aspect=ra, auto_size=_display_size(d, ra))
        except Exception as e:
            d.update(ok=False, error=str(e), tex_w=0, tex_h=0, src_w=0, src_h=0, ttype=0, ncol=0, bpp=0, aspect=1.0, resized=False,
                     screen_aspect=None, screen_basis='none', screen_note='', auto_aspect=1.0, auto_size=(0, 0))
        return d

    def listing(self):
        return [self.info(i) for i in self.indices]

    # ---- decoding ----
    def texture_image(self, i):
        """the texture exactly as stored (power-of-two size) as RGBA"""
        w, h, idx, pal, _ = _decode_indices(self.p, self.p.ptrs[i])
        return _render(idx, pal, w, h)

    def image(self, i, restore_size=True, reference='auto'):
        """RGBA image of the bitmap; with restore_size at its true proportions (as drawn in the game when known, else the source size)."""
        from PIL import Image
        im = self.texture_image(i)
        if restore_size:
            d = self.info(i)
            size = _display_size(d, self.reference_aspect(d, reference))
            if size != im.size: im = im.resize(size, Image.LANCZOS)
        return im

    def export_png(self, i, out, restore_size=True, reference='auto'):
        im = self.image(i, restore_size, reference)
        im.save(out, 'PNG')
        return im.size

    # ---- replacing ----
    def _prepare(self, i, image, fit, dither, reference='auto'):
        d = self.info(i)
        if not d['ok']: raise ValueError('bitmap %d cannot be edited: %s' % (i, d['error']))
        tex = fit_image(load_image(image), (d['tex_w'], d['tex_h']), self.reference_aspect(d, reference), fit)
        idx, pal = quantize(tex, d['ncol'], dither)
        return d, idx, pal

    def preview_replacement(self, i, image, fit='cover', dither=False, restore_size=True, reference='auto'):
        """RGBA image of what the bitmap would look like after replace() (nothing is written)"""
        from PIL import Image
        d, idx, pal = self._prepare(i, image, fit, dither, reference)
        im = _render(idx, pal, d['tex_w'], d['tex_h'])
        if restore_size:
            size = _display_size(d, self.reference_aspect(d, reference))
            if size != im.size: im = im.resize(size, Image.LANCZOS)
        return im

    def _snapshot(self, i):
        p = self.p; b = p.bitmap_parts(p.ptrs[i])
        n = (b['tw'] * b['th'] + 1) // 2 if b['ttype'] == 6 else b['tw'] * b['th']
        return p.raw(b['pix'], n), p.raw(b['pal'] + 0x10, 4 * b['ncol'])

    def replace(self, i, image, fit='cover', dither=False, reference='auto'):
        d, idx, pal = self._prepare(i, image, fit, dither, reference)
        snap = self._snapshot(i)
        rpck.bitmap_encode_into(self.p, self.p.ptrs[i], idx, pal)
        self.snaps.setdefault(i, snap)
        return d

    def revert(self, i):
        snap = self.snaps.pop(i, None)
        if snap is None: return False
        p = self.p; b = p.bitmap_parts(p.ptrs[i]); pix, pal = snap
        p.d[b['pix'] - p.base:b['pix'] - p.base + len(pix)] = pix
        p.d[b['pal'] + 0x10 - p.base:b['pal'] + 0x10 - p.base + len(pal)] = pal
        return True

    def revert_all(self):
        for i in list(self.snaps): self.revert(i)

    @property
    def modified(self):
        return sorted(self.snaps)

    def save(self, out):
        with open(out, 'wb') as f: f.write(self.p.tobytes())
        return out


# ------------------------------------------------------------------------------------------------ CLI
def describe(d):
    if not d['ok']: return '#%-4d unsupported: %s' % (d['index'], d['error'])
    s = '#%-4d %4dx%-4d' % (d['index'], d['src_w'], d['src_h'])
    if d['resized']: s += ' (texture %dx%d)' % (d['tex_w'], d['tex_h'])
    s += '  %dbpp %d colours  aspect %.3f' % (d['bpp'], d['ncol'], d['aspect'])
    if d['screen_aspect'] and abs(d['screen_aspect'] / d['aspect'] - 1) >= 0.01: s += '  drawn at %.3f' % d['screen_aspect']
    elif d['screen_note']: s += '  (%s)' % d['screen_note']
    if d['shared_with']: s += '  shares pixels with ' + ','.join('#%d' % j for j in d['shared_with'])
    return s


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pck'); sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('list')
    e = sub.add_parser('export'); e.add_argument('index', type=int); e.add_argument('out'); e.add_argument('--raw', action='store_true', help='keep the power-of-two texture size')
    e.add_argument('--aspect', choices=ASPECTS, default='auto', help='auto = proportions the game draws it with (when known), source = stored source size')
    e = sub.add_parser('export-all'); e.add_argument('folder'); e.add_argument('--raw', action='store_true'); e.add_argument('--aspect', choices=ASPECTS, default='auto')
    r = sub.add_parser('replace'); r.add_argument('index', type=int); r.add_argument('image'); r.add_argument('out')
    r.add_argument('--fit', choices=FITS, default='cover'); r.add_argument('--dither', action='store_true')
    r.add_argument('--aspect', choices=ASPECTS, default='auto', help='aspect the new image is fitted to: auto = as the game draws it (when known), source = stored source size')
    a = ap.parse_args()
    bs = BitmapSet(a.pck)
    if a.cmd == 'list':
        for d in bs.listing(): print(describe(d))
        print('%d bitmap(s)' % len(bs.indices))
    elif a.cmd == 'export':
        if a.index not in bs.indices: sys.exit('object %d is not a bitmap' % a.index)
        print('wrote %s (%dx%d)' % ((a.out,) + bs.export_png(a.index, a.out, not a.raw, a.aspect)))
    elif a.cmd == 'export-all':
        os.makedirs(a.folder, exist_ok=True); base = os.path.splitext(os.path.basename(a.pck))[0]
        for i in bs.indices:
            try: bs.export_png(i, os.path.join(a.folder, '%s_%04d.png' % (base, i)), not a.raw, a.aspect)
            except Exception as ex: print('#%d skipped: %s' % (i, ex))
        print('done')
    elif a.cmd == 'replace':
        if a.index not in bs.indices: sys.exit('object %d is not a bitmap' % a.index)
        bs.replace(a.index, a.image, a.fit, a.dither, a.aspect)
        with rpck.replacing(a.pck, a.out) as (_, bak):    # same file as the input: the original is kept as <name>.old
            bs.save(a.out)
        if bak: print('original kept as %s' % bak)
        print('wrote %s (%d bytes)' % (a.out, len(bs.p.d) + rpck.HDR))


if __name__ == '__main__':
    main()
