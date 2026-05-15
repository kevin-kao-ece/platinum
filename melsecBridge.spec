# -*- mode: python ; coding: utf-8 -*-
# PyInstaller onefile spec for NeoEdge Melsec Bridge (Windows Service + console debug).
# Build: build_exe.bat  ->  dist\melsecBridge.exe

block_cipher = None

a = Analysis(
    ["win_service.py"],
    pathex=[],
    binaries=[],
    datas=[("static", "static")],
    hiddenimports=[
        "main",
        "web",
        "melsec",
        "itService",
        "logHelper",
        "licenseHelp",
        # pywin32 (Windows Service)
        "pywintypes",
        "win32api",
        "win32event",
        "win32service",
        "win32serviceutil",
        "win32timezone",
        "servicemanager",
        # pymcprotocol / PLC
        "pymcprotocol",
        "pymcprotocol.type3e",
        "pymcprotocol.type4e",
        "pymcprotocol.mcprotocolconst",
        # FastAPI / Uvicorn
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "httptools",
        "websockets",
        "websockets.legacy",
        "websockets.legacy.server",
        "multipart",
        "starlette.routing",
        "pydantic",
        # Data backends
        "influxdb_client",
        "pymongo",
        "bson",
        # License / crypto
        "cryptography",
        "cryptography.hazmat.primitives.ciphers.algorithms",
        "cryptography.hazmat.backends.openssl",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="melsecBridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
