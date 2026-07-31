@echo off
REM Build script for Windows
REM This script builds the executable for Windows using the project uv environment.

echo Building TerraForge executable...

REM Check if uv is installed
uv --version >nul 2>&1
if errorlevel 1 (
    echo Error: uv is not installed
    exit /b 1
)

REM Install project dependencies — without this a clean environment fails at
REM import time mid-build (I20a).
echo Installing dependencies...
uv pip install -r requirements.txt
if errorlevel 1 (
    echo Error: failed to install dependencies
    exit /b 1
)

REM GDAL version consistency check (I20d): the pip pin in requirements.txt must
REM match the installed GDAL (major.minor). On Windows GDAL usually comes from
REM conda-forge, so read the version from the osgeo binding.
set REQ_GDAL=
for /f "tokens=3 delims==" %%v in ('findstr /b /c:"GDAL==" requirements.txt') do set REQ_GDAL=%%v
if "%REQ_GDAL%"=="" (
    echo Error: requirements.txt 缺少 GDAL== pin
    exit /b 1
)
set SYS_GDAL=
for /f "delims=" %%v in ('uv run python -c "from osgeo import gdal; print(gdal.__version__)" 2^>nul') do set SYS_GDAL=%%v
if "%SYS_GDAL%"=="" (
    echo Error: osgeo not importable — install GDAL first ^(e.g. conda install -c conda-forge gdal^).
    exit /b 1
)
for /f "tokens=1,2 delims=." %%a in ("%REQ_GDAL%") do set REQ_MM=%%a.%%b
for /f "tokens=1,2 delims=." %%a in ("%SYS_GDAL%") do set SYS_MM=%%a.%%b
if not "%REQ_MM%"=="%SYS_MM%" (
    echo Error: requirements.txt pins GDAL==%REQ_GDAL% but installed GDAL is %SYS_GDAL%.
    echo Fix: update the pin in requirements.txt to match the installed GDAL ^(major.minor must agree^),
    echo      or install GDAL %REQ_GDAL%.
    exit /b 1
)
echo GDAL version check OK ^(pin %REQ_GDAL%, system %SYS_GDAL%^)

REM Check if Nuitka is installed in the uv environment
uv run python -c "import nuitka" >nul 2>&1
if errorlevel 1 (
    echo Installing Nuitka...
    uv pip install nuitka
)

REM Clean previous builds
echo Cleaning previous builds...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

REM Build the executable
echo Building executable...
uv run python nuitka_build.py

REM Check if build was successful
if exist "dist\terraforge" (
    echo Build successful!
    echo Executable location: dist\terraforge\
    echo.
    echo To run the application:
    echo   cd dist\terraforge
    echo   terraforge.exe
) else (
    echo Build failed!
    exit /b 1
)
