#!/bin/bash
# 推送代码并创建版本标签。
# 版本来源（单一事实源）：src/core/config.py 的 Config.APP_VERSION；也可用第一个参数覆盖：
#   ./push-release.sh            # 用 src/core/config.py 里的版本
#   ./push-release.sh 0.2.0      # 显式指定

set -euo pipefail

VERSION="${1:-}"
if [ -z "$VERSION" ]; then
    # src/core/config.py 中该行为类属性（带缩进）：APP_VERSION = 'x.y.z'
    VERSION=$(grep -oE "^[[:space:]]*APP_VERSION[[:space:]]*=[[:space:]]*'[0-9.]+'" src/core/config.py 2>/dev/null | head -1 | cut -d"'" -f2 || true)
fi
if [ -z "$VERSION" ]; then
    echo "❌ 无法确定版本号：请传入参数，或检查 src/core/config.py 的 Config.APP_VERSION"
    exit 1
fi
TAG="v${VERSION}"

if git rev-parse "$TAG" >/dev/null 2>&1; then
    echo "❌ 标签 $TAG 已存在，请先 bump src/core/config.py 的 APP_VERSION 或删除旧标签"
    exit 1
fi

echo "================================"
echo "推送代码并创建 $TAG 版本"
echo "================================"
echo ""

# 先在本地建标签再推送：标签创建失败时尚未产生任何远端改动，
# 避免旧脚本「代码已推、标签失败」的半完成态。
echo "步骤 1: 创建本地标签 $TAG..."
git tag -a "$TAG" -m "版本 $TAG"
echo "✅ 标签创建成功"
echo ""

echo "步骤 2: 推送代码到 GitHub..."
git push origin master
echo "✅ 代码推送成功"
echo ""

echo "步骤 3: 推送标签到 GitHub..."
git push origin "$TAG"
echo "✅ 标签推送成功"
echo ""

echo "================================"
echo "🎉 完成！"
echo "================================"
echo ""
echo "下一步："
echo "1. 访问 https://github.com/JungleZy/map-download/actions"
echo "2. 查看 'Build Executables' 工作流"
echo "3. 等待构建完成（约 15-30 分钟）"
echo "4. 从 Releases 页面下载可执行文件"
echo ""
echo "GitHub 仓库: https://github.com/JungleZy/map-download"
echo "================================"
