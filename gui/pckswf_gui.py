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
from pckload import DEFAULT_FRAME                # noqa: E402  (bundled assets/frame.svg)
import pckbitmaps                                # noqa: E402  (bitmap viewer / exporter / replacer)
import rpck                                      # noqa: E402  (rpck.replacing: save over the source pck, keeping it as .old)

from PySide6.QtCore import Qt, QObject, QSize, QThread, Signal, Slot, QSettings, QTimer   # noqa: E402
from PySide6.QtGui import (QAction, QColor, QFont, QGuiApplication, QIcon, QImage, QMovie, QPainter, QPalette, QPen,   # noqa: E402
                           QPixmap)
from PySide6.QtWidgets import (                                                         # noqa: E402
    QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox, QSplitter, QTabWidget,
    QVBoxLayout, QWidget)

APP_NAME = 'PCK <-> SWF Tools'
APP_VERSION = '1.0.0'
PORT_TAB_NAME = 'Port SWF \u2192 New PCK'        # the "swf import" tab; rename here if you prefer another name
ANIM_TAB_NAME = 'Animated BG (GIF)'              # the "replace a looping background movie with a GIF" tab
LOAD_TAB_NAME = 'Loading Screen'                 # the "loading screen accent colour + artwork" tab
BITMAP_TAB_NAME = 'Bitmaps'                      # the "view / export / replace the bitmaps of a pck" tab

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
        self.dither = QCheckBox('Dither colours (smoother gradients, adds grain)')
        f.addRow('New artwork', self.img)
        f.addRow('Fit', self.fit)
        f.addRow('Palette', self.pal)
        f.addRow(self.dither)
        self.use_frame = QCheckBox('Draw a frame over the artwork')
        self.use_frame.setToolTip('Draws the frame (a thin line along the left and bottom edge by default) over the artwork and cuts the artwork along it, so the '
                                  'rounded corner follows the frame (smooth edge). Without a frame the original rounded corners are kept. Without a new '
                                  'image it is drawn over the current artwork, which is then re-encoded (256 colours per texture, as before).')
        self.frame = PathRow('open', None, 'Frame (*.svg *.png *.webp);;All files (*)', 'Frame SVG / PNG with transparency', self.start_dir)
        self.frame.setText(DEFAULT_FRAME)
        self.frame.setEnabled(False)
        f.addRow(self.use_frame)
        f.addRow('Frame file', self.frame)
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
        for w in (self.img.changed, self.fit.currentIndexChanged, self.pal.currentIndexChanged, self.dither.toggled,\
                  self.use_frame.toggled, self.frame.changed):
            w.connect(self._update_preview)
        self.use_frame.toggled.connect(self.frame.setEnabled)
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
        art_ok = ok and bool(self.info.get('art')) and not self.info['art']['unsupported']
        self.img.setEnabled(art_ok)
        self.use_frame.setEnabled(art_ok)
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
        kw = dict(image=img, fit=self.fit.currentData(), keep_mask=True, dither=self.dither.isChecked(),
                  palette=self.pal.currentData(), frame=self._frame_path())

        def done(ok, res):
            if token != self._prev_token:
                return
            if ok:
                self.preview.setPixmap(QPixmap(res))
            else:
                self.preview.setText('(preview failed - see the log)')
        self.win.run_job(lambda: ops.load_preview(t, out, **kw), done)

    def _frame_path(self):
        """the frame file to draw, or None when the option is off / the file is missing"""
        p = self.frame.text()
        return p if self.use_frame.isChecked() and os.path.isfile(p) else None

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
        img = self.img.text()
        if img and not os.path.isfile(img):
            return self.win.error('The artwork image does not exist.')
        changed = {}
        for key, btn in (('bar', self.c_bar), ('text', self.c_text), ('tint', self.c_tint)):
            changed[key] = btn.rgb if btn.rgb and btn.rgb != self.orig.get(key) else None
        if self.use_frame.isChecked() and not self._frame_path():
            return self.win.error('The frame file does not exist.')
        frame = self._frame_path()
        if not img and not frame and not any(changed.values()):
            return self.win.error('Nothing to change: pick different colours, an artwork image and/or a frame.')
        kw = dict(image=img or None, fit=self.fit.currentData(), keep_mask=True, dither=self.dither.isChecked(),
                  palette=self.pal.currentData(), frame=frame, **changed)
        self.win.log('gui', 'Save loading screen: %s' % dst)
        self.win.run_job(lambda: ops.load_edit(src, dst, **kw), self._done, [self.run])

    def _done(self, ok, _res):
        self._refresh()
        self.win.job_finished_message(ok, 'Save loading screen')


# ----------------------------------------------------------------------------------------------------------------
# bitmaps tab: grid browser + detail dialog
# ----------------------------------------------------------------------------------------------------------------
THUMB_MAX = 512                                   # thumbnails are cached at most this big (the grid/dialog scale them)
IMAGE_EXTS = ['.png', '.jpg', '.jpeg', '.bmp', '.webp', '.gif', '.tif', '.tiff']
IMAGE_FILTER = 'Images (*.png *.jpg *.jpeg *.bmp *.webp *.gif *.tif *.tiff);;All files (*)'
FIT_CHOICES = (('Crop to fill (no distortion)', 'cover'), ('Fit inside (transparent bars)', 'contain'),
               ('Stretch to fill (may distort)', 'stretch'))
_CHECKER = []


def pil_to_qimage(im):
    """PIL image -> detached QImage (RGBA)."""
    im = im.convert('RGBA')
    return QImage(im.tobytes('raw', 'RGBA'), im.width, im.height, 4 * im.width, QImage.Format_RGBA8888).copy()


def checker_pixmap(cell=8):
    """Checkerboard tile, so transparent pixels of a bitmap are visible."""
    if not _CHECKER:
        pm = QPixmap(cell * 2, cell * 2)
        pm.fill(QColor(72, 72, 72))
        p = QPainter(pm)
        p.fillRect(0, 0, cell, cell, QColor(100, 100, 100))
        p.fillRect(cell, cell, cell, cell, QColor(100, 100, 100))
        p.end()
        _CHECKER.append(pm)
    return _CHECKER[0]


def fitted_pixmap(img, box):
    """QImage -> QPixmap scaled to fit `box` (QSize) with its aspect ratio kept, painted over a checkerboard."""
    w, h = img.width(), img.height()
    k = min(max(1, box.width()) / w, max(1, box.height()) / h)
    nw, nh = max(1, round(w * k)), max(1, round(h * k))
    mode = Qt.FastTransformation if k > 1 else Qt.SmoothTransformation          # small bitmaps stay crisp when enlarged
    scaled = img.scaled(QSize(nw, nh), Qt.IgnoreAspectRatio, mode)
    pm = QPixmap(nw, nh)
    p = QPainter(pm)
    p.drawTiledPixmap(0, 0, nw, nh, checker_pixmap())
    p.drawImage(0, 0, scaled)
    p.end()
    return pm


def make_tile(img, s, modified=False):
    """Grid icon: the bitmap at its true aspect ratio, centred in an s x s cell."""
    pm = QPixmap(s, s)
    pm.fill(Qt.transparent)
    inner = fitted_pixmap(img, QSize(s - 6, s - 6))
    x, y = (s - inner.width()) // 2, (s - inner.height()) // 2
    p = QPainter(pm)
    p.drawPixmap(x, y, inner)
    p.setPen(QPen(QColor(MK['orange'] if modified else '#6a6a62'), 3 if modified else 1))
    p.setBrush(Qt.NoBrush)
    p.drawRect(x, y, inner.width() - 1, inner.height() - 1)
    p.end()
    return QIcon(pm)


def placeholder_tile(s, text):
    pm = QPixmap(s, s)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setPen(QColor(MK['comment']))
    f = p.font()
    f.setPointSize(max(10, s // 6))
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, text)
    p.end()
    return QIcon(pm)


class ThumbLoader(QThread):
    """Decodes the bitmaps of a (cloned) BitmapSet one by one so the grid fills while the UI stays responsive."""
    ready = Signal(int, int, object, str)           # generation, bitmap index, QImage or None, error text

    def __init__(self, gen, bset, indices, parent=None):
        super().__init__(parent)
        self.gen, self.bset, self.indices = gen, bset, list(indices)
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        for i in self.indices:
            if self._stop:
                return
            try:
                im = self.bset.image(i)
                im.thumbnail((THUMB_MAX, THUMB_MAX))
                self.ready.emit(self.gen, i, pil_to_qimage(im), '')
            except Exception as e:                   # an unsupported texture must not stop the others
                self.ready.emit(self.gen, i, None, str(e))


class PreviewPane(QLabel):
    """Shows a QImage scaled to the available space (aspect ratio kept, checkerboard behind it)."""

    def __init__(self, text=''):
        super().__init__(text)
        self._img, self._text = None, text
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(220, 160)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)

    def set_image(self, img, text=None):
        self._img = img
        if text is not None:
            self._text = text
        self._redraw()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._redraw()

    def _redraw(self):
        if self._img is None:
            self.clear()
            self.setText(self._text)
        else:
            self.setPixmap(fitted_pixmap(self._img, QSize(max(1, self.width() - 8), max(1, self.height() - 8))))


class BitmapGrid(QListWidget):
    """Icon grid. leftClicked is emitted for a plain left click (not with Ctrl/Shift, which select several)."""
    leftClicked = Signal(object)

    def __init__(self):
        super().__init__()
        self.setViewMode(QListWidget.IconMode)
        self.setResizeMode(QListWidget.Adjust)
        self.setMovement(QListWidget.Static)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setUniformItemSizes(True)
        self.setWordWrap(True)
        self.setSpacing(4)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self._pressed = None

    def mousePressEvent(self, e):
        self._pressed = self.itemAt(e.position().toPoint()) if e.button() == Qt.LeftButton else None
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        it = self.itemAt(e.position().toPoint())
        plain = not bool(e.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier))
        super().mouseReleaseEvent(e)
        if e.button() == Qt.LeftButton and plain and it is not None and it is self._pressed:
            self.leftClicked.emit(it)
        self._pressed = None


class BitmapDialog(QDialog):
    """One bitmap: current picture, export as PNG, replace with a new image (with a preview of the quantised result)."""
    changed = Signal(int)

    def __init__(self, tab, index, replace_now=False):
        super().__init__(tab.win)
        self.tab, self.bset, self.index = tab, tab.bset, index
        self._src_path, self._src_img = None, None
        self.setWindowTitle('Bitmap #%d' % index)
        self.resize(940, 700)
        lay = QVBoxLayout(self)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.RichText)
        lay.addWidget(self.info)

        split = QSplitter(Qt.Horizontal)
        for attr, title, text in (('cur', 'Current', ''), ('new', 'Replacement (as the game will show it: palette-reduced)',
                                                           '(choose an image below)')):
            box = QWidget()
            bl = QVBoxLayout(box)
            bl.setContentsMargins(0, 0, 0, 0)
            bl.addWidget(QLabel('<b>%s</b>' % title))
            pane = PreviewPane(text)
            setattr(self, attr, pane)
            bl.addWidget(pane, 1)
            split.addWidget(box)
        lay.addWidget(split, 1)

        g = QGroupBox('Export')
        h = QHBoxLayout(g)
        self.restore = QCheckBox()
        self.restore.setChecked(True)
        self.btn_export = QPushButton('Export as PNG\u2026')
        h.addWidget(self.restore, 1)
        h.addWidget(self.btn_export)
        lay.addWidget(g)

        g = QGroupBox('Replace')
        f = QFormLayout(g)
        self.img = PathRow('open', IMAGE_EXTS, IMAGE_FILTER, 'Drop a PNG / JPG / \u2026 here or browse', tab.start_dir)
        self.fit = QComboBox()
        for label, val in FIT_CHOICES:
            self.fit.addItem(label, val)
        self.ref = QComboBox()
        self.ref.addItem('As drawn in the game (when known)', 'auto')
        self.ref.addItem('Stored source size', 'source')
        self.ref.setToolTip('The aspect ratio the new image is fitted to. \"As drawn\" uses the proportions the bitmap actually has on '
                            'screen (the shape that uses it, including its placement scale); \"Stored source size\" uses the size '
                            'recorded in the bitmap itself. They only differ for some bitmaps.')
        self.dither = QCheckBox('Dither colours (smoother gradients, adds grain)')
        self.warn = QLabel()
        self.warn.setWordWrap(True)
        f.addRow('New image', self.img)
        f.addRow('Aspect ratio', self.ref)
        f.addRow('Fit', self.fit)
        f.addRow(self.dither)
        f.addRow(self.warn)
        row = QHBoxLayout()
        self.btn_apply = QPushButton('Apply replacement')
        self.btn_revert = QPushButton('Revert to original')
        row.addWidget(self.btn_apply)
        row.addWidget(self.btn_revert)
        row.addStretch()
        f.addRow(row)
        lay.addWidget(g)

        row = QHBoxLayout()
        self.hint = QLabel('<i>Replacements stay in memory until you use \u201cSave PCK as\u2026\u201d on the Bitmaps tab.</i>')
        close = QPushButton('Close')
        close.clicked.connect(self.accept)
        row.addWidget(self.hint, 1)
        row.addWidget(close)
        lay.addLayout(row)

        self.img.changed.connect(self._update_new)
        self.fit.currentIndexChanged.connect(self._update_new)
        self.ref.currentIndexChanged.connect(self._update_new)
        self.dither.toggled.connect(self._update_new)
        self.btn_export.clicked.connect(self._export)
        self.btn_apply.clicked.connect(self._apply)
        self.btn_revert.clicked.connect(self._revert)
        self._refresh_current()
        self._update_new()
        if replace_now:
            QTimer.singleShot(0, self.img.browse)

    # ---- current state ----
    def _refresh_current(self):
        d = self.bset.info(self.index)
        self.d = d
        if not d['ok']:
            self.info.setText('<b>#%d</b> &ndash; this bitmap cannot be shown or edited: %s' % (self.index, html.escape(d['error'])))
            self.cur.set_image(None, 'unsupported')
            for w in (self.btn_export, self.btn_apply, self.img, self.fit, self.ref, self.dither):
                w.setEnabled(False)
            self.btn_revert.setEnabled(False)
            return
        s = ('<b>#%d</b> &nbsp; %d \u00d7 %d px (aspect %.3f : 1) &nbsp;&middot;&nbsp; %d-bit palette, %d colours'
             % (self.index, d['src_w'], d['src_h'], d['aspect'], d['bpp'], d['ncol']))
        drawn = d['screen_aspect'] and abs(d['screen_aspect'] / d['aspect'] - 1) >= 0.01
        if d['resized'] and not drawn:
            s += ('<br>Stored as a %d \u00d7 %d texture that the game stretches back to %d \u00d7 %d, so that is the size shown here.'
                  % (d['tex_w'], d['tex_h'], d['src_w'], d['src_h']))
        if drawn:
            s += ('<br>Stored as a %d \u00d7 %d texture, but the game draws it at <b>%.3f : 1</b> (%s), so it is shown at %d \u00d7 %d here '
                  'and new images are fitted to that aspect.'
                  % (d['tex_w'], d['tex_h'], d['screen_aspect'],
                     'its shape scaled by the placement' if d['screen_basis'] == 'placement' else 'its shape',
                     d['auto_size'][0], d['auto_size'][1]))
        elif d['screen_note']:
            s += '<br><span style=\"color:%s\">%s &ndash; using the stored source size.</span>' % (MK['orange'], html.escape(d['screen_note'][:1].upper() + d['screen_note'][1:]))
        if d['shared_with']:
            s += ('<br><span style="color:%s">Shares its pixel data with %s &ndash; replacing it changes those too.</span>'
                  % (MK['orange'], ', '.join('#%d' % j for j in d['shared_with'])))
        if d['modified']:
            s += '<br><span style="color:%s">Replaced (not saved yet).</span>' % MK['orange']
        self.info.setText(s)
        shown = d['auto_size'] != (d['tex_w'], d['tex_h'])
        self.restore.setText('Restore the true proportions (%d \u00d7 %d)' % d['auto_size']
                             if shown else 'Export at the stored size (%d \u00d7 %d)' % (d['tex_w'], d['tex_h']))
        self.restore.setEnabled(shown)
        self.btn_revert.setEnabled(d['modified'])
        try:
            self.cur.set_image(pil_to_qimage(self.bset.image(self.index)))
        except Exception as e:
            self.cur.set_image(None, 'cannot decode: %s' % e)

    # ---- replacement preview ----
    def _source(self, path):
        if path != self._src_path:
            self._src_img, self._src_path = pckbitmaps.load_image(path), path
        return self._src_img

    def _update_new(self, *_):
        path = self.img.text()
        ok = False
        self.warn.setText('')
        if not self.d['ok']:
            return
        if not path or not os.path.isfile(path):
            self.new.set_image(None, '(choose an image below)' if not path else 'file not found')
        else:
            try:
                src = self._source(path)
                ref = self.ref.currentData()
                pv = self.bset.preview_replacement(self.index, src, self.fit.currentData(), self.dither.isChecked(), True, ref)
                self.new.set_image(pil_to_qimage(pv))
                ok = True
                sa, da = src.width / src.height, self.bset.reference_aspect(self.d, ref)
                if abs(sa / da - 1) > 0.03:
                    how = {'cover': 'cropped to fit', 'contain': 'letterboxed with transparent bars',
                           'stretch': 'stretched (distorted)'}[self.fit.currentData()]
                    self.warn.setText('<span style="color:%s">Your image is %d \u00d7 %d (%.2f : 1) but this bitmap is %.2f : 1 &ndash; '
                                      'it will be %s.</span>' % (MK['orange'], src.width, src.height, sa, da, how))
                else:
                    self.warn.setText('Same aspect ratio as the bitmap.')
            except Exception as e:
                traceback.print_exc()
                self.new.set_image(None, 'Cannot use this image:\n%s' % e)
        self.btn_apply.setEnabled(ok)

    # ---- actions ----
    def _export(self):
        self.tab.export_indices([self.index], self.restore.isChecked() and self.restore.isEnabled(), self)

    def _apply(self):
        path = self.img.text()
        try:
            self.bset.replace(self.index, self._source(path), self.fit.currentData(), self.dither.isChecked(), self.ref.currentData())
        except Exception as e:
            traceback.print_exc()
            return self.tab.win.error('Could not replace the bitmap:\n%s' % e)
        self.tab.win.log('ok', 'Bitmap #%d replaced with %s (in memory - use "Save PCK as..." to write it)' % (self.index, path))
        self.img.setText('')
        self.changed.emit(self.index)
        self._refresh_current()
        self._update_new()

    def _revert(self):
        if self.bset.revert(self.index):
            self.tab.win.log('gui', 'Bitmap #%d reverted to the original' % self.index)
            self.changed.emit(self.index)
        self._refresh_current()
        self._update_new()


class BitmapsTab(BaseTab):
    """Grid browser for the bitmaps of a pck: click one to export it as PNG or replace it with a new image."""

    def __init__(self, win):
        super().__init__(win)
        self.bset = None
        self.loaded = ''                     # path of the pck the grid shows
        self.thumbs = {}                     # bitmap index -> QImage (at most THUMB_MAX px)
        self.items = {}                      # bitmap index -> QListWidgetItem
        self.loader = None
        self.gen = 0
        self.rev = self.saved_rev = 0        # change counter / value at the last save
        self._fresh = set()                  # indices re-rendered on the GUI thread (a late thumbnail must not overwrite them)
        self._reverting = False
        self._done = 0
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel('<i>Click a bitmap to open it (export as PNG, replace with a new image); right-click for the menu, Ctrl/Shift-click '
                             'selects several. Bitmaps are shown at their true proportions. Replacements stay in memory until you save a new pck.</i>'))
        self.pck = PathRow('open', ['.pck'], 'PCK files (*.pck);;All files (*)', 'Drop a .pck here', self.start_dir)
        row = QHBoxLayout()
        row.addWidget(QLabel('PCK file'))
        row.addWidget(self.pck, 1)
        lay.addLayout(row)

        row = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText('Filter (e.g. 20, 128x64, modified)\u2026')
        self.filter.setClearButtonEnabled(True)
        self.tile = QSlider(Qt.Horizontal)
        self.tile.setRange(64, 320)
        self.tile.setSingleStep(16)
        self.tile.setPageStep(32)
        self.tile.setFixedWidth(160)
        try:
            self.tile.setValue(int(self.win.settings.get('bitmap_tile', 160)))
        except (TypeError, ValueError):
            self.tile.setValue(160)
        row.addWidget(self.filter, 1)
        row.addWidget(QLabel('Size'))
        row.addWidget(self.tile)
        lay.addLayout(row)

        self.grid = BitmapGrid()
        lay.addWidget(self.grid, 1)

        row = QHBoxLayout()
        self.status = QLabel('(open a pck)')
        self.btn_save = QPushButton('Save PCK as\u2026')
        self.btn_revert_all = QPushButton('Revert all')
        self.btn_save.setToolTip('Writes the pck with the replaced bitmaps to a new file (choosing the original\'s own name keeps the original as <name>.pck.old)')
        row.addWidget(self.status, 1)
        row.addWidget(self.btn_revert_all)
        row.addWidget(self.btn_save)
        lay.addLayout(row)

        self._tile_timer = QTimer(self)
        self._tile_timer.setSingleShot(True)
        self._tile_timer.setInterval(80)
        self._tile_timer.timeout.connect(self._retile)
        self.pck.changed.connect(self._pck_changed)
        self.filter.textChanged.connect(self._apply_filter)
        self.tile.valueChanged.connect(lambda *_: self._tile_timer.start())
        self.grid.leftClicked.connect(lambda it: QTimer.singleShot(0, lambda: self.open_bitmap(it.data(Qt.UserRole))))
        self.grid.customContextMenuRequested.connect(self._menu)
        self.btn_save.clicked.connect(self.save)
        self.btn_revert_all.clicked.connect(self.revert_all)
        self._retile()
        self._refresh()

    # ---- state ----
    @property
    def dirty(self):
        return self.bset is not None and bool(self.bset.modified) and self.rev != self.saved_rev

    def confirm_discard(self):
        """True when there is nothing unsaved, or the user agrees to drop it."""
        if not self.dirty:
            return True
        r = QMessageBox.question(self, 'Unsaved bitmap changes',
                                 '%d replaced bitmap(s) have not been saved to a new pck. Discard them?' % len(self.bset.modified))
        return r == QMessageBox.Yes

    def _refresh(self):
        n_mod = len(self.bset.modified) if self.bset else 0
        self.btn_save.setEnabled(n_mod > 0)
        self.btn_revert_all.setEnabled(n_mod > 0)
        if not self.bset:
            return
        total = len(self.bset.indices)
        s = '%s: %d bitmap(s)' % (os.path.basename(self.loaded), total)
        if self._done < total:
            s += ' \u2013 decoding %d / %d\u2026' % (self._done, total)
        if n_mod:
            s += ' \u2013 %d replaced (unsaved)' % n_mod if self.dirty else ' \u2013 %d replaced (saved)' % n_mod
        self.status.setText(s)

    def selected_indices(self):
        return sorted(it.data(Qt.UserRole) for it in self.grid.selectedItems())

    # ---- loading ----
    def _stop_loader(self):
        if self.loader:
            self.loader.stop()
            self.loader.wait(3000)
            self.loader = None

    def _pck_changed(self, t):
        if self._reverting:
            return
        if t != self.loaded and not self.confirm_discard():
            self._reverting = True
            self.pck.setText(self.loaded)
            self._reverting = False
            return
        self._stop_loader()
        self.gen += 1
        self.bset, self.loaded = None, ''
        self.thumbs.clear()
        self.items.clear()
        self._fresh.clear()
        self.grid.clear()
        self.rev = self.saved_rev = 0
        self._done = 0
        self.status.setText('(open a pck)')
        self._refresh()
        if not t or not os.path.isfile(t):
            return
        self.win.log('gui', 'Reading bitmaps from %s' % t)
        try:
            bs = pckbitmaps.BitmapSet(t)
        except Exception as e:
            traceback.print_exc()
            return self.win.error('Could not read this pck:\n%s' % e)
        self.bset, self.loaded = bs, t
        if not bs.indices:
            self.status.setText('%s has no bitmaps.' % os.path.basename(t))
            return
        s = self.tile.value()
        for d in bs.listing():
            it = QListWidgetItem(placeholder_tile(s, '\u2026'), '')
            it.setData(Qt.UserRole, d['index'])
            self.items[d['index']] = it
            self._label(d['index'])
            self.grid.addItem(it)
        self._retile()
        self._apply_filter()
        self.loader = ThumbLoader(self.gen, bs.clone(), bs.indices, self)
        self.loader.ready.connect(self._thumb_ready)
        self.loader.start()
        self._refresh()

    @Slot(int, int, object, str)
    def _thumb_ready(self, gen, i, img, err):
        if gen != self.gen:
            return
        self._done += 1
        if i not in self._fresh and i in self.items:
            if img is None:
                self.items[i].setIcon(placeholder_tile(self.tile.value(), '?'))
                self.items[i].setToolTip('Bitmap #%d cannot be shown: %s' % (i, err))
            else:
                self.thumbs[i] = img
                self._icon(i)
        if self._done >= len(self.bset.indices) or self._done % 8 == 0:
            self._refresh()

    # ---- grid items ----
    def _label(self, i):
        d = self.bset.info(i)
        it = self.items[i]
        mod = i in self.bset.snaps
        it.setText('#%d%s\n%s' % (i, ' \u25cf' if mod else '', ('%d\u00d7%d' % d['auto_size']) if d['ok'] else 'unsupported'))
        f = it.font()
        f.setBold(mod)
        it.setFont(f)
        tip = describe_tip(d)
        if mod:
            tip += '\nReplaced (unsaved)' if self.dirty else '\nReplaced'
        it.setToolTip(tip)

    def _icon(self, i):
        img = self.thumbs.get(i)
        if img is not None:
            self.items[i].setIcon(make_tile(img, self.tile.value(), i in self.bset.snaps))

    def _retile(self):
        s = self.tile.value()
        self.win.settings.set('bitmap_tile', s)
        self.grid.setIconSize(QSize(s, s))
        self.grid.setGridSize(QSize(s + 16, s + 50))
        for i in self.items:
            if i in self.thumbs:
                self._icon(i)

    def _apply_filter(self, *_):
        q = self.filter.text().strip().lower().replace('x', '\u00d7')
        for i, it in self.items.items():
            hay = it.text().lower().replace('\n', ' ') + (' modified replaced' if i in self.bset.snaps else '')
            it.setHidden(bool(q) and q not in hay)

    def _changed(self, i):
        """A bitmap was replaced / reverted (from the dialog or the menu): re-render its tile on the GUI thread."""
        self.rev += 1
        self._fresh.add(i)
        try:
            im = self.bset.image(i)
            im.thumbnail((THUMB_MAX, THUMB_MAX))
            self.thumbs[i] = pil_to_qimage(im)
        except Exception:
            traceback.print_exc()
        self._label(i)
        self._icon(i)
        self._refresh()

    # ---- actions ----
    def open_bitmap(self, i, replace_now=False):
        if not self.bset or i not in self.items:
            return
        dlg = BitmapDialog(self, i, replace_now)
        dlg.changed.connect(self._changed)
        dlg.exec()

    def export_indices(self, idxs, restore=True, parent=None):
        """Export one bitmap to a chosen .png, or several into a chosen folder."""
        idxs = [i for i in idxs if self.bset and self.bset.info(i)['ok']]
        if not idxs:
            return
        parent = parent or self
        base = os.path.splitext(os.path.basename(self.loaded))[0]
        last = str(self.win.settings.get('bitmap_export_dir', '')) or self.start_dir()
        try:
            if len(idxs) == 1:
                i = idxs[0]
                path, _ = QFileDialog.getSaveFileName(parent, 'Export bitmap as PNG', os.path.join(last, '%s_bitmap_%d.png' % (base, i)),
                                                      'PNG image (*.png)')
                if not path:
                    return
                if not path.lower().endswith('.png'):
                    path += '.png'
                w, h = self.bset.export_png(i, path, restore)
                self.win.log('ok', 'Exported bitmap #%d -> %s (%d x %d)' % (i, path, w, h))
                self.win.settings.set('bitmap_export_dir', os.path.dirname(path))
            else:
                folder = QFileDialog.getExistingDirectory(parent, 'Export %d bitmaps as PNG into folder' % len(idxs), last)
                if not folder:
                    return
                n = 0
                for i in idxs:
                    try:
                        self.bset.export_png(i, os.path.join(folder, '%s_bitmap_%d.png' % (base, i)), restore)
                        n += 1
                    except Exception:
                        traceback.print_exc()
                self.win.log('ok', 'Exported %d of %d bitmaps into %s' % (n, len(idxs), folder))
                self.win.settings.set('bitmap_export_dir', folder)
        except Exception as e:
            traceback.print_exc()
            self.win.error('Export failed:\n%s' % e)

    def _menu(self, pos):
        it = self.grid.itemAt(pos)
        if not it:
            return
        if not it.isSelected():
            self.grid.clearSelection()
            it.setSelected(True)
        idxs = self.selected_indices()
        one = len(idxs) == 1
        usable = [i for i in idxs if self.bset.info(i)['ok']]
        m = QMenu(self)
        a_open = m.addAction('Open\u2026') if one else None
        a_exp = m.addAction('Export as PNG\u2026' if len(usable) <= 1 else 'Export %d images as PNG\u2026' % len(usable))
        a_exp.setEnabled(bool(usable))
        a_rep = m.addAction('Replace with image\u2026') if one else None
        if a_rep:
            a_rep.setEnabled(bool(usable))
        m.addSeparator()
        mods = [i for i in idxs if i in self.bset.snaps]
        a_rev = m.addAction('Revert to original')
        a_rev.setEnabled(bool(mods))
        act = m.exec(self.grid.viewport().mapToGlobal(pos))
        if act is None:
            return
        if act is a_open:
            self.open_bitmap(idxs[0])
        elif act is a_exp:
            self.export_indices(usable)
        elif act is a_rep:
            self.open_bitmap(idxs[0], replace_now=True)
        elif act is a_rev:
            for i in mods:
                self.bset.revert(i)
                self._changed(i)

    def revert_all(self):
        if not self.bset or not self.bset.modified:
            return
        for i in list(self.bset.modified):
            self.bset.revert(i)
            self._changed(i)
        self.win.log('gui', 'All bitmaps reverted to the original')

    def save(self):
        if not self.bset or not self.bset.modified:
            return
        src = self.loaded
        path, _ = QFileDialog.getSaveFileName(self, 'Save PCK as', os.path.splitext(src)[0] + '_edited.pck', 'PCK files (*.pck)')
        if not path:
            return
        if not path.lower().endswith('.pck'):
            path += '.pck'
        try:
            with rpck.replacing(src, path) as (_, bak):       # same file as the loaded pck: the original is kept as <name>.old
                self.bset.save(path)
        except Exception as e:
            traceback.print_exc()
            return self.win.error('Could not write the pck:\n%s' % e)
        if bak:
            self.win.log('gui', 'Original kept as %s' % bak)
        self.saved_rev = self.rev
        for i in self.bset.modified:
            self._label(i)
        self._refresh()
        self.win.log('ok', 'Saved %s with %d replaced bitmap(s)' % (path, len(self.bset.modified)))


def describe_tip(d):
    """Tooltip text of a grid item."""
    if not d['ok']:
        return 'Bitmap #%d cannot be shown: %s' % (d['index'], d['error'])
    s = 'Bitmap #%d: %d x %d px, %d-bit palette (%d colours)' % (d['index'], d['auto_size'][0], d['auto_size'][1], d['bpp'], d['ncol'])
    if d['auto_size'] != (d['tex_w'], d['tex_h']):
        s += '\nStored as a %d x %d texture' % (d['tex_w'], d['tex_h'])
    if d['screen_aspect'] and abs(d['screen_aspect'] / d['aspect'] - 1) >= 0.01:
        s += '\nDrawn at %.3f : 1 (its stored source size is %d x %d)' % (d['screen_aspect'], d['src_w'], d['src_h'])
    return s


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
        a_bmp = m.addAction('Open in %s tab' % BITMAP_TAB_NAME)
        act = m.exec(self.list.viewport().mapToGlobal(pos))
        if act is a_copy:
            QGuiApplication.clipboard().setText(path)
            self.win.log('gui', 'Copied to clipboard: %s' % path)
        elif act is a_prev:
            self.win.preview_pck(path)
        elif act is a_bmp:
            self.win.tab_bitmaps.pck.setText(path)
            if self.win.tab_bitmaps.loaded == path:                      # not refused by the unsaved-changes prompt
                self.win.tabs.setCurrentWidget(self.win.tab_bitmaps)


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
            '<li><b>%s</b>: browse every bitmap of a pck as a grid, export one as PNG or replace it with a new image.</li>'
            '<li><b>PCK Browser</b>: right-click a file to copy its path, preview it or open it in the bitmap viewer.</li>'
            '</ol>'
            '<p>Everything the library does is logged in the console at the bottom of the window.</p>'
            % (html.escape(APP_NAME), html.escape(PORT_TAB_NAME), html.escape(ANIM_TAB_NAME), html.escape(LOAD_TAB_NAME),
               html.escape(BITMAP_TAB_NAME)))
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
        self.tab_bitmaps = BitmapsTab(self)
        self.tab_browser = BrowserTab(self)
        self.tabs.addTab(self.tab_p2s.scrolled(), 'PCK \u2192 SWF')
        self.tabs.addTab(self.tab_s2p.scrolled(), 'SWF \u2192 PCK')
        self.tabs.addTab(self.tab_port.scrolled(), PORT_TAB_NAME)
        self.tabs.addTab(self.tab_anim.scrolled(), ANIM_TAB_NAME)
        self.tabs.addTab(self.tab_load.scrolled(), LOAD_TAB_NAME)
        self.tabs.addTab(self.tab_bitmaps, BITMAP_TAB_NAME)             # not wrapped in a scroll area: the grid scrolls itself
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
        if not self.tab_bitmaps.confirm_discard():
            e.ignore()
            return
        self.tab_bitmaps._stop_loader()
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
