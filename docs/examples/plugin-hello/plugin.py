"""Hello 示例插件：一条**完全离线**的最简管线。

它做的事：把任务区域在某一层级上切成瓦片，给每块写一张纯色 PNG，最后把整个
瓦片目录登记成产物。不联网、不依赖任何第三方库，所以拷到哪台机器上都能跑。

想看的东西都在 `HelloPipeline.run()` 里：
  * 声明式参数（enum / int / bool / str）与它们的缺省值；
  * `ctx.progress()` 报进度、`ctx.log()` / `ctx.log_event()` 写任务日志；
  * `ctx.record_tile_outcome()` 记缺块——示例故意留一块 `NO_DATA`，让你看到
    「已解释的缺块」在界面上长什么样（任务终态是 completed_with_gaps）；
  * `ctx.register_artifact()` 登记产物（路径必须落在 `ctx.output_dir` 内）；
  * 返回 `PluginOutcome`——返回别的东西任务一律判 failed。

真范本在 `src/plugins/builtin/`：mvt_pipeline.py（管线）、tianditu_source.py
（数据源）、gpkg_exporter.py（导出器）、artifact_meta.py（钩子）。
"""

from __future__ import annotations

import json
import struct
import zlib

from src.contracts.artifact import ArtifactKind
from src.contracts.outcome import TileOutcome
from src.contracts.region_tiles import (MAX_ZOOM, MIN_ZOOM,
                                        iter_region_tile_spans)
from src.plugins.protocols import (ParamSchema, ParamSpec, PluginDefinition,
                                   PluginOutcome)
from src.services.disk_budget import DiskEstimate

#: 瓦片边长（像素）。256 是 XYZ 的惯例，改了地图上会错位。
_TILE_PX = 256

#: 一张纯色 PNG 压完大约几百字节，估算按这个数走。
_BYTES_PER_TILE = 512

#: 硬上限。示例是给人看的，不该因为有人填了 z18 就在盘上刷出几百万个文件。
#: 真插件应该把这种上限写进 estimate() 的 detail 里让用户能复核。
_MAX_TILES = 4096

_COLORS = {
    'red': (220, 68, 61),
    'green': (46, 160, 87),
    'blue': (37, 108, 219),
}


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    body = tag + data
    return (struct.pack('>I', len(data)) + body
            + struct.pack('>I', zlib.crc32(body) & 0xFFFFFFFF))


def _solid_png(rgb) -> bytes:
    """纯色 PNG。手写编码器只为了让示例零依赖——真插件请用 Pillow/GDAL。"""
    row = b'\x00' + bytes(rgb) * _TILE_PX
    header = struct.pack('>IIBBBBB', _TILE_PX, _TILE_PX, 8, 2, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n'
            + _png_chunk(b'IHDR', header)
            + _png_chunk(b'IDAT', zlib.compress(row * _TILE_PX, 6))
            + _png_chunk(b'IEND', b''))


def _tiles(region, zoom):
    """区域 + 层级 → 逐块 (x, y)。`iter_region_tile_spans` 是全仓唯一的口径。"""
    for y, x0, x1 in iter_region_tile_spans(region, zoom):
        for x in range(x0, x1 + 1):
            yield x, y


class HelloPipeline:
    """`PipelinePlugin` 协议的三个方法，一个都不能少、参数个数一个都不能错。

    宿主在**加载期**用 `inspect.signature` 比参数个数（`registry._check_method`），
    把 `run(self, ctx)` 写成 `run(self)` 会直接拒载，任务根本建不出来。
    """

    # ---------------------------------------------------------- 参数声明

    def params_schema(self) -> ParamSchema:
        """任务表单。区域（bbox）与任务名由宿主解释，这里只声明插件自己的键。

        注意 `required` 与 `default` 的关系：`validate_params` 的判定是
        `if spec.required and spec.default is None`，所以**只要给了 default，
        required 就永远不会触发**。想要「必填」就别给 default。
        """
        return ParamSchema(specs=(
            ParamSpec(key='zoom', type='int', label='层级',
                      default=4, min=MIN_ZOOM, max=MAX_ZOOM),
            ParamSpec(key='color', type='enum', label='瓦片颜色',
                      default='green', choices=tuple(_COLORS)),
            ParamSpec(key='demo_gap', type='bool', label='演示一块缺块',
                      default=True),
            ParamSpec(key='note', type='str', label='备注',
                      required=False, default=''),
        ))

    # ---------------------------------------------------------- 磁盘估算

    def estimate(self, params, region) -> DiskEstimate:
        """估算只用于磁盘预算与 UI 展示；抛异常宿主按「没有估算」处理，不会失败。"""
        zoom = int(params.get('zoom') or 0)
        total = sum(1 for _ in _tiles(region, zoom))
        size = total * _BYTES_PER_TILE
        return DiskEstimate(
            network_bytes=0, cache_bytes=0, temp_bytes=0,
            output_bytes=size, peak_bytes=size, tile_count=total,
            detail={
                'zoom': zoom,
                'tiles': total,
                'bytes_per_tile': _BYTES_PER_TILE,
                'assumptions': [f'每块纯色 PNG 按 {_BYTES_PER_TILE} B 估',
                                '全程离线，network_bytes 恒为 0'],
            })

    # ---------------------------------------------------------- 运行

    def run(self, ctx) -> PluginOutcome:
        params = ctx.params                      # 只读视图，改不动宿主那份
        zoom = int(params.get('zoom') or 0)
        color = _COLORS[str(params.get('color') or 'green')]
        demo_gap = bool(params.get('demo_gap'))

        plan = list(_tiles(ctx.region, zoom))
        if not plan:
            raise ValueError(f'区域在 z{zoom} 上一块瓦片都没有，请检查四至')
        if len(plan) > _MAX_TILES:
            raise ValueError(
                f'z{zoom} 需要 {len(plan)} 块瓦片，超过示例上限 {_MAX_TILES}；'
                '请把层级调小或把区域框小一点')

        tile_root = ctx.output_dir / 'tiles'
        payload = _solid_png(color)
        ctx.log(f'开始生成 {len(plan)} 块 z{zoom} 占位瓦片 → {tile_root}')
        ctx.log_event('hello_start', zoom=zoom, tiles=len(plan),
                      color=str(params.get('color')), demo_gap=demo_gap)

        gaps = 0
        for done, (x, y) in enumerate(plan, start=1):
            if ctx.stop_requested():
                # 删除即取消：被叫停的一趟不冒充完成，也不产出成品。
                ctx.log('收到停止请求，本趟不产出', 'warning')
                ctx.flush_outcomes()
                return PluginOutcome.PENDING_DECISION
            if demo_gap and done == 1:
                # NO_DATA = 上游明确说这里没有数据，是**已解释**的缺块：
                # 任务可以直接落 completed_with_gaps，不需要问用户。
                # 换成 RETRYABLE_FAILURE / PERMANENT_FAILURE / CACHE_FAILURE 就是
                # 「没交代的洞」，那时应当返回 PENDING_DECISION 等用户决定。
                ctx.record_tile_outcome(zoom, x, y, TileOutcome.NO_DATA)
                gaps += 1
            else:
                path = tile_root / str(zoom) / str(x) / f'{y}.png'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                # success 的语义是「消除缺块行」——补漏成功时把上一趟的洞抹掉。
                ctx.record_tile_outcome(zoom, x, y, TileOutcome.SUCCESS)
            # 进度广播由宿主按 2Hz 节流，这里照实报即可。
            ctx.progress(done, len(plan), 'generate')

        # 攒批记账要在读取结果之前落库一次。
        ctx.flush_outcomes()

        summary = ctx.output_dir / 'summary.json'
        summary.write_text(json.dumps({
            'zoom': zoom,
            'tiles_written': len(plan) - gaps,
            'gaps': gaps,
            'note': str(params.get('note') or ''),
            # RegionSpec 的四至是 bbox_* 字段；.bbox 属性给 (north, south, east, west)
            'bbox': list(ctx.region.bbox),
        }, ensure_ascii=False, indent=2), encoding='utf-8')

        # 登记的路径必须在 ctx.output_dir 内，越界抛 ValueError。
        # `ArtifactKind` 是封闭枚举（xyz_dir/geotiff/mbtiles/terrain_dir/
        # contour_dir/dem_dir），没有「任意文件」这一档，所以 summary.json 只
        # 落在产物目录里、不单独登记；真正登记的是瓦片目录。
        ctx.register_artifact(tile_root, kind=ArtifactKind.XYZ_DIR,
                              has_gaps=bool(gaps), fmt='png',
                              meta={'summary': str(summary), 'zoom': zoom})
        ctx.log(f'完成：{len(plan) - gaps} 块瓦片，{gaps} 块缺块')
        return (PluginOutcome.COMPLETED_WITH_GAPS if gaps
                else PluginOutcome.COMPLETED)


def register() -> PluginDefinition:
    """入口。宿主 import 完 plugin.py 就调它，必须返回 PluginDefinition。"""
    return PluginDefinition(pipeline=HelloPipeline())
