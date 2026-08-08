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

REM GDAL consistency check (I20d). 判据与解析都在 scripts\check_gdal.py —— build.sh
REM 调的是同一个文件。这里【不能】查 `GDAL==` 精确钉:requirements.txt 顶部写明了
REM 绑定版本跟随机器、必须给范围,而 2026-08-08 前这里查的正是 findstr "GDAL==",
REM 于是每次构建都命中「缺少 GDAL== pin」直接拒绝(见 check_gdal.py 的模块注释)。
uv run python scripts\check_gdal.py
if errorlevel 1 exit /b 1

REM Nuitka 从 requirements.txt 装,不能裸 `uv pip install nuitka`:nuitka_build.py
REM 调的是 Nuitka 的【私有】API(八个关键字参数),上游改签名就会在 tag 已经推出去
REM 之后打断 Windows 构建。requirements.txt 里已经钉了版本,这里只按那份清单补装。
uv run python -c "import nuitka" >nul 2>&1
if errorlevel 1 (
    echo Installing Nuitka ^(pinned in requirements.txt^)...
    uv pip install -r requirements.txt
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
