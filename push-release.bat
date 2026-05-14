@echo off
REM 推送 0.0.1 版本到 GitHub 的脚本 (Windows)

echo ================================
echo 推送代码并创建 v0.0.1 版本
echo ================================
echo.

REM 1. 推送代码到 GitHub
echo 步骤 1: 推送代码到 GitHub...
git push origin master

if %errorlevel% equ 0 (
    echo ✅ 代码推送成功
) else (
    echo ❌ 代码推送失败，请检查 GitHub 认证
    exit /b 1
)

echo.

REM 2. 创建版本标签
echo 步骤 2: 创建版本标签 v0.0.1...
git tag -a v0.0.1 -m "测试版本 v0.0.1 - 跨平台打包配置"

if %errorlevel% equ 0 (
    echo ✅ 标签创建成功
) else (
    echo ❌ 标签创建失败
    exit /b 1
)

echo.

REM 3. 推送标签到 GitHub
echo 步骤 3: 推送标签到 GitHub...
git push origin v0.0.1

if %errorlevel% equ 0 (
    echo ✅ 标签推送成功
) else (
    echo ❌ 标签推送失败
    exit /b 1
)

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
