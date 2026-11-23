"""SWF bitmaps -> engine swfBITMAP objects (8bpp PSMT8, swizzled pixels + CSM1-swizzled palette).

Layout (verified byte-for-byte against all 8 swizzled 8bpp sizes in the sample pcks):
  bitmap object 0x20: +8 info*, +0xC u16 w,h, +0x10 f32 1/w, +0x14 f32 1/h
  ps2Texture 0x60 (obj+0x20): +4 next (texture chain), +8 pixels*, +0x28 u16 5, +0x2A u16 blocks=(w/16)*(h/16), +0x2C w, +0x2E h,
      +0x30 u8 1 (8bpp swizzled), +0x32 u8 h/2, +0x48 u16 blocks, +0x58 u8 buffer width in 64px units
  pixels w*h, 0x30 gap, bmpInfo 0xA0 (GS upload packet): +0x40/+0x78 TEX0 lo = lw<<26 | (lh&3)<<30 | 0x13<<20 | tbw<<14,
      +0x44/+0x7C TEX0 hi = 0x20000000 | (lh>>2) | 4, +0x70 u32 4 + w*h/256, +0x88 u16 w,h, +0x90 palette*, +0x94 ps2Texture*
  palette 0x10 header + 256 RGBA (PS2 alpha 0x80 = opaque), CSM1-swizzled."""
import io, math, struct, zlib
from rpck import swizzle8, swizzle_palette, VT


def f32_trunc(x):
    """PS2 FPU rounds toward zero: 1/44 is stored as 0x3cb9...f4 style truncated floats in the originals"""
    bits = struct.unpack('<I', struct.pack('<f', x))[0]; v = struct.unpack('<f', struct.pack('<I', bits))[0]
    if abs(v) > abs(x): bits -= 1
    return struct.unpack('<f', struct.pack('<I', bits))[0]


def decode_bitmap(code, pl, jpeg_tables=b''):
    """-> PIL RGBA image"""
    from PIL import Image
    cid = struct.unpack_from('<H', pl)[0]
    if code in (6, 21, 35):
        if code == 35:
            alen = struct.unpack_from('<I', pl, 2)[0]; data = pl[6:6 + alen]; alpha = zlib.decompress(pl[6 + alen:])
        else:
            data = pl[2:]; alpha = None
        # Flash JPEG streams often start with an erroneous EOI/SOI pair (FFD9 FFD8) and may carry a doubled SOI
        if data.startswith(b'\xff\xd9\xff\xd8'): data = data[4:]
        while data[:4] == b'\xff\xd8\xff\xd8': data = data[2:]
        if code == 6:     # DefineBits: shared tables (SOI .. EOI) + image data (SOI ..) -> one stream
            data = jpeg_tables[:-2] + data[2:] if jpeg_tables.endswith(b'\xff\xd9') and data[:2] == b'\xff\xd8' else jpeg_tables + data
        from PIL import ImageFile
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        im = Image.open(io.BytesIO(data)); im.load()
        im = im.convert('RGBA')
        if alpha: im.putalpha(Image.frombytes('L', im.size, alpha[:im.size[0] * im.size[1]]))
        return im
    # lossless
    _, fmt, w, h = struct.unpack_from('<HBHH', pl)
    if fmt == 3:
        n = pl[7] + 1; raw = zlib.decompress(pl[8:]); ct = raw[:(4 if code == 36 else 3) * n]; step = 4 if code == 36 else 3
        stride = (w + 3) & ~3; im = Image.new('RGBA', (w, h))
        pal = []
        for k in range(n):
            c = ct[step * k:step * k + step]
            if code == 36:
                r, g, b, a = c
                if a: r, g, b = min(255, r * 255 // a), min(255, g * 255 // a), min(255, b * 255 // a)
            else: r, g, b = c; a = 255
            pal.append((r, g, b, a))
        px = []
        o = step * n
        for y in range(h):
            for x in range(w): px.append(pal[raw[o + y * stride + x]] if raw[o + y * stride + x] < n else (0, 0, 0, 0))
        im.putdata(px); return im
    if fmt in (4, 5):
        raw = zlib.decompress(pl[7:]); im = Image.new('RGBA', (w, h)); px = []
        if fmt == 5:
            for k in range(w * h):
                a, r, g, b = raw[4 * k:4 * k + 4]
                if code == 36 and a: r, g, b = min(255, r * 255 // a), min(255, g * 255 // a), min(255, b * 255 // a)
                px.append((r, g, b, a if code == 36 else 255))
        else:
            stride = (w * 2 + 3) & ~3
            for y in range(h):
                for x in range(w):
                    v = struct.unpack_from('>H', raw, y * stride + 2 * x)[0]; px.append((((v >> 10) & 31) * 255 // 31, ((v >> 5) & 31) * 255 // 31, (v & 31) * 255 // 31, 255))
        im.putdata(px); return im
    raise ValueError('lossless format %d' % fmt)


POW2 = [16, 32, 64, 128, 256]

def choose_sizes(dims, budget, max_dim=256, overhead=0x6F0 + 0x20):
    """dims {id: (w0,h0)} -> {id: (w, h, w, h)}: power-of-two texture the WHOLE image is stretched over (the originals' convention:
    UV 0..1 covers the image, the stored 1/size is the source size and the fill matrix undoes the stretch).
    Only textures at least as wide as they are tall are used (all 186 originals are; portrait header fields are unverified).
    The geometric-mean resolution (<= 1 per axis) is maximised for every image under the total byte budget."""
    ids = list(dims)
    if not ids: return {}
    cands = [(w, h) for w in POW2 for h in POW2 if w >= h and w <= max_dim and w // h <= 8]
    def pick(T, w0, h0):
        best = None
        for w, h in cands:
            if w * h > T and (w, h) != (16, 16): continue
            q = math.sqrt(min(1.0, w / w0) * min(1.0, h / h0))
            key = (round(q, 4), -(w * h))
            if best is None or key > best[0]: best = (key, (w, h))
        w, h = best[1]
        return w, h, w, h
    lo, hi = 256.0, float(max_dim * max_dim); best = {i: (16, 16, 16, 16) for i in ids}
    for _ in range(18):
        T = (lo + hi) / 2; sizes = {i: pick(min(T, 1e12), *dims[i]) for i in ids}
        if sum(w * h + overhead for w, h, _, _ in sizes.values()) <= budget: best = sizes; lo = T
        else: hi = T
    return best


def quantize(im, w, h, sw=None, sh=None):   # sw,sh default to w,h: the image is stretched over the whole texture
    """RGBA PIL image -> (indices bytes for a w x h texture, palette [(r,g,b,a_ps2)]*256).
    The image is scaled to sw x sh and placed top-left; the padding repeats the edge pixels so bilinear sampling does not bleed."""
    from PIL import Image
    sw, sh = sw or w, sh or h
    small = im.resize((sw, sh), Image.LANCZOS)
    if (sw, sh) != (w, h):
        im = Image.new('RGBA', (w, h)); im.paste(small, (0, 0))
        if sw < w: im.paste(small.crop((sw - 1, 0, sw, sh)).resize((w - sw, sh)), (sw, 0))
        if sh < h: im.paste(im.crop((0, sh - 1, w, sh)).resize((w, h - sh)), (0, sh))
    else: im = small
    a = im.getchannel('A'); has_alpha = a.getextrema()[0] < 250
    if has_alpha:
        q = im.quantize(colors=256, method=Image.Quantize.FASTOCTREE)
        pal = q.getpalette()[:768]; pal += [0] * (768 - len(pal)); idx = q.tobytes()
        acc = {}
        for i, av in zip(idx, a.tobytes()): acc.setdefault(i, []).append(av)
        cols = []
        for i in range(256):
            v = acc.get(i); al = round(sum(v) / len(v)) if v else 0
            cols.append((pal[3 * i], pal[3 * i + 1], pal[3 * i + 2], min(128, round(al / 2))))
        return idx, cols
    q = im.convert('RGB').quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG)
    pal = q.getpalette()[:768]; pal += [0] * (768 - len(pal))
    return q.tobytes(), [(pal[3 * i], pal[3 * i + 1], pal[3 * i + 2], 128) for i in range(256)]


def template_from(donor):
    """8bpp swizzled template pieces from a donor pck"""
    for i, a in enumerate(donor.ptrs):
        if a and donor.u8(a) == 4:
            b = donor.bitmap_parts(a)
            if b['ttype'] == 1 and b['ncol'] == 256:
                return dict(tex=donor.raw(b['tex'], 0x60), info=donor.raw(b['info'], 0xA0), palhdr=donor.raw(b['pal'], 0x10))
    raise SystemExit('the donor pck has no 8bpp swizzled bitmap to use as a template')


def emit_bitmap(p, tpl, w, h, idx, pal, chain, src=None):
    """allocate one bitmap (object + texture + pixels + GS upload block + palette) and return its object address.
    chain: dict with key 'last' = previous texture address (texture list threading, starts at the game's global head)"""
    lw, lh = int(math.log2(w)), int(math.log2(h)); tbw = max(1, (w + 63) // 64); blocks = (w // 16) * (h // 16)
    size = 0x20 + 0x60 + w * h + 0x30 + 0xA0 + 0x410
    A = p.alloc(b'\xCD' * size, 16)
    tex = A + 0x20; pix = tex + 0x60; info = pix + w * h + 0x30; pal_a = info + 0xA0
    # texture header
    t = bytearray(tpl['tex']); struct.pack_into('<II', t, 4, chain['last'], pix)
    struct.pack_into('<H', t, 0x2A, blocks); struct.pack_into('<HH', t, 0x2C, w, h); t[0x32] = (h // 2) & 0xFF
    struct.pack_into('<H', t, 0x48, blocks); t[0x58] = tbw
    # GS upload block
    inf = bytearray(tpl['info']); lo = (lw << 26) | ((lh & 3) << 30) | (0x13 << 20) | (tbw << 14); hi = 0x20000000 | ((lh >> 2) & 3) | 4
    struct.pack_into('<II', inf, 0x40, lo, hi); struct.pack_into('<II', inf, 0x78, lo, hi)
    struct.pack_into('<I', inf, 0x70, 4 + w * h // 256); struct.pack_into('<HH', inf, 0x88, w, h); struct.pack_into('<II', inf, 0x90, pal_a, tex)
    # bitmap object
    obj = bytearray(b'\xCD' * 0x20); obj[0] = 4; struct.pack_into('<II', obj, 4, VT[4], info); struct.pack_into('<HH', obj, 0xC, w, h); sw, sh = src or (w, h); struct.pack_into('<ff', obj, 0x10, 1.0 / sw, 1.0 / sh)    # inverse SOURCE size (originals store e.g. 1/425 for a logo in a 512 texture)
    o = A - p.base
    p.d[o:o + 0x20] = obj; p.d[o + 0x20:o + 0x80] = t
    p.d[pix - p.base:pix - p.base + w * h] = swizzle8(bytes(idx), w, h)
    p.d[info - p.base:info - p.base + 0xA0] = inf
    sp = swizzle_palette([tuple(c) for c in pal])
    p.d[pal_a - p.base:pal_a - p.base + 0x10] = tpl['palhdr']
    p.d[pal_a - p.base + 0x10:pal_a - p.base + 0x410] = b''.join(bytes(c) for c in sp)
    chain['last'] = tex
    return A
