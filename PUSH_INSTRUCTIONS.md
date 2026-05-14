# 推送 v0.0.1 版本到 GitHub

## 自动推送脚本已创建

我已经为你创建了推送脚本，因为需要 GitHub 认证，请在终端手动运行：

### Linux/macOS
```bash
./push-release.sh
```

### Windows
```cmd
push-release.bat
```

## 或者手动执行以下命令

### 1. 推送代码到 GitHub
```bash
git push origin master
```

### 2. 创建版本标签
```bash
git tag -a v0.0.1 -m "测试版本 v0.0.1 - 跨平台打包配置"
```

### 3. 推送标签到 GitHub
```bash
git push origin v0.0.1
```

## 推送后的步骤

1. **访问 GitHub Actions**
   - URL: https://github.com/JungleZy/map-download/actions
   - 查看 "Build Executables" 工作流

2. **监控构建进度**
   - 点击最新的工作流运行
   - 查看三个平台的构建状态
   - 预计时间：15-30 分钟

3. **下载可执行文件**
   - 构建完成后访问：https://github.com/JungleZy/map-download/releases
   - 下载 v0.0.1 的三个平台压缩包

4. **测试可执行文件**
   - 解压并运行
   - 验证所有功能正常

## 如果构建失败

1. 查看 Actions 日志
2. 根据错误信息调整配置
3. 修复后重新推送

## GitHub 认证

如果推送时需要认证：

### 使用 Personal Access Token (推荐)
```bash
# 设置 Git 使用 token
git remote set-url origin https://YOUR_TOKEN@github.com/JungleZy/map-download.git
```

### 或使用 SSH
```bash
# 改用 SSH URL
git remote set-url origin git@github.com:JungleZy/map-download.git
```

## 当前状态

- ✅ 所有代码已提交到本地 Git
- ✅ 推送脚本已创建
- ⏳ 等待推送到 GitHub
- ⏳ 等待创建标签
- ⏳ 等待 GitHub Actions 构建

## 下一步

请在终端运行 `./push-release.sh` 或 `push-release.bat` 来推送代码和创建版本！
