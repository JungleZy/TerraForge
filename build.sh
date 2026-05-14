#!/bin/bash

# Build script for local testing
# This script builds the executable for the current platform

echo "Building Google Maps Downloader executable..."

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 is not installed"
    exit 1
fi

# Check if PyInstaller is installed
if ! python3 -c "import PyInstaller" &> /dev/null; then
    echo "Installing PyInstaller..."
    pip install pyinstaller
fi

# Clean previous builds
echo "Cleaning previous builds..."
rm -rf build dist

# Build the executable
echo "Building executable..."
pyinstaller build.spec

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
