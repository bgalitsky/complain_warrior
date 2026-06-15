# fb_poster_sidecar.spec
# Run with:  pyinstaller fb_poster_sidecar.spec

block_cipher = None

a = Analysis(
    ['sidecar/fb_poster_sidecar.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['selenium', 'sqlite3', 'json', 're'],
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
    name='fb_poster_sidecar',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,   # must be True — Tauri reads stdout
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
