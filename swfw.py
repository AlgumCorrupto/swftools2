"""Minimal SWF (v7) writer helpers."""
import struct, zlib

class Bits:
    def __init__(s): s.b = bytearray(); s.cur = 0; s.n = 0
    def ub(s, v, nb):
        for i in range(nb - 1, -1, -1):
            s.cur = (s.cur << 1) | ((v >> i) & 1); s.n += 1
            if s.n == 8: s.b.append(s.cur); s.cur = 0; s.n = 0
    def sb(s, v, nb): s.ub(v & ((1 << nb) - 1), nb)
    def align(s):
        while s.n: s.ub(0, 1)
    def bytes(s): s.align(); return bytes(s.b)

def sbits(*vals):
    m = 0
    for v in vals: m = max(m, v if v >= 0 else ~v)
    return m.bit_length() + 1

def rect(xmin, xmax, ymin, ymax):
    nb = sbits(xmin, xmax, ymin, ymax); b = Bits(); b.ub(nb, 5)
    for v in (xmin, xmax, ymin, ymax): b.sb(v, nb)
    return b.bytes()

def fx16(f): return int(round(f * 65536))

def matrix(a=1.0, b=0.0, c=0.0, d=1.0, tx=0, ty=0):
    """SWF MATRIX; x' = a x + c y + tx, y' = b x + d y + ty; tx,ty in twips (ints)"""
    w = Bits()
    if abs(a - 1) > 1e-6 or abs(d - 1) > 1e-6:
        w.ub(1, 1); nb = sbits(fx16(a), fx16(d)); w.ub(nb, 5); w.sb(fx16(a), nb); w.sb(fx16(d), nb)
    else: w.ub(0, 1)
    if abs(b) > 1e-9 or abs(c) > 1e-9:
        w.ub(1, 1); nb = sbits(fx16(b), fx16(c)); w.ub(nb, 5); w.sb(fx16(b), nb); w.sb(fx16(c), nb)
    else: w.ub(0, 1)
    nb = sbits(int(tx), int(ty)); w.ub(nb, 5); w.sb(int(tx), nb); w.sb(int(ty), nb)
    return w.bytes()

def cxform_alpha(mult, add):
    """mult: 4 ints in 8.8 (256=1.0); add: 4 ints"""
    w = Bits(); w.ub(1, 1); w.ub(1, 1)
    nb = sbits(*mult, *add); w.ub(nb, 4)
    for v in mult: w.sb(v, nb)
    for v in add: w.sb(v, nb)
    return w.bytes()

def tag(code, payload):
    if len(payload) < 63: return struct.pack('<H', (code << 6) | len(payload)) + payload
    return struct.pack('<HI', (code << 6) | 63, len(payload)) + payload

def swf_file(version, frame_rect, rate88, nframes, tags, compress=False):
    body = frame_rect + struct.pack('<HH', rate88, nframes) + b''.join(tags) + tag(0, b'')
    ln = 8 + len(body)
    if compress: return b'CWS' + bytes([version]) + struct.pack('<I', ln) + zlib.compress(body, 9)
    return b'FWS' + bytes([version]) + struct.pack('<I', ln) + body

# ---- affine helpers: (a,b,c,d,tx,ty), x'=a x+c y+tx ; y'=b x+d y+ty
def a_mul(m, n):  # m after n
    a, b, c, d, tx, ty = m; A, B, C, D, TX, TY = n
    return (a * A + c * B, b * A + d * B, a * C + c * D, b * C + d * D, a * TX + c * TY + tx, b * TX + d * TY + ty)
def a_inv(m):
    a, b, c, d, tx, ty = m; det = a * d - b * c
    if abs(det) < 1e-12: raise ZeroDivisionError('singular matrix')
    ia, ib, ic, id_ = d / det, -b / det, -c / det, a / det
    return (ia, ib, ic, id_, -(ia * tx + ic * ty), -(ib * tx + id_ * ty))

def shape_records(runs):
    """runs: list of dict(fill=<FILLSTYLE bytes|None>, line=<LINESTYLE bytes|None>, polys=[(pts, closed)]).
    Each run starts with a NewStyles record so draw order == run order. Returns SHAPE records bytes."""
    w = Bits(); cx = cy = 0
    for run in runs:
        nf = 1 if run['fill'] else 0; nl = 1 if run['line'] else 0
        # NewStyles record that also resets all three style indices to 0 (they are read with the previous NumFillBits /
        # NumLineBits, which the shape header sets to 1): players like Ruffle keep the old index across NewStyles, and an index
        # left over from the previous run would point past the new, possibly empty, style arrays.
        w.ub(0, 1); w.ub(1, 1); w.ub(1, 1); w.ub(1, 1); w.ub(1, 1); w.ub(0, 1)   # type, NewStyles, Line, Fill1, Fill0, no MoveTo
        w.ub(0, 1); w.ub(0, 1); w.ub(0, 1)                                        # FillStyle0 = FillStyle1 = LineStyle = 0
        w.align()
        w.b += bytes([nf]) + (run['fill'] if nf else b'') + bytes([nl]) + (run['line'] if nl else b'')
        w.ub(1, 4); w.ub(1, 4)     # NumFillBits, NumLineBits (always 1 bit: index 0/1, so the explicit resets below fit)
        for pts, closed in run['polys']:
            if len(pts) < 2: continue
            area = sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts)))
            if run.get('oriented'): area = 1      # loops already wound interior-right (holes oppositely): always FillStyle1
            x0, y0 = pts[0]
            w.ub(0, 1); w.ub(0, 1)                    # type, NewStyles
            # Line, Fill1, Fill0, MoveTo: always set all three style indices (0 = none). Players such as Ruffle keep the
            # previous index across a NewStyles record, so a stale index into the new (possibly empty) style array crashes them.
            w.ub(1, 1); w.ub(1, 1); w.ub(1, 1); w.ub(1, 1)
            nb = sbits(x0, y0); w.ub(nb, 5); w.sb(x0, nb); w.sb(y0, nb)
            f1, f0 = ((1, 0) if area >= 0 else (0, 1)) if nf else (0, 0)
            w.ub(f0, 1); w.ub(f1, 1); w.ub(1 if nl else 0, 1)
            cx, cy = x0, y0
            seq = list(pts[1:]) + ([pts[0]] if closed else [])
            for x, y in seq:
                dx, dy = x - cx, y - cy
                if dx == 0 and dy == 0: continue
                nb = max(2, sbits(dx, dy)); w.ub(1, 1); w.ub(1, 1); w.ub(nb - 2, 4); w.ub(1, 1); w.sb(dx, nb); w.sb(dy, nb)
                cx, cy = x, y
    w.ub(0, 6)
    return w.bytes()


def glyph_shape(segs):
    """SHAPE for a font glyph (1 fill bit, 0 line bits). segs: list of ('M',x,y) ('L',x,y) ('Q',cx,cy,x,y), integer em-grid coords.
    Contours must be oriented so the interior is on the right (caller normalises)."""
    w = Bits(); w.ub(1, 4); w.ub(0, 4)
    cx = cy = 0
    for s in segs:
        if s[0] == 'M':
            w.ub(0, 1); w.ub(0, 1); w.ub(0, 1); w.ub(1, 1); w.ub(0, 1); w.ub(1, 1)   # style change: fill1 + moveto
            nb = sbits(s[1], s[2]); w.ub(nb, 5); w.sb(s[1], nb); w.sb(s[2], nb); w.ub(1, 1)   # FillStyle1 = 1
            cx, cy = s[1], s[2]
        elif s[0] == 'L':
            dx, dy = s[1] - cx, s[2] - cy
            if dx == 0 and dy == 0: continue
            nb = max(2, sbits(dx, dy)); w.ub(1, 1); w.ub(1, 1); w.ub(nb - 2, 4); w.ub(1, 1); w.sb(dx, nb); w.sb(dy, nb)
            cx, cy = s[1], s[2]
        else:
            c1x, c1y, ax, ay = s[1] - cx, s[2] - cy, s[3] - s[1], s[4] - s[2]
            nb = max(2, sbits(c1x, c1y, ax, ay)); w.ub(1, 1); w.ub(0, 1); w.ub(nb - 2, 4)
            for v in (c1x, c1y, ax, ay): w.sb(v, nb)
            cx, cy = s[3], s[4]
    w.ub(0, 6)
    return w.bytes()
