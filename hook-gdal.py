"""
Runtime hook for PyInstaller to set up GDAL environment
"""
import os
import sys

# Set GDAL/PROJ data paths for PyInstaller bundles.
if hasattr(sys, '_MEIPASS'):
    gdal_data_path = os.path.join(sys._MEIPASS, 'gdal-data')
    if os.path.exists(gdal_data_path):
        os.environ['GDAL_DATA'] = gdal_data_path

    proj_data_path = os.path.join(sys._MEIPASS, 'proj-data')
    if os.path.exists(proj_data_path):
        os.environ['PROJ_LIB'] = proj_data_path
        os.environ['PROJ_DATA'] = proj_data_path
