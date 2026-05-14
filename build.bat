@echo off
REM Build script for Windows
REM This script builds the executable for Windows

echo Building Google Maps Downloader executable...

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo Error: Python is not installed
    exit /b 1
)

REM Check if PyInstaller is installed
python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    pip install pyinstaller
)

REM Clean previous builds
echo Cleaning previous builds...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

REM Build the executable
echo Building executable...
pyinstaller build.spec

REM Check if build was successful
if exist "dist\map-downloader" (
    echo Build successful!
    echo Executable location: dist\map-downloader\
    echo.
    echo To run the application:
    echo   cd dist\map-downloader
    echo   map-downloader.exe
) else (
    echo Build failed!
    exit /b 1
)
