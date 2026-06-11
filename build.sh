#!/bin/bash

# Build script for local testing
# This script builds the executable for the current platform using the project uv environment.

set -e

echo "Building Google Maps Downloader executable..."

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    echo "Error: uv is not installed"
    exit 1
fi

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
if [ -d "dist/map-downloader" ]; then
    echo "Build successful!"
    echo "Executable location: dist/map-downloader/"
    echo ""
    echo "To run the application:"
    echo "  cd dist/map-downloader"
    echo "  ./map-downloader"
else
    echo "Build failed!"
    exit 1
fi
