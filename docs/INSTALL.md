# 安装指南

本文介绍从源码安装 TerraForge 的完整流程。如果只是使用，推荐直接从 [Releases](https://github.com/JungleZy/map-download/releases) 下载预编译可执行文件，无需安装任何依赖。

## 前置条件

- **Python 3.12+**
- **GDAL 系统库**
- **[uv](https://docs.astral.sh/uv/)** —— 本项目用 uv 管理虚拟环境与依赖，安装：

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
brew install gdal
```

**Windows:** 通过 [OSGeo4W](https://trac.osgeo.org/osgeo4w/) 或 conda 安装 GDAL。

### 2. 克隆代码并安装 Python 依赖

```bash
git clone https://github.com/JungleZy/map-download.git
cd map-download

uv venv                              # 创建 .venv（如已存在可跳过）
uv pip install -r requirements.txt
```

`requirements.txt` 中 `GDAL==3.8.4` 需与系统 GDAL 版本一致。版本不匹配或构建失败时，改用与系统匹配的版本：

```bash
gdal-config --version                             # 查看系统 GDAL 版本
uv pip install gdal==$(gdal-config --version)
```

### 3. 验证安装

```bash
uv run python -c "import flask; print('Flask:', flask.__version__)"
uv run python -c "from osgeo import gdal; print('GDAL:', gdal.__version__)"
```

### 4. 启动应用

```bash
uv run python app.py
```

应用将在 `http://0.0.0.0:5000` 启动（数据库首次启动时自动初始化）。浏览器访问 `http://localhost:5000`。

## 故障排除

### ImportError: cannot import name '_gdal_array' from 'osgeo'

GDAL Python 绑定在编译时没有 numpy 支持（`band.ReadAsArray()` 等功能依赖它）。需要在虚拟环境中先装好 numpy，再从源码强制重建：

```bash
uv pip install numpy setuptools wheel
UV_NO_CACHE=1 uv pip install --force-reinstall --no-build-isolation --no-binary :all: "GDAL==$(gdal-config --version)"
```

`UV_NO_CACHE=1` 是必须的：uv 会缓存 sdist 构建结果，不加它会静默复用之前没有 numpy 的损坏构建。验证：

```bash
ls .venv/lib/python3.12/site-packages/osgeo/ | grep _gdal_array
```

### GDAL 版本不匹配

```bash
gdal-config --version                  # 系统 GDAL 版本
uv pip install GDAL==$(gdal-config --version)
```

### pip 命令未找到 / 权限错误

本项目不直接使用 pip，一律通过 `uv pip` 在项目虚拟环境中安装，无需 sudo，也不污染系统 Python。
