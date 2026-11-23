import sys, glob, struct, io
sys.path.insert(0, '/home/claude/w/tool')
from rpck import *; import swfr, swf2pck, pck2swf
from PIL import Image, ImageDraw
def raster(sh, W_=160, H_=120, fills=None):
    # coverage mask per fill index (union), world twips -> pixel
    pts = [pt for op, v in sh['ops'] if op in (1, 2) for pt in v]
    if not pts: return None
    xs = [x for x, y in pts]; ys = [y for x, y in pts]; x0, x1, y0, y1 = min(xs), max(xs) + 1, min(ys), max(ys) + 1
    sx = (W_ - 1) / (x1 - x0); sy = (H_ - 1) / (y1 - y0); masks = {}
    cur = 1
    for op, v in sh['ops']:
        if op == 4: cur = v
        elif op == 2:
            m = masks.setdefault(cur, Image.new('1', (W_, H_), 0)); ImageDraw.Draw(m).polygon([((x - x0) * sx, (y - y0) * sy) for x, y in v], fill=1)
    return masks, (x0, x1, y0, y1)
tot = {}
for f in ('legal', 'loadscreen_career_tokyo_tkbclass1', 'ds_vp_cad16_04', 'vs_vp_delegance_03'):
    swf = open(f'/home/claude/w/{f}.swf', 'rb').read()
    out, changed, warn = swf2pck.convert(swf, None, log=lambda m: print('  ', m), force=True)
    a = Pck(open(f'/mnt/user-data/uploads/{f}.pck', 'rb').read()); b = Pck(out)
    bad = []; iou_min = 1.0; nsh = 0
    assert b.count == a.count and b.fc == a.fc
    # semantic comparison through pck2swf
    ca = pck2swf.Conv(a); cb = pck2swf.Conv(b)
    for i in range(1, a.count):
        t = a.otype(i)
        if t != b.otype(i): bad.append((i, 'type')); continue
        if t == 2:
            if swfr.parse_sprite(pck2swf.payload_of(ca.sprite(i)[0])) != swfr.parse_sprite(pck2swf.payload_of(cb.sprite(i)[0])): bad.append((i, 'sprite'))
        elif t == 1:
            nsh += 1
            sa, sb = a.read_shape(a.ptrs[i]), b.read_shape(b.ptrs[i])
            ra, rb = raster(sa), raster(sb)
            if (ra is None) != (rb is None): bad.append((i, 'empty mismatch')); continue
            if ra is None: continue
            # compare union coverage and per-style fill color
            ma = Image.new('1', ra[0][next(iter(ra[0]))].size, 0) if ra[0] else None
            ua = Image.new('1', (160, 120), 0); ub = Image.new('1', (160, 120), 0)
            for m in ra[0].values(): ua.paste(1, mask=m)
            for m in rb[0].values(): ub.paste(1, mask=m)
            import numpy as np
            A = np.array(ua); B = np.array(ub); inter = (A & B).sum(); un = (A | B).sum()
            iou = inter / un if un else 1.0; iou_min = min(iou_min, iou)
            if iou < 0.95: bad.append((i, 'shape iou %.3f' % iou))
            fa = sorted((x['flag'], x['color'], x['bitmap'] != 0) for x in sa['fills']); fb = sorted((x['flag'], x['color'], x['bitmap'] != 0) for x in sb['fills'])
            if not set(fb) <= set(fa): bad.append((i, 'fill style set differs'))
        elif t == 4:
            if bitmap_decode(a, a.ptrs[i])[:4] != bitmap_decode(b, b.ptrs[i])[:4]: bad.append((i, 'bitmap'))
        else:
            if ca.__class__ and (a.raw(a.ptrs[i], 8) != b.raw(b.ptrs[i], 8)): bad.append((i, 'header'))
    ra = [[a.read_cmd(c) for c in a.cmds(h)] for h in a.frames_of(a.frames, a.fc)]
    rb = [[b.read_cmd(c) for c in b.cmds(h)] for h in b.frames_of(b.frames, b.fc)]
    strip = lambda fr: [[{k: v for k, v in c.items() if k not in ('addr', 'flags')} for c in f] for f in fr]
    if strip(ra) != strip(rb): bad.append((0, 'root'))
    print(f, 'objects', a.count - 1, 'shapes', nsh, 'min shape IoU %.3f' % iou_min, 'size %d -> %d' % (len(a.d), len(b.d)), 'MISMATCHES', bad[:10], len(bad))
