"""
Runtime hook for PyInstaller to set up GDAL environment
"""
import os
import sys

# Set GDAL_DATA environment variable
if hasattr(sys, '_MEIPASS'):
    # Running in PyInstaller bundle
    gdal_data_path = os.path.join(sys._MEIPASS, 'gdal-data')
    if os.path.exists(gdal_data_path):
        os.environ['GDAL_DATA'] = gdal_data_path
        os.environ['PROJ_LIB'] = gdal_data_path
