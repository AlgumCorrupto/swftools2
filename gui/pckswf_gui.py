#!/usr/bin/env python3
"""PCK <-> SWF Tools GUI (PySide6 + qt-themes, Monokai).

Run:  python3 gui/pckswf_gui.py
"""
import os
import sys
import io
import html
import threading
import traceback
from datetime import datetime

os.environ.setdefault('QT_API', 'pyside6')      # for qt-themes / qtpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ops                                      # noqa: E402  (also puts the tools folder on sys.path)

from PySide6.QtCore import Qt, QObject, QThread, Signal, Slot, QSettings, QTimer   # noqa: E402
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QMovie, QPalette, QPixmap               # noqa: E402
from PySide6.QtWidgets import (                                                         # noqa: E402
    QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget)

APP_NAME = 'PCK <-> SWF Tools'
APP_VERSION = '1.0.0'
PORT_TAB_NAME = 'Port SWF \u2192 New PCK'        # the "swf import" tab; rename here if you prefer another name
ANIM_TAB_NAME = 'Animated BG (GIF)'              # the "replace a looping background movie with a GIF" tab
LOAD_TAB_NAME = 'Loading Screen'                 # the "loading screen accent colour + artwork" tab

CREDITS_HTML = """
<h3>%(name)s <small>v%(ver)s</small></h3>
<p>A Qt front-end for the MC3 (PS2) <b>.pck &harr; .swf</b> conversion tools (pck-swf-tools v3):
pck2swf, swf2pck and swfimport.</p>
<h4>Credits</h4>
<ul>
<li><b>pck-swf-tools v3</b> (AlgumCorrupto/rato.jpg)</li>
<li><b>RAGE-SWF-research</b> (AlgumCorrupto/rato.jpg, Thiekus, offbruno) &ndash; <a href="https://github.com/AlgumCorrupto/RAGE-SWF-research">github.com/AlgumCorrupto/RAGE-SWF-research</a></li>
<li><b>mc3pc</b> (weatheredclown) &ndash; <a href="https://github.com/weatheredclown/mc3pc">github.com/weatheredclown/mc3pc</a></li>
<li><b>JPEXS Free Flash Decompiler</b> &ndash; <a href="https://github.com/jindrapetrik/jpexs-decompiler">github.com/jindrapetrik/jpexs-decompiler</a></li>
<li><b>Qt for Python (PySide6)</b> and <b>qt-themes</b> (Monokai theme)</li>
<li><b>Pillow</b>, <b>NumPy</b> and <b>fontTools</b> &ndash; used by the library</li>
</ul>
<p>Not affiliated with the game's publisher. Use only with files you own.</p>
""" % dict(name=html.escape(APP_NAME), ver=APP_VERSION)

# Monokai palette (used for the console and as a fallback theme)
MK = dict(bg='#272822', bg2='#1e1f1c', fg='#f8f8f2', comment='#75715e', red='#f92672', orange='#fd971f',
          yellow='#e6db74', green='#a6e22e', blue='#66d9ef', purple='#ae81ff', sel='#49483e')


# ----------------------------------------------------------------------------------------------------------------
# settings
# ----------------------------------------------------------------------------------------------------------------
class Settings:
    def __init__(self):
        self.q = QSettings('pckswf-tools', 'pckswf-gui')

    def get(self, key, default=''):
        v = self.q.value(key, default)
        return default if v is None else v

    def set(self, key, value):
        self.q.setValue(key, value)

    @property
    def flash(self): return str(self.get('flash_path'))

    @property
    def jpexs(self): return str(self.get('jpexs_path'))

    @property
    def pck_dir(self): return str(self.get('pck_dir'))

    @property
    def show_welcome(self): return str(self.get('show_welcome', 'true')).lower() != 'false'


# ----------------------------------------------------------------------------------------------------------------
# console / logging (stdout+stderr of the library are routed here)
# ----------------------------------------------------------------------------------------------------------------
class LogBridge(QObject):
    message = Signal(str, str)          # level, text   (safe to emit from any thread)


class LogStream(io.TextIOBase):
    def __init__(self, bridge, is_err):
        super().__init__()
        self.bridge, self.is_err, self.buf, self.lock = bridge, is_err, '', threading.Lock()

    def writable(self): return True

    def write(self, s):
        if not isinstance(s, str):
            s = str(s)
        with self.lock:
            self.buf += s
            *lines, self.buf = self.buf.split('\n')
        for ln in lines:
            self._emit(ln)
        return len(s)

    def flush(self):
        with self.lock:
            rest, self.buf = self.buf, ''
        if rest:
            self._emit(rest)

    def _emit(self, line):
        if not line.strip():
            return
        low = line.lower().lstrip()
        if self.is_err or low.startswith(('error', 'traceback', 'fatal')):
            level = 'error'
        elif low.startswith('warning'):
            level = 'warn'
        else:
            level = 'info'
        self.bridge.message.emit(level, line)


class Console(QWidget):
    COLORS = {'info': MK['fg'], 'warn': MK['yellow'], 'error': MK['red'], 'ok': MK['green'], 'gui': MK['blue']}

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        head.addWidget(QLabel('<b>Console</b>'))
        head.addStretch()
        clear = QPushButton('Clear')
        clear.clicked.connect(lambda: self.view.clear())
        head.addWidget(clear)
        lay.addLayout(head)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(20000)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        f = QFont('Monospace')
        f.setStyleHint(QFont.Monospace)
        self.view.setFont(f)
        self.view.setStyleSheet('QPlainTextEdit { background: %s; color: %s; }' % (MK['bg2'], MK['fg']))
        lay.addWidget(self.view)

    @Slot(str, str)
    def append(self, level, text):
        ts = datetime.now().strftime('%H:%M:%S')
        col = self.COLORS.get(level, MK['fg'])
        self.view.appendHtml('<span style="white-space:pre; color:%s">%s</span> '
                             '<span style="white-space:pre; color:%s">%s</span>'
                             % (MK['comment'], ts, col, html.escape(text)))
        self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())


# ----------------------------------------------------------------------------------------------------------------
# worker thread
# ----------------------------------------------------------------------------------------------------------------
class Job(QThread):
    done = Signal(object, bool, object)         # job, ok, result

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn
        self.on_done = None
        self.widgets = ()

    def run(self):
        try:
            res = self.fn()
            self.done.emit(self, True, res)
        except BaseException:                    # library code may raise SystemExit too
            traceback.print_exc()                # -> stderr -> console
            self.done.emit(self, False, None)


# ----------------------------------------------------------------------------------------------------------------
# small widgets
# ----------------------------------------------------------------------------------------------------------------
class DropLineEdit(QLineEdit):
    """Line edit that accepts a dropped file/folder (optionally filtered by extension)."""

    def __init__(self, exts=None, want_dir=False):
        super().__init__()
        self.exts = tuple(e.lower() for e in (exts or ()))
        self.want_dir = want_dir
        self.setAcceptDrops(True)

    def _pick(self, md):
        for u in md.urls():
            if not u.isLocalFile():
                continue
            p = u.toLocalFile()
            if self.want_dir:
                if os.path.isdir(p):
                    return p
            elif os.path.isfile(p) and (not self.exts or p.lower().endswith(self.exts)):
                return p
        return None

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and self._pick(e.mimeData()):
            e.acceptProposedAction()
        else:
            e.ignore()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, e):
        p = self._pick(e.mimeData())
        if p:
            self.setText(p)
            e.acceptProposedAction()


class PathRow(QWidget):
    """DropLineEdit + Browse button. mode: 'open' | 'save' | 'dir'."""
    changed = Signal(str)

    def __init__(self, mode='open', exts=None, filt='All files (*)', placeholder='', start_dir=lambda: ''):
        super().__init__()
        self.mode, self.filt, self.start_dir = mode, filt, start_dir
        self.manual = False                      # True once the user typed/browsed (stops auto-suggestions)
        self.edit = DropLineEdit(exts, want_dir=(mode == 'dir'))
        self.edit.setPlaceholderText(placeholder)
        self.edit.textChanged.connect(self.changed)
        self.edit.textEdited.connect(lambda _: setattr(self, 'manual', True))
        btn = QPushButton('Browse\u2026')
        btn.clicked.connect(self.browse)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.edit, 1)
        lay.addWidget(btn)

    def text(self): return self.edit.text().strip()
    def setText(self, t): self.edit.setText(t)

    def suggest(self, t):
        if not self.manual:
            self.edit.setText(t)

    def browse(self):
        cur = self.text()
        start = cur if cur else self.start_dir()
        if self.mode == 'dir':
            p = QFileDialog.getExistingDirectory(self, 'Select folder', start)
        elif self.mode == 'save':
            p, _ = QFileDialog.getSaveFileName(self, 'Save as', start, self.filt)
        else:
            p, _ = QFileDialog.getOpenFileName(self, 'Select file', start, self.filt)
        if p:
            self.edit.setText(p)
            self.manual = True


class TessOptions(QGroupBox):
    """Tessellator tuning (tess.TOL / SIMPLIFY / MIN_AREA)."""

    def __init__(self, default_tol):
        super().__init__('Tessellator / optimization')
        form = QFormLayout(self)
        self.preset = QComboBox()
        self.preset.addItems(list(ops.TESS_PRESETS) + ['Custom'])
        self.preset.currentTextChanged.connect(self._apply_preset)
        self.tol = QDoubleSpinBox()
        self.tol.setRange(0.1, 500)
        self.tol.setDecimals(1)
        self.tol.setSingleStep(0.5)
        self.tol.setValue(default_tol)
        self.tol.setToolTip('Curve flattening tolerance in twips (20 twips = 1 px). Smaller = rounder curves, bigger file.')
        self.simp = QDoubleSpinBox()
        self.simp.setRange(0, 1000)
        self.simp.setDecimals(1)
        self.simp.setToolTip('Douglas-Peucker tolerance on edge chains in twips (20 = 1 px; try 10-20). 0 = off. '
                             'Lower poly count, smaller pck, faster.')
        self.minarea = QDoubleSpinBox()
        self.minarea.setRange(0, 10000)
        self.minarea.setDecimals(1)
        self.minarea.setToolTip('Drop fill loops smaller than this many square pixels (specks in traced art; try 1-4).')
        for w in (self.simp, self.minarea):
            w.valueChanged.connect(self._mark_custom)
        form.addRow('Preset (simplify / min-area)', self.preset)
        form.addRow('Curve tolerance (twips)', self.tol)
        form.addRow('Simplify (twips)', self.simp)
        form.addRow('Min area (px\u00b2)', self.minarea)

    def _apply_preset(self, name):
        if name in ops.TESS_PRESETS:
            s, m = ops.TESS_PRESETS[name]
            for w, v in ((self.simp, s), (self.minarea, m)):
                w.blockSignals(True)
                w.setValue(v)
                w.blockSignals(False)

    def _mark_custom(self):
        self.preset.blockSignals(True)
        cur = (self.simp.value(), self.minarea.value())
        match = [n for n, v in ops.TESS_PRESETS.items() if v == cur]
        self.preset.setCurrentText(match[0] if match else 'Custom')
        self.preset.blockSignals(False)

    def values(self):
        return dict(tolerance=self.tol.value(), simplify=self.simp.value(), min_area=self.minarea.value())


# ----------------------------------------------------------------------------------------------------------------
# tabs
# ----------------------------------------------------------------------------------------------------------------
class BaseTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win

    def scrolled(self):
        """Wrap this tab's content in a scroll area (call after building the layout)."""
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.NoFrame)
        area.setWidget(self)
        return area

    def start_dir(self):
        return self.win.settings.pck_dir


class PckToSwfTab(BaseTab):
    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        g = QGroupBox('Input')
        f = QFormLayout(g)
        self.src = PathRow('open', ['.pck'], 'PCK files (*.pck);;All files (*)', 'Drop a .pck here or click Browse\u2026', self.start_dir)
        f.addRow('PCK file', self.src)
        lay.addWidget(g)

        g = QGroupBox('Output')
        f = QFormLayout(g)
        self.dst = PathRow('save', None, 'Flash movie (*.swf)', 'Output .swf', self.start_dir)
        f.addRow('SWF file', self.dst)
        f.addRow(QLabel('<i>Always exported in preview mode (engine _x/_y offsets baked in; the SWF can be '
                        'converted back with the "SWF \u2192 PCK" tab).</i>'))
        lay.addWidget(g)

        g = QGroupBox('Options')
        f = QFormLayout(g)
        self.zlib = QCheckBox('Compress the SWF (zlib / CWS)')
        self.embed = QCheckBox('Embed the source pck in the SWF (needed to convert back without a template)')
        self.embed.setChecked(True)
        self.font = PathRow('open', None, 'Fonts (*.ttf *.otf *.ttc);;All files (*)', 'Optional: TTF used for glyph outlines (default: system font)')
        self.fonttex = PathRow('dir', None, placeholder='Optional: folder with *.fonttex and texture/*.tex')
        self.fontmap = QLineEdit()
        self.fontmap.setPlaceholderText('Optional: font index = fonttex name, e.g. 30=minidub,33=stencil')
        f.addRow(self.zlib)
        f.addRow(self.embed)
        f.addRow('Glyph font (--font)', self.font)
        f.addRow('Fonttex folder (--fonttex)', self.fonttex)
        f.addRow('Font map (--fontmap)', self.fontmap)
        lay.addWidget(g)

        row = QHBoxLayout()
        self.run = QPushButton('Export SWF')
        self.btn_jpexs = QPushButton('Open in JPEXS')
        self.btn_flash = QPushButton('Preview in Flash Player')
        self.run.clicked.connect(self.export)
        self.btn_jpexs.clicked.connect(lambda: win.open_with_jpexs(self.dst.text()))
        self.btn_flash.clicked.connect(lambda: win.open_with_flash(self.dst.text()))
        for b in (self.run, self.btn_jpexs, self.btn_flash):
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        lay.addStretch()

        self.src.changed.connect(self._src_changed)
        self.dst.changed.connect(self._refresh_buttons)
        self._refresh_buttons()

    def _src_changed(self, t):
        if t:
            self.dst.suggest(os.path.splitext(t)[0] + '.swf')

    def _refresh_buttons(self, *_):
        ok = os.path.isfile(self.dst.text())
        self.btn_jpexs.setEnabled(ok)
        self.btn_flash.setEnabled(ok)

    def export(self):
        src, dst = self.src.text(), self.dst.text()
        if not src or not os.path.isfile(src):
            return self.win.error('Please select an existing .pck file.')
        if not dst:
            return self.win.error('Please choose an output .swf path.')
        try:
            ops.parse_fontmap(self.fontmap.text())
        except ValueError as e:
            return self.win.error(str(e))
        kw = dict(compress=self.zlib.isChecked(), embed=self.embed.isChecked(), font=self.font.text() or None,
                  fonttex=self.fonttex.text() or None, fontmap=self.fontmap.text())
        self.win.log('gui', 'PCK \u2192 SWF: %s' % src)
        self.win.run_job(lambda: ops.pck_to_swf(src, dst, **kw), self._done, [self.run])

    def _done(self, ok, _res):
        self._refresh_buttons()
        self.win.job_finished_message(ok, 'PCK \u2192 SWF')


class SwfToPckTab(BaseTab):
    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        g = QGroupBox('Input')
        f = QFormLayout(g)
        self.src = PathRow('open', ['.swf'], 'Flash movie (*.swf);;All files (*)', 'Drop a .swf here or click Browse\u2026', self.start_dir)
        self.tpl = PathRow('open', ['.pck'], 'PCK files (*.pck);;All files (*)',
                           'Optional: original .pck (empty = use the pck embedded in the SWF)', self.start_dir)
        f.addRow('SWF file', self.src)
        f.addRow('Template PCK (--template)', self.tpl)
        lay.addWidget(g)

        g = QGroupBox('Output')
        f = QFormLayout(g)
        self.dst = PathRow('save', None, 'PCK files (*.pck)', 'Output .pck', self.start_dir)
        f.addRow('PCK file', self.dst)
        lay.addWidget(g)

        g = QGroupBox('Options')
        f = QFormLayout(g)
        self.force = QCheckBox('Force re-emit of every shape / sprite / root (--force, for testing)')
        f.addRow(self.force)
        lay.addWidget(g)

        self.tess = TessOptions(8.0)
        lay.addWidget(self.tess)

        row = QHBoxLayout()
        self.run = QPushButton('Convert to PCK')
        self.run.clicked.connect(self.convert)
        row.addWidget(self.run)
        row.addStretch()
        lay.addLayout(row)
        lay.addStretch()
        self.src.changed.connect(lambda t: t and self.dst.suggest(os.path.splitext(t)[0] + '_new.pck'))

    def convert(self):
        src, dst, tpl = self.src.text(), self.dst.text(), self.tpl.text()
        if not src or not os.path.isfile(src):
            return self.win.error('Please select an existing .swf file.')
        if not dst:
            return self.win.error('Please choose an output .pck path.')
        if tpl and not os.path.isfile(tpl):
            return self.win.error('The template pck does not exist:\n%s' % tpl)
        kw = dict(template=tpl or None, force=self.force.isChecked(), **self.tess.values())
        self.win.log('gui', 'SWF \u2192 PCK: %s' % src)
        self.win.run_job(lambda: ops.swf_to_pck(src, dst, **kw), lambda ok, r: self.win.job_finished_message(ok, 'SWF \u2192 PCK'), [self.run])


class SwfPortTab(BaseTab):
    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel('<i>Builds a <b>brand-new</b> .pck from an ordinary Flash SWF, using an original game pck as a '
                             'donor/template. (To convert a SWF that was exported from a pck, use "SWF \u2192 PCK".)</i>'))
        g = QGroupBox('Files')
        f = QFormLayout(g)
        self.src = PathRow('open', ['.swf'], 'Flash movie (*.swf);;All files (*)', 'Drop the plain .swf here or click Browse\u2026', self.start_dir)
        self.donor = PathRow('open', ['.pck'], 'PCK files (*.pck);;All files (*)', 'Donor .pck (any original game pck)', self.start_dir)
        self.dst = PathRow('save', None, 'PCK files (*.pck)', 'Output .pck', self.start_dir)
        self.jpt = PathRow('open', ['.swf'], 'Flash movie (*.swf);;All files (*)', 'Optional: SWF to take JPEGTables from', self.start_dir)
        f.addRow('SWF to port', self.src)
        f.addRow('Donor PCK (--donor)', self.donor)
        f.addRow('Output PCK', self.dst)
        f.addRow('JPEGTables from (--jpeg-tables)', self.jpt)
        lay.addWidget(g)

        g = QGroupBox('Stage / scripts')
        f = QFormLayout(g)
        self.fit = QCheckBox('Fit stage to')
        self.fw, self.fh = QSpinBox(), QSpinBox()
        for s, v in ((self.fw, 640), (self.fh, 480)):
            s.setRange(1, 4096)
            s.setValue(v)
        fr = QHBoxLayout()
        fr.addWidget(self.fit)
        fr.addWidget(self.fw)
        fr.addWidget(QLabel('\u00d7'))
        fr.addWidget(self.fh)
        fr.addStretch()
        f.addRow(fr)
        self.nopre = QCheckBox('Remove getBytesLoaded/getBytesTotal preloader (--no-preloader)')
        self.drop = QLineEdit()
        self.drop.setPlaceholderText('Drop DoAction scripts containing text (--drop-action); separate several with ;')
        self.omit_text = QCheckBox('text (no fonts / text / edit text)')
        self.omit_act = QCheckBox('actions (no scripts)')
        om = QHBoxLayout()
        om.addWidget(self.omit_text)
        om.addWidget(self.omit_act)
        om.addStretch()
        f.addRow(self.nopre)
        f.addRow('Drop actions', self.drop)
        f.addRow('Omit (for bisecting crashes)', om)
        lay.addWidget(g)

        g = QGroupBox('Bitmaps / fonts')
        f = QFormLayout(g)
        self.budget = QSpinBox()
        self.budget.setRange(1000, 100000000)
        self.budget.setSingleStep(50000)
        self.budget.setValue(600000)
        self.budget.setSuffix(' bytes')
        self.maxdim = QComboBox()
        self.maxdim.addItems(['64', '128', '256'])
        self.maxdim.setCurrentText('256')
        self.tss = QDoubleSpinBox()
        self.tss.setRange(0.1, 100)
        self.tss.setSingleStep(0.25)
        self.tss.setValue(1.75)
        self.tss.setToolTip('Static text scaled up more than this becomes vector shapes instead of bitmap-font text.')
        self.sizes = QLineEdit()
        self.sizes.setPlaceholderText('Extra font sheet sizes in twips for every font, e.g. 240,400')
        f.addRow('Bitmap budget (--bitmap-budget)', self.budget)
        f.addRow('Max bitmap dimension', self.maxdim)
        f.addRow('Text \u2192 shape scale', self.tss)
        f.addRow('Extra font sizes (--sizes)', self.sizes)
        lay.addWidget(g)

        self.tess = TessOptions(4.0)
        lay.addWidget(self.tess)

        row = QHBoxLayout()
        self.run = QPushButton('Build new PCK')
        self.run.clicked.connect(self.build)
        row.addWidget(self.run)
        row.addStretch()
        lay.addLayout(row)
        lay.addStretch()
        self.src.changed.connect(lambda t: t and self.dst.suggest(os.path.splitext(t)[0] + '.pck'))

    def build(self):
        src, donor, dst = self.src.text(), self.donor.text(), self.dst.text()
        if not src or not os.path.isfile(src):
            return self.win.error('Please select an existing .swf file to port.')
        if not donor or not os.path.isfile(donor):
            return self.win.error('Please select a donor .pck (any original game pck).')
        if not dst:
            return self.win.error('Please choose an output .pck path.')
        jpt = self.jpt.text()
        if jpt and not os.path.isfile(jpt):
            return self.win.error('The JPEGTables SWF does not exist:\n%s' % jpt)
        sizes = [x.strip() for x in self.sizes.text().split(',') if x.strip()]
        if not all(x.isdigit() for x in sizes):
            return self.win.error('Extra font sizes must be whole numbers (twips) separated by commas.')
        omit = [n for n, c in (('text', self.omit_text), ('actions', self.omit_act)) if c.isChecked()]
        kw = dict(fit=(self.fw.value(), self.fh.value()) if self.fit.isChecked() else None,
                  drop_actions=[x.strip() for x in self.drop.text().split(';') if x.strip()], sizes=sizes,
                  no_preloader=self.nopre.isChecked(), omit=omit, bitmap_budget=self.budget.value(),
                  max_bitmap_dim=int(self.maxdim.currentText()), text_shape_scale=self.tss.value(),
                  jpeg_tables=jpt or None, **self.tess.values())
        self.win.log('gui', 'Port SWF: %s (donor %s)' % (src, os.path.basename(donor)))
        self.win.run_job(lambda: ops.swf_port(src, dst, donor, **kw), lambda ok, r: self.win.job_finished_message(ok, 'Port SWF'), [self.run])


class AnimPreviewDialog(QDialog):
    """Plays an exported GIF (QMovie) in a small window."""

    def __init__(self, parent, gif, title):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.movie = QMovie(gif)
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setMovie(self.movie)
        lay = QVBoxLayout(self)
        lay.addWidget(self.label)
        self.movie.jumpToFrame(0)
        sz = self.movie.currentPixmap().size()
        if sz.isValid() and sz.width() > 0:
            self.label.setMinimumSize(sz)
        self.movie.start()

    def done(self, r):
        self.movie.stop()
        super().done(r)


class AnimTab(BaseTab):
    """Replace the looping background movie of a menu pck (bg_arcade, bg_career, bg_options, ...) with an animated GIF."""

    def __init__(self, win):
        super().__init__(win)
        self.anims = []
        self.src_info = None                 # (frame count, duration ms) of the chosen replacement
        self._info_token = 0
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel('<i>These pcks store a looping movie as a sprite that shows one small bitmap per frame. '
                             'This tab swaps those frames for the frames of your own GIF (APNG, animated WebP or a folder of images work too).</i>'))

        g = QGroupBox('1. PCK with the animation')
        f = QFormLayout(g)
        self.pck = PathRow('open', ['.pck'], 'PCK files (*.pck);;All files (*)', 'Drop a .pck here (e.g. bg_arcade.pck)', self.start_dir)
        f.addRow('PCK file', self.pck)
        self.list = QListWidget()
        self.list.setMaximumHeight(96)
        f.addRow('Animations found', self.list)
        row = QHBoxLayout()
        self.btn_prev = QPushButton('Play selected')
        self.btn_exp = QPushButton('Export selected as GIF\u2026')
        self.btn_prev.setToolTip('Decode the current frames to a GIF and play it here')
        self.btn_exp.setToolTip('Save the current animation as a GIF (handy as a starting point for your own)')
        row.addWidget(self.btn_prev)
        row.addWidget(self.btn_exp)
        row.addStretch()
        f.addRow(row)
        lay.addWidget(g)

        g = QGroupBox('2. Your replacement')
        f = QFormLayout(g)
        self.gif = PathRow('open', None, 'Animations (*.gif *.png *.apng *.webp);;All files (*)',
                           'Drop a .gif (or APNG / animated WebP) here', self.start_dir)
        self.folder = PathRow('dir', None, placeholder='Optional: a folder of images instead (played in name order)')
        self.fps = QDoubleSpinBox()
        self.fps.setRange(1, 120)
        self.fps.setDecimals(2)
        self.fps.setValue(15)
        self.fps.setToolTip('Frame rate of the images in the folder (a GIF carries its own timing)')
        self.fit = QComboBox()
        self.fit.addItem('Crop to fill (no distortion)', 'cover')
        self.fit.addItem('Stretch to fill', 'stretch')
        self.fit.addItem('Fit inside (black bars)', 'contain')
        self.fit.setToolTip('The game stretches the small texture over the screen area. "Crop" cuts your GIF to that '
                            'area\'s aspect ratio, "Stretch" squeezes the whole GIF in, "Fit inside" letterboxes it.')
        self.length = QComboBox()
        self.length.addItem('Play at the GIF\'s own speed', 'timing')
        self.length.addItem('Keep the original frame count (stretches the GIF over it)', 'original')
        self.length.addItem('One frame per GIF frame', 'frames')
        self.length.addItem('Custom frame count\u2026', 'custom')
        self.custom = QSpinBox()
        self.custom.setRange(1, 65535)
        self.custom.setValue(100)
        self.custom.setEnabled(False)
        lrow = QHBoxLayout()
        lrow.addWidget(self.length, 1)
        lrow.addWidget(self.custom)
        self.opac_chk = QCheckBox('Override opacity')
        self.opac = QSpinBox()
        self.opac.setRange(0, 100)
        self.opac.setSuffix(' %')
        self.opac.setValue(100)
        self.opac.setEnabled(False)
        self.opac_chk.setToolTip('The original movies are drawn semi-transparent (about 34 %). Leave this off to keep that look, '
                                 'or tick it to draw your GIF at the chosen opacity.')
        orow = QHBoxLayout()
        orow.addWidget(self.opac_chk)
        orow.addWidget(self.opac)
        orow.addStretch()
        self.dither = QCheckBox('Dither colours (256 colours per frame; dithering can shimmer in motion)')
        f.addRow('Animated image', self.gif)
        f.addRow('...or image folder', self.folder)
        f.addRow('Folder frame rate', self.fps)
        f.addRow('Fit', self.fit)
        f.addRow('Length', lrow)
        f.addRow('Opacity', orow)
        f.addRow(self.dither)
        self.estimate = QLabel()
        self.estimate.setWordWrap(True)
        self.estimate.setTextFormat(Qt.RichText)
        f.addRow(self.estimate)
        lay.addWidget(g)

        g = QGroupBox('3. Output')
        f = QFormLayout(g)
        self.dst = PathRow('save', None, 'PCK files (*.pck)', 'Output .pck', self.start_dir)
        f.addRow('Output PCK', self.dst)
        lay.addWidget(g)

        row = QHBoxLayout()
        self.run = QPushButton('Replace animation && save PCK')
        self.btn_flash = QPushButton('Preview result in Flash Player')
        self.run.clicked.connect(self.replace)
        self.btn_flash.clicked.connect(lambda: win.preview_pck(self.dst.text()))
        row.addWidget(self.run)
        row.addWidget(self.btn_flash)
        row.addStretch()
        lay.addLayout(row)
        lay.addStretch()

        self.pck.changed.connect(self._pck_changed)
        self.list.currentRowChanged.connect(self._selection_changed)
        self.gif.changed.connect(self._source_changed)
        self.folder.changed.connect(self._source_changed)
        self.fps.valueChanged.connect(self._source_changed)
        self.length.currentIndexChanged.connect(self._length_changed)
        self.custom.valueChanged.connect(self._update_estimate)
        self.opac_chk.toggled.connect(self.opac.setEnabled)
        self.btn_prev.clicked.connect(self.play_selected)
        self.btn_exp.clicked.connect(self.export_selected)
        self.dst.changed.connect(self._refresh_buttons)
        self._refresh_buttons()

    # ---- state --------------------------------------------------------------------------------------------------
    def _current(self):
        r = self.list.currentRow()
        return self.anims[r] if 0 <= r < len(self.anims) else None

    def _refresh_buttons(self, *_):
        a = self._current()
        ok = a is not None
        self.btn_prev.setEnabled(ok)
        self.btn_exp.setEnabled(ok)
        self.run.setEnabled(ok and not a['unsupported'])
        self.btn_flash.setEnabled(os.path.isfile(self.dst.text()))

    # ---- scanning -----------------------------------------------------------------------------------------------
    def _pck_changed(self, t):
        self.anims = []
        self.list.clear()
        self._refresh_buttons()
        if not t or not os.path.isfile(t):
            return
        self.dst.suggest(os.path.splitext(t)[0] + '_gif.pck')
        self.win.log('gui', 'Scanning %s' % t)
        self.win.run_job(lambda: ops.anim_scan(t), lambda ok, res: self._scanned(t, ok, res), [self.pck])

    def _scanned(self, path, ok, res):
        if path != self.pck.text():           # the user picked another file meanwhile
            return
        if not ok:
            return self.win.job_finished_message(False, 'Scan')
        self.anims = res or []
        self.list.clear()
        for a in self.anims:
            it = QListWidgetItem(a['text'])
            it.setToolTip(a['text'])
            self.list.addItem(it)
        if not self.anims:
            return self.win.error('No frame-by-frame animation was found in this pck.\n'
                                  '(Looking for a sprite with at least 8 frames that each show one bitmap, as in bg_*.pck.)')
        self.list.setCurrentRow(0)

    def _selection_changed(self, *_):
        a = self._current()
        if a:
            self.opac.setValue(max(0, min(100, round(a['alpha'] * 100))))
        self._refresh_buttons()
        self._update_estimate()

    # ---- replacement source -------------------------------------------------------------------------------------
    def _source(self):
        return self.gif.text() or self.folder.text()

    def _source_changed(self, *_):
        src = self._source()
        self.src_info = None
        self._info_token += 1
        token, fps = self._info_token, self.fps.value()
        self._update_estimate()
        if not src or not (os.path.isfile(src) or os.path.isdir(src)):
            return

        def done(ok, res):
            if token == self._info_token and ok:
                self.src_info = res
                self._update_estimate()
        self.win.run_job(lambda: ops.anim_source_info(src, fps), done)

    def _length_changed(self, *_):
        self.custom.setEnabled(self.length.currentData() == 'custom')
        self._update_estimate()

    def _target_frames(self, a):
        mode = self.length.currentData()
        if mode == 'custom':
            return self.custom.value()
        if mode == 'original':
            return a['nframes']
        if not self.src_info:
            return None
        n, ms = self.src_info
        return max(1, min(65535, int(round(ms / 1000.0 * a['fps'])) if mode == 'timing' else n))

    def _update_estimate(self, *_):
        a = self._current()
        if not a:
            self.estimate.setText('')
            return
        n = self._target_frames(a)
        if n is None:
            self.estimate.setText('<i>Choose a GIF to see the resulting frame count.</i>')
            return
        txt = '%d frame(s) at %.4g fps = %.1f s loop (the original has %d).' % (n, a['fps'], n / a['fps'], a['nframes'])
        grow = max(0, n - a['nframes']) * a['per_frame']
        if grow:
            txt += ('<br><span style="color:%s">The pck grows by about %d KB (to %.2f MB). The game loads a pck into a fixed-size heap '
                    '(its limit is unknown), so prefer staying near the original %.2f MB.</span>'
                    % (MK['orange'], round(grow / 1024.0), (a['size'] + grow) / 1048576.0, a['size'] / 1048576.0))
        elif n < a['nframes']:
            txt += '<br>Fewer frames than the original: the old frames stay in the file unused (same size).'
        self.estimate.setText(txt)

    # ---- actions ------------------------------------------------------------------------------------------------
    def play_selected(self):
        a, pck = self._current(), self.pck.text()
        if not a:
            return
        if self.win.preview_dir is None:
            self.win.preview_dir = ops.new_preview_dir()
        out = os.path.join(self.win.preview_dir, '%s_%d.gif' % (os.path.splitext(os.path.basename(pck))[0], a['sprite']))

        def done(ok, res):
            if ok:
                AnimPreviewDialog(self.win, res, 'Sprite %d \u2013 %s' % (a['sprite'], os.path.basename(pck))).exec()
            else:
                self.win.job_finished_message(False, 'Export')
        self.win.run_job(lambda: ops.anim_export_gif(pck, a['sprite'], out), done, [self.btn_prev])

    def export_selected(self):
        a, pck = self._current(), self.pck.text()
        if not a:
            return
        start = os.path.splitext(pck)[0] + '_%d.gif' % a['sprite']
        out, _ = QFileDialog.getSaveFileName(self, 'Export animation as GIF', start, 'GIF (*.gif)')
        if not out:
            return
        self.win.log('gui', 'Export animation: %s' % out)
        self.win.run_job(lambda: ops.anim_export_gif(pck, a['sprite'], out),
                         lambda ok, _r: self.win.job_finished_message(ok, 'Export'), [self.btn_exp])

    def replace(self):
        a, src, dst, pck = self._current(), self._source(), self.dst.text(), self.pck.text()
        if not a:
            return self.win.error('Please select a PCK that contains an animation first.')
        if a['unsupported']:
            return self.win.error('This animation cannot be replaced: %s.' % a['unsupported'])
        if not src or not (os.path.isfile(src) or os.path.isdir(src)):
            return self.win.error('Please choose an existing GIF (or image folder).')
        if not dst:
            return self.win.error('Please choose an output .pck path.')
        if os.path.abspath(dst) == os.path.abspath(pck):
            return self.win.error('The output must be a different file than the source pck (so the original stays untouched).')
        mode = self.length.currentData()
        kw = dict(fit=self.fit.currentData(), length=(self.custom.value() if mode == 'custom' else mode),
                  opacity=(self.opac.value() / 100.0 if self.opac_chk.isChecked() else None),
                  dither=self.dither.isChecked(), fps=(self.fps.value() if os.path.isdir(src) else None))
        sprite = a['sprite']
        self.win.log('gui', 'Replace animation (sprite %d): %s' % (sprite, src))
        self.win.run_job(lambda: ops.anim_replace(pck, dst, sprite, src, **kw), self._done, [self.run])

    def _done(self, ok, _res):
        self._refresh_buttons()
        self.win.job_finished_message(ok, 'Replace animation')


class ColorButton(QPushButton):
    """A button that shows a colour (as its background and #rrggbb text) and opens a colour dialog when clicked."""
    changed = Signal()

    def __init__(self, label):
        super().__init__()
        self.label = label
        self.rgb = None
        self.clicked.connect(self._pick)
        self.set_rgb(None)

    def set_rgb(self, rgb, quiet=True):
        self.rgb = tuple(rgb) if rgb else None
        if self.rgb:
            lum = 0.299 * self.rgb[0] + 0.587 * self.rgb[1] + 0.114 * self.rgb[2]
            self.setText('#%02x%02x%02x' % self.rgb)
            self.setStyleSheet('background-color: rgb(%d,%d,%d); color: %s; padding: 5px 14px;' % (self.rgb + ('#000' if lum > 140 else '#fff',)))
        else:
            self.setText('not found')
            self.setStyleSheet('')
        self.setEnabled(self.rgb is not None)
        if not quiet:
            self.changed.emit()

    def _pick(self):
        if not self.rgb:
            return
        c = QColorDialog.getColor(QColor(*self.rgb), self, self.label)
        if c.isValid():
            self.set_rgb((c.red(), c.green(), c.blue()), quiet=False)


class LoadTab(BaseTab):
    """Edit the accent colours and the artwork (two 256x256 textures stitched into 512x256) of a loading-screen pck."""

    def __init__(self, win):
        super().__init__(win)
        self.info = None
        self.orig = {}                       # colours as read from the pck
        self._prev_token = 0
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel('<i>Loading screens (loading_*.pck, loadscreen_*.pck) have an accent colour and an artwork that is two textures stitched '
                             'side by side. Change either or both; only what you change is written.</i>'))

        g = QGroupBox('1. Loading screen PCK')
        f = QFormLayout(g)
        self.pck = PathRow('open', ['.pck'], 'PCK files (*.pck);;All files (*)', 'Drop a loading screen .pck here', self.start_dir)
        f.addRow('PCK file', self.pck)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        f.addRow(self.summary)
        lay.addWidget(g)

        g = QGroupBox('2. Accent colour')
        f = QFormLayout(g)
        self.c_bar = ColorButton('Loader bar colour')
        self.c_text = ColorButton('Big text colour')
        self.c_tint = ColorButton('Strip tint colour')
        self.c_bar.setToolTip('Fill of the loader bar')
        self.c_text.setToolTip('The big translucent text behind the screen (the originals use a brighter shade than the bar, except the green one)')
        self.c_tint.setToolTip('What the white strip pattern at the bottom becomes (stored as a colour transform: multiplier + add)')
        row = QHBoxLayout()
        row.addWidget(self.c_bar)
        row.addWidget(self.c_text)
        row.addWidget(self.c_tint)
        row.addStretch()
        f.addRow('Bar / Text / Strip tint', row)
        row = QHBoxLayout()
        self.btn_all = QPushButton('Set all from one colour\u2026')
        self.btn_all.setToolTip('Bar and text get the colour, the strip tint gets it at half saturation and half brightness (how loading_generic_a is built)')
        self.btn_reset = QPushButton('Reset colours')
        row.addWidget(self.btn_all)
        row.addWidget(self.btn_reset)
        row.addStretch()
        f.addRow(row)
        lay.addWidget(g)

        g = QGroupBox('3. Artwork')
        f = QFormLayout(g)
        self.preview = QLabel('(open a loading screen pck)')
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumHeight(270)
        f.addRow(self.preview)
        self.img = PathRow('open', None, 'Images (*.png *.jpg *.jpeg *.bmp *.webp *.gif *.tif *.tiff);;All files (*)',
                           'Optional: drop the new artwork image here', self.start_dir)
        self.fit = QComboBox()
        self.fit.addItem('Crop to fill (no distortion)', 'cover')
        self.fit.addItem('Stretch to fill', 'stretch')
        self.fit.addItem('Fit inside (black bars)', 'contain')
        self.fit.setToolTip('The artwork is shown at about the aspect ratio of the preview. "Crop" cuts your image to it.')
        self.pal = QComboBox()
        self.pal.addItem('Shared palette (no seam between the halves)', 'shared')
        self.pal.addItem('Separate palettes (more colours, may show a seam)', 'separate')
        self.mask = QCheckBox('Keep the original rounded corners')
        self.mask.setChecked(True)
        self.dither = QCheckBox('Dither colours (smoother gradients, adds grain)')
        f.addRow('New artwork', self.img)
        f.addRow('Fit', self.fit)
        f.addRow('Palette', self.pal)
        f.addRow(self.mask)
        f.addRow(self.dither)
        row = QHBoxLayout()
        self.btn_exp = QPushButton('Export current artwork as PNG\u2026')
        row.addWidget(self.btn_exp)
        row.addStretch()
        f.addRow(row)
        lay.addWidget(g)

        g = QGroupBox('4. Output')
        f = QFormLayout(g)
        self.dst = PathRow('save', None, 'PCK files (*.pck)', 'Output .pck', self.start_dir)
        f.addRow('Output PCK', self.dst)
        lay.addWidget(g)

        row = QHBoxLayout()
        self.run = QPushButton('Save loading screen PCK')
        self.btn_flash = QPushButton('Preview result in Flash Player')
        self.run.clicked.connect(self.save)
        self.btn_flash.clicked.connect(lambda: win.preview_pck(self.dst.text()))
        row.addWidget(self.run)
        row.addWidget(self.btn_flash)
        row.addStretch()
        lay.addLayout(row)
        lay.addStretch()

        self.pck.changed.connect(self._pck_changed)
        for w in (self.img.changed, self.fit.currentIndexChanged, self.pal.currentIndexChanged, self.mask.toggled, self.dither.toggled):
            w.connect(self._update_preview)
        self.btn_all.clicked.connect(self.set_all)
        self.btn_reset.clicked.connect(self.reset_colors)
        self.btn_exp.clicked.connect(self.export_art)
        self.dst.changed.connect(self._refresh)
        self._refresh()

    def _refresh(self, *_):
        ok = self.info is not None
        self.run.setEnabled(ok)
        self.btn_all.setEnabled(ok and any(self.orig.get(k) for k in ('bar', 'text', 'tint')))
        self.btn_reset.setEnabled(ok)
        self.btn_exp.setEnabled(ok and bool(self.info.get('art')))
        self.img.setEnabled(ok and bool(self.info.get('art')) and not self.info['art']['unsupported'])
        self.btn_flash.setEnabled(os.path.isfile(self.dst.text()))

    # ---- loading --------------------------------------------------------------------------------------------------
    def _pck_changed(self, t):
        self.info = None
        self.orig = {}
        for b in (self.c_bar, self.c_text, self.c_tint):
            b.set_rgb(None)
        self.summary.setText('')
        self._refresh()
        if not t or not os.path.isfile(t):
            return
        self.dst.suggest(os.path.splitext(t)[0] + '_edited.pck')
        self.win.log('gui', 'Scanning %s' % t)
        self.win.run_job(lambda: ops.load_scan(t), lambda ok, res: self._scanned(t, ok, res), [self.pck])

    def _scanned(self, path, ok, res):
        if path != self.pck.text():
            return
        if not ok:
            return self.win.job_finished_message(False, 'Scan')
        self.info = res
        self.orig = dict(res['colors'])
        self.summary.setText(res['text'].replace('\n', '<br>'))
        self.reset_colors()
        if not res['art'] and not any(self.orig.values()):
            self.info = None
            self.win.error('This does not look like a loading screen pck: no artwork and no accent colours were found.')
        self._refresh()
        self._update_preview()

    def reset_colors(self):
        self.c_bar.set_rgb(self.orig.get('bar'))
        self.c_text.set_rgb(self.orig.get('text'))
        self.c_tint.set_rgb(self.orig.get('tint'))

    def set_all(self):
        start = self.orig.get('text') or self.orig.get('bar') or (128, 128, 128)
        c = QColorDialog.getColor(QColor(*start), self, 'Accent colour')
        if not c.isValid():
            return
        import colorsys
        r, g, b = c.red(), c.green(), c.blue()
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        tint = tuple(int(round(x * 255)) for x in colorsys.hsv_to_rgb(h, s * 0.5, v * 0.5))
        if self.orig.get('bar'):
            self.c_bar.set_rgb((r, g, b))
        if self.orig.get('text'):
            self.c_text.set_rgb((r, g, b))
        if self.orig.get('tint'):
            self.c_tint.set_rgb(tint)

    # ---- artwork preview ------------------------------------------------------------------------------------------
    def _update_preview(self, *_):
        t = self.pck.text()
        if not self.info or not self.info.get('art'):
            return
        if self.win.preview_dir is None:
            self.win.preview_dir = ops.new_preview_dir()
        self._prev_token += 1
        token = self._prev_token
        out = os.path.join(self.win.preview_dir, 'loadart_%d.png' % token)
        img = self.img.text() if os.path.isfile(self.img.text()) else None
        kw = dict(image=img, fit=self.fit.currentData(), keep_mask=self.mask.isChecked(), dither=self.dither.isChecked(),
                  palette=self.pal.currentData())

        def done(ok, res):
            if token != self._prev_token:
                return
            if ok:
                self.preview.setPixmap(QPixmap(res))
            else:
                self.preview.setText('(preview failed - see the log)')
        self.win.run_job(lambda: ops.load_preview(t, out, **kw), done)

    def export_art(self):
        t = self.pck.text()
        start = os.path.splitext(t)[0] + '_art.png'
        out, _ = QFileDialog.getSaveFileName(self, 'Export artwork', start, 'PNG (*.png)')
        if not out:
            return
        self.win.log('gui', 'Export artwork: %s' % out)
        self.win.run_job(lambda: ops.load_export_art(t, out), lambda ok, _r: self.win.job_finished_message(ok, 'Export'), [self.btn_exp])

    # ---- saving ---------------------------------------------------------------------------------------------------
    def save(self):
        src, dst = self.pck.text(), self.dst.text()
        if not self.info:
            return self.win.error('Please open a loading screen pck first.')
        if not dst:
            return self.win.error('Please choose an output .pck path.')
        if os.path.abspath(dst) == os.path.abspath(src):
            return self.win.error('The output must be a different file than the source pck (so the original stays untouched).')
        img = self.img.text()
        if img and not os.path.isfile(img):
            return self.win.error('The artwork image does not exist.')
        changed = {}
        for key, btn in (('bar', self.c_bar), ('text', self.c_text), ('tint', self.c_tint)):
            changed[key] = btn.rgb if btn.rgb and btn.rgb != self.orig.get(key) else None
        if not img and not any(changed.values()):
            return self.win.error('Nothing to change: pick different colours and/or an artwork image.')
        kw = dict(image=img or None, fit=self.fit.currentData(), keep_mask=self.mask.isChecked(), dither=self.dither.isChecked(),
                  palette=self.pal.currentData(), **changed)
        self.win.log('gui', 'Save loading screen: %s' % dst)
        self.win.run_job(lambda: ops.load_edit(src, dst, **kw), self._done, [self.run])

    def _done(self, ok, _res):
        self._refresh()
        self.win.job_finished_message(ok, 'Save loading screen')


class BrowserTab(BaseTab):
    def __init__(self, win):
        super().__init__(win)
        self.all = []
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.folder = QLabel()
        self.folder.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.sub = QCheckBox('Include subfolders')
        self.sub.toggled.connect(self.refresh)
        btn = QPushButton('Refresh')
        btn.clicked.connect(self.refresh)
        top.addWidget(self.folder, 1)
        top.addWidget(self.sub)
        top.addWidget(btn)
        lay.addLayout(top)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText('Filter\u2026')
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        lay.addWidget(self.filter)
        self.list = QListWidget()
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        self.list.itemDoubleClicked.connect(lambda it: self.win.preview_pck(it.data(Qt.UserRole)))
        lay.addWidget(self.list, 1)
        self.count = QLabel()
        lay.addWidget(self.count)
        self.refresh()

    def refresh(self, *_):
        d = self.win.settings.pck_dir
        self.all = []
        self.folder.setText('Folder: %s' % (d or '(not set \u2013 see Settings)'))
        if d and os.path.isdir(d):
            try:
                if self.sub.isChecked():
                    for root, _dirs, files in os.walk(d):
                        for fn in files:
                            if fn.lower().endswith('.pck'):
                                self.all.append(os.path.join(root, fn))
                else:
                    for fn in os.listdir(d):
                        p = os.path.join(d, fn)
                        if fn.lower().endswith('.pck') and os.path.isfile(p):
                            self.all.append(p)
            except OSError as e:
                self.win.error('Could not read the pck folder:\n%s' % e)
        elif d:
            self.win.error('The pck folder set in Settings does not exist:\n%s' % d)
        self.all.sort(key=lambda p: p.lower())
        self._apply_filter()

    def _apply_filter(self, *_):
        q = self.filter.text().lower()
        base = self.win.settings.pck_dir
        self.list.clear()
        for p in self.all:
            name = os.path.relpath(p, base) if base else p
            if q in name.lower():
                it = QListWidgetItem(name)
                it.setData(Qt.UserRole, p)
                it.setToolTip(p)
                self.list.addItem(it)
        self.count.setText('%d of %d pck file(s)' % (self.list.count(), len(self.all)))

    def _menu(self, pos):
        it = self.list.itemAt(pos)
        if not it:
            return
        path = it.data(Qt.UserRole)
        m = QMenu(self)
        a_copy = m.addAction('Copy full path')
        a_prev = m.addAction('Preview')
        act = m.exec(self.list.viewport().mapToGlobal(pos))
        if act is a_copy:
            QGuiApplication.clipboard().setText(path)
            self.win.log('gui', 'Copied to clipboard: %s' % path)
        elif act is a_prev:
            self.win.preview_pck(path)


# ----------------------------------------------------------------------------------------------------------------
# dialogs
# ----------------------------------------------------------------------------------------------------------------
class SettingsDialog(QDialog):
    def __init__(self, parent, settings):
        super().__init__(parent)
        self.setWindowTitle('Settings')
        self.setMinimumWidth(640)
        self.settings = settings
        form = QFormLayout()
        self.flash = PathRow('open', None, 'All files (*)', 'Flash Player (standalone projector) executable')
        self.jpexs = PathRow('open', None, 'All files (*)', 'JPEXS Free Flash Decompiler (ffdec / ffdec.sh / ffdec.jar)')
        self.pcks = PathRow('dir', None, placeholder='Folder full of .pck files')
        self.flash.setText(settings.flash)
        self.jpexs.setText(settings.jpexs)
        self.pcks.setText(settings.pck_dir)
        form.addRow('Flash Player path', self.flash)
        form.addRow('JPEXS path', self.jpexs)
        form.addRow('PCK folder', self.pcks)
        ok, cancel = QPushButton('Save'), QPushButton('Cancel')
        ok.setDefault(True)
        ok.clicked.connect(self._save)
        cancel.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(ok)
        row.addWidget(cancel)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addLayout(row)

    def _save(self):
        problems = []
        for label, row, is_dir in (('Flash Player', self.flash, False), ('JPEXS', self.jpexs, False), ('PCK folder', self.pcks, True)):
            t = row.text()
            if t and not (os.path.isdir(t) if is_dir else os.path.exists(t)):
                problems.append('%s: "%s" does not exist.' % (label, t))
        if problems:
            QMessageBox.warning(self, 'Invalid settings', '\n'.join(problems))
            return
        self.settings.set('flash_path', self.flash.text())
        self.settings.set('jpexs_path', self.jpexs.text())
        self.settings.set('pck_dir', self.pcks.text())
        self.accept()


class WelcomeDialog(QDialog):
    def __init__(self, parent, settings, open_settings):
        super().__init__(parent)
        self.setWindowTitle('Welcome')
        self.setMinimumWidth(560)
        self.settings, self.open_settings = settings, open_settings
        lbl = QLabel(
            '<h2>Welcome to %s</h2>'
            '<p>Convert MC3 (PS2) <b>.pck</b> UI files to Flash and back.</p>'
            '<ol>'
            '<li>Open <b>Settings</b> and set the paths to your <b>Flash Player</b>, <b>JPEXS</b> and the '
            '<b>folder with your .pck files</b>.</li>'
            '<li><b>PCK \u2192 SWF</b>: drop a .pck, export a preview SWF, then open it in JPEXS or the Flash Player.</li>'
            '<li><b>SWF \u2192 PCK</b>: turn an edited SWF (one that came from a pck) back into a .pck.</li>'
            '<li><b>%s</b>: build a brand-new .pck from a plain SWF, using an original pck as donor.</li>'
            '<li><b>%s</b>: replace the looping background movie of a menu pck (bg_arcade, bg_career, bg_options, \u2026) with your own GIF.</li>'
            '<li><b>%s</b>: change the accent colour and the artwork of a loading screen pck.</li>'
            '<li><b>PCK Browser</b>: right-click a file to copy its path or preview it.</li>'
            '</ol>'
            '<p>Everything the library does is logged in the console at the bottom of the window.</p>'
            % (html.escape(APP_NAME), html.escape(PORT_TAB_NAME), html.escape(ANIM_TAB_NAME), html.escape(LOAD_TAB_NAME)))
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.RichText)
        b_set, b_close, b_never = QPushButton('Open Settings'), QPushButton('Close'), QPushButton('Never show again')
        b_set.clicked.connect(self._settings)
        b_close.clicked.connect(self.accept)
        b_never.clicked.connect(self._never)
        row = QHBoxLayout()
        row.addWidget(b_never)
        row.addStretch()
        row.addWidget(b_set)
        row.addWidget(b_close)
        lay = QVBoxLayout(self)
        lay.addWidget(lbl)
        lay.addLayout(row)

    def _settings(self):
        self.accept()
        self.open_settings()

    def _never(self):
        self.settings.set('show_welcome', 'false')
        self.accept()


class AboutDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle('About %s' % APP_NAME)
        self.setMinimumWidth(520)
        lbl = QLabel(CREDITS_HTML)
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.RichText)
        lbl.setOpenExternalLinks(True)
        ok = QPushButton('Close')
        ok.clicked.connect(self.accept)
        lay = QVBoxLayout(self)
        lay.addWidget(lbl)
        lay.addWidget(ok, 0, Qt.AlignRight)


# ----------------------------------------------------------------------------------------------------------------
# main window
# ----------------------------------------------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, bridge):
        super().__init__()
        self.settings = Settings()
        self.jobs = set()
        self.preview_dir = None
        self.setWindowTitle(APP_NAME)
        self.resize(1000, 780)

        self.console = Console()
        bridge.message.connect(self.console.append)

        self.tabs = QTabWidget()
        self.tab_p2s = PckToSwfTab(self)
        self.tab_s2p = SwfToPckTab(self)
        self.tab_port = SwfPortTab(self)
        self.tab_anim = AnimTab(self)
        self.tab_load = LoadTab(self)
        self.tab_browser = BrowserTab(self)
        self.tabs.addTab(self.tab_p2s.scrolled(), 'PCK \u2192 SWF')
        self.tabs.addTab(self.tab_s2p.scrolled(), 'SWF \u2192 PCK')
        self.tabs.addTab(self.tab_port.scrolled(), PORT_TAB_NAME)
        self.tabs.addTab(self.tab_anim.scrolled(), ANIM_TAB_NAME)
        self.tabs.addTab(self.tab_load.scrolled(), LOAD_TAB_NAME)
        self.tabs.addTab(self.tab_browser, 'PCK Browser')

        split = QSplitter(Qt.Vertical)
        split.addWidget(self.tabs)
        split.addWidget(self.console)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setSizes([540, 220])
        self.setCentralWidget(split)

        mb = self.menuBar()
        act_settings = mb.addAction('&Settings')           # top-level entry: opens the window directly
        act_settings.triggered.connect(self.open_settings)
        help_menu = mb.addMenu('&Help')
        a_welcome = QAction('&Welcome window', self)
        a_about = QAction('&About', self)
        a_welcome.triggered.connect(self.show_welcome)
        a_about.triggered.connect(lambda: AboutDialog(self).exec())
        help_menu.addAction(a_welcome)
        help_menu.addSeparator()
        help_menu.addAction(a_about)

        self.statusBar().showMessage('Ready')
        self.log('gui', '%s v%s ready.' % (APP_NAME, APP_VERSION))

    # ---- helpers ------------------------------------------------------------------------------------------------
    def log(self, level, text):
        self.console.append(level, text)

    def error(self, msg):
        """GUI error -> popup (library errors go to the console instead)."""
        self.log('error', msg.replace('\n', ' '))
        QMessageBox.warning(self, 'Error', msg)

    def run_job(self, fn, done=None, widgets=()):
        job = Job(fn, self)
        job.on_done, job.widgets = done, list(widgets)
        for w in job.widgets:
            w.setEnabled(False)
        job.done.connect(self._job_done)
        job.finished.connect(self._prune_jobs)
        self.jobs.add(job)
        self.statusBar().showMessage('Working\u2026')
        job.start()
        return job

    @Slot(object, bool, object)
    def _job_done(self, job, ok, res):
        for w in job.widgets:
            w.setEnabled(True)
        try:
            if job.on_done:
                job.on_done(ok, res)
        except Exception:
            sys.excepthook(*sys.exc_info())
        if not any(j is not job and j.isRunning() for j in self.jobs):
            self.statusBar().showMessage('Ready')

    @Slot()
    def _prune_jobs(self):
        for j in [j for j in self.jobs if j.isFinished()]:
            self.jobs.discard(j)
            j.deleteLater()

    def job_finished_message(self, ok, what):
        if ok:
            self.log('ok', '%s finished.' % what)
        else:
            self.log('error', '%s failed \u2013 see the messages above.' % what)
            self.statusBar().showMessage('%s failed (see console)' % what)

    # ---- external tools ---------------------------------------------------------------------------------------
    def _need_tool(self, path, what):
        if path and os.path.exists(path):
            return True
        box = QMessageBox(QMessageBox.Warning, 'Path not set',
                          'The %s path is %s.\nSet it in Settings first.' % (what, 'invalid' if path else 'not set'), parent=self)
        b = box.addButton('Open Settings', QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Close)
        box.exec()
        if box.clickedButton() is b:
            self.open_settings()
        return False

    def _launch(self, tool, target, what):
        try:
            lp = ops.launch(tool, target)
        except OSError as e:
            return self.error('Could not start %s:\n%s\n\nCommand: %s' % (what, e, ' '.join(ops.build_command(tool, target))))
        self.log('gui', 'Started %s: %s' % (what, ' '.join('"%s"' % c if ' ' in c else c for c in lp.cmd)))

        def check():                                      # a program that dies right away is reported in the console
            err = lp.check()
            if err:
                self.log('error', '%s %s' % (what, err))
            try:
                if lp.proc.poll() is not None:
                    os.remove(lp.errfile)
            except OSError:
                pass
        QTimer.singleShot(2500, check)

    def open_with_flash(self, swf):
        if not os.path.isfile(swf):
            return self.error('The SWF does not exist:\n%s' % swf)
        if self._need_tool(self.settings.flash, 'Flash Player'):
            self._launch(self.settings.flash, swf, 'Flash Player')

    def open_with_jpexs(self, swf):
        if not os.path.isfile(swf):
            return self.error('The SWF does not exist:\n%s' % swf)
        if self._need_tool(self.settings.jpexs, 'JPEXS'):
            self._launch(self.settings.jpexs, swf, 'JPEXS')

    def preview_pck(self, pck):
        if not os.path.isfile(pck):
            return self.error('The file does not exist:\n%s' % pck)
        if not self._need_tool(self.settings.flash, 'Flash Player'):
            return
        if self.preview_dir is None:
            self.preview_dir = ops.new_preview_dir()
        d = self.preview_dir
        self.log('gui', 'Preview: %s' % pck)

        def done(ok, res):
            if ok:
                self.open_with_flash(res)
            else:
                self.job_finished_message(False, 'Preview')
        self.run_job(lambda: ops.preview_swf(pck, d), done)

    # ---- dialogs ------------------------------------------------------------------------------------------------
    def open_settings(self):
        if SettingsDialog(self, self.settings).exec() == QDialog.Accepted:
            self.log('gui', 'Settings saved.')
            self.tab_browser.refresh()

    def show_welcome(self):
        WelcomeDialog(self, self.settings, self.open_settings).exec()

    def closeEvent(self, e):
        running = [j for j in self.jobs if j.isRunning()]
        if running:
            r = QMessageBox.question(self, 'Quit', 'A conversion is still running. Quit anyway?')
            if r != QMessageBox.Yes:
                e.ignore()
                return
            for j in running:
                j.terminate()
                j.wait(2000)
        if self.preview_dir:
            ops.remove_dir(self.preview_dir)
        e.accept()


# ----------------------------------------------------------------------------------------------------------------
# theme + entry point
# ----------------------------------------------------------------------------------------------------------------
def apply_theme(app):
    """qt-themes Monokai; falls back to a hand-made Monokai palette if the module is missing."""
    try:
        import qt_themes
        qt_themes.set_theme('monokai')
        return None
    except Exception as e:                                   # noqa: BLE001
        app.setStyle('Fusion')
        pal = QPalette()
        c = QColor
        for role, col in ((QPalette.Window, MK['bg']), (QPalette.WindowText, MK['fg']), (QPalette.Base, MK['bg2']),
                          (QPalette.AlternateBase, MK['bg']), (QPalette.Text, MK['fg']), (QPalette.Button, MK['sel']),
                          (QPalette.ButtonText, MK['fg']), (QPalette.ToolTipBase, MK['sel']), (QPalette.ToolTipText, MK['fg']),
                          (QPalette.Highlight, MK['green']), (QPalette.HighlightedText, MK['bg']),
                          (QPalette.Link, MK['blue']), (QPalette.PlaceholderText, MK['comment'])):
            pal.setColor(role, c(col))
        app.setPalette(pal)
        return 'qt-themes unavailable (%s); using built-in Monokai palette. Install it with: pip install qt-themes' % e


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    theme_note = apply_theme(app)

    bridge = LogBridge()
    sys.stdout = LogStream(bridge, False)
    sys.stderr = LogStream(bridge, True)

    win = MainWindow(bridge)
    if theme_note:
        win.log('warn', theme_note)

    def hook(et, ev, tb):                                    # uncaught GUI exceptions -> console + popup
        text = ''.join(traceback.format_exception(et, ev, tb))
        sys.stderr.write(text)
        QMessageBox.critical(win, 'Unexpected GUI error', '%s: %s\n\nDetails are in the console.' % (et.__name__, ev))
    sys.excepthook = hook

    win.show()
    if win.settings.show_welcome:
        QTimer.singleShot(0, win.show_welcome)
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
