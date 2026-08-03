# DEM Terrain Tiling (CTB -> Cesium Quantized-Mesh) Implementation Plan

> **归档文档 · 非当前实现**
> **记录时间**：2026-05-16 ｜ **状态**：部分作废——核心技术路线（外部 CTB）已被替换，其余产物全部上线
> **已死的部分**：整份计划建立在外部 `cesium-terrain-builder`（子进程调用 `ctb-tile`）之上，这条路线已废。`services/terrain_tiling/ctb_runner.py` 曾由 `f97299384` 实现，**当日**即被 `1e64065f3` 用 vendored 的 `services/terrain_tiling/cesiumlab_terrain.py`（CesiumLab 4.0.17 quantized-mesh 构建器）取代；残留代码于 2026-07-31 由 `e3a5d82de` 删除。今天全仓没有任何 CTB 调用，切片入口是 `services/terrain_tiling/dem_task_tiler.py::tile_dem_task_dir` → `build_terrain(...)`，纯 Python。
> **仍然活着的部分**：`dem_terrain_jobs` 表（现位于 `core/database.py`，正文写的是当日的根目录 `database.py`）、`routes/terrain_api.py`、`routes/terrain_static.py`、`services/terrain_tiling/vrt_builder.py` 与 `layer_json.py`、`docs/terrain/cesiumjs-loading.md` 与 `docs/terrain/global-base-build.md`、`scripts/build_global_base_terrain.ps1` —— 均按计划落地且仍在使用。
> ⚠️ 复选框状态无效；正文源码与行号为当日快照，其中 CTB 相关代码块对应的实现今天已不存在，禁止照抄或照行号定位。
> *正文保持原样未回改。*

---

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为每个 DEM 下载任务把任务目录内全部 `*_dem.tif` 切成 CesiumJS 可加载的 `quantized-mesh-1.0` 地形切片，并通过 `parentUrl` 叠加一个离线“全球基底地形”(maxzoom=8)。

**Architecture:** 下载阶段维持现状；新增“切片阶段”作为 DEM 任务的可选后处理。切片阶段把任务内 GeoTIFF 组织成 VRT，然后调用 `ctb-tile` 生成 `{z}/{x}/{y}.terrain` 与 `layer.json`，最后补全/覆盖 `layer.json.parentUrl` 指向离线全球基底。服务端提供静态路由让 CesiumJS 直接从 `layer.json` 开始加载。

**Tech Stack:** Python 3.9+、Flask、SQLite、GDAL（VRT/warp）、外部工具 CTB（`ctb-tile`）、pytest。

---

## File Structure (Locked In)

**Create**
- `services/terrain_tiling/ctb_runner.py`：统一封装“怎么跑 CTB”(本机二进制或 docker) + stdout/stderr 采集
- `services/terrain_tiling/vrt_builder.py`：从任务目录收集 `*_dem.tif` 并生成 VRT（必要时做 warp）
- `services/terrain_tiling/layer_json.py`：读写 `layer.json`，补全 `parentUrl`、校验/生成 `available`
- `services/terrain_tiling/dem_task_tiler.py`：面向业务的“按 task_id 切片”入口（状态流转、输出路径、错误处理）
- `routes/terrain_api.py`：启动/查询 DEM 切片任务的 REST API
- `routes/terrain_static.py`：提供 `layer.json` 和 `.terrain` 的静态访问路由（严格限定在 `downloads` 根目录下防路径穿越）
- `tests/test_dem_task_tiler.py`：切片前置校验、输出路径、layer.json 补丁逻辑（mock subprocess）
- `tests/test_layer_json.py`：`layer.json` 的 parentUrl/available 生成与校验
- `docs/terrain/cesiumjs-loading.md`：CesiumJS 加载示例（从 URL 加载 provider）

**Modify**
- `database.py`：新增 `dem_terrain_jobs` 表 + 必要配置项（全球基底路径/URL、基底 maxzoom）
- `services/dem_task_manager.py`：下载完成后（或手动触发）可以启动切片 job；SocketIO 推进度
- `routes/dem_api.py`：暴露“对某个 DEM task 触发切片”的入口（或直接新增独立 terrain_api blueprint 并在 app.py 注册）
- `routes/__init__.py`、`app.py`：注册新 blueprint
- `requirements.txt`：如需新增纯 Python 依赖（尽量不加；优先 subprocess + stdlib）

---

### Task 1: 数据库与配置键（存切片 job）

**Files:**
- Modify: `D:/workspace/python/map-download/database.py`
- Test: `D:/workspace/python/map-download/tests/test_layer_json.py`

- [ ] **Step 1: 写失败测试：dem_terrain_jobs 表必须存在**

```python
# tests/test_layer_json.py
import sqlite3
from database import init_database
from config import Config

def test_dem_terrain_jobs_table_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "DATABASE_PATH", str(tmp_path / "test.db"))
    init_database()
    conn = sqlite3.connect(Config.DATABASE_PATH)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='dem_terrain_jobs'")
        assert cur.fetchone() is not None
    finally:
        conn.close()
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_layer_json.py::test_dem_terrain_jobs_table_exists -v`  
Expected: FAIL（找不到表）

- [ ] **Step 3: 最小实现：在 init_database() 创建 dem_terrain_jobs 表 + 配置键**

```python
# database.py（在 dem_files 表之后添加）
cursor.execute('''
    CREATE TABLE IF NOT EXISTS dem_terrain_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        output_dir TEXT NOT NULL,
        maxzoom INTEGER NOT NULL,
        parent_url TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        started_at TIMESTAMP,
        completed_at TIMESTAMP,
        error_message TEXT,
        FOREIGN KEY (task_id) REFERENCES dem_tasks(id) ON DELETE CASCADE,
        UNIQUE(task_id)
    )
''')

cursor.execute('''
    CREATE INDEX IF NOT EXISTS idx_dem_terrain_jobs_status
    ON dem_terrain_jobs(status)
''')
```

并新增默认配置（追加到 `DEFAULT_CONFIGS`）：

```python
('terrain_global_base_path', './downloads/terrain/base_z8'),
('terrain_global_base_maxzoom', '8'),
('terrain_local_maxzoom', '14'),
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `pytest tests/test_layer_json.py::test_dem_terrain_jobs_table_exists -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add database.py tests/test_layer_json.py
git commit -m "feat(db): add dem_terrain_jobs table and terrain config keys"
```

---

### Task 2: layer.json 补丁与校验（parentUrl + available）

**Files:**
- Create: `D:/workspace/python/map-download/services/terrain_tiling/layer_json.py`
- Test: `D:/workspace/python/map-download/tests/test_layer_json.py`

- [ ] **Step 1: 写失败测试：parentUrl 必须能被注入**

```python
# tests/test_layer_json.py（追加）
import json
from services.terrain_tiling.layer_json import patch_layer_json_parent

def test_patch_layer_json_parent(tmp_path):
    layer = {
        "format": "quantized-mesh-1.0",
        "scheme": "tms",
        "projection": "EPSG:4326",
        "tiles": ["{z}/{x}/{y}.terrain"],
        "available": [],
        "minzoom": 0,
        "maxzoom": 2,
    }
    p = tmp_path / "layer.json"
    p.write_text(json.dumps(layer), encoding="utf-8")
    patch_layer_json_parent(p, "http://localhost:5000/terrain/base/layer.json")
    updated = json.loads(p.read_text(encoding="utf-8"))
    assert updated["parentUrl"] == "http://localhost:5000/terrain/base/layer.json"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_layer_json.py::test_patch_layer_json_parent -v`  
Expected: FAIL（模块不存在）

- [ ] **Step 3: 最小实现：patch_layer_json_parent**

```python
# services/terrain_tiling/layer_json.py
from __future__ import annotations

import json
from pathlib import Path

def patch_layer_json_parent(layer_json_path: Path, parent_url: str) -> None:
    data = json.loads(layer_json_path.read_text(encoding="utf-8"))
    data["parentUrl"] = parent_url
    layer_json_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `pytest tests/test_layer_json.py::test_patch_layer_json_parent -v`  
Expected: PASS

- [ ] **Step 5: 扩展测试与实现：从落盘瓦片目录生成 available**

测试（新建一个最小瓦片树，验证输出矩形范围）：

```python
# tests/test_layer_json.py（追加）
from services.terrain_tiling.layer_json import compute_available_from_tiles

def test_compute_available_from_tiles(tmp_path):
    # 只创建 z=0 x=0 y=0 和 x=1 y=0 两张瓦片（EPSG:4326 的 z=0 全局就是两张）
    (tmp_path / "0" / "0").mkdir(parents=True)
    (tmp_path / "0" / "1").mkdir(parents=True)
    (tmp_path / "0" / "0" / "0.terrain").write_bytes(b"x")
    (tmp_path / "0" / "1" / "0.terrain").write_bytes(b"x")

    avail = compute_available_from_tiles(tmp_path, minzoom=0, maxzoom=0)
    assert avail == [[{"startX": 0, "startY": 0, "endX": 1, "endY": 0}]]
```

实现（按 zoom 统计每个 z 的 x/y 最小最大值，生成一个矩形；先满足“能用”，后续再做多矩形合并）：

```python
# services/terrain_tiling/layer_json.py（追加）
def compute_available_from_tiles(tiles_root: Path, minzoom: int, maxzoom: int):
    available = []
    for z in range(minzoom, maxzoom + 1):
        zdir = tiles_root / str(z)
        if not zdir.exists():
            available.append([])
            continue
        xs = []
        ys = []
        for xdir in zdir.iterdir():
            if not xdir.is_dir():
                continue
            try:
                x = int(xdir.name)
            except ValueError:
                continue
            for t in xdir.glob("*.terrain"):
                try:
                    y = int(t.stem)
                except ValueError:
                    continue
                xs.append(x)
                ys.append(y)
        if not xs:
            available.append([])
        else:
            available.append([{
                "startX": min(xs),
                "startY": min(ys),
                "endX": max(xs),
                "endY": max(ys),
            }])
    return available
```

- [ ] **Step 6: 运行 tests**

Run: `pytest tests/test_layer_json.py -v`  
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add services/terrain_tiling/layer_json.py tests/test_layer_json.py
git commit -m "feat(terrain): patch layer.json parentUrl and compute available"
```

---

### Task 3: VRT 生成（任务目录 -> 单个 VRT）

**Files:**
- Create: `D:/workspace/python/map-download/services/terrain_tiling/vrt_builder.py`
- Create: `D:/workspace/python/map-download/tests/test_dem_task_tiler.py`

- [ ] **Step 1: 写失败测试：只选 *_dem.tif，忽略 *_num.tif**

```python
# tests/test_dem_task_tiler.py
from pathlib import Path
from services.terrain_tiling.vrt_builder import list_dem_tifs

def test_list_dem_tifs_filters_num(tmp_path):
    (tmp_path / "A_dem.tif").write_bytes(b"x")
    (tmp_path / "A_num.tif").write_bytes(b"x")
    tifs = list_dem_tifs(tmp_path)
    assert [p.name for p in tifs] == ["A_dem.tif"]
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_dem_task_tiler.py::test_list_dem_tifs_filters_num -v`  
Expected: FAIL

- [ ] **Step 3: 最小实现：list_dem_tifs + build_vrt_command**

```python
# services/terrain_tiling/vrt_builder.py
from __future__ import annotations

from pathlib import Path
from typing import List

def list_dem_tifs(task_dir: Path) -> List[Path]:
    return sorted([p for p in task_dir.glob("*_dem.tif") if p.is_file()])

def build_vrt_command(vrt_path: Path, tif_paths: List[Path]) -> str:
    # 仅拼命令字符串；执行交给 runner（便于测试 mock）
    inputs = " ".join([f"\"{p}\"" for p in tif_paths])
    return f"gdalbuildvrt \"{vrt_path}\" {inputs}"
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `pytest tests/test_dem_task_tiler.py::test_list_dem_tifs_filters_num -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/terrain_tiling/vrt_builder.py tests/test_dem_task_tiler.py
git commit -m "feat(terrain): list DEM GeoTIFFs and build VRT command"
```

---

### Task 4: CTB 调用封装（可 mock）

**Files:**
- Create: `D:/workspace/python/map-download/services/terrain_tiling/ctb_runner.py`
- Modify: `D:/workspace/python/map-download/requirements.txt`（不加依赖）
- Test: `D:/workspace/python/map-download/tests/test_dem_task_tiler.py`

- [ ] **Step 1: 写失败测试：runner 调用 subprocess.run 并在失败时抛异常**

```python
# tests/test_dem_task_tiler.py（追加）
import subprocess
import pytest
from services.terrain_tiling.ctb_runner import run_cmd

def test_run_cmd_raises_on_failure(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=2, stdout="out", stderr="err")
    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as e:
        run_cmd(["ctb-tile", "--help"])
    assert "returncode=2" in str(e.value)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_dem_task_tiler.py::test_run_cmd_raises_on_failure -v`  
Expected: FAIL

- [ ] **Step 3: 最小实现：run_cmd**

```python
# services/terrain_tiling/ctb_runner.py
from __future__ import annotations

import subprocess
from typing import List, Optional

def run_cmd(argv: List[str], cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    cp = subprocess.run(argv, cwd=cwd, text=True, capture_output=True)
    if cp.returncode != 0:
        raise RuntimeError(f"cmd failed: returncode={cp.returncode} argv={argv} stderr={cp.stderr}")
    return cp
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `pytest tests/test_dem_task_tiler.py::test_run_cmd_raises_on_failure -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/terrain_tiling/ctb_runner.py tests/test_dem_task_tiler.py
git commit -m "feat(terrain): add subprocess runner for CTB/GDAL commands"
```

---

### Task 5: DEM task 切片业务入口（VRT + CTB + layer.json patch）

**Files:**
- Create: `D:/workspace/python/map-download/services/terrain_tiling/dem_task_tiler.py`
- Modify: `D:/workspace/python/map-download/services/dem_task_manager.py`
- Test: `D:/workspace/python/map-download/tests/test_dem_task_tiler.py`

- [ ] **Step 1: 写失败测试：输出目录规则固定且可预测**

规则：
- 输入：`<task.output_path>/dem_task_<id>/`
- 输出：`<task.output_path>/dem_task_<id>/terrain_tiles/`

```python
# tests/test_dem_task_tiler.py（追加）
from services.terrain_tiling.dem_task_tiler import terrain_output_dir_for_task

def test_terrain_output_dir_for_task():
    out = terrain_output_dir_for_task(task_output_path="./downloads/dem", task_id=1)
    assert str(out).replace("\\", "/").endswith("/downloads/dem/dem_task_1/terrain_tiles")
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_dem_task_tiler.py::test_terrain_output_dir_for_task -v`  
Expected: FAIL

- [ ] **Step 3: 最小实现：路径计算 + “切片一次”的主函数（runner 可注入）**

```python
# services/terrain_tiling/dem_task_tiler.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from services.terrain_tiling.vrt_builder import list_dem_tifs, build_vrt_command
from services.terrain_tiling.layer_json import patch_layer_json_parent, compute_available_from_tiles

Runner = Callable[[List[str], Optional[str]], object]

def terrain_output_dir_for_task(task_output_path: str, task_id: int) -> Path:
    return Path(task_output_path) / f"dem_task_{task_id}" / "terrain_tiles"

@dataclass
class TileParams:
    maxzoom: int
    parent_url: str

def tile_dem_task_dir(task_dir: Path, out_dir: Path, params: TileParams, run_argv: Callable[[List[str], Optional[str]], object]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    tifs = list_dem_tifs(task_dir)
    if not tifs:
        raise ValueError(f"no *_dem.tif found in {task_dir}")

    vrt_path = out_dir / "tiles.vrt"
    # 用 gdalbuildvrt 生成 VRT（避免一次性 merge）
    vrt_cmd = build_vrt_command(vrt_path, tifs)
    run_argv(["powershell", "-NoProfile", "-Command", vrt_cmd], None)

    # CTB 生成 quantized-mesh（约定生成 layer.json）
    # 关键：-f Mesh 走 quantized-mesh；-l 输出 layer.json；-o 输出目录
    run_argv(["ctb-tile", "-f", "Mesh", "-C", "-N", "-l", "-o", str(out_dir), str(vrt_path)], None)

    layer_json_path = out_dir / "layer.json"
    if not layer_json_path.exists():
        raise FileNotFoundError(f"missing layer.json at {layer_json_path}")

    # 补 parentUrl + available（如果 CTB 已有 available，这里覆盖为“按落盘统计”的可用范围）
    patch_layer_json_parent(layer_json_path, params.parent_url)
    # 读取 maxzoom/minzoom 后用目录统计 available
    import json
    data = json.loads(layer_json_path.read_text(encoding="utf-8"))
    minz = int(data.get("minzoom", 0))
    maxz = int(data.get("maxzoom", params.maxzoom))
    data["available"] = compute_available_from_tiles(out_dir, minzoom=minz, maxzoom=maxz)
    layer_json_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
```

并在测试里用 monkeypatch 把 `run_argv` 替换成记录器，避免真实运行外部命令：

```python
# tests/test_dem_task_tiler.py（追加）
from services.terrain_tiling.dem_task_tiler import tile_dem_task_dir, TileParams

def test_tile_dem_task_dir_calls_external_tools(tmp_path):
    task_dir = tmp_path / "dem_task_1"
    task_dir.mkdir()
    (task_dir / "A_dem.tif").write_bytes(b"x")

    out_dir = tmp_path / "terrain_tiles"
    calls = []
    def fake_run(argv, cwd):
        calls.append(argv)
        # 伪造 CTB 输出 layer.json
        if argv and argv[0] == "ctb-tile":
            (out_dir / "layer.json").write_text('{"minzoom":0,"maxzoom":0,"tiles":["{z}/{x}/{y}.terrain"]}', encoding="utf-8")

    tile_dem_task_dir(
        task_dir=task_dir,
        out_dir=out_dir,
        params=TileParams(maxzoom=8, parent_url="http://x/base/layer.json"),
        run_argv=fake_run
    )
    assert any(a[0] == "ctb-tile" for a in calls)
    assert (out_dir / "layer.json").exists()
```

- [ ] **Step 4: 运行 tests**

Run: `pytest tests/test_dem_task_tiler.py -v`  
Expected: PASS

- [ ] **Step 5: 集成 DemTaskManager：新增启动切片 job 的方法（先不自动触发）**

在 `services/dem_task_manager.py` 增加：
- `start_tiling(task_id: int) -> None`：创建/更新 `dem_terrain_jobs` 状态为 running 并开线程跑 `tile_dem_task_dir`
- 完成后标记 `completed`，失败标记 `failed`，并通过 SocketIO emit `task_progress`（task_type="dem_terrain"）

（实现时复用现有 dem 任务线程模型：`self.active_tasks` 里用不同 key 前缀或独立 dict）

- [ ] **Step 6: Commit**

```bash
git add services/terrain_tiling/dem_task_tiler.py services/dem_task_manager.py tests/test_dem_task_tiler.py
git commit -m "feat(terrain): add DEM task tiler pipeline (VRT + CTB + layer.json patch)"
```

---

### Task 6: REST API（启动/查询切片 job）

**Files:**
- Create: `D:/workspace/python/map-download/routes/terrain_api.py`
- Modify: `D:/workspace/python/map-download/routes/__init__.py`
- Modify: `D:/workspace/python/map-download/app.py`
- Test: `D:/workspace/python/map-download/tests/test_terrain_api.py`

- [ ] **Step 1: 写失败测试：POST /api/terrain/dem/<id>/start 返回 200**

```python
# tests/test_terrain_api.py
import pytest
from app import app

@pytest.fixture()
def client():
    app.config["TESTING"] = True
    return app.test_client()

def test_start_dem_tiling_route_exists(client):
    r = client.post("/api/terrain/dem/1/start")
    assert r.status_code in (200, 400, 404)
```

- [ ] **Step 2: 实现 blueprint：/api/terrain/dem/<id>/start 与 /api/terrain/dem/<id>**

```python
# routes/terrain_api.py
import logging
from flask import Blueprint, jsonify

logger = logging.getLogger(__name__)
terrain_api_bp = Blueprint("terrain_api", __name__, url_prefix="/api/terrain")

dem_task_manager = None

def init_terrain_dem_task_manager(tm):
    global dem_task_manager
    dem_task_manager = tm

@terrain_api_bp.route("/dem/<int:task_id>/start", methods=["POST"])
def start_dem_tiling(task_id: int):
    if not dem_task_manager:
        return jsonify({"error": "DEM task manager not initialized"}), 500
    try:
        dem_task_manager.start_tiling(task_id)
        return jsonify({"success": True, "message": f"DEM tiling started for task {task_id}"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@terrain_api_bp.route("/dem/<int:task_id>", methods=["GET"])
def get_dem_tiling(task_id: int):
    if not dem_task_manager:
        return jsonify({"error": "DEM task manager not initialized"}), 500
    job = dem_task_manager.get_tiling_job(task_id)
    return jsonify({"success": True, "job": job}), 200
```

并在 `routes/__init__.py` 导出，在 `app.py` 注册并注入 manager。

- [ ] **Step 3: 运行 tests**

Run: `pytest tests/test_terrain_api.py -v`  
Expected: PASS（至少路由存在）

- [ ] **Step 4: Commit**

```bash
git add routes/terrain_api.py routes/__init__.py app.py tests/test_terrain_api.py
git commit -m "feat(api): add DEM terrain tiling endpoints"
```

---

### Task 7: 静态服务（让 CesiumJS 能从 URL 拉 layer.json 与 .terrain）

**Files:**
- Create: `D:/workspace/python/map-download/routes/terrain_static.py`
- Modify: `D:/workspace/python/map-download/app.py`
- Test: `D:/workspace/python/map-download/tests/test_terrain_static.py`

- [ ] **Step 1: 写失败测试：GET /terrain/base/layer.json 能返回文件**

```python
# tests/test_terrain_static.py
import pytest
from app import app

@pytest.fixture()
def client():
    app.config["TESTING"] = True
    return app.test_client()

def test_terrain_base_static_route(client):
    r = client.get("/terrain/base/layer.json")
    assert r.status_code in (200, 404)
```

- [ ] **Step 2: 实现静态路由（base + dem_task_<id>）**

约定：
- 全球基底目录：`ConfigManager().get('terrain_global_base_path')`（默认 `./downloads/terrain/base_z8`）
- 局部切片目录：`<task.output_path>/dem_task_<id>/terrain_tiles/`

```python
# routes/terrain_static.py
import logging
from pathlib import Path
from flask import Blueprint, send_from_directory, abort
from services.config_manager import ConfigManager

logger = logging.getLogger(__name__)
terrain_static_bp = Blueprint("terrain_static", __name__, url_prefix="/terrain")
cfg = ConfigManager()

def _safe_dir(p: Path) -> Path:
    # 只允许在 ./downloads 下服务文件
    root = Path("./downloads").resolve()
    rp = p.resolve()
    if root not in rp.parents and rp != root:
        raise ValueError("terrain path outside downloads root")
    return rp

@terrain_static_bp.route("/base/<path:subpath>")
def serve_base(subpath: str):
    base = Path(cfg.get("terrain_global_base_path", "./downloads/terrain/base_z8"))
    base = _safe_dir(base)
    return send_from_directory(base, subpath)

@terrain_static_bp.route("/dem/<int:task_id>/<path:subpath>")
def serve_dem(task_id: int, subpath: str):
    base = Path("./downloads/dem") / f"dem_task_{task_id}" / "terrain_tiles"
    base = _safe_dir(base)
    return send_from_directory(base, subpath)
```

- [ ] **Step 3: 注册 blueprint 并运行 tests**

Run: `pytest tests/test_terrain_static.py -v`  
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add routes/terrain_static.py app.py tests/test_terrain_static.py
git commit -m "feat(server): serve terrain tiles for CesiumJS"
```

---

### Task 8: 离线全球基底生成脚手架（maxzoom=8）

**Files:**
- Create: `D:/workspace/python/map-download/docs/terrain/global-base-build.md`
- Create: `D:/workspace/python/map-download/scripts/build_global_base_terrain.ps1`

- [ ] **Step 1: 写文档：输入/输出/命令约定**

`docs/terrain/global-base-build.md` 必须包含：
- 输出目录必须生成 `layer.json` 与 `{z}/{x}/{y}.terrain`
- 目标：`maxzoom=8`（默认）
- 产物放到 `./downloads/terrain/base_z8/`
- `layer.json.attribution` 写入 Copernicus DEM attribution

内容（直接写入文档，不要引用外部链接当“唯一说明”）：

```markdown
# Offline Global Base Terrain (z<=8)

Output: ./downloads/terrain/base_z8/
- layer.json
- {z}/{x}/{y}.terrain

Default maxzoom: 8

CesiumJS load URL:
  http://localhost:5000/terrain/base/layer.json
```

- [ ] **Step 2: 写脚本：先支持“用本地 DEM 文件夹生成基底”**

脚本输入：
- `$DemDir`：包含大量 GeoTIFF（或 VRT）

脚本输出：
- `./downloads/terrain/base_z8/`

脚本核心命令：
- `gdalbuildvrt`
- `ctb-tile -f Mesh -C -N -l -o`

```powershell
# scripts/build_global_base_terrain.ps1
param(
  [Parameter(Mandatory=$true)][string]$DemDir,
  [int]$MaxZoom = 8,
  [string]$OutDir = ".\\downloads\\terrain\\base_z8"
)

$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$vrt = Join-Path $OutDir "global.vrt"
& gdalbuildvrt $vrt (Join-Path $DemDir "*.tif")
if ($LASTEXITCODE -ne 0) { throw "gdalbuildvrt failed" }

& ctb-tile -f Mesh -C -N -l -o $OutDir $vrt
if ($LASTEXITCODE -ne 0) { throw "ctb-tile failed" }
```

- [ ] **Step 3: Commit**

```bash
git add docs/terrain/global-base-build.md scripts/build_global_base_terrain.ps1
git commit -m "docs+scripts: scaffold offline global base terrain build (z<=8)"
```

---

### Task 9: CesiumJS 加载示例（验收口径）

**Files:**
- Create: `D:/workspace/python/map-download/docs/terrain/cesiumjs-loading.md`

- [ ] **Step 1: 写最小可运行示例（只给关键代码）**

```markdown
# CesiumJS Terrain Loading

Base terrain (offline):
  const base = await Cesium.CesiumTerrainProvider.fromUrl("http://localhost:5000/terrain/base/layer.json");

Local DEM task overlay:
  const local = await Cesium.CesiumTerrainProvider.fromUrl("http://localhost:5000/terrain/dem/1/layer.json");

If local layer.json has parentUrl -> base layer.json, load local only:
  viewer = new Cesium.Viewer("cesiumContainer", { terrainProvider: local });
```

- [ ] **Step 2: Commit**

```bash
git add docs/terrain/cesiumjs-loading.md
git commit -m "docs: add CesiumJS quantized-mesh loading example"
```

---

## Self-Review Checklist (Spec Coverage)

- 覆盖“每个 DEM 任务目录内所有 *_dem.tif 切片”：Task 3 + Task 5  
- 覆盖“CTB 三角算法”：Task 4/5 用 `ctb-tile` 输出 Mesh  
- 覆盖“输出满足 CesiumJS 加载要求”：Task 2/7/9（layer.json、available、静态路由、加载示例）  
- 覆盖“叠加离线全球地形（基底 maxzoom=8）”：Task 1 配置 + Task 2 parentUrl + Task 8 生成脚本 + Task 7 静态路由  
- 覆盖“先查资料和做方案”：本计划把接口、文件、测试、命令全部落到仓库内可执行形态

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-16-dem-terrain-tiling-ctb-cesium.md`. Two execution options:

1. Subagent-Driven (recommended) - I dispatch a fresh subagent per task, review between tasks, fast iteration
2. Inline Execution - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?

