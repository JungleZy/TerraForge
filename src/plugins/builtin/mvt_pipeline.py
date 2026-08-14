"""MVT 矢量瓦片下载 → MBTiles（`metadata.format=pbf`）。

边界（规格 §7.2）：TileJSON 探测 + 按区域下载 PBF/MVT + 打包成一个矢量
MBTiles。**不做**：Mapbox Style 复制、字体/sprite 抓取、把 pbf 解码导出成
完整矢量数据集（GeoJSON/GPKG）。想要后者的用户拿到的是一个标准 MBTiles，
用 tippecanoe / ogr2ogr 自行转换。

§13-3 缺块语义全量继承：
  * 404 → `no_data`（上游明确说这里没有数据，是**已解释**的缺块）→ 任务可以
    直接落 `completed_with_gaps`；
  * 429 / 5xx / 超时 / 连接错 → `retryable_failure`；
  * 其余 4xx → `permanent_failure`；暂存落盘失败 → `cache_failure`。
  只要有一个**非** `no_data` 的洞，默认严格：返回 `PENDING_DECISION`、
  **不产出** MBTiles，等用户在界面上决定「补漏」还是「接受缺块」。
  用户点「接受缺块」后宿主把 `params['_gap_accepted'] = True` 回写任务行再重跑
  （`task_manager.accept_gaps`），本插件读到它就**跳过下载**、直接用暂存区收尾
  打包——否则「接受缺块」会变成把全部瓦片重下一遍。

## 三个必须写下来的取舍

1. **gzip 会被 aiohttp 透明解压。** 主流瓦片服务器带 `Content-Encoding: gzip`
   发 pbf，`resp.read()` 拿到的已经是解压后的字节，落进 MBTiles 的也就是**未
   压缩的** pbf。MBTiles 1.3 允许两者（`MBTilesWriter` 对 pbf 不做魔数校验，
   `mbtiles.py:94-96` 写明理由：压缩与否都合法，没有可靠魔数），MapLibre /
   tileserver-gl / QGIS 都读得动未压缩 pbf，代价只是库比压缩版大。**不**在这里
   重新 gzip：那要为每块瓦片付一次压缩，而它唯一的收益是体积。反过来，服务器
   若在**没有** `Content-Encoding` 的情况下直接吐 `.pbf.gz` 的字节（按扩展名
   给），aiohttp 不会解压，我们就原样存——那同样是合法的 MBTiles。
2. **暂存区在 `ctx.output_dir/.staging` 下，不在共享缓存里。** 本插件的任务不绑
   数据源（URL 来自 TileJSON，不走 `tile_servers`），`ctx.cache_path()` 没有源
   命名空间可用、按设计会抛。落在任务目录里还有一个好处：`Tile.cache_path` 的
   缓存文件名恒为 `.png`（后缀不参与判定，缓存存的是原始字节），而
   `MBTilesWriter.add_dir` 是**按扩展名**收文件的，暂存区自己管后缀才能用 `.pbf`。
   打包成功后暂存区即删（`peak = temp + output` 只在打包那一刻成立）。
3. **`pending_decision` 时暂存区必须留着。** 那是「接受缺块」重跑时唯一的数据
   来源；这一趟连 TileJSON 都要重取一次（`vector_layers` 是矢量库的必填
   metadata，缺了 MapLibre 认不出图层）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil

import aiohttp

from src.contracts.outcome import TileOutcome
from src.contracts.region_tiles import (MAX_ZOOM, MIN_ZOOM,
                                        iter_region_tile_spans,
                                        validate_zoom_range)
from src.contracts.reservation import ResourceKind
from src.plugins.protocols import (ParamSchema, ParamSpec, PluginDefinition,
                                   PluginOutcome)
from src.services.disk_budget import DiskEstimate
from src.services.tile_url_probe import should_bypass_proxy

logger = logging.getLogger(__name__)

MANIFEST = {
    'id': 'mvt',
    'name': 'MVT 矢量瓦片下载',
    'version': '1.0.0',
    'api_version': '1',
    'capabilities': ['pipeline'],
    'permissions': ['network', 'filesystem'],
    'description': '按 TileJSON 下载 PBF/MVT 矢量瓦片并打包为 MBTiles'
                   '（metadata.format=pbf）。不含 style/字体/sprite，也不解码'
                   '成矢量数据集。',
}

#: 估算单价（字节/瓦片）：pbf 矢量瓦片的量级估计，只用于磁盘预算，偏保守
#: （大）——估小的代价是跑到一半写满盘。真实值跨数据集差一个数量级
#: （境界线 1 KB、含 POI 的城市层 100 KB+），所以 detail 里如实写明它是假设。
_AVG_PBF_BYTES = 20 * 1024

#: 单块瓦片的总超时。与 `download_engine` 的默认请求超时同量级；矢量瓦片比
#: 影像小，超过这个数基本就是链路不通而不是慢。
_TILE_TIMEOUT_S = 30.0

#: TileJSON 只有一份，且它是整个任务的前置条件——超时短一点，让「地址填错」
#: 在十几秒内就报出来，而不是让用户对着转圈等半分钟。
_TILEJSON_TIMEOUT_S = 15.0

#: 拿不到 NETWORK 配额时的并发兜底。**正常路径上不该被用到**：宿主
#: （`plugins/task_manager._network_request`）对声明了 `network` 权限的插件
#: 一定会请求 NETWORK，本插件的 manifest 就声明了它。走到兜底只有两种可能：
#: 有人手搓了一个不带该配额的 TaskContext（测试），或者宿主那条请求被改坏了
#: ——后者意味着连接不进全局账本，必须在日志里喊出来，见 `_concurrency`。
_DEFAULT_CONCURRENCY = 4

#: 暂存区目录名。前缀点号：它落在用户的产物目录里，不该混在成品旁边。
#: `MBTilesWriter.add_dir` 只认纯数字的层级目录，所以这个名字本身也不会被
#: 误当成一层瓦片。
_STAGING_DIR = '.staging'

#: 产物文件名允许的字符。`name` 是**用户参数**，直接拼进路径的话
#: `name='../../x'` 会让 MBTilesWriter 写到 output_dir 之外（登记那一步的
#: `register_artifact` 会拒收，但文件已经落在别人家里了）。允许清单而不是
#: 过滤 `..`：清单挡得住所有形态，包括 Windows 的盘符与保留字符。
_UNSAFE_STEM_RE = re.compile(r'[^0-9A-Za-z._\u4e00-\u9fff-]+')


def _open_session(ctx) -> aiohttp.ClientSession:
    """测试注入点。生产：连接池尺寸钉在 NETWORK 配额上的普通会话。

    `limit_per_host` 与 `limit` 同值：矢量瓦片通常只有一个主机（TileJSON 给的
    是单个模板），per-host 再压一层等于把配额白扔——与 `download_engine.py:1031`
    同一口径。`trust_env=True` 是兜底：显式 `proxy=` 为空时仍让 aiohttp 读
    `HTTP(S)_PROXY`（系统代理是 `apply_system_proxy()` 灌进环境的）。
    """
    concurrency = _concurrency(ctx)
    connector = aiohttp.TCPConnector(limit=concurrency,
                                     limit_per_host=concurrency)
    return aiohttp.ClientSession(connector=connector, trust_env=True)


def _concurrency(ctx) -> int:
    """并发上限**只**来自调度器配额，不自己拍数（§13-4 契约第 2 条）。"""
    granted = ctx.granted(ResourceKind.NETWORK)
    if granted:
        return max(1, granted)
    logger.warning(
        'MVT 未拿到 NETWORK 配额，回退到兜底并发 %d：本次下载的连接**不在**'
        '全局 max_network_connections 账本内（契约第 2 条）。宿主侧应由'
        ' plugins/task_manager._network_request 按 manifest 的 network 权限'
        '预留。', _DEFAULT_CONCURRENCY)
    return _DEFAULT_CONCURRENCY


def _proxy_for(url: str, proxy):
    """本次请求真正要用的代理。

    逐 URL 判 bypass，理由与 `download_engine.py:682-686` 逐字相同：aiohttp 的
    显式 `proxy=` 会完全盖掉 `trust_env` 那一套（连 NO_PROXY 都失效），自建的
    内网瓦片服务因此会被送进外网代理。
    """
    if not proxy:
        return None
    return None if should_bypass_proxy(url) else proxy


def _zoom_range(params) -> tuple:
    """参数里的层级区间。区间校验借 `validate_zoom_range`——错误文案与四条核心
    管线同一份（路由层把 ValueError 原文当 400 返回）。"""
    zoom_min = int(params.get('zoom_min') or 0)
    zoom_max = int(params.get('zoom_max') or zoom_min)
    return validate_zoom_range(zoom_min, zoom_max)


def _safe_stem(name: str) -> str:
    stem = _UNSAFE_STEM_RE.sub('_', str(name or '').strip()).strip('._')
    return (stem or 'mvt')[:80]


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _vector_layers(meta) -> list:
    """TileJSON 的 `vector_layers` → `MBTilesWriter` 要的图层清单。

    只留「是 dict 且 id 非空」的条目：`_normalise_vector_layers` 对坏条目直接
    抛，而在这里抛意味着几十万块已经下完的瓦片打不成包。一条都不剩时退化成
    单个 `default` 图层并告警——库仍然可读、瓦片仍然在里面，只是 MapLibre 的
    `source-layer` 要用户自己填（缺 `json` 键的话地图上会是**全空**，那更糟）。
    """
    raw = meta.get('vector_layers')
    layers = []
    if isinstance(raw, (list, tuple)):
        for entry in raw:
            if isinstance(entry, dict) and str(entry.get('id') or '').strip():
                layers.append(entry)
    return layers


class MvtPipeline:
    """TileJSON → PBF 下载 → 矢量 MBTiles。"""

    def params_schema(self) -> ParamSchema:
        """区域走任务创建时的 bbox（宿主解释），这里只声明插件自己的键。

        层级上界用 `MAX_ZOOM`（21）而不是 22：`validate_zoom_range` 是全仓唯一
        的层级口径，声明 22 只会让一个通过了表单校验的任务在 `estimate()` /
        `run()` 里炸掉。
        """
        return ParamSchema(specs=(
            ParamSpec(key='tilejson_url', type='str', label='TileJSON URL'),
            ParamSpec(key='zoom_min', type='int', label='最小层级',
                      default=0, min=MIN_ZOOM, max=MAX_ZOOM),
            ParamSpec(key='zoom_max', type='int', label='最大层级',
                      default=14, min=MIN_ZOOM, max=MAX_ZOOM),
            ParamSpec(key='name', type='str', label='名称', required=False,
                      default=''),
        ))

    def estimate(self, params, region) -> DiskEstimate:
        """五种估算。暂存区算 `temp`（打包后即删）而**不是** `cache`——它落在
        任务目录里，不进共享缓存；峰值是「暂存区 + 成品」同时存在的那一刻。"""
        zoom_min, zoom_max = _zoom_range(params)
        tiles_by_zoom = {}
        total_tiles = 0
        for zoom in range(zoom_min, zoom_max + 1):
            count = sum(x1 - x0 + 1
                        for _y, x0, x1 in iter_region_tile_spans(region, zoom))
            tiles_by_zoom[str(zoom)] = count
            total_tiles += count
        total = total_tiles * _AVG_PBF_BYTES
        return DiskEstimate(
            network_bytes=total, cache_bytes=0, temp_bytes=total,
            output_bytes=total, peak_bytes=total * 2, tile_count=total_tiles,
            detail={
                'zoom_min': zoom_min,
                'zoom_max': zoom_max,
                'tiles_by_zoom': tiles_by_zoom,
                'bytes_per_tile': _AVG_PBF_BYTES,
                'assumptions': [
                    f'每块 pbf 按 {_AVG_PBF_BYTES} B 估（假设，不是本部署的实测'
                    '值；真实值跨数据集可差一个数量级）',
                    '暂存区（temp）与成品（output）在打包那一刻共存，峰值取两者之和',
                    'gzip 的 pbf 被 aiohttp 解压后落盘，所以按未压缩体积估',
                ],
            })

    # ------------------------------------------------------------ 运行

    def run(self, ctx) -> PluginOutcome:
        return asyncio.run(self._run(ctx))

    async def _run(self, ctx) -> PluginOutcome:
        params = ctx.params
        zoom_min, zoom_max = _zoom_range(params)
        raw_url = str(params.get('tilejson_url') or '').strip()
        if not raw_url:
            raise ValueError('缺少参数 tilejson_url')
        tilejson_url = ctx.check_url(raw_url)
        gap_accepted = bool(params.get('_gap_accepted'))
        staging = ctx.output_dir / _STAGING_DIR
        # proxy_url() 会阻塞到探测超时（proxy_autodetect.py:525-526 明确警告），
        # 在事件循环里直调会把整个下载卡住。
        proxy = (await asyncio.to_thread(ctx.proxy_url)) or None

        session = _open_session(ctx)
        try:
            meta = await self._fetch_tilejson(session, tilejson_url, proxy)
            zoom_min, zoom_max = self._clamp_zooms(ctx, meta, zoom_min, zoom_max)
            # 模板过一次 SSRF 闸就够：`{z}/{x}/{y}` 替换不改主机，逐块再过一遍
            # 等于每块瓦片付一次 DNS 解析。
            tile_url_tpl = ctx.check_url(str(meta['tiles'][0]))
            tms = str(meta.get('scheme') or 'xyz').strip().lower() == 'tms'
            total = sum(
                x1 - x0 + 1
                for zoom in range(zoom_min, zoom_max + 1)
                for _y, x0, x1 in iter_region_tile_spans(ctx.region, zoom))
            ctx.log_event('mvt_start', total=total,
                          zooms=f'{zoom_min}-{zoom_max}',
                          concurrency=_concurrency(ctx), scheme='tms' if tms else 'xyz',
                          gap_accepted=gap_accepted)

            if gap_accepted:
                # §13-3：用户已经接受缺块，这一趟只收尾。重下一遍既慢又可能
                # 把上次的洞换成另一批洞（上游是活的），决策就白做了。
                ctx.log('已接受缺块：跳过下载，用暂存区的瓦片直接打包')
            else:
                await self._download_all(ctx, session, tile_url_tpl, staging,
                                        zoom_min, zoom_max, total, proxy, tms)
            ctx.flush_outcomes()

            if ctx.stop_requested():
                # 被叫停的一趟既不完整也没被用户判定过，不打包、也不冒充完成。
                ctx.log_event('mvt_stopped', done_zooms=f'{zoom_min}-{zoom_max}')
                return PluginOutcome.PENDING_DECISION

            gap_states = self._gap_states(ctx)
            unexplained = sorted(s for s in gap_states
                                 if s != TileOutcome.NO_DATA.value)
            if unexplained and not gap_accepted:
                ctx.log_event('mvt_pending_decision',
                              states=','.join(unexplained))
                ctx.log('存在未解释的缺块（' + '、'.join(unexplained)
                        + '）：默认严格，不产出 MBTiles，等用户决定补漏还是接受缺块',
                        'warning')
                return PluginOutcome.PENDING_DECISION

            has_gaps = bool(gap_states)
            packed = self._write_mbtiles(ctx, meta, staging,
                                         str(params.get('name') or '') or 'mvt')
            if packed is None:
                # 一块都没下到（典型：整个区域在上游是空的，全 404）。空库不
                # 合规（bounds/minzoom/maxzoom 推不出来），所以不产物、也不失败：
                # 缺块已被 no_data 解释干净，这就是这个区域的真实答案。
                ctx.log_event('mvt_no_tiles', gaps=','.join(sorted(gap_states)))
                ctx.log('区域内没有任何瓦片可打包（上游全部无数据），不产出 MBTiles',
                        'warning')
                return (PluginOutcome.COMPLETED_WITH_GAPS if has_gaps
                        else PluginOutcome.COMPLETED)

            out, stats = packed
            # 瓦片数/层级范围只能走 meta：`register_artifact` 的 bytes_total 等
            # 列一律由宿主的 `_measure` 现算，而对**文件**形态的产物它只量得到
            # 字节数（`task_context.py:256-267`）——核心管线是自己拼 Artifact
            # 才填得上这三列的。库里真实的数字放这里，UI 与诊断不至于只看到 0。
            ctx.register_artifact(out, kind=self._artifact_kind(),
                                  has_gaps=has_gaps, fmt='pbf',
                                  meta={'tilejson_url': tilejson_url,
                                        'vector_layers': _vector_layers(meta),
                                        'tile_count': stats['tile_count'],
                                        'minzoom': stats['minzoom'],
                                        'maxzoom': stats['maxzoom'],
                                        'bounds': stats['bounds']})
            # 登记之后再清暂存区：登记抛了（路径越界之类）还留着数据可救。
            self._clear_staging(ctx, staging)
            return (PluginOutcome.COMPLETED_WITH_GAPS if has_gaps
                    else PluginOutcome.COMPLETED)
        finally:
            # 测试注入的假会话没有 close()；真会话的 close() 是协程。
            close = getattr(session, 'close', None)
            if close is not None:
                result = close()
                if asyncio.iscoroutine(result):
                    await result

    # ------------------------------------------------------------ TileJSON

    async def _fetch_tilejson(self, session, url, proxy) -> dict:
        """取 TileJSON。失败一律抛——它是整个任务的前置条件，没有它连要下哪些
        URL 都不知道，退化成缺块反而会产出一个空库。"""
        async with session.get(
                url, proxy=_proxy_for(url, proxy),
                timeout=aiohttp.ClientTimeout(total=_TILEJSON_TIMEOUT_S)) as resp:
            if resp.status != 200:
                raise RuntimeError(f'TileJSON 获取失败：HTTP {resp.status}（{url}）')
            raw = await resp.read()
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as e:
            raise RuntimeError(f'TileJSON 不是合法 JSON（{url}）：{e}') from e
        tiles = data.get('tiles') if isinstance(data, dict) else None
        if (not isinstance(tiles, (list, tuple)) or not tiles
                or not str(tiles[0]).strip()):
            raise RuntimeError(f'TileJSON 缺少可用的 tiles 数组（{url}）')
        return data

    def _clamp_zooms(self, ctx, meta, zoom_min, zoom_max) -> tuple:
        """按 TileJSON 声明的 min/maxzoom 收窄请求区间。

        不收窄的话，超出上游范围的那些层每一块都要打一次必然 404 的请求——把
        一次「层级填大了」变成几十万次无用出网 + 一屏 no_data 缺块。整个区间都
        在范围之外时抛：那是参数错，不是缺块。
        """
        src_min = _int_or_none(meta.get('minzoom'))
        src_max = _int_or_none(meta.get('maxzoom'))
        low = zoom_min if src_min is None else max(zoom_min, src_min)
        high = zoom_max if src_max is None else min(zoom_max, src_max)
        if (low, high) == (zoom_min, zoom_max):
            return zoom_min, zoom_max
        if low > high:
            raise RuntimeError(
                f'请求层级 {zoom_min}-{zoom_max} 完全落在 TileJSON 声明的'
                f' {src_min}-{src_max} 之外，没有可下载的层级')
        ctx.log_event('mvt_zoom_clamped', requested=f'{zoom_min}-{zoom_max}',
                      source=f'{src_min}-{src_max}', effective=f'{low}-{high}')
        ctx.log(f'请求层级 {zoom_min}-{zoom_max} 超出 TileJSON 声明的'
                f' {src_min}-{src_max}，按 {low}-{high} 下载', 'warning')
        return low, high

    # ------------------------------------------------------------ 下载

    async def _download_all(self, ctx, session, tpl, staging,
                            zoom_min, zoom_max, total, proxy, tms) -> None:
        """有界任务池：同时在飞的请求数恰好是 NETWORK 配额。

        不用「逐块 await」（等于并发 1，配额白拿），也不用 `gather` 整个区域
        （百万块瓦片先物化成百万个 Task，内存直接爆）。`asyncio.wait` 的
        FIRST_COMPLETED 让池子始终满而不越界。
        """
        concurrency = _concurrency(ctx)
        pending = set()
        done = 0

        async def reap(drain=False):
            nonlocal pending, done
            if not pending:
                return
            finished, pending = await asyncio.wait(
                pending, return_when=(asyncio.ALL_COMPLETED if drain
                                      else asyncio.FIRST_COMPLETED))
            for task in finished:
                # `_download_tile` 自己把一切失败翻译成 outcome，真抛出来的
                # 只可能是 bug——让它冒到 run() 去，任务判 failed 比静默漏块好。
                task.result()
            done += len(finished)
            # 进度节流在宿主那一侧（2Hz，task_manager.py:450-473），这里照实报。
            ctx.progress(done, total, 'download')

        for zoom in range(zoom_min, zoom_max + 1):
            for y, x0, x1 in iter_region_tile_spans(ctx.region, zoom):
                for x in range(x0, x1 + 1):
                    if ctx.stop_requested():
                        await reap(drain=True)
                        return
                    pending.add(asyncio.create_task(self._download_tile(
                        ctx, session, tpl, staging, zoom, x, y, proxy, tms)))
                    if len(pending) >= concurrency:
                        await reap()
        await reap(drain=True)

    async def _download_tile(self, ctx, session, tpl, staging,
                             z, x, y, proxy, tms) -> None:
        """一块瓦片：命中暂存 → 跳过；否则下载并落盘。**绝不抛**。"""
        stage = staging / str(z) / str(x) / f'{y}.pbf'
        try:
            if stage.stat().st_size > 0:
                # 上一趟已经拿到了（补漏重跑的常态）。仍然记一次 success——
                # 它的语义是「消除缺块行」，上一趟留下的洞要在这里抹掉。
                ctx.record_tile_outcome(z, x, y, TileOutcome.SUCCESS)
                return
        except OSError:
            pass                      # 不存在 / 读不到属性，照常下载
        # TileJSON 2.x 的 `scheme: tms` 说的是**模板里的 {y} 是 TMS 行号**。
        # 内部一律用 XYZ（缺块记账、MBTilesWriter 的入参都是 XYZ），只在拼 URL
        # 这一瞬翻转，翻错的后果是整个库南北颠倒且没人报错。
        y_req = ((1 << z) - 1 - y) if tms else y
        url = (tpl.replace('{z}', str(z)).replace('{x}', str(x))
               .replace('{y}', str(y_req)))
        try:
            async with session.get(
                    url, proxy=_proxy_for(url, proxy),
                    timeout=aiohttp.ClientTimeout(total=_TILE_TIMEOUT_S)) as resp:
                status = resp.status
                if status == 404:
                    ctx.record_tile_outcome(z, x, y, TileOutcome.NO_DATA)
                    return
                if status != 200:
                    outcome = (TileOutcome.RETRYABLE_FAILURE
                               if status == 429 or status >= 500
                               else TileOutcome.PERMANENT_FAILURE)
                    ctx.record_tile_outcome(z, x, y, outcome, f'HTTP {status}')
                    return
                body = await resp.read()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 超时 / 连接重置 / DNS / 代理拒绝：都是「原因在我们这边或链路上」，
            # §13-3 归 retryable。
            ctx.record_tile_outcome(z, x, y, TileOutcome.RETRYABLE_FAILURE,
                                    f'{type(e).__name__}: {e}')
            return
        if not body:
            # 200 + 空体是「这块没有要素」的常见表达。当 no_data 记而不是当成功：
            # `MBTilesWriter.add_tile` 拒收 0 字节瓦片，报成功会让任务显示干净
            # 而库里少一块，正是 §13-3 要防的那种静默洞。
            ctx.record_tile_outcome(z, x, y, TileOutcome.NO_DATA, '空响应体')
            return
        try:
            stage.parent.mkdir(parents=True, exist_ok=True)
            # 先写 `.part` 再 replace：崩在写一半上时，留下的是一个 add_dir 会
            # 跳过的 `.part`（后缀不匹配），而不是一个「存在且非空」因此会被
            # 上面那条命中检查当成已下好的半截瓦片。
            part = stage.with_name(stage.name + '.part')
            part.write_bytes(body)
            os.replace(part, stage)
        except OSError as e:
            # 盘满 / 权限：拿到了字节但存不下。`cache_failure` 就是这个语义，
            # 且它不是 no_data——会把任务推到 pending_decision，正确。
            ctx.record_tile_outcome(z, x, y, TileOutcome.CACHE_FAILURE,
                                    f'{type(e).__name__}: {e}')
            return
        ctx.record_tile_outcome(z, x, y, TileOutcome.SUCCESS)

    # ------------------------------------------------------------ 收尾

    def _gap_states(self, ctx) -> set:
        """本任务在缺块表里留下的 status 集合。

        为什么读库而不是在内存里数自己记的那些：判决必须与**用户看到的**一致。
        `gap_tiles`（界面上的缺块数、`accept_gaps` 的依据）就是这张表的行数，
        而重跑只会增量改写它——只看本轮内存计数会把上一趟留下的洞判成不存在。
        """
        from src.core.database import get_connection
        conn = get_connection()
        try:
            return {str(r['status']) for r in conn.execute(
                'SELECT DISTINCT status FROM plugin_task_tiles'
                ' WHERE task_id = ?', (ctx.task_id,)).fetchall()}
        finally:
            conn.close()

    def _write_mbtiles(self, ctx, meta, staging, name):
        """暂存区 → 矢量 MBTiles，返回 `(路径, finalize() 的统计)`。

        一块都没有时返回 None（不产出空库）。

        `with` **不自动 finalize**（`mbtiles.py:267-276`：自动 finalize 会把
        「中途抛异常、只写了一半」变成一个看起来完整的库），所以显式调；
        提前 return 的那条路径由 `__exit__` 删掉 `.part` 残件，最终路径上什么
        都不会出现。
        """
        from src.services.mbtiles import MBTilesWriter

        if not staging.is_dir():
            return None
        out = ctx.output_dir / f'{_safe_stem(name)}.mbtiles'
        layers = _vector_layers(meta)
        if not layers:
            layers = [{'id': 'default', 'fields': {}}]
            ctx.log('TileJSON 没有可用的 vector_layers，metadata.json 记为单个'
                    ' default 图层：库可读，但 MapLibre 的 source-layer 需要'
                    '自行确认', 'warning')
        with MBTilesWriter(out, fmt='pbf', name=_safe_stem(name),
                           attribution=str(meta.get('attribution') or ''),
                           description=str(meta.get('description') or ''),
                           vector_layers=layers) as writer:
            if not writer.add_dir(staging, extension='.pbf'):
                return None
            result = writer.finalize()
        ctx.log_event('mvt_packed', tiles=result['tile_count'],
                      bytes=result['bytes'],
                      zooms=f"{result['minzoom']}-{result['maxzoom']}")
        return out, result

    def _clear_staging(self, ctx, staging) -> None:
        """打包成功后删暂存区：瓦片字节已经在库里了，留着就是双份占用。

        失败只记日志——产物已经登记好了，为清不掉一个临时目录把一次成功的
        任务判失败不成比例（残件跟着任务目录一起被删除流程带走）。
        """
        try:
            shutil.rmtree(staging)
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning('暂存区清理失败（%s）：%r', staging, e)
            ctx.log(f'暂存区未能清理（{staging.name}），不影响产物：{e!r}', 'warning')

    def _artifact_kind(self):
        from src.contracts.artifact import ArtifactKind
        return ArtifactKind.MBTILES


def register() -> PluginDefinition:
    return PluginDefinition(pipeline=MvtPipeline())
