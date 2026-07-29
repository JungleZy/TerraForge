# 打包说明

本项目已配置 GitHub Actions 自动构建跨平台可执行文件。

## 使用步骤

### 1. 推送代码到 GitHub

```bash
git add .
git commit -m "Add build configuration"
git push origin master
```

### 2. 创建发布版本（推荐）

```bash
# 创建标签
git tag -a v1.0.0 -m "Release version 1.0.0"

# 推送标签到 GitHub
git push origin v1.0.0
```

### 3. 等待构建完成

- 访问 GitHub 仓库的 Actions 标签页
- 查看 "Build Executables" 工作流运行状态
- 构建通常需要 10-20 分钟

### 4. 下载可执行文件

构建完成后，有两种方式获取：

**方式一：从 Artifacts 下载**
- 在 Actions 页面点击完成的工作流
- 在 "Artifacts" 部分下载对应平台的文件

**方式二：从 Releases 下载（仅标签触发）**
- 访问仓库的 Releases 页面
- 找到对应版本
- 下载附件中的可执行文件

## 手动触发构建

如果不想创建标签，可以手动触发：

1. 访问 GitHub 仓库的 Actions 标签页
2. 选择 "Build Executables" 工作流
3. 点击 "Run workflow" 按钮
4. 选择分支（通常是 master）
5. 点击绿色的 "Run workflow" 按钮

## 本地测试构建

在推送到 GitHub 之前，可以本地测试：

```bash
# Linux/macOS
./build.sh

# Windows
build.bat
```

构建产物在 `dist/terraforge/` 目录。

## 分发给用户

构建完成后，将压缩包分发给用户：

- `terraforge-windows.zip` - Windows 用户
- `terraforge-macos.tar.gz` - macOS 用户
- `terraforge-linux.tar.gz` - Linux 用户

用户只需解压并运行可执行文件，无需安装 Python 或任何依赖。

## 注意事项

1. **首次构建可能失败**：GDAL 在不同平台的安装可能需要调整
2. **文件大小**：每个平台的压缩包约 100-300MB
3. **GitHub Actions 限制**：免费账户每月有 2000 分钟的构建时间
4. **更新 GitHub 用户名**：记得在 `DISTRIBUTION.md` 和 `README.md` 中替换 `YOUR_USERNAME`

## 故障排除

### 构建失败

查看 Actions 日志，常见问题：

1. **GDAL 安装失败**：检查 `.github/workflows/build.yml` 中的 GDAL 安装命令
2. **依赖冲突**：确保 `requirements.txt` 中的版本兼容
3. **内存不足**：减少并发构建或优化依赖

### 可执行文件无法运行

1. **缺少 GDAL 数据**：检查 `build.spec` 中的 GDAL_DATA 配置
2. **权限问题**：Linux/macOS 需要 `chmod +x`
3. **杀毒软件拦截**：Windows 可能需要添加例外

## 更多信息

详细构建文档请参考 [docs/BUILD.md](docs/BUILD.md)
