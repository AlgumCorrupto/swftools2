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
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QPalette               # noqa: E402
from PySide6.QtWidgets import (                                                         # noqa: E402
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget)

APP_NAME = 'PCK <-> SWF Tools'
APP_VERSION = '1.0.0'
PORT_TAB_NAME = 'Port SWF \u2192 New PCK'        # the "swf import" tab; rename here if you prefer another name

CREDITS_HTML = """
<h3>%(name)s <small>v%(ver)s</small></h3>
<p>A Qt front-end for the MC3 (PS2) <b>.pck &harr; .swf</b> conversion tools (pck-swf-tools v3):
pck2swf, swf2pck and swfimport.</p>
<h4>Credits</h4>
<ul>
<li><b>pck-swf-tools v3</b> &ndash; the conversion library this GUI drives (tessellator, preview baking, bitmap/font handling).</li>
<li><b>RAGE-SWF-research</b> (AlgumCorrupto) &ndash; <a href="https://github.com/AlgumCorrupto/RAGE-SWF-research">github.com/AlgumCorrupto/RAGE-SWF-research</a></li>
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
            '<li><b>PCK Browser</b>: right-click a file to copy its path or preview it.</li>'
            '</ol>'
            '<p>Everything the library does is logged in the console at the bottom of the window.</p>'
            % (html.escape(APP_NAME), html.escape(PORT_TAB_NAME)))
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
        self.tab_browser = BrowserTab(self)
        self.tabs.addTab(self.tab_p2s.scrolled(), 'PCK \u2192 SWF')
        self.tabs.addTab(self.tab_s2p.scrolled(), 'SWF \u2192 PCK')
        self.tabs.addTab(self.tab_port.scrolled(), PORT_TAB_NAME)
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
