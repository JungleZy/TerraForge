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
- ⏸️ 任务调度：暂停 / 恢复，断点续传，已下载瓦片不重复下载
- 🗂 下载历史可视化：历史区域叠加在地图上，已完成任务可直接预览瓦片 / 地形 / 晕渲效果
- 💾 保存路径全盘可选：任意绝对路径 + 目录浏览弹窗；删除任务可选是否清理磁盘产物（带安全护栏），运行中的任务也能直接删

### 平台能力

- 🎨 深色 / 浅色 / 跟随系统主题
- ⚙️ 完善的配置页：并发数（支持实测网速推荐）、代理（留空即自动检测可用代理）、缓存管理、GDAL 参数、Earthdata 账号等
- 🧹 缓存管理：按分类查看占用、手动清理，缓存不会被静默删除
- 🌐 局域网访问支持，适合内网部署
- 📦 Nuitka 打包为独立可执行文件，目标机器零依赖

## 技术栈

| 层 | 技术 |
| --- | --- |
| 后端 | Flask · Flask-SocketIO · aiohttp · GDAL · SQLite |
| 前端 | CesiumJS 1.143 · Bootstrap 5.3 · Socket.IO（第三方库全部本地 vendor 于 `static/vendor/`，不依赖 CDN） |
| 打包 | Nuitka（standalone，自动收集 GDAL/PROJ 数据与系统库闭包） |
| 测试 | pytest（API 契约、任务生命周期、路径安全，以及对 JS/CSS/模板的源码契约测试） |
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
# 1. 安装 GDAL 系统库（Ubuntu/Debian）
sudo apt-get install -y gdal-bin libgdal-dev

# 2. 安装 Python 依赖 —— 这四条的顺序不能调换
uv venv                                                          # .venv 不存在时
uv pip install setuptools wheel
uv pip install numpy==1.26.4
uv pip install --no-build-isolation "GDAL==$(gdal-config --version)"
uv pip install -r requirements.txt

# 3. 启动（数据库首次启动时自动初始化）
uv run python app.py
```

应用监听 `http://0.0.0.0:5000`。**Windows / Apple Silicon Mac 走 conda-forge 路线，不是上面这套**；顺序为什么不能换、装坏了怎么重建，全部见 [docs/guides/INSTALL.md](docs/guides/INSTALL.md)。

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
2. DEM 任务下载完成后，可对其启动「地形切片」，生成 Cesium quantized-mesh 地形 —— 入口有两个：任务详情弹窗里的「地形切片」，或「数据处理」弹窗把**处理类型**选「本地高程切片」、**数据来源**选「已下载的高程任务」（后者可以顺手改最大切片层级）
3. 已有 GeoTIFF 可直接上传为**本地地形任务**，跳过下载直接切片
4. 历史记录页可预览地形效果（无切片时按需渲染晕渲图）

### 等高线地图

1. 在「数据处理」弹窗把**处理类型**切到「等高线瓦片」，设置间距、配色、晕渲等样式（支持样式预览）
2. **数据来源**二选一：
   - 「上传文件」—— 直接上传高程文件（.tif/.tiff，可多选）
   - 「已下载的高程任务」—— 复用某个已完成 DEM 任务下载好的 DEM，不用再上传；源文件零拷贝，删除等高线任务不会动源 DEM
3. 产物以标准 XYZ 瓦片组织，可直接供 Leaflet / OpenLayers / CesiumJS 使用

### 历史记录

访问 `/history` 查看全部任务：统计概览、区域地图叠加、任务检索与预览。删除任务时通过 `delete_files` 选项控制是否同时清理磁盘产物。

### 配置

访问 `/config` 页面：

- **外观** — 深色 / 浅色 / 跟随系统
- **基础设置** — 默认保存路径（绝对路径，支持「浏览」）、默认样式与缩放级别
- **下载设置** — 并发数（「测速推荐」按当前网络实测给出建议值）、超时、重试、代理、瓦片服务器列表（逐条验证连通性）
  - **代理自动检测（默认开启）**：代理服务器一栏留空时，程序自己找可用代理 —— 环境变量与系统代理设置、Windows 的 PAC 自动配置脚本、本机（WSL 下含 Windows 宿主）上 Clash/v2rayN 等常见代理端口。每个候选都会用一张真实瓦片实测，通过了才采用；都不通就直连。手动填了代理地址就以手动值为准，自动检测不参与。配置页有「立即检测」按钮和当前状态显示。
  - WSL 下用宿主机上的代理，还需要在代理客户端开启「允许局域网连接」并放行 Windows 防火墙，否则 WSL 连不到宿主的代理端口（自动检测同样探不到）。
- **缓存设置** — 启用/禁用瓦片缓存；缓存管理按分类查看占用并手动清理（二次确认），缓存不会自动删除
- **GDAL 设置** — 压缩方式、重采样算法
- **其他设置** — 历史记录保留天数、地图初始位置
- **Earthdata 设置** — NASA Earthdata Login 账号（仅 ASTER GDEM v3 与水体掩膜数据需要；默认的 Copernicus GLO-30 免认证）

## 项目结构

按**目录**列，不按文件列：上一版是 2026-08-04 的逐文件快照，四天就漏掉了整个 `src/i18n/`、`src/app_factory.py` 和 `src/core/` 一半的文件——逐文件的树只会一直烂下去。要模块级的分工与调用关系，看 [CLAUDE.md](CLAUDE.md)。

```
map-download/
├── app.py                  # 入口：只排启动时序（进程守卫 → GDAL 环境 → 横幅 → create_app → run_server）
├── src/
│   ├── app_factory.py      # 唯一的组合根：create_app() 造四个管理器、注入蓝图、再注册蓝图
│   ├── core/               # 基础设施：配置、SQLite 与内联迁移、日志、单实例锁、打包与进程身份判定
│   ├── models/             # 任务 / 瓦片数据模型与状态枚举
│   ├── services/           # 业务逻辑：四条管线的 manager 与 engine、地形切片、配置 / 代理 / 清理等共享服务
│   ├── routes/             # Flask 蓝图：四组 REST API、三组静态瓦片服务、/basemap 转发、页面与 WebSocket
│   └── i18n/               # 界面语言（zh / en）：catalog/<domain>.py 消息表 + Jinja 与 JS 侧注入
├── templates/              # 服务端渲染模板（主页 / 历史 / 配置）
├── static/                 # CSS、JS，以及本地 vendor 的第三方库（CesiumJS / Bootstrap / Socket.IO / Vue / 字体，不依赖 CDN）
├── tests/                  # pytest 套件（conftest.py 提供隔离设施与沙箱）
├── scripts/                # 辅助脚本：GDAL 构建闸门 check_gdal.py、发版推送、全球基础地形构建
├── assets/terrain/         # 随包的全球基础地形分卷（base_z8.tar.gz.part{aa,ab}，167 MB）
├── docs/                   # 项目文档，分层与可信度见 docs/README.md
├── nuitka_build.py         # Nuitka 打包配置（GDAL/PROJ 环境设置在 src/core/bundle.py）
├── build.sh / build.bat    # 本地构建脚本（调用前先过 scripts/check_gdal.py）
├── requirements.txt        # Python 依赖
└── data/ downloads/ cache/ logs/   # 运行时生成：SQLite 库、下载产物、瓦片缓存、按天轮转的日志
```

## API 端点

### 页面

- `GET /` - 主页：地图选区、任务面板、数据处理弹窗
- `GET /history` - 历史记录页
- `GET /config` - 配置页

### 瓦片任务（Google 地图下载）

- `POST /api/tasks` - 创建新任务
- `GET /api/tasks` - 获取所有任务
- `GET /api/tasks/<id>` - 获取任务详情
- `POST /api/tasks/<id>/start` - 启动任务
- `POST /api/tasks/<id>/pause` - 暂停任务
- `POST /api/tasks/<id>/resume` - 恢复任务
- `DELETE /api/tasks/<id>` - 删除任务（`?delete_files=true` 同时清理磁盘产物）

### DEM 任务（高程下载）

- `POST /api/dem/tasks` - 创建 DEM 任务
- `GET /api/dem/tasks` - 获取所有 DEM 任务
- `GET /api/dem/tasks/<id>` - 获取 DEM 任务详情
- `POST /api/dem/tasks/<id>/start` - 启动
- `POST /api/dem/tasks/<id>/pause` - 暂停
- `POST /api/dem/tasks/<id>/resume` - 恢复
- `DELETE /api/dem/tasks/<id>` - 删除（`?delete_files=true` 同时清理磁盘产物）

### 地形切片（Cesium quantized-mesh）

- `POST /api/terrain/dem/<id>/start` - 对已下载的 DEM 任务启动地形切片（可选 `maxzoom` 覆盖配置默认层级，JSON 或表单均可）
- `GET /api/terrain/dem/<id>` - 查询切片任务状态
- `POST /api/terrain/local/tasks` - 上传 GeoTIFF 创建本地地形任务
- `GET /api/terrain/local/tasks` - 获取所有本地地形任务
- `GET /api/terrain/local/tasks/<id>` - 获取本地地形任务详情
- `DELETE /api/terrain/local/tasks/<id>` - 删除（默认清理磁盘产物，`?delete_files=false` 保留）

### 等高线任务

- `GET /api/contour/style_preview` - 等高线样式预览
- `POST /api/contour/tasks` - 创建等高线任务（multipart：`files` 上传 DEM，或 `dem_task_id` 复用某个已完成 DEM 任务的目录；二者互斥）
- `GET /api/contour/tasks` - 获取所有等高线任务
- `GET /api/contour/tasks/<id>` - 获取等高线任务详情
- `POST /api/contour/tasks/<id>/start` - 启动
- `POST /api/contour/tasks/<id>/pause` - 暂停
- `POST /api/contour/tasks/<id>/resume` - 恢复
- `DELETE /api/contour/tasks/<id>` - 删除（`?delete_files=true` 同时清理磁盘产物）

### 静态瓦片服务

- `GET /tiles/<task_id>/<path>` - 地图瓦片文件
- `GET /terrain/base/<path>` - 全球基础地形（base_z8）
- `GET /terrain/dem/<task_id>/<path>` - DEM 地形切片
- `GET /terrain/local/<task_id>/<path>` - 本地地形切片
- `GET /contour/<task_id>/<path>` - 等高线瓦片
- `GET /terrain/dem/<task_id>/hillshade` - DEM 任务源高程的晕渲预览元信息（PNG 地址 + 地理四至），没做地形切片时按需渲染
- `GET /terrain/dem/<task_id>/hillshade.png` - 上一条对应的 PNG 本体
- `GET /terrain/local/<task_id>/hillshade` - 本地地形任务上传文件的晕渲预览元信息
- `GET /terrain/local/<task_id>/hillshade.png` - 上一条对应的 PNG 本体

### 底图

- `GET /basemap/<z>/<x>/<y>` - 底图瓦片的**同源转发，这一跳是强制的**：浏览器只拿得到这条路径，真实上游地址不出服务端。直连上游会被 CORS 把真实状态码埋成一句 CORS 报错，而且浏览器不吃配置里的 `proxy_url` —— 底图和下载会走成两条出网路径，代理配好了底图仍然是个蓝球
- 取不到瓦片时会**自动回退**到链上的下一张（Esri 卫星 → Google 卫星 → OpenStreetMap 路网），换了会在界面上说一句。链里只放 WGS-84 的源：底图是用来框选下载范围的，静默换上一张 GCJ-02 的图等于让人框错地方。Google 路网（`lyrs=m`）因此不在链里——它中国区是 GCJ-02，而且与 Google 卫星同主机，卫星取不到时它也取不到
- `GET /api/basemap` - 底图图层描述符（同源 url、最大层级、署名、源标识）。`/history` 独立页取它；首页由模板内联下发，不走这个接口

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
- `GET|POST /api/config/proxy_status` - 代理自动检测：GET 读当前状态，POST 强制重新探测（同步执行，最坏二十几秒）

### 缓存管理

- `GET /api/cache/stats` - 按分类（各瓦片样式 / DEM 缓存）统计缓存占用与文件数
- `POST /api/cache/clear` - 手动清理某个缓存分类，`{"category": "__all__"}` 清理全部

### 目录浏览

- `GET /api/fs/browse?path=<绝对路径>` - 列出目录的非隐藏子目录（保存路径「浏览」弹窗的数据源；0.2.4 起全盘可浏览，Windows 根级返回盘符列表）

### 栅格头部探测

- `POST /api/raster/inspect` - 解释浏览器读出的 GeoTIFF 头部标签，返回坐标系、WGS84 范围、分辨率、数据类型与建议最大层级。**不接收文件本身**：前端 `static/js/geotiff_meta.js` 用 `File.slice` 只读几 KB 的 IFD，几百 MB 的 DEM 不会为看一眼元信息先整包上传。Body `{"files": [...], "mode": "terrain"|"contour"}`，`mode` 决定建议层级按哪条切片管线算

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
- 任务删除约定：四条管线的 `DELETE` 在**任何状态**下都受理，没有前置的停止动作。任务没在跑就同步删完；在跑就置停止标志、行立即消失，产物清理留给后台线程收尾。产物清理由 `?delete_files` 控制（带路径安全护栏）；**只有「在跑 + 要求删产物」这一种组合**才会在响应里带 `files_deferred: true`，此时不下发 `files_removed` / `files_message`，其余情况该字段根本不出现。行不存在一律 404

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

### 查看日志

运行日志落在程序目录下的 `logs/terraforge.log`（打包版本是可执行文件旁边），每天零点轮转一次，旧文件叫 `terraforge.log.2026-08-07`，保留 7 天。

**控制台与日志文件的内容故意不一样**：控制台不打印**成功**的瓦片请求（浏览地图一次就是几十上百条 `GET /basemap/3/4/4 200`，会把有用的信息顶掉），日志文件全都留着。失败的瓦片请求（403 / 404 / 504）两边都打 —— 底图变蓝球、地形不显示时，那一行往往是唯一的线索。

- 想让控制台也打印瓦片请求：用 `LOG_LEVEL=DEBUG` 启动。
- `LOG_LEVEL` 可取 `CRITICAL/ERROR/WARNING/INFO/DEBUG`，默认 `INFO`；填错会警告并回退默认值，不会启动失败。
- 日志目录不可写时（装在只读目录）会打一条警告后继续运行，只是没有落盘。

### GDAL 导入错误

`ImportError: No module named 'osgeo'`（绑定没装）与 `ImportError: cannot import name '_gdal_array' from 'osgeo'`（绑定装了但编译时没看到 numpy，拼接 / 切片 / 等高线全炸）都在 [docs/guides/INSTALL.md](docs/guides/INSTALL.md) 里处理：前者见「2. 克隆代码并安装 Python 依赖」，后者见「故障排除」。**别在这里凭记忆敲一条 `uv pip install gdal==...`** —— 不带 `--no-build-isolation` 装出来的正是第二种坏绑定。

### 数据库锁定错误

`database is locked` — 确保没有多个应用实例同时运行并访问同一个 `data/map_downloader.db`。

### 下载速度慢

- 在配置页使用「测速推荐」或手动提高并发数
- 检查网络连接。代理默认自动检测（配置页「立即检测」可看结果），检测不到时再手动填写代理地址
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
