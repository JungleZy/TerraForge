# TerraForge

**中文** · [English](README.en.md)

[![Website](https://img.shields.io/badge/%E5%AE%98%E7%BD%91-terraforge--gis.pages.dev-38bdf8)](https://terraforge-gis.pages.dev/)
[![Release](https://img.shields.io/github/v/release/JungleZy/TerraForge)](https://github.com/JungleZy/TerraForge/releases)
[![Build](https://github.com/JungleZy/TerraForge/actions/workflows/test-build.yml/badge.svg)](https://github.com/JungleZy/TerraForge/actions/workflows/test-build.yml)
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

1. 从 [Releases](https://github.com/JungleZy/TerraForge/releases) 下载对应平台的压缩包
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

按**目录**列，不按文件列：上一版是 2026-08-04 的逐文件快照，四天就漏掉了整个 `src/i18n/`、`src/app_factory.py` 和 `src/core/` 一半的文件——逐文件的树只会一直烂下去。唯一的例外是 `src/contracts/`：它是**封闭的一小组合同**（六个文件，就是全部），逐个列出来才说得清「哪条规则的事实源在哪」，而这正是加它的意义。要其余部分模块级的分工与调用关系，看 [CLAUDE.md](CLAUDE.md)。

```
map-download/
├── app.py                  # 入口：只排启动时序（进程守卫 → GDAL 环境 → 横幅 → create_app → run_server）
├── src/
│   ├── app_factory.py      # 唯一的组合根：create_app() 造四个管理器、注入蓝图、再注册蓝图
│   ├── contracts/          # 四条管线共用的合同层，零 Flask / GDAL / SQLite 依赖；每一条都是**唯一事实源**
│   │   ├── region.py           # RegionSpec：区域的唯一表示（矩形 / 多边形 / 多部件 / 孔洞 / 跨反经线拆分）
│   │   ├── region_tiles.py     # 全仓唯一一处经纬度↔瓦片换算：估算、下载、拼接、MBTiles 四至、界面预览共用
│   │   ├── source.py           # SourceSnapshot：冻结下载源身份，产出 8 位指纹与缓存命名空间（凭据不入摘要）
│   │   ├── outcome.py          # TileOutcome 五分类 + 任务状态词表（SQL 一律用其中的**正列表**）
│   │   ├── artifact.py         # Artifact / ArtifactKind 与 PIPELINES 元组
│   │   └── reservation.py      # ResourceKind / ResourceRequest / ResourceReservation（上下文管理器，退出即释放）
│   ├── core/               # 基础设施：配置、SQLite 与内联迁移、日志、单实例锁、打包与进程身份判定
│   ├── models/             # 任务 / 瓦片数据模型与状态枚举
│   ├── services/           # 业务逻辑：四条管线的 manager 与 engine、地形切片、配置 / 代理 / 清理等共享服务；0.3.3 起还有全局调度（resource_scheduler）、磁盘预算（disk_budget）、每任务日志（task_logging）、缓存治理（cache_exclusive、source_registry）、MBTiles 写入与打包（mbtiles、artifact_export、artifact_store）、区域与图源与地名（region_import、source_wizard、geocoding、url_guard）
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

- `POST /api/tasks` - 创建新任务。可选 `export_mbtiles`（真值 = 跑完后额外打一个 `.mbtiles`，与 `output_format` 正交，见「成果导出」）
- `GET /api/tasks` - 获取所有任务
- `GET /api/tasks/<id>` - 获取任务详情
- `POST /api/tasks/<id>/start` - 启动任务
- `POST /api/tasks/<id>/pause` - 暂停任务
- `POST /api/tasks/<id>/resume` - 恢复任务
- `DELETE /api/tasks/<id>` - 删除任务（`?delete_files=true` 同时清理磁盘产物；`?clear_cache=1` 另外清掉**只被这个任务引用**的共享缓存瓦片 —— 与别的任务重叠的那部分一张不动）。带 `clear_cache` 时响应多三个字段：`cache_removed_bytes` / `cache_removed_files`，以及 `cache_deferred`（任务还在跑，清理推迟到工作线程退出后）
- `GET /api/tasks/<id>/gaps` - 缺块摘要：总数、按 `TileOutcome` 分类计数、`explained`（是否**只有** `no_data` 这一类）、当前决策与最多 20 个样例格子
- `POST /api/tasks/<id>/refill` - 补漏：只重跑记录在案、且 outcome 属于可重试类的那些格子。允许的起始状态是 `completed_with_gaps` / `pending_decision` / `failed`；幂等，重复点不会重复下载
- `POST /api/tasks/<id>/accept_gaps` - 显式接受缺块：`pending_decision` → `completed_with_gaps`，并补跑严格模式此前拒绝执行的拼接 / 复制阶段。成果与历史**永久带缺块标记**，不会被当成完整成品
- `GET /api/tasks/<id>/artifacts?pipeline=<管线>` - 这个任务已登记的产物清单（XYZ 目录 / GeoTIFF / MBTiles 等，一个任务可以同时有多种）。`pipeline` 缺省为 `map`

### DEM 任务（高程下载）

- `POST /api/dem/tasks` - 创建 DEM 任务。区域两种写法二选一：老的 `north`/`south`/`east`/`west` 四至，或新的 `region`（一个 `RegionSpec`）。**给了 `region` 时四至变成可选** —— 而且跨 180° 经线的 DEM 任务**只能**用 `region` 建：裸四至那条路对 `east <= west` 一律 400，那道校验是有意保留的
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
- `DELETE /api/terrain/local/tasks/<id>` - 删除（`?delete_files=true` 同时清理磁盘产物）。**0.3.3 起默认改为不删**：这条此前是四条管线里唯一默认连文件一起删的，现在四条一致。界面不受影响 —— 删除对话框一直显式带着这个参数

### 等高线任务

- `GET /api/contour/style_preview` - 等高线样式预览
- `POST /api/contour/tasks` - 创建等高线任务（multipart：`files` 上传 DEM，或 `dem_task_id` 复用某个已完成 DEM 任务的目录；二者互斥）
- `GET /api/contour/tasks` - 获取所有等高线任务
- `GET /api/contour/tasks/<id>` - 获取等高线任务详情
- `POST /api/contour/tasks/<id>/start` - 启动
- `POST /api/contour/tasks/<id>/pause` - 暂停
- `POST /api/contour/tasks/<id>/resume` - 恢复
- `DELETE /api/contour/tasks/<id>` - 删除（`?delete_files=true` 同时清理磁盘产物）

### 插件

- `GET /api/plugins` - 插件列表。**加载失败的插件也在列表里**，带 `load_error` —— 坏插件不许打穿宿主，但必须在界面上看得见
- `POST /api/plugins/<id>/enable` - 启用插件
- `POST /api/plugins/<id>/disable` - 禁用插件
- `GET|PUT /api/plugins/<id>/config` - 读 / 写插件配置。写入先过插件自己声明的 `config_schema()`，不合法回 400 与逐键的 `errors`
- `GET /api/plugins/sources` - 全部**已启用**插件提供的数据源。凭据只出键名不出值
- `GET /api/plugins/<id>/schema` - 声明式任务表单的参数 schema（`key`/`type`/`label`/`default`/`required`/`min`/`max`/`choices`），是前端渲染表单的唯一数据源。没有管线能力或插件被禁用时回空数组，不是 404
- `POST /api/plugins/<id>/tasks` - 创建插件任务。Body 含 `bbox`（`[north, south, east, west]`）/ `output_path` / `name` / `auto_start`，其余键交给插件 schema 校验。插件不可用回 404，参数非法回 400。`auto_start` 为真时响应多两个字段：`started`，以及启动失败时的 `start_error` —— 任务**已经建好了**，只是没起来（插件正好在这两步之间被禁用就是这条路），所以不判整个请求失败
- `GET /api/plugins/tasks?active=1` - 插件任务列表，`active=1` 只要进行中的
- `GET /api/plugins/tasks/<id>` - 插件任务详情
- `POST /api/plugins/tasks/<id>/start` - 启动插件任务。幂等；插件任务没有断点续跑，一次 start 就是完整重跑一遍
- `GET /api/plugins/tasks/<id>/gaps` - 缺块摘要，载荷与瓦片管线的 `/gaps` 逐键同形：`total`、按 `TileOutcome` 分类计数的 `by_outcome`（四个键恒存，没有的给 0）、`explained`（是否**只有** `no_data`——为真时不该再问用户补漏还是接受）、`decision`、`status` 与最多 20 个样例格子
- `POST /api/plugins/tasks/<id>/accept-gaps` - 接受缺块并重跑收尾。成果与历史**永久带缺块标记**
- `DELETE /api/plugins/tasks/<id>` - 删除插件任务（`?delete_files=1` 同时清理磁盘产物）。不带该参数时产物目录会被登记进 `retained_outputs`，响应里多一个 `files_retained_path` —— 用户选择保留文件，一个字节都不动，但那个目录必须留下一条 DB 引用
- `POST /api/plugins/export/<id>` - 按格式导出插件任务产物，Body `{"format": "gpkg"}`。格式来自已启用插件注册的 Exporter；未知格式回 400 并给出 `supported_formats`
- `GET /api/plugins/<id>/assets/<path>` - 插件 UI 资产。两道门：路径 `resolve()` 后必须仍在插件目录内，且必须在 `plugin.toml` 的 `ui.assets` 白名单里声明（目录里的 `plugin.py`、`vendor/` 一律出不去）

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
- `GET /mbtiles/<pipeline>/<id>/<z>/<x>/<y>.<ext>` - 从已导出的 MBTiles 库里读一张瓦片。**影像、等高线与将来的矢量共用这一条路由** —— 刻意不按数据类型各开一条（`docs/notes/external-projects-takeaways.md` §5.3 明确禁止）。`<pipeline>` 取值来自 `src/contracts/artifact.py` 的 `PIPELINES`；库里存的是 TMS 行号，这条路由收 XYZ 并在内部翻转

### 成果导出

- `POST /api/export/<pipeline>/<id>` - 把任务的瓦片金字塔打包成单个 `.mbtiles`，Body `{"format": "mbtiles"}`。**这是「多一份产物」，不是换一种输出格式**：`output_format` 一个取值都没加，原来的瓦片目录一张不动（它正是打包的原料，也是 `/tiles/<id>/` 预览的数据源）。幂等。`<pipeline>` 对着 `PIPELINES` 校验，打包器不支持的管线（`dem` / `local_terrain` 没有瓦片金字塔）回 400 并在 body 里给出 `supported_pipelines`。建任务时勾选 `export_mbtiles` 可以让它在跑完后自动执行一次

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
- `POST /api/config/analyze_tile_url` - 图源向导：粘一条瓦片服务地址，认出模板形态（`{z}/{x}/{y}` 占位符、`{s}` 子域列表、行号方案、查询参数）并列出可疑之处。Body `{"url": "..."}`

### 缓存管理

- `GET /api/cache/stats` - 按分类（各瓦片样式 / DEM 缓存）统计缓存占用与文件数
- `POST /api/cache/clear` - 手动清理某个缓存分类，`{"category": "__all__"}` 清理全部
- `GET /api/cache/namespaces` - 按**来源命名空间**（`<样式码>-<源指纹>`）统计缓存占用。与上面那条按分类统计的区别：同一个样式换过服务器就会有多个命名空间，只有这条分得开
- `POST /api/cache/sweep_orphans` - 清掉没有任何现存任务认领的孤儿命名空间。在用的一个不动

### 目录浏览

- `GET /api/fs/browse?path=<绝对路径>` - 列出目录的非隐藏子目录（保存路径「浏览」弹窗的数据源；0.2.4 起全盘可浏览，Windows 根级返回盘符列表）

### 栅格头部探测

- `POST /api/raster/inspect` - 解释浏览器读出的 GeoTIFF 头部标签，返回坐标系、WGS84 范围、分辨率、数据类型与建议最大层级。**不接收文件本身**：前端 `static/js/geotiff_meta.js` 用 `File.slice` 只读几 KB 的 IFD，几百 MB 的 DEM 不会为看一眼元信息先整包上传。Body `{"files": [...], "mode": "terrain"|"contour"}`，`mode` 决定建议层级按哪条切片管线算

### 区域与地点

- `POST /api/region/import` - multipart 上传一个区域文件（`file` 字段，GeoJSON / KML / KMZ / Shapefile 的 .zip），解析成 `RegionSpec`。返回 `{region, summary, warnings}`。支持多边形、多部件与孔洞；坐标系必须是 WGS-84 经纬度
- `POST /api/region/estimate` - 对一个区域 + 层级范围估算瓦片数与磁盘占用，并给出磁盘预算裁决。Body 取 `region` 或 `bbox`，加 `zoom_min` / `zoom_max` / `style` / `output_format` / `output_path`。返回 `{tile_count, estimate, verdict}`
- `GET /api/places/search?q=<关键词>&limit=<条数>` - 地名搜索。**未配置 `geocoder_url` 时返回 HTTP 200 与 `{"enabled": false, "results": []}`，不是错误** —— 程序不内置任何地名服务（理由见 `docs/notes/external-projects-takeaways.md` §11 与 §13 末尾的待决清单），界面据此把这一栏藏起来而不是显示成坏掉的功能

### 任务日志与诊断

- `GET /api/logs/<pipeline>/<id>?limit=<行数>&errors_only=<bool>` - 读某个任务自己的日志。`<pipeline>` 取值来自 `PIPELINES`；凭据在**落盘前**就被抹掉了
- `GET /api/logs/<pipeline>/<id>/diagnostics` - 同一份日志的 `text/plain` 附件，设计上就是可以直接贴进 issue 的

**为什么日志不走 WebSocket**：本应用没有 room / namespace，任何一次 emit 都发给所有连着的客户端 —— 逐行日志事件等于把一个任务的日志广播给每一个打开着页面的人。日志尾随因此只走上面这两条 REST，由前端轮询。

### 运行状态

- `GET /api/scheduler/status` - 全局资源调度器快照：各类资源（网络连接 / CPU / GDAL 槽位 / 磁盘字节）的上限与当前占用、等待中的任务。排查「为什么第三个任务不开始跑」看这条

### WebSocket 事件

- `task_progress` - 实时任务进度更新（瓦片 / DEM / 等高线 / 本地地形）
- `task_completed` / `task_failed` - 任务完成 / 失败通知
- `task_stitch_progress` / `task_stitch_failed` / `task_copy_progress` - 瓦片拼接与文件复制进度
- `task_gap_decision` - 任务进入「待决策」时、以及决策被应用时各发一次。载荷 `{task_id, task_type, status, gap_tiles, by_outcome}`

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

本项目采用 **MIT**，全文见 [LICENSE](LICENSE)。

随本项目分发的第三方组件（CesiumJS、Bootstrap、Vue、Socket.IO、Inter / JetBrains Mono 字体、随包的 GEBCO 2024 派生地形数据，以及二进制发行时的 Python 依赖与原生库）各有其许可证与署名义务，逐条列在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

⚠️ MIT 只覆盖本项目的**软件代码**，**不授予**任何数据与在线服务的使用权。Google、Esri、天地图、OSM、Cesium Ion 等图源的 attribution、批量下载政策、Token 与配额需各自独立处理，见上方免责声明。

## 贡献

欢迎提交 Issue 和 Pull Request。

## 免责声明

本工具仅供学习和研究使用。使用者应遵守 Google Maps 服务条款和相关法律法规。作者不对使用本工具产生的任何后果负责。
