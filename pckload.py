#!/usr/bin/env python3
"""pckload: edit the accent colour and the artwork of MC3 loading-screen pcks (loading_*.pck, loadscreen_*.pck).

What a loading screen contains (verified on loading_generic_a, loading_network_s10, loadscreen_arcade_t05):
  * ARTWORK  the sprite named 'pict1' holds ONE shape with two polygons side by side; the left polygon is filled with one 256x256
             8bpp bitmap, the right one with another, so the picture is 2 textures stitched into 512x256 (shown at ~450x265 px, aspect
             1.7-1.8, i.e. slightly squeezed).  The rounded corner is baked in as transparent palette entries.
             Each texture is stretched over ITS OWN polygon, and in some files the polygons overlap by a few px (arcade: up to ~4 px).
             The picture (export / preview / what replace_art fits an image to) is therefore the polygons' screen layout, not the two
             textures glued at x=256: cutting a new image at 256 would draw the overlapping strip twice and show a seam.
  * ACCENT   three independent values that the designers picked by hand per screen:
               bar   solid fill 0 of the loader-bar shape (the only shape with two solid fills, the 2nd being grey 179,179,179)
               text  colour of the big translucent EDITTEXT (root sprite whose only content is an EDITTEXT, drawn at ~36 % alpha)
               tint  colour transform of the strip pattern in the 'panelbottom' sprite: white becomes 255*mult/64 + add
The accent items are found by structure, so a pck that deviates reports 'not found' for that part instead of being edited blindly.
"""
import os, sys, struct, colorsys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpck import Pck, bitmap_decode, bitmap_encode_into
import pckanim

FILL_COLOR_OFF = 0x04      # RGBA of a solid fill inside the 0x28-byte fill entry (a bitmap fill keeps its bitmap pointer at +0x20)
EDITTEXT_COLOR_OFF = 0x14  # RGBA of an EDITTEXT


# ----------------------------------------------------------------------------------------------------------------------
# finding things
# ----------------------------------------------------------------------------------------------------------------------
def _poly_fills(p, a):
    """[(bitmap addr, (xmin, xmax, ymin, ymax) px)] for a shape built from bitmap-filled polygons; None otherwise"""
    nf = p.u16(a + 0x14); ft = p.u32(a + 8); q = p.u32(a + 0x10); fill = None; out = []
    while True:
        op = p.u16(q)
        if op == 0: break
        if op == 4: fill = p.u16(q + 2); q += 4
        elif op == 5: q += 4
        elif op == 2:
            n = p.u16(q + 2); pts = [struct.unpack_from('<hh', p.d, q + 4 + 4 * k - p.base) for k in range(n)]; q += 4 + 4 * n
            if not fill or fill > nf: return None
            f = ft + 0x28 * (fill - 1); bm = p.u32(f + 0x20)
            if p.u8(f) not in (0x40, 0x41) or not bm or not p.inside(bm): return None
            xs = [x for x, y in pts]; ys = [y for x, y in pts]
            out.append((bm, (min(xs) / 20.0, max(xs) / 20.0, min(ys) / 20.0, max(ys) / 20.0)))
        else:
            return None
    return out


def _tex_boxes(p, shape, left, right):
    """[left, right] (xmin, xmax, ymin, ymax) px that each texture (UV 0..1) covers, from the fill matrices (px -> UV); None if rotated/skewed.
    These are usually a little LARGER than the polygons (the polygon crops the texture by 1-3 px), so they - not the polygon boxes - say
    where a texture pixel lands on screen."""
    try: sh = p.read_shape(p.ptrs[shape])
    except Exception: return None
    got = {}
    for f in sh['fills']:
        if f.get('bitmap') in (left, right) and f['flag'] in (0x40, 0x41):
            m = f['matrix']
            if abs(m[1]) > 1e-9 or abs(m[2]) > 1e-9 or not m[0] or not m[3]: return None
            xs = sorted(((0 - m[4]) / m[0], (1 - m[4]) / m[0])); ys = sorted(((0 - m[5]) / m[3], (1 - m[5]) / m[3]))
            got[f['bitmap']] = (xs[0], xs[1], ys[0], ys[1])
    return [got[left], got[right]] if left in got and right in got else None


def _root_named(p):
    names = {}
    for h in p.frames_of(p.frames, p.fc):
        for c in p.cmds(h):
            if p.u8(c) in (0, 1):
                o = p.read_cmd(c)
                if o.get('name'): names[o['name']] = o
    return names


def _sprite_cmds(p, sprite, frame=0):
    a = p.ptrs[sprite]
    if not a or p.u8(a) != 2 or p.u32(a + 0x10) < 1: return []
    return [p.read_cmd(c) for c in p.cmds(p.frames_of(p.u32(a + 0xC), p.u32(a + 0x10))[frame]) if p.u8(c) in (0, 1)]


def find_loadscreen(p):
    """-> dict(art=..., bar=..., text=..., tint=...) ; a part that is not found is None (art also carries 'unsupported')"""
    ls = dict(art=None, bar=None, text=None, tint=None)
    # artwork: shape with exactly two adjacent bitmap polygons
    best = None
    for i in range(1, p.count):
        a = p.ptrs[i]
        if not a or p.u8(a) != 1 or p.u16(a + 0x16): continue
        try: pf = _poly_fills(p, a)
        except (struct.error, ValueError, IndexError): continue
        if not pf or len(pf) != 2: continue
        L, R = sorted(pf, key=lambda t: t[1][0])
        if abs(L[1][1] - R[1][0]) > 8 or abs(L[1][2] - R[1][2]) > 8 or abs(L[1][3] - R[1][3]) > 8: continue   # the halves may overlap by a few px
        area = (R[1][1] - L[1][0]) * (L[1][3] - L[1][2])
        if best is None or area > best[0]: best = (area, i, L, R, pf)
    if best:
        _, i, L, R, pf = best; pl, pr = p.bitmap_parts(L[0]), p.bitmap_parts(R[0])
        x0, x1 = L[1][0], R[1][1]; y0, y1 = min(L[1][2], R[1][2]), max(L[1][3], R[1][3])
        un = None
        if (pl['ttype'], pl['ncol'], pl['tw'], pl['th']) != (pr['ttype'], pr['ncol'], pr['tw'], pr['th']) or (pl['ttype'], pl['ncol']) != (1, 256):
            un = 'the two halves are not equal 8bpp/256-colour textures'
        ls['art'] = dict(shape=i, left=L[0], right=R[0], tex=(pl['tw'], pl['th']), size=(pl['tw'] * 2, pl['th']),
                         screen=(x1 - x0, y1 - y0), aspect=(x1 - x0) / (y1 - y0), unsupported=un,
                         boxes=(L[1], R[1]), origin=(x0, y0),                                  # polygon boxes (xmin, xmax, ymin, ymax) px, left / right half
                         top='right' if pf.index(R) > pf.index(L) else 'left',                  # the half that is drawn last (on top where they overlap)
                         overlap=L[1][1] - R[1][0])                                             # px the halves overlap (negative = gap)
        tb = _tex_boxes(p, i, L[0], R[0])
        if tb: ls['art']['tboxes'] = tb                                                         # px box the fill matrix stretches each TEXTURE over (the polygon only crops it)
    # loader bar: the only shape with two solid fills, the second grey 179
    for i in range(1, p.count):
        a = p.ptrs[i]
        if not a or p.u8(a) != 1 or p.u16(a + 0x14) != 2: continue
        ft = p.u32(a + 8)
        if p.u8(ft) == 0 and p.u8(ft + 0x28) == 0 and tuple(p.raw(ft + 0x28 + FILL_COLOR_OFF, 4)) == (179, 179, 179, 255):
            ls['bar'] = dict(shape=i, addr=ft + FILL_COLOR_OFF); break
    names = _root_named(p)
    # big translucent text: a root-placed sprite whose frame 0 contains only an EDITTEXT
    cand = []
    for h in p.frames_of(p.frames, p.fc):
        for c in p.cmds(h):
            if p.u8(c) not in (0, 1): continue
            o = p.read_cmd(c); ch = o['char']
            if ch >= p.count or not p.ptrs[ch] or p.u8(p.ptrs[ch]) != 2: continue
            inner = _sprite_cmds(p, ch)
            if len(inner) == 1 and inner[0]['char'] < p.count and p.ptrs[inner[0]['char']] and p.u8(p.ptrs[inner[0]['char']]) == 7:
                cand.append((0 if (o['cxform'] and o['cxform'][3] < 64) else 1, inner[0]['char']))
    if cand:
        et = sorted(cand)[0][1]; ls['text'] = dict(obj=et, addr=p.ptrs[et] + EDITTEXT_COLOR_OFF)
    # strip tint: in 'panelbottom', the first placed sprite that itself only places something with a grey-multiplier colour transform
    pb = names.get('panelbottom')
    if pb:
        for o in _sprite_cmds(p, pb['char']):
            if o['char'] >= p.count or not p.ptrs[o['char']] or p.u8(p.ptrs[o['char']]) != 2: continue
            inner = _sprite_cmds(p, o['char'])
            if len(inner) == 1 and inner[0]['cxform'] and len(set(inner[0]['cxform'][:3])) == 1:
                ls['tint'] = dict(sprite=o['char'], cmd=inner[0]['addr']); break
    return ls


# ----------------------------------------------------------------------------------------------------------------------
# colours
# ----------------------------------------------------------------------------------------------------------------------
def _cx(p, cmd):
    xp = p.u32(cmd + 0x14)
    return xp, (list(p.raw(xp, 8)) if xp else None)


def tint_final(mult, add):
    """colour that WHITE becomes through the colour transform (what the eye sees): 255*mult/64 + add (add is a signed byte)"""
    sg = lambda v: v - 256 if v > 127 else v
    return tuple(max(0, min(255, round(255.0 * mult[i] / 64 + sg(add[i])))) for i in range(3))


def tint_from_final(rgb, prefer=8):
    """(mult byte, add bytes) such that white becomes `rgb`.  Keeps the multiplier `prefer` (the file's own, so non-white content of the
    pattern is affected as little as possible; 8 is what two of the three originals use) while the colour stays above 255*mult/64,
    otherwise lowers it so that no negative add is needed"""
    m = max(0, min(63, int(prefer)))
    low = min(rgb)
    if low < 255 * m // 64: m = max(0, int(low * 64 // 255))
    base = 255.0 * m / 64
    add = [max(0, min(127, int(round(c - base)))) for c in rgb]
    return m, add


def read_colors(p, ls):
    """-> dict with 'bar', 'text', 'tint' (final colour) as (r, g, b) or None"""
    out = dict(bar=None, text=None, tint=None)
    if ls['bar']: out['bar'] = tuple(p.raw(ls['bar']['addr'], 3))
    if ls['text']: out['text'] = tuple(p.raw(ls['text']['addr'], 3))
    if ls['tint']:
        _, cx = _cx(p, ls['tint']['cmd'])
        if cx: out['tint'] = tint_final(cx[:3], cx[4:7])
    return out


def derive_from_accent(rgb):
    """one accent colour -> bar, text, strip tint.  bar = text = accent and the strip tint is the accent at half saturation and half brightness:
    this is exactly how loading_generic_a (green) is built; the other screens were tuned by hand (brighter text), so tweak those separately."""
    h, s, v = colorsys.rgb_to_hsv(*[c / 255.0 for c in rgb[:3]])
    tint = tuple(int(round(c * 255)) for c in colorsys.hsv_to_rgb(h, s * 0.5, v * 0.5))
    return dict(bar=tuple(rgb[:3]), text=tuple(rgb[:3]), tint=tint)


def _cxform_shared(p, ptr, cmd):
    n = 0
    def scan(fa, cnt):
        nonlocal n
        for h in p.frames_of(fa, cnt):
            for c in p.cmds(h):
                if c != cmd and p.u8(c) in (0, 1) and p.u32(c + 0x14) == ptr: n += 1
    scan(p.frames, p.fc)
    for i in range(1, p.count):
        a = p.ptrs[i]
        if a and p.u8(a) == 2: scan(p.u32(a + 0xC), p.u32(a + 0x10))
    return n > 0


def set_colors(p, ls, bar=None, text=None, tint=None, log=print):
    """write any of bar/text/tint ((r,g,b) tuples; None keeps it).  Raises if a requested part was not found."""
    for key, val in (('bar', bar), ('text', text), ('tint', tint)):
        if val is not None and not ls[key]: raise ValueError('this pck has no %s colour to change (structure not recognised)' % key)
    if bar is not None:
        p.put(ls['bar']['addr'], '<BBB', *[int(c) for c in bar[:3]]); log('bar colour -> #%02x%02x%02x' % tuple(bar[:3]))
    if text is not None:
        p.put(ls['text']['addr'], '<BBB', *[int(c) for c in text[:3]]); log('text colour -> #%02x%02x%02x' % tuple(text[:3]))
    if tint is not None:
        cmd = ls['tint']['cmd']; ptr, cx = _cx(p, cmd)
        m, add = tint_from_final(tint, cx[0] if cx else 8)
        new = bytes([m, m, m, cx[3] if cx else 64, add[0], add[1], add[2], cx[7] if cx else 0])
        if ptr and not _cxform_shared(p, ptr, cmd): p.d[ptr - p.base:ptr - p.base + 8] = new
        else: p.put(cmd + 0x14, '<I', p.alloc(new, 4))
        log('strip tint -> #%02x%02x%02x (mult %d, add %s)' % (tuple(tint[:3]) + (m, tuple(add))))


# ----------------------------------------------------------------------------------------------------------------------
# artwork
# ----------------------------------------------------------------------------------------------------------------------
def _tiles(p, ls):
    """[left, right] texture as RGBA"""
    from PIL import Image
    a = ls['art']; out = []
    for b in (a['left'], a['right']):
        w, h, idx, pal, tt = bitmap_decode(p, b)
        cols = [(r, g, bl, min(255, al * 2)) for r, g, bl, al in pal]
        out.append(Image.frombytes('RGBA', (w, h), b''.join(bytes(cols[i]) for i in idx)))
    return out


def decode_textures(p, ls):
    """-> RGBA image of the two textures side by side (left | right), exactly as stored (tw*2 x th)"""
    from PIL import Image
    a = ls['art']; tw, th = a['tex']; out = Image.new('RGBA', a['size'])
    for k, t in enumerate(_tiles(p, ls)): out.paste(t, (k * tw, 0))
    return out


def _to_canvas(a, boxes):
    W, H = a['size']; ox, oy = a['origin']; sw, sh = a['screen']
    if sw <= 0 or sh <= 0: return [(0.0, 0.0, W / 2.0, float(H)), (W / 2.0, 0.0, float(W), float(H))]
    return [((bx0 - ox) / sw * W, (by0 - oy) / sh * H, (bx1 - ox) / sw * W, (by1 - oy) / sh * H) for bx0, bx1, by0, by1 in boxes]


def half_boxes(a):
    """canvas-space (float) boxes (x0, y0, x1, y1) that the left / right TEXTURE is stretched over (from the fill matrices; the polygon boxes when those
    are not known).  The canvas is the artwork's visible rectangle (union of the polygons) stretched to `size` (512x256).  The boxes can reach a little
    outside the canvas and overlap each other by more than the polygons do."""
    return _to_canvas(a, a.get('tboxes') or a['boxes'])


def clip_boxes(a):
    """canvas-space boxes of the two POLYGONS: the part of each texture that is actually drawn"""
    return _to_canvas(a, a['boxes'])


def _pad(img, m, fill_alpha=None):
    """img with an m px border that repeats the edge pixels (textures may reach slightly outside the canvas)"""
    from PIL import Image
    W, H = img.size; out = Image.new(img.mode, (W + 2 * m, H + 2 * m)); out.paste(img, (m, m))
    out.paste(img.crop((0, 0, 1, H)).resize((m, H)), (0, m)); out.paste(img.crop((W - 1, 0, W, H)).resize((m, H)), (W + m, m))
    out.paste(img.crop((0, 0, W, 1)).resize((W, m)), (m, 0)); out.paste(img.crop((0, H - 1, W, H)).resize((W, m)), (m, H + m))
    for cx, cy, px, py in ((0, 0, 0, 0), (W - 1, 0, W + m, 0), (0, H - 1, 0, H + m), (W - 1, H - 1, W + m, H + m)):
        out.paste(img.crop((cx, cy, cx + 1, cy + 1)).resize((m, m)), (px, py))
    return out


def halves_to_textures(img, boxes, tex, resample=None):
    """canvas image (W x H) -> the two textures side by side (2*tw x th): each half samples the canvas box its fill stretches it over
    (outside the canvas the edge pixels are repeated)"""
    from PIL import Image
    tw, th = tex; out = Image.new(img.mode, (tw * 2, th)); resample = resample or Image.LANCZOS
    M = 16; padded = None
    for k, (c0, r0, c1, r1) in enumerate(boxes):
        if c1 - c0 < 1e-6 or r1 - r0 < 1e-6: continue
        if all(abs(v - round(v)) < 1e-6 for v in (c0, r0, c1, r1)) and (round(c1 - c0), round(r1 - r0)) == (tw, th) and c0 >= 0 and r0 >= 0 and c1 <= img.width and r1 <= img.height:
            t = img.crop(tuple(int(round(v)) for v in (c0, r0, c1, r1)))              # 1:1 inside the canvas: no resampling at all
        else:
            if padded is None: padded = _pad(img, M)
            t = padded.resize((tw, th), resample, box=(max(0.0, c0 + M), max(0.0, r0 + M), min(float(padded.width), c1 + M), min(float(padded.height), r1 + M)))
        out.paste(t, (k * tw, 0))
    return out


def compose_halves(tiles, boxes, size, top='right', clip=None):
    """the two textures -> the picture the game draws (canvas W x H): every texture stretched over its box and cropped to its polygon (`clip`),
    the `top` half drawn last"""
    from PIL import Image
    out = Image.new('RGBA', size, (0, 0, 0, 0))
    for k in ((0, 1) if top == 'right' else (1, 0)):
        x0, y0, x1, y1 = [int(round(v)) for v in boxes[k]]
        if x1 <= x0 or y1 <= y0: continue
        t = tiles[k] if tiles[k].size == (x1 - x0, y1 - y0) else tiles[k].resize((x1 - x0, y1 - y0), Image.LANCZOS)
        layer = Image.new('RGBA', size, (0, 0, 0, 0)); layer.paste(t, (x0, y0))
        cx0, cy0, cx1, cy1 = [int(round(v)) for v in (clip[k] if clip else boxes[k])]
        layer = layer.crop((max(0, cx0), max(0, cy0), min(size[0], cx1), min(size[1], cy1)))
        if layer.width > 0 and layer.height > 0: out.alpha_composite(layer, (max(0, cx0), max(0, cy0)))
    return out


def decode_art(p, ls):
    """-> RGBA image of the artwork as the game draws it (the polygons' screen layout at 512x256; see half_boxes).  With polygons that tile exactly
    this is the two textures side by side."""
    a = ls['art']
    return compose_halves(_tiles(p, ls), half_boxes(a), a['size'], a.get('top', 'right'), clip_boxes(a))


def display_preview(img, aspect, width=None):
    """the stitched image squeezed to the aspect the game shows it with"""
    from PIL import Image
    w = width or img.size[0]
    return img.resize((w, max(1, round(w / aspect))), Image.LANCZOS)


PARTIAL_LEVELS = 8      # palette entries reserved for anti-aliased (partly transparent) edge pixels


def _quantize(rgb, alpha, dither):
    """RGB image + per-pixel alpha bytes (0 = transparent, 255 = opaque, in between = anti-aliased edge) -> (indices, 256 palette entries).
    Transparent pixels get a reserved alpha-0 entry (index 255); edge pixels get one of PARTIAL_LEVELS reserved entries that share the edge colour
    and differ in alpha (the palette stores alpha per entry, 0..128 = opaque), so rounded edges are smooth instead of stair-stepped."""
    from PIL import Image
    n = len(alpha); px = rgb.tobytes()
    part = [i for i in range(n) if 0 < alpha[i] < 255]; any_t = any(v == 0 for v in alpha); any_p = bool(part)
    if any_t or any_p:                          # keep transparent / edge pixels out of the palette statistics
        opaque = [px[3 * i:3 * i + 3] for i in range(n) if alpha[i] == 255][::37]
        mean = tuple(int(sum(c[k] for c in opaque) / max(1, len(opaque))) for k in range(3)) if opaque else (0, 0, 0)
        fl = bytearray(px)
        for i in range(n):
            if alpha[i] != 255: fl[3 * i:3 * i + 3] = bytes(mean)
        rgb = Image.frombytes('RGB', rgb.size, bytes(fl))
    edge = (0, 0, 0)
    if any_p:                                   # one colour for all edge entries: the mean colour of the edge pixels
        edge = tuple(int(round(sum(px[3 * i + k] for i in part) / len(part))) for k in range(3))
    ncol = 256 - (1 if any_t else 0) - (PARTIAL_LEVELS if any_p else 0)
    idx, rgbs = pckanim.quantize_opaque(rgb, ncol, dither); idx = bytearray(idx)
    cols = [(r, g, bl, 128) for r, g, bl in rgbs]
    while len(cols) < ncol: cols.append((0, 0, 0, 128))
    if any_p:
        cols += [edge + (max(1, round(128.0 * k / (PARTIAL_LEVELS + 1))),) for k in range(1, PARTIAL_LEVELS + 1)]
    if any_t: cols.append((0, 0, 0, 0))
    for i in range(n):
        if alpha[i] == 0: idx[i] = 255
        elif alpha[i] < 255: idx[i] = ncol + max(1, min(PARTIAL_LEVELS, round(alpha[i] * (PARTIAL_LEVELS + 1) / 255.0))) - 1
    return bytes(idx), cols


# ----------------------------------------------------------------------------------------------------------------------
# frame overlay
# ----------------------------------------------------------------------------------------------------------------------
DEFAULT_FRAME = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', 'frame.svg')


def load_frame(src, size):
    """frame overlay (SVG, or any raster image with transparency) -> RGBA `size`.  SVG is rendered with cairosvg (pip install cairosvg);
    the whole image / viewBox is stretched to `size` (the artwork is 512x256, like the bundled assets/frame.svg)."""
    from PIL import Image
    if not isinstance(src, (str, bytes, os.PathLike)): return src.convert('RGBA').resize(size, Image.LANCZOS)
    if str(src).lower().endswith(('.svg', '.svgz')):
        try:
            import cairosvg, io
        except (ImportError, OSError) as e:
            raise RuntimeError('rendering an SVG frame needs cairosvg (pip install cairosvg, plus the cairo library); or use a PNG frame with transparency (%s)' % e)
        return Image.open(io.BytesIO(cairosvg.svg2png(url=str(src), output_width=size[0], output_height=size[1]))).convert('RGBA')
    with Image.open(src) as im: return im.convert('RGBA').resize(size, Image.LANCZOS)


def frame_shape(fr):
    """RGBA frame -> (alpha 'L', paint 'L').  alpha = per-pixel opacity of the artwork: 255 inside the frame line (including the inner edge of the
    line), the line's own anti-aliasing at its outer edge (so the rounded edge is smooth), 0 outside.  paint = the line pixels that must be drawn
    in the frame colour instead of blended with the artwork.  The inside is found by flood filling from the centre of the image, so the frame must
    run edge to edge and enclose the picture (the bundled one does: top edge, down the left, round the corner, along the bottom to the right edge)."""
    from PIL import ImageDraw, ImageChops, ImageFilter
    W, H = fr.size; al = fr.getchannel('A')
    weak = al.point(lambda v: 255 if v >= 32 else 0)                      # seals the picture area
    if weak.getpixel((W // 2, H // 2)): raise ValueError('the frame covers the centre of the artwork, so its inside cannot be found')
    fill = weak.copy(); ImageDraw.floodfill(fill, (W // 2, H // 2), 128)
    inside = fill.point(lambda v: 255 if v == 128 else 0)
    near = inside.filter(ImageFilter.MaxFilter(3))                        # inside + the line pixels touching it (inner edge)
    opaque_part = ImageChops.lighter(near, inside)
    outer = ImageChops.subtract(al, opaque_part)                          # line pixels away from the inside: keep their own alpha
    alpha = ImageChops.lighter(opaque_part.point(lambda v: 255 if v else 0), outer)
    paint = ImageChops.subtract(weak, opaque_part)
    return alpha, paint


def replace_art(p, ls, image, fit='cover', keep_mask=True, dither=False, palette='shared', log=print, frame=None):
    """Replace the artwork by `image` (PIL image).  fit: 'cover' crops to the on-screen aspect, 'stretch', 'contain' (black bars).
    frame: optional SVG / image path (see load_frame) drawn over the fitted artwork; the artwork is then cut along the frame line (anti-aliased) and
    the original corners are not used.  Without a frame, keep_mask keeps the original transparent (rounded) corners.  palette: 'shared' = both
    textures use one palette (no colour step at the seam; default) or 'separate' = 256 colours each (more accurate colours, the seam can show a
    faint step).  Returns the stitched RGBA that was written (before palette reduction)."""
    from PIL import Image
    a = ls['art']
    if not a: raise ValueError('no artwork found in this pck')
    if a['unsupported']: raise ValueError('artwork cannot be replaced: %s' % a['unsupported'])
    W, H = a['size']; tw, th = a['tex']
    fitted = pckanim.fit_image(image, (W, H), a['aspect'], fit)
    if frame:
        fr = load_frame(frame, (W, H)); fitted.alpha_composite(fr)
        mask, paint = frame_shape(fr)
        solid = fr.copy(); solid.putalpha(255)                            # the outer part of the line is drawn in the frame colour, not blended with the artwork
        fitted.paste(solid, (0, 0), paint)
    elif keep_mask:
        mask = decode_art(p, ls).getchannel('A').point(lambda v: 255 if v >= 64 else 0)
    else:
        mask = fitted.getchannel('A').point(lambda v: 255 if v >= 128 else 0)
    bg = Image.new('RGBA', (W, H), (0, 0, 0, 255)); bg.alpha_composite(fitted)
    rgb = bg.convert('RGB')
    canvas_rgb, canvas_mask = rgb, mask                                    # what is drawn (the written preview); the textures are cut from it below
    boxes = half_boxes(a)                                                  # each texture takes the part of the picture ITS polygon covers, so where the
    rgb = halves_to_textures(rgb, boxes, (tw, th))                         # polygons overlap both hold the same content and there is no seam
    mask = halves_to_textures(mask, boxes, (tw, th), Image.BILINEAR)
    if not frame: mask = mask.point(lambda v: 255 if v >= 128 else 0)      # (binary masks stay binary after resampling; a frame keeps its anti-aliased edge)
    if palette == 'shared':
        idx, pal = _quantize(rgb, mask.tobytes(), dither)
        for k, b in enumerate((a['left'], a['right'])):
            bitmap_encode_into(p, b, b''.join(idx[r * W + k * tw:r * W + (k + 1) * tw] for r in range(H)), pal)
    elif palette == 'separate':
        for k, b in enumerate((a['left'], a['right'])):
            box = (k * tw, 0, (k + 1) * tw, th)
            idx, pal = _quantize(rgb.crop(box), mask.crop(box).tobytes(), dither)
            bitmap_encode_into(p, b, idx, pal)
    else:
        raise ValueError("palette must be 'shared' or 'separate'")
    log('artwork replaced (%dx%d, two %dx%d textures, %s fit, %s palette%s%s)' % (W, H, tw, th, fit, palette, ', dithered' if dither else '',
        ', cut along the frame' if frame else (', original corner mask kept' if keep_mask else '')))
    out = canvas_rgb.convert('RGBA'); out.putalpha(canvas_mask)
    return out


# ----------------------------------------------------------------------------------------------------------------------
# entry points
# ----------------------------------------------------------------------------------------------------------------------
def _art_source(p, ls, image, frame):
    """PIL image to feed replace_art: the given image, or (frame only) the current artwork so the frame is drawn over it."""
    from PIL import Image
    if image is not None:
        if isinstance(image, (str, bytes, os.PathLike)):
            with Image.open(image) as im: return im.convert('RGBA'), None
        return image, None
    if frame: return decode_art(p, ls), 'stretch'
    return None, None


def preview(pck, image=None, fit='cover', keep_mask=True, dither=False, palette='shared', width=460, frame=None):
    """RGBA preview of the artwork exactly as it would be stored (palette-reduced) and shown (squeezed to the game's aspect), on a dark background.
    image: path / PIL image / None for the current artwork.  frame: optional frame overlay (drawn over the current artwork when no image is given).  Nothing is written."""
    from PIL import Image
    p = Pck(pck); ls = find_loadscreen(p)
    if not ls['art']: raise ValueError('no artwork found in this pck')
    image, f = _art_source(p, ls, image, frame)
    if image is not None:
        replace_art(p, ls, image, f or fit, keep_mask, dither, palette, log=lambda m: None, frame=frame)
    art = display_preview(decode_art(p, ls), ls['art']['aspect'], width)
    bg = Image.new('RGBA', art.size, (24, 24, 28, 255)); bg.alpha_composite(art)
    return bg


def parse_color(s):
    if isinstance(s, (tuple, list)): return tuple(int(c) for c in s[:3])
    s = s.strip().lstrip('#')
    if len(s) == 3: s = ''.join(c * 2 for c in s)
    if len(s) != 6: raise ValueError('colour must be #RRGGBB')
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def scan(pck):
    """-> plain dict for GUIs"""
    p = Pck(pck); ls = find_loadscreen(p); cols = read_colors(p, ls)
    return dict(ls=ls, colors=cols, art=ls['art'], size=len(p.d) + 0x80,
                text=summary(p, ls, cols))


def summary(p, ls, cols=None):
    cols = cols or read_colors(p, ls); a = ls['art']
    hx = lambda c: 'not found' if c is None else '#%02x%02x%02x' % tuple(c)
    s = 'artwork: ' + ('not found' if not a else '%dx%d px (two %dx%d textures), shown ~%dx%d px, aspect %.2f%s' % (
        a['size'][0], a['size'][1], a['tex'][0], a['tex'][1], round(a['screen'][0]), round(a['screen'][1]), a['aspect'],
        '  [NOT replaceable: %s]' % a['unsupported'] if a['unsupported'] else ''))
    if a and abs(a.get('overlap', 0)) >= 0.25:
        s += '\nthe two halves %s by %.1f px on screen (the artwork is mapped to follow that)' % ('overlap' if a['overlap'] > 0 else 'leave a gap', abs(a['overlap']))
    return s + '\nbar %s, text %s, strip tint %s' % (hx(cols['bar']), hx(cols['text']), hx(cols['tint']))


def edit(pck, out, bar=None, text=None, tint=None, accent=None, image=None, fit='cover', keep_mask=True, dither=False, palette='shared', log=print, frame=None):
    """read `pck`, apply colour and/or artwork changes, write `out`.  accent=(r,g,b) sets bar+text+tint from one colour (explicit bar/text/tint win).  frame: optional frame overlay (SVG/image) drawn over the artwork (over the current one if no image is given)."""
    p = Pck(pck); ls = find_loadscreen(p); size0 = len(p.d) + 0x80
    log(summary(p, ls))
    if accent is not None:
        d = derive_from_accent(parse_color(accent))
        bar = parse_color(bar) if bar else d['bar']; text = parse_color(text) if text else d['text']; tint = parse_color(tint) if tint else d['tint']
    else:
        bar = parse_color(bar) if bar else None; text = parse_color(text) if text else None; tint = parse_color(tint) if tint else None
    if bar is not None or text is not None or tint is not None: set_colors(p, ls, bar, text, tint, log)
    src, f = _art_source(p, ls, image, frame)
    if src is not None: replace_art(p, ls, src, f or fit, keep_mask, dither, palette, log, frame=frame)
    while len(p.d) % 16: p.d.append(0xCD)
    data = p.tobytes()
    with open(out, 'wb') as f: f.write(data)
    log('wrote %s (%d bytes, %+d vs the original)' % (out, len(data), len(data) - size0))
    return dict(size=len(data), delta=len(data) - size0)


def export_art(pck, out):
    p = Pck(pck); ls = find_loadscreen(p)
    if not ls['art']: raise ValueError('no artwork found in this pck')
    decode_art(p, ls).save(out); print('wrote %s' % out); return out


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pck'); sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('info', help='show the artwork and accent colours')
    e = sub.add_parser('export', help='export the stitched artwork as PNG'); e.add_argument('out')
    r = sub.add_parser('edit', help='change colours and/or replace the artwork'); r.add_argument('out')
    r.add_argument('--accent', help='#RRGGBB: sets bar, text and strip tint (generic-style derivation)')
    r.add_argument('--bar'); r.add_argument('--text'); r.add_argument('--tint', help='the colour white should become in the strip pattern')
    r.add_argument('--image', help='new artwork (any image)'); r.add_argument('--fit', choices=['cover', 'contain', 'stretch'], default='cover')
    r.add_argument('--no-mask', action='store_true', help='do not keep the original rounded corners'); r.add_argument('--dither', action='store_true'); r.add_argument('--frame', nargs='?', const=DEFAULT_FRAME, help='draw a frame (SVG / PNG with transparency) over the artwork and cut the artwork along it (replaces the original corners); without a value the bundled assets/frame.svg'); r.add_argument('--palette', choices=['shared', 'separate'], default='shared', help='shared (default): no seam; separate: 256 colours per texture')
    a = ap.parse_args()
    if a.cmd == 'info':
        p = Pck(a.pck); print(summary(p, find_loadscreen(p)))
    elif a.cmd == 'export': export_art(a.pck, a.out)
    else: edit(a.pck, a.out, a.bar, a.text, a.tint, a.accent, a.image, a.fit, not a.no_mask, a.dither, a.palette, frame=a.frame)


if __name__ == '__main__':
    main()
