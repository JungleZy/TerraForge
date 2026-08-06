"""随包底图的启动预热：后台解压 + 进度广播。

**为什么不放进 base_terrain.py**：那个模块刻意不 import numpy / osgeo / flask，
才能在没有 GDAL 的环境里单测（它自己的 docstring 写着这条）。让它认识 socketio
会毁掉这个性质。而且「怎么解压」与「什么时候跑、进度报给谁」本来就是两件事。

**为什么不放进 app_factory.py**：那是组装根，只做接线不放业务逻辑。

状态是进程级单例。解压一次启动最多跑一次，没有第二个消费者需要独立状态。
"""
from __future__ import annotations

import logging
import threading
import time

from src.services.terrain_tiling.base_terrain import (
    base_cache_dir,
    ensure_base_unpacked,
    is_base_ready,
)

logger = logging.getLogger(__name__)

EVENT_NAME = "base_unpack_progress"

# 广播最小间隔（秒）。解压是流式读，_extract_stream 的 readinto 每次调用都触发
# 一次 stage_cb —— 167 MB 下是上万次，不节流会把前端打爆（范本：
# dem_task_manager._PROGRESS_EMIT_MIN_INTERVAL，那里的注释写明「严格时间窗，
# 无『变化必发』豁免」，同样适用）。
# 取 0.5 而不是照抄那边的 1.0：解压是分钟级的一次性过程，切片是小时级长跑，
# 同样的绝对间隔在短过程上显得迟钝。0.5 秒下整个解压最多几百次 emit。
_EMIT_MIN_INTERVAL = 0.5

# 后台解压线程写、socketio 的 connect handler 在请求线程里读 —— 是真实的跨线程
# 访问，锁不是形式主义。
_LOCK = threading.Lock()
_STATE = {"phase": "idle", "fraction": 0.0, "error": None}


def snapshot() -> dict:
    """线程安全地读当前状态。

    返回**副本**：后台线程随时在写，调用方拿着内部 dict 的引用会读到撕裂的状态
    （fraction 已更新而 phase 还没）。connect handler 正是拿它去序列化。
    """
    with _LOCK:
        return dict(_STATE)


def reset_state() -> None:
    """只给测试用：模块级单例在同一个 pytest 进程里跨用例残留。"""
    with _LOCK:
        _STATE.update({"phase": "idle", "fraction": 0.0, "error": None})


def _set(phase: str, fraction: float, error: str | None = None) -> dict:
    """写状态并返回快照（供紧接着的 emit 用，避免二次加锁）。"""
    with _LOCK:
        _STATE["phase"] = phase
        _STATE["fraction"] = fraction
        _STATE["error"] = error
        return dict(_STATE)


def _emit(socketio, payload: dict) -> None:
    """广播状态，吞掉异常。

    一次广播故障（客户端断开等）不该影响解压 —— 与 base_terrain._emit、
    cesiumlab_terrain._gdal_stage_callback 同一条既有约定。
    """
    try:
        socketio.emit(EVENT_NAME, payload)
    except Exception:
        logger.debug("base_unpack 进度广播失败（已忽略）", exc_info=True)


def start_warmup(socketio) -> None:
    """确保底图在解压或已就位。幂等；已就位时连线程都不起。

    已就位是 99% 的启动路径，开销就是 is_base_ready 的三次 iterdir 计数。
    phase == 'running' 时直接返回，不起第二个线程 —— 两个线程会同时抢跨进程锁，
    后到的白等几分钟。phase == 'failed' 时会**重新尝试**：本函数的语义是「确保
    底图在解压或已就位」，不是「一辈子只跑一次」。
    """
    # is_base_ready 是磁盘 IO，放在锁外 —— 持锁做 IO 会把 connect handler 的
    # snapshot() 一起堵住。
    try:
        ready = is_base_ready(base_cache_dir())
    except Exception:
        # 路径解析失败（打包目录异常等）当作没就位，让后台去试并给出真正的原因。
        ready = False

    with _LOCK:
        if _STATE["phase"] == "running":
            return
        # 检查与置位必须在同一个锁里：分成两段的话，两个并发调用都能通过检查，
        # 然后各起一个线程。
        _STATE["phase"] = "ready" if ready else "running"
        _STATE["fraction"] = 1.0 if ready else 0.0
        _STATE["error"] = None
        payload = dict(_STATE)

    _emit(socketio, payload)
    if ready:
        logger.debug("Terrain: 随包底图已就位，跳过预热")
        return

    logger.info("Terrain: 随包底图未就位，启动后台预热")
    socketio.start_background_task(_run, socketio)


def _run(socketio) -> None:
    """后台执行体：解压 + 节流上报。任何异常都转成 failed，绝不冒到线程外。"""
    last_emit = [0.0]

    def stage_cb(_phase, fraction):
        # last_emit 初值 0.0 而 time.monotonic() 是个大数 —— 第一次回调必发，
        # 前端立刻看到进度条而不是等满一个窗口。
        now = time.monotonic()
        if now - last_emit[0] < _EMIT_MIN_INTERVAL:
            return
        last_emit[0] = now
        _emit(socketio, _set("running", float(fraction)))

    try:
        result = ensure_base_unpacked(stage_cb=stage_cb)
    except RuntimeError as e:
        logger.warning(f"Terrain: 随包底图预热失败（{e}）；"
                       f"切片时会重试一次，仍失败则退回 parentUrl 级联")
        _emit(socketio, _set("failed", 0.0, str(e)))
        return
    except Exception as e:
        # 契约是 RuntimeError，但线程里漏出的任何异常都会静默吞掉线程，状态永远
        # 停在 running —— 前端进度条一直转，没有任何东西告诉用户已经没救了。
        logger.exception("Terrain: 底图预热遇到未预期的异常")
        _emit(socketio, _set("failed", 0.0, str(e)))
        return

    if result is None:
        # base_terrain 用 None 表示「找不到分卷」，那不是异常但在用户视角与解压
        # 失败是同一件事：底图不可用。当成成功会让状态栏安静地什么都不显示。
        msg = "找不到随包底图分卷（assets/terrain/base_z8.tar.gz.part*）"
        logger.warning(f"Terrain: {msg}")
        _emit(socketio, _set("failed", 0.0, msg))
        return

    logger.info(f"Terrain: 底图预热完成 {result}")
    _emit(socketio, _set("ready", 1.0))
