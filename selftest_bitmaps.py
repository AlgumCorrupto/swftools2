"""selftest_bitmaps: decode / export / replace / revert checks for pckbitmaps.

    python3 selftest_bitmaps.py [extra.pck ...]

Always runs on the pck embedded in examples/legal_pck_fixed.swf (so it works on a clean checkout); extra pcks given on the
command line are checked too.
"""
import sys, os, io, json, zlib, base64, random, struct
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import rpck, swfr, pckbitmaps as pb, pckaudit
from PIL import Image, ImageDraw


def embedded_pck():
    s = swfr.read_swf(open(os.path.join(HERE, 'examples', 'legal_pck_fixed.swf'), 'rb').read())
    pl = [pl for c, pl in s['tags'] if c == 77 and pl.startswith(b'RPCK1:')][0]
    meta = json.loads(zlib.decompress(base64.b64decode(pl[6:-1] if pl.endswith(b'\0') else pl[6:])))
    return zlib.decompress(base64.b64decode(meta['pck']))


def test_image(w=300, h=300):
    im = Image.new('RGBA', (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w): px[x, y] = (x * 255 // w, y * 255 // h, (x + y) * 255 // (w + h), 255)
    ImageDraw.Draw(im).ellipse((w // 4, h // 4, 3 * w // 4, 3 * h // 4), outline=(255, 255, 255, 255), width=max(2, w // 30))
    return im


def check(name, raw, tmp):
    bs = pb.BitmapSet(raw); n = 0
    assert bs.indices, '%s: no bitmaps' % name
    # the numpy / reference decoders agree on every bitmap, and listing is consistent
    for i in bs.indices:
        d = bs.info(i)
        if not d['ok']: print('  #%d unsupported (%s) - skipped' % (i, d['error'])); continue
        a = bs.p.ptrs[i]; ref = rpck.bitmap_decode(bs.p, a); got = pb._decode_indices(bs.p, a)
        assert tuple(ref[:5]) == tuple(got), '%s #%d: fast decode differs' % (name, i)
        im = bs.image(i); assert im.size == (d['src_w'], d['src_h']), (name, i, im.size)
        assert bs.texture_image(i).size == (d['tex_w'], d['tex_h'])
        n += 1
    ok = [i for i in bs.indices if bs.info(i)['ok']]
    # replace a few bitmaps (all three fits), the file size stays, neighbours are untouched, the preview matches the result
    random.seed(1)
    for i in random.sample(ok, min(3, len(ok))):
        d = bs.info(i)
        for fit in pb.FITS:
            b = pb.BitmapSet(raw); before = {j: list(b.texture_image(j).tobytes()) for j in ok if j != i}
            src = test_image(300, 200)
            pv = b.preview_replacement(i, src, fit)
            b.replace(i, src, fit, dither=(fit == 'stretch'))
            out = os.path.join(tmp, '%s_%d_%s.pck' % (name, i, fit)); b.save(out)
            assert os.path.getsize(out) == len(raw), 'size changed'
            r = pb.BitmapSet(out)
            if not (fit == 'stretch'):                       # (dithering is the same in preview and write, but keep the check simple)
                assert r.image(i).tobytes() == pv.tobytes(), '%s #%d %s: preview != written' % (name, i, fit)
            assert all(list(r.texture_image(j).tobytes()) == before[j] for j in before), 'neighbour bitmap changed'
            errs = pckaudit.audit(out)[1]; assert not errs, errs
            b.revert(i); assert b.save(os.path.join(tmp, 'rev.pck')) and open(os.path.join(tmp, 'rev.pck'), 'rb').read() == raw, 'revert not identical'
    # export -> import with the same fit gives the same picture back (no drift in the true-proportions path)
    i = ok[0]; b = pb.BitmapSet(raw); png = os.path.join(tmp, 'x.png'); b.export_png(i, png)
    b.replace(i, png, 'cover'); b.save(os.path.join(tmp, 'rt.pck'))
    a1, a2 = pb.BitmapSet(raw).texture_image(i), pb.BitmapSet(os.path.join(tmp, 'rt.pck')).texture_image(i)
    diff = sum(abs(x - y) for x, y in zip(a1.convert('RGB').tobytes(), a2.convert('RGB').tobytes())) / (a1.width * a1.height * 3)
    assert diff < 6, '%s #%d: export/import round trip drifted (mean abs diff %.1f)' % (name, i, diff)
    print('%s: %d bitmaps OK' % (name, n))


def set_scale(p, cmd, sx, sy):
    """give the Place/PlaceClip command `cmd` a scale matrix (keeps its translation)"""
    m = p.read_pmatrix(p.u32(cmd + 0x10), p.u8(cmd + 1))
    p.put(cmd + 0x10, '<I', p.alloc(struct.pack('<hhhh', round(sx * 256), round(sy * 256), m[4], m[5]))); p.put(cmd + 1, 'B', 1)


def check_drawn_aspect(raw, tmp):
    """a bitmap drawn at another aspect than its source size: the drawn aspect (fill matrix x placement chain) is the reference"""
    plc = pb.BitmapSet(raw)._placements(); shape, bm = 6, 5                      # 128x128 bitmap in shape 6, shape placed inside sprite 7
    base = pb.BitmapSet(raw).info(bm)
    assert base['aspect'] == 1.0 and abs(base['screen_aspect'] - 1.0) < 1e-6 and base['auto_size'] == (base['src_w'], base['src_h'])
    p = pb.Pck(raw); set_scale(p, plc[shape][0][1], 2, 1)                       # drawn twice as wide as high
    b = pb.BitmapSet(p.tobytes()); d = b.info(bm)
    assert abs(d['screen_aspect'] - 2.0) < 1e-6 and d['screen_basis'] == 'placement' and abs(d['auto_aspect'] - 2.0) < 1e-6, d
    assert d['auto_size'] == (256, 128), d['auto_size']
    assert b.image(bm).size == (256, 128) and b.image(bm, reference='source').size == (128, 128) and b.image(bm, restore_size=False).size == (128, 128)
    wide = test_image(400, 200)                                                  # exactly the drawn aspect
    pv = b.preview_replacement(bm, wide, 'cover'); assert pv.size == (256, 128)
    b.replace(bm, wide, 'cover'); out = os.path.join(tmp, 'drawn.pck'); b.save(out)
    r = pb.BitmapSet(out); st = pb.BitmapSet(p.tobytes()); st.replace(bm, wide, 'stretch')
    assert r.texture_image(bm).tobytes() == st.texture_image(bm).tobytes(), 'a 2:1 image must not be cropped when the bitmap is drawn at 2:1'
    assert r.image(bm).tobytes() == pv.tobytes(), 'preview != written'
    src = pb.BitmapSet(p.tobytes()); src.replace(bm, wide, 'cover', reference='source')   # source aspect 1:1 crops the sides instead
    assert src.texture_image(bm).tobytes() != st.texture_image(bm).tobytes(), 'reference=source must crop a 2:1 image to 1:1'
    assert not pckaudit.audit(out)[1]
    # the packed matrices of the whole chain are composed: shape 2x1 inside a sprite drawn 1x2 -> 1:1 again
    p2 = pb.Pck(raw); set_scale(p2, plc[shape][0][1], 2, 1); set_scale(p2, plc[7][0][1], 1, 2)
    d2 = pb.BitmapSet(p2.tobytes()).info(bm); assert abs(d2['screen_aspect'] - 1.0) < 1e-6 and d2['auto_size'] == (128, 128), d2
    # two uses that disagree -> not trusted, the source size is the reference
    b3 = pb.BitmapSet(raw); b3._geometry(); b3._geo[bm] = [dict(aspect=1.0, placed=True, shape=1), dict(aspect=1.6, placed=True, shape=2)]
    sc = b3.screen(bm); assert sc['aspect'] is None and 'different proportions' in sc['note'], sc
    print('drawn aspect OK')


def check_same_file(raw, tmp):
    """saving over the source pck keeps the original as <name>.old (.old2 ... never overwritten); a failed save puts it back"""
    import subprocess
    pck = os.path.join(tmp, 'same.pck'); open(pck, 'wb').write(raw)
    png = os.path.join(tmp, 'same.png'); test_image(200, 200).save(png)
    cli = [sys.executable, os.path.join(HERE, 'pckbitmaps.py'), pck, 'replace', '5', png, pck]
    r = subprocess.run(cli, capture_output=True, text=True); assert r.returncode == 0, r.stderr
    assert 'original kept as' in r.stdout and open(pck + '.old', 'rb').read() == raw
    first = open(pck, 'rb').read(); assert first != raw and len(first) == len(raw) and not pckaudit.audit(pck)[1]
    r = subprocess.run(cli, capture_output=True, text=True); assert r.returncode == 0, r.stderr          # second time: .old stays, .old2 appears
    assert open(pck + '.old', 'rb').read() == raw and open(pck + '.old2', 'rb').read() == first
    # a failing body restores the original and leaves no partial output
    before = open(pck, 'rb').read()
    try:
        with rpck.replacing(pck, pck) as (rd, bak):
            assert os.path.exists(rd) and not os.path.exists(pck); open(pck, 'wb').write(b'partial'); raise RuntimeError('boom')
    except RuntimeError: pass
    assert open(pck, 'rb').read() == before and not os.path.exists(pck + '.old3'), 'failed save must restore the original'
    # different output: nothing is renamed
    with rpck.replacing(pck, os.path.join(tmp, 'other.pck')) as (rd, bak): assert rd == pck and bak is None
    print('same-file save OK')


if __name__ == '__main__':
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        check('legal (embedded)', embedded_pck(), tmp)
        check_drawn_aspect(embedded_pck(), tmp)
        check_same_file(embedded_pck(), tmp)
        for f in sys.argv[1:]:
            check(os.path.basename(f), open(f, 'rb').read(), tmp)
    print('ALL OK')
