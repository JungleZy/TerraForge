# TerraForge - 打包配置总结

## 已创建的文件

### 1. GitHub Actions 工作流
- `.github/workflows/build.yml` - 主构建工作流，支持 Windows/macOS/Linux
- `.github/workflows/test-build.yml` - 测试构建工作流

### 2. PyInstaller 配置
- `build.spec` - PyInstaller 构建配置
- `hook-gdal.py` - GDAL 运行时钩子

### 3. 构建脚本
- `build.sh` - Linux/macOS 本地构建脚本
- `build.bat` - Windows 本地构建脚本

### 4. 文档
- `PACKAGING.md` - 打包使用说明（中文）
- `DISTRIBUTION.md` - 分发说明（英文）
- `docs/BUILD.md` - 详细构建文档（英文）

## 使用方法

### 自动构建（推荐）

1. 推送代码到 GitHub
2. 创建版本标签：
   ```bash
   git tag -a v1.0.0 -m "Release version 1.0.0"
   git push origin v1.0.0
   ```
3. GitHub Actions 自动构建所有平台
4. 从 Releases 页面下载可执行文件

### 本地构建

```bash
# Linux/macOS
./build.sh

# Windows
build.bat
```

## 下一步

1. 将代码推送到 GitHub
2. 在 `README.md` 和 `DISTRIBUTION.md` 中替换 `YOUR_USERNAME` 为你的 GitHub 用户名
3. 创建第一个版本标签触发构建
4. 等待构建完成并测试可执行文件

## 注意事项

- 首次构建可能需要调整 GDAL 配置
- 每个平台的压缩包约 100-300MB
- 免费 GitHub 账户每月有 2000 分钟构建时间
- 可执行文件包含所有依赖，可在没有 Python 的机器上运行
