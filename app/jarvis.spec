# -*- mode: python ; coding: utf-8 -*-
# JarvisPC を1つの exe にする PyInstaller 設定。
# console=False が大前提（黒いコンソール窓を絶対に出さない）。
from PyInstaller.utils.hooks import collect_all

datas = [
    ('llama', 'llama'),
    ('vosk-model-small-ja-0.22', 'vosk-model-small-ja-0.22'),
]
binaries = []
hiddenimports = []
for _pkg in ('vosk', 'sounddevice', 'imageio_ffmpeg'):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

a = Analysis(
    ['jarvis.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
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
    name='JarvisPC',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
