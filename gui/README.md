# PCK <-> SWF Tools GUI

    pip install -r gui/requirements.txt
    python3 gui/pckswf_gui.py

Keep the `gui/` folder inside the pck-swf-tools-v3 folder (it imports pck2swf / swf2pck / swfimport from the parent folder).

* Settings (menu bar): Flash Player path, JPEXS path (exe, .sh or .jar), PCK folder. Stored with QSettings.
* Help -> Welcome window / About.
* Tabs: PCK -> SWF (always preview mode), SWF -> PCK, "Port SWF -> New PCK" (swfimport; rename via PORT_TAB_NAME), PCK Browser.
* Library work runs in QThreads; library stdout/stderr and tracebacks go to the console, GUI problems are pop-ups.
* Previews from the PCK Browser are written to a temp folder (removed when the app closes).
* `gui/ops.py` has no Qt dependency, so the same operations can be scripted.

## Executables (GitHub Actions)

`.github/workflows/build.yml` builds Linux and Windows bundles with PyInstaller (one-folder builds) on every push / PR
and uploads them as workflow artifacts. Pushing a tag such as `v1.0.0` also publishes them as a GitHub Release:

    git tag v1.0.0 && git push origin v1.0.0

The repository root must be the `pck-swf-tools-v3` folder (library `.py` files at the root, `gui/` and `.github/` beside them).
Linux users may need `libxcb-cursor0` installed for Qt's xcb platform plugin.
