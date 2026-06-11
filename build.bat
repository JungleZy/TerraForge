@echo off
REM Build script for Windows
REM This script builds the executable for Windows using the project uv environment.

echo Building Google Maps Downloader executable...

REM Check if uv is installed
uv --version >nul 2>&1
if errorlevel 1 (
    echo Error: uv is not installed
    exit /b 1
)

REM Check if PyInstaller is installed in the uv environment
uv run python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    uv pip install pyinstaller
)

REM Clean previous builds
echo Cleaning previous builds...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

REM Build the executable
echo Building executable...
uv run python -m PyInstaller build.spec

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
