# Google Maps 下载器实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建基于 Web 的 Google Maps 瓦片下载器，支持交互式地图选择区域、实时进度监控、任务调度和历史记录可视化。

**Architecture:** Flask 后端 + Leaflet.js 前端，使用 asyncio 异步下载瓦片，GDAL 进行地理配准和拼接，SQLite 存储任务和配置，WebSocket 实时推送进度。

**Tech Stack:** Flask, Flask-SocketIO, aiohttp, GDAL, Pillow, SQLite, Leaflet.js, Bootstrap 5

---

## 文件结构规划

### 后端核心文件
- `config.py` - 应用配置类
- `database.py` - 数据库初始化和连接管理
- `models/task.py` - Task 数据模型
- `models/config.py` - Config 数据模型
- `services/download_engine.py` - 下载引擎（瓦片计算、下载、拼接）
- `services/task_manager.py` - 任务管理器（创建、启动、暂停、恢复、取消）
- `services/config_manager.py` - 配置管理器
- `routes/main.py` - 页面路由
- `routes/api.py` - API 路由
- `routes/socketio_events.py` - WebSocket 事件处理
- `app.py` - Flask 应用入口

### 前端文件
- `templates/base.html` - 基础模板
- `templates/index.html` - 主页（地图界面）
- `templates/history.html` - 历史记录页面
- `templates/config.html` - 配置页面
- `static/css/style.css` - 自定义样式
- `static/js/map.js` - 地图交互逻辑
- `static/js/tasks.js` - 任务管理逻辑
- `static/js/history.js` - 历史记录逻辑
- `static/js/config.js` - 配置页面逻辑

### 其他文件
- `requirements.txt` - Python 依赖
- `.gitignore` - Git 忽略文件

---

## Task 1: 项目基础设置

**Files:**
- Create: `requirements.txt`
- Create: `.gitignore`
- Create: `config.py`

- [ ] **Step 1: 创建 requirements.txt**

```txt
Flask==2.3.3
Flask-SocketIO==5.3.4
python-socketio==5.9.0
python-engineio==4.7.1
aiohttp==3.9.1
GDAL==3.6.4
Pillow==10.1.0
```

- [ ] **Step 2: 创建 .gitignore**

```
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
env/
venv/
ENV/
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg
data/*.db
cache/
downloads/
*.log
.DS_Store
.vscode/
.idea/
```

- [ ] **Step 3: 创建 config.py**

```python
import os

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    DATABASE_PATH = os.path.join(BASE_DIR, 'data', 'map_downloader.db')
    DOWNLOADS_DIR = os.path.join(BASE_DIR, 'downloads')
    CACHE_DIR = os.path.join(BASE_DIR, 'cache')
    
    @staticmethod
    def init_app():
        os.makedirs(os.path.dirname(Config.DATABASE_PATH), exist_ok=True)
        os.makedirs(Config.DOWNLOADS_DIR, exist_ok=True)
        os.makedirs(Config.CACHE_DIR, exist_ok=True)
```

- [ ] **Step 4: 提交**

```bash
git add requirements.txt .gitignore config.py
git commit -m "feat: add project foundation files"
```

---

## Task 2: 数据库初始化

**Files:**
- Create: `database.py`

- [ ] **Step 1: 创建 database.py**

```python
import sqlite3
from config import Config

def get_connection():
    conn = sqlite3.connect(Config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_database():
    Config.init_app()
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            status TEXT NOT NULL,
            north REAL NOT NULL,
            south REAL NOT NULL,
            east REAL NOT NULL,
            west REAL NOT NULL,
            zoom_min INTEGER NOT NULL,
            zoom_max INTEGER NOT NULL,
            style TEXT NOT NULL,
            output_format TEXT NOT NULL,
            output_path TEXT NOT NULL,
            total_tiles INTEGER DEFAULT 0,
            downloaded_tiles INTEGER DEFAULT 0,
            failed_tiles INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            error_message TEXT
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS task_tiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            zoom INTEGER NOT NULL,
            x INTEGER NOT NULL,
            y INTEGER NOT NULL,
            status TEXT NOT NULL,
            retry_count INTEGER DEFAULT 0,
            error_message TEXT,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            UNIQUE(task_id, zoom, x, y)
        )
    ''')
    
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_task_tiles_status 
        ON task_tiles(task_id, status)
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    default_configs = [
        ('default_save_path', './downloads'),
        ('default_style', 'm'),
        ('default_zoom_min', '10'),
        ('default_zoom_max', '15'),
        ('default_output_format', 'both'),
        ('concurrent_downloads', '10'),
        ('request_timeout', '30'),
        ('max_retries', '3'),
        ('proxy_url', ''),
        ('tile_servers', 'mts0,mts1,mts2,mts3'),
        ('cache_enabled', 'true'),
        ('cache_max_size_mb', '1000'),
        ('history_retention_days', '90'),
        ('map_center_lat', '39.9'),
        ('map_center_lng', '116.4'),
        ('map_initial_zoom', '10'),
        ('gdal_compression', 'LZW'),
        ('gdal_resampling', 'cubic'),
    ]
    
    for key, value in default_configs:
        cursor.execute(
            'INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)',
            (key, value)
        )
    
    conn.commit()
    conn.close()
    print('Database initialized successfully')

if __name__ == '__main__':
    init_database()
```

- [ ] **Step 2: 运行数据库初始化**

```bash
python database.py
```

Expected: "Database initialized successfully"

- [ ] **Step 3: 验证数据库创建**

```bash
ls -la data/map_downloader.db
```

Expected: 文件存在

- [ ] **Step 4: 提交**

```bash
git add database.py
git commit -m "feat: add database initialization"
```

---

## Task 3: 数据模型

**Files:**
- Create: `models/__init__.py`
- Create: `models/task.py`
- Create: `models/config.py`

- [ ] **Step 1: 创建 models/__init__.py**

```python
from .task import Task
from .config import ConfigModel

__all__ = ['Task', 'ConfigModel']
```

- [ ] **Step 2: 创建 models/task.py**

```python
from dataclasses import dataclass
from typing import Optional
from datetime import datetime

@dataclass
class Task:
    id: Optional[int]
    name: str
    status: str
    north: float
    south: float
    east: float
    west: float
    zoom_min: int
    zoom_max: int
    style: str
    output_format: str
    output_path: str
    total_tiles: int = 0
    downloaded_tiles: int = 0
    failed_tiles: int = 0
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'status': self.status,
            'north': self.north,
            'south': self.south,
            'east': self.east,
            'west': self.west,
            'zoom_min': self.zoom_min,
            'zoom_max': self.zoom_max,
            'style': self.style,
            'output_format': self.output_format,
            'output_path': self.output_path,
            'total_tiles': self.total_tiles,
            'downloaded_tiles': self.downloaded_tiles,
            'failed_tiles': self.failed_tiles,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'error_message': self.error_message,
        }
    
    @property
    def progress_percent(self):
        if self.total_tiles == 0:
            return 0
        return int((self.downloaded_tiles / self.total_tiles) * 100)

@dataclass
class Tile:
    task_id: int
    zoom: int
    x: int
    y: int
    status: str = 'pending'
    retry_count: int = 0
    error_message: Optional[str] = None
    
    @property
    def cache_path(self):
        from config import Config
        return f"{Config.CACHE_DIR}/{self.zoom}/{self.x}/{self.y}.png"
```

- [ ] **Step 3: 创建 models/config.py**

```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class ConfigModel:
    key: str
    value: str
    updated_at: Optional[str] = None
    
    def to_dict(self):
        return {
            'key': self.key,
            'value': self.value,
            'updated_at': self.updated_at,
        }
```

- [ ] **Step 4: 提交**

```bash
git add models/
git commit -m "feat: add data models for Task and Config"
```

---

## Task 4: 配置管理器

**Files:**
- Create: `services/__init__.py`
- Create: `services/config_manager.py`
- Create: `tests/test_config_manager.py`

- [ ] **Step 1: 创建测试文件 tests/test_config_manager.py**

```python
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from services.config_manager import ConfigManager
from database import init_database

@pytest.fixture
def config_manager():
    init_database()
    return ConfigManager()

def test_get_existing_config(config_manager):
    value = config_manager.get('default_style')
    assert value == 'm'

def test_get_with_default(config_manager):
    value = config_manager.get('nonexistent_key', 'default_value')
    assert value == 'default_value'

def test_set_config(config_manager):
    config_manager.set('test_key', 'test_value')
    value = config_manager.get('test_key')
    assert value == 'test_value'

def test_get_all(config_manager):
    all_config = config_manager.get_all()
    assert isinstance(all_config, dict)
    assert 'default_style' in all_config
```

- [ ] **Step 2: 运行测试确认失败**

```bash
mkdir -p tests
python -m pytest tests/test_config_manager.py -v
```

Expected: FAIL with "ModuleNotFoundError: No module named 'services.config_manager'"

- [ ] **Step 3: 创建 services/__init__.py**

```python
from .config_manager import ConfigManager
from .download_engine import DownloadEngine
from .task_manager import TaskManager

__all__ = ['ConfigManager', 'DownloadEngine', 'TaskManager']
```

- [ ] **Step 4: 创建 services/config_manager.py**

```python
from database import get_connection
from typing import Any, Dict, Optional

class ConfigManager:
    def get(self, key: str, default: Any = None) -> Any:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM config WHERE key = ?', (key,))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return row['value']
        return default
    
    def set(self, key: str, value: Any):
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO config (key, value, updated_at) 
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(key) DO UPDATE SET 
               value = excluded.value, 
               updated_at = CURRENT_TIMESTAMP''',
            (key, str(value))
        )
        conn.commit()
        conn.close()
    
    def get_all(self) -> Dict[str, str]:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT key, value FROM config')
        rows = cursor.fetchall()
        conn.close()
        
        return {row['key']: row['value'] for row in rows}
    
    def reset_to_defaults(self):
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM config')
        
        default_configs = [
            ('default_save_path', './downloads'),
            ('default_style', 'm'),
            ('default_zoom_min', '10'),
            ('default_zoom_max', '15'),
            ('default_output_format', 'both'),
            ('concurrent_downloads', '10'),
            ('request_timeout', '30'),
            ('max_retries', '3'),
            ('proxy_url', ''),
            ('tile_servers', 'mts0,mts1,mts2,mts3'),
            ('cache_enabled', 'true'),
            ('cache_max_size_mb', '1000'),
            ('history_retention_days', '90'),
            ('map_center_lat', '39.9'),
            ('map_center_lng', '116.4'),
            ('map_initial_zoom', '10'),
            ('gdal_compression', 'LZW'),
            ('gdal_resampling', 'cubic'),
        ]
        
        for key, value in default_configs:
            cursor.execute(
                'INSERT INTO config (key, value) VALUES (?, ?)',
                (key, value)
            )
        
        conn.commit()
        conn.close()
    
    def validate_config(self, key: str, value: Any) -> bool:
        validations = {
            'concurrent_downloads': lambda v: v.isdigit() and 1 <= int(v) <= 100,
            'request_timeout': lambda v: v.isdigit() and 1 <= int(v) <= 300,
            'max_retries': lambda v: v.isdigit() and 0 <= int(v) <= 10,
            'cache_max_size_mb': lambda v: v.isdigit() and int(v) >= 0,
            'history_retention_days': lambda v: v.isdigit() and int(v) >= 0,
            'map_center_lat': lambda v: self._is_valid_lat(v),
            'map_center_lng': lambda v: self._is_valid_lng(v),
            'map_initial_zoom': lambda v: v.isdigit() and 0 <= int(v) <= 21,
            'default_zoom_min': lambda v: v.isdigit() and 0 <= int(v) <= 21,
            'default_zoom_max': lambda v: v.isdigit() and 0 <= int(v) <= 21,
        }
        
        if key in validations:
            return validations[key](str(value))
        return True
    
    def _is_valid_lat(self, value: str) -> bool:
        try:
            lat = float(value)
            return -90 <= lat <= 90
        except ValueError:
            return False
    
    def _is_valid_lng(self, value: str) -> bool:
        try:
            lng = float(value)
            return -180 <= lng <= 180
        except ValueError:
            return False
```

- [ ] **Step 5: 运行测试确认通过**

```bash
python -m pytest tests/test_config_manager.py -v
```

Expected: All tests PASS

- [ ] **Step 6: 提交**

```bash
git add services/ tests/
git commit -m "feat: add ConfigManager with validation"
```

---

## Task 5: 下载引擎 - 瓦片坐标计算

**Files:**
- Create: `services/download_engine.py`
- Create: `tests/test_download_engine.py`

- [ ] **Step 1: 创建测试文件**

```python
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from services.download_engine import DownloadEngine

@pytest.fixture
def engine():
    return DownloadEngine()

def test_lat_lon_to_tile_zoom_10(engine):
    x, y = engine.lat_lon_to_tile(39.9, 116.4, 10)
    assert isinstance(x, int)
    assert isinstance(y, int)
    assert 0 <= x < 2**10
    assert 0 <= y < 2**10

def test_calculate_tiles_single_zoom(engine):
    tiles = engine.calculate_tiles(40.0, 39.8, 116.5, 116.3, 10, 10)
    assert len(tiles) > 0
    assert all(tile.zoom == 10 for tile in tiles)

def test_calculate_tiles_range(engine):
    tiles = engine.calculate_tiles(40.0, 39.8, 116.5, 116.3, 10, 11)
    zoom_10_tiles = [t for t in tiles if t.zoom == 10]
    zoom_11_tiles = [t for t in tiles if t.zoom == 11]
    assert len(zoom_10_tiles) > 0
    assert len(zoom_11_tiles) > 0
    assert len(zoom_11_tiles) > len(zoom_10_tiles)

def test_get_tile_url(engine):
    url = engine.get_tile_url(100, 200, 10, 'm', 0)
    assert 'mts0.googleapis.com' in url
    assert 'lyrs=m' in url
    assert 'x=100' in url
    assert 'y=200' in url
    assert 'z=10' in url
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_download_engine.py -v
```

Expected: FAIL

- [ ] **Step 3: 创建 services/download_engine.py（第一部分）**

```python
import math
import os
import asyncio
import aiohttp
from typing import List, Tuple
from models.task import Tile
from services.config_manager import ConfigManager

class DownloadEngine:
    def __init__(self):
        self.config_manager = ConfigManager()
    
    def lat_lon_to_tile(self, lat: float, lon: float, zoom: int) -> Tuple[int, int]:
        n = 2 ** zoom
        x = int((lon + 180) / 360 * n)
        lat_rad = math.radians(lat)
        y = int((1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2 * n)
        return x, y
    
    def calculate_tiles(self, north: float, south: float, east: float, west: float, 
                       zoom_min: int, zoom_max: int) -> List[Tile]:
        tiles = []
        task_id = 0
        
        for zoom in range(zoom_min, zoom_max + 1):
            x_min, y_max = self.lat_lon_to_tile(north, west, zoom)
            x_max, y_min = self.lat_lon_to_tile(south, east, zoom)
            
            for x in range(x_min, x_max + 1):
                for y in range(y_min, y_max + 1):
                    tile = Tile(
                        task_id=task_id,
                        zoom=zoom,
                        x=x,
                        y=y,
                        status='pending'
                    )
                    tiles.append(tile)
        
        return tiles
    
    def get_tile_url(self, x: int, y: int, z: int, style: str, server_index: int) -> str:
        servers = self.config_manager.get('tile_servers', 'mts0,mts1,mts2,mts3').split(',')
        server = servers[server_index % len(servers)]
        return f"http://{server}.googleapis.com/vt?lyrs={style}&x={x}&y={y}&z={z}"
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest tests/test_download_engine.py -v
```

Expected: All tests PASS

- [ ] **Step 5: 提交**

```bash
git add services/download_engine.py tests/test_download_engine.py
git commit -m "feat: add tile coordinate calculation"
```

---

由于计划内容较长，我将继续在下一部分编写剩余任务...

## Task 6: 下载引擎 - 异步下载瓦片

**Files:**
- Modify: `services/download_engine.py`

- [ ] **Step 1: 添加异步下载方法**

在 `services/download_engine.py` 中添加：

```python
    async def download_tile(self, tile: Tile, style: str, session: aiohttp.ClientSession) -> bytes:
        max_retries = int(self.config_manager.get('max_retries', '3'))
        timeout = int(self.config_manager.get('request_timeout', '30'))
        servers = self.config_manager.get('tile_servers', 'mts0,mts1,mts2,mts3').split(',')
        
        for attempt in range(max_retries):
            server_index = attempt % len(servers)
            url = self.get_tile_url(tile.x, tile.y, tile.zoom, style, server_index)
            
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                    if response.status == 200:
                        return await response.read()
                    else:
                        if attempt == max_retries - 1:
                            raise Exception(f"HTTP {response.status}")
            except asyncio.TimeoutError:
                if attempt == max_retries - 1:
                    raise Exception("Timeout")
            except Exception as e:
                if attempt == max_retries - 1:
                    raise e
            
            await asyncio.sleep(2 ** attempt)
        
        raise Exception("Max retries exceeded")
    
    async def download_tiles_batch(self, tiles: List[Tile], task_id: int, style: str, 
                                   progress_callback=None):
        concurrent = int(self.config_manager.get('concurrent_downloads', '10'))
        cache_enabled = self.config_manager.get('cache_enabled', 'true') == 'true'
        
        connector = aiohttp.TCPConnector(limit=concurrent)
        async with aiohttp.ClientSession(connector=connector) as session:
            semaphore = asyncio.Semaphore(concurrent)
            
            async def download_with_semaphore(tile):
                async with semaphore:
                    return await self._download_single_tile(tile, style, session, 
                                                            cache_enabled, progress_callback)
            
            tasks = [download_with_semaphore(tile) for tile in tiles]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            return results
    
    async def _download_single_tile(self, tile: Tile, style: str, session: aiohttp.ClientSession,
                                    cache_enabled: bool, progress_callback):
        cache_path = self._get_cache_path(tile, style)
        
        if cache_enabled and os.path.exists(cache_path):
            if progress_callback:
                await progress_callback(tile, 'completed', None)
            return {'tile': tile, 'status': 'cached'}
        
        try:
            data = await self.download_tile(tile, style, session)
            
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, 'wb') as f:
                f.write(data)
            
            if progress_callback:
                await progress_callback(tile, 'completed', None)
            
            return {'tile': tile, 'status': 'completed', 'size': len(data)}
        
        except Exception as e:
            if progress_callback:
                await progress_callback(tile, 'failed', str(e))
            
            return {'tile': tile, 'status': 'failed', 'error': str(e)}
    
    def _get_cache_path(self, tile: Tile, style: str):
        from config import Config
        return os.path.join(Config.CACHE_DIR, style, str(tile.zoom), 
                          str(tile.x), f"{tile.y}.png")
```

- [ ] **Step 2: 提交**

```bash
git add services/download_engine.py
git commit -m "feat: add async tile download with retry and caching"
```

---

## Task 7: 下载引擎 - GDAL 瓦片拼接

**Files:**
- Modify: `services/download_engine.py`

- [ ] **Step 1: 添加 GDAL 拼接方法**

在 `services/download_engine.py` 顶部添加导入：

```python
from osgeo import gdal, osr
```

在类中添加方法：

```python
    def stitch_tiles_with_gdal(self, tiles: List[Tile], style: str, output_path: str, 
                               zoom_level: int):
        tile_paths = []
        for tile in tiles:
            cache_path = self._get_cache_path(tile, style)
            if os.path.exists(cache_path):
                georef_path = self._add_georeference(cache_path, tile)
                tile_paths.append(georef_path)
        
        if not tile_paths:
            raise Exception("No tiles to stitch")
        
        vrt_path = output_path.replace('.tif', '.vrt').replace('.png', '.vrt')
        
        compression = self.config_manager.get('gdal_compression', 'LZW')
        resampling = self.config_manager.get('gdal_resampling', 'cubic')
        
        vrt_options = gdal.BuildVRTOptions(resampleAlg=resampling)
        vrt = gdal.BuildVRT(vrt_path, tile_paths, options=vrt_options)
        
        if output_path.endswith('.tif'):
            translate_options = gdal.TranslateOptions(
                format='GTiff',
                creationOptions=[f'COMPRESS={compression}', 'TILED=YES']
            )
        else:
            translate_options = gdal.TranslateOptions(format='PNG')
        
        gdal.Translate(output_path, vrt, options=translate_options)
        
        vrt = None
        
        if os.path.exists(vrt_path):
            os.remove(vrt_path)
        
        return output_path
    
    def _add_georeference(self, tile_path: str, tile: Tile):
        georef_path = tile_path.replace('.png', '_geo.tif')
        
        if os.path.exists(georef_path):
            return georef_path
        
        ds = gdal.Open(tile_path)
        if ds is None:
            return tile_path
        
        driver = gdal.GetDriverByName('GTiff')
        out_ds = driver.CreateCopy(georef_path, ds, 0)
        
        n = 2 ** tile.zoom
        tile_size = 256
        
        min_lon = tile.x / n * 360.0 - 180.0
        max_lon = (tile.x + 1) / n * 360.0 - 180.0
        
        max_lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * tile.y / n)))
        min_lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * (tile.y + 1) / n)))
        max_lat = math.degrees(max_lat_rad)
        min_lat = math.degrees(min_lat_rad)
        
        pixel_width = (max_lon - min_lon) / tile_size
        pixel_height = (max_lat - min_lat) / tile_size
        
        geotransform = [
            min_lon,
            pixel_width,
            0,
            max_lat,
            0,
            -pixel_height
        ]
        
        out_ds.SetGeoTransform(geotransform)
        
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        out_ds.SetProjection(srs.ExportToWkt())
        
        out_ds = None
        ds = None
        
        return georef_path
```

- [ ] **Step 2: 提交**

```bash
git add services/download_engine.py
git commit -m "feat: add GDAL tile stitching with georeference"
```

---


## Task 8: 任务管理器

**Files:**
- Create: `services/task_manager.py`

- [ ] **Step 1: 创建 services/task_manager.py（第一部分）**

```python
import asyncio
import threading
from datetime import datetime
from typing import Dict, List, Optional
from database import get_connection
from models.task import Task, Tile
from services.download_engine import DownloadEngine
from services.config_manager import ConfigManager

class TaskManager:
    def __init__(self, socketio=None):
        self.socketio = socketio
        self.download_engine = DownloadEngine()
        self.config_manager = ConfigManager()
        self.active_tasks: Dict[int, dict] = {}
        self.stop_flags: Dict[int, threading.Event] = {}
    
    def create_task(self, params: dict) -> int:
        conn = get_connection()
        cursor = conn.cursor()
        
        tiles = self.download_engine.calculate_tiles(
            params['north'], params['south'], params['east'], params['west'],
            params['zoom_min'], params['zoom_max']
        )
        
        cursor.execute('''
            INSERT INTO tasks (name, status, north, south, east, west, 
                             zoom_min, zoom_max, style, output_format, output_path, total_tiles)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            params['name'],
            'pending',
            params['north'],
            params['south'],
            params['east'],
            params['west'],
            params['zoom_min'],
            params['zoom_max'],
            params['style'],
            params['output_format'],
            params['output_path'],
            len(tiles)
        ))
        
        task_id = cursor.lastrowid
        
        for tile in tiles:
            tile.task_id = task_id
            cursor.execute('''
                INSERT INTO task_tiles (task_id, zoom, x, y, status)
                VALUES (?, ?, ?, ?, ?)
            ''', (task_id, tile.zoom, tile.x, tile.y, 'pending'))
        
        conn.commit()
        conn.close()
        
        return task_id
    
    def start_task(self, task_id: int):
        task = self._get_task(task_id)
        if not task:
            raise Exception(f"Task {task_id} not found")
        
        if task.status not in ['pending', 'paused']:
            raise Exception(f"Task {task_id} cannot be started (status: {task.status})")
        
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE tasks SET status = ?, started_at = CURRENT_TIMESTAMP 
            WHERE id = ?
        ''', ('running', task_id))
        conn.commit()
        conn.close()
        
        self.stop_flags[task_id] = threading.Event()
        
        thread = threading.Thread(target=self._run_task, args=(task_id,))
        thread.daemon = True
        thread.start()
        
        self.active_tasks[task_id] = {
            'thread': thread,
            'start_time': datetime.now()
        }
    
    def pause_task(self, task_id: int):
        if task_id in self.stop_flags:
            self.stop_flags[task_id].set()
        
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('UPDATE tasks SET status = ? WHERE id = ?', ('paused', task_id))
        conn.commit()
        conn.close()
    
    def resume_task(self, task_id: int):
        self.start_task(task_id)
    
    def cancel_task(self, task_id: int):
        if task_id in self.stop_flags:
            self.stop_flags[task_id].set()
        
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('UPDATE tasks SET status = ? WHERE id = ?', ('cancelled', task_id))
        conn.commit()
        conn.close()
    
    def get_task_status(self, task_id: int) -> dict:
        task = self._get_task(task_id)
        if not task:
            return None
        
        return task.to_dict()
    
    def get_active_tasks(self) -> List[dict]:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM tasks 
            WHERE status IN ('pending', 'running', 'paused')
            ORDER BY created_at DESC
        ''')
        rows = cursor.fetchall()
        conn.close()
        
        tasks = []
        for row in rows:
            task = self._row_to_task(row)
            tasks.append(task.to_dict())
        
        return tasks
    
    def _get_task(self, task_id: int) -> Optional[Task]:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM tasks WHERE id = ?', (task_id,))
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            return None
        
        return self._row_to_task(row)
    
    def _row_to_task(self, row) -> Task:
        return Task(
            id=row['id'],
            name=row['name'],
            status=row['status'],
            north=row['north'],
            south=row['south'],
            east=row['east'],
            west=row['west'],
            zoom_min=row['zoom_min'],
            zoom_max=row['zoom_max'],
            style=row['style'],
            output_format=row['output_format'],
            output_path=row['output_path'],
            total_tiles=row['total_tiles'],
            downloaded_tiles=row['downloaded_tiles'],
            failed_tiles=row['failed_tiles'],
            created_at=row['created_at'],
            started_at=row['started_at'],
            completed_at=row['completed_at'],
            error_message=row['error_message']
        )
```

- [ ] **Step 2: 创建 services/task_manager.py（第二部分 - 执行逻辑）**

在同一文件中继续添加：

```python
    def _run_task(self, task_id: int):
        try:
            asyncio.run(self._execute_task(task_id))
        except Exception as e:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE tasks SET status = ?, error_message = ? WHERE id = ?
            ''', ('failed', str(e), task_id))
            conn.commit()
            conn.close()
    
    async def _execute_task(self, task_id: int):
        task = self._get_task(task_id)
        
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM task_tiles 
            WHERE task_id = ? AND status IN ('pending', 'failed')
            AND retry_count < ?
        ''', (task_id, int(self.config_manager.get('max_retries', '3'))))
        rows = cursor.fetchall()
        conn.close()
        
        tiles = []
        for row in rows:
            tile = Tile(
                task_id=row['task_id'],
                zoom=row['zoom'],
                x=row['x'],
                y=row['y'],
                status=row['status'],
                retry_count=row['retry_count']
            )
            tiles.append(tile)
        
        async def progress_callback(tile, status, error):
            if self.stop_flags.get(task_id) and self.stop_flags[task_id].is_set():
                return
            
            conn = get_connection()
            cursor = conn.cursor()
            
            if status == 'completed':
                cursor.execute('''
                    UPDATE task_tiles SET status = ? WHERE task_id = ? AND zoom = ? AND x = ? AND y = ?
                ''', ('completed', tile.task_id, tile.zoom, tile.x, tile.y))
                
                cursor.execute('''
                    UPDATE tasks SET downloaded_tiles = downloaded_tiles + 1 WHERE id = ?
                ''', (task_id,))
            
            elif status == 'failed':
                cursor.execute('''
                    UPDATE task_tiles SET status = ?, retry_count = retry_count + 1, error_message = ?
                    WHERE task_id = ? AND zoom = ? AND x = ? AND y = ?
                ''', ('failed', error, tile.task_id, tile.zoom, tile.x, tile.y))
                
                cursor.execute('''
                    UPDATE tasks SET failed_tiles = failed_tiles + 1 WHERE id = ?
                ''', (task_id,))
            
            conn.commit()
            conn.close()
            
            if self.socketio:
                task_status = self.get_task_status(task_id)
                self.socketio.emit('task_progress', task_status)
        
        await self.download_engine.download_tiles_batch(
            tiles, task_id, task.style, progress_callback
        )
        
        if self.stop_flags.get(task_id) and self.stop_flags[task_id].is_set():
            return
        
        if task.output_format in ['image_only', 'both']:
            for zoom in range(task.zoom_min, task.zoom_max + 1):
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT * FROM task_tiles 
                    WHERE task_id = ? AND zoom = ? AND status = 'completed'
                ''', (task_id, zoom))
                rows = cursor.fetchall()
                conn.close()
                
                zoom_tiles = []
                for row in rows:
                    tile = Tile(
                        task_id=row['task_id'],
                        zoom=row['zoom'],
                        x=row['x'],
                        y=row['y']
                    )
                    zoom_tiles.append(tile)
                
                if zoom_tiles:
                    output_file = f"{task.output_path}_z{zoom}.tif"
                    self.download_engine.stitch_tiles_with_gdal(
                        zoom_tiles, task.style, output_file, zoom
                    )
        
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE tasks SET status = ?, completed_at = CURRENT_TIMESTAMP 
            WHERE id = ?
        ''', ('completed', task_id))
        conn.commit()
        conn.close()
        
        if task_id in self.active_tasks:
            del self.active_tasks[task_id]
        if task_id in self.stop_flags:
            del self.stop_flags[task_id]
```

- [ ] **Step 3: 提交**

```bash
git add services/task_manager.py
git commit -m "feat: add TaskManager with start/pause/resume/cancel"
```

---

## Task 9: Flask 路由

**Files:**
- Create: `routes/__init__.py`
- Create: `routes/main.py`
- Create: `routes/api.py`
- Create: `routes/socketio_events.py`

- [ ] **Step 1: 创建 routes/__init__.py**

```python
from .main import main_bp
from .api import api_bp

__all__ = ['main_bp', 'api_bp']
```

- [ ] **Step 2: 创建 routes/main.py**

```python
from flask import Blueprint, render_template
from services.config_manager import ConfigManager

main_bp = Blueprint('main', __name__)
config_manager = ConfigManager()

@main_bp.route('/')
def index():
    config = config_manager.get_all()
    return render_template('index.html', config=config)

@main_bp.route('/history')
def history():
    return render_template('history.html')

@main_bp.route('/config')
def config():
    config_data = config_manager.get_all()
    return render_template('config.html', config=config_data)
```

- [ ] **Step 3: 创建 routes/api.py（第一部分）**

```python
from flask import Blueprint, request, jsonify
from services.task_manager import TaskManager
from services.config_manager import ConfigManager
from database import get_connection

api_bp = Blueprint('api', __name__, url_prefix='/api')

task_manager = None
config_manager = ConfigManager()

def init_task_manager(tm):
    global task_manager
    task_manager = tm

@api_bp.route('/tasks', methods=['POST'])
def create_task():
    data = request.json
    
    required_fields = ['name', 'north', 'south', 'east', 'west', 
                      'zoom_min', 'zoom_max', 'style', 'output_format', 'output_path']
    for field in required_fields:
        if field not in data:
            return jsonify({'error': f'Missing field: {field}'}), 400
    
    if not (-90 <= data['north'] <= 90 and -90 <= data['south'] <= 90):
        return jsonify({'error': 'Invalid latitude'}), 400
    
    if not (-180 <= data['east'] <= 180 and -180 <= data['west'] <= 180):
        return jsonify({'error': 'Invalid longitude'}), 400
    
    if not (0 <= data['zoom_min'] <= 21 and 0 <= data['zoom_max'] <= 21):
        return jsonify({'error': 'Invalid zoom level'}), 400
    
    if data['zoom_min'] > data['zoom_max']:
        return jsonify({'error': 'zoom_min must be <= zoom_max'}), 400
    
    try:
        task_id = task_manager.create_task(data)
        return jsonify({'task_id': task_id}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@api_bp.route('/tasks', methods=['GET'])
def get_tasks():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM tasks ORDER BY created_at DESC LIMIT 100')
    rows = cursor.fetchall()
    conn.close()
    
    tasks = []
    for row in rows:
        tasks.append(dict(row))
    
    return jsonify(tasks)

@api_bp.route('/tasks/<int:task_id>', methods=['GET'])
def get_task(task_id):
    task = task_manager.get_task_status(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    return jsonify(task)
```

- [ ] **Step 4: 创建 routes/api.py（第二部分）**

继续在同一文件中添加：

```python
@api_bp.route('/tasks/<int:task_id>/start', methods=['POST'])
def start_task(task_id):
    try:
        task_manager.start_task(task_id)
        return jsonify({'message': 'Task started'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@api_bp.route('/tasks/<int:task_id>/pause', methods=['POST'])
def pause_task(task_id):
    try:
        task_manager.pause_task(task_id)
        return jsonify({'message': 'Task paused'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@api_bp.route('/tasks/<int:task_id>/resume', methods=['POST'])
def resume_task(task_id):
    try:
        task_manager.resume_task(task_id)
        return jsonify({'message': 'Task resumed'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@api_bp.route('/tasks/<int:task_id>/cancel', methods=['POST'])
def cancel_task(task_id):
    try:
        task_manager.cancel_task(task_id)
        return jsonify({'message': 'Task cancelled'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@api_bp.route('/tasks/<int:task_id>', methods=['DELETE'])
def delete_task(task_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Task deleted'}), 200

@api_bp.route('/history', methods=['GET'])
def get_history():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT * FROM tasks 
        WHERE status IN ('completed', 'failed', 'cancelled')
        ORDER BY completed_at DESC 
        LIMIT ? OFFSET ?
    ''', (per_page, (page - 1) * per_page))
    rows = cursor.fetchall()
    
    cursor.execute('''
        SELECT COUNT(*) as total FROM tasks 
        WHERE status IN ('completed', 'failed', 'cancelled')
    ''')
    total = cursor.fetchone()['total']
    conn.close()
    
    tasks = [dict(row) for row in rows]
    
    return jsonify({
        'tasks': tasks,
        'total': total,
        'page': page,
        'per_page': per_page
    })

@api_bp.route('/config', methods=['GET'])
def get_config():
    config = config_manager.get_all()
    return jsonify(config)

@api_bp.route('/config', methods=['PUT'])
def update_config():
    data = request.json
    
    for key, value in data.items():
        if not config_manager.validate_config(key, value):
            return jsonify({'error': f'Invalid value for {key}'}), 400
        config_manager.set(key, value)
    
    return jsonify({'message': 'Config updated'}), 200
```

- [ ] **Step 5: 创建 routes/socketio_events.py**

```python
from flask_socketio import emit

def register_socketio_events(socketio):
    @socketio.on('connect')
    def handle_connect():
        print('Client connected')
        emit('connected', {'message': 'Connected to server'})
    
    @socketio.on('disconnect')
    def handle_disconnect():
        print('Client disconnected')
```

- [ ] **Step 6: 提交**

```bash
git add routes/
git commit -m "feat: add Flask routes for pages and API"
```

---


## Task 10: Flask 应用入口

**Files:**
- Create: `app.py`

- [ ] **Step 1: 创建 app.py**

```python
from flask import Flask
from flask_socketio import SocketIO
from config import Config
from database import init_database
from routes import main_bp, api_bp
from routes.api import init_task_manager
from routes.socketio_events import register_socketio_events
from services.task_manager import TaskManager

app = Flask(__name__)
app.config.from_object(Config)

socketio = SocketIO(app, cors_allowed_origins="*")

init_database()

task_manager = TaskManager(socketio=socketio)
init_task_manager(task_manager)

app.register_blueprint(main_bp)
app.register_blueprint(api_bp)

register_socketio_events(socketio)

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
```

- [ ] **Step 2: 测试应用启动**

```bash
python app.py
```

Expected: Server starts on http://0.0.0.0:5000

- [ ] **Step 3: 提交**

```bash
git add app.py
git commit -m "feat: add Flask application entry point"
```

---

## Task 11: 前端基础模板

**Files:**
- Create: `templates/base.html`
- Create: `static/css/style.css`

- [ ] **Step 1: 创建目录**

```bash
mkdir -p templates static/css static/js
```

- [ ] **Step 2: 创建 templates/base.html**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}Google Maps 下载器{% endblock %}</title>
    
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <link rel="stylesheet" href="{{ url_for('static', filename='css/style.css') }}">
    
    {% block extra_css %}{% endblock %}
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-dark bg-dark">
        <div class="container-fluid">
            <a class="navbar-brand" href="/">Google Maps 下载器</a>
            <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav">
                <span class="navbar-toggler-icon"></span>
            </button>
            <div class="collapse navbar-collapse" id="navbarNav">
                <ul class="navbar-nav">
                    <li class="nav-item">
                        <a class="nav-link {% if request.endpoint == 'main.index' %}active{% endif %}" href="/">地图下载</a>
                    </li>
                    <li class="nav-item">
                        <a class="nav-link {% if request.endpoint == 'main.history' %}active{% endif %}" href="/history">历史记录</a>
                    </li>
                    <li class="nav-item">
                        <a class="nav-link {% if request.endpoint == 'main.config' %}active{% endif %}" href="/config">配置</a>
                    </li>
                </ul>
            </div>
        </div>
    </nav>

    <div class="container-fluid mt-3">
        {% block content %}{% endblock %}
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
    
    {% block extra_js %}{% endblock %}
</body>
</html>
```

- [ ] **Step 3: 创建 static/css/style.css**

```css
body {
    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
}

#map {
    height: 600px;
    width: 100%;
    border: 2px solid #dee2e6;
    border-radius: 4px;
}

.task-card {
    margin-bottom: 15px;
    border: 1px solid #dee2e6;
    border-radius: 4px;
    padding: 15px;
}

.task-card.running {
    border-left: 4px solid #0d6efd;
}

.task-card.completed {
    border-left: 4px solid #198754;
}

.task-card.failed {
    border-left: 4px solid #dc3545;
}

.task-card.paused {
    border-left: 4px solid #ffc107;
}

.progress-detail {
    font-size: 0.9em;
    color: #6c757d;
    margin-top: 5px;
}

.zoom-progress {
    margin-top: 10px;
}

.zoom-progress-item {
    margin-bottom: 5px;
}

.zoom-progress-label {
    font-size: 0.85em;
    margin-bottom: 2px;
}

.btn-group-sm .btn {
    padding: 0.25rem 0.5rem;
    font-size: 0.875rem;
}

.history-map {
    height: 400px;
    margin-bottom: 20px;
}

.config-section {
    margin-bottom: 30px;
}

.config-section h5 {
    border-bottom: 2px solid #dee2e6;
    padding-bottom: 10px;
    margin-bottom: 15px;
}
```

- [ ] **Step 4: 提交**

```bash
git add templates/base.html static/css/style.css
git commit -m "feat: add base template and CSS styles"
```

---

## Task 12: 主页地图界面

**Files:**
- Create: `templates/index.html`
- Create: `static/js/map.js`
- Create: `static/js/tasks.js`

- [ ] **Step 1: 创建 templates/index.html**

```html
{% extends "base.html" %}

{% block title %}地图下载 - Google Maps 下载器{% endblock %}

{% block content %}
<div class="row">
    <div class="col-md-8">
        <div id="map"></div>
    </div>
    
    <div class="col-md-4">
        <div class="card">
            <div class="card-header">
                <h5>下载参数</h5>
            </div>
            <div class="card-body">
                <form id="downloadForm">
                    <div class="mb-3">
                        <label for="taskName" class="form-label">任务名称</label>
                        <input type="text" class="form-control" id="taskName" required>
                    </div>
                    
                    <div class="mb-3">
                        <label for="mapStyle" class="form-label">地图样式</label>
                        <select class="form-select" id="mapStyle">
                            <option value="m">标准地图</option>
                            <option value="s">卫星图</option>
                            <option value="y">卫星图+标注</option>
                            <option value="h">道路图</option>
                            <option value="t">地形图</option>
                        </select>
                    </div>
                    
                    <div class="row">
                        <div class="col-6 mb-3">
                            <label for="zoomMin" class="form-label">最小缩放级别</label>
                            <input type="number" class="form-control" id="zoomMin" min="0" max="21" value="10">
                        </div>
                        <div class="col-6 mb-3">
                            <label for="zoomMax" class="form-label">最大缩放级别</label>
                            <input type="number" class="form-control" id="zoomMax" min="0" max="21" value="15">
                        </div>
                    </div>
                    
                    <div class="mb-3">
                        <label for="outputFormat" class="form-label">输出格式</label>
                        <select class="form-select" id="outputFormat">
                            <option value="both">瓦片+拼接图</option>
                            <option value="tiles_only">仅瓦片</option>
                            <option value="image_only">仅拼接图</option>
                        </select>
                    </div>
                    
                    <div class="mb-3">
                        <label for="outputPath" class="form-label">保存路径</label>
                        <input type="text" class="form-control" id="outputPath" value="./downloads/map">
                    </div>
                    
                    <div class="alert alert-info" id="boundsInfo">
                        <small>请在地图上框选下载区域</small>
                    </div>
                    
                    <button type="submit" class="btn btn-primary w-100" id="createTaskBtn" disabled>
                        创建下载任务
                    </button>
                </form>
            </div>
        </div>
        
        <div class="card mt-3">
            <div class="card-header">
                <h5>活动任务</h5>
            </div>
            <div class="card-body" id="activeTasks">
                <p class="text-muted">暂无活动任务</p>
            </div>
        </div>
    </div>
</div>
{% endblock %}

{% block extra_js %}
<script src="{{ url_for('static', filename='js/map.js') }}"></script>
<script src="{{ url_for('static', filename='js/tasks.js') }}"></script>
<script>
    const config = {{ config|tojson }};
    initMap(config);
    initTasks();
</script>
{% endblock %}
```

- [ ] **Step 2: 创建 static/js/map.js**

```javascript
let map;
let drawnItems;
let currentBounds = null;

function initMap(config) {
    const centerLat = parseFloat(config.map_center_lat || 39.9);
    const centerLng = parseFloat(config.map_center_lng || 116.4);
    const initialZoom = parseInt(config.map_initial_zoom || 10);
    
    map = L.map('map').setView([centerLat, centerLng], initialZoom);
    
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '© OpenStreetMap contributors'
    }).addTo(map);
    
    drawnItems = new L.FeatureGroup();
    map.addLayer(drawnItems);
    
    const drawControl = new L.Control.Draw({
        draw: {
            rectangle: true,
            polygon: false,
            circle: false,
            marker: false,
            polyline: false,
            circlemarker: false
        },
        edit: {
            featureGroup: drawnItems,
            remove: true
        }
    });
    map.addControl(drawControl);
    
    map.on(L.Draw.Event.CREATED, function(event) {
        drawnItems.clearLayers();
        const layer = event.layer;
        drawnItems.addLayer(layer);
        
        const bounds = layer.getBounds();
        currentBounds = {
            north: bounds.getNorth(),
            south: bounds.getSouth(),
            east: bounds.getEast(),
            west: bounds.getWest()
        };
        
        updateBoundsInfo();
        document.getElementById('createTaskBtn').disabled = false;
    });
    
    map.on(L.Draw.Event.DELETED, function() {
        currentBounds = null;
        updateBoundsInfo();
        document.getElementById('createTaskBtn').disabled = true;
    });
}

function updateBoundsInfo() {
    const boundsInfo = document.getElementById('boundsInfo');
    if (currentBounds) {
        boundsInfo.innerHTML = `
            <small>
                <strong>选中区域：</strong><br>
                北: ${currentBounds.north.toFixed(6)}<br>
                南: ${currentBounds.south.toFixed(6)}<br>
                东: ${currentBounds.east.toFixed(6)}<br>
                西: ${currentBounds.west.toFixed(6)}
            </small>
        `;
    } else {
        boundsInfo.innerHTML = '<small>请在地图上框选下载区域</small>';
    }
}

document.getElementById('downloadForm').addEventListener('submit', async function(e) {
    e.preventDefault();
    
    if (!currentBounds) {
        alert('请先在地图上框选下载区域');
        return;
    }
    
    const taskData = {
        name: document.getElementById('taskName').value,
        north: currentBounds.north,
        south: currentBounds.south,
        east: currentBounds.east,
        west: currentBounds.west,
        zoom_min: parseInt(document.getElementById('zoomMin').value),
        zoom_max: parseInt(document.getElementById('zoomMax').value),
        style: document.getElementById('mapStyle').value,
        output_format: document.getElementById('outputFormat').value,
        output_path: document.getElementById('outputPath').value
    };
    
    try {
        const response = await fetch('/api/tasks', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(taskData)
        });
        
        const result = await response.json();
        
        if (response.ok) {
            alert('任务创建成功！ID: ' + result.task_id);
            document.getElementById('downloadForm').reset();
            drawnItems.clearLayers();
            currentBounds = null;
            updateBoundsInfo();
            document.getElementById('createTaskBtn').disabled = true;
            loadActiveTasks();
        } else {
            alert('创建任务失败: ' + result.error);
        }
    } catch (error) {
        alert('创建任务失败: ' + error.message);
    }
});
```

- [ ] **Step 3: 创建 static/js/tasks.js**

```javascript
let socket;

function initTasks() {
    socket = io();
    
    socket.on('connect', function() {
        console.log('Connected to server');
    });
    
    socket.on('task_progress', function(data) {
        updateTaskCard(data);
    });
    
    loadActiveTasks();
    setInterval(loadActiveTasks, 5000);
}

async function loadActiveTasks() {
    try {
        const response = await fetch('/api/tasks');
        const tasks = await response.json();
        
        const activeTasks = tasks.filter(t => 
            ['pending', 'running', 'paused'].includes(t.status)
        );
        
        renderActiveTasks(activeTasks);
    } catch (error) {
        console.error('Failed to load tasks:', error);
    }
}

function renderActiveTasks(tasks) {
    const container = document.getElementById('activeTasks');
    
    if (tasks.length === 0) {
        container.innerHTML = '<p class="text-muted">暂无活动任务</p>';
        return;
    }
    
    container.innerHTML = tasks.map(task => createTaskCard(task)).join('');
}

function createTaskCard(task) {
    const progress = task.total_tiles > 0 
        ? Math.round((task.downloaded_tiles / task.total_tiles) * 100) 
        : 0;
    
    return `
        <div class="task-card ${task.status}" id="task-${task.id}">
            <div class="d-flex justify-content-between align-items-start">
                <div>
                    <h6>${task.name}</h6>
                    <span class="badge bg-${getStatusColor(task.status)}">${getStatusText(task.status)}</span>
                </div>
                <div class="btn-group btn-group-sm">
                    ${task.status === 'pending' ? `
                        <button class="btn btn-success" onclick="startTask(${task.id})">启动</button>
                    ` : ''}
                    ${task.status === 'running' ? `
                        <button class="btn btn-warning" onclick="pauseTask(${task.id})">暂停</button>
                    ` : ''}
                    ${task.status === 'paused' ? `
                        <button class="btn btn-success" onclick="resumeTask(${task.id})">恢复</button>
                    ` : ''}
                    <button class="btn btn-danger" onclick="cancelTask(${task.id})">取消</button>
                </div>
            </div>
            
            <div class="progress mt-2" style="height: 25px;">
                <div class="progress-bar" role="progressbar" 
                     style="width: ${progress}%" 
                     aria-valuenow="${progress}" 
                     aria-valuemin="0" 
                     aria-valuemax="100">
                    ${progress}%
                </div>
            </div>
            
            <div class="progress-detail">
                已下载: ${task.downloaded_tiles} / ${task.total_tiles} 瓦片
                ${task.failed_tiles > 0 ? `<span class="text-danger">| 失败: ${task.failed_tiles}</span>` : ''}
            </div>
        </div>
    `;
}

function updateTaskCard(task) {
    const card = document.getElementById(`task-${task.id}`);
    if (card) {
        const parent = card.parentElement;
        card.outerHTML = createTaskCard(task);
    }
}

function getStatusColor(status) {
    const colors = {
        'pending': 'secondary',
        'running': 'primary',
        'paused': 'warning',
        'completed': 'success',
        'failed': 'danger',
        'cancelled': 'dark'
    };
    return colors[status] || 'secondary';
}

function getStatusText(status) {
    const texts = {
        'pending': '等待中',
        'running': '运行中',
        'paused': '已暂停',
        'completed': '已完成',
        'failed': '失败',
        'cancelled': '已取消'
    };
    return texts[status] || status;
}

async function startTask(taskId) {
    try {
        const response = await fetch(`/api/tasks/${taskId}/start`, {
            method: 'POST'
        });
        if (response.ok) {
            loadActiveTasks();
        }
    } catch (error) {
        alert('启动任务失败: ' + error.message);
    }
}

async function pauseTask(taskId) {
    try {
        const response = await fetch(`/api/tasks/${taskId}/pause`, {
            method: 'POST'
        });
        if (response.ok) {
            loadActiveTasks();
        }
    } catch (error) {
        alert('暂停任务失败: ' + error.message);
    }
}

async function resumeTask(taskId) {
    try {
        const response = await fetch(`/api/tasks/${taskId}/resume`, {
            method: 'POST'
        });
        if (response.ok) {
            loadActiveTasks();
        }
    } catch (error) {
        alert('恢复任务失败: ' + error.message);
    }
}

async function cancelTask(taskId) {
    if (!confirm('确定要取消这个任务吗？')) {
        return;
    }
    
    try {
        const response = await fetch(`/api/tasks/${taskId}/cancel`, {
            method: 'POST'
        });
        if (response.ok) {
            loadActiveTasks();
        }
    } catch (error) {
        alert('取消任务失败: ' + error.message);
    }
}
```

- [ ] **Step 4: 提交**

```bash
git add templates/index.html static/js/
git commit -m "feat: add main page with map and task management"
```

---


## Task 13: 历史记录页面

**Files:**
- Create: `templates/history.html`
- Create: `static/js/history.js`

- [ ] **Step 1: 创建 templates/history.html**

```html
{% extends "base.html" %}

{% block title %}历史记录 - Google Maps 下载器{% endblock %}

{% block content %}
<div class="row">
    <div class="col-12">
        <h3>下载历史</h3>
        
        <div class="card mb-3">
            <div class="card-header">
                <h5>历史区域地图</h5>
            </div>
            <div class="card-body">
                <div id="historyMap" class="history-map"></div>
            </div>
        </div>
        
        <div class="card">
            <div class="card-header d-flex justify-content-between align-items-center">
                <h5>任务列表</h5>
                <div>
                    <input type="text" class="form-control form-control-sm" id="searchInput" placeholder="搜索任务...">
                </div>
            </div>
            <div class="card-body">
                <div class="table-responsive">
                    <table class="table table-hover">
                        <thead>
                            <tr>
                                <th>ID</th>
                                <th>名称</th>
                                <th>状态</th>
                                <th>区域</th>
                                <th>缩放级别</th>
                                <th>样式</th>
                                <th>瓦片数</th>
                                <th>完成时间</th>
                                <th>操作</th>
                            </tr>
                        </thead>
                        <tbody id="historyTableBody">
                            <tr>
                                <td colspan="9" class="text-center">加载中...</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
                
                <nav>
                    <ul class="pagination justify-content-center" id="pagination">
                    </ul>
                </nav>
            </div>
        </div>
    </div>
</div>
{% endblock %}

{% block extra_js %}
<script src="{{ url_for('static', filename='js/history.js') }}"></script>
<script>
    initHistory();
</script>
{% endblock %}
```

- [ ] **Step 2: 创建 static/js/history.js**

```javascript
let historyMap;
let currentPage = 1;
let allTasks = [];

function initHistory() {
    initHistoryMap();
    loadHistory(1);
    
    document.getElementById('searchInput').addEventListener('input', function(e) {
        filterTasks(e.target.value);
    });
}

function initHistoryMap() {
    historyMap = L.map('historyMap').setView([39.9, 116.4], 5);
    
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '© OpenStreetMap contributors'
    }).addTo(historyMap);
}

async function loadHistory(page = 1) {
    try {
        const response = await fetch(`/api/history?page=${page}&per_page=20`);
        const data = await response.json();
        
        allTasks = data.tasks;
        renderHistoryTable(data.tasks);
        renderPagination(data.page, Math.ceil(data.total / data.per_page));
        renderHistoryMap(data.tasks);
    } catch (error) {
        console.error('Failed to load history:', error);
        document.getElementById('historyTableBody').innerHTML = 
            '<tr><td colspan="9" class="text-center text-danger">加载失败</td></tr>';
    }
}

function renderHistoryTable(tasks) {
    const tbody = document.getElementById('historyTableBody');
    
    if (tasks.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" class="text-center">暂无历史记录</td></tr>';
        return;
    }
    
    tbody.innerHTML = tasks.map(task => `
        <tr>
            <td>${task.id}</td>
            <td>${task.name}</td>
            <td><span class="badge bg-${getStatusColor(task.status)}">${getStatusText(task.status)}</span></td>
            <td>
                <small>
                    N:${task.north.toFixed(4)}, S:${task.south.toFixed(4)}<br>
                    E:${task.east.toFixed(4)}, W:${task.west.toFixed(4)}
                </small>
            </td>
            <td>${task.zoom_min}-${task.zoom_max}</td>
            <td>${getStyleText(task.style)}</td>
            <td>${task.downloaded_tiles}/${task.total_tiles}</td>
            <td><small>${formatDate(task.completed_at)}</small></td>
            <td>
                <button class="btn btn-sm btn-info" onclick="viewTaskDetails(${task.id})">详情</button>
                <button class="btn btn-sm btn-danger" onclick="deleteTask(${task.id})">删除</button>
            </td>
        </tr>
    `).join('');
}

function renderPagination(currentPage, totalPages) {
    const pagination = document.getElementById('pagination');
    
    let html = '';
    
    if (currentPage > 1) {
        html += `<li class="page-item"><a class="page-link" href="#" onclick="loadHistory(${currentPage - 1}); return false;">上一页</a></li>`;
    }
    
    for (let i = Math.max(1, currentPage - 2); i <= Math.min(totalPages, currentPage + 2); i++) {
        html += `<li class="page-item ${i === currentPage ? 'active' : ''}">
            <a class="page-link" href="#" onclick="loadHistory(${i}); return false;">${i}</a>
        </li>`;
    }
    
    if (currentPage < totalPages) {
        html += `<li class="page-item"><a class="page-link" href="#" onclick="loadHistory(${currentPage + 1}); return false;">下一页</a></li>`;
    }
    
    pagination.innerHTML = html;
}

function renderHistoryMap(tasks) {
    historyMap.eachLayer(layer => {
        if (layer instanceof L.Rectangle) {
            historyMap.removeLayer(layer);
        }
    });
    
    tasks.forEach(task => {
        const bounds = [[task.south, task.west], [task.north, task.east]];
        const color = task.status === 'completed' ? 'green' : 
                     task.status === 'failed' ? 'red' : 'orange';
        
        const rectangle = L.rectangle(bounds, {
            color: color,
            weight: 2,
            fillOpacity: 0.2
        }).addTo(historyMap);
        
        rectangle.bindPopup(`
            <strong>${task.name}</strong><br>
            状态: ${getStatusText(task.status)}<br>
            瓦片: ${task.downloaded_tiles}/${task.total_tiles}
        `);
    });
    
    if (tasks.length > 0) {
        const allBounds = tasks.map(t => [[t.south, t.west], [t.north, t.east]]);
        const group = L.featureGroup(allBounds.map(b => L.rectangle(b)));
        historyMap.fitBounds(group.getBounds());
    }
}

function filterTasks(searchTerm) {
    const filtered = allTasks.filter(task => 
        task.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
        task.id.toString().includes(searchTerm)
    );
    renderHistoryTable(filtered);
}

function getStatusColor(status) {
    const colors = {
        'completed': 'success',
        'failed': 'danger',
        'cancelled': 'dark'
    };
    return colors[status] || 'secondary';
}

function getStatusText(status) {
    const texts = {
        'completed': '已完成',
        'failed': '失败',
        'cancelled': '已取消'
    };
    return texts[status] || status;
}

function getStyleText(style) {
    const styles = {
        'm': '标准',
        's': '卫星',
        'y': '卫星+标注',
        'h': '道路',
        't': '地形'
    };
    return styles[style] || style;
}

function formatDate(dateStr) {
    if (!dateStr) return '-';
    const date = new Date(dateStr);
    return date.toLocaleString('zh-CN');
}

async function viewTaskDetails(taskId) {
    try {
        const response = await fetch(`/api/tasks/${taskId}`);
        const task = await response.json();
        
        alert(`任务详情:\n\n` +
              `ID: ${task.id}\n` +
              `名称: ${task.name}\n` +
              `状态: ${getStatusText(task.status)}\n` +
              `总瓦片: ${task.total_tiles}\n` +
              `已下载: ${task.downloaded_tiles}\n` +
              `失败: ${task.failed_tiles}\n` +
              `输出路径: ${task.output_path}`);
    } catch (error) {
        alert('获取任务详情失败');
    }
}

async function deleteTask(taskId) {
    if (!confirm('确定要删除这个任务吗？')) {
        return;
    }
    
    try {
        const response = await fetch(`/api/tasks/${taskId}`, {
            method: 'DELETE'
        });
        
        if (response.ok) {
            alert('任务已删除');
            loadHistory(currentPage);
        } else {
            alert('删除失败');
        }
    } catch (error) {
        alert('删除失败: ' + error.message);
    }
}
```

- [ ] **Step 3: 提交**

```bash
git add templates/history.html static/js/history.js
git commit -m "feat: add history page with map visualization"
```

---

## Task 14: 配置页面

**Files:**
- Create: `templates/config.html`
- Create: `static/js/config.js`

- [ ] **Step 1: 创建 templates/config.html**

```html
{% extends "base.html" %}

{% block title %}配置 - Google Maps 下载器{% endblock %}

{% block content %}
<div class="row">
    <div class="col-md-8 offset-md-2">
        <h3>系统配置</h3>
        
        <form id="configForm">
            <div class="config-section">
                <h5>基础设置</h5>
                
                <div class="mb-3">
                    <label for="default_save_path" class="form-label">默认保存路径</label>
                    <input type="text" class="form-control" id="default_save_path" 
                           value="{{ config.default_save_path }}">
                </div>
                
                <div class="row">
                    <div class="col-md-4 mb-3">
                        <label for="default_style" class="form-label">默认地图样式</label>
                        <select class="form-select" id="default_style">
                            <option value="m" {% if config.default_style == 'm' %}selected{% endif %}>标准</option>
                            <option value="s" {% if config.default_style == 's' %}selected{% endif %}>卫星</option>
                            <option value="y" {% if config.default_style == 'y' %}selected{% endif %}>卫星+标注</option>
                            <option value="h" {% if config.default_style == 'h' %}selected{% endif %}>道路</option>
                            <option value="t" {% if config.default_style == 't' %}selected{% endif %}>地形</option>
                        </select>
                    </div>
                    
                    <div class="col-md-4 mb-3">
                        <label for="default_zoom_min" class="form-label">默认最小缩放</label>
                        <input type="number" class="form-control" id="default_zoom_min" 
                               value="{{ config.default_zoom_min }}" min="0" max="21">
                    </div>
                    
                    <div class="col-md-4 mb-3">
                        <label for="default_zoom_max" class="form-label">默认最大缩放</label>
                        <input type="number" class="form-control" id="default_zoom_max" 
                               value="{{ config.default_zoom_max }}" min="0" max="21">
                    </div>
                </div>
            </div>
            
            <div class="config-section">
                <h5>下载设置</h5>
                
                <div class="row">
                    <div class="col-md-4 mb-3">
                        <label for="concurrent_downloads" class="form-label">并发下载数</label>
                        <input type="number" class="form-control" id="concurrent_downloads" 
                               value="{{ config.concurrent_downloads }}" min="1" max="100">
                    </div>
                    
                    <div class="col-md-4 mb-3">
                        <label for="request_timeout" class="form-label">请求超时(秒)</label>
                        <input type="number" class="form-control" id="request_timeout" 
                               value="{{ config.request_timeout }}" min="1" max="300">
                    </div>
                    
                    <div class="col-md-4 mb-3">
                        <label for="max_retries" class="form-label">最大重试次数</label>
                        <input type="number" class="form-control" id="max_retries" 
                               value="{{ config.max_retries }}" min="0" max="10">
                    </div>
                </div>
                
                <div class="mb-3">
                    <label for="proxy_url" class="form-label">代理服务器 (可选)</label>
                    <input type="text" class="form-control" id="proxy_url" 
                           value="{{ config.proxy_url }}" placeholder="http://proxy.example.com:8080">
                </div>
                
                <div class="mb-3">
                    <label for="tile_servers" class="form-label">瓦片服务器列表</label>
                    <input type="text" class="form-control" id="tile_servers" 
                           value="{{ config.tile_servers }}">
                    <small class="form-text text-muted">用逗号分隔多个服务器</small>
                </div>
            </div>
            
            <div class="config-section">
                <h5>缓存设置</h5>
                
                <div class="mb-3">
                    <div class="form-check">
                        <input class="form-check-input" type="checkbox" id="cache_enabled" 
                               {% if config.cache_enabled == 'true' %}checked{% endif %}>
                        <label class="form-check-label" for="cache_enabled">
                            启用瓦片缓存
                        </label>
                    </div>
                </div>
                
                <div class="mb-3">
                    <label for="cache_max_size_mb" class="form-label">缓存最大大小 (MB)</label>
                    <input type="number" class="form-control" id="cache_max_size_mb" 
                           value="{{ config.cache_max_size_mb }}" min="0">
                </div>
            </div>
            
            <div class="config-section">
                <h5>GDAL 设置</h5>
                
                <div class="row">
                    <div class="col-md-6 mb-3">
                        <label for="gdal_compression" class="form-label">压缩方式</label>
                        <select class="form-select" id="gdal_compression">
                            <option value="LZW" {% if config.gdal_compression == 'LZW' %}selected{% endif %}>LZW</option>
                            <option value="DEFLATE" {% if config.gdal_compression == 'DEFLATE' %}selected{% endif %}>DEFLATE</option>
                            <option value="JPEG" {% if config.gdal_compression == 'JPEG' %}selected{% endif %}>JPEG</option>
                            <option value="NONE" {% if config.gdal_compression == 'NONE' %}selected{% endif %}>无压缩</option>
                        </select>
                    </div>
                    
                    <div class="col-md-6 mb-3">
                        <label for="gdal_resampling" class="form-label">重采样算法</label>
                        <select class="form-select" id="gdal_resampling">
                            <option value="nearest" {% if config.gdal_resampling == 'nearest' %}selected{% endif %}>最近邻</option>
                            <option value="bilinear" {% if config.gdal_resampling == 'bilinear' %}selected{% endif %}>双线性</option>
                            <option value="cubic" {% if config.gdal_resampling == 'cubic' %}selected{% endif %}>三次卷积</option>
                            <option value="lanczos" {% if config.gdal_resampling == 'lanczos' %}selected{% endif %}>Lanczos</option>
                        </select>
                    </div>
                </div>
            </div>
            
            <div class="config-section">
                <h5>其他设置</h5>
                
                <div class="mb-3">
                    <label for="history_retention_days" class="form-label">历史记录保留天数</label>
                    <input type="number" class="form-control" id="history_retention_days" 
                           value="{{ config.history_retention_days }}" min="0">
                </div>
                
                <div class="row">
                    <div class="col-md-4 mb-3">
                        <label for="map_center_lat" class="form-label">地图中心纬度</label>
                        <input type="number" class="form-control" id="map_center_lat" 
                               value="{{ config.map_center_lat }}" step="0.1" min="-90" max="90">
                    </div>
                    
                    <div class="col-md-4 mb-3">
                        <label for="map_center_lng" class="form-label">地图中心经度</label>
                        <input type="number" class="form-control" id="map_center_lng" 
                               value="{{ config.map_center_lng }}" step="0.1" min="-180" max="180">
                    </div>
                    
                    <div class="col-md-4 mb-3">
                        <label for="map_initial_zoom" class="form-label">地图初始缩放</label>
                        <input type="number" class="form-control" id="map_initial_zoom" 
                               value="{{ config.map_initial_zoom }}" min="0" max="21">
                    </div>
                </div>
            </div>
            
            <div class="d-flex justify-content-between">
                <button type="button" class="btn btn-secondary" onclick="resetConfig()">重置为默认值</button>
                <button type="submit" class="btn btn-primary">保存配置</button>
            </div>
        </form>
    </div>
</div>
{% endblock %}

{% block extra_js %}
<script src="{{ url_for('static', filename='js/config.js') }}"></script>
<script>
    initConfig();
</script>
{% endblock %}
```

- [ ] **Step 2: 创建 static/js/config.js**

```javascript
function initConfig() {
    document.getElementById('configForm').addEventListener('submit', saveConfig);
}

async function saveConfig(e) {
    e.preventDefault();
    
    const configData = {
        default_save_path: document.getElementById('default_save_path').value,
        default_style: document.getElementById('default_style').value,
        default_zoom_min: document.getElementById('default_zoom_min').value,
        default_zoom_max: document.getElementById('default_zoom_max').value,
        concurrent_downloads: document.getElementById('concurrent_downloads').value,
        request_timeout: document.getElementById('request_timeout').value,
        max_retries: document.getElementById('max_retries').value,
        proxy_url: document.getElementById('proxy_url').value,
        tile_servers: document.getElementById('tile_servers').value,
        cache_enabled: document.getElementById('cache_enabled').checked ? 'true' : 'false',
        cache_max_size_mb: document.getElementById('cache_max_size_mb').value,
        gdal_compression: document.getElementById('gdal_compression').value,
        gdal_resampling: document.getElementById('gdal_resampling').value,
        history_retention_days: document.getElementById('history_retention_days').value,
        map_center_lat: document.getElementById('map_center_lat').value,
        map_center_lng: document.getElementById('map_center_lng').value,
        map_initial_zoom: document.getElementById('map_initial_zoom').value
    };
    
    try {
        const response = await fetch('/api/config', {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(configData)
        });
        
        const result = await response.json();
        
        if (response.ok) {
            alert('配置保存成功！');
        } else {
            alert('保存失败: ' + result.error);
        }
    } catch (error) {
        alert('保存失败: ' + error.message);
    }
}

async function resetConfig() {
    if (!confirm('确定要重置所有配置为默认值吗？')) {
        return;
    }
    
    location.reload();
}
```

- [ ] **Step 3: 提交**

```bash
git add templates/config.html static/js/config.js
git commit -m "feat: add configuration page"
```

---

## Task 15: 添加 Leaflet.draw 插件

**Files:**
- Modify: `templates/base.html`

- [ ] **Step 1: 在 base.html 中添加 Leaflet.draw CDN**

在 `<head>` 部分的 Leaflet CSS 后添加：

```html
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/1.0.4/leaflet.draw.css" />
```

在 `<body>` 底部的 Leaflet JS 后添加：

```html
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/1.0.4/leaflet.draw.js"></script>
```

- [ ] **Step 2: 提交**

```bash
git add templates/base.html
git commit -m "feat: add Leaflet.draw plugin for rectangle selection"
```

---

## Task 16: 最终测试和文档

**Files:**
- Create: `README.md`

- [ ] **Step 1: 创建 README.md**

```markdown
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
- **前端:** Leaflet.js, Bootstrap 5, Socket.IO

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

### 查看历史

访问 `/history` 页面查看所有历史下载任务和区域可视化。

### 配置

访问 `/config` 页面修改系统配置。

## 项目结构

```
map-download/
├── app.py                  # Flask 应用入口
├── config.py               # 配置类
├── database.py             # 数据库初始化
├── models/                 # 数据模型
├── services/               # 业务逻辑
│   ├── download_engine.py  # 下载引擎
│   ├── task_manager.py     # 任务管理器
│   └── config_manager.py   # 配置管理器
├── routes/                 # Flask 路由
├── templates/              # HTML 模板
├── static/                 # 静态资源
├── downloads/              # 下载文件目录
├── cache/                  # 瓦片缓存目录
└── data/                   # SQLite 数据库
```

## 注意事项

- Google Maps 服务条款可能禁止批量下载
- 仅用于个人学习和研究
- 大区域高缩放级别下载可能需要数小时
- 确保有足够的磁盘空间

## 许可证

MIT License
```

- [ ] **Step 2: 运行完整测试**

```bash
python app.py
```

访问 http://localhost:5000 并测试：
- 创建任务
- 启动/暂停/恢复任务
- 查看历史记录
- 修改配置

- [ ] **Step 3: 提交**

```bash
git add README.md
git commit -m "docs: add comprehensive README"
```

---

## 自我审查

### 1. 规范覆盖检查

- ✅ 交互式地图选择区域 - Task 12
- ✅ 多种地图样式支持 - Task 12
- ✅ 缩放级别范围下载 - Task 5, 8
- ✅ 实时下载进度监控 - Task 8, 12
- ✅ 任务调度（暂停/恢复/取消） - Task 8, 9
- ✅ 断点续传 - Task 8
- ✅ 下载历史可视化 - Task 13
- ✅ 高级配置 - Task 4, 14
- ✅ 局域网访问 - Task 10
- ✅ GDAL 瓦片拼接 - Task 7
- ✅ 异步并发下载 - Task 6
- ✅ WebSocket 实时通信 - Task 9, 10

### 2. 占位符扫描

无 TBD、TODO 或占位符。

### 3. 类型一致性

- Task 模型在 Task 3 定义，在 Task 8 中使用 - ✅ 一致
- Tile 模型在 Task 3 定义，在 Task 5-8 中使用 - ✅ 一致
- API 路由在 Task 9 定义，前端在 Task 12-14 调用 - ✅ 一致
- SocketIO 事件在 Task 9 定义，Task 12 使用 - ✅ 一致

---

## 执行选项

计划已完成并保存到 `docs/superpowers/plans/2026-05-06-google-maps-downloader.md`。

**两种执行方式：**

**1. Subagent-Driven (推荐)** - 我为每个任务派发新的子代理，任务间进行审查，快速迭代

**2. Inline Execution** - 在当前会话中使用 executing-plans 执行任务，批量执行并设置检查点

你选择哪种方式？

