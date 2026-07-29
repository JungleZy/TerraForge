# TerraForge

基于 Web 的 GIS 数据获取与加工系统：Google Maps 瓦片下载、ASTER GDEM 高程数据获取、Cesium 3D 地形切片与等高线生成，支持交互式地图选区、实时进度监控、历史记录可视化和高级配置管理。

## 目录

- [功能特性](#功能特性)
- [技术栈](#技术栈)
- [快速开始](#快速开始)
- [使用](#使用)
- [项目结构](#项目结构)
- [API 端点](#api-端点)
- [开发](#开发)
- [性能优化](#性能优化)
- [注意事项](#注意事项)
- [构建可执行文件](#构建可执行文件)
- [故障排除](#故障排除)
- [许可证](#许可证)

## 功能特性

- 🗺️ 交互式地图界面选择下载区域
- 📊 实时下载进度监控（分级进度、速度、剩余时间）
- ⏸️ 任务调度（暂停/恢复/取消、断点续传）
- 📜 下载历史可视化（在地图上显示历史区域）
- ⚙️ 高级配置（并发数、缓存、服务器轮询）
- 🌐 局域网访问支持
- 🗜️ GDAL 地理配准和多格式输出

## 技术栈

- **后端:** Flask, Flask-SocketIO, aiohttp, GDAL, SQLite
- **前端:** CesiumJS 1.143, Bootstrap 5.3, Socket.IO 4.5.4（第三方库全部本地 vendor 于 `static/vendor/`，不依赖 CDN）

## 快速开始

### 方式一：使用预编译可执行文件（推荐）

无需安装 Python 和依赖，直接运行：

1. 从 [Releases](https://github.com/JungleZy/map-download/releases) 下载对应平台的压缩包
2. 解压文件
3. 运行可执行文件：
   - **Windows**: 双击 `terraforge.exe`
   - **macOS/Linux**: 运行 `./terraforge`
4. 浏览器访问 `http://localhost:5000`

详见 [DISTRIBUTION.md](docs/packaging/DISTRIBUTION.md)

### 方式二：从源码安装

#### 系统依赖

**Ubuntu/Debian:**
```bash
sudo apt-get update
sudo apt-get install -y gdal-bin libgdal-dev python3-gdal
```

**macOS:**
```bash
brew install gdal
```

#### Python 依赖

项目使用 [uv](https://docs.astral.sh/uv/) 管理虚拟环境：

```bash
uv venv                              # 如果 .venv 不存在
uv pip install -r requirements.txt
```

#### 数据库初始化

```bash
uv run python -c "from core.database import init_database; init_database()"
```

## 使用

### 启动应用

```bash
uv run python app.py
```

应用将在 `http://0.0.0.0:5000` 启动。

### 创建下载任务

1. 访问主页
2. 在地图上使用矩形工具框选下载区域
3. 设置缩放级别范围、地图样式和输出格式
4. 点击"创建下载任务"
5. 点击"启动"开始下载

### 地图样式选项

- **标准地图 (m)**: 标准道路地图
- **卫星图 (s)**: 纯卫星影像
- **卫星图+标注 (y)**: 卫星影像带道路标注
- **道路图 (h)**: 仅道路网络
- **地形图 (t)**: 地形等高线

### 输出格式

- **瓦片+拼接图**: 保存原始瓦片并生成拼接的 GeoTIFF
- **仅瓦片**: 只保存原始 PNG 瓦片
- **仅拼接图**: 只生成拼接的 GeoTIFF

### 查看历史

访问 `/history` 页面查看所有历史下载任务和区域可视化。

### 配置

访问 `/config` 页面修改系统配置：

- **基础设置**: 默认保存路径、地图样式、缩放级别
- **下载设置**: 并发数、超时时间、重试次数、代理服务器
- **缓存设置**: 启用/禁用缓存、缓存大小限制
- **GDAL 设置**: 压缩方式、重采样算法
- **其他设置**: 历史记录保留天数、地图初始位置

## 项目结构

```
map-download/
├── app.py                  # Flask 应用入口（组合根：注册蓝图、注入管理器）
├── core/                   # 应用基础设施
│   ├── config.py          # 配置类（路径、密钥、版本）
│   ├── database.py        # 数据库初始化与连接管理（含幂等迁移）
│   ├── startup_banner.py  # 启动信息横幅
│   └── process_watchdog.py # reloader 子进程看门狗
├── nuitka_build.py         # Nuitka 打包配置（GDAL/PROJ 环境设置在 core/bundle.py）
├── build.sh / build.bat    # 本地构建脚本
├── requirements.txt        # Python 依赖
├── models/                 # 数据模型
│   ├── task.py            # 任务/瓦片模型与枚举
│   └── config.py          # Config 模型
├── services/               # 业务逻辑
│   ├── download_engine.py  # Google 瓦片下载引擎
│   ├── task_manager.py     # 瓦片任务管理器
│   ├── config_manager.py   # 配置管理器
│   ├── dem_download_engine.py  # DEM 下载引擎（NASA ASTER GDEM）
│   ├── dem_task_manager.py     # DEM 任务管理器
│   ├── dem_granules.py         # ASTGTM 1°×1° 分幅工具
│   ├── earthdata_client.py     # NASA Earthdata Login 认证
│   ├── contour_engine.py       # 等高线生成引擎
│   ├── contour_task_manager.py # 等高线任务管理器
│   ├── contour_task_tiler.py   # 等高线瓦片切分
│   ├── local_terrain_task_manager.py  # 本地地形（上传 GeoTIFF）任务管理器
│   ├── terrain_tiling/         # Cesium quantized-mesh 地形切片
│   ├── geo_validation.py       # bbox / 缩放级别校验（三条管线共用）
│   ├── system_proxy.py         # 系统代理检测
│   └── task_cleanup.py         # 任务产物清理
├── routes/                 # Flask 路由
│   ├── main.py            # 页面路由
│   ├── api.py             # 瓦片任务 / 历史 / 配置 API
│   ├── dem_api.py         # DEM 任务 API
│   ├── terrain_api.py     # DEM 地形切片 API
│   ├── local_terrain_api.py  # 本地地形 API
│   ├── contour_api.py     # 等高线 API
│   ├── terrain_static.py  # 地形瓦片静态服务
│   ├── tiles_static.py    # 地图瓦片静态服务
│   ├── contour_static.py  # 等高线瓦片静态服务
│   └── socketio_events.py # WebSocket 事件
├── templates/              # HTML 模板
│   ├── base.html          # 基础模板
│   ├── index.html         # 主页
│   ├── history.html       # 历史记录页（含 _history_content.html 局部模板）
│   └── config.html        # 配置页（含 _config_content.html 局部模板）
├── static/                 # 静态资源
│   ├── css/style.css      # 自定义样式
│   ├── js/                # map / tasks / history / config / panels / ui
│   └── vendor/            # 本地第三方库（CesiumJS、Bootstrap、Socket.IO、字体）
├── migrations/             # 数据库迁移脚本
├── scripts/                # 辅助脚本（发版推送、全球基础地形构建）
├── tests/                  # pytest 测试套件
├── docs/                   # 项目文档（构建、设计、评审记录、packaging/ 发版资料）
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

### DEM 任务（ASTER GDEM 高程下载）

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

### WebSocket 事件

- `task_progress` - 实时任务进度更新（瓦片 / DEM / 等高线 / 本地地形）
- `task_completed` / `task_failed` - 任务完成 / 失败通知
- `task_stitch_progress` / `task_stitch_failed` / `task_copy_progress` - 瓦片拼接与文件复制进度

## 开发

### 运行测试

```bash
uv run pytest                 # 运行全部测试
uv run pytest tests/test_config_manager.py   # 运行单个测试文件
```

### 代码组织约定

- 四条任务管线（瓦片 / DEM / 地形 / 等高线）均遵循 `routes/*_api.py`（HTTP 层）→ `services/*_task_manager.py`（状态与调度）→ `services/*_engine.py`（实际执行）的分层
- 共享的校验逻辑集中在 `services/geo_validation.py`，不要在各管线重复实现
- 任务取消约定：仅 `pending` / `running` / `paused` 状态可取消；`DELETE` 接口通过 `?delete_files=true` 清理磁盘产物

### 更多文档

- [docs/BUILD.md](docs/BUILD.md) — 构建详细说明
- [docs/QUICKSTART.md](docs/QUICKSTART.md) / [docs/INSTALL.md](docs/INSTALL.md) — 快速启动与安装指南
- [docs/packaging/](docs/packaging/) — 打包与发版资料（分发说明、发布检查清单、历史记录）
- [RELEASE_NOTES.md](RELEASE_NOTES.md) — 发版说明
- [CLAUDE.md](CLAUDE.md) — 架构与开发约定（面向 AI 协作者，对人类开发者同样有参考价值）

## 性能优化

- **并发下载**: 支持多线程并发下载瓦片
- **智能缓存**: 已下载的瓦片自动缓存，避免重复下载
- **断点续传**: 任务可暂停和恢复，已下载的瓦片不会重新下载
- **服务器轮询**: 自动在多个瓦片服务器间轮询，提高下载速度
- **异步处理**: 使用 asyncio 和 aiohttp 实现高效异步下载

## 注意事项

- Google Maps 服务条款可能禁止批量下载
- 仅用于个人学习和研究目的
- 大区域高缩放级别下载可能需要数小时甚至数天
- 确保有足够的磁盘空间（高缩放级别可能产生数GB数据）
- 建议合理设置并发数，避免对服务器造成过大压力

## 构建可执行文件

如果你想自己构建跨平台可执行文件：

```bash
# Linux/macOS
./build.sh

# Windows
build.bat
```

详细构建说明请参考 [docs/BUILD.md](docs/BUILD.md)

## 故障排除

### GDAL 导入错误

如果遇到 `ImportError: No module named 'osgeo'`，请确保正确安装了 GDAL：

```bash
# Ubuntu/Debian
sudo apt-get install python3-gdal

# macOS
uv pip install gdal==$(gdal-config --version)
```

### 数据库锁定错误

如果遇到 `database is locked` 错误，请确保没有多个进程同时访问数据库。

### 下载速度慢

- 增加并发下载数（配置页面）
- 检查网络连接
- 考虑使用代理服务器

## 许可证

MIT License

## 贡献

欢迎提交 Issue 和 Pull Request。

## 免责声明

本工具仅供学习和研究使用。使用者应遵守 Google Maps 服务条款和相关法律法规。作者不对使用本工具产生的任何后果负责。
