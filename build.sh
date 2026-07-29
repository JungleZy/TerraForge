#!/bin/bash

# Build script for local testing
# This script builds the executable for the current platform using the project uv environment.

set -euo pipefail

echo "Building TerraForge executable..."

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    echo "Error: uv is not installed"
    exit 1
fi

# Install project dependencies — without this a clean environment fails at
# import time mid-build (I20a).
echo "Installing dependencies..."
uv pip install -r requirements.txt

# GDAL version consistency check (I20d): the pip pin in requirements.txt must
# match the system GDAL (major.minor), otherwise the bindings fail to compile
# or silently misbehave at runtime.
REQUIRED_GDAL=$(grep -oE '^GDAL==[0-9.]+' requirements.txt | head -1 | cut -d= -f3)
SYSTEM_GDAL=$(gdal-config --version 2>/dev/null || uv run python -c "from osgeo import gdal; print(gdal.__version__)" 2>/dev/null || true)
if [ -z "$SYSTEM_GDAL" ]; then
    echo "Error: no system GDAL found (gdal-config missing and osgeo not importable)."
    echo "Install system GDAL first (e.g. apt-get install gdal-bin libgdal-dev / conda install gdal)."
    exit 1
fi
REQ_MM=$(echo "$REQUIRED_GDAL" | cut -d. -f1,2)
SYS_MM=$(echo "$SYSTEM_GDAL" | cut -d. -f1,2)
if [ "$REQ_MM" != "$SYS_MM" ]; then
    echo "Error: requirements.txt pins GDAL==$REQUIRED_GDAL but system GDAL is $SYSTEM_GDAL."
    echo "Fix: update the pin in requirements.txt to match the system GDAL (major.minor must agree),"
    echo "     or install system GDAL $REQUIRED_GDAL."
    exit 1
fi
echo "GDAL version check OK (pin $REQUIRED_GDAL, system $SYSTEM_GDAL)"

# Check if PyInstaller is installed in the uv environment
if ! uv run python -c "import PyInstaller" &> /dev/null; then
    echo "Installing PyInstaller..."
    uv pip install pyinstaller
fi

# Clean previous builds
echo "Cleaning previous builds..."
rm -rf build dist

# Build the executable
echo "Building executable..."
uv run python -m PyInstaller build.spec

# Check if build was successful
if [ -d "dist/terraforge" ]; then
    echo "Build successful!"
    echo "Executable location: dist/terraforge/"
    echo ""
    echo "To run the application:"
    echo "  cd dist/terraforge"
    echo "  ./terraforge"
else
    echo "Build failed!"
    exit 1
fi
