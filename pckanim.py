#!/usr/bin/env python3
"""pckanim: find, export and replace the frame-by-frame bitmap animations ("movies") inside MC3 .pck files.

The bg_*.pck menu backgrounds (bg_arcade / bg_career / bg_options ...) store a looping video as a sprite whose every frame
places ONE shape that is filled with ONE 64x64 8bpp bitmap (the game stretches it over the screen, ~9.7x).  This module

  * finds such sprites ("animations"), also the sprites that share their frames (bg_options: 224- and 220-frame clips),
  * decodes them to RGBA frames / an animated GIF,
  * replaces them with any animated image (GIF, APNG, animated WebP) or a folder of images.

How a replacement is written (append-only, like swf2pck; nothing the engine already loads is moved):
  * frame k < old frame count : the existing bitmap's pixels + palette are patched in place (same 64x64 8bpp texture)
  * more frames than before   : per extra frame a bitmap (swfbitmap.emit_bitmap, texture chained after the file's last
                                texture), a cloned shape, a cloned Place command; a new frame table, a new object list
  * fewer frames              : the sprite's frame counts are lowered (old frames stay in the file as unused data)
  * optional opacity          : the placement's colour transform alpha is replaced (default: the original value is kept)
Sprites that share frames with the main animation (linked sprites) follow it, keeping their original length difference.
"""
import os, sys, struct, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpck import Pck, VT, bitmap_decode, bitmap_encode_into, OBJ_NAMES
import swfbitmap as BM
from sheetfont import sheets_of

MIN_FRAMES = 8              # a sprite needs at least this many frames to count as an animation
GLOBAL_TEX_HEAD = 0x245040


# ----------------------------------------------------------------------------------------------------------------------
# finding animations
# ----------------------------------------------------------------------------------------------------------------------
def _shape_info(p, ci):
    """object index -> (bitmap object address, polygon bbox in px) when the object is a one-polygon bitmap-filled shape"""
    a = p.ptrs[ci]
    if not a or p.u8(a) != 1:
        return None
    nf, ns = p.u16(a + 0x14), p.u16(a + 0x16)
    if ns != 0 or nf == 0:
        return None
    ft = p.u32(a + 8); q = p.u32(a + 0x10); fill = None; polys = []
    while True:
        op = p.u16(q)
        if op == 0: break
        if op == 4: fill = p.u16(q + 2); q += 4
        elif op == 2:
            n = p.u16(q + 2); polys.append([struct.unpack_from('<hh', p.d, q + 4 + 4 * k - p.base) for k in range(n)]); q += 4 + 4 * n
        else: return None
    if len(polys) != 1 or not fill or fill > nf:
        return None
    f = ft + 0x28 * (fill - 1)
    if p.u8(f) not in (0x40, 0x41):
        return None
    bm = p.u32(f + 0x20)
    if not bm or not p.inside(bm) or p.u8(bm) != 4:
        return None
    xs = [x for x, y in polys[0]]; ys = [y for x, y in polys[0]]
    return bm, ((max(xs) - min(xs)) / 20.0, (max(ys) - min(ys)) / 20.0)


def _placements(p):
    """character index -> [(owner sprite index or 0 for the root, command address)] for every Place/PlaceClip"""
    out = {}
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
        if a and p.u8(a) == 2:
            walk(i, p.u32(a + 0xC), p.u32(a + 0x10))
    return out


def find_animations(p):
    """-> list of animation dicts (see keys below), longest first.  `p` is an rpck.Pck."""
    addr2idx = {a: i for i, a in enumerate(p.ptrs) if a}
    cands = []
    for i in range(1, p.count):
        a = p.ptrs[i]
        if not a or p.u8(a) != 2:
            continue
        n = p.u32(a + 0x10)
        if n < MIN_FRAMES or n > 0xFFFF:
            continue
        try:
            heads = p.frames_of(p.u32(a + 0xC), n); shapes = []; ok = True
            for h in heads:
                cl = p.cmds(h)
                if len(cl) != 1 or p.u8(cl[0]) not in (0, 1) or p.u16(cl[0] + 0xE) == 0xFFFF:
                    ok = False; break
                shapes.append(p.u16(cl[0] + 0xE))
            if not ok:
                continue
            infos = [_shape_info(p, c) for c in shapes]
            if any(x is None for x in infos):
                continue
        except (ValueError, struct.error, IndexError):
            continue
        cands.append(dict(sprite=i, nframes=n, shapes=shapes, heads=heads, bitmaps=[x[0] for x in infos], bbox=infos[0][1]))
    cands.sort(key=lambda c: (-c['nframes'], c['sprite']))
    plc = _placements(p); anims = []
    for c in cands:
        host = next((m for m in anims if c['shapes'] == m['shapes'][:c['nframes']]), None)
        if host is not None:
            host['linked'].append(dict(sprite=c['sprite'], nframes=c['nframes'], heads=c['heads']))
            continue
        c['linked'] = []; anims.append(c)
    for m in anims:
        bms = m['bitmaps']; m['unsupported'] = None
        parts = [p.bitmap_parts(b) for b in bms]
        m['tex'] = (parts[0]['tw'], parts[0]['th'])
        if len(set(bms)) != len(bms):
            m['unsupported'] = 'some frames share one bitmap'
        elif any((b['ttype'], b['tw'], b['th'], b['ncol']) != (1, m['tex'][0], m['tex'][1], 256) for b in parts):
            m['unsupported'] = 'frames are not all 8bpp swizzled textures of the same size'
        m['fps'] = p.rate88 / 256.0
        # where is it placed (opacity, on-screen aspect)?
        m['placements'] = []
        for s in [m['sprite']] + [l['sprite'] for l in m['linked']]:
            for owner, cmd in plc.get(s, []):
                xp = p.u32(cmd + 0x14)
                m['placements'].append(dict(sprite=s, owner=owner, cmd=cmd, alpha=(p.u8(xp + 3) / 64.0 if xp else 1.0)))
        fm = p.read_cmd(m['heads'][0])['matrix']
        fsx, fsy = (fm[0] / 256.0, fm[1] / 256.0) if fm else (1.0, 1.0)
        bw, bh = m['bbox']; psx = psy = 1.0
        if m['placements']:
            pm = p.read_cmd(m['placements'][0]['cmd'])['matrix']
            if pm: psx, psy = pm[0] / 256.0, pm[1] / 256.0
        m['screen'] = (bw * fsx * abs(psx), bh * fsy * abs(psy))
        m['aspect'] = m['screen'][0] / m['screen'][1] if m['screen'][1] else 1.0
        m['alpha'] = m['placements'][0]['alpha'] if m['placements'] else 1.0
        m['frame_objs'] = [addr2idx[b] for b in bms]
    return anims


def describe(a):
    s = 'sprite %d: %d frames of %dx%d (plays at %.4g fps, %.1f s), on screen ~%dx%d px (aspect %.2f), opacity %d%%' % (
        a['sprite'], a['nframes'], a['tex'][0], a['tex'][1], a['fps'], a['nframes'] / a['fps'] if a['fps'] else 0,
        round(a['screen'][0]), round(a['screen'][1]), a['aspect'], round(a['alpha'] * 100))
    if a['linked']:
        s += '; also drives ' + ', '.join('sprite %d (%d frames)' % (l['sprite'], l['nframes']) for l in a['linked'])
    if a['unsupported']:
        s += '  [not replaceable: %s]' % a['unsupported']
    return s


# ----------------------------------------------------------------------------------------------------------------------
# decoding / exporting
# ----------------------------------------------------------------------------------------------------------------------
def decode_frames(p, anim):
    """-> list of PIL RGBA images (one per frame)"""
    from PIL import Image
    out = []
    for b in anim['bitmaps']:
        w, h, idx, pal, tt = bitmap_decode(p, b)
        cols = [(r, g, bl, min(255, al * 2)) for r, g, bl, al in pal]
        px = b''.join(bytes(cols[i]) for i in idx)
        out.append(Image.frombytes('RGBA', (w, h), px))
    return out


def _gif_durations(n, fps):
    ms = 1000.0 / fps
    return [max(20, int(round((k + 1) * ms / 10)) * 10 - int(round(k * ms / 10)) * 10) for k in range(n)]    # GIF delays are 1/100 s: keep the average exact


def save_gif(frames, path, fps, scale_to=None):
    """frames: RGBA images.  scale_to=(w,h) resizes (e.g. the on-screen shape) for a nicer preview."""
    from PIL import Image
    fr = [f.resize(scale_to, Image.BILINEAR) for f in frames] if scale_to else list(frames)
    pal = []
    for f in fr:
        bg = Image.new('RGBA', f.size, (0, 0, 0, 255)); bg.alpha_composite(f); pal.append(bg.convert('RGB'))   # preview on black
    pal[0].save(path, save_all=True, append_images=pal[1:], duration=_gif_durations(len(pal), fps), loop=0, optimize=False, disposal=1)
    return path


def export_gif(pck, sprite, out, scale=True):
    p = Pck(pck); anim = pick(find_animations(p), sprite)
    fr = decode_frames(p, anim)
    save_gif(fr, out, anim['fps'], (round(anim['screen'][0]), round(anim['screen'][1])) if scale else None)
    print('wrote %s (%d frames, %.4g fps)' % (out, len(fr), anim['fps']))
    return out


def export_frames(pck, sprite, folder):
    p = Pck(pck); anim = pick(find_animations(p), sprite)
    os.makedirs(folder, exist_ok=True)
    for k, f in enumerate(decode_frames(p, anim)):
        f.save(os.path.join(folder, 'frame_%04d.png' % k))
    print('wrote %d frames to %s' % (anim['nframes'], folder))
    return folder


def pick(anims, sprite=None):
    if not anims:
        raise ValueError('no frame animation found in this pck')
    if sprite is None:
        if len(anims) > 1:
            raise ValueError('several animations found (%s): choose one' % ', '.join(str(a['sprite']) for a in anims))
        return anims[0]
    for a in anims:
        if a['sprite'] == int(sprite) or int(sprite) in [l['sprite'] for l in a['linked']]:
            return a
    raise ValueError('sprite %s is not an animation (found: %s)' % (sprite, ', '.join(str(a['sprite']) for a in anims)))


# ----------------------------------------------------------------------------------------------------------------------
# loading the replacement
# ----------------------------------------------------------------------------------------------------------------------
IMG_EXT = ('.png', '.jpg', '.jpeg', '.bmp', '.webp', '.gif', '.tif', '.tiff')


def fit_image(im, size, aspect, fit):
    """RGBA image -> RGBA `size` texture image.  The game stretches the texture to an area of the given aspect ratio, so
    fit='cover' crops the source to that aspect first (no distortion on screen), 'contain' letterboxes it on black,
    'stretch' squeezes the whole image into the texture (distorts if the aspect differs)."""
    from PIL import Image
    im = im.convert('RGBA'); w, h = im.size
    if fit == 'stretch' or aspect <= 0:
        return im.resize(size, Image.LANCZOS)
    if fit == 'cover':
        if w / h > aspect:  nw, nh = max(1, round(h * aspect)), h
        else:               nw, nh = w, max(1, round(w / aspect))
        x0, y0 = (w - nw) // 2, (h - nh) // 2
        return im.crop((x0, y0, x0 + nw, y0 + nh)).resize(size, Image.LANCZOS)
    if fit == 'contain':
        if w / h > aspect:  nw, nh = w, max(1, round(w / aspect))
        else:               nw, nh = max(1, round(h * aspect)), h
        canvas = Image.new('RGBA', (nw, nh), (0, 0, 0, 255)); canvas.alpha_composite(im, ((nw - w) // 2, (nh - h) // 2))
        return canvas.resize(size, Image.LANCZOS)
    raise ValueError("fit must be 'cover', 'contain' or 'stretch'")


def load_source(src, size, aspect, fit='cover', fps=15.0):
    """animated image (GIF / APNG / WebP) or a folder of images -> ([RGBA frames already fitted to `size`], [duration ms])"""
    from PIL import Image, ImageSequence
    frames, durs = [], []
    if os.path.isdir(src):
        names = sorted(f for f in os.listdir(src) if f.lower().endswith(IMG_EXT))
        if not names: raise ValueError('no images found in %s' % src)
        import re
        names.sort(key=lambda s: [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)])
        for n in names:
            with Image.open(os.path.join(src, n)) as im:
                frames.append(fit_image(im, size, aspect, fit)); durs.append(1000.0 / fps)
    else:
        with Image.open(src) as im:
            for fr in ImageSequence.Iterator(im):
                d = fr.info.get('duration', 0) or 100            # browsers treat 0 / missing GIF delays as 100 ms
                frames.append(fit_image(fr, size, aspect, fit)); durs.append(float(d))
    if not frames: raise ValueError('%s contains no frames' % src)
    return frames, durs


def source_info(src, fps=15.0):
    """(frame count, total duration in ms) of an animated image or image folder, without decoding the pixels"""
    from PIL import Image
    if os.path.isdir(src):
        n = sum(1 for f in os.listdir(src) if f.lower().endswith(IMG_EXT))
        return n, n * 1000.0 / fps
    with Image.open(src) as im:
        n = getattr(im, 'n_frames', 1); total = 0.0
        for k in range(n):
            im.seek(k); total += float(im.info.get('duration', 0) or 100)
    return n, total


def resample(frames, durs, n):
    """pick n frames spread evenly over the source's duration (nearest earlier frame)"""
    if n == len(frames): return list(frames)
    total = sum(durs); ends = []; t = 0.0
    for d in durs: t += d; ends.append(t)
    out = []; j = 0
    for k in range(n):
        tt = (k + 0.5) * total / n
        while j < len(ends) - 1 and ends[j] <= tt: j += 1
        out.append(frames[j])
    return out


def count_for(mode, old_n, n_src, total_ms, fps):
    """output frame count: mode 'original' | 'timing' (source duration x pck fps) | 'frames' (one per source frame) | int"""
    if isinstance(mode, int): n = mode
    elif mode == 'original': n = old_n
    elif mode == 'timing': n = int(round(total_ms / 1000.0 * fps))
    elif mode == 'frames': n = n_src
    else: raise ValueError('unknown length mode %r' % (mode,))
    return max(1, min(0xFFFF, n))


def target_count(mode, old_n, frames, durs, fps):
    return count_for(mode, old_n, len(frames), sum(durs), fps)


# ----------------------------------------------------------------------------------------------------------------------
# writing
# ----------------------------------------------------------------------------------------------------------------------
def quantize_opaque(rgb, ncol=256, dither=False):
    """RGB image -> (index bytes, list of ncol (r, g, b)).  Median cut; with dither the pixels are then re-mapped onto that palette with
    Floyd-Steinberg (Pillow ignores `dither` unless a palette is supplied, so this takes two passes)."""
    from PIL import Image
    q = rgb.quantize(colors=ncol, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    pal = q.getpalette()[:3 * ncol]; pal += [0] * (3 * ncol - len(pal)); idx = q.tobytes()
    if dither:
        padded = pal + pal[3 * (ncol - 1):3 * ncol] * (256 - ncol)        # unused slots repeat the last colour, never a new one
        pimg = Image.new('P', (1, 1)); pimg.putpalette(padded)
        idx = bytes(min(i, ncol - 1) for i in rgb.quantize(palette=pimg, dither=Image.Dither.FLOYDSTEINBERG).tobytes())
    return idx, [(pal[3 * i], pal[3 * i + 1], pal[3 * i + 2]) for i in range(ncol)]


def quantize_frame(im, dither=False):
    """RGBA texture image -> (indices, 256 palette entries (r, g, b, ps2 alpha 0..128))"""
    from PIL import Image
    a = im.getchannel('A')
    if a.getextrema()[0] < 250:                                  # transparent pixels: octree quantiser keeps the alpha channel
        q = im.quantize(colors=256, method=Image.Quantize.FASTOCTREE)
        pal = q.getpalette()[:768]; pal += [0] * (768 - len(pal)); idx = q.tobytes(); acc = {}
        for i, av in zip(idx, a.tobytes()): acc.setdefault(i, []).append(av)
        cols = []
        for i in range(256):
            v = acc.get(i); al = round(sum(v) / len(v)) if v else 0
            cols.append((pal[3 * i], pal[3 * i + 1], pal[3 * i + 2], min(128, round(al / 2))))
        return idx, cols
    idx, cols = quantize_opaque(im.convert('RGB'), 256, dither)
    return idx, [(r, g, b, 128) for r, g, b in cols]


def _texture_tail(p):
    """the last texture of the file's texture chain (every later texture links to its predecessor, the first links to the game's global)"""
    nexts = {}
    for i in range(1, p.count):
        a = p.ptrs[i]
        if not a: continue
        t = p.u8(a)
        if t == 4:
            tex = p.bitmap_parts(a)['tex']; nexts[tex] = p.u32(tex + 4)
        elif t == 5:
            for sh in sheets_of(p, a):
                sp = sh['addr']; A = p.u32(sp)
                for pg in range(max(1, p.u32(sp + 0x10))):
                    tex = p.u32(p.u32(A + 4 * pg) + 0x94); nexts[tex] = p.u32(tex + 4)
    tails = [t for t in nexts if t not in set(nexts.values())]
    if len(tails) != 1:
        raise RuntimeError('cannot determine the end of the texture chain (%d candidates)' % len(tails))
    return tails[0]


def _clone_shape(p, tpl_idx, old_bmp, new_bmp):
    a = p.ptrs[tpl_idx]; nf = p.u16(a + 0x14)
    ft, dl = p.u32(a + 8), p.u32(a + 0x10); q = dl
    while True:
        op = p.u16(q)
        if op == 0: q += 2; break
        q += 4 if op in (4, 5) else 4 + 4 * p.u16(q + 2)
    dll = q - dl
    ft_off = (0x20 + dll + 15) & ~15; st_off = ft_off + 0x28 * nf; total = (st_off + 0x10 + 15) & ~15
    A = p.alloc(b'\xCD' * total, 16); o = A - p.base
    hdr = bytearray(p.raw(a, 0x18)); struct.pack_into('<III', hdr, 8, A + ft_off, A + st_off, A + 0x20)
    p.d[o:o + 0x18] = hdr
    p.d[o + 0x20:o + 0x20 + dll] = p.raw(dl, dll)
    fills = bytearray(p.raw(ft, 0x28 * nf))
    for k in range(nf):
        if struct.unpack_from('<I', fills, 0x28 * k + 0x20)[0] == old_bmp: struct.pack_into('<I', fills, 0x28 * k + 0x20, new_bmp)
    p.d[o + ft_off:o + ft_off + len(fills)] = fills
    return A


def _retime(p, sprite, shapes):
    """make `sprite` play `shapes[k]` in frame k (one Place on depth 1 per frame, like the original clips)"""
    a = p.ptrs[sprite]; old_n = p.u32(a + 0x10); fa = p.u32(a + 0xC); n = len(shapes)
    heads = p.frames_of(fa, old_n)
    if n <= old_n:
        for k in range(n):
            if p.u16(heads[k] + 0xE) != shapes[k]: raise RuntimeError('sprite %d frame %d does not show the expected shape' % (sprite, k))
        p.put(a + 8, '<H', n); p.put(a + 0x10, '<I', n); return
    tpl = bytes(p.raw(heads[1], 0x18))
    if p.u32(heads[1] + 0x10) or p.u32(heads[1] + 0x14): raise RuntimeError('unexpected matrix/colour transform on frame 1 of sprite %d' % sprite)
    ent = bytearray()
    for k in range(n):
        if k < old_n and p.u16(heads[k] + 0xE) == shapes[k]:
            head = heads[k]
        else:
            raw = bytearray(tpl if k >= old_n else p.raw(heads[k], 0x18))
            struct.pack_into('<I', raw, 4, 0); struct.pack_into('<H', raw, 0xE, shapes[k])
            head = p.alloc(bytes(raw), 4)
        ent += struct.pack('<II', p.u32(fa + 8 * k) if k < old_n else 0, head)
    ta = p.alloc(bytes(ent), 4)
    p.put(a + 0xC, '<I', ta); p.put(a + 8, '<H', n); p.put(a + 0x10, '<I', n)


def _set_alpha(p, cmd, alpha):
    xp = p.u32(cmd + 0x14)
    cx = bytearray(p.raw(xp, 8)) if xp else bytearray([64, 64, 64, 64, 0, 0, 0, 0])
    cx[3] = max(0, min(255, int(round(alpha * 64))))
    p.put(cmd + 0x14, '<I', p.alloc(bytes(cx), 4))


def replace_animation(p, anim, images, opacity=None, dither=False, log=print):
    """Replace `anim` (from find_animations(p)) by `images` (RGBA frames, already the texture size).  Modifies p in place."""
    if anim['unsupported']:
        raise ValueError('sprite %d cannot be replaced: %s' % (anim['sprite'], anim['unsupported']))
    tw, th = anim['tex']; old_n, n = anim['nframes'], len(images)
    if not 1 <= n <= 0xFFFF: raise ValueError('frame count %d is out of range' % n)
    if any(im.size != (tw, th) for im in images): raise ValueError('frames must be %dx%d' % (tw, th))
    q = [quantize_frame(im, dither) for im in images]
    for k in range(min(n, old_n)):
        bitmap_encode_into(p, anim['bitmaps'][k], q[k][0], q[k][1])
    log('patched %d bitmap(s) in place' % min(n, old_n))
    shapes = list(anim['shapes'][:n])
    if n > old_n:
        b0 = p.bitmap_parts(anim['bitmaps'][0])
        tpl = dict(tex=p.raw(b0['tex'], 0x60), info=p.raw(b0['info'], 0xA0), palhdr=p.raw(b0['pal'], 0x10))
        chain = dict(last=_texture_tail(p)); new_objs = []; base = p.count; before = len(p.d)
        for k in range(old_n, n):
            bm = BM.emit_bitmap(p, tpl, tw, th, q[k][0], q[k][1], chain, (tw, th))
            sh = _clone_shape(p, anim['shapes'][0], anim['bitmaps'][0], bm)
            shapes.append(base + len(new_objs) + 1); new_objs += [bm, sh]
        la = p.alloc(struct.pack('<%dI' % (len(p.ptrs) + len(new_objs)), *(p.ptrs + new_objs)), 4)
        p.put(p.base + 0x10, '<I', la); p.put(p.base + 0x2A, '<H', len(p.ptrs) + len(new_objs)); p.reload_root()
        log('appended %d frame(s): %d bytes' % (n - old_n, len(p.d) - before))
    _retime(p, anim['sprite'], shapes)
    for l in anim['linked']:
        ln = max(1, min(n, n - (old_n - l['nframes'])))
        _retime(p, l['sprite'], shapes[:ln])
        log('sprite %d (shares the frames): %d -> %d frames' % (l['sprite'], l['nframes'], ln))
    if opacity is not None:
        for pl in anim['placements']: _set_alpha(p, pl['cmd'], opacity)
        log('opacity set to %d%% on %d placement(s)' % (round(opacity * 100), len(anim['placements'])))
    while len(p.d) % 16: p.d.append(0xCD)
    return n


def replace(pck, out, sprite, src, fit='cover', length='original', opacity=None, dither=False, fps=None, log=print):
    """Library entry point: read `pck`, replace animation `sprite` with the image/folder `src`, write `out`.
    length: 'original' (same frame count), 'timing' (source duration x the pck's frame rate), 'frames' (one per source frame) or a number.
    opacity: None keeps the original, else 0..1.  fps: frame rate for folders of images (default: the pck's)."""
    p = Pck(pck); anims = find_animations(p); anim = pick(anims, sprite)
    if anim['unsupported']: raise ValueError('sprite %d cannot be replaced: %s' % (anim['sprite'], anim['unsupported']))
    log(describe(anim))
    frames, durs = load_source(src, anim['tex'], anim['aspect'], fit, fps or anim['fps'])
    n = target_count(length, anim['nframes'], frames, durs, anim['fps'])
    log('source: %d frame(s), %.2f s  ->  %d frame(s) at %.4g fps (%.2f s)' % (len(frames), sum(durs) / 1000.0, n, anim['fps'], n / anim['fps']))
    size0 = len(p.d) + 0x80
    replace_animation(p, anim, resample(frames, durs, n), opacity, dither, log)
    data = p.tobytes()
    with open(out, 'wb') as f: f.write(data)
    log('wrote %s (%d bytes, %+d vs the original)' % (out, len(data), len(data) - size0))
    return dict(frames=n, size=len(data), delta=len(data) - size0)


def estimate_growth(anim, n):
    """approximate extra bytes for n frames"""
    per = 0x20 + 0x60 + anim['tex'][0] * anim['tex'][1] + 0x30 + 0xA0 + 0x410 + 0xA0 + 0x18 + 8 + 4
    return max(0, n - anim['nframes']) * per


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pck'); sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('list', help='list the animations in a pck')
    e = sub.add_parser('export', help='export an animation as GIF (or a folder of PNGs with --frames)')
    e.add_argument('out'); e.add_argument('--sprite', type=int); e.add_argument('--frames', action='store_true'); e.add_argument('--raw', action='store_true', help='keep the 64x64 texture size (default: scaled to the on-screen aspect)')
    r = sub.add_parser('replace', help='replace an animation with a GIF/APNG/WebP or a folder of images')
    r.add_argument('src'); r.add_argument('out'); r.add_argument('--sprite', type=int)
    r.add_argument('--fit', choices=['cover', 'contain', 'stretch'], default='cover')
    r.add_argument('--length', default='original', help="'original' (default), 'timing', 'frames' or a frame count")
    r.add_argument('--opacity', type=float, help='0-100 (%%); default keeps the original')
    r.add_argument('--dither', action='store_true'); r.add_argument('--fps', type=float, help='frame rate for folders of images')
    a = ap.parse_args()
    if a.cmd == 'list':
        for an in find_animations(Pck(a.pck)): print(describe(an))
    elif a.cmd == 'export':
        if a.frames: export_frames(a.pck, a.sprite, a.out)
        else: export_gif(a.pck, a.sprite, a.out, scale=not a.raw)
    else:
        ln = int(a.length) if a.length.isdigit() else a.length
        replace(a.pck, a.out, a.sprite, a.src, a.fit, ln, None if a.opacity is None else a.opacity / 100.0, a.dither, a.fps)


if __name__ == '__main__':
    main()
