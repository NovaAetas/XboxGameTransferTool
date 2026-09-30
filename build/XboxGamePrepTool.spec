# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['../xbox_game_prep_gui.py'],
    pathex=['..'],
    binaries=[],
    datas=[
        ('../tools', 'tools'),
        ('../README.md', '.'),
        ('../LICENSE', '.'),
        ('../Start Xbox Game Prep Tool.cmd', '.'),
    ],
    hiddenimports=['tkinter', 'tkinter.ttk', 'xbox_hdd_prep'],
    hookspath=['gui-hooks'],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='XboxGamePrepTool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='XboxGamePrepTool',
)
