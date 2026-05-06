# Google Maps 下载器设计文档

## 1. 项目概述

基于 Web 的 Google Maps 瓦片下载器，支持通过交互式地图选择区域、实时下载进度监控、历史记录可视化和高级配置管理。

### 核心功能
- 交互式地图界面选择下载区域（左上角+右下角坐标）
- 支持多种地图样式（标准、卫星、混合等）
- 缩放级别范围下载（如 10-15 级）
- 实时下载进度监控（分级进度、速度、剩余时间）
- 任务调度（暂停/恢复/取消、断点续传）
- 下载历史可视化（在地图上显示历史区域）
- 高级配置（并发数、缓存、服务器轮询）
- 局域网访问支持

## 2. 技术架构

### 2.1 技术栈

**后端：**
- Flask 2.3+ - Web 框架
- Flask-SocketIO 5.3+ - WebSocket 实时通信
- aiohttp 3.9+ - 异步 HTTP 客户端
- GDAL 3.6+ - 地理空间数据处理（坐标转换、瓦片拼接、格式转换）
- Pillow 10.0+ - 辅助图像处理
- SQLite 3 - 数据持久化

**前端：**
- Leaflet.js 1.9+ - 交互式地图
- Bootstrap 5.3+ - UI 框架
- Socket.IO Client - 实时通信
- Chart.js - 进度可视化（可选）

**Python 版本：** 3.9+

### 2.2 系统架构

```
┌──────────────────────────────────────────────────┐
│              Web Browser (前端)                   │
│  ┌────────────┐  ┌──────────┐  ┌──────────────┐ │
│  │ Leaflet Map│  │ Task UI  │  │ History View │ │
│  └────────────┘  └──────────┘  └──────────────┘ │
└──────────────────────────────────────────────────┘
                      ↕ HTTP/WebSocket
┌──────────────────────────────────────────────────┐
│              Flask Application                    │
│  ┌────────────┐  ┌──────────┐  ┌──────────────┐ │
│  │   Routes   │  │ SocketIO │  │   API        │ │
│  └────────────┘  └──────────┘  └──────────────┘ │
└──────────────────────────────────────────────────┘
                      ↕
┌──────────────────────────────────────────────────┐
│           Business Logic Layer                    │
│  ┌────────────┐  ┌──────────┐  ┌──────────────┐ │
│  │Task Manager│  │DownloadEng│  │Config Manager│ │
│  └────────────┘  └──────────┘  └──────────────┘ │
└──────────────────────────────────────────────────┘
                      ↕
┌──────────────────────────────────────────────────┐
│              Data Layer                           │
│  ┌────────────┐  ┌──────────┐  ┌──────────────┐ │
│  │  SQLite DB │  │File System│  │  Tile Cache  │ │
│  └────────────┘  └──────────┘  └──────────────┘ │
└──────────────────────────────────────────────────┘
```

## 3. 核心组件设计

### 3.1 数据库设计

**tasks 表（下载任务）：**
```sql
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    status TEXT NOT NULL,  -- pending, running, paused, completed, failed, cancelled
    north REAL NOT NULL,
    south REAL NOT NULL,
    east REAL NOT NULL,
    west REAL NOT NULL,
    zoom_min INTEGER NOT NULL,
    zoom_max INTEGER NOT NULL,
    style TEXT NOT NULL,  -- m, s, y, h, etc.
    output_format TEXT NOT NULL,  -- image_only, tiles_only, both
    output_path TEXT NOT NULL,
    total_tiles INTEGER DEFAULT 0,
    downloaded_tiles INTEGER DEFAULT 0,
    failed_tiles INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    error_message TEXT
);
```

**task_tiles 表（瓦片下载状态，用于断点续传）：**
```sql
CREATE TABLE task_tiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    zoom INTEGER NOT NULL,
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    status TEXT NOT NULL,  -- pending, downloading, completed, failed
    retry_count INTEGER DEFAULT 0,
    error_message TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    UNIQUE(task_id, zoom, x, y)
);
```

**config 表（配置项）：**
```sql
CREATE TABLE config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**默认配置项：**
- `default_save_path`: 默认保存路径
- `default_style`: 默认地图样式（m）
- `default_zoom_min`: 默认最小缩放级别（10）
- `default_zoom_max`: 默认最大缩放级别（15）
- `default_output_format`: 默认输出格式（GeoTIFF）
- `concurrent_downloads`: 并发下载数（10）
- `request_timeout`: 请求超时时间（30秒）
- `max_retries`: 最大重试次数（3）
- `proxy_url`: 代理服务器地址（可选）
- `tile_servers`: 瓦片服务器列表（mts0,mts1,mts2,mts3）
- `cache_enabled`: 是否启用缓存（true）
- `cache_max_size_mb`: 缓存最大大小（1000MB）
- `history_retention_days`: 历史记录保留天数（90）
- `map_center_lat`: 地图初始中心纬度（39.9）
- `map_center_lng`: 地图初始中心经度（116.4）
- `map_initial_zoom`: 地图初始缩放级别（10）
- `gdal_compression`: GDAL 压缩方式（LZW）
- `gdal_resampling`: GDAL 重采样算法（cubic）

### 3.2 下载引擎（DownloadEngine）

**职责：**
- 计算给定区域和缩放级别的所有瓦片坐标
- 异步并发下载瓦片
- 服务器轮询（mts0/mts1/mts2/mts3）
- 失败重试机制
- 瓦片缓存管理
- 使用 GDAL 进行图像拼接和地理配准

**核心方法：**
```python
class DownloadEngine:
    def calculate_tiles(self, north, south, east, west, zoom_min, zoom_max) -> List[Tile]
    async def download_tile(self, tile: Tile, style: str) -> bytes
    async def download_tiles_batch(self, tiles: List[Tile], task_id: int, style: str)
    def stitch_tiles_with_gdal(self, tiles: List[Tile], output_path: str, output_format: str)
    def get_tile_url(self, x: int, y: int, z: int, style: str, server_index: int) -> str
    def manage_cache(self)
```

**瓦片坐标计算：**
使用 Web Mercator 投影（EPSG:3857）将经纬度转换为瓦片坐标：
```python
def lat_lon_to_tile(lat, lon, zoom):
    n = 2 ** zoom
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
    return x, y
```

**URL 模板：**
```
http://mts{0-3}.googleapis.com/vt?lyrs={style}&x={x}&y={y}&z={z}
```

**缓存策略：**
- 缓存路径：`./cache/{style}/{z}/{x}/{y}.png`
- LRU 淘汰策略
- 定期清理超过保留期限的缓存

**GDAL 集成优势：**
- **精确的坐标转换：** 使用 GDAL 的坐标转换功能，支持多种投影系统
- **高效的瓦片拼接：** 使用 `gdal.Warp()` 或 `gdal_merge.py` 拼接瓦片，比 Pillow 更快且支持大图
- **地理配准：** 自动为输出图像添加地理参考信息（GeoTIFF）
- **多格式支持：** 输出 GeoTIFF、PNG、JPEG、MBTiles 等多种格式
- **内存优化：** GDAL 的流式处理可以处理超大图像而不会内存溢出

**GDAL 拼接实现示例：**
```python
def stitch_tiles_with_gdal(self, tiles, output_path, zoom_level):
    # 创建 VRT（虚拟数据集）
    vrt_options = gdal.BuildVRTOptions(resampleAlg='cubic')
    vrt = gdal.BuildVRT('temp.vrt', [tile.path for tile in tiles], options=vrt_options)
    
    # 转换为最终格式（GeoTIFF 或 PNG）
    translate_options = gdal.TranslateOptions(
        format='GTiff',
        creationOptions=['COMPRESS=LZW', 'TILED=YES']
    )
    gdal.Translate(output_path, vrt, options=translate_options)
    vrt = None  # 关闭数据集
```

### 3.3 任务管理器（TaskManager）

**职责：**
- 任务创建、启动、暂停、恢复、取消
- 任务队列管理（支持并行多任务）
- 断点续传支持
- 进度跟踪和统计
- 通过 SocketIO 推送实时进度

**核心方法：**
```python
class TaskManager:
    def create_task(self, params: dict) -> int
    def start_task(self, task_id: int)
    def pause_task(self, task_id: int)
    def resume_task(self, task_id: int)
    def cancel_task(self, task_id: int)
    def get_task_status(self, task_id: int) -> dict
    def get_active_tasks(self) -> List[dict]
    def cleanup_old_tasks(self)
```

**任务状态机：**
```
pending → running → completed
    ↓         ↓         
    ↓      paused → running
    ↓         ↓
    ↓      cancelled
    ↓
  failed
```

**断点续传实现：**
1. 任务启动时，检查 `task_tiles` 表中未完成的瓦片
2. 只下载状态为 `pending` 或 `failed`（重试次数未超限）的瓦片
3. 每下载完一个瓦片，更新 `task_tiles` 状态
4. 暂停时，保留所有瓦片状态
5. 恢复时，从上次中断处继续

### 3.4 配置管理器（ConfigManager）

**职责：**
- 读取/更新配置项
- 配置验证
- 默认值管理

**核心方法：**
```python
class ConfigManager:
    def get(self, key: str, default=None) -> Any
    def set(self, key: str, value: Any)
    def get_all(self) -> dict
    def reset_to_defaults(self)
    def validate_config(self, key: str, value: Any) -> bool
```

### 3.5 Flask 路由设计

**页面路由：**
- `GET /` - 主页（地图界面）
- `GET /history` - 历史记录页面
- `GET /config` - 配置页面

**API 路由：**
- `POST /api/tasks` - 创建下载任务
- `GET /api/tasks` - 获取所有任务列表
- `GET /api/tasks/<id>` - 获取任务详情
- `POST /api/tasks/<id>/start` - 启动任务
- `POST /api/tasks/<id>/pause` - 暂停任务
- `POST /api/tasks/<id>/resume` - 恢复任务
- `POST /api/tasks/<id>/cancel` - 取消任务
- `DELETE /api/tasks/<id>` - 删除任务
- `GET /api/history` - 获取历史记录（带分页）
- `GET /api/config` - 获取所有配置
- `PUT /api/config` - 更新配置

**SocketIO 事件：**
- `connect` - 客户端连接
- `disconnect` - 客户端断开
- `task_progress` - 服务器推送任务进度（自动）
  ```json
  {
    "task_id": 1,
    "status": "running",
    "total_tiles": 1000,
    "downloaded_tiles": 450,
    "failed_tiles": 2,
    "current_zoom": 12,
    "zoom_progress": {
      "10": {"total": 100, "downloaded": 100},
      "11": {"total": 400, "downloaded": 350},
      "12": {"total": 500, "downloaded": 0}
    },
    "download_speed": "2.5 MB/s",
    "eta_seconds": 120,
    "current_tile": {"x": 123, "y": 456, "z": 12}
  }
  ```

### 3.6 前端界面设计

**主页（地图界面）：**
- 左侧：Leaflet 地图，支持矩形框选工具
- 右侧面板：
  - 下载参数表单（缩放级别范围、地图样式、输出格式）
  - 活动任务列表（显示进度）
  - 开始下载按钮

**任务进度面板（展开式）：**
- 任务名称和状态
- 总体进度条（百分比 + 已下载/总瓦片数）
- 分级进度（每个缩放级别的进度条）
- 实时统计：
  - 下载速度（MB/s）
  - 预计剩余时间
  - 当前正在下载的瓦片坐标
  - 失败瓦片数
- 操作按钮：暂停/恢复、取消

**历史记录页面：**
- 表格显示历史任务
- 地图视图：显示所有历史下载区域的边界框
- 筛选和搜索功能

**配置页面：**
- 分组配置表单
- 保存和重置按钮

## 4. 项目结构

```
map-download/
├── app.py
├── requirements.txt
├── config.py
├── database.py
├── models/
│   ├── __init__.py
│   ├── task.py
│   └── config.py
├── services/
│   ├── __init__.py
│   ├── download_engine.py
│   ├── task_manager.py
│   └── config_manager.py
├── routes/
│   ├── __init__.py
│   ├── main.py
│   ├── api.py
│   └── socketio_events.py
├── static/
│   ├── css/
│   │   └── style.css
│   ├── js/
│   │   ├── map.js
│   │   ├── tasks.js
│   │   ├── history.js
│   │   └── config.js
│   └── images/
├── templates/
│   ├── base.html
│   ├── index.html
│   ├── history.html
│   └── config.html
├── downloads/
├── cache/
└── data/
    └── map_downloader.db
```

## 5. 关键流程

### 5.1 创建下载任务
1. 用户在地图上框选区域
2. 前端收集参数并 POST 到 /api/tasks
3. TaskManager 计算总瓦片数
4. 插入 tasks 和 task_tiles 表
5. 返回 task_id

### 5.2 执行下载
1. 用户启动任务
2. TaskManager 创建后台线程
3. DownloadEngine 异步下载瓦片
4. 实时更新数据库和推送进度
5. 完成后使用 GDAL 拼接图像（如需要）
   - 为每个瓦片添加地理参考信息
   - 使用 VRT 虚拟数据集组织瓦片
   - 转换为目标格式（GeoTIFF/PNG/JPEG）
   - 可选：生成金字塔（多分辨率）以提高大图显示性能

### 5.3 断点续传
1. 暂停时保留所有瓦片状态
2. 恢复时查询未完成瓦片
3. 继续下载

## 6. 错误处理

- 网络错误：重试 3 次，服务器轮询
- 文件系统错误：暂停任务，通知用户
- 数据库错误：重试，记录日志
- 输入验证：前后端双重验证

## 7. 性能优化

- 异步并发下载
- 连接池复用
- 瓦片缓存
- 数据库索引
- WebSocket 节流
- GDAL VRT 虚拟数据集（避免加载所有瓦片到内存）
- GDAL 流式处理（处理超大图像）
- 可选的图像金字塔生成（提高大图浏览性能）

## 8. 安全考虑

- 输入验证（坐标、缩放级别）
- 路径遍历防护
- 资源限制（最大瓦片数、并发任务数）
- 局域网访问配置

## 9. 部署

```bash
# 安装系统依赖（Ubuntu/Debian）
sudo apt-get update
sudo apt-get install -y gdal-bin libgdal-dev python3-gdal

# 安装 Python 依赖
pip install -r requirements.txt

# 初始化数据库
python database.py

# 启动应用
python app.py
```

应用在 `http://0.0.0.0:5000` 启动。

**注意：** GDAL 的安装可能因操作系统而异：
- **Ubuntu/Debian:** `apt-get install gdal-bin libgdal-dev`
- **CentOS/RHEL:** `yum install gdal gdal-devel`
- **macOS:** `brew install gdal`
- **Windows:** 使用 OSGeo4W 或 Conda 安装

## 10. 风险和限制

- Google Maps 服务条款可能禁止批量下载
- 仅用于个人学习和研究
- 大区域下载耗时长
- 需要足够磁盘空间
