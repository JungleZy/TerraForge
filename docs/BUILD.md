# 构建可执行文件

## 概述

本项目可以使用 Nuitka 打包成 Windows、macOS 和 Linux 的独立可执行文件。可执行文件包含所有依赖，可在没有安装 Python 的机器上运行。

## 自动构建（GitHub Actions）

### 触发构建

GitHub Actions 工作流会在以下情况自动构建所有平台的可执行文件：

1. **推送标签**（推荐用于发布）：
   ```bash
   git tag -a v1.0.0 -m "发布版本 1.0.0"
   git push origin v1.0.0
   ```

2. **手动触发**（通过 GitHub 网页界面）：
   - 访问 Actions 标签页
   - 选择 "Build Executables" 工作流
   - 点击 "Run workflow"

### 下载构建的可执行文件

工作流完成后：
- 访问 Actions 标签页
- 点击完成的工作流运行
- 从 "Artifacts" 部分下载构建产物
- 对于标签发布，可执行文件也会附加到 GitHub Release

## 本地构建

### 前置要求

1. **Python 3.12+** 已安装
2. **GDAL** 已安装：
   - **Ubuntu/Debian**: `sudo apt-get install gdal-bin libgdal-dev`
   - **macOS**: `brew install gdal`
   - **Windows**: `choco install gdal` 或从 https://gdal.org 下载

3. **安装依赖**：
   ```bash
   pip install -r requirements.txt
   pip install nuitka
   ```

### 构建命令

#### Linux/macOS
```bash
./build.sh
```

#### Windows
```cmd
build.bat
```

#### 手动构建
```bash
python nuitka_build.py
```

### 输出

可执行文件将创建在：
```
dist/terraforge/
├── terraforge(.exe)    # 主程序
├── templates/              # Web UI 模板
├── static/                 # 静态资源
└── [其他打包文件]
```

## 分发

### 打包用于分发

#### Linux/macOS
```bash
cd dist
tar -czf terraforge-linux.tar.gz terraforge/
```

#### Windows
```cmd
cd dist
powershell Compress-Archive -Path terraforge/* -DestinationPath terraforge-windows.zip
```

### 测试可执行文件

1. 进入 dist 文件夹：
   ```bash
   cd dist/terraforge
   ```

2. 运行可执行文件：
   - **Linux/macOS**: `./terraforge`
   - **Windows**: `terraforge.exe`

3. 打开浏览器访问 `http://localhost:5000`

## 故障排除

### GDAL 问题

如果可执行文件中 GDAL 加载失败：

1. **检查 GDAL 安装**：
   ```bash
   gdal-config --version  # Linux/macOS
   gdalinfo --version     # Windows
   ```

2. **验证 GDAL_DATA 路径**（打包模式下由 `core/bundle.py:setup_bundle_env()` 设置）

3. **使用详细输出重新构建**：在 `nuitka_build.py` 的 Nuitka 参数中追加
   `--show-progress --show-modules`，重新运行 `python nuitka_build.py`

### 缺少依赖

如果可执行文件因导入错误失败：

1. 将缺少的模块添加到 `nuitka_build.py` 中的 `--include-package` 参数
2. 重新构建可执行文件

### 可执行文件体积过大

要减小体积：

1. 从 `requirements.txt` 中移除未使用的依赖
2. 在 `nuitka_build.py` 中使用 `--nofollow-import-to` 排除不必要的包
3. 如确认环境兼容，可尝试 `--lto=yes` 减小编译产物体积

### 平台特定问题

#### macOS: "应用已损坏"
```bash
xattr -cr dist/terraforge
```

#### Linux: 权限被拒绝
```bash
chmod +x dist/terraforge/terraforge
```

#### Windows: 杀毒软件误报
- 在杀毒软件中添加例外
- 使用代码签名证书对可执行文件签名

## 构建配置

### nuitka_build.py

`nuitka_build.py` 控制构建过程：

- **`--include-data-dir`**: 包含模板、静态文件、GDAL/PROJ 数据
- **`--include-package`**: Nuitka 静态分析未自动检测到的 Python 包
- **`--include-package-data`**: 包内数据文件（matplotlib mpl-data、certifi CA 证书）
- **GDAL/PROJ 数据目录发现**: 按平台从环境变量、`gdal-config`、conda 布局中定位，找不到时构建直接失败
- **系统依赖库补拷**: Nuitka 只复制 Python/conda 前缀内的依赖库；Linux（apt GDAL，补拷 ldd 闭包）和 Windows 非 conda 布局（OSGeo4W 等，用 Nuitka 自带扫描器补拷 DLL 闭包）会在构建后把 GDAL 相关系统库补进 dist 根目录，并自检无缺失依赖

### 自定义

要自定义构建：

1. **更改应用名称**: 修改 `nuitka_build.py` 中的 `APP_NAME`
2. **添加图标**: 在 Nuitka 参数中添加 `--windows-icon-from-ico=icon.ico` / `--macos-app-icon=icon.icns`
3. **单文件模式**: 在 Nuitka 参数中添加 `--onefile`（启动时会解压到临时目录，启动变慢）
4. **无控制台窗口**: 在 Nuitka 参数中添加 `--windows-console-mode=disable`（仅 Windows）

## CI/CD 集成

`.github/workflows/build.yml` 工作流：

1. 设置 Python 3.12
2. 为每个平台安装 GDAL
3. 安装 Python 依赖
4. 使用 Nuitka 构建可执行文件
5. 打包结果
6. 上传构建产物
7. 创建 GitHub Release（对于标签）

### 工作流自定义

编辑 `.github/workflows/build.yml` 以：
- 更改 Python 版本
- 添加代码签名
- 修改打包格式
- 添加额外的构建步骤

## 安全考虑

- 可执行文件包含所有源代码（可被提取）
- 考虑对敏感代码进行混淆
- 为生产分发签名可执行文件
- 分发前使用杀毒软件扫描

## 性能

可执行文件启动比 Python 脚本慢，因为：
- 解包打包的文件
- 加载所有依赖

改进方法：
- 使用 `--onefile` 分发单文件（但首次启动需解压，更慢）
- 用 `--nofollow-import-to` 排除未使用的模块
- 在代码中使用延迟导入

## 已知问题

### Windows GDAL 安装
Windows 上的 GDAL 安装可能不稳定。如果构建失败：
- 尝试使用 OSGeo4W 安装 GDAL
- 或使用预编译的 GDAL wheel（注意 wheel 的 cp 标签要匹配 Python 版本）：`pip install GDAL-3.8.4-cp312-cp312-win_amd64.whl`

### macOS 代码签名
未签名的 macOS 应用可能被 Gatekeeper 阻止。考虑：
- 使用 Apple Developer 证书签名
- 或指导用户使用 `xattr -cr` 命令

### Linux 依赖
不同 Linux 发行版可能需要不同的系统库。建议：
- 在 Ubuntu 20.04 上构建（兼容性好）
- 或为每个主要发行版单独构建

## 技术支持

构建问题：
1. 查看 Nuitka 文档：https://nuitka.net
2. 查看 GitHub Actions 中的构建日志
3. 提交 issue 并附上构建输出
