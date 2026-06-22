# -*- mode: python ; coding: utf-8 -*-

import sys
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

APP_VERSION = '0.0.7'

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
# certifi: aiohttp uses ssl.create_default_context on Windows. Some PyInstaller
# bundles end up without a usable CA store, which manifests as silent SSL
# handshake timeouts (the 30s-and-empty-error pattern in v0.0.1 reports).
hiddenimports += ['certifi']
datas += collect_data_files('certifi')

# Binary files (GDAL libraries)
binaries = []


def _first_existing_dir(candidates, required_file=None):
    for cand in candidates:
        cand = (cand or '').strip()
        if not cand:
            continue
        if os.path.isdir(cand) and (required_file is None or os.path.exists(os.path.join(cand, required_file))):
            return cand
    return ''


def _proj_data_candidates():
    candidates = [
        os.environ.get('PROJ_DATA', ''),
        os.environ.get('PROJ_LIB', ''),
    ]
    conda_prefix = os.environ.get('CONDA_PREFIX', '').strip()
    if conda_prefix:
        candidates.append(os.path.join(conda_prefix, 'share', 'proj'))
        candidates.append(os.path.join(conda_prefix, 'Library', 'share', 'proj'))

    candidates.append(os.path.join(os.path.dirname(sys.executable), 'Library', 'share', 'proj'))

    if sys.platform == 'darwin':
        candidates.extend([
            '/opt/homebrew/share/proj',
            '/usr/local/share/proj',
            '/usr/share/proj',
        ])
    else:
        candidates.extend([
            '/usr/share/proj',
            '/usr/local/share/proj',
        ])

    pkg_config_datadir = os.popen('pkg-config --variable=datadir proj 2>/dev/null').read().strip()
    if pkg_config_datadir:
        candidates.append(os.path.join(pkg_config_datadir, 'proj'))

    return candidates


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
    # macOS GDAL binaries — CI installs gdal via conda-forge (matching Windows)
    # and exports GDAL_DATA. Fall back to gdal-config for local dev builds
    # using brew/system gdal.
    gdal_data = os.environ.get('GDAL_DATA', '').strip()
    if not (gdal_data and os.path.isdir(gdal_data)):
        gdal_data = os.popen('gdal-config --datadir').read().strip()
    if gdal_data and os.path.isdir(gdal_data):
        datas += [(gdal_data, 'gdal-data')]
else:
    # Linux GDAL binaries
    gdal_data = os.popen('gdal-config --datadir').read().strip()
    if gdal_data:
        datas += [(gdal_data, 'gdal-data')]

proj_data = _first_existing_dir(_proj_data_candidates(), required_file='proj.db')
if proj_data:
    datas += [(proj_data, 'proj-data')]

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
