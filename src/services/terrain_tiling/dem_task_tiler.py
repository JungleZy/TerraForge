from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from src.services.terrain_tiling.base_terrain import (
    ensure_base_unpacked,
    graft_base_into,
    ungraft_base_from,
)
from src.services.terrain_tiling.layer_json import (
    merge_base_availability,
    patch_layer_json_parent,
)
from src.services.terrain_tiling.vrt_builder import list_dem_tifs

logger = logging.getLogger(__name__)


def terrain_output_dir_for_task(task_output_path: str, task_id: int) -> Path:
    return Path(task_output_path) / f"dem_task_{task_id}" / "terrain_tiles"


@dataclass(frozen=True)
class TileParams:
    maxzoom: int
    parent_url: str
    # 65x65 vertex grid: at z14 this samples ~19 m spacing, matching 30 m DEMs
    # (Copernicus GLO-30 / ASTER). estimate_max_level in cesiumlab_terrain.py
    # derives the per-tile interval from tile_size (180/(tile_size-1) deg).
    tile_size: int = 65
    workers: int = 0
    # 三角化后端与误差系数。
    #
    # 后端默认 'grid'：实测 6 个真实 DEM、20 个配置的三轴（墙钟/字节/精度）
    # 支配判定里，'auto' 一个 Pareto 前沿都没进 —— 它多花 2.6~5.9 倍时间，而
    # 省下的体积用「降一级」买要便宜 2.4~3.9 倍。崎岖地形上它 98.8% 的瓦片
    # 本来就选 grid，纯属把同一个产物用 6 倍 CPU 重算一遍。
    # 依据：docs/reference/terrain/tiling-presets-measured.md 第三、四节。
    #
    # ⚠️ CLI（cesiumlab_terrain.main）与全球底图构建脚本仍用 'auto'，那是
    # **有意分叉**：底图覆盖海洋与大片平原（martini 收益最大），且只构建一次，
    # CPU 代价无所谓。不要"顺手统一"这两处，见同文档第八节末尾。
    #
    # max_error_k 在 grid 后端下不参与计算，保留是为了排障时切 'martini' 做对比。
    triangulator: str = "grid"
    max_error_k: float = 0.15
    # 逐顶点法线（oct 编码扩展段）。默认【关】：前端 enableLighting 默认关
    # （static/js/terrain_lighting.js:46-52），而实测法线吃 +35%~+100% 字节、
    # 约 2.1 倍切片时间，几何精度分毫不涨。此前这个开关根本没透传到
    # build_terrain，恒走它的 kwarg 默认 True。
    # ⚠️ 关掉后 layer.json 的 extensions 写成 []，Cesium 的 hasVertexNormals 是
    # provider 级单一标志 —— 光照按钮会静默退化成全球日夜渐变，且连植入的随包
    # 底图自带的法线也一起作废。UI 上必须写明这一点。
    normals: bool = False
    # 档位偏移：精度 +1 / 均衡 0 / 速度 -1，叠加在 maxzoom 上，由 build_terrain
    # 落地（那里是 max_level 唯一的解析点）。取值表住在
    # geo_validation.TILING_QUALITY_OFFSETS，不要在这里抄第二份。
    level_offset: int = 0
    # 进度回调/协作停止透传给 build_terrain（默认 None = 关闭）。放在 params
    # 而不是 tile_dem_task_dir 的独立参数：多个契约测试用 (task_dir, out_dir,
    # params) 三参替身钉住管理器到 tiler 的调用形态，加独立参数会破坏它们。
    progress_cb: Optional[Callable[[int, int], None]] = None
    # stage_cb(phase, fraction)：瓦片循环之前那些耗时阶段（多幅 DEM 物化成单
    # 文件、建金字塔）的进度。不能并进 progress_cb —— 那一段发生在 total 算出来
    # 之前，没有分母（详见 build_terrain 的注释）。
    stage_cb: Optional[Callable[[str, float], None]] = None
    stop_flag: Optional[threading.Event] = None


def tile_dem_task_dir(
    task_dir: Path,
    out_dir: Path,
    params: TileParams,
    build_terrain_fn: Optional[Callable[..., None]] = None,
) -> dict:
    """切片一个 DEM 任务目录，返回 build_terrain 的计数 dict。

    Returns:
        {"total", "rendered", "failed", "max_level", "chose_martini",
        "chose_grid"}。max_level 是**实际**切到的最深层级（档位偏移后可能不等于
        请求的 maxzoom）。

        后两个是逐瓦片择优的落点统计（哪个三角化后端赢了）。⚠️ **应用侧读它没有
        排障价值**：TileParams.triangulator 恒为 'grid'（非 auto 分支里
        cesiumlab_terrain._worker_tile 直接 backend = triangulator），于是
        (chose_martini, chose_grid) 无条件等于 (0, rendered)，与地形是山地还是
        平原毫无关系。只有直接调 build_terrain 并传 triangulator='auto' 时，
        「全 grid = 粗糙地形 / 全 martini = 平缓地形」那套读法才成立。

        M11 之前这里丢弃返回值
        （签名 `-> None`），于是 build_terrain 的逐瓦片容错（异常只记 warning）
        变成纯静默：缺瓦片的作业照报 completed，layer.json 还按完整矩形声明
        available，Cesium 请求后拿 404 且父层不兜底。极端情况下所有瓦片都失败、
        terrain_tiles/ 一片没有，job 仍标 completed。

        注入的 build_terrain_fn 返回 None（老测试替身）时计数归一成全 0，
        调用方按「无计数信息」处理，行为与改动前一致；但 max_level 归一成
        **None** 而不是 0 —— 0 是合法层级，拿它当「未知」会让调用方把
        `effective_maxzoom` 落成 0。
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    dem_tifs = list_dem_tifs(task_dir)
    if not dem_tifs:
        raise ValueError(f"No DEM tifs found under {task_dir}")

    # Use cesiumlab_terrain.py as the source of truth for tiling behavior.
    # Import lazily so unit tests can inject a stub without requiring numpy/GDAL.
    if build_terrain_fn is None:
        try:
            from src.services.terrain_tiling.cesiumlab_terrain import build_terrain as build_terrain_fn  # type: ignore[assignment]
        except Exception as e:  # pragma: no cover
            raise RuntimeError(
                "Terrain tiling runtime deps missing (need numpy + GDAL bindings). "
                "Install them, or inject build_terrain_fn for tests."
            ) from e

    # 解压排在切片前：首次解压是分钟级，要独占 stage_cb 上报通道，否则和切片
    # 进度抢同一条通道，前端只能看到进度条来回跳。
    try:
        base_dir = ensure_base_unpacked(stage_cb=params.stage_cb)
    except RuntimeError as e:
        # 解压失败 = 底图不可用，退回 parentUrl 级联，**不让整个切片任务失败**。
        # ensure_base_unpacked 把「assets/ 不可写」也包装成 RuntimeError（打包
        # 安装到 Program Files、从只读介质运行都会命中）。不接住的话，v0.2.8 能
        # 正常切片的场景在这版变成整个地形任务失败，而报错文案是「随包底图解压
        # 失败」，用户不会知道这本来可以忽略。
        # 这里退回兜底是干净的：此刻任务目录一个字节都还没被碰过，语义与「分卷
        # 缺失返回 None」一致。graft 阶段失败则必须让任务失败 —— 那时目录里已经
        # 躺着半个底图，缺的瓦片会让 Cesium 拿 404 并把整个 provider 降级成
        # heightmap，比根本没有底图更糟。
        logger.warning(
            f"Terrain: 随包底图不可用（{e}），本次切片退回 parentUrl 级联；"
            f"产出目录不会自包含")
        base_dir = None

    if base_dir is not None:
        # 上一轮植入留下的是**指向共享缓存的硬链接**，而瓦片落盘走
        # Path.write_bytes（就地截断同一 inode）。maxzoom <= 7 的任务自己就要写
        # z0-7，重跑时那一笔会直接改写 assets/terrain/base_z8 里的底图，全局污染
        # 且零信号。必须赶在 build_terrain 之前摘干净。
        ungraft_base_from(out_dir, base_dir)

    # 底图独占 z0-z7，任务只出 z8+：两边零冲突，也没有「半张瓦片是真数据、
    # 半张是采到 DEM 外的外推值」那种接缝。
    # 恒传 8，不再在这里 min(8, maxzoom)：档位偏移后的最终层级只有
    # build_terrain 知道（它可能还要走 estimate），钳位因此挪进了那边
    # （见 cesiumlab_terrain 里 min_level = min(min_level, max_level) 那行）。
    min_level = 8 if base_dir is not None else 0

    counts = build_terrain_fn(
        inputs=[str(p) for p in dem_tifs],
        output_dir=str(out_dir),
        min_level=min_level,
        max_level=int(params.maxzoom),
        tile_size=int(params.tile_size),
        workers=int(params.workers),
        progress_cb=params.progress_cb,
        stage_cb=params.stage_cb,
        stop_flag=params.stop_flag,
        triangulator=params.triangulator,
        max_error_k=params.max_error_k,
        normals=params.normals,
        level_offset=params.level_offset,
    )

    # 注入的 build_terrain_fn 返回 None（老测试替身）时归一成全 0 计数；提前到
    # 这里归一，停止分支与正常分支共用同一个返回值。
    raw = counts if isinstance(counts, dict) else {}
    counts = {k: int(raw.get(k, 0) or 0)
              for k in ("total", "rendered", "failed", "chose_martini", "chose_grid")}
    # ⚠️ max_level 是**层级**不是计数，不能跟着上面那行归零：z0 是合法层级
    # （maxzoom<=1 配 speed 档就会切到 0），拿 0 当「未知」会让调用方把
    # effective_maxzoom 落成一个假的 0，界面上显示 "0 - 0"。缺失一律 None，
    # 调用方据此决定「回落到请求值」还是「显示产物事实」。
    max_level = raw.get("max_level")
    counts["max_level"] = None if max_level is None else int(max_level)

    if params.stop_flag is not None and params.stop_flag.is_set():
        # build_terrain 中途被停了就不要再植底图：graft_base_into 是 4.3 万个
        # 硬链接 / 518 个目录，而 DEM/local terrain 的唯一停止入口是**删除任务**,
        # 输出目录马上就要被 rmtree —— 用户点了删除还得干等一整轮 graft。更糟的
        # 是 graft 失败（磁盘满）会抛出去，把一个用户取消的作业记成 failed，
        # 错误文案还指向随包底图，指错方向。
        # 这个检查也顺带越过了下面的 layer.json 存在性校验：停止时产物本就残缺，
        # 缺 layer.json 不该报成 FileNotFoundError。
        logger.info("Terrain: 切片被停止，跳过底图植入与 availability 合并")
        return counts

    layer_json_path = out_dir / "layer.json"
    if not layer_json_path.is_file():
        raise FileNotFoundError(f"Missing layer.json at {layer_json_path}")

    if base_dir is not None:
        # 植入必须在切片**之后**：graft_base_into 的冲突判断是遍历时读一次的
        # 目录级快照，与切片并发会绕过 skip-if-exists（它 docstring 里的前提）。
        # 失败即任务失败：半个底图会让 Cesium 拿 404 并把整个 provider 降级成
        # heightmap，比根本没有底图更糟。
        graft_base_into(out_dir, base_dir)
        merge_base_availability(layer_json_path, base_dir / "layer.json")
    else:
        patch_layer_json_parent(layer_json_path, params.parent_url)

    return counts
