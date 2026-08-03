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
    if errorlevel 1 (
        echo ERROR: Failed to install Nuitka
        exit /b 1
    )
)

REM Clean previous builds
echo Cleaning previous builds...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

REM Build the executable
echo Building executable...
uv run python nuitka_build.py

REM M20: 必须查退出码。nuitka_build.py 很早就把 dist\app.dist 重命名成
REM dist\terraforge，之后才依次跑产物自检 —— 所以那些自检一旦触发，目标目录
REM 已经存在了，光靠下面的 `if exist` 判定会把失败的构建报成 "Build successful!"。
REM 被吞掉的包括：exe 未生成、OSGeo4W 等非 conda 布局下 GDAL DLL 闭包补拷失败
REM （nuitka_build.py 主动 raise，错误文案明说是为了防止交付「能构建能启动但每次
REM GDAL 调用都失败」的包）、以及未 pin 版本的 Nuitka 升级后私有 API 签名变化。
REM build.sh 靠 `set -euo pipefail` 天然有这个保护，两个脚本此前不对称。
if errorlevel 1 (
    echo Build failed! ^(nuitka_build.py exited with an error^)
    exit /b 1
)

REM Check if build was successful ^(附加断言;退出码才是主判据^)
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
