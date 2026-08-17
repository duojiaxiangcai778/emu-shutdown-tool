# -*- mode: python ; coding: utf-8 -*-

import os

_a = Analysis(
    ['模拟器管理工具.pyw'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['collections.abc'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter.test', 'unittest', 'test', 'doctest',
        'pydoc', 'pdb', 'profile', 'cProfile', 'trace',
        'lib2to3', 'ensurepip', 'idlelib', 'turtledemo',
        'numpy', 'pandas', 'scipy', 'matplotlib',
        'multiprocessing', 'distutils', 'setuptools',
    ],
    noarchive=False,
    optimize=2,
)
pyz = PYZ(_a.pure)

exe = EXE(
    pyz,
    _a.scripts,
    _a.binaries,
    _a.datas,
    [],
    name='模拟器管理工具',
    debug=False,
    bootloader_ignore_signals=False,
    # Windows 构建不使用 ELF strip，避免在无 strip 工具的环境中产生无意义警告。
    strip=False,
    upx=False,
    runtime_tmpdir=os.path.join(os.environ.get('LOCALAPPDATA', ''), 'EmuToolCache'),
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['app_icon.ico'],
)
