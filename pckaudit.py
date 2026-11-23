#!/usr/bin/env python3
"""pckaudit: check a swf .pck against the invariants every original file satisfies (use on files you generate)."""
import sys, struct
from rpck import *
from sheetfont import sheets_of

GLOBAL_TEX_HEAD = 0x245040      # first texture of every file links here (game global)
GLOBAL_EMPTY_STR = 0x214116     # edit text with empty initial text points at this game global

def audit(fn):
    p = Pck(fn); errs = []; end = p.base + len(p.d)
    def ok(a, what, null_ok=True):
        if a == 0:
            if not null_ok: errs.append('%s is null' % what)
            return
        if a in (GLOBAL_TEX_HEAD, GLOBAL_EMPTY_STR): return
        if not (p.base <= a < end): errs.append('%s -> %#x is outside the file' % (what, a))
    if p.ptrs[0] != 0: errs.append('list[0] != 0')
    for i in range(1, p.count):
        if not p.ptrs[i]: errs.append('object list entry %d is null' % i); continue
        ok(p.ptrs[i], 'object %d' % i)
    ok(p.frames, 'root frames', False)
    def walk(h, where):
        seen = set()
        while h:
            if h in seen: errs.append('%s: command list loops' % where); return
            seen.add(h); ok(h, where + ' cmd')
            t = p.u8(h)
            if t in (0, 1):
                ok(p.u32(h + 0x10), where + ' matrix'); ok(p.u32(h + 0x14), where + ' cxform')
                c = p.u16(h + 0xE)
                if c != 0xFFFF and not (0 < c < p.count and p.ptrs[c]): errs.append('%s: places missing character %d' % (where, c))
                if t == 1:
                    ok(p.u32(h + 0x20), where + ' events'); ok(p.u32(h + 0x24), where + ' name')
                    for k in range(p.u32(h + 0x1C)): ok(p.u32(p.u32(h + 0x20) + 12 * k + 4), where + ' event code')
            elif t == 3: ok(p.u32(h + 0xC), where + ' code')
            elif t != 2: errs.append('%s: unknown command type %d' % (where, t)); return
            h = p.u32(h + 4)
    for k, h in enumerate(p.frames_of(p.frames, p.fc)): walk(h, 'root frame %d' % k)
    texs = set()
    for i in range(1, p.count):
        a = p.ptrs[i]
        if not a: continue
        t = p.u8(a)
        if t == 2:
            n = p.u32(a + 0x10); ok(p.u32(a + 0xC), 'sprite %d frames' % i, False)
            for k, h in enumerate(p.frames_of(p.u32(a + 0xC), n)): walk(h, 'sprite %d frame %d' % (i, k))
        elif t == 1:
            for k in (8, 0xC, 0x10): ok(p.u32(a + k), 'shape %d ptr +%#x' % (i, k), False)
        elif t == 6: ok(p.u32(a + 0x20), 'text %d records' % i, False)
        elif t == 7:
            ok(p.u32(a + 8), 'edittext %d text' % i, False); ok(p.u32(a + 0xC), 'edittext %d var name' % i, False)
            f = p.u16(a + 0x18)
            if not (0 < f < p.count and p.ptrs[f] and p.u8(p.ptrs[f]) == 5): errs.append('edittext %d refers to font index %d which is not a font' % (i, f))
        elif t == 5:
            for k in (0xC, 0x10): ok(p.u32(a + k), 'font %d ptr +%#x' % (i, k), False)
            for sh in sheets_of(p, a):
                sp = sh['addr']; A, B = p.u32(sp), p.u32(sp + 4); ok(A, 'sheet atlas array', False); ok(B, 'sheet records', False)
                for pg in range(max(1, p.u32(sp + 0x10))):
                    info = p.u32(A + 4 * pg); ok(info, 'page info', False); ok(p.u32(info + 0x90), 'page palette', False); tex = p.u32(info + 0x94); ok(tex, 'page texture', False)
                    ok(p.u32(tex + 8), 'page pixels', False); nx = p.u32(tex + 4); ok(nx, 'page texture next')
                    # the texture chain must terminate inside the file
                    seen = set(); q = tex
                    while q and q not in seen and p.inside(q): seen.add(q); q = p.u32(q + 4)
                    if q and q != GLOBAL_TEX_HEAD and (q in seen or not p.inside(q)): errs.append('font %d: texture chain does not terminate (-> %#x)' % (i, q))
    return p, errs

if __name__ == '__main__':
    bad = 0
    for fn in sys.argv[1:]:
        p, e = audit(fn); print('%s: %d object(s), %d problem(s)' % (fn, p.count - 1, len(e)))
        for x in e[:30]: print('   -', x)
        bad += len(e)
    sys.exit(1 if bad else 0)
