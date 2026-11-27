#!/usr/bin/env python3
"""swf2pck: apply an (edited) SWF back onto a PS2 MC3/RAGE swf .pck.

The SWF produced by pck2swf embeds its source pck; by default that is the template.
Objects whose SWF definition is unchanged are left byte-for-byte alone (so fonts, buttons and
other engine-only data survive).  Changed/new SHAPE, SPRITE, root timeline are re-emitted in an
append-only arena; changed BITMAPs of the same size are patched in place."""
import sys, struct, json, zlib, base64, hashlib
from rpck import *
import swfr, swfw as W, tess
from pck2swf import Conv, payload_of

def clamp(v, lo, hi): return max(lo, min(hi, v))

class Writer:
    def __init__(s, p): s.p = p; s.warn = []

    # ---- low level ----
    def code(s, actions):
        if not actions or actions[-1] != 0: actions += b'\x00'
        return s.p.alloc(b'\x01\x00' + actions, 4)

    def pmatrix(s, m):
        a, b, c, d, tx, ty = m
        sx, sy, r0, r1 = (clamp(round(v * 256), -32768, 32767) for v in (a, d, b, c))
        tx, ty = clamp(int(tx), -32768, 32767), clamp(int(ty), -32768, 32767)
        if r0 or r1: return 3, s.p.alloc(struct.pack('<hhhhhh', sx, sy, r0, r1, tx, ty), 4)
        if sx != 256 or sy != 256: return 1, s.p.alloc(struct.pack('<hhhh', sx, sy, tx, ty), 4)
        return 0, s.p.alloc(struct.pack('<hh', tx, ty), 4)

    def cxform(s, cx):
        mult, add = cx
        return s.p.alloc(bytes([clamp(round(m/4), -128, 127) & 255 for m in mult] + [clamp(round(a/4), -128, 127) & 255 for a in add]), 4)

    def cmd_list(s, items):
        p = s.p; head = prev = 0
        for kind, d in items:
            if kind == 'place':
                typ1 = d['name'] is not None or d['events']
                f1, mp = (s.pmatrix(d['matrix']) if d['matrix'] else (0xCD, 0))
                xp = s.cxform(d['cxform']) if d['cxform'] else 0
                char = d['char'] if d['char'] is not None else 0xFFFF
                if not typ1:
                    a = p.alloc(struct.pack('<BBBBIIHHII', 0, f1, 0xCD, 0xCD, 0, CMD_VT[0], d['depth'], char, mp, xp), 4)
                else:
                    evs = d['events'] or []; arr = 0; allf = 0
                    if evs:
                        blob = b''
                        for fl, act, key in evs:
                            cp = s.code(bytearray(act)); allf |= fl; blob += struct.pack('<III', fl, cp, key)
                        arr = p.alloc(blob, 4)
                    nm = p.alloc(d['name'].encode('latin-1') + b'\0', 4) if d['name'] is not None else 0
                    a = p.alloc(struct.pack('<BBBBIIHHIIIIII', 1, f1, 0xCD, 0xCD, 0, CMD_VT[1], d['depth'], char, mp, xp, allf, len(evs), arr, nm), 4)
            elif kind == 'remove':
                a = p.alloc(struct.pack('<BBHII', 2, 0xCD, d, 0, CMD_VT[2]), 4)
            else:
                cp = s.code(bytearray(d))
                a = p.alloc(struct.pack('<BBBBIII', 3, 0xCD, 0xCD, 0xCD, 0, CMD_VT[3], cp), 4)
            if prev: p.put(prev + 4, '<I', a)
            else: head = a
            prev = a
        return head

    def timeline(s, frames, bg=0):
        heads = [s.cmd_list(f) for f in frames]
        return s.p.alloc(b''.join(struct.pack('<II', bg if k == 0 else 0, h) for k, h in enumerate(heads)), 4), len(frames)

    def sprite(s, frames):
        fa, n = s.timeline(frames)
        return s.p.alloc(struct.pack('<B3xIHxxIII', 2, VT[2], n, fa, n, 0) + b'\xcd' * 12, 16)

    # ---- shapes ----
    def shape(s, sh):
        p = s.p; fills = []; strokes = []; dl = bytearray()
        def fkey(f): return json.dumps(f, sort_keys=True, default=list)
        def fidx(f):
            k = fkey(f)
            for i, (kk, _) in enumerate(fills):
                if kk == k: return i + 1
            fills.append((k, f)); return len(fills)
        def sidx(st):
            if st not in strokes: strokes.append(st)
            return strokes.index(st) + 1
        for g in sh['groups']:
            if tess.SIMPLIFY > 0 and not g.get('_simp'): g['edges'] = tess.simplify_edges(g['edges']); g['_simp'] = True
            for si in range(1, len(g['fills']) + 1):
                fe = []
                for kind, pts, f0, f1, ln in g['edges']:
                    ps = [pts[0]] + (pts[1:] if kind == 'l' else tess.flatten_q(*pts))
                    seg = list(zip(ps[:-1], ps[1:]))
                    if f1 == si: fe += seg
                    if f0 == si: fe += [(b, a) for a, b in seg]
                if not fe: continue
                loops = tess.stitch([((round(a[0]), round(a[1])), (round(b[0]), round(b[1]))) for a, b in fe])
                tris = tess.triangulate(tess.drop_specks(loops))
                if not tris: continue
                dl += struct.pack('<HH', 4, fidx(s.fillspec(g['fills'][si - 1])))
                for t in tris:
                    dl += struct.pack('<HH', 2, len(t))
                    for x, y in t: dl += struct.pack('<hh', clamp(x, -32768, 32767), clamp(y, -32768, 32767))
            for color, tris in g.get('stroke_tris', []):
                dl += struct.pack('<HH', 4, fidx(dict(flag=0, color=list(color), matrix=[1, 0, 0, 1, 0, 0], bitmap=0, grad=None)))
                for tri in tris:
                    dl += struct.pack('<HH', 2, len(tri))
                    for x, y in tri: dl += struct.pack('<hh', clamp(x, -32768, 32767), clamp(y, -32768, 32767))
            for li in range(1, len(g['lines']) + 1 if 'stroke_tris' not in g else 1):
                ls = g['lines'][li - 1]; chain = []
                for kind, pts, f0, f1, ln in g['edges']:
                    if ln != li: continue
                    ps = [pts[0]] + (pts[1:] if kind == 'l' else tess.flatten_q(*pts))
                    if chain and chain[-1] == ps[0]: chain += ps[1:]
                    else:
                        if len(chain) > 1: dl_chain(dl, chain, strokes, ls, sidx)
                        chain = list(ps)
                if len(chain) > 1: dl_chain(dl, chain, strokes, ls, sidx)
        dl += struct.pack('<H', 0)
        dla = p.alloc(bytes(dl) + b'\0\0', 4)
        # gradients first (pointers needed in fill table)
        ft = bytearray()
        for k, f in fills:
            gp = 0; bp = 0; flag = f['flag']
            if f.get('grad'):
                g = f['grad']; n = len(g['stops'])
                ra = p.alloc(bytes(r for r, c in g['stops']) + b'\0' * 3, 4); ca = p.alloc(b''.join(bytes(c) for r, c in g['stops']), 4)
                gp = p.alloc(struct.pack('<6fH2xII', *g['matrix'], n, ra, ca), 4)
            if f.get('bitmap'): bp = s.bitmap_info(f['bitmap'])[0]
            ft += struct.pack('<B3x4B6fII', flag, *f['color'], *f['matrix'], bp, gp)
        fa = p.alloc(bytes(ft), 4) if ft else p.alloc(b'', 4)
        st = b''.join(struct.pack('<HH4B', w, 0, *c) for w, c in strokes)
        sa = p.alloc(st, 4)
        return p.alloc(struct.pack('<B3xIIIIHH', 1, VT[1], fa, sa, dla, len(fills), len(strokes)) + b'\xcd' * 8, 16)

    def bitmap_info(s, bi):
        """-> (object address, width, height) used for UV matrices, or None. Overridden by swfimport (original SWF dimensions)."""
        p = s.p
        if bi < len(p.ptrs) and p.ptrs[bi] and p.otype(bi) == 4:
            bp = p.bitmap_parts(p.ptrs[bi]); return p.ptrs[bi], bp['tw'], bp['th']
        return None

    def fillspec(s, f):
        p = s.p; t = f['type']
        if t == 0: return dict(flag=0, color=list(f['color']), matrix=[1, 0, 0, 1, 0, 0], bitmap=0, grad=None)
        if t in (0x10, 0x12):
            if t == 0x12: s.warn.append('radial gradient approximated as linear')
            G = W.a_inv(f['matrix'])
            ratios = [(r, c) for r, c in f['stops']]
            return dict(flag=0x10, color=[255, 255, 255, 255], matrix=[1, 0, 0, 1, 0, 0], bitmap=0,
                        grad=dict(matrix=list(G), stops=[(r, list(c)) for r, c in ratios]))
        bi = f['bitmap']; info = s.bitmap_info(bi)
        if info is None:
            s.warn.append('fill references bitmap %d which is not an engine bitmap; using white' % bi)
            return dict(flag=0, color=[255, 255, 255, 255], matrix=[1, 0, 0, 1, 0, 0], bitmap=0, grad=None)
        w, h = info[1], info[2]; su, sv = (info[3], info[4]) if len(info) > 3 else (1.0, 1.0)
        F = W.a_inv(f['matrix'])
        Me = W.a_mul((1 / w, 0, 0, 1 / h, 0, 0), W.a_mul(F, (20, 0, 0, 20, 0, 0)))
        if (su, sv) != (1.0, 1.0): Me = W.a_mul((su, 0, 0, sv, 0, 0), Me)     # image occupies the top-left su x sv of the texture
        return dict(flag=0x41 if t in (0x41, 0x43) else 0x40, color=[255, 255, 255, 255], matrix=list(Me), bitmap=bi, grad=None)

def dl_chain(dl, chain, strokes, ls, sidx):
    st = (max(1, ls['width']), tuple(ls['color']))
    dl += struct.pack('<HH', 5, sidx(st)) + struct.pack('<HH', 1, len(chain))
    for x, y in chain: dl += struct.pack('<hh', clamp(round(x), -32768, 32767), clamp(round(y), -32768, 32767))

# ---------------------------------------------------------------------------------------
def quantize(px, w, h, ncol):
    from PIL import Image
    im = Image.new('RGBA', (w, h)); im.putdata([tuple(c) for c in px])
    q = im.quantize(colors=ncol, method=Image.Quantize.FASTOCTREE)
    pal = q.getpalette()[:3 * ncol]; pal += [0] * (3 * ncol - len(pal))
    idx = bytes(q.tobytes())
    cols = [[pal[3 * i], pal[3 * i + 1], pal[3 * i + 2], 0] for i in range(ncol)]
    from collections import defaultdict
    acc = defaultdict(list)
    for i, c in zip(idx, px): acc[i].append(c[3])
    for i, v in acc.items(): cols[i][3] = round(sum(v) / len(v))
    return idx, [tuple(c) for c in cols]

def convert(swf_bytes, template_bytes=None, log=print, force=False):
    swf = swfr.read_swf(swf_bytes); meta = None
    for code, pl in swf['tags']:
        if code == 77 and pl.startswith(b'RPCK1:'):
            meta = json.loads(zlib.decompress(base64.b64decode(pl[6:-1] if pl.endswith(b'\0') else pl[6:])))
    if template_bytes is None:
        if not meta: raise SystemExit('SWF has no embedded source pck; pass --template original.pck')
        template_bytes = zlib.decompress(base64.b64decode(meta['pck']))
    p = Pck(template_bytes); orig = Pck(template_bytes); cv = Conv(orig); W_ = Writer(p)
    prev = bool(meta and meta.get('preview'))
    wrappers = set(meta.get('wrappers', [])) if prev else set()
    clones = {int(k): v for k, v in (meta or {}).get('clones', {}).items()} if prev else {}
    cvp = None
    if prev:
        cvp = Conv(orig, True); cvp.sans = meta['sans']; log('note: preview SWF - undoing clip offset baking')
    def unbake(frames):
        out = []
        for fr in frames:
            nf = []
            for kind, d in fr:
                if kind == 'place' and d['char'] in wrappers:
                    _, _, wf = swfr.parse_sprite(wrapdefs[d['char']]); inner = [dd for k, dd in wf[0] if k == 'place'][0]
                    d = dict(d); d['char'] = inner['char']; d['name'] = inner['name']; d['events'] = inner['events']
                    fl = (0x80 if d['events'] else 0) | (0x20 if d['name'] is not None else 0) | (0x08 if d['cxform'] else 0) | (0x04 if d['matrix'] else 0) | 0x02
                    d['flags'] = fl; d['move'] = False
                while kind == 'place' and d['char'] in clones:      # clones can be clones of clones
                    o, dx, dy, tx, ty, sx, sy, r0, r1 = clones[d['char']]; d = dict(d); d['char'] = o
                    if d['matrix']:
                        a, b, c, dd, mx, my = d['matrix']
                        if (round(a * 256), round(dd * 256), round(b * 256), round(c * 256)) == (sx, sy, r0, r1) and abs(mx) < 1 and abs(my) < 1: mx, my = tx, ty
                        else: mx, my = round(mx + a * dx + c * dy), round(my + b * dx + dd * dy)
                        d['matrix'] = (a, b, c, dd, mx, my)
                nf.append((kind, d))
            out.append(nf)
        return out
    chars = {}   # id -> (code, payload)
    wrapdefs = {}; clonedefs = {}
    for code, pl in swf['tags']:
        if code in (32, 36, 39, 48, 33, 37, 34):
            cid = struct.unpack_from('<H', pl)[0]
            if prev and (cid in clones or cid == meta['sans'] or cid in wrappers):
                if cid in wrappers: wrapdefs[cid] = pl
                if cid in clones and code == 39: clonedefs[cid] = pl
                continue
            chars[cid] = (code, pl)
    TYPE = {32: 1, 36: 4, 39: 2, 48: 5, 33: 6, 37: 7, 34: 3}
    ptrs = list(p.ptrs); changed = []; moved = {}
    def orig_tag(i, t):
        o = {1: cv.shape, 4: cv.bitmap, 5: cv.font, 6: cv.text, 7: cv.edittext, 3: cv.button}
        if t == 2: return cv.sprite(i)[0]
        return o[t](i)
    for i, (code, pl) in sorted(chars.items()):
        t = TYPE[code]
        exists = i < len(ptrs) and ptrs[i]
        if exists and orig.otype(i) != t:
            log('warning: character %d changed type (%s -> %s); skipped' % (i, OBJ_NAMES.get(orig.otype(i)), OBJ_NAMES[t])); continue
        if exists and t in (5, 6, 7): continue      # fonts/text/edit text are carried over unchanged
        if exists:
            try: ot = payload_of(orig_tag(i, t))
            except Exception: ot = None
            if ot == pl and (not force or t in (3, 5, 6, 7)): continue
        try:
            if t == 2:
                _, nf, frames = swfr.parse_sprite(pl)
                if prev: frames = unbake(frames)
                if exists and not force and ot is not None and swfr.parse_sprite(ot)[2] == frames: continue
                new = W_.sprite(frames)
            elif t == 1:
                sh = swfr.parse_shape3(pl)
                if exists and not force and ot is not None and swfr.parse_shape3(ot)['groups'] == sh['groups']: continue
                new = W_.shape(sh)
            elif t == 4:
                if not exists: log('warning: new bitmap %d not supported (needs PS2 GS texture blob); skipped' % i); continue
                b = swfr.parse_bitmap(pl)
                if ot is not None:
                    ob = swfr.parse_bitmap(ot)
                    if ob.get('idx') == b.get('idx') and ob.get('pal') == b.get('pal'): continue
                bp = p.bitmap_parts(ptrs[i])
                if (b['w'], b['h']) != (bp['tw'], bp['th']): log('warning: bitmap %d size changed (%dx%d -> %dx%d); unsupported, skipped' % (i, bp['tw'], bp['th'], b['w'], b['h'])); continue
                if 'px' in b: idx, pal = quantize(b['px'], b['w'], b['h'], bp['ncol'])
                else:
                    idx, pal = b['idx'], b['pal']
                    if len(pal) > bp['ncol'] or max(idx) >= bp['ncol']: log('warning: bitmap %d has too many colors; skipped' % i); continue
                pal = [(r, g, bl, min(128, round(a / 2))) for r, g, bl, a in pal]
                if bp['ttype'] == 6 and max(idx) > 15: log('warning: bitmap %d needs <=16 colors; skipped' % i); continue
                bitmap_encode_into(p, ptrs[i], idx, pal); changed.append((i, 'bitmap patched in place')); continue
            else:
                if exists: log('note: %s %d differs in the SWF but this type is carried over unchanged' % (OBJ_NAMES[t], i))
                else: log('warning: new %s %d not supported; skipped' % (OBJ_NAMES[t], i))
                continue
        except Exception as e:
            log('warning: character %d (%s) failed: %r' % (i, OBJ_NAMES[t], e)); continue
        if exists: moved[ptrs[i]] = new
        while len(ptrs) <= i: ptrs.append(0)
        ptrs[i] = new; changed.append((i, '%s re-emitted' % OBJ_NAMES[t]))
    # edits made inside the shifted clip copies of a preview: undo the shift and apply them to the original sprite
    if prev and clonedefs:
        def ident(m): return m if m else (1.0, 0.0, 0.0, 1.0, 0, 0)
        def norm(frames): return [[(k, dict(d, matrix=ident(d['matrix'])) if k == 'place' else d) for k, d in fr] for fr in frames]
        def unshift(frames, dx, dy):
            out = []
            for fr in frames:
                nf = []
                for k, d in fr:
                    if k == 'place' and d['matrix']:
                        a, b, c, dd, mx, my = d['matrix']; d = dict(d); d['matrix'] = (a, b, c, dd, mx - dx, my - dy)
                    nf.append((k, d))
                out.append(nf)
            return out
        done = {i for i, _ in changed}
        for nid, pl in sorted(clonedefs.items()):
            o, dx, dy = clones[nid][:3]
            if o in done or not (o < len(ptrs) and ptrs[o]): continue
            frames = unshift(unbake(swfr.parse_sprite(pl)[2]), dx, dy)
            try: orig_frames = swfr.parse_sprite(payload_of(cv.sprite(o)[0]))[2]
            except Exception: continue
            if norm(frames) == norm(orig_frames): continue
            new = W_.sprite(frames); moved[ptrs[o]] = new; ptrs[o] = new; changed.append((o, 'sprite re-emitted (edited through its preview copy)')); done.add(o)
    # root timeline
    rt = [(c, pl) for c, pl in swf['tags'] if c in (26, 28, 12, 1)]
    rframes = swfr.parse_timeline(rt)
    if prev: rframes = unbake(rframes)
    oroot = swfr.parse_timeline([(c, payload_of(t)) for c, t in [(struct.unpack_from('<H', t)[0] >> 6, t) for t in cv.timeline([[orig.read_cmd(c) for c in orig.cmds(h)] for h in orig.frames_of(orig.frames, orig.fc)])]])
    bgt = [pl for c, pl in swf['tags'] if c == 9]
    bg = struct.unpack('<I', bytes(bgt[0][:3]) + b'\xff')[0] if bgt else p.u32(p.frames)
    if force or rframes != oroot or bg != orig.u32(p.frames):
        fa, n = W_.timeline(rframes, bg); p.put(0xc + p.base, '<I', fa); p.put(8 + p.base, '<H', n); changed.append((0, 'root timeline re-emitted'))
    rate = swf['rate']
    if rate != p.rate88: p.put(0x28 + p.base, '<H', rate); changed.append((0, 'frame rate'))
    fr = swf['frame']; want = tuple(float(v) / 20 for v in (fr[0], fr[1], fr[2], fr[3]))
    if want != (p.xmin, p.xmax, p.ymin, p.ymax): p.put(0x18 + p.base, '<4f', *want); changed.append((0, 'stage size'))
    # fix button references to moved objects
    if moved:
        for i, a in enumerate(ptrs):
            if a and p.u8(a) == 3:
                for r in range(p.u8(a + 9)):
                    ra = p.u32(a + 0xc) + 0x28 * r + 0x24
                    if p.u32(ra) in moved: p.put(ra, '<I', moved[p.u32(ra)])
    # object list
    if len(ptrs) != p.count or ptrs != p.ptrs:
        if len(ptrs) > p.count:
            la = p.alloc(struct.pack('<%dI' % len(ptrs), *ptrs), 4); p.put(0x10 + p.base, '<I', la); p.put(0x2a + p.base, '<H', len(ptrs))
        else:
            for k, a in enumerate(ptrs): p.put(p.lst + 4 * k, '<I', a)
    return p.tobytes(), changed, W_.warn

def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('swf'); ap.add_argument('pck'); ap.add_argument('--template', help='original .pck (default: embedded in the SWF)')
    ap.add_argument('--force', action='store_true', help='re-emit all shapes/sprites/root even if unchanged (testing)')
    a = ap.parse_args()
    tb = open(a.template, 'rb').read() if a.template else None
    out, changed, warn = convert(open(a.swf, 'rb').read(), tb, force=a.force)
    open(a.pck, 'wb').write(out)
    print('wrote %s (%d bytes); %d change(s)' % (a.pck, len(out), len(changed)))
    for i, w in changed: print('  #%d: %s' % (i, w))
    for w in sorted(set(warn)): print('warning:', w)

if __name__ == '__main__': main()
