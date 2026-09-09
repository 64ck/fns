# -*- mode: python ; coding: utf-8 -*-
"""Сборка одного исполняемого файла: pyinstaller fnsportal.spec

Внутрь кладутся интерфейс портала и эталонные справочники; при первом запуске
справочники распаковываются рядом с программой, чтобы их можно было править.
"""

import datetime
import json
import os
import pathlib
import subprocess


def _stamp_build() -> None:
    """Кладёт в сборку отметку версии — по ней программа понимает,
    вышло ли обновление."""
    commit = ""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=20,
        ).stdout.strip()
    except Exception:
        pass
    now = datetime.datetime.now(datetime.timezone.utc)
    info = {
        "commit": commit,
        "built_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date": now.strftime("%Y-%m-%d"),
        "repo": os.environ.get("GITHUB_REPOSITORY", "64ck/fns"),
    }
    path = pathlib.Path("reference/build_info.json")
    path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")


_stamp_build()

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
