# 安装指南

本文介绍从源码安装 TerraForge 的完整流程。如果只是使用，推荐直接从 [Releases](https://github.com/JungleZy/map-download/releases) 下载预编译可执行文件，无需安装任何依赖。

## 前置条件

- **Python 3.12+**
- **GDAL 系统库**
- **[uv](https://docs.astral.sh/uv/)** —— 本项目用 uv 管理虚拟环境与依赖（Windows / macOS 走步骤 2 的 conda 路线时用 conda env 代替），安装：

```bash
# Linux/macOS
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## 安装步骤

### 1. 安装 GDAL 系统库

**Ubuntu/Debian:**

```bash
sudo apt-get update
sudo apt-get install -y gdal-bin libgdal-dev
```

**macOS:**

```bash
brew install gdal        # 仅 Intel Mac
```

Apple Silicon（arm64）不要走 brew：brew 的 gdal 是 arm64-only，而 pip 的 GDAL 源码包会尝试构建 universal2 wheel 并链接失败。改用 conda-forge，见步骤 2。

**Windows:** 用 conda-forge 安装（见步骤 2）——choco 的 gdal 包已废弃。也可用 [OSGeo4W](https://trac.osgeo.org/osgeo4w/)，但 Python 绑定要自行匹配版本。

### 2. 克隆代码并安装 Python 依赖

```bash
git clone https://github.com/JungleZy/map-download.git
cd map-download
```

GDAL 的 Python 绑定在 PyPI 上只有源码包，安装时要现场编译，而**编译顺序错了会静默装出一个残缺的绑定**：`import gdal` 照常成功，一调用 `band.ReadAsArray()` 就 `ImportError: cannot import name '_gdal_array'`（瓦片拼接、地形切片、等高线渲染全都要用它）。所以下面的命令顺序不能调换。

**Linux（步骤 1 用 apt 装的 GDAL）：**

```bash
uv venv --python 3.12                             # 创建 .venv（如已存在可跳过）
uv pip install setuptools wheel                   # --no-build-isolation 下要自备构建后端，Python 3.12 不再自带 setuptools
uv pip install numpy==1.26.4                      # 必须早于 GDAL：编译 GDAL 时能 import numpy，才编得出 _gdal_array
uv pip install --no-build-isolation "GDAL==$(gdal-config --version)"   # 版本必须跟随系统 libgdal，见下方
uv pip install -r requirements.txt                # 其余依赖
```

默认的 build isolation 会把 GDAL 丢到一个临时的干净环境里编译，那里没有 numpy，于是 `_gdal_array` 被跳过、编译却照样"成功"——这就是残缺绑定的由来。项目 CI 在 Linux 上也是这个顺序。

**为什么这里不写一个具体的版本号。** Python 绑定是 sdist 现场编译的，编译时要对上系统 `libgdal` 的头文件，所以它的版本**跟随机器**：开发机（ubuntugis-unstable PPA）3.11.4、CI `ubuntu-latest`（noble apt）3.8.4、Windows/macOS（conda-forge）3.8。`$(gdal-config --version)` 正是「问机器要它自己那一版」。钉一个具体值，在另外两台机器上就会卸掉能用的绑定、按钉的版本重编 —— 而那次重编没有 `--no-build-isolation`，编出来的绑定又缺 `_gdal_array`，绕回本节开头那个坑。依赖声明因此给的是范围（见 `requirements.txt` 顶部的注释），换版本不需要回填任何文件；`scripts/check_gdal.py` 校验的也正是「装出来的版本落在那个范围内 + `_gdal_array` 在位」，而不是某个精确值。

**Windows / macOS（conda-forge 装 GDAL）：**

这两个平台不要用 pip/uv 装 GDAL（Windows 缺编译环境、macOS arm64 会链接失败，原因见步骤 1）。conda-forge 的 gdal 包**自带已含 numpy 支持的 Python 绑定**，不需要任何编译，CI 在这两个平台走的也是这条路：

```bash
conda create -n terraforge -c conda-forge python=3.12 gdal=3.8 numpy
conda activate terraforge

# GDAL 已由 conda 提供，装其余依赖前要把 requirements.txt 里的 GDAL 那一行剥掉，
# 否则 pip 会去 PyPI 重新编译源码包。下面这条命令在 bash / cmd / PowerShell 里都能跑：
python -c "import re,pathlib; src=pathlib.Path('requirements.txt').read_text(encoding='utf-8'); pathlib.Path('req-no-gdal.txt').write_text(''.join(l for l in src.splitlines(True) if not re.match(r'\s*GDAL\s*[=<>!~]', l, re.I)), encoding='utf-8')"
pip install -r req-no-gdal.txt
# req-no-gdal.txt 是临时文件，装完可删（别提交）
```

> 走 conda 路线时环境就是这个 conda env（自带 pip），本文后续及其它文档里的 `uv run python xxx` 一律换成直接 `python xxx`。

### 3. 验证安装

```bash
uv run python -c "import flask; print('Flask:', flask.__version__)"
uv run python -c "from osgeo import gdal; print('GDAL:', gdal.__version__)"
uv run python -c "from osgeo import gdal_array; print('gdal_array:', gdal_array.__file__)"
```

三条都通过才算装好，其中第三条是关键：`from osgeo import gdal` 只证明绑定装上了，**绑定缺 numpy 支持时它照样成功**；`gdal_array` 才会真正去 import `_gdal_array` 这个 C 扩展，编译时漏掉它这里就会 `ImportError`（处理办法见故障排除）。

### 4. 启动应用

```bash
uv run python app.py
```

应用将在 `http://0.0.0.0:5000` 启动（数据库首次启动时自动初始化）。浏览器访问 `http://localhost:5000`。

## 故障排除

### ImportError: cannot import name '_gdal_array' from 'osgeo'

按步骤 2 装不会遇到这个问题。如果之前直接 `uv pip install -r requirements.txt` 装过（GDAL 在没有 numpy 的隔离环境里编译了一次），需要先在 venv 里备好 numpy，再从源码强制重建（Linux / Intel Mac 的 pip 编译路线；conda 路线的绑定自带 numpy 支持，不会出这个问题）：

```bash
uv pip install numpy setuptools wheel
UV_NO_CACHE=1 uv pip install --force-reinstall --no-build-isolation --no-binary :all: "GDAL==$(gdal-config --version)"
```

`UV_NO_CACHE=1` 是必须的：uv 会缓存 sdist 构建结果，不加它会静默复用之前没有 numpy 的损坏构建。重建后用步骤 3 的第三条命令验证：

```bash
uv run python -c "from osgeo import gdal_array; print(gdal_array.__file__)"
```

### GDAL 版本不匹配

```bash
gdal-config --version                  # 系统 GDAL 版本
uv pip install --no-build-isolation GDAL==$(gdal-config --version)
```

换版本**不需要动 `requirements.txt`**：那里给的是范围 `GDAL>=3.8,<4`（原因见该文件顶部的注释——绑定是 sdist 现编，版本跟随机器），装什么具体版本都不必回填。`build.sh` / `build.bat` 开头会调 `scripts/check_gdal.py` 校验两件事：装出来的版本落在这个范围内，且 `_gdal_array` 在位（带 build isolation 装会静默丢掉它）。任一条不满足就报错退出，附带正确的装法。

### pip 命令未找到 / 权限错误

uv 创建的 `.venv` 默认不装 pip，所以 Linux 路线一律用 `uv pip install`（在项目虚拟环境里安装，无需 sudo，也不污染系统 Python），不要用裸 `pip`。Windows / macOS 的 conda 路线相反——conda env 自带 pip，步骤 2 里直接用 `pip install` 即可。
