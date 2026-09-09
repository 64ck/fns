# -*- mode: python ; coding: utf-8 -*-
"""Сборка одного исполняемого файла: pyinstaller fnsportal.spec

Внутрь кладутся интерфейс портала и эталонные справочники; при первом запуске
справочники распаковываются рядом с программой, чтобы их можно было править.
"""

datas = [
    ("portal/static", "portal/static"),
    ("reference", "reference"),
]

hiddenimports = [
    "openpyxl", "xlrd",
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    "anyio._backends._asyncio",
]

a = Analysis(
    ["desktop.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "PIL", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="fns-portal",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
