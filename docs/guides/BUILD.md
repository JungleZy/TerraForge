# 构建可执行文件

## 概述

本项目可以使用 Nuitka 打包成 Windows、macOS 和 Linux 的独立可执行文件。可执行文件包含所有依赖，可在没有安装 Python 的机器上运行。

## 自动构建（GitHub Actions）

### 发版前检查

打 tag 之前逐条过一遍，漏掉任何一条都会发出错版本：

1. **改版本号**：`src/core/config.py:38` 的 `APP_VERSION` 是**唯一真源**（`app.py` 读取它）。
   不要再去找 `build.spec` —— 那是 PyInstaller 时代的文件，已随 Nuitka 迁移删除。
2. **更新仓库根的 `RELEASE_NOTES.md`**：GitHub Release 的正文直接取自这个文件
   （`build.yml` 的 `body_path: RELEASE_NOTES.md`），它只放**本次**发版内容；
   历史条目归档在 `CHANGELOG.md`。忘了改就会发出上一版的说明。
3. **本地跑通 `./build.sh`（或 `build.bat`），并启动产物做一次真实任务**
   —— 下载一小块瓦片、切一次地形，把 GDAL 读写路径真正走一遍。
   CI 绿不代表产物可用（见下面「CI 覆盖范围」）。
4. **先 push commit，再打 tag**：tag 只是指向某个 commit 的指针，
   本地提交没 push 上去，CI 会 checkout 到旧代码，构建出不含新改动的产物。

### 触发构建

GitHub Actions 工作流会在以下情况自动构建所有平台的可执行文件：

1. **推送标签**（推荐用于发布）：
   ```bash
   git push origin master        # 先确认改动已在远端
   git tag -a v1.0.0 -m "发布版本 1.0.0"
   git push origin v1.0.0
   ```

2. **手动触发**（通过 GitHub 网页界面）：
   - 访问 Actions 标签页
   - 选择 "Build Executables" 工作流
   - 点击 "Run workflow"

### 下载构建的可执行文件

取哪里取决于构建是怎么触发的：

- **标签构建**：产物**只**作为附件挂在 GitHub Release 上，**没有 Artifacts**
  （`build.yml` 里 upload-artifact 对 tag 显式跳过，避免同一个文件双倍占用账号级
  500 MB 的 Actions 存储配额）。去 Releases 页面下载。
- **手动触发 / 分支构建**：Actions 标签页 → 点开对应的工作流运行 →
  从 "Artifacts" 部分下载（保留 7 天）。

## 本地构建

### 前置要求

1. **Python 3.12+** 已安装
2. **GDAL 与 Python 依赖**（项目使用 uv 管理环境，CI 用的就是下面这套流程）

   **顺序不能颠倒**：GDAL 的 Python 绑定是从源码编译的，编译时必须能 `import numpy`，
   否则编不出 `_gdal_array` 扩展 —— 而缺了它构建照样成功、CI 冒烟测试照样绿，
   只有运行时调 `band.ReadAsArray()` / `WriteArray()`（拼接 GeoTIFF、地形切片）才炸。

   **Linux（apt + uv pip 编译）**
   ```bash
   sudo apt-get update
   sudo apt-get install -y gdal-bin libgdal-dev patchelf
   uv venv                                                  # 如果 .venv 不存在
   uv pip install setuptools wheel                          # --no-build-isolation 需要 setuptools.build_meta 后端
   uv pip install numpy==1.26.4                             # 必须在 GDAL 之前
   uv pip install --no-build-isolation GDAL==3.8.4          # 版本要与 gdal-config --version 的 major.minor 一致
   uv pip install -r requirements.txt
   uv pip install nuitka
   ```

   **Windows / macOS（conda-forge 预编译包）**
   ```bash
   conda install -y -c conda-forge gdal=3.8 numpy
   # GDAL 已由 conda 提供，再 pip 装一遍会去编译 sdist 并失败 —— 剥掉 pin 装其余依赖
   grep -viE '^[[:space:]]*gdal([[:space:]=<>!~]|$)' requirements.txt > /tmp/req.txt
   uv pip install -r /tmp/req.txt
   uv pip install nuitka
   ```
   Windows 上这段要在 bash 里跑（Git Bash 或 conda 自带的 bash，CI 用的也是 `bash -l`）；
   在 cmd/PowerShell 里没有 `grep`，可手工复制一份 `requirements.txt` 删掉 `GDAL==` 那行再装。

   这两个平台不用 brew / choco：Windows 的 choco gdal 包已废弃；macOS 运行器是
   Apple Silicon(arm64)，brew 的 gdal 只有 arm64，而 pip 的 GDAL sdist 会尝试构建
   universal2 wheel 并链接失败。conda-forge 提供匹配的 arm64 预编译包和 Python 绑定，无需编译。

3. **验证 GDAL 绑定完整**（这一步必做）：
   ```bash
   uv run python -c "from osgeo import gdal_array; print('gdal_array OK')"
   ```
   只 `import gdal` 是查不出问题的 —— 缺 `_gdal_array` 时 `gdal` 本身照常导入。
   也可以直接看文件是否存在：`ls .venv/lib/python3.12/site-packages/osgeo/ | grep _gdal_array`
   （Windows 下是 `_gdal_array*.pyd`）。

   报 `ImportError: cannot import name '_gdal_array'` 时的修复步骤见
   [INSTALL.md 的故障排除](INSTALL.md#importerror-cannot-import-name-_gdal_array-from-osgeo)。

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
uv run python nuitka_build.py
```

`build.sh` / `build.bat` 在调用 Nuitka 前会做一次 **GDAL 版本一致性硬校验**：
比对 `requirements.txt` 里的 `GDAL==` pin 与实际绑定版本（`osgeo.gdal.__version__`，
取不到时回退 `gdal-config --version`），major.minor 不一致直接报错退出。
直接跑 `nuitka_build.py` 会跳过这个校验。

### 输出

可执行文件将创建在：
```
dist/terraforge/
├── terraforge(.exe)    # 主程序
├── templates/          # Web UI 模板
├── static/             # 静态资源
├── gdal-data/          # GDAL 数据文件（坐标系、EPSG 表等）
├── proj-data/          # PROJ 数据文件（proj.db）
└── [依赖库与其他打包文件]
```

**必须整目录复制/分发。** 单独拷走可执行文件会缺 `gdal-data/` / `proj-data/` 与依赖库，
程序启动时 `src/core/bundle.py:setup_bundle_env()` 找不到数据目录会直接报错。

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

2. **验证 GDAL_DATA 路径**（打包模式下由 `src/core/bundle.py:setup_bundle_env()` 设置）

3. **使用详细输出重新构建**：在 `nuitka_build.py` 的 Nuitka 参数中追加
   `--show-progress --show-modules`，重新运行 `uv run python nuitka_build.py`

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

`.github/workflows/build.yml` 工作流（矩阵：`ubuntu-latest` / `windows-latest` / `macos-latest`）：

1. 设置 Python 3.12（Linux 用 setup-python；Windows/macOS 用 Miniconda）
2. 为每个平台安装 GDAL（Linux=apt，Windows/macOS=conda-forge，见上面「前置要求」）
3. 安装 Python 依赖
4. **跑完整测试套件** `python -m pytest tests/ -q` —— 测试失败即构建失败
5. 使用 Nuitka 构建可执行文件
6. 冒烟测试：启动 exe 并请求首页
7. 打包结果
8. 上传构建产物（**仅非标签构建**）
9. 创建 GitHub Release（对于标签，正文取自 `RELEASE_NOTES.md`）

### CI 覆盖范围（重要）

**CI 全绿不等于产物可用。** 冒烟测试只做一件事：启动 exe 后 `curl http://127.0.0.1:5000/`
拿到 200，**完全不碰 GDAL 代码路径**。所以 GDAL 缺 `_gdal_array` 时，
构建成功、冒烟通过，坏包照样发出去，用户要到实际下载拼接或切地形时才炸。

结论：发版前必须本地启动产物做一次真实任务（见「发版前检查」第 3 条）。

### 工作流自定义

编辑 `.github/workflows/build.yml` 以：
- 更改 Python 版本
- 添加代码签名
- 修改打包格式
- 添加额外的构建步骤

## 安全考虑

- Nuitka 把 Python 源码编译成 C 再编成机器码，产物里**不含 .py 源文件**，
  不像解释器打包方案那样能直接解包取回源码（但仍可被反编译分析，不等于加密）
- 应用监听 `0.0.0.0:5000`（端口硬编码不可配置）且**没有任何鉴权**，
  启动横幅会主动打印局域网 URL。只在可信网络内运行，不要直接暴露到公网
- 为生产分发签名可执行文件
- 分发前使用杀毒软件扫描

## 性能

Nuitka standalone 产物启动比 `python app.py` 略慢，主要开销在加载依赖库
（GDAL/PROJ 等原生库 + 全部 Python 模块），不涉及解包 —— 文件是直接躺在
`dist/terraforge/` 里的。

改进方法：
- 用 `--nofollow-import-to` 排除未使用的模块
- 在代码中使用延迟导入
- 避免 `--onefile`：它会在每次启动时把整个 dist 解压到临时目录，启动明显更慢

## 已知问题

### Windows GDAL 安装
Windows 上的 GDAL 安装可能不稳定。**首选 conda-forge**（CI 就是这么装的，见「前置要求」）：
`conda install -y -c conda-forge gdal=3.8 numpy`。choco 的 gdal 包已废弃，不要用。

conda 不可用时的备选：
- 用 OSGeo4W 安装 GDAL（`nuitka_build.py` 支持这种布局，构建后会补拷 DLL 闭包）
- 或使用预编译的 GDAL wheel（注意 wheel 的 cp 标签要匹配 Python 版本）：
  `uv pip install GDAL-3.8.4-cp312-cp312-win_amd64.whl`
  —— 装完仍要跑「前置要求」第 3 步验证 `_gdal_array` 是否存在

### macOS 代码签名
未签名的 macOS 应用可能被 Gatekeeper 阻止。考虑：
- 使用 Apple Developer 证书签名
- 或指导用户使用 `xattr -cr` 命令

### Linux 依赖
不同 Linux 发行版可能需要不同的系统库（glibc 版本决定了产物能跑在哪些发行版上）。建议：
- 与 CI 保持一致，在 `ubuntu-latest` 对应的 Ubuntu 版本上构建
- 需要兼容更老的发行版时，在更老的基线系统上构建
- 或为每个主要发行版单独构建

## 技术支持

构建问题：
1. 查看 Nuitka 文档：https://nuitka.net
2. 查看 GitHub Actions 中的构建日志
3. 提交 issue 并附上构建输出
