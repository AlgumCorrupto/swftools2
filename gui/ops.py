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


def anim_scan(pck):
    """Frame animations (menu background movies) in a pck -> list of plain dicts (see pckanim.find_animations)."""
    import pckanim
    from rpck import Pck
    p = Pck(pck)
    res = []
    for a in pckanim.find_animations(p):
        print(pckanim.describe(a))
        res.append(dict(sprite=a['sprite'], nframes=a['nframes'], tex=a['tex'], fps=a['fps'], screen=a['screen'],
                        aspect=a['aspect'], alpha=a['alpha'], linked=[(l['sprite'], l['nframes']) for l in a['linked']],
                        unsupported=a['unsupported'], text=pckanim.describe(a), size=len(p.d) + 0x80,
                        per_frame=pckanim.estimate_growth(a, a['nframes'] + 1)))
    if not res:
        print('no frame animation found in %s' % pck)
    return res


def anim_source_info(src, fps=15.0):
    """(frames, duration ms) of the replacement GIF / image folder."""
    import pckanim
    return pckanim.source_info(src, fps)


def anim_export_gif(pck, sprite, out):
    """Write the animation `sprite` of `pck` as a GIF (scaled to its on-screen aspect)."""
    import pckanim
    return pckanim.export_gif(pck, sprite, out)


def anim_replace(pck, out, sprite, src, fit='cover', length='timing', opacity=None, dither=False, fps=None):
    """Replace animation `sprite` of `pck` with the GIF/APNG/WebP or image folder `src` and write `out`.
    length: 'original' | 'timing' | 'frames' | int;  opacity: None (keep) or 0..1."""
    import pckanim
    return pckanim.replace(pck, out, sprite, src, fit, length, opacity, bool(dither), fps, log=lambda m: print(m))


def load_scan(pck):
    """Loading-screen pck -> plain dict: text summary, colours {bar,text,tint} ((r,g,b) or None), artwork info or None."""
    import pckload
    s = pckload.scan(pck)
    print(s['text'])
    a = s['art']
    return dict(text=s['text'], colors=s['colors'], size=s['size'],
                art=None if not a else dict(size=a['size'], tex=a['tex'], screen=a['screen'], aspect=a['aspect'], unsupported=a['unsupported']))


def load_preview(pck, out_png, image=None, fit='cover', keep_mask=True, dither=False, palette='shared', frame=None):
    """Write a PNG preview of the artwork (current, or as it would look with `image`); returns the path."""
    import pckload
    pckload.preview(pck, image or None, fit, keep_mask, dither, palette, frame=frame or None).save(out_png)
    return out_png


def load_export_art(pck, out):
    """Write the stitched 512x256 artwork as PNG."""
    import pckload
    return pckload.export_art(pck, out)


def load_edit(pck, out, bar=None, text=None, tint=None, image=None, fit='cover', keep_mask=True, dither=False, palette='shared', frame=None):
    """Apply colour changes ((r,g,b) or None = keep), a new artwork image and/or a frame overlay (SVG / image path) to a loading-screen pck and write `out`."""
    import pckload
    return pckload.edit(pck, out, bar, text, tint, None, image or None, fit, keep_mask, dither, palette, log=lambda m: print(m), frame=frame or None)


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


def clean_env():
    """Environment for child processes. A PyInstaller bundle points LD_LIBRARY_PATH / QT_* at its own bundled libraries,
    which makes external programs (flash projector, java/JPEXS) load the wrong libs or crash -> restore the original."""
    env = os.environ.copy()
    if getattr(sys, 'frozen', False):
        for var in ('LD_LIBRARY_PATH', 'LD_PRELOAD', 'DYLD_LIBRARY_PATH', 'DYLD_FALLBACK_LIBRARY_PATH'):
            orig = env.get(var + '_ORIG')
            if orig is not None:
                env[var] = orig
            else:
                env.pop(var, None)
            env.pop(var + '_ORIG', None)
        for var in ('QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH', 'QML2_IMPORT_PATH', 'QML_IMPORT_PATH'):
            env.pop(var, None)
    return env


class Launched:
    """A started external program; poll() reports an early crash together with the tail of its stderr."""
    def __init__(self, cmd, proc, errfile):
        self.cmd, self.proc, self.errfile = cmd, proc, errfile

    def check(self):
        """None while still running (or exited cleanly); else an error text."""
        rc = self.proc.poll()
        if rc in (None, 0):
            return None
        try:
            with open(self.errfile, 'rb') as f:
                tail = f.read()[-2000:].decode('utf-8', 'replace').strip()
        except OSError:
            tail = ''
        return 'exited with code %s%s' % (rc, (':\n' + tail) if tail else ' (no error output)')


def launch(tool, target):
    """Start `tool target` detached; raises OSError if it cannot be started. Returns a Launched."""
    cmd = build_command(tool, target)
    fd, errfile = tempfile.mkstemp(prefix='pckswf_launch_', suffix='.log')
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=fd, close_fds=True, env=clean_env())
    if os.name == 'nt':
        kw['creationflags'] = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw['start_new_session'] = True
    try:
        proc = subprocess.Popen(cmd, **kw)
    finally:
        os.close(fd)
    return Launched(cmd, proc, errfile)
