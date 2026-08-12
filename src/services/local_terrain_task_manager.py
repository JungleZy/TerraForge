"""
Local Terrain Task Manager

Creates terrain tiling tasks from user-uploaded GeoTIFF files, backed by
local_terrain_tasks/local_terrain_files tables. Reuses the existing terrain
tiler (tile_dem_task_dir) by saving uploads as *_dem.tif.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from src.contracts.artifact import PIPELINES
from src.contracts.reservation import ResourceKind, ResourceRequest
from src.core.config import Config
from src.core.database import get_connection, utc_now_iso
from src.services import disk_budget
from src.services.config_manager import ConfigManager
from src.services.geo_validation import (AUTO_MAXZOOM, DEFAULT_TILING_QUALITY,
                                         TILING_QUALITY_OFFSETS, coerce_maxzoom,
                                         maxzoom_from_db, maxzoom_to_db,
                                         validate_tiling_quality)
from src.services.resource_scheduler import get_scheduler, plan_tiling_reservation
from src.services.task_cleanup import (fail_stranded_running_task, remove_task_dir_if_safe,
                                       resolve_stored_output_dir)
from src.services.task_logging import open_task_log
from src.services.terrain_tiling.dem_task_tiler import TileParams, tile_dem_task_dir
from src.services.terrain_tiling.layer_json import parent_url_if_base_available
from src.services.terrain_tiling.vrt_builder import list_dem_tifs

logger = logging.getLogger(__name__)

# 管线名从合同表取，不手写字面量（同 dem_task_manager 里那条注释的理由）。
_PIPELINE = PIPELINES[PIPELINES.index('local_terrain')]

# 切片进度的 emit 节流下限（秒）。与 dem_task_manager._PROGRESS_EMIT_MIN_INTERVAL
# 同一量级：瓦片回调每张一次、GDAL 的阶段回调频率更不受控，裸发就是每秒几十次。
_TERRAIN_EMIT_MIN_INTERVAL = 1.0
# 瓦片循环之前那些阶段的中文名，与 dem_task_manager 保持一致。
_TERRAIN_STAGE_LABELS = {"merge": "合并 DEM", "overview": "建金字塔"}

_ALLOWED_EXT = (".tif", ".tiff")
# (original_filename, content): bytes 或带 read() 的文件对象（路由直传
# werkzeug FileStorage 的流）。流式写盘，避免把大上传全量读进内存（M5）。
UploadFile = Tuple[str, Any]

def _parent_layer_url() -> str | None:
    """layer.json 的 parentUrl（级联到全局 base terrain）；base 不可用时返回 None。

    配置键与 DEM 管线共用：config 表的 terrain_base_parent_url（应用内相对路径
    或完整 URL，见 src/core/database.py DEFAULT_CONFIGS），未配置时回退
    `/terrain/base` —— 相对地址由浏览器继承提供 layer.json 的 origin，瓦片走
    5001 专用 origin、换端口、反代、远程访问都不用改配置。此前两处硬编码
    localhost:5000，非 5000 端口/反代部署下 parentUrl 必 404（M20）。

    两道闸门缺一不可（见 layer_json）：目录形式 + base 真的存在。任一不满足
    都是 404，而 Cesium 对 404 的处理是塞假 heightmap 图层并污染共享 builder
    ⇒ 本任务自己的 quantized-mesh 瓦片也按 heightmap 解析，高程全错且不报错。
    全球 base 是可选产物，「没建」是默认装机的常态，所以必须能返回 None。
    """
    cfg = ConfigManager()
    base_dir = resolve_stored_output_dir(
        # 兜底值与 DEFAULT_CONFIGS 逐字一致（旧的 ./downloads/... 会把底图判成
        # 不可用，然后写一个 404 的 parentUrl —— v0.2.8 修过的 heightmap 陷阱）。
        cfg.get("terrain_global_base_path", "./assets/terrain/base_z8"))
    return parent_url_if_base_available(
        cfg.get("terrain_base_parent_url", "") or "/terrain/base",
        base_dir,
    )


def _save_upload(dest: Path, content: Any) -> int:
    """Persist one upload to dest; returns bytes written. File-like content
    is copied in chunks so uploads never materialize fully in memory."""
    if isinstance(content, (bytes, bytearray)):
        dest.write_bytes(content)
        return len(content)
    with open(dest, "wb") as out:
        shutil.copyfileobj(content, out, length=1024 * 1024)
    return dest.stat().st_size


class LocalTerrainTaskManager:
    def __init__(self, socketio=None):
        self.socketio = socketio
        self.config = ConfigManager()
        self.active_tasks: Dict[int, threading.Thread] = {}
        # 切片协作停止标记：随 start_tiling 登记、_run_tiling_job 结束清理。
        # build_terrain 批间/逐瓦片检查（见 cesium_terrain.py），所以运行中的
        # 切片能被叫停 —— 置位的唯一入口是 delete_task。
        self.stop_flags: Dict[int, threading.Event] = {}
        self._state_lock = threading.Lock()
        self._recover_orphan_running_tasks()

    def _recover_orphan_running_tasks(self) -> None:
        """Demote leftover 'running' rows to 'failed'.

        Tiling is a one-shot build_terrain call with no resume model, so a
        leftover 'running' row from a dead process can only be restarted.

        `failed` 是硬终态，所以解释**必须**落进任务自己的日志（§4.5）：用户点开
        任务详情看到的原本是「失败」两个字加一片空白，而日志文件在崩溃那一瞬间
        戛然而止，最后一行是某个瓦片的进度。进程崩溃 / 断电 / 关窗口是这条管线上
        最常发生的真实终态转移，不是边角情况。
        """
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM local_terrain_tasks WHERE status = 'running'")
            ids = [row["id"] for row in cur.fetchall()]
            if ids:
                now = utc_now_iso()
                cur.executemany(
                    "UPDATE local_terrain_tasks SET status='failed', completed_at=?, "
                    "error_message='Process was interrupted before completion; re-upload to retile' "
                    "WHERE id=? AND status='running'",
                    [(now, i) for i in ids],
                )
                conn.commit()
                logger.warning(f"Recovered orphan local terrain tasks (failed): {ids}")
                # 上面那条 warning 只进**全局**日志。落库已提交，所以下面写日志
                # 失败也不影响状态机 —— _log_recovery 自己兜住。
                for tid in ids:
                    self._log_recovery(
                        tid, 'failed',
                        '进程在切片期间退出（崩溃 / 断电 / 关窗口）：重启时发现库里'
                        '还写着 running 而没有任何线程，已判为 failed。切片是一次性的'
                        ' build_terrain 调用，没有断点续跑 —— 上传的源文件如果还在，'
                        '重新起一个切片任务即可。')
        except Exception as e:
            logger.error(f"Failed to recover local terrain orphans: {e}")
            conn.rollback()
        finally:
            conn.close()

    def _log_recovery(self, task_id: int, status: str, note: str) -> None:
        """把一次「启动时孤儿恢复」写进**这个任务自己的**日志。绝不抛。

        绝不抛是硬要求：调用点在 `__init__` 里，一个次要 sink 的环境问题没有
        资格让整个 LocalTerrainTaskManager 构造不出来 —— 那等于一条日志写不动
        就让服务起不来（同 `open_task_log` 类 docstring 的论证）。

        句柄短命（开 → 写 → 关）：`open_task_log` 会摘掉同 (pipeline, task_id)
        的遗留 handler，留着不关会让后续真正跑起来的那一轮写到已轮转走的
        inode。恢复跑在 `__init__`，此刻没有任何任务线程持有句柄，不存在互抢。
        """
        try:
            tlog = open_task_log(_PIPELINE, task_id)
            try:
                tlog.event('terminal', status=status, reason='process_restart')
                tlog.warning('%s', note)
            finally:
                tlog.close()
        except Exception as e:
            logger.warning(
                f"Local terrain task {task_id}: 孤儿恢复日志写入失败（忽略）: {e!r}")

    def _default_maxzoom(self) -> Union[int, str]:
        # 配置值过 coerce_maxzoom 而不是裸 int()：terrain_local_maxzoom 在
        # config_manager._UNCONSTRAINED_KEYS 里，写入侧没有取值规则，
        # PUT /api/config 收得下 99，也收得下任何拼错的字符串。
        # 尤其不能把原始值直接交给 maxzoom_to_db —— 它对越界值静默放行
        # （`int('-1')` = -1，而 -1 正是自动挡的哨兵），配置里一个 '-1'
        # 就会在库里变成一条与「用户真的选了自动」无从分辨的记录。
        # 校验失败不抛：配置是装机默认，一个坏值不该让所有任务都建不起来。
        # 但必须留痕，否则「我明明配了 25」在系统里一处都查不到。
        # （显式传参那条相反：调用方给了非法值必须当场报错，不能静默改写。）
        raw = self.config.get("terrain_local_maxzoom", AUTO_MAXZOOM)
        try:
            value = coerce_maxzoom(raw, "terrain_local_maxzoom")
        except Exception as e:
            logger.warning(
                f"配置 terrain_local_maxzoom={raw!r} 不可用({e})，"
                f"本次改用出厂默认 {AUTO_MAXZOOM!r}")
            return AUTO_MAXZOOM
        # 空值与缺键都算「没配过」→ 出厂默认（自动）。
        return AUTO_MAXZOOM if value is None else value

    def _default_quality(self) -> str:
        # 兜底值与 `database.DEFAULT_CONFIGS` 里的 terrain_quality_preset 逐字
        # 一致：兜底和出厂默认不一致会造出「改了没反应」的假旋钮。
        return (self.config.get("terrain_quality_preset", DEFAULT_TILING_QUALITY)
                or DEFAULT_TILING_QUALITY)

    def _default_vertex_normals(self) -> bool:
        # 布尔配置在库里存的是字符串 'true'/'false'
        # （见 `database.DEFAULT_CONFIGS` 里的 terrain_vertex_normals）。
        return (self.config.get("terrain_vertex_normals", "false") or "false") == "true"

    def _normalize_tiling_params(
        self,
        maxzoom: Optional[Union[int, str]],
        quality: Optional[str],
        vertex_normals: Optional[bool],
    ) -> Tuple[Union[int, str], str, bool]:
        """maxzoom/quality/vertex_normals 的「未传 → 配置默认 → 校验」归一。

        两个创建入口（上传 / 零拷贝 DEM 任务来源）共用：同一份配置不管从哪个
        入口走都必须切出一样的产物（「改了没反应的假旋钮」那条规矩）。
        """
        # 先归一再回落，顺序不能倒：coerce_maxzoom 把 None **与空串**一并收成
        # 「未表态」，先判 None 的话空串会绕过归一直接进落库转换，而那一步拿到
        # 的正是归一后的 None —— `int(None)` 当场 TypeError（500，不是 400）。
        # 走到这里之后是三态里的两态：int 或 'auto'。落库形态的转换留到 INSERT
        # 的绑定参数那一步（maxzoom_to_db）。DEM 侧 start_tiling 同序。
        maxzoom = coerce_maxzoom(maxzoom, "maxzoom")
        if maxzoom is None:
            # _default_maxzoom 的返回值已是归一后的形态，不必再过一次。
            maxzoom = self._default_maxzoom()

        # 档位与法线跟 maxzoom 同形：请求未给就取配置默认。校验落在管理器而
        # 不是路由层 —— maxzoom 的校验也在本函数里（上面那句 coerce_maxzoom，
        # 排在配置回落之前），新参数跟着它放。
        # 拼错的档位当场 ValueError（路由转 400），不静默退回 balanced：
        # 「改了档位重切、产物却一模一样且零报错」是这条路径最难查的假象。
        if quality is None:
            # 报错里点名配置键而不是请求字段（范本 dem_task_manager.start_tiling
            # 里 quality is None 的分支）：
            # 走到这条分支说明用户根本没提交过 quality，说 "quality (...) must be
            # one of" 会把他指到一个不存在的输入上，真正该改的是配置页那一项。
            quality = validate_tiling_quality(self._default_quality(),
                                              "terrain_quality_preset")
        else:
            quality = validate_tiling_quality(quality)
        if vertex_normals is None:
            vertex_normals = self._default_vertex_normals()
        return maxzoom, quality, bool(vertex_normals)

    def create_task_with_files(
        self,
        name: str,
        files: Sequence[UploadFile],
        maxzoom: Optional[Union[int, str]] = None,
        quality: Optional[str] = None,
        vertex_normals: Optional[bool] = None,
    ) -> int:
        """Create a task, persist uploaded tifs, then auto-start tiling.

        files: sequence of (original_filename, content) where content is bytes
        or a file-like object (the route passes werkzeug FileStorage streams;
        they are copied to disk in chunks, never read fully into memory).
        """
        name = (name or "Local Terrain Task").strip() or "Local Terrain Task"

        valid: List[UploadFile] = []
        for original, content in files:
            ext = Path(original or "").suffix.lower()
            if ext not in _ALLOWED_EXT:
                raise ValueError(f"Unsupported file type: {original} (only .tif/.tiff)")
            if isinstance(content, (bytes, bytearray)) and not content:
                raise ValueError(f"Empty file: {original}")
            valid.append((original, content))

        if not valid:
            raise ValueError("No valid .tif/.tiff files uploaded")

        maxzoom, quality, vertex_normals = self._normalize_tiling_params(
            maxzoom, quality, vertex_normals)

        base = Path(Config.DOWNLOADS_DIR) / "terrain"
        parent_url = _parent_layer_url()

        # 上传先全量落盘到任务目录旁的暂存目录,再进 DB 事务。此前 INSERT
        # 隐式 BEGIN 的写事务里逐文件写盘,GB 级上传期间占死 WAL 唯一写者,
        # 其他写方 30s busy_timeout 后 500。现在写事务里只剩毫秒级行写入;
        # 暂存目录与任务目录同盘,事务内 os.replace 改名即就位。
        base.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="local_upload_", dir=base))
        task_root: Optional[Path] = None
        try:
            # (original, stored, size, error)：单文件写盘失败不致命,
            # 记成 failed 行继续(与此前逐文件 INSERT 'failed' 的行为一致)
            staged: List[Tuple[str, str, int, Optional[str]]] = []
            for idx, (original, content) in enumerate(valid, start=1):
                stored = f"upload_{idx}_dem.tif"
                dest = staging / stored
                try:
                    size = _save_upload(dest, content)
                except Exception as e:
                    staged.append((original, stored, 0, str(e)))
                    continue
                if size == 0:
                    raise ValueError(f"Empty file: {original}")
                staged.append((original, stored, size, None))

            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO local_terrain_tasks
                      (name, status, output_path, source_dir, output_dir,
                       total_files, uploaded_files, failed_files, maxzoom,
                       quality, vertex_normals, parent_url)
                       -- vertex_normals 必须显式给值：DEFAULT 是 NULL（= 未知，
                       -- 见 database.py 建表处）。漏掉它，刚建的任务会以「法线
                       -- 未知」示人，而这一刻我们恰恰是知道的。
                    VALUES (?, 'pending', '', '', '', ?, 0, 0, ?, ?, ?, ?)
                    """,
                    (name, len(valid), maxzoom_to_db(maxzoom), quality,
                     1 if vertex_normals else 0, parent_url),
                )
                task_id = cur.lastrowid

                task_root = base / f"local_task_{task_id}"
                source_dir = task_root / "source"
                output_dir = task_root / "terrain_tiles"
                source_dir.mkdir(parents=True, exist_ok=True)

                cur.execute(
                    "UPDATE local_terrain_tasks SET output_path=?, source_dir=?, output_dir=? WHERE id=?",
                    (str(task_root), str(source_dir), str(output_dir), task_id),
                )

                uploaded = 0
                failed = 0
                for original, stored, size, error in staged:
                    dest = source_dir / stored
                    if error is not None:
                        failed += 1
                        cur.execute(
                            """
                            INSERT INTO local_terrain_files
                              (task_id, original_filename, stored_filename, local_path, size_bytes, status, error_message)
                            VALUES (?, ?, ?, ?, ?, 'failed', ?)
                            """,
                            (task_id, original, stored, str(dest), 0, error),
                        )
                        continue
                    os.replace(staging / stored, dest)  # 同盘改名,毫秒级
                    cur.execute(
                        """
                        INSERT INTO local_terrain_files
                          (task_id, original_filename, stored_filename, local_path, size_bytes, status)
                        VALUES (?, ?, ?, ?, ?, 'uploaded')
                        """,
                        (task_id, original, stored, str(dest), size),
                    )
                    uploaded += 1

                cur.execute(
                    "UPDATE local_terrain_tasks SET uploaded_files=?, failed_files=? WHERE id=?",
                    (uploaded, failed, task_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                # 创建中途失败：文件已先落盘，只回滚 DB 不清目录会留残留；SQLite
                # rowid 复用后，残留 tif 会被下个同 id 任务的 list_dem_tifs 扫进
                # 渲染（M12）。best-effort 清掉任务目录（限 DOWNLOADS_DIR 内）。
                if task_root is not None:
                    remove_task_dir_if_safe(task_root)
                raise
            finally:
                conn.close()
        except Exception:
            # 暂存目录里的残留(未走完 DB 事务的部分)一并清掉;best-effort,
            # 清理失败不掩盖原异常。
            shutil.rmtree(staging, ignore_errors=True)
            raise
        # 成功的文件已 os.replace 走,failed 的文件留在暂存目录里(写盘失败
        # 多半只有残件),与空目录一并删除
        shutil.rmtree(staging, ignore_errors=True)

        if uploaded == 0:
            # 全部写盘失败：任务行保留并标记 failed，但残文件没有保留价值，
            # 同样清掉任务目录避免磁盘残留（M12）。
            remove_task_dir_if_safe(task_root)
            self._mark_failed(task_id, "All uploaded files failed to save")
            raise ValueError("All uploaded files failed to save")

        self.start_tiling(task_id)
        return task_id

    def create_task_from_dem_task(
        self,
        name: str,
        dem_task_id: Any,
        maxzoom: Optional[Union[int, str]] = None,
        quality: Optional[str] = None,
        vertex_normals: Optional[bool] = None,
    ) -> int:
        """把已完成的高程下载任务转成一个独立的地形切片任务（零拷贝源）。

        任务行「处理」按钮走的入口。与 contour_task_manager.create_task_from_dem_task
        同一模式：local_terrain_files.local_path 直接指向 DEM 任务目录里的原 tif，
        本任务目录只放产物（terrain_tiles）—— 删本任务只清自己的目录，源 DEM
        任务的数据不受影响；反方向删 DEM 任务后本任务不可重切（start_tiling
        重算源目录时当场报错），与等高线同款取舍。
        """
        from src.services.terrain_tiling.vrt_builder import list_dem_tifs

        name = (name or "Local Terrain Task").strip() or "Local Terrain Task"
        dem_task_id = int(dem_task_id)

        conn = get_connection()
        try:
            dem_row = conn.execute(
                "SELECT status, output_path FROM dem_tasks WHERE id=?",
                (dem_task_id,)).fetchone()
        finally:
            conn.close()
        if not dem_row:
            raise ValueError(f"DEM task {dem_task_id} not found")
        # 与 dem_task_manager.start_tiling 同一道闸门：没下完的任务数据残缺，
        # 在它上面切片会"成功"产出带缺口的地形。
        if dem_row["status"] != "completed":
            raise ValueError(
                f"Cannot use DEM task {dem_task_id} with status "
                f"'{dem_row['status']}'; wait for the download to complete"
            )

        source_dir = (resolve_stored_output_dir(dem_row["output_path"])
                      / f"dem_task_{dem_task_id}")
        tifs = list_dem_tifs(source_dir)
        if not tifs:
            raise ValueError(f"No DEM tifs found under {source_dir}")

        maxzoom, quality, vertex_normals = self._normalize_tiling_params(
            maxzoom, quality, vertex_normals)

        base = Path(Config.DOWNLOADS_DIR) / "terrain"
        parent_url = _parent_layer_url()
        base.mkdir(parents=True, exist_ok=True)

        task_root: Optional[Path] = None
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO local_terrain_tasks
                  (name, status, output_path, source_dir, output_dir,
                   total_files, uploaded_files, failed_files, maxzoom,
                   quality, vertex_normals, parent_url, source_dem_task_id)
                   -- vertex_normals 显式给值的理由同 create_task_with_files：
                   -- NULL = 未知，而这一刻我们是知道的。
                VALUES (?, 'pending', '', ?, '', ?, ?, 0, ?, ?, ?, ?, ?)
                """,
                (name, str(source_dir), len(tifs), len(tifs),
                 maxzoom_to_db(maxzoom), quality,
                 1 if vertex_normals else 0, parent_url, dem_task_id),
            )
            task_id = cur.lastrowid

            # 只建产物目录；源 tif 不拷进来（零拷贝），source/ 目录不存在，
            # start_tiling 按 source_dem_task_id 重算真正的源目录。
            task_root = base / f"local_task_{task_id}"
            output_dir = task_root / "terrain_tiles"
            output_dir.mkdir(parents=True, exist_ok=True)
            cur.execute(
                "UPDATE local_terrain_tasks SET output_path=?, output_dir=? WHERE id=?",
                (str(task_root), str(output_dir), task_id),
            )

            for tif in tifs:
                cur.execute(
                    """
                    INSERT INTO local_terrain_files
                      (task_id, original_filename, stored_filename, local_path, size_bytes, status)
                    VALUES (?, ?, ?, ?, ?, 'uploaded')
                    """,
                    (task_id, tif.name, tif.name, str(tif), tif.stat().st_size),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            # 与 create_task_with_files 同一理由（M12 的 rowid 复用残留）：
            # 建到一半失败要 best-effort 清掉任务目录。
            if task_root is not None:
                remove_task_dir_if_safe(task_root)
            raise
        finally:
            conn.close()

        self.start_tiling(task_id)
        return task_id

    def _mark_failed(self, task_id: int, message: str) -> None:
        """create 阶段「全部上传失败」时把刚建的行标 failed。

        WHERE 带 `status='pending'` 守卫：唯一调用点在 create_task_with_files
        里、任务刚 INSERT 完还没跑起来，本就只可能是 pending。加上它是为了让
        「置 failed 的 UPDATE 一律不得改写终态记录」这条约定在四条管线里没有
        例外（见 tests/test_pipeline_parity.py）。

        终态的原因同样要落进任务自己的日志（§4.5）。这条路径上没有现成的句柄：
        任务在建的过程中就死了，切片线程压根没起来，而开每任务日志是
        `_run_tiling_job` 的第一件事。
        """
        conn = get_connection()
        changed = False
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE local_terrain_tasks SET status='failed', error_message=?, "
                "completed_at=? WHERE id=? AND status='pending'",
                (message, utc_now_iso(), task_id),
            )
            conn.commit()
            changed = bool(cur.rowcount)
        finally:
            conn.close()
        if changed:
            self._log_terminal_failure(task_id, 'upload_failed', message)

    def _log_terminal_failure(self, task_id: int, reason: str, message: str) -> None:
        """把一次「没有 tlog 在场」的 failed 终态写进任务自己的日志。绝不抛。

        两个调用点（`_mark_failed`、`_mark_running_task_failed`）的共同处境是
        切片线程从未开始，所以没有句柄可用；而它们写的都是硬终态。只写全局
        terraforge.log 的话，用户点开任务详情看到的是「失败」两个字加一片空白。

        只在 UPDATE **真的改了行**之后才调：无条件写会让「已判 failed」出现在
        一个其实跑完了的任务的日志里，比不写更糟（同 fail_stranded_running_task
        那几处的口径）。
        """
        try:
            tlog = open_task_log(_PIPELINE, task_id)
            try:
                tlog.event('terminal', status='failed', reason=reason, detail=message)
                tlog.error('本地地形任务终态 failed：%s', message)
            finally:
                tlog.close()
        except Exception as e:
            logger.warning(
                f"Local terrain task {task_id}: 终态日志写入失败（忽略）: {e!r}")

    def get_task(self, task_id: int) -> Dict[str, Any]:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM local_terrain_tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Local terrain task {task_id} not found")
            return dict(row)
        finally:
            conn.close()

    def list_files(self, task_id: int) -> List[Dict[str, Any]]:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM local_terrain_files WHERE task_id = ? ORDER BY id",
                (task_id,),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def list_tasks(self, limit: int = 100, status: Optional[str] = None) -> List[Dict[str, Any]]:
        # SQLite LIMIT -1 = 无上限：<1 或 >100 都回退默认窗口（同 dem 管线约定，M13）。
        limit = int(limit or 100)
        if limit < 1 or limit > 100:
            limit = 100
        conn = get_connection()
        try:
            cur = conn.cursor()
            # status='active' 是路由层契约的特殊值（同 /api/history_all）：
            # 展开成活动三态；其余取值（含 None）维持原行为。
            if status == 'active':
                cur.execute(
                    "SELECT * FROM local_terrain_tasks "
                    "WHERE status IN ('pending','running','paused') "
                    "ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            else:
                cur.execute(
                    "SELECT * FROM local_terrain_tasks ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def start_tiling(self, task_id: int) -> None:
        task_id = int(task_id)
        # 预置成 None：下面 except 里的登记回收要在「异常抛在线程对象构造之前」
        # 的情况下也能安全跑，那时这两个名字还没绑定。reservation 同理 ——
        # 准入之前抛出时它还没有值，而 except 要能无条件把它还回去。
        th = None
        stop_flag = None
        reservation = None
        owner = (_PIPELINE, task_id, 'tiling')
        conn = get_connection()
        try:
            cur = conn.cursor()
            # 检查/更新/登记线程全部在同一把锁内完成（task_manager.start_task 范本），
            # 并发调用时第二个会看到条件 UPDATE 的 rowcount=0 或 status='running'。
            with self._state_lock:
                active_thread = self.active_tasks.get(task_id)
                if active_thread and active_thread.is_alive():
                    raise ValueError(f"Local terrain task {task_id} is already running")

                cur.execute(
                    "SELECT status, maxzoom, quality, vertex_normals, parent_url, "
                    "source_dem_task_id "
                    "FROM local_terrain_tasks WHERE id=?",
                    (task_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"Local terrain task {task_id} not found")
                if row["status"] == "running":
                    raise ValueError(f"Local terrain task {task_id} is already running")

                # 产物与源目录先算出来：下面的磁盘预检要知道写到哪块盘、源有多大。
                # 不信库存路径，从当前 Config.DOWNLOADS_DIR 重算（同 terrain_static
                # 的约定）：冻结 exe 搬迁后旧绝对路径不会把切片写去错的地方。
                task_root = Path(Config.DOWNLOADS_DIR) / "terrain" / f"local_task_{task_id}"
                # 零拷贝来源（create_task_from_dem_task 建的行）：源 tif 不在本
                # 任务目录，按 contour 的同一口径从 DEM 任务的 output_path 重算
                # （不信库存绝对路径，exe 搬迁后照旧能找对）。源任务已被删时
                # 当场报错，不拿一个空目录去切出「成功」的空产物。
                if row["source_dem_task_id"] is not None:
                    dem_row = cur.execute(
                        "SELECT output_path FROM dem_tasks WHERE id=?",
                        (row["source_dem_task_id"],),
                    ).fetchone()
                    if not dem_row:
                        raise ValueError(
                            f"Source DEM task {row['source_dem_task_id']} not found")
                    source_dir = (resolve_stored_output_dir(dem_row["output_path"])
                                  / f"dem_task_{row['source_dem_task_id']}")
                else:
                    source_dir = task_root / "source"
                output_dir = task_root / "terrain_tiles"

                # ---- 磁盘估算(只记录) + 全局配额(真的会拒)。在 UPDATE 成
                # running 之前 ----
                # 位置是刻意的：配额拒绝在 UPDATE 之前时行还停在原状态，不需要
                # 任何回补。（本方法的 except 确实有 rollback 兜着，但那条路依赖
                # 「commit 是锁里最后一行」这个易碎的前提 —— 下面那整段注释讲的
                # 就是它，别再往它身上加负担。）
                source_bytes = 0
                for tif in list_dem_tifs(source_dir):
                    try:
                        source_bytes += tif.stat().st_size
                    except OSError:
                        pass
                # 本地上传的 DEM 什么数据类型都可能（无人机 DSM 常是 Float32），
                # 而 Int16 与 Float32 的物化系数差一倍多（0.78 vs 1.9）。这里判不出
                # 来就按贵的那档报数。真正按波段类型分档的判定在
                # cesium_terrain._source_is_float，那时文件已经要被打开了。
                estimate = disk_budget.estimate_terrain_tiling(
                    source_bytes, float_source=True)
                verdict = disk_budget.check_budget(output_dir, estimate, self.config)
                # 不拦（拦截语义 2026-08 起移除，见 disk_budget 模块 docstring）：
                # 不通过也只记 warning 照常启动，数字留在日志里。
                (logger.info if verdict.ok else logger.warning)(
                    f"Local terrain task {task_id} 磁盘预检（源 "
                    f"{source_bytes / (1024 * 1024):.0f} MiB）：{verdict.reason}")

                scheduler = get_scheduler()
                # 这里**没有**「先按 owner 键回收一张同名凭据」那一步，是刻意的：
                # 那种写法能把一张还在服役的凭据摘掉（缺陷形态见 release_owner 的
                # docstring），而它声称要防的泄漏在这条路径上并不存在 —— 上面的
                # is_alive() 闸门与 status=='running' 闸门证明没有活线程，而线程侧
                # 的归还写在 _run_tiling_job 的 finally 里、**先于**它把自己从
                # active_tasks 摘掉，所以「线程已不在册」蕴含「凭据已归还」。真出现
                # 重复 owner 只可能是另有 bug，那时 reserve 会当场 ValueError 说
                # 清楚，而不是让我们悄悄吊销一份别人正在用的配额。
                # 本管线没有自己的 worker 配置键，直接按调度器的 CPU 上限要，让全局
                # 配额去分（max_cpu_workers 出厂 0 时那个上限就是 min(4, cpu_count)，
                # 与改造前 build_terrain 的自动挡同值）。
                requested_workers = scheduler.limits().get(ResourceKind.CPU_WORKER, 1)
                reservation = scheduler.reserve(
                    owner,
                    plan_tiling_reservation(requested_workers) + [
                        # DISK_BYTES 全额或不给，minimum 必须等于 requested。预留是
                        # 为了让**别的**任务看得见这一份：check_budget 的判决本身
                        # 对并发任务不可见（disk_budget 模块 docstring）。
                        ResourceRequest(kind=ResourceKind.DISK_BYTES,
                                        requested=verdict.required_bytes,
                                        minimum=verdict.required_bytes)],
                )
                if reservation is None:
                    free = scheduler.snapshot()['available']
                    raise ValueError(
                        f"Local terrain task {task_id} cannot start now: the global "
                        f"resource budget is saturated (free task_slot="
                        f"{free.get('task_slot')}, cpu_worker={free.get('cpu_worker')}, "
                        f"gdal_slot={free.get('gdal_slot')}). Wait for a running task "
                        f"to finish, or raise max_concurrent_tasks / max_cpu_workers / "
                        f"max_gdal_slots in Settings.")

                # 法线必须在 UPDATE 之前定下来，因为要跟着写回去。
                # NULL = 这一行没有记录过法线状态（本列出现之前建的任务）。
                # 不能 bool(None) 当 False —— 那是把「未知」静默解释成「关」，
                # 而落地前切片器的默认恰恰是**开**，等于按相反的设定重切。
                # 未知就走配置默认，与 create_task_with_files 里「未传→配置默认」
                # 同一条规矩。
                vertex_normals = (self._default_vertex_normals()
                                  if row["vertex_normals"] is None
                                  else bool(row["vertex_normals"]))
                cur.execute(
                    "UPDATE local_terrain_tasks SET status='running', started_at=?, "
                    # effective_maxzoom 一并清空：上一轮的实际层级是上一轮的产物
                    # 事实，本轮切完之前显示它就是撒谎（DEM 侧的 upsert 同理）。
                    "completed_at=NULL, error_message=NULL, effective_maxzoom=NULL, "
                    # 法线一并写回：本轮真的按这个值烘焙瓦片，行里再留 NULL 就成了
                    # 「产物是已知的、面板却说未知」—— 反方向的同一个谎。
                    # 存量行的 NULL 到此为止，只有再没被切过的行才继续是未知。
                    "vertex_normals=? "
                    "WHERE id=? AND status != 'running'",
                    (utc_now_iso(), 1 if vertex_normals else 0, task_id),
                )
                if cur.rowcount != 1:
                    raise ValueError(
                        f"Local terrain task {task_id} could not be started "
                        "because its status changed"
                    )
                # 哨兵 -1 还原成 None = 自动，直接就是 TileParams.maxzoom 要的
                # 形态（build_terrain 只认 max_level is None 触发按源分辨率估算）。
                maxzoom = maxzoom_from_db(row["maxzoom"])
                # 档位（与上面的法线）必须从库读回：本方法不带参，而「创建即切片」这条
                # 唯一路径正是走它 —— create_task_with_files 末尾直接调
                # start_tiling，档位只在建任务时算过一次、落进了任务行。
                # 不读回的话所有本地任务都用 balanced 切，界面上选的档位形同虚设：
                # 状态照样 completed、全程零报错，只有产物不对。
                # `or DEFAULT_TILING_QUALITY`：存量行的 quality 可能是 NULL。
                quality = validate_tiling_quality(row["quality"] or DEFAULT_TILING_QUALITY)
                # ⚠️ conn.commit() 必须是这把锁里的**最后一行**。从这里到锁尾
                # 的每一步都可能抛：读库值转换（脏库值）、_parent_layer_url()
                # 另开连接读 config 表（database is locked / 磁盘错误）、
                # Path(Config.DOWNLOADS_DIR)（DOWNLOADS_DIR 为 None 时 TypeError）、
                # threading.Thread(...) 构造。任何一处抛在 commit 之后，except 里
                # 的 conn.rollback() 就是 no-op —— status='running' 已经落地而切片
                # 线程根本没起来，行永久卡在 running：再次 start 被状态检查拒，
                # 只能删掉重建，或重启进程靠 _recover_orphan_running_tasks 解开
                # （delete 是通的 —— delete_task_row 按 id 无条件 DELETE）。
                # 下面那个 L2 回补块（`try: th.start()` 的 except）只包
                # th.start()，够不到这段窗口。
                #
                # 推迟 commit 不会自锁：_parent_layer_url() 走的是另一条连接，而
                # get_connection() 开的是 WAL（`database.get_connection` 里那句
                # `PRAGMA journal_mode = WAL`），WAL 下读者不被未提交的写者
                # 阻塞，那条 SELECT 立即返回。

                parent_url = row["parent_url"] or _parent_layer_url()

                stop_flag = threading.Event()
                self.stop_flags[task_id] = stop_flag
                th = threading.Thread(
                    target=self._run_tiling_entry,
                    args=(task_id, source_dir, output_dir, maxzoom, parent_url,
                          stop_flag, quality, vertex_normals, reservation),
                    daemon=True,
                    name=f"LocalTerrainTiling-{task_id}",
                )
                self.active_tasks[task_id] = th
                conn.commit()
        except Exception:
            conn.rollback()
            # commit 自己抛（磁盘满等）时上面两行登记已经落进 dict，而这个线程
            # 永远不会 start()。留着它 delete_task 会被 task_deletion.py 的
            # `thread.ident is None ⇒ 视为在跑` 判据误导，走后台收尾路径空等。
            with self._state_lock:
                if th is not None and self.active_tasks.get(task_id) is th:
                    self.active_tasks.pop(task_id, None)
                if stop_flag is not None and self.stop_flags.get(task_id) is stop_flag:
                    self.stop_flags.pop(task_id, None)
            # 线程没接手，配额就没人还 —— 但只还**本次调用自己申请到的**那一张。
            # 这个 if 之后原本还有一句无条件的 `release_owner(owner)`，它在
            # 「已在运行」那条拒绝路径上是致命的：那时本地的 reservation 还是 None
            # （闸门在 reserve 之前就抛了），那一句却照样按键把**正在跑的那一轮**
            # 的凭据吊销掉 —— 作业继续跑，调度器账上一格不占，全局上界失效。
            # reservation.release() 内部走 _on_release，本来就按身份摘登记，
            # 再补一刀既没必要也不安全。
            if reservation is not None:
                reservation.release()
            raise
        finally:
            conn.close()

        self._emit_progress(task_id)
        try:
            th.start()
        except Exception as e:
            # L2: 锁内已把任务置 running 并 commit。线程创建失败后不回补的话,
            # 任务永久停在 running —— 再次 start 被状态检查拒,只能删掉重建或
            # 重启进程靠孤儿恢复解开（delete 本身不受影响,是通的）。
            # 回补:清登记 + 置 failed。
            with self._state_lock:
                if self.active_tasks.get(task_id) is th:
                    self.active_tasks.pop(task_id, None)
                if self.stop_flags.get(task_id) is stop_flag:
                    self.stop_flags.pop(task_id, None)
            # 同上：只还本次调用申请到的那一张，release() 自己会按身份摘登记。
            if reservation is not None:
                reservation.release()
            self._mark_running_task_failed(task_id, f"tiling thread failed to start: {e}")
            raise

    def _mark_running_task_failed(self, task_id: int, message: str) -> None:
        """把任务行从 running 回补成 failed（L2 的线程启动失败路径）。

        与 `_mark_failed` 的区别是带 `AND status='running'` 守卫：这条路径只
        用于「已置 running 但线程没起来」，不能误改其它状态的行。

        终态原因进任务自己的日志的理由见 `_log_terminal_failure`。
        """
        conn = get_connection()
        changed = False
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE local_terrain_tasks SET status='failed', error_message=?, "
                "completed_at=? WHERE id=? AND status='running'",
                (message, utc_now_iso(), task_id),
            )
            conn.commit()
            changed = bool(cur.rowcount)
        except Exception as e:
            logger.error(f"Failed to mark local terrain task {task_id} as failed: {e}")
        finally:
            conn.close()
        if changed:
            self._log_terminal_failure(task_id, 'tiling_thread_start_failed', message)

    def _run_tiling_entry(
        self, task_id: int, source_dir: Path, output_dir: Path,
        maxzoom: Optional[int], parent_url: str,
        stop_flag: Optional[threading.Event] = None,
        quality: str = DEFAULT_TILING_QUALITY,
        vertex_normals: bool = False, reservation=None,
    ) -> None:
        """切片线程的**真正**入口：配额的归还挂在线程上，不挂在 _run_tiling_job 里。

        TASK_SLOT 泄漏一份就永久少一个全局任务名额（进程重启才清），表现是
        「所有管线突然都起不了任务，日志里一句话都没有」。_run_tiling_job 自己的
        finally 已经归还一次，这一层兜的是它够不着的形态：被替身换掉，或者在
        finally 之外炸出 BaseException。release 幂等，两处都跑不会还两次。
        """
        try:
            self._run_tiling_job(task_id, source_dir, output_dir, maxzoom, parent_url,
                                 stop_flag, quality, vertex_normals,
                                 reservation=reservation)
        finally:
            # 只归还**交接给这个线程的那一张**。按 owner 键归还会在「本线程迟到
            # 收尾、用户已经把下一轮起起来了」的窗口里吊销下一轮的凭据，而那个
            # 窗口可以长达数十秒（fail_stranded_running_task 会新开一条
            # busy_timeout=30s 的 sqlite 连接）。身份比较之下迟到的这次返回 0。
            if reservation is not None:
                get_scheduler().release_owner(
                    (_PIPELINE, task_id, 'tiling'), reservation)

    def _run_tiling_job(
        # maxzoom=None 是自动挡（起切时由 maxzoom_from_db 从哨兵还原），原样
        # 进 TileParams —— build_terrain 按源数据像素尺寸现算基准层级。
        self, task_id: int, source_dir: Path, output_dir: Path,
        maxzoom: Optional[int], parent_url: str,
        stop_flag: Optional[threading.Event] = None,
        quality: str = DEFAULT_TILING_QUALITY,
        vertex_normals: bool = False, reservation=None,
    ) -> None:
        failure = None
        # 每任务日志（§4.5）。open_task_log 永不返回 None、所有方法都不抛。
        tlog = open_task_log(_PIPELINE, task_id)
        # 授予的 CPU 名额必须真的传到进程池：不传的话 build_terrain 走它自己的
        # min(4, cpu_count)，两个地形任务并行就是 8 个重型 worker，全局上限白设。
        granted_workers = reservation.cpu_workers if reservation is not None else 0
        try:
            tlog.event('stage', name='tiling', maxzoom=maxzoom
                       if maxzoom is not None else 'auto', quality=quality,
                       normals=bool(vertex_normals),
                       workers=granted_workers or 'auto',
                       source_dir=str(source_dir), output_dir=str(output_dir))
            # 切片期间的进度。此前这里**一个回调都没传** —— 而任务行的进度条是
            # 按 uploaded_files 算的，上传一结束就写满，于是整个切片过程（可以是
            # 几十分钟）界面上恒显 100%，看不出还在跑。
            emit_state = {"last": float("-inf")}

            def _emit_terrain(payload: Dict[str, Any], edge: bool) -> None:
                now = time.monotonic()
                if not edge and now - emit_state["last"] < _TERRAIN_EMIT_MIN_INTERVAL:
                    return
                emit_state["last"] = now
                socketio = getattr(self, "socketio", None)
                if not socketio:
                    return
                # 这发 emit 被 build_terrain 在瓦片循环 / GDAL 回调里**同步**调用，
                # 抛出会一路穿透把整个作业记成 failed —— 而 GDAL 更把回调抛异常
                # 当成「用户请求中止」（实测产物会被删掉）。与 dem_task_manager
                # 的 U1 同一约定：只记日志。
                try:
                    socketio.emit("terrain_job_progress", dict(
                        payload, task_id=task_id, task_type="local_terrain",
                        status="running"))
                except Exception as e:
                    logger.warning(
                        f"Local terrain task {task_id}: emit progress failed "
                        f"(ignored): {e}")

            def tiling_progress(done: int, total: int) -> None:
                _emit_terrain({"rendered_tiles": done, "total_tiles": total},
                              edge=(total <= 0 or done >= total))

            def tiling_stage(phase: str, fraction: float) -> None:
                _emit_terrain({
                    "stage": phase,
                    "stage_label": _TERRAIN_STAGE_LABELS.get(phase, phase),
                    "stage_fraction": max(0.0, min(1.0, float(fraction))),
                }, edge=(fraction <= 0.0 or fraction >= 1.0))

            # `or {}`：多个契约测试直接 monkeypatch 掉 tile_dem_task_dir 并
            # 返回 None，归一成空计数后行为与改动前一致（不判 failed）。
            counts = tile_dem_task_dir(
                task_dir=source_dir,
                out_dir=output_dir,
                params=TileParams(maxzoom=maxzoom, parent_url=parent_url,
                                  normals=vertex_normals,
                                  workers=granted_workers,
                                  level_offset=TILING_QUALITY_OFFSETS[quality],
                                  progress_cb=tiling_progress,
                                  stage_cb=tiling_stage,
                                  stop_flag=stop_flag,
                                  # 同 dem_task_manager：运行中复查要认出「这份
                                  # DISK_BYTES 预留是本任务自己的」，否则报出的
                                  # 可用空间比真实值小一份自身预算（虚警）。
                                  # 用 reservation.owner 而不是手写
                                  # 元组 —— 手写的那份对不上时 _reserved_by_others
                                  # 只是静默地什么都匹配不到，退回旧的双重计数。
                                  owner=(reservation.owner
                                         if reservation is not None else None)),
            ) or {}
            # M11: 消费 build_terrain 的失败计数 —— 此前返回值被整个丢弃，
            # 逐瓦片容错变成纯静默（缺瓦片仍报 completed，layer.json 过度声明）。
            rendered = int(counts.get("rendered", 0) or 0)
            failed = int(counts.get("failed", 0) or 0)
            total = int(counts.get("total", 0) or 0)
            # 实际切到的最深层级：档位偏移 + 钳位之后的产物事实，与 layer.json
            # 的 maxzoom 同源。None = 切片器没回报（注入的替身），保持库里原值。
            effective_maxzoom = counts.get("max_level")
            if total > 0 and rendered == 0 and not (stop_flag is not None and stop_flag.is_set()):
                raise RuntimeError(
                    f"terrain tiling produced no tiles ({failed}/{total} failed)")
            warning = f"部分地形瓦片切片失败({failed}/{total})" if failed > 0 else None
            if warning:
                logger.warning(f"Local terrain task {task_id}: {warning}")
                tlog.warning('%s', warning)
            tlog.event('tiles', rendered=rendered, failed=failed, total=total,
                       effective_maxzoom=effective_maxzoom
                       if effective_maxzoom is not None else 'unreported')

            if stop_flag is not None and stop_flag.is_set():
                # 中途停止的唯一入口是删除任务（切片没有暂停/恢复语义）——
                # 正常情况下 local_terrain_tasks 行此刻已经不在了，写状态是静默
                # no-op，_emit_tiling_finished 也没有行可更新。但「行一定已经
                # 没了」这个假设不成立：task_deletion.delete_task_row 的 commit
                # 失败分支回滚 DELETE 却【不】回滚停止标志（有意的），于是行还在、
                # 标志已置、这里正常 return —— 没有异常给下面的兜底 except 接。
                # 那种行由 finally 的搁死补偿判 failed。范本见
                # dem_task_manager._run_tiling_job。
                tlog.event('terminal', status='stopped',
                           reason='stop flag set (task deleted)',
                           rendered=rendered, total=total)
                return

            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE local_terrain_tasks SET status='completed', completed_at=?, "
                    # COALESCE：没回报层级时不要把已有值抹成 NULL。
                    "error_message=?, effective_maxzoom=COALESCE(?, effective_maxzoom) "
                    "WHERE id=? AND status='running'",
                    (utc_now_iso(), warning, effective_maxzoom, task_id),
                )
                conn.commit()
            finally:
                conn.close()
            tlog.event('terminal', status='completed', rendered=rendered,
                       failed=failed, total=total, warning=warning or '')
            self._emit_tiling_finished(task_id, "completed")
            if self.socketio:
                # emit 在 completed 落库之后才跑，抛异常会落到兜底 except 把这条
                # 终态记录改写成 failed（M1 同款）—— 自带 try 只记日志。
                try:
                    self.socketio.emit(
                        "task_completed",
                        {"task_id": task_id, "task_type": "local_terrain",
                         "status": "completed", "warning": warning},
                    )
                except Exception as emit_error:
                    logger.warning(
                        f"Local terrain task {task_id}: emit task_completed "
                        f"failed (ignored): {emit_error}")
        except Exception as e:
            # failure 必须在开连接之前先记下：下面这句 get_connection() 自己就会抛
            # （库被锁/磁盘满），抛了就没人再写终态，只剩 finally 的搁死补偿，而它
            # 要拿这个原因写进 error_message。
            failure = e
            # GDAL 的失败到这里已经是一个 Python 异常了 —— 回溯必须进任务日志，
            # 「为什么切片失败」的答案九成在那段回溯里（BuildVRT 丢源、ENOSPC、
            # BrokenProcessPool 各有各的形状）。
            tlog.exception('本地地形切片终态 failed：%s', e)
            tlog.event('terminal', status='failed', reason=str(e))
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE local_terrain_tasks SET status='failed', completed_at=?, "
                    "error_message=? WHERE id=? AND status='running'",
                    (utc_now_iso(), str(e), task_id),
                )
                conn.commit()
            finally:
                conn.close()
            self._emit_tiling_finished(task_id, "failed")
            logger.error(f"Local terrain tiling failed for task {task_id}: {e}")
            if self.socketio:
                self.socketio.emit(
                    "task_failed",
                    {"task_id": task_id, "task_type": "local_terrain",
                     "status": "failed", "error_message": str(e)},
                )
        finally:
            # 配额与线程登记同生共死，所以在同一个 finally 里还。
            if reservation is not None:
                reservation.release()
            with self._state_lock:
                stranded_owner = (
                    self.active_tasks.get(task_id) is threading.current_thread())
                if stranded_owner:
                    self.active_tasks.pop(task_id, None)
                    self.stop_flags.pop(task_id, None)
            if stranded_owner:
                # 行还停在 running 就是搁死了（理由与竞态分析见 helper 的
                # docstring）。盖住两条路：上面那个兜底 except 自己抛出去了，
                # 以及 stop 分支正常 return 而行没被删掉。
                stranded_reason = (f'切片线程异常: {failure}' if failure is not None
                                   else '')
                if fail_stranded_running_task('local_terrain_tasks', task_id,
                                              stranded_reason):
                    # 只在**真的改了行**（running → failed）时补这一笔。那个
                    # helper 只写全局日志，而 §4.5 的门槛是「任何终态都能从
                    # **任务自己的**日志解释原因」—— 没有下面两行，任务日志的
                    # 最后一句是某个瓦片的进度，库里却写着 failed，两份记录当面
                    # 打架。看返回值而不是猜：正常收尾时那条带
                    # `WHERE status='running'` 的 UPDATE 是无害的 no-op，那种
                    # 情况下多写一句「已判 failed」比不写更糟。
                    tlog.event('terminal', status='failed', reason='thread_stranded',
                               detail=stranded_reason or 'worker exited without settling the row')
                    tlog.error(
                        '切片线程退出时任务行仍停在 running，已由兜底判为 failed：%s',
                        stranded_reason or 'worker 没有走到任何终态写入')
            tlog.close()

    def _emit_tiling_finished(self, task_id: int, status: str) -> None:
        """切片收尾时补一发 terrain_job_progress（与 dem_task_manager 同款）。

        没有它，前端 updateTerrainJobProgress 里 `status !== 'running'` 那条
        清空分支永不触发，行上的「切片中 N / N」会一直挂到刷新页面为止。
        """
        socketio = getattr(self, "socketio", None)
        if not socketio:
            return
        try:
            socketio.emit("terrain_job_progress", {
                "task_id": task_id,
                "task_type": "local_terrain",
                "status": status,
            })
        except Exception as emit_error:
            logger.warning(
                f"Local terrain task {task_id}: emit finish failed "
                f"(ignored): {emit_error}")

    def delete_task(self, task_id: int, delete_files: bool = True, on_row_gone=None):
        """删除任务。没在跑就同步删，在跑就置停止标志 + 后台收尾。

        delete_files 默认 True 是本管线的历史约定（另外三条的路由默认 false）。
        前端总是显式传参，改默认值只影响直连 API 的人，不值得为对称制造破坏性变更。

        产物路径不信库存 output_path，从当前 Config.DOWNLOADS_DIR 重算（同
        terrain_static 的约定）：冻结 exe 搬迁后库存的还是旧位置的绝对路径，信它
        的话下面那道 parent 护栏会因为 parent 对不上而一律拒删，delete_files
        静默退化成空操作。

        on_row_gone 由调用方给：清 /terrain/local 静态路由缓存的那个 hook 依赖
        Flask 请求上下文（走 current_app.extensions），放在这里等于让服务层持有
        一个只对路由调用方有效的回调，非路由调用方那里它会静默失效。
        """
        from src.services.task_deletion import delete_task_row

        artifact_dir = None
        if delete_files:
            artifact_dir = Path(Config.DOWNLOADS_DIR) / "terrain" / f"local_task_{task_id}"
            # 第二道护栏：只允许删 DOWNLOADS_DIR/terrain 直下的目录。它与
            # remove_task_dir_if_safe 的通用护栏是两道，都要 —— 通用护栏只认
            # 「别删到 BASE_DIR 之外/根目录」，管不住本管线自己的目录布局。
            # 越界时不把路径交给助手，等价于原实现的「拒删并返回 False」。
            terrain_root = (Path(Config.DOWNLOADS_DIR) / "terrain").resolve()
            if artifact_dir.resolve().parent != terrain_root:
                logger.warning(
                    f"Refusing to remove local terrain dir outside "
                    f"{terrain_root}: {artifact_dir}")
                artifact_dir = None

        return delete_task_row(
            manager=self,
            task_id=task_id,
            table="local_terrain_tasks",
            artifact_dir=artifact_dir,
            on_row_gone=on_row_gone,
        )

    def _emit_progress(self, task_id: int) -> None:
        if not self.socketio:
            return
        try:
            task = self.get_task(task_id)
        except ValueError:
            # 行没了 = 任务已被删除，这不是故障，静默返回。
            #
            # 为什么要显式挡：这一发推送的 payload 是**整行**，里面
            # status='running'（start_tiling 刚提交完才调到这里）。用户在这个
            # 窗口里删掉任务的话，前端收到后既在时间流里找不到这个 key、也不在
            # 活动集里（deleteTask 已经摘干净），于是走 prependStreamRow 把行
            # 插回去（static/js/tasks.js），变成一条永远停在「运行中」、只能刷
            # 新页面才消失的幽灵行 —— 与 contour 渲染进度那条是同一个 bug。
            #
            # 之前靠下面那个宽 except 兜住 get_task 抛的 ValueError，结果对但
            # 机制是意外：把 get_task 改成返回 None 的重构会静悄悄换成
            # TypeError，再把 payload 改成缓存快照就直接漏出幽灵行；而且正常的
            # 并发删除每次都往日志里记一条 warning，是假警报。
            return
        except Exception as e:
            # 取行本身失败（database is locked 等）不该逃出去：唯一调用点
            # start_tiling 里的 `self._emit_progress(task_id)` 在「已置 running、
            # 线程已登记进 active_tasks / stop_flags」之后、
            # L2 回补块（`try: th.start()` 的 except）之前 —— 异常从这里逃出去
            # 谁也接不住，留下的是一个行停在 running、登记里挂着永不启动的线程、
            # 路由却返 500 的任务。上面收窄成 ValueError 是为了把「行没了」和
            # 「取行失败」分开，不是为了把后者放出去。
            logger.warning(
                f"Failed to load local terrain task {task_id} for progress emit: {e}")
            return
        try:
            task["task_type"] = "local_terrain"
            self.socketio.emit("task_progress", task)
        except Exception as e:
            logger.warning(f"Failed to emit local terrain progress for {task_id}: {e}")
