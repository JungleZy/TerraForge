# TerraForge

[![Release](https://img.shields.io/github/v/release/JungleZy/map-download)](https://github.com/JungleZy/map-download/releases)
[![Build](https://github.com/JungleZy/map-download/actions/workflows/test-build.yml/badge.svg)](https://github.com/JungleZy/map-download/actions/workflows/test-build.yml)
[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](#许可证)

基于 Web 的 GIS 数据获取与加工系统。在一个界面里完成四类地理数据工作：**Google Maps 瓦片下载**、**DEM 高程数据获取**、**Cesium 3D 地形切片**与**等高线地图生成**，支持交互式地图选区、实时进度监控、历史记录可视化与完善的配置管理。

提供 Windows / macOS / Linux 预编译可执行文件，解压即用，无需安装 Python 环境。

## 目录

- [功能特性](#功能特性)
- [技术栈](#技术栈)
- [快速开始](#快速开始)
- [使用指南](#使用指南)
- [项目结构](#项目结构)
- [API 端点](#api-端点)
- [开发](#开发)
- [性能设计](#性能设计)
- [构建可执行文件](#构建可执行文件)
- [故障排除](#故障排除)
- [注意事项](#注意事项)
- [许可证](#许可证)

## 功能特性

### 四条数据管线

- **地图瓦片下载** — 交互式框选区域，从 Google Maps 下载瓦片，可选拼接为带地理配准的 GeoTIFF（GDAL）
- **DEM 高程下载** — 按区域自动计算并下载 1°×1° 高程分幅：默认 Copernicus GLO-30（公开 S3 桶，免认证），可选 ASTER GDEM v3（ASTGTM.003，需 Earthdata 账号）
- **3D 地形切片** — 将下载的 DEM 或本地上传的 GeoTIFF 切成 Cesium quantized-mesh 地形，内置全球低层级基础地形，CesiumJS 端自动级联加载
- **等高线生成** — 从上传的 DEM 渲染等高线 XYZ 瓦片：间距、色彩、分层设色、晕渲均可配置，支持样式预览

### 任务与进度

- 📊 WebSocket 实时进度：下载速度、剩余时间、分 zoom 拼接与复制阶段全程可见，大任务不再「卡 100%」
- ⏸️ 任务调度：暂停 / 恢复 / 取消，断点续传，已下载瓦片不重复下载
- 🗂 下载历史可视化：历史区域叠加在地图上，已完成任务可直接预览瓦片 / 地形 / 晕渲效果
- 💾 保存路径全盘可选：任意绝对路径 + 目录浏览弹窗；删除任务可选是否清理磁盘产物（带安全护栏）

### 平台能力

- 🎨 深色 / 浅色 / 跟随系统主题
- ⚙️ 完善的配置页：并发数（支持实测网速推荐）、代理、缓存管理、GDAL 参数、Earthdata 账号等
- 🧹 缓存管理：按分类查看占用、手动清理，缓存不会被静默删除
- 🌐 局域网访问支持，适合内网部署
- 📦 Nuitka 打包为独立可执行文件，目标机器零依赖

## 技术栈

| 层 | 技术 |
| --- | --- |
| 后端 | Flask · Flask-SocketIO · aiohttp · GDAL · SQLite |
| 前端 | CesiumJS 1.143 · Bootstrap 5.3 · Socket.IO（第三方库全部本地 vendor 于 `static/vendor/`，不依赖 CDN） |
| 打包 | Nuitka（standalone，自动收集 GDAL/PROJ 数据与系统库闭包） |
| 测试 | pytest（含 API 契约、任务生命周期、路径安全等 90+ 测试文件） |
| 环境管理 | uv |

## 快速开始

### 方式一：预编译可执行文件（推荐）

1. 从 [Releases](https://github.com/JungleZy/map-download/releases) 下载对应平台的压缩包
2. 解压后运行：
   - **Windows**: 双击 `terraforge.exe`
   - **macOS / Linux**: `./terraforge`
3. 浏览器访问 `http://localhost:5000`

详见 [docs/guides/DISTRIBUTION.md](docs/guides/DISTRIBUTION.md)。

### 方式二：从源码运行

**前置要求**：Python 3.12+、GDAL 系统库、[uv](https://docs.astral.sh/uv/)

```bash
# 1. 安装 GDAL 系统库
#    Ubuntu/Debian:
sudo apt-get install -y gdal-bin libgdal-dev
#    macOS:
brew install gdal

# 2. 安装 Python 依赖（uv 管理虚拟环境）
uv venv                              # .venv 不存在时
uv pip install -r requirements.txt

# 3. 启动（数据库首次启动时自动初始化）
uv run python app.py
```

应用监听 `http://0.0.0.0:5000`。更详细的安装说明（含 GDAL 绑定编译问题）见 [docs/guides/INSTALL.md](docs/guides/INSTALL.md)。

## 使用指南

### 下载地图瓦片

1. 主页地图上用矩形工具框选下载区域
2. 设置任务名称、地图样式、缩放级别范围与输出格式
3. 保存路径需为绝对路径，可点「浏览」在弹窗中选择（0.2.4 起全盘可选）
4. 创建任务后点击「启动」，实时观察进度

**地图样式**：

| 样式 | 代码 | 说明 |
| --- | --- | --- |
| 标准地图 | `m` | 标准道路地图 |
| 卫星图 | `s` | 纯卫星影像 |
| 卫星图+标注 | `y` | 卫星影像带道路标注 |
| 道路图 | `h` | 仅道路网络 |
| 地形图 | `t` | 地形等高线 |

**输出格式**：由「瓦片」「GeoTIFF」两个复选框组合——都选（默认）= 瓦片 + 拼接 GeoTIFF；只选其一 = 仅该产物。瓦片在下载过程中实时镜像到产物目录（边下边复制），下载完成后拼接阶段进度同样可见。

**产物结构**：

```
<保存路径>/task_<任务ID>/
├── <zoom>/<x>/<y>.png        # 原始瓦片
└── <任务名>_zoom_<zoom>.tif  # 拼接 GeoTIFF（按 zoom 一层一张）
```

### DEM 高程与 3D 地形

1. 下载类型切换为 DEM，选择数据源后框选区域创建任务（默认 Copernicus GLO-30 免认证；选 ASTER GDEM v3 需先在配置页填写 Earthdata 账号）
2. DEM 任务下载完成后，可对其启动「地形切片」，生成 Cesium quantized-mesh 地形
3. 已有 GeoTIFF 可直接上传为**本地地形任务**，跳过下载直接切片
4. 历史记录页可预览地形效果（无切片时按需渲染晕渲图）

### 等高线地图

1. 在左侧「数据处理」面板把**处理类型**切到「等高线瓦片」，上传高程文件（.tif/.tiff，可多选），设置间距、配色、晕渲等样式（支持样式预览）
2. 任务从上传的 DEM 渲染等高线并输出 XYZ 瓦片；远程高程数据请先用 DEM 任务下载
3. 产物以标准 XYZ 瓦片组织，可直接供 Leaflet / OpenLayers / CesiumJS 使用

### 历史记录

访问 `/history` 查看全部任务：统计概览、区域地图叠加、任务检索与预览。删除任务时通过 `delete_files` 选项控制是否同时清理磁盘产物。

### 配置

访问 `/config` 页面：

- **外观** — 深色 / 浅色 / 跟随系统
- **基础设置** — 默认保存路径（绝对路径，支持「浏览」）、默认样式与缩放级别
- **下载设置** — 并发数（「测速推荐」按当前网络实测给出建议值）、超时、重试、代理、瓦片服务器列表（逐条验证连通性）
- **缓存设置** — 启用/禁用瓦片缓存；缓存管理按分类查看占用并手动清理（二次确认），缓存不会自动删除
- **GDAL 设置** — 压缩方式、重采样算法
- **其他设置** — 历史记录保留天数、地图初始位置
- **Earthdata 设置** — NASA Earthdata Login 账号（仅 ASTER GDEM v3 与水体掩膜数据需要；默认的 Copernicus GLO-30 免认证）

## 项目结构

```
map-download/
├── app.py                  # Flask 应用入口（组合根：注册蓝图、注入管理器）
├── nuitka_build.py         # Nuitka 打包配置（GDAL/PROJ 环境设置在 src/core/bundle.py）
├── build.sh / build.bat    # 本地构建脚本
├── requirements.txt        # Python 依赖
├── src/                    # 全部业务源码（可导入包，根目录天然在 sys.path 上）
│   ├── core/               # 应用基础设施
│   │   ├── config.py          # 配置类（路径、密钥、版本）
│   │   ├── database.py        # 数据库初始化与连接管理（含幂等迁移）
│   │   ├── startup_banner.py  # 启动信息横幅
│   │   └── process_watchdog.py # reloader 子进程看门狗
│   ├── models/             # 数据模型（任务/瓦片模型与枚举）
│   ├── services/           # 业务逻辑
│   │   ├── download_engine.py  # Google 瓦片下载引擎
│   │   ├── task_manager.py     # 瓦片任务管理器
│   │   ├── config_manager.py   # 配置管理器
│   │   ├── dem_download_engine.py  # DEM 下载引擎（Copernicus GLO-30 / ASTER GDEM）
│   │   ├── dem_task_manager.py     # DEM 任务管理器
│   │   ├── dem_granules.py         # 1°×1° 分幅命名工具（GLO-30 / ASTGTM / ASTWBD）
│   │   ├── earthdata_client.py     # NASA Earthdata Login 认证
│   │   ├── contour_engine.py       # 等高线生成引擎
│   │   ├── contour_task_manager.py # 等高线任务管理器
│   │   ├── contour_task_tiler.py   # 等高线瓦片切分
│   │   ├── local_terrain_task_manager.py  # 本地地形（上传 GeoTIFF）任务管理器
│   │   ├── terrain_tiling/         # Cesium quantized-mesh 地形切片
│   │   ├── geo_validation.py       # bbox / 缩放级别校验（各管线共用）
│   │   ├── system_proxy.py         # 系统代理检测
│   │   └── task_cleanup.py         # 任务产物清理与缓存管理
│   └── routes/             # Flask 路由
│       ├── main.py            # 页面路由
│       ├── api.py             # 瓦片任务 / 历史 / 配置 / 缓存 API
│       ├── dem_api.py         # DEM 任务 API
│       ├── terrain_api.py     # DEM 地形切片 API
│       ├── local_terrain_api.py  # 本地地形 API
│       ├── contour_api.py     # 等高线 API
│       ├── terrain_static.py  # 地形瓦片静态服务
│       ├── tiles_static.py    # 地图瓦片静态服务
│       ├── contour_static.py  # 等高线瓦片静态服务
│       └── socketio_events.py # WebSocket 事件
├── templates/              # HTML 模板（主页 / 历史 / 配置）
├── static/                 # 静态资源
│   ├── css/style.css      # 自定义样式（明暗主题 token）
│   ├── js/                # map / tasks / history / config / panels / ui
│   └── vendor/            # 本地第三方库（CesiumJS、Bootstrap、Socket.IO、字体）
├── scripts/                # 辅助脚本（发版推送、全球基础地形构建）
├── tests/                  # pytest 测试套件
├── docs/                   # 项目文档（guides/ 上手与构建、reference/ 实现说明、notes/ 调研笔记、reviews/ 评审记录、archive/ 历史归档、assets/ 图片资源）
├── downloads/              # 下载文件目录（运行时生成）
├── cache/                  # 瓦片缓存目录（运行时生成）
└── data/                   # SQLite 数据库（运行时生成）
```

## API 端点

### 瓦片任务（Google 地图下载）

- `POST /api/tasks` - 创建新任务
- `GET /api/tasks` - 获取所有任务
- `GET /api/tasks/<id>` - 获取任务详情
- `POST /api/tasks/<id>/start` - 启动任务
- `POST /api/tasks/<id>/pause` - 暂停任务
- `POST /api/tasks/<id>/resume` - 恢复任务
- `POST /api/tasks/<id>/cancel` - 取消任务（仅 pending/running/paused 可取消）
- `DELETE /api/tasks/<id>` - 删除任务（`?delete_files=true` 同时清理磁盘产物）

### DEM 任务（高程下载）

- `POST /api/dem/tasks` - 创建 DEM 任务
- `GET /api/dem/tasks` - 获取所有 DEM 任务
- `GET /api/dem/tasks/<id>` - 获取 DEM 任务详情
- `POST /api/dem/tasks/<id>/start` - 启动
- `POST /api/dem/tasks/<id>/pause` - 暂停
- `POST /api/dem/tasks/<id>/resume` - 恢复
- `POST /api/dem/tasks/<id>/cancel` - 取消（仅 pending/running/paused 可取消）
- `DELETE /api/dem/tasks/<id>` - 删除（`?delete_files=true` 同时清理磁盘产物；running 任务需先暂停或取消）

### 地形切片（Cesium quantized-mesh）

- `POST /api/terrain/dem/<id>/start` - 对已下载的 DEM 任务启动地形切片
- `GET /api/terrain/dem/<id>` - 查询切片任务状态
- `POST /api/terrain/local/tasks` - 上传 GeoTIFF 创建本地地形任务
- `GET /api/terrain/local/tasks` - 获取所有本地地形任务
- `GET /api/terrain/local/tasks/<id>` - 获取本地地形任务详情
- `POST /api/terrain/local/tasks/<id>/cancel` - 取消（仅 pending 可取消）
- `DELETE /api/terrain/local/tasks/<id>` - 删除（默认清理磁盘产物，`?delete_files=false` 保留）

### 等高线任务

- `GET /api/contour/style_preview` - 等高线样式预览
- `POST /api/contour/tasks` - 创建等高线任务
- `GET /api/contour/tasks` - 获取所有等高线任务
- `GET /api/contour/tasks/<id>` - 获取等高线任务详情
- `POST /api/contour/tasks/<id>/start` - 启动
- `POST /api/contour/tasks/<id>/pause` - 暂停
- `POST /api/contour/tasks/<id>/resume` - 恢复
- `POST /api/contour/tasks/<id>/cancel` - 取消（仅 pending/running/paused 可取消）
- `DELETE /api/contour/tasks/<id>` - 删除（`?delete_files=true` 同时清理磁盘产物；running 任务需先暂停或取消）

### 静态瓦片服务

- `GET /tiles/<task_id>/<path>` - 地图瓦片文件
- `GET /terrain/base/<path>` - 全球基础地形（base_z8）
- `GET /terrain/dem/<task_id>/<path>` - DEM 地形切片
- `GET /terrain/local/<task_id>/<path>` - 本地地形切片
- `GET /contour/<task_id>/<path>` - 等高线瓦片

### 历史记录

- `GET /api/history` - 获取历史记录（支持分页）
- `GET /api/history_all` - 获取全部历史记录
- `GET /api/history_stats` - 历史统计

### 配置管理

- `GET /api/config` - 获取所有配置
- `PUT /api/config` - 更新配置
- `POST /api/config/reset` - 重置为默认配置
- `POST /api/config/recommend_concurrency` - 实测当前网络吞吐，推荐并发数（约 30 秒）
- `POST /api/config/verify_tile_url` - 校验单个瓦片服务器条目的连通性

### 缓存管理

- `GET /api/cache/stats` - 按分类（各瓦片样式 / DEM 缓存）统计缓存占用与文件数
- `POST /api/cache/clear` - 手动清理某个缓存分类，`{"category": "__all__"}` 清理全部

### 目录浏览

- `GET /api/fs/browse?path=<绝对路径>` - 列出目录的非隐藏子目录（保存路径「浏览」弹窗的数据源；0.2.4 起全盘可浏览，Windows 根级返回盘符列表）

### WebSocket 事件

- `task_progress` - 实时任务进度更新（瓦片 / DEM / 等高线 / 本地地形）
- `task_completed` / `task_failed` - 任务完成 / 失败通知
- `task_stitch_progress` / `task_stitch_failed` / `task_copy_progress` - 瓦片拼接与文件复制进度

## 开发

### 运行测试

```bash
uv run pytest                                   # 全部测试
uv run pytest tests/test_config_manager.py      # 单个测试文件
```

### 代码组织约定

- 四条任务管线（瓦片 / DEM / 地形 / 等高线）均遵循 `routes/*_api.py`（HTTP 层）→ `services/*_task_manager.py`（状态与调度）→ `services/*_engine.py`（实际执行）的分层
- 共享的校验逻辑集中在 `src/services/geo_validation.py`，不要在各管线重复实现
- 任务取消约定：仅 `pending` / `running` / `paused` 状态可取消，取消永不改写终态；`DELETE` 接口通过 `?delete_files=true` 清理磁盘产物（带路径安全护栏）

### 更多文档

**[docs/README.md](docs/README.md) — 文档总索引**：各目录职责、哪些内容能当现状依据、按需求快速导航。不确定该看哪份时先看它。

- [docs/guides/BUILD.md](docs/guides/BUILD.md) — 构建详细说明
- [docs/guides/QUICKSTART.md](docs/guides/QUICKSTART.md) / [docs/guides/INSTALL.md](docs/guides/INSTALL.md) — 快速启动与安装指南
- docs/ 按用途分目录：[guides/](docs/guides/) 照着做的上手与构建文档（含面向最终用户的 `DISTRIBUTION.md`）、[reference/](docs/reference/) 当前实现说明、[notes/](docs/notes/) 调研与未实施计划、[reviews/](docs/reviews/) 带日期的时点审查、[archive/](docs/archive/) 历史归档（正文保留原貌，不再维护）
- [RELEASE_NOTES.md](RELEASE_NOTES.md) — 当前版本发版说明（作为 GitHub Release 正文发布）；[CHANGELOG.md](CHANGELOG.md) — 全版本更新历史
- [CLAUDE.md](CLAUDE.md) — 架构与开发约定（面向 AI 协作者，对人类开发者同样有参考价值）

## 性能设计

- **异步并发下载** — asyncio + aiohttp，并发数可配，可按实测网速推荐
- **多服务器轮询** — 自动在多个瓦片服务器间分散请求
- **共享瓦片缓存** — 缓存按 样式+坐标 键控、跨任务共享，重复选区 / 续跑零下载
- **边下边复制** — 瓦片落缓存即镜像到产物目录，下载结束 ≈ 产物就绪
- **断点续传** — 暂停 / 恢复 / 重试均不重下已有瓦片；拼接与复制均支持断点跳过
- **原子写入** — 瓦片 `.part` 临时文件 + rename 落盘，中断不产生坏缓存

## 构建可执行文件

```bash
# Linux/macOS
./build.sh

# Windows
build.bat
```

产物输出到 `dist/terraforge/`。详细说明（CI 构建、分发打包、Nuitka 配置）见 [docs/guides/BUILD.md](docs/guides/BUILD.md)。

## 故障排除

### GDAL 导入错误

`ImportError: No module named 'osgeo'` — GDAL 未正确安装：

```bash
# Ubuntu/Debian
sudo apt-get install gdal-bin libgdal-dev
uv pip install gdal==$(gdal-config --version)

# macOS
brew install gdal
uv pip install gdal==$(gdal-config --version)
```

`ImportError: cannot import name '_gdal_array' from 'osgeo'` — GDAL 绑定编译时缺少 numpy 支持，需强制从源码重建：

```bash
uv pip install numpy setuptools wheel
UV_NO_CACHE=1 uv pip install --force-reinstall --no-build-isolation --no-binary :all: "GDAL==$(gdal-config --version)"
```

### 数据库锁定错误

`database is locked` — 确保没有多个应用实例同时运行并访问同一个 `data/map_downloader.db`。

### 下载速度慢

- 在配置页使用「测速推荐」或手动提高并发数
- 检查网络连接，必要时配置代理服务器
- 确认瓦片服务器列表中的条目连通正常（配置页逐条「验证」）

## 注意事项

- Google Maps 服务条款可能禁止批量下载，**本工具仅供个人学习和研究使用**
- 大区域高缩放级别下载可能需要数小时甚至数天，并产生数 GB 数据，请确保磁盘空间充足
- 请合理设置并发数，避免对瓦片服务器造成过大压力
- 使用 ASTER GDEM v3 数据源需要有效的 NASA Earthdata Login 账号；默认的 Copernicus GLO-30 走公开 S3 桶，无需账号

## 许可证

MIT License

## 贡献

欢迎提交 Issue 和 Pull Request。

## 免责声明

本工具仅供学习和研究使用。使用者应遵守 Google Maps 服务条款和相关法律法规。作者不对使用本工具产生的任何后果负责。
