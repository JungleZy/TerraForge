# Google Maps 下载器

基于 Web 的 Google Maps 瓦片下载器，支持交互式地图选择区域、实时下载进度监控、历史记录可视化和高级配置管理。

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
- **前端:** Leaflet.js, Leaflet.draw, Bootstrap 5, Socket.IO

## 安装

### 系统依赖

**Ubuntu/Debian:**
```bash
sudo apt-get update
sudo apt-get install -y gdal-bin libgdal-dev python3-gdal
```

**macOS:**
```bash
brew install gdal
```

### Python 依赖

```bash
pip install -r requirements.txt
```

### 数据库初始化

```bash
python database.py
```

## 使用

### 启动应用

```bash
python app.py
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
├── app.py                  # Flask 应用入口
├── config.py               # 配置类
├── database.py             # 数据库初始化
├── models/                 # 数据模型
│   ├── task.py            # Task 和 Tile 模型
│   └── config.py          # Config 模型
├── services/               # 业务逻辑
│   ├── download_engine.py  # 下载引擎
│   ├── task_manager.py     # 任务管理器
│   └── config_manager.py   # 配置管理器
├── routes/                 # Flask 路由
│   ├── main.py            # 页面路由
│   ├── api.py             # API 路由
│   └── socketio_events.py # WebSocket 事件
├── templates/              # HTML 模板
│   ├── base.html          # 基础模板
│   ├── index.html         # 主页
│   ├── history.html       # 历史记录页
│   └── config.html        # 配置页
├── static/                 # 静态资源
│   ├── css/
│   │   └── style.css      # 自定义样式
│   └── js/
│       ├── map.js         # 地图交互
│       ├── tasks.js       # 任务管理
│       ├── history.js     # 历史记录
│       └── config.js      # 配置管理
├── downloads/              # 下载文件目录
├── cache/                  # 瓦片缓存目录
└── data/                   # SQLite 数据库
```

## API 端点

### 任务管理

- `POST /api/tasks` - 创建新任务
- `GET /api/tasks` - 获取所有任务
- `GET /api/tasks/<id>` - 获取任务详情
- `POST /api/tasks/<id>/start` - 启动任务
- `POST /api/tasks/<id>/pause` - 暂停任务
- `POST /api/tasks/<id>/resume` - 恢复任务
- `POST /api/tasks/<id>/cancel` - 取消任务
- `DELETE /api/tasks/<id>` - 删除任务

### 历史记录

- `GET /api/history` - 获取历史记录（支持分页）

### 配置管理

- `GET /api/config` - 获取所有配置
- `PUT /api/config` - 更新配置

### WebSocket 事件

- `task_progress` - 实时任务进度更新

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

## 故障排除

### GDAL 导入错误

如果遇到 `ImportError: No module named 'osgeo'`，请确保正确安装了 GDAL：

```bash
# Ubuntu/Debian
sudo apt-get install python3-gdal

# macOS
pip install gdal==$(gdal-config --version)
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
