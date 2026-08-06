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
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.core.config import Config
from src.core.database import get_connection, utc_now_iso
from src.services.config_manager import ConfigManager
from src.services.geo_validation import validate_zoom
from src.services.task_cleanup import remove_task_dir_if_safe, resolve_stored_output_dir
from src.services.terrain_tiling.dem_task_tiler import TileParams, tile_dem_task_dir
from src.services.terrain_tiling.layer_json import parent_url_if_base_available

logger = logging.getLogger(__name__)

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

    配置键与 DEM 管线共用：config 表的 terrain_base_parent_url（完整 URL，
    见 src/core/database.py DEFAULT_CONFIGS），未配置时回退 localhost:5000 保持
    既有行为。此前两处硬编码，非 5000 端口/反代部署下 parentUrl 必 404（M20）。

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
        cfg.get("terrain_base_parent_url", "") or "http://localhost:5000/terrain/base",
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
        # build_terrain 批间/逐瓦片检查（见 cesiumlab_terrain.py）；当前 cancel
        # 仍只拦 pending（有意折中，API 契约被测试钉住），这里把 flag 传下去
        # 让切片具备中途停的能力，后续开放运行中取消时 set 即可生效。
        self.stop_flags: Dict[int, threading.Event] = {}
        self._state_lock = threading.Lock()
        self._recover_orphan_running_tasks()

    def _recover_orphan_running_tasks(self) -> None:
        """Demote leftover 'running' rows to 'failed'.

        Tiling is a one-shot build_terrain call with no resume model, so a
        leftover 'running' row from a dead process can only be restarted.
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
        except Exception as e:
            logger.error(f"Failed to recover local terrain orphans: {e}")
            conn.rollback()
        finally:
            conn.close()

    def _default_maxzoom(self) -> int:
        raw = self.config.get("terrain_local_maxzoom", "14")
        try:
            return int(raw) if raw is not None else 14
        except Exception:
            return 14

    def create_task_with_files(
        self,
        name: str,
        files: Sequence[UploadFile],
        maxzoom: Optional[int] = None,
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

        if maxzoom is None:
            maxzoom = self._default_maxzoom()
        maxzoom = validate_zoom(maxzoom, "maxzoom")

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
                       total_files, uploaded_files, failed_files, maxzoom, parent_url)
                    VALUES (?, 'pending', '', '', '', ?, 0, 0, ?, ?)
                    """,
                    (name, len(valid), maxzoom, parent_url),
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

    def _mark_failed(self, task_id: int, message: str) -> None:
        """create 阶段「全部上传失败」时把刚建的行标 failed。

        WHERE 带 `status='pending'` 守卫：唯一调用点在 create_task_with_files
        里、任务刚 INSERT 完还没跑起来，本就只可能是 pending。加上它是为了让
        「置 failed 的 UPDATE 一律不得改写终态记录」这条约定在四条管线里没有
        例外（见 tests/test_pipeline_parity.py）。
        """
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE local_terrain_tasks SET status='failed', error_message=?, "
                "completed_at=? WHERE id=? AND status='pending'",
                (message, utc_now_iso(), task_id),
            )
            conn.commit()
        finally:
            conn.close()

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
                    "SELECT status, maxzoom, parent_url "
                    "FROM local_terrain_tasks WHERE id=?",
                    (task_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"Local terrain task {task_id} not found")
                if row["status"] == "running":
                    raise ValueError(f"Local terrain task {task_id} is already running")

                cur.execute(
                    "UPDATE local_terrain_tasks SET status='running', started_at=?, "
                    "completed_at=NULL, error_message=NULL WHERE id=? AND status != 'running'",
                    (utc_now_iso(), task_id),
                )
                if cur.rowcount != 1:
                    raise ValueError(
                        f"Local terrain task {task_id} could not be started "
                        "because its status changed"
                    )
                conn.commit()

                maxzoom = int(row["maxzoom"])
                parent_url = row["parent_url"] or _parent_layer_url()
                # 不信库存路径，从当前 Config.DOWNLOADS_DIR 重算（同 terrain_static
                # 的约定）：冻结 exe 搬迁后旧绝对路径不会把切片写去错的地方。
                task_root = Path(Config.DOWNLOADS_DIR) / "terrain" / f"local_task_{task_id}"
                source_dir = task_root / "source"
                output_dir = task_root / "terrain_tiles"

                stop_flag = threading.Event()
                self.stop_flags[task_id] = stop_flag
                th = threading.Thread(
                    target=self._run_tiling_job,
                    args=(task_id, source_dir, output_dir, maxzoom, parent_url, stop_flag),
                    daemon=True,
                    name=f"LocalTerrainTiling-{task_id}",
                )
                self.active_tasks[task_id] = th
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        self._emit_progress(task_id)
        try:
            th.start()
        except Exception as e:
            # L2: 锁内已把任务置 running 并 commit。线程创建失败后不回补的话,
            # 任务永久停在 running —— 再次 start 被状态检查拒、delete 也被拒,
            # 只能重启进程靠孤儿恢复解开。回补:清登记 + 置 failed。
            with self._state_lock:
                if self.active_tasks.get(task_id) is th:
                    self.active_tasks.pop(task_id, None)
                if self.stop_flags.get(task_id) is stop_flag:
                    self.stop_flags.pop(task_id, None)
            self._mark_running_task_failed(task_id, f"tiling thread failed to start: {e}")
            raise

    def _mark_running_task_failed(self, task_id: int, message: str) -> None:
        """把任务行从 running 回补成 failed（L2 的线程启动失败路径）。

        与 `_mark_failed` 的区别是带 `AND status='running'` 守卫：这条路径只
        用于「已置 running 但线程没起来」，不能误改其它状态的行。
        """
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE local_terrain_tasks SET status='failed', error_message=?, "
                "completed_at=? WHERE id=? AND status='running'",
                (message, utc_now_iso(), task_id),
            )
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to mark local terrain task {task_id} as failed: {e}")
        finally:
            conn.close()

    def _run_tiling_job(
        self, task_id: int, source_dir: Path, output_dir: Path, maxzoom: int, parent_url: str,
        stop_flag: Optional[threading.Event] = None,
    ) -> None:
        try:
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
                                  progress_cb=tiling_progress,
                                  stage_cb=tiling_stage,
                                  stop_flag=stop_flag),
            ) or {}
            # M11: 消费 build_terrain 的失败计数 —— 此前返回值被整个丢弃，
            # 逐瓦片容错变成纯静默（缺瓦片仍报 completed，layer.json 过度声明）。
            rendered = int(counts.get("rendered", 0) or 0)
            failed = int(counts.get("failed", 0) or 0)
            total = int(counts.get("total", 0) or 0)
            if total > 0 and rendered == 0 and not (stop_flag is not None and stop_flag.is_set()):
                raise RuntimeError(
                    f"terrain tiling produced no tiles ({failed}/{total} failed)")
            warning = f"部分地形瓦片切片失败({failed}/{total})" if failed > 0 else None
            if warning:
                logger.warning(f"Local terrain task {task_id}: {warning}")

            conn = get_connection()
            try:
                cur = conn.cursor()
                if stop_flag is not None and stop_flag.is_set():
                    # 中途停止：build_terrain 提前收尾（部分瓦片 + layer.json 已
                    # 落盘），不能报 completed —— 落 cancelled，重跑请重新 start。
                    cur.execute(
                        "UPDATE local_terrain_tasks SET status='cancelled', completed_at=?, "
                        "error_message=NULL WHERE id=? AND status='running'",
                        (utc_now_iso(), task_id),
                    )
                    conn.commit()
                    return
                cur.execute(
                    "UPDATE local_terrain_tasks SET status='completed', completed_at=?, "
                    "error_message=? WHERE id=? AND status='running'",
                    (utc_now_iso(), warning, task_id),
                )
                conn.commit()
            finally:
                conn.close()
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
            logger.error(f"Local terrain tiling failed for task {task_id}: {e}")
            if self.socketio:
                self.socketio.emit(
                    "task_failed",
                    {"task_id": task_id, "task_type": "local_terrain",
                     "status": "failed", "error_message": str(e)},
                )
        finally:
            with self._state_lock:
                if self.active_tasks.get(task_id) is threading.current_thread():
                    self.active_tasks.pop(task_id, None)
                    self.stop_flags.pop(task_id, None)

    def cancel_task(self, task_id: int) -> None:
        """Cancel if not yet tiling; a running tiling job is rejected.

        build_terrain 现在支持 stop_flag 协作停止（批间/逐瓦片检查，见
        cesiumlab_terrain.py），运行中的切片技术上已能中途停（flag 在
        self.stop_flags）；但 cancel 仍只拦 pending 是有意折中 —— 「取消
        运行中任务」的语义（部分瓦片去留、是否自动重跑）没定，且该 API
        契约被测试钉住。后续开放时在 running 分支 set stop_flags[task_id]
        即可生效。"""
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE local_terrain_tasks SET status='cancelled' "
                "WHERE id=? AND status='pending'",
                (task_id,),
            )
            if cur.rowcount == 0:
                cur.execute("SELECT status FROM local_terrain_tasks WHERE id=?", (task_id,))
                r = cur.fetchone()
                if not r:
                    raise ValueError(f"Local terrain task {task_id} not found")
                if r["status"] == "running":
                    raise ValueError(
                        "Tiling is in progress and cannot be interrupted; "
                        "wait for it to finish"
                    )
            conn.commit()
        finally:
            conn.close()

    def delete_task(self, task_id: int, delete_files: bool = True) -> Optional[bool]:
        """Delete a task's DB rows and, unless delete_files=False, its on-disk
        files. Refuses while running：切片虽能经 stop_flag 中途停（见
        cancel_task），但 delete 会 rmtree 整个输出目录，不能跟在写的
        GDAL 进程后面删。Removing the row CASCADEs to the files table; the
        local_task_<id> directory (source uploads + output tiles) is also
        removed so cancelled/failed tasks don't leave large GeoTIFFs behind."""
        conn = get_connection()
        try:
            cur = conn.cursor()
            # 与 start_tiling 同一把 _state_lock 锁内复查 active 线程 + DB 状态
            # (范本: contour_task_manager.delete_task) —— 此前无锁、不查 active
            # 线程,与正在跑的 tiling 线程存在 check-then-act 竞态。
            with self._state_lock:
                active = self.active_tasks.get(task_id)
                if active and active.is_alive():
                    raise ValueError(
                        "Tiling is in progress and cannot be interrupted; "
                        "wait for it to finish before deleting"
                    )
                cur.execute("SELECT status FROM local_terrain_tasks WHERE id=?", (task_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"Local terrain task {task_id} not found")
                if row["status"] == "running":
                    raise ValueError(
                        "Tiling is in progress and cannot be interrupted; "
                        "wait for it to finish before deleting"
                    )
                cur.execute("DELETE FROM local_terrain_tasks WHERE id=?", (task_id,))
                conn.commit()
        finally:
            conn.close()

        # Best-effort directory cleanup after the row is gone.
        # 返回值（M10）：True=已删 / False=护栏拦下或删除出错 / None=调用方没要求
        # 删文件。此前无论哪种情况路由都回 200 {"success": true}，护栏命中时用户
        # 会以为文件已经清掉了。
        if not delete_files:
            return None
        try:
            # 不信库存 output_path，从当前 Config.DOWNLOADS_DIR 重算（同
            # terrain_static 的约定）：冻结 exe 搬迁后库存的旧绝对路径不会让
            # 下面的守卫失效、也不会误删旧位置的目录。
            task_root = Path(Config.DOWNLOADS_DIR) / "terrain" / f"local_task_{task_id}"
            # Guard: only remove inside DOWNLOADS_DIR/terrain.
            terrain_root = (Path(Config.DOWNLOADS_DIR) / "terrain").resolve()
            if task_root.resolve().parent != terrain_root:
                logger.warning(
                    f"Refusing to remove local terrain dir outside "
                    f"{terrain_root}: {task_root}")
                return False
            if task_root.exists():
                shutil.rmtree(task_root, ignore_errors=True)
            return True
        except Exception as e:
            logger.warning(f"Failed to remove local terrain dir for task {task_id}: {e}")
            return False

    def _emit_progress(self, task_id: int) -> None:
        if not self.socketio:
            return
        try:
            task = self.get_task(task_id)
            task["task_type"] = "local_terrain"
            self.socketio.emit("task_progress", task)
        except Exception as e:
            logger.warning(f"Failed to emit local terrain progress for {task_id}: {e}")
