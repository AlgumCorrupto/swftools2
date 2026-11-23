"""Qt-free operations used by the GUI (and testable without it).

Every function prints progress/warnings to stdout (the GUI redirects stdout/stderr to its console) and raises on
failure; the GUI worker thread logs the traceback to the console.
"""
import os, sys, re, shutil, subprocess, tempfile, threading

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

# tess.py keeps its tuning knobs in module globals, so jobs that set them must not overlap
TESS_LOCK = threading.Lock()

TESS_PRESETS = {            # name: (simplify twips, min area px^2)  -- numbers from NOTES.md
    'Off': (0.0, 0.0),
    'Light (20 / 2)': (20.0, 2.0),
    'Medium (40 / 4)': (40.0, 4.0),
    'Aggressive (80 / 8)': (80.0, 8.0),
}


def parse_fontmap(text):
    """'30=minidub,33=stencil' -> {30: 'minidub', 33: 'stencil'}; ValueError on malformed input."""
    text = (text or '').strip()
    if not text:
        return {}
    out = {}
    for part in text.split(','):
        m = re.fullmatch(r'\s*(\d+)\s*=\s*(\S+)\s*', part)
        if not m:
            raise ValueError('Invalid font map entry %r (expected e.g. 30=minidub,33=stencil)' % part)
        out[int(m.group(1))] = m.group(2)
    return out


def _set_tess(tolerance, simplify, min_area):
    import tess
    tess.TOL = float(tolerance)
    tess.SIMPLIFY = float(simplify)
    tess.MIN_AREA = float(min_area) * 400.0     # px^2 -> twips^2


def pck_to_swf(pck, swf, compress=False, embed=True, font=None, fonttex=None, fontmap=None):
    """Export a .pck to a .swf. Always preview mode (convertible back by swf_to_pck)."""
    from pck2swf import Conv, GlyphSrc
    from rpck import Pck
    fm = parse_fontmap(fontmap) if isinstance(fontmap, str) or fontmap is None else dict(fontmap)
    raw = open(pck, 'rb').read()
    p = Pck(raw)
    gs = GlyphSrc(font or None)
    if not gs.ok:
        print('note: no usable TTF/fontTools - glyphs will be placeholder boxes')
    cvx = Conv(p, True, gs, fonttex or None, fm)
    out, warn = cvx.convert(bool(compress), bool(embed), raw)
    for fi, nm in sorted(cvx.fontinfo.items()):
        print('font %d -> %s' % (fi, nm))
    with open(swf, 'wb') as f:
        f.write(out)
    print('wrote %s (%d bytes, %d objects)' % (swf, len(out), p.count - 1))
    for w in warn:
        print('warning:', w)
    return swf


def swf_to_pck(swf, pck, template=None, force=False, tolerance=8.0, simplify=0.0, min_area=0.0):
    """Convert an edited SWF (made by pck_to_swf) back into a .pck (patching the template / embedded original)."""
    import swf2pck
    tb = open(template, 'rb').read() if template else None
    with TESS_LOCK:
        _set_tess(tolerance, simplify, min_area)
        out, changed, warn = swf2pck.convert(open(swf, 'rb').read(), tb, log=lambda m: print(m), force=bool(force))
    with open(pck, 'wb') as f:
        f.write(out)
    print('wrote %s (%d bytes); %d change(s)' % (pck, len(out), len(changed)))
    for i, w in changed:
        print('  #%d: %s' % (i, w))
    for w in sorted(set(warn)):
        print('warning:', w)
    return pck


def swf_port(swf, pck, donor, fit=None, drop_actions=(), sizes=(), no_preloader=False, omit=(),
             bitmap_budget=600000, max_bitmap_dim=256, text_shape_scale=1.75, jpeg_tables=None,
             tolerance=4.0, simplify=0.0, min_area=0.0):
    """Build a brand-new .pck from a plain SWF using `donor` as the template (swfimport.build)."""
    import swfimport
    with TESS_LOCK:
        _set_tess(tolerance, simplify, min_area)
        out, warn = swfimport.build(open(swf, 'rb').read(), donor, fit, list(drop_actions), list(sizes),
                                    log=lambda m: print(m), no_preloader=bool(no_preloader), omit=list(omit),
                                    bitmap_budget=int(bitmap_budget), max_bitmap_dim=int(max_bitmap_dim),
                                    text_shape_scale=float(text_shape_scale), jpeg_tables_from=jpeg_tables or None)
    with open(pck, 'wb') as f:
        f.write(out)
    print('wrote %s (%d bytes)' % (pck, len(out)))
    return pck


def preview_swf(pck, tmpdir):
    """Export `pck` to a preview .swf inside tmpdir and return its path."""
    os.makedirs(tmpdir, exist_ok=True)
    name = os.path.splitext(os.path.basename(pck))[0] + '.swf'
    out = os.path.join(tmpdir, name)
    pck_to_swf(pck, out, compress=False, embed=False)
    return out


def new_preview_dir():
    """A fresh directory inside the OS temp folder for preview SWFs."""
    return tempfile.mkdtemp(prefix='pckswf_preview_')


def remove_dir(path):
    shutil.rmtree(path, ignore_errors=True)


def build_command(tool, target):
    """Command line that opens `target` with `tool` (flash player / JPEXS: exe, script, .jar or macOS .app)."""
    low = tool.lower()
    if low.endswith('.jar'):
        return ['java', '-jar', tool, target]
    if sys.platform == 'darwin' and low.endswith('.app'):
        return ['open', '-a', tool, target]
    return [tool, target]


def launch(tool, target):
    """Start `tool target` detached; raises OSError if it cannot be started."""
    cmd = build_command(tool, target)
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if os.name == 'nt':
        kw['creationflags'] = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw['start_new_session'] = True
    subprocess.Popen(cmd, **kw)
    return cmd
