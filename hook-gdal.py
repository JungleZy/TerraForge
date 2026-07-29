"""
Runtime hook for PyInstaller to set up GDAL environment
"""
import os
import sys

# Set GDAL/PROJ data paths for PyInstaller bundles.
if hasattr(sys, '_MEIPASS'):
    gdal_data_path = os.path.join(sys._MEIPASS, 'gdal-data')
    proj_data_path = os.path.join(sys._MEIPASS, 'proj-data')

    # Fail loudly at startup when the bundle is missing its data dirs (I20b).
    # Silently skipping used to produce an exe that starts fine but breaks on
    # the first GDAL call. build.spec already refuses to build such a bundle,
    # so reaching this branch means the package is genuinely corrupt.
    missing = [p for p in (gdal_data_path, proj_data_path) if not os.path.isdir(p)]
    if missing:
        raise RuntimeError(
            'Corrupt bundle: missing GDAL/PROJ data directories: '
            + ', '.join(missing)
            + '. Rebuild the executable (build.spec refuses to bundle without them).'
        )

    os.environ['GDAL_DATA'] = gdal_data_path
    os.environ['PROJ_LIB'] = proj_data_path
    os.environ['PROJ_DATA'] = proj_data_path
