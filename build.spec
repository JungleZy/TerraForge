# -*- mode: python ; coding: utf-8 -*-

import sys
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

APP_VERSION = '0.0.1'

block_cipher = None

# Collect all data files
datas = []
datas += collect_data_files('flask_socketio')
datas += collect_data_files('socketio')
datas += collect_data_files('engineio')

# Add templates and static files
datas += [('templates', 'templates')]
datas += [('static', 'static')]

# Create empty directories for runtime data (will be created by app if not exist)
# Don't include actual data/cache/downloads to keep package small

# Collect hidden imports
hiddenimports = []
hiddenimports += collect_submodules('flask_socketio')
hiddenimports += collect_submodules('socketio')
hiddenimports += collect_submodules('engineio')
hiddenimports += collect_submodules('aiohttp')
hiddenimports += collect_submodules('aiofiles')
hiddenimports += ['osgeo', 'osgeo.gdal', 'osgeo.ogr', 'osgeo.osr', 'osgeo.gdal_array', 'osgeo.gdalconst']
hiddenimports += ['PIL', 'PIL._imaging']
hiddenimports += ['dns', 'dns.resolver']
hiddenimports += ['werkzeug', 'werkzeug.security']
hiddenimports += ['jinja2', 'jinja2.ext']
hiddenimports += ['click']
hiddenimports += ['sqlite3']

# Binary files (GDAL libraries)
binaries = []

# Platform-specific GDAL handling
if sys.platform == 'win32':
    # Windows GDAL binaries — prefer GDAL_DATA from env (CI sets it after
    # installing gdal via conda-forge). Fall back to discovering the data dir
    # from the osgeo package layout, so local builds without the env var work.
    gdal_data = os.environ.get('GDAL_DATA', '').strip()
    if not (gdal_data and os.path.isdir(gdal_data)):
        try:
            import osgeo
            osgeo_root = os.path.dirname(osgeo.__file__)
            for cand in (
                os.path.join(osgeo_root, 'data', 'gdal'),
                os.path.join(os.path.dirname(sys.executable), 'Library', 'share', 'gdal'),
            ):
                if os.path.isdir(cand):
                    gdal_data = cand
                    break
        except ImportError:
            pass
    if gdal_data and os.path.isdir(gdal_data):
        datas += [(gdal_data, 'gdal-data')]
elif sys.platform == 'darwin':
    # macOS GDAL binaries
    gdal_data = os.popen('gdal-config --datadir').read().strip()
    if gdal_data:
        datas += [(gdal_data, 'gdal-data')]
else:
    # Linux GDAL binaries
    gdal_data = os.popen('gdal-config --datadir').read().strip()
    if gdal_data:
        datas += [(gdal_data, 'gdal-data')]

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['hook-gdal.py'],
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
    [],
    exclude_binaries=True,
    name='map-downloader',
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
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='map-downloader',
)
