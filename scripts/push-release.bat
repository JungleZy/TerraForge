@echo off
REM 推送代码并创建版本标签 (Windows)
REM 版本来源（单一事实源）：core/config.py 的 Config.APP_VERSION；也可用第一个参数覆盖：
REM   push-release.bat            用 core/config.py 里的版本
REM   push-release.bat 0.2.0      显式指定

setlocal

set VERSION=%~1
if "%VERSION%"=="" (
    REM core/config.py 中该行为类属性（带缩进）：APP_VERSION = 'x.y.z'
    for /f "tokens=2 delims='" %%v in ('findstr /r /c:"^ *APP_VERSION *= *'" core/config.py') do set VERSION=%%v
)
if "%VERSION%"=="" (
    echo ❌ 无法确定版本号：请传入参数，或检查 core/config.py 的 Config.APP_VERSION
    exit /b 1
)
set TAG=v%VERSION%

git rev-parse %TAG% >nul 2>&1
if not errorlevel 1 (
    echo ❌ 标签 %TAG% 已存在，请先 bump core/config.py 的 APP_VERSION 或删除旧标签
    exit /b 1
)

echo ================================
echo 推送代码并创建 %TAG% 版本
echo ================================
echo.

REM 先在本地建标签再推送：标签创建失败时尚未产生任何远端改动，
REM 避免旧脚本「代码已推、标签失败」的半完成态。
echo 步骤 1: 创建本地标签 %TAG%...
git tag -a %TAG% -m "版本 %TAG%"
if errorlevel 1 (
    echo ❌ 标签创建失败
    exit /b 1
)
echo ✅ 标签创建成功
echo.

echo 步骤 2: 推送代码到 GitHub...
git push origin master
if errorlevel 1 (
    echo ❌ 代码推送失败，请检查 GitHub 认证
    exit /b 1
)
echo ✅ 代码推送成功
echo.

echo 步骤 3: 推送标签到 GitHub...
git push origin %TAG%
if errorlevel 1 (
    echo ❌ 标签推送失败
    exit /b 1
)
echo ✅ 标签推送成功
echo.

echo ================================
echo 🎉 完成！
echo ================================
echo.
echo 下一步：
echo 1. 访问 https://github.com/JungleZy/map-download/actions
echo 2. 查看 'Build Executables' 工作流
echo 3. 等待构建完成（约 15-30 分钟）
echo 4. 从 Releases 页面下载可执行文件
echo.
echo GitHub 仓库: https://github.com/JungleZy/map-download
echo ================================
pause
