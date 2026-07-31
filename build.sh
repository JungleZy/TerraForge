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
# match the GDAL the bindings were built against (major.minor), otherwise the
# bindings fail to compile or silently misbehave at runtime. Prefer the osgeo
# binding's own version — that is what actually gets bundled; gdal-config may
# belong to a different GDAL installation on the same machine.
REQUIRED_GDAL=$(grep -oE '^GDAL==[0-9.]+' requirements.txt | head -1 | cut -d= -f3)
if [ -z "$REQUIRED_GDAL" ]; then
    echo "Error: requirements.txt 缺少 GDAL== pin"
    exit 1
fi
SYSTEM_GDAL=$(uv run python -c "from osgeo import gdal; print(gdal.__version__)" 2>/dev/null || gdal-config --version 2>/dev/null || true)
if [ -z "$SYSTEM_GDAL" ]; then
    echo "Error: no GDAL found (osgeo not importable and gdal-config missing)."
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

# Check if Nuitka is installed in the uv environment
if ! uv run python -c "import nuitka" &> /dev/null; then
    echo "Installing Nuitka..."
    uv pip install nuitka
fi

# Clean previous builds
echo "Cleaning previous builds..."
rm -rf build dist

# Build the executable
echo "Building executable..."
uv run python nuitka_build.py

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
