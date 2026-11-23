"""Reader for Angel/RAGE .fonttex (glyph metrics) and .tex (textures) used by the PS2/PC MC3 builds."""
import struct, os, glob

def read_fonttex(fn):
    b = open(fn, 'rb').read(); ver = struct.unpack_from('<f', b, 0)[0]; h = struct.unpack_from('<10I', b, 4); o = 44
    n = struct.unpack_from('<I', b, o)[0]; o += 4; gl = []
    for i in range(n):
        ch, x, y, w, hh, yo, xo = struct.unpack_from('<IBBBBbb', b, o); o += 10
        gl.append(dict(ch=ch, x=x, y=y, w=w, h=hh, yo=yo, xo=xo, page=0))
    npg = struct.unpack_from('<I', b, o)[0]; o += 4; ends = struct.unpack_from('<%dI' % npg, b, o)
    start = 0
    for pg, e in enumerate(ends):
        for i in range(start, min(e, n)): gl[i]['page'] = pg
        start = e
    sp = struct.unpack_from('<i', b, 4 + 4)[0]
    return dict(name=os.path.basename(fn)[:-8], height=h[0], spacing=struct.unpack_from('<i', b, 8)[0], space=h[4], glyphs=gl, pages=npg)

def read_tex(fn):
    """-> (w, h, rows of RGBA bytes) for the first mip; supports P8/PA8/P4/PA4/I8/A8I8/A4I4/RGB888/RGBA8888/A1R5G5B5"""
    b = open(fn, 'rb').read(); w, h, t, mips, unk = struct.unpack_from('<HHHHH', b, 0); bits = struct.unpack_from('<I', b, 10)[0]; o = 14
    px = bytearray(w * h * 4)
    def pal(n):
        nonlocal o
        p = [(b[o + 4 * i + 2], b[o + 4 * i + 1], b[o + 4 * i], b[o + 4 * i + 3]) for i in range(n)]; o += 4 * n; return p
    if t in (1, 14):
        p = pal(256)
        for i in range(w * h): px[4 * i:4 * i + 4] = bytes(p[b[o + i]]) if t == 14 else bytes(p[b[o + i]][:3]) + b'\xff'
    elif t in (15, 16):
        p = pal(16)
        for i in range(w * h):
            v = (b[o + (i >> 1)] >> (4 * (i & 1))) & 15; px[4 * i:4 * i + 4] = bytes(p[v]) if t == 16 else bytes(p[v][:3]) + b'\xff'
    elif t == 8:
        for i in range(w * h): px[4 * i:4 * i + 4] = bytes([255, 255, 255, b[o + i]])      # alpha/intensity as alpha mask
    elif t == 9:
        for i in range(w * h): v = b[o + i]; px[4 * i:4 * i + 4] = bytes([(v & 15) * 17] * 3 + [(v >> 4) * 17])
    elif t == 10:
        for i in range(w * h): px[4 * i:4 * i + 4] = bytes([b[o + 2 * i + 1]] * 3 + [b[o + 2 * i]])
    elif t == 17:
        for i in range(w * h): px[4 * i:4 * i + 4] = bytes(b[o + 3 * i:o + 3 * i + 3]) + b'\xff'
    elif t == 18:
        px[:] = b[o:o + 4 * w * h]
    elif t == 6:
        for i in range(w * h):
            v = struct.unpack_from('<H', b, o + 2 * i)[0]; px[4 * i:4 * i + 4] = bytes([((v >> 10) & 31) * 255 // 31, ((v >> 5) & 31) * 255 // 31, (v & 31) * 255 // 31, 255 if v >> 15 else 0])
    else: raise ValueError('unsupported .tex type %d in %s' % (t, fn))
    return w, h, t, bytes(px)


class FontTexGlyphs:
    """Glyph source backed by a .fonttex + its _NN.tex pages: bitmap pixels -> rectilinear vector outlines (em = 1024)."""
    EM = 1024
    def __init__(s, root, name, em_px=None):
        s.ft = read_fonttex(os.path.join(root, name + '.fonttex')); s.name = name; s.ok = True
        s.pages = {}; s.root = root; s.cache = {}
        s.gm = {g['ch']: g for g in s.ft['glyphs']}
        s.em_px = em_px or float(s.ft['height'])
        k = s.EM / s.em_px; s.asc, s.desc = round(s.ft['height'] * k * 0.8), round(s.ft['height'] * k * 0.25)

    def page(s, n):
        if n not in s.pages:
            fn = os.path.join(s.root, 'texture', '%s_%02d.tex' % (s.name, n))
            s.pages[n] = read_tex(fn) if os.path.exists(fn) else None
        return s.pages[n]

    def has(s, code):
        return code in s.gm or (97 <= code <= 122 and code - 32 in s.gm)

    def glyph(s, code):
        if code in s.cache: return s.cache[code]
        k = s.EM / s.em_px
        if code == 32 or code == 0xA0: r = ([], round(s.ft['space'] * k))
        else:
            g = s.gm.get(code) or (s.gm.get(code - 32) if 97 <= code <= 122 else None)
            pg = s.page(g['page']) if g else None
            if not g or not pg: r = None
            else:
                import tess
                w, h, t, px = pg
                def on(x, y):
                    return 0 <= x < g['w'] and 0 <= y < g['h'] and px[4 * ((g['y'] + y) * w + g['x'] + x) + 3] >= 96
                edges = []
                for y in range(g['h']):
                    for x in range(g['w']):
                        if on(x, y):      # clockwise (interior on the right, y down); shared edges cancel in stitch()
                            edges += [((x, y), (x + 1, y)), ((x + 1, y), (x + 1, y + 1)), ((x + 1, y + 1), (x, y + 1)), ((x, y + 1), (x, y))]
                out = []
                for loop in tess.stitch(edges):
                    pts = [loop[0]]
                    for q in loop[1:]:
                        pts.append(q)
                    # drop collinear points
                    m = len(pts); keep = [pts[i] for i in range(m) if (pts[i][0] - pts[i - 1][0]) * (pts[(i + 1) % m][1] - pts[i][1]) != (pts[i][1] - pts[i - 1][1]) * (pts[(i + 1) % m][0] - pts[i][0])]
                    if len(keep) < 3: continue
                    P = [(round((g['xo'] + x) * k), round((g['yo'] + y) * k)) for x, y in keep]
                    out.append(('M', P[0][0], P[0][1]))
                    out += [('L', x, y) for x, y in P[1:]] + [('L', P[0][0], P[0][1])]
                r = (out, round((g['w'] + s.ft['spacing']) * k))
        s.cache[code] = r; return r
