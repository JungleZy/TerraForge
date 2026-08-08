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

# GDAL 一致性检查(I20d)。判据与解析都在 scripts/check_gdal.py —— build.bat 调的是
# 同一个文件。这里【不能】查 `GDAL==` 精确钉:requirements.txt 顶部写明了绑定版本
# 跟随机器、必须给范围,而 2026-08-08 前这里查的正是 `^GDAL==`,配上 `set -euo
# pipefail` 让脚本在赋值那一行就静默 exit 1(见 check_gdal.py 的模块注释)。
uv run python scripts/check_gdal.py

# Nuitka 从 requirements.txt 装,不能裸 `uv pip install nuitka`:nuitka_build.py 调
# 的是 Nuitka 的**私有** API(DllDependenciesWin32.detectBinaryPathDLLsWin32,
# 八个关键字参数),上游改签名就会在 tag 已经推出去之后打断 Windows 构建。
# requirements.txt 里已经钉了版本,这里只在缺失时按那份清单补装。
if ! uv run python -c "import nuitka" &> /dev/null; then
    echo "Installing Nuitka (pinned in requirements.txt)..."
    uv pip install -r requirements.txt
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
