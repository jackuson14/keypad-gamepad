# PyInstaller spec for the analog gamepad GUI -> single windowed .exe.
# Build with:  py -m PyInstaller analog_gui.spec   (or tools\build_exe.ps1)
#
# Bundles:
#   - hidapi.dll           (native backend for the `hid` package)
#   - vgamepad data/DLLs   (ViGEmClient.dll etc., via collect_all)
#   - pystray backends     (tray icon)
#   - sv_ttk theme files   (.tcl Sun Valley dark/light theme assets)
#   - certifi cacert.pem   (CA bundle so the in-app update check can verify HTTPS)
#   - discovered_keymap.json as a seed default (copied to the user dir on first run)

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], ["hid"]
for pkg in ("vgamepad", "pystray", "PIL", "sv_ttk", "certifi"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

binaries += [("hidapi.dll", ".")]
datas += [("discovered_keymap.json", ".")]

a = Analysis(
    ["analog_gui.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="keypad-gamepad-analog",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    runtime_tmpdir=None,
    console=False,           # windowed app (no console)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
)
