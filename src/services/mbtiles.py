"""MBTiles 1.3 通用产物容器 —— 写入端、读取端与校验（§5.3 / §13-2）。

## 为什么第一版就是「容器」而不是「影像导出器」

§13-2 已决：MBTiles 同时承载**影像、等高线与 MVT**。这三者只在
`metadata.format` 与矢量库额外的 `json` 键上有差别，瓦片写入路径完全相同。
先做影像再回头加矢量，代价不是多写几行 —— 而是 schema 与 metadata 契约要
改，而 MBTiles 是**用户拿走就带走**的分发件：改契约救不回已经落在用户盘上、
已经发出去的库。所以 `fmt` 从构造函数第一个关键字参数就是必填，矢量库缺
`vector_layers` 直接在**构造时**炸，不给「写了一万块瓦片才发现 metadata 不
合规」的机会。

**一个库只装一种 format。** 规范没有多图层容器语义，影像 / 等高线 / MVT
各自成库。混装唯一换来的是不可移植：读取端拿 `metadata.format` 决定怎么解
码每一块瓦片，库里混了两种格式就一定有一半解不开。因此 `add_tile` 会嗅探
魔数，与声明的 `fmt` 不符直接拒收。

## 行号方案：TMS，且**绝不写 `scheme` 元数据键**

MBTiles 用 TMS 行号：`tile_row = 2^zoom - 1 - y_xyz`（y 轴朝上）。本模块
写入时做这个翻转，`read_tile` 读回时做逆翻转，所以调用方全程只用 XYZ 坐标。

**同时写 `scheme=xyz` 又做翻转，是这里唯一必须避免的自相矛盾。**
GeoLibre 的读取端（`src-tauri/src/lib.rs:3789`）正是按 `metadata.scheme ==
'xyz'` 分支：见到该键就**不翻转**，缺该键才按 TMS 翻。也就是说，一个既翻了
行号又声明 `scheme=xyz` 的库，在 GeoLibre 里会被翻**第二次** —— 表现不是报
错，而是整张地图南北颠倒且看起来「像是有数据」。TMS 是规范默认值，缺省即
正确，所以本模块**从不写 `scheme` 键**，`MBTilesWriter(scheme=...)` 那个参数
描述的是**调用方传进来的坐标是哪一套**，与 metadata 无关。

## 为什么不用 `src.core.database.get_connection`

那个连接工厂硬绑 `Config.DATABASE_PATH`（应用自己的任务库）。产物库是任意
路径的独立文件，且要在写入期开 `synchronous=OFF` 这类只对**派生产物**成立
的 pragma —— 不能污染任务库的连接配置。所以这里直接用 stdlib `sqlite3`。

## 原子写

写 `<path>.part.<pid>`，`finalize()` 里 `os.replace` 就位。理由与
`download_engine.py:1219-1247` 的拼接产物一致：产物存在性就是断点判据，
半成品留在最终路径上会被后续流程当成「已完成」。pid **必须紧跟在
`.part.` 后面**，因为 `task_cleanup._part_owner_pid`（`task_cleanup.py:401`）
就是按 `<name>.part.<pid>[.<id>]` 解析归属，用它区分「另一个活进程正在写」
与「上次崩溃留下的残件」；把别的东西塞进 pid 槽位会让启动清扫得出反向结论。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.contracts.region_tiles import MAX_ZOOM, MIN_ZOOM, tile_lon_lat_bounds

logger = logging.getLogger(__name__)

__all__ = [
    'MBTilesError',
    'MBTilesWriter',
    'SUPPORTED_FORMATS',
    'VECTOR_FORMATS',
    'read_metadata',
    'read_tile',
    'validate_mbtiles',
]


#: 允许出现在 `metadata.format` 里的取值。MBTiles 1.3 规范定义了 jpg / png /
#: webp 三种栅格，外加 OGC 矢量瓦片的 pbf。写规范外的值等于把库变成只有
#: TerraForge 自己能读的私有文件，违背「单文件、通吃 QGIS/MapLibre/tileserver」
#: 这个引入 MBTiles 的唯一理由（§5.3）。
SUPPORTED_FORMATS: Tuple[str, ...] = ('png', 'jpg', 'webp', 'pbf')

#: 需要 `json` 元数据键描述 `vector_layers` 的格式。缺了它 MapLibre 与
#: tileserver 无法识别图层 —— 库能打开、瓦片能取出、地图上什么都不显示。
VECTOR_FORMATS: Tuple[str, ...] = ('pbf',)

#: 调用方常写的别名 → 规范取值。只做这一层归一，不做 slug 化。
_FORMAT_ALIASES = {'jpeg': 'jpg', 'mvt': 'pbf', 'protobuf': 'pbf'}

#: 声明格式 → 该格式的文件头魔数。
#
# 为什么不 import `download_engine.looks_like_image`（`download_engine.py:148`）：
#   1. 它回答的是**另一个问题** —— 「这坨字节是不是某种图片」，用来挡运营商
#      劫持返回的 HTML。这里要回答的是「这坨字节是不是**声明的那一种**图片」，
#      一个平铺的 bool 表达不了「png 库里混进了 jpg」，而后者正是「一个库只装
#      一种 format」要拦的东西。
#   2. `download_engine` 在模块级引 aiohttp / aiofiles / ConfigManager / 代理探测
#      整条下载栈。本模块要被预览路由与产物校验（只读、不下载）复用，为了一个
#      三字节前缀把下载栈拖进来不划算。
# webp / pbf 不做嗅探：webp 只有 `RIFF....WEBP` 一个弱特征，而 pbf 按规范应当
# 是 **gzip 压缩的** protobuf，压缩与否都合法，没有可靠魔数 —— 与其做一个会误
# 杀合法瓦片的检查，不如明确只在 png/jpg 上强制。
_FORMAT_MAGIC: Dict[str, Tuple[bytes, ...]] = {
    'png': (b'\x89PNG\r\n\x1a\n',),
    'jpg': (b'\xff\xd8\xff',),
}

#: `validate_mbtiles` 抽查瓦片魔数的条数上限。
#
# 为什么抽查而不是全查：校验器要在几 GB、上百万行的库上跑，且它的调用点是
# 「导出完了给一份体检报告」—— 把每一块瓦片的 BLOB 都读出来会让体检本身变成
# 一次全表扫描（实测量级：一个 z13 的市级库就是几十万行）。而声明格式是
# **库级**属性，不是逐瓦片属性：`add_tile` 在写入端逐块守着，校验端要答的是
# 「这个库整体是不是被改过 metadata.format / 是不是别人拿混装内容拼的」，
# 32 块足够让任何一种系统性不符暴露出来。
#
# 代价说清楚：一个百万块里只掺了一块异种瓦片的库，抽查抽不到。那种库的正确
# 拦截点在写入端（add_tile 已经在拦），校验端只承诺**声明与主体不符**能被发现。
_FORMAT_SAMPLE_TILES = 32

# 标准 schema。`tiles` 的唯一性**就是**约定俗成的那个索引：规范参考实现建的是
# 裸表 + `CREATE UNIQUE INDEX tile_index`，而不是表级 UNIQUE 约束 —— 后者会让
# SQLite 再建一个 `sqlite_autoindex_tiles_1`，多一份等价索引（写入变慢、体积变
# 大），且按名字找 `tile_index` 的第三方工具会认不出来。
_SCHEMA_SQL = (
    "CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS tiles ("
    "zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)",
    "CREATE UNIQUE INDEX IF NOT EXISTS tile_index "
    "ON tiles (zoom_level, tile_column, tile_row)",
)

# INSERT OR REPLACE 而不是 INSERT：重跑一次导出（补块、续传后重新打包）必须
# 幂等，同坐标覆盖而不是撞唯一索引把整批事务回滚掉。
_INSERT_SQL = ("INSERT OR REPLACE INTO tiles "
               "(zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)")

# 声明 bounds 与实际瓦片外包框比对时的容差（度）。z21 的瓦片宽约 1.7e-4 度，
# 1e-6 远小于一块瓦片，足以吸收字符串往返的十进制截断而不放过真实错误。
_BOUNDS_EPSILON = 1e-6


class MBTilesError(RuntimeError):
    """容器层面的错误：格式不自洽、坐标越界、库损坏、无法落盘。

    刻意**不是** ValueError 的子类。路由把 ValueError 一律映射成 HTTP 400
    「用户输入有问题」，而这里绝大多数错误（png 库里混进 jpg、finalize 时磁盘
    满、库文件损坏）不是用户在表单里填错了什么，报 400 会把排查方向带偏。
    校验类问题走 `validate_mbtiles` 的 `problems` 列表，不抛。
    """


# ---------------------------------------------------------------------------
# 公共小工具
# ---------------------------------------------------------------------------

def _normalise_format(fmt: Any) -> str:
    """归一化格式名，非法值抛 MBTilesError。"""
    text = str(fmt or '').strip().lower().lstrip('.')
    text = _FORMAT_ALIASES.get(text, text)
    if text not in SUPPORTED_FORMATS:
        raise MBTilesError(
            f"MBTiles format must be one of {'/'.join(SUPPORTED_FORMATS)}, got {fmt!r}")
    return text


def _tms_row(zoom: int, y_xyz: int) -> int:
    """XYZ 行号 → TMS 行号。全模块**只此一处**做这个翻转。

    写入端与读取端共用它，是「翻转与反翻转永远对称」的唯一保证；两边各写一遍
    `2**z - 1 - y` 的话，任何一边改了钳位或类型转换都会静默错位半个地球。
    """
    return (1 << zoom) - 1 - y_xyz


def _check_tile_coords(zoom: int, x: int, y: int) -> Tuple[int, int, int]:
    """校验 XYZ 坐标并返回 int 三元组，越界抛 MBTilesError。

    越界坐标写进去不会报错，只会在读取端变成永远取不到的死行，或者让
    `validate_mbtiles` 的行号检查在**下游用户**那里才炸。挡在写入口最便宜。
    """
    try:
        zoom, x, y = int(zoom), int(x), int(y)
    except (TypeError, ValueError) as exc:
        raise MBTilesError(f"Tile coordinates must be integers: {(zoom, x, y)!r}") from exc
    if not MIN_ZOOM <= zoom <= MAX_ZOOM:
        raise MBTilesError(
            f"Zoom level must be between {MIN_ZOOM} and {MAX_ZOOM}, got {zoom}")
    limit = 1 << zoom
    if not (0 <= x < limit and 0 <= y < limit):
        raise MBTilesError(
            f"Tile ({zoom}/{x}/{y}) is outside the z{zoom} grid (0..{limit - 1})")
    return zoom, x, y


def _sniff_mismatch(fmt: str, data: bytes) -> bool:
    """声明格式与数据魔数是否**矛盾**。未登记魔数的格式一律返回 False。"""
    prefixes = _FORMAT_MAGIC.get(fmt)
    if not prefixes:
        return False
    return not data.startswith(prefixes)


def _format_bounds(bounds: Sequence[float]) -> str:
    """`(w, s, e, n)` → metadata 里的逗号分隔字符串。"""
    return ','.join(f"{float(v):.6f}" for v in bounds)


def _parse_bounds(text: Any) -> Optional[List[float]]:
    """metadata 的 bounds 字符串 → 四个 float，形状不对返回 None。"""
    parts = str(text or '').replace(' ', '').split(',')
    if len(parts) != 4:
        return None
    try:
        return [float(p) for p in parts]
    except ValueError:
        return None


def _normalise_vector_layers(layers: Any) -> List[Dict[str, Any]]:
    """校验并归一化 `vector_layers`，不合规抛 MBTilesError。

    只强制 `id` 非空 —— 那是 MapLibre 的 `source-layer` 匹配键，空了整层数据在
    地图上就是不存在。`fields` 缺失时补空对象：规范要求该键存在，tileserver 对
    缺键的图层会直接跳过。
    """
    if not isinstance(layers, (list, tuple)) or not layers:
        raise MBTilesError(
            "Vector MBTiles requires a non-empty vector_layers list "
            "(MapLibre and tileserver cannot identify layers without it)")
    out: List[Dict[str, Any]] = []
    seen = set()
    for index, layer in enumerate(layers):
        if not isinstance(layer, dict):
            raise MBTilesError(f"vector_layers[{index}] must be a dict, got {type(layer).__name__}")
        layer_id = str(layer.get('id') or '').strip()
        if not layer_id:
            raise MBTilesError(f"vector_layers[{index}] has an empty 'id'")
        if layer_id in seen:
            raise MBTilesError(f"vector_layers has a duplicate id {layer_id!r}")
        seen.add(layer_id)
        entry: Dict[str, Any] = {'id': layer_id, 'fields': dict(layer.get('fields') or {})}
        for optional in ('description', 'minzoom', 'maxzoom'):
            if layer.get(optional) is not None:
                entry[optional] = layer[optional]
        out.append(entry)
    return out


def _connect_ro(path: Path) -> sqlite3.Connection:
    """只读打开一个已存在的 MBTiles。

    用 URI 形式的 `mode=ro`：拼字符串会被路径里的 `?` / `#` 打断，而
    `Path.as_uri()` 会把它们百分号转义。只读还顺带保证预览路由不会因为一次
    误写把用户的产物改脏。
    """
    path = Path(path)
    if not path.is_file():
        raise MBTilesError(f"MBTiles file not found: {path}")
    try:
        return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except (sqlite3.Error, ValueError) as exc:
        raise MBTilesError(f"Cannot open MBTiles {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# 写入端
# ---------------------------------------------------------------------------

class MBTilesWriter:
    """把瓦片写进一个 MBTiles 1.3 容器。栅格与矢量共用。

    用法（**`with` 不会自动 finalize**，见下）::

        with MBTilesWriter(out, fmt='png', name='satellite') as w:
            w.add_tile(5, 26, 12, png_bytes)
            result = w.finalize()

    `__exit__` 只负责「关连接、删残件」这个兜底：异常退出、或者忘了调
    `finalize()`，最终路径上**什么都不会出现**。这是刻意的 ——
    自动 finalize 会把「循环中途抛异常、只写了一半」变成一个看起来完整、
    metadata 也自洽的库，而那正是最难发现的一类产物缺陷。

    参数
    ----
    path
        最终产物路径。写入期间它**不存在**，只有 `.part.<pid>` 在。
    fmt
        `png` / `jpg` / `webp` / `pbf`，见 `SUPPORTED_FORMATS`。一库一格式。
    scheme
        **调用方传给 `add_tile` 的坐标是哪一套**：`'xyz'`（默认，y 轴朝下）
        或 `'tms'`（y 轴朝上，已经是行号）。它**不会**被写进 metadata ——
        见模块 docstring 里 `scheme=xyz` 的自相矛盾。
    name / attribution / description
        metadata 文本。`name` 是规范必填项，留空则回退到文件名主干（写一个
        空字符串反而会让 tileserver 的图层列表出现无名条目）。`attribution`
        承载 `SourceSnapshot.attribution`（§4.3）。
    vector_layers
        矢量库必填，栅格库必须不填。构造时校验，不拖到 finalize。
    batch_size
        每多少块瓦片提交一次事务。逐瓦片提交会让每块瓦片付一次 fsync + 日志
        往返，百万级瓦片就是把导出从分钟级拖到小时级。
    layer_type / version
        metadata 的 `type`（`baselayer` / `overlay`）与 `version`。它们不在
        §5.3 列的必填集合里但属于规范推荐项，给了默认值，按合同调用可以完全
        忽略。
    """

    def __init__(self, path, *, fmt, scheme='xyz', name='', attribution='',
                 vector_layers=None, batch_size=512,
                 layer_type='baselayer', description='', version='1.0.0'):
        self.path = Path(path)
        self.format = _normalise_format(fmt)

        scheme = str(scheme or 'xyz').strip().lower()
        if scheme not in ('xyz', 'tms'):
            raise MBTilesError(f"scheme must be 'xyz' or 'tms', got {scheme!r}")
        self.input_scheme = scheme

        if self.format in VECTOR_FORMATS:
            # 构造时就炸，而不是 finalize 时 —— 矢量库缺 vector_layers 是**不可
            # 修复**的元数据缺陷，让调用方先写完几十万块瓦片再发现纯属浪费。
            self.vector_layers = _normalise_vector_layers(vector_layers)
        else:
            if vector_layers:
                raise MBTilesError(
                    f"vector_layers is only valid for {'/'.join(VECTOR_FORMATS)}, "
                    f"not for format={self.format}")
            self.vector_layers = []

        self.name = str(name or '').strip() or self.path.stem
        self.attribution = str(attribution or '')
        self.description = str(description or '')
        self.layer_type = str(layer_type or 'baselayer').strip().lower()
        if self.layer_type not in ('baselayer', 'overlay'):
            # 非法值只降级不中断：type 不影响可读性，规范之外的取值会被读取端
            # 忽略，为它中断一次已经跑了几小时的导出不成比例。
            logger.warning("Unknown MBTiles type %r, falling back to 'baselayer'", layer_type)
            self.layer_type = 'baselayer'
        self.version = str(version or '1.0.0')

        try:
            self.batch_size = max(1, int(batch_size))
        except (TypeError, ValueError):
            logger.warning("Invalid MBTiles batch_size %r, falling back to 512", batch_size)
            self.batch_size = 512

        self._pending: List[Tuple[int, int, int, bytes]] = []
        self._added = 0
        self._minzoom: Optional[int] = None
        self._maxzoom: Optional[int] = None
        self._west: Optional[float] = None
        self._south: Optional[float] = None
        self._east: Optional[float] = None
        self._north: Optional[float] = None
        self._result: Optional[Dict[str, Any]] = None

        self.part_path = self.path.with_name(f"{self.path.name}.part.{os.getpid()}")
        self._conn = self._open_part()

    # -- 生命周期 ----------------------------------------------------------

    def _open_part(self) -> sqlite3.Connection:
        """建目录、清掉同 pid 的旧残件、开库建表设 pragma。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 同 pid 的残件只可能来自本进程上一次失败的导出（`task_cleanup` 按 pid
        # 判归属，活进程的残件它不敢碰）。不删就会把上次那批瓦片一起算进
        # tile_count 与 bounds —— 产出一个「多出一片来路不明区域」的库。
        for residue in (self.part_path,
                        self.part_path.with_name(self.part_path.name + '-journal')):
            try:
                residue.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise MBTilesError(f"Cannot remove stale part file {residue}: {exc}") from exc

        try:
            conn = sqlite3.connect(str(self.part_path))
            # journal_mode=DELETE 是**必须显式钉死**的：WAL 会把已提交的页留在
            # 旁边的 `-wal` / `-shm` 里，而 finalize 只 os.replace 主文件 ——
            # 产物会变成一个缺最近若干批瓦片、且不自包含的库。
            # synchronous=OFF 拿掉每次提交的 fsync。断电确实可能撕坏这个文件，
            # 但它是**派生产物**不是事实来源：源数据在 cache / 任务库里，撕坏了
            # 丢掉重打包即可，为此付几十万次 fsync 不值。
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA synchronous=OFF")
            for statement in _SCHEMA_SQL:
                conn.execute(statement)
            conn.commit()
            return conn
        except sqlite3.Error as exc:
            raise MBTilesError(f"Cannot create MBTiles {self.part_path}: {exc}") from exc

    def __enter__(self) -> 'MBTilesWriter':
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._result is None:
            if exc_type is None and self._added:
                logger.warning(
                    "MBTilesWriter for %s left the context without finalize(); "
                    "discarding %d tile(s)", self.path, self._added)
            self._abort()
        return False

    def _abort(self) -> None:
        """关连接、删 part 文件。绝不动最终路径。"""
        self._pending.clear()
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error as exc:
                logger.warning("Error closing MBTiles part %s: %s", self.part_path, exc)
        try:
            self.part_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            # 只记录：残件由 task_cleanup 的启动清扫按 pid 兜底，为清理失败再抛
            # 一次异常会盖掉调用方真正在处理的那个错误。
            logger.warning("Failed to remove MBTiles part %s: %s", self.part_path, exc)

    # -- 写入 --------------------------------------------------------------

    def add_tile(self, zoom: int, x: int, y: int, data: bytes) -> None:
        """加入一块瓦片。坐标按构造时的 `scheme` 解释，落库一律 TMS 行号。"""
        if self._result is not None:
            raise MBTilesError(f"MBTiles {self.path} is already finalized")
        if self._conn is None:
            raise MBTilesError(f"MBTiles writer for {self.path} was aborted")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise MBTilesError(
                f"Tile data must be bytes, got {type(data).__name__} for {zoom}/{x}/{y}")
        data = bytes(data)
        if not data:
            # 空瓦片写进去比不写更糟：读取端拿到 0 字节会当成「有这块、但解不开」，
            # 而缺行至少是一个明确的洞。
            raise MBTilesError(f"Refusing to write an empty tile at {zoom}/{x}/{y}")

        if self.input_scheme == 'tms':
            # 调用方给的已经是 TMS 行号；用等价的 XYZ y 走一遍越界与 bounds 计算，
            # 免得两套坐标各写一份校验。
            zoom_i = int(zoom)
            if not MIN_ZOOM <= zoom_i <= MAX_ZOOM:
                raise MBTilesError(
                    f"Zoom level must be between {MIN_ZOOM} and {MAX_ZOOM}, got {zoom}")
            zoom_i, x_i, y_xyz = _check_tile_coords(zoom_i, x, _tms_row(zoom_i, int(y)))
        else:
            zoom_i, x_i, y_xyz = _check_tile_coords(zoom, x, y)

        if _sniff_mismatch(self.format, data):
            raise MBTilesError(
                f"Tile {zoom_i}/{x_i}/{y_xyz} does not look like {self.format} "
                f"(magic {data[:8]!r}); one MBTiles holds exactly one format")

        self._pending.append((zoom_i, x_i, _tms_row(zoom_i, y_xyz), data))
        self._added += 1
        self._track_extent(zoom_i, x_i, y_xyz)
        if len(self._pending) >= self.batch_size:
            self._flush()

    def add_dir(self, root, *, extension) -> int:
        """把一个 `{z}/{x}/{y}.<ext>` 金字塔目录整个装进容器，返回加入的瓦片数。

        存在的意义：已经下载好的 XYZ 目录不必重下就能变成 MBTiles（§5.3 列的
        「可直接复用现有 XYZ 下载结果」）。

        遍历用 scandir 而不是 rglob：成品目录动辄几十万文件，`rglob('*.png')`
        会先把整棵树物化成列表。非数字目录名、扩展名不匹配的文件一律跳过 ——
        原子写残件 `123.png.part.4242` 的后缀段是 pid，天然落在过滤之外。

        0 字节文件跳过并记警告，**不中断**：那是旧版本中断写入留下的已知残件
        形态，为一个残件让整个金字塔打不成包，等于在任何崩溃过一次的盘上永远
        导不出 MBTiles；缺的那块会如实反映在 `tile_count` 里。魔数不匹配则照常
        抛出 —— 那说明声明的 `fmt` 就是错的，整个库都不对。
        """
        root = Path(root)
        if not root.is_dir():
            raise MBTilesError(f"Tile directory not found: {root}")
        suffix = '.' + str(extension or '').strip().lstrip('.').lower()
        if suffix == '.':
            raise MBTilesError("add_dir requires a non-empty extension")

        added = 0
        skipped_empty = 0
        for zoom_entry in _scan_numeric_dirs(root):
            zoom = int(zoom_entry.name)
            for x_entry in _scan_numeric_dirs(Path(zoom_entry.path)):
                x = int(x_entry.name)
                try:
                    with os.scandir(x_entry.path) as files:
                        entries = list(files)
                except OSError as exc:
                    logger.warning("Cannot list tile directory %s: %s", x_entry.path, exc)
                    continue
                for file_entry in entries:
                    stem, dot, ext = file_entry.name.rpartition('.')
                    if not dot or ('.' + ext.lower()) != suffix or not stem.isdigit():
                        continue
                    try:
                        if not file_entry.is_file(follow_symlinks=False):
                            continue
                        data = Path(file_entry.path).read_bytes()
                    except OSError as exc:
                        logger.warning("Cannot read tile %s: %s", file_entry.path, exc)
                        continue
                    if not data:
                        skipped_empty += 1
                        logger.warning("Skipping zero-byte tile %s", file_entry.path)
                        continue
                    self.add_tile(zoom, x, int(stem), data)
                    added += 1
        if skipped_empty:
            logger.warning("add_dir(%s) skipped %d zero-byte tile file(s)", root, skipped_empty)
        logger.info("add_dir(%s, *%s) added %d tile(s)", root, suffix, added)
        return added

    def _track_extent(self, zoom: int, x: int, y_xyz: int) -> None:
        """按**实际写入的瓦片**累积 bounds 与层级范围。

        绝不用请求区域算 bounds：瓦片网格比区域粗，metadata 必须描述文件里
        **真有什么**。用区域的话，一个只覆盖半块瓦片的小多边形会导出一个
        bounds 比内容小的库，读取端在边缘按 bounds 裁剪就会切掉真实存在的
        像素；反过来跳过了的无覆盖瓦片又会让 bounds 虚胖。
        """
        self._minzoom = zoom if self._minzoom is None else min(self._minzoom, zoom)
        self._maxzoom = zoom if self._maxzoom is None else max(self._maxzoom, zoom)
        west, south, east, north = tile_lon_lat_bounds(zoom, x, y_xyz)
        if self._west is None:
            self._west, self._south, self._east, self._north = west, south, east, north
            return
        self._west = min(self._west, west)
        self._south = min(self._south, south)
        self._east = max(self._east, east)
        self._north = max(self._north, north)

    def _flush(self) -> None:
        """一批一个事务。`executemany` + 一次 commit，绝不逐瓦片提交。"""
        if not self._pending or self._conn is None:
            return
        batch, self._pending = self._pending, []
        try:
            self._conn.executemany(_INSERT_SQL, batch)
            self._conn.commit()
        except sqlite3.Error as exc:
            raise MBTilesError(f"Failed to write {len(batch)} tile(s) to "
                               f"{self.part_path}: {exc}") from exc

    # -- 收尾 --------------------------------------------------------------

    def finalize(self) -> Dict[str, Any]:
        """落 metadata、关库、`os.replace` 就位。幂等。

        返回 `{'path','tile_count','minzoom','maxzoom','bounds','bytes'}`。
        `tile_count` 取自 `SELECT COUNT(*)` 而不是调用计数：`INSERT OR REPLACE`
        下重复坐标不增加行数，用计数器报出来的数会比库里真实的行数大。

        一块瓦片都没有时抛 MBTilesError 并删掉 part 文件 —— 空库推不出 bounds /
        minzoom / maxzoom 这三个必填键，写出来的东西不合规；而且调用方不该为
        一个空导出登记 Artifact。外部来源的空库仍由 `validate_mbtiles` 如实报告。
        """
        if self._result is not None:
            return dict(self._result)
        if self._conn is None:
            raise MBTilesError(f"MBTiles writer for {self.path} was aborted")

        self._flush()
        try:
            tile_count = int(self._conn.execute("SELECT COUNT(*) FROM tiles").fetchone()[0])
        except sqlite3.Error as exc:
            raise MBTilesError(f"Cannot count tiles in {self.part_path}: {exc}") from exc

        if tile_count == 0 or self._minzoom is None or self._west is None:
            self._abort()
            raise MBTilesError(
                f"Refusing to finalize an empty MBTiles at {self.path}: "
                "bounds/minzoom/maxzoom are required metadata and cannot be derived")

        bounds = (self._west, self._south, self._east, self._north)
        try:
            self._conn.executemany(
                "INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)",
                self._metadata_rows(bounds))
            self._conn.commit()
            self._conn.close()
        except sqlite3.Error as exc:
            raise MBTilesError(f"Cannot finalize MBTiles {self.part_path}: {exc}") from exc
        finally:
            self._conn = None

        # 不做 VACUUM：库是一次性顺序追加写出来的，没有删除也就没有可回收的
        # 空页，而 VACUUM 要把整库重写一遍 —— 峰值占用翻倍、耗时翻倍，正是
        # disk_budget 最不想在收尾阶段遇到的东西。
        try:
            os.replace(str(self.part_path), str(self.path))
        except OSError as exc:
            self._abort()
            raise MBTilesError(f"Cannot move MBTiles into place at {self.path}: {exc}") from exc

        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0

        self._result = {
            'path': str(self.path),
            'tile_count': tile_count,
            'minzoom': self._minzoom,
            'maxzoom': self._maxzoom,
            'bounds': [round(v, 6) for v in bounds],
            'bytes': size,
        }
        logger.info("Wrote MBTiles %s: %d tile(s), z%d-%d, %d bytes",
                    self.path, tile_count, self._minzoom, self._maxzoom, size)
        return dict(self._result)

    def _metadata_rows(self, bounds: Sequence[float]) -> List[Tuple[str, str]]:
        """组装 metadata 行。**永不产生 `scheme` 键**，见模块 docstring。"""
        rows: List[Tuple[str, str]] = [
            ('name', self.name),
            ('format', self.format),
            ('bounds', _format_bounds(bounds)),
            ('minzoom', str(self._minzoom)),
            ('maxzoom', str(self._maxzoom)),
            ('type', self.layer_type),
            ('version', self.version),
        ]
        if self.description:
            rows.append(('description', self.description))
        if self.attribution:
            rows.append(('attribution', self.attribution))
        if self.vector_layers:
            # 规范要求这是一个 JSON **对象**（含 vector_layers 数组），不是裸数组。
            rows.append(('json', json.dumps({'vector_layers': self.vector_layers},
                                            separators=(',', ':'), ensure_ascii=False)))
        return rows


def _scan_numeric_dirs(root: Path):
    """`root` 直下名字是纯数字的子目录，按 scandir 原序返回（顺序不影响结果）。"""
    try:
        with os.scandir(root) as it:
            entries = list(it)
    except OSError as exc:
        logger.warning("Cannot list %s: %s", root, exc)
        return []
    out = []
    for entry in entries:
        try:
            if entry.name.isdigit() and entry.is_dir(follow_symlinks=False):
                out.append(entry)
        except OSError:
            continue
    return out


# ---------------------------------------------------------------------------
# 读取端
# ---------------------------------------------------------------------------

def read_metadata(path) -> Dict[str, str]:
    """读回 metadata 表，原样返回 name → value 的字符串映射。

    刻意不解析 `json` / `bounds`：这是「文件里到底写了什么」的忠实视图，
    诊断一个可疑的库时任何一次善意的规整都会把线索抹掉。需要解析结果的
    调用方走 `validate_mbtiles`。
    """
    conn = _connect_ro(Path(path))
    try:
        return {str(name): '' if value is None else str(value)
                for name, value in conn.execute("SELECT name, value FROM metadata")}
    except sqlite3.Error as exc:
        raise MBTilesError(f"Cannot read metadata from {path}: {exc}") from exc
    finally:
        conn.close()


def read_tile(path, zoom: int, x: int, y: int) -> Optional[bytes]:
    """按 **XYZ** 坐标取一块瓦片，不存在返回 None。

    行号翻转在这里完成（`_tms_row`），所以预览路由与调用方全程不接触 TMS。
    坐标非法（越界、非整数、层级超范围）也返回 None 而不抛：这条路径服务的是
    HTTP 预览，一个手敲错的 URL 应当是 404，不是 500。
    """
    try:
        zoom_i, x_i, y_i = _check_tile_coords(zoom, x, y)
    except MBTilesError:
        return None
    conn = _connect_ro(Path(path))
    try:
        row = conn.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
            (zoom_i, x_i, _tms_row(zoom_i, y_i))).fetchone()
    except sqlite3.Error as exc:
        raise MBTilesError(f"Cannot read tile {zoom}/{x}/{y} from {path}: {exc}") from exc
    finally:
        conn.close()
    if row is None or row[0] is None:
        return None
    return bytes(row[0])


def validate_mbtiles(path) -> Dict[str, Any]:
    """校验一个 MBTiles，返回报告。**问题只报告，不抛。**

    返回
    ----
    `{'ok', 'tile_count', 'minzoom', 'maxzoom', 'format', 'bounds',
      'vector_layers', 'problems'}`

    报的键里 `minzoom` / `maxzoom` 是**瓦片表里的实际范围**（事实基准），
    `format` / `bounds` / `vector_layers` 报的是 metadata 的声明值。
    三者都会与库内事实对账，声明与事实不符一律进 `problems`：
    zoom 对瓦片表的实际层级，bounds 对实际瓦片的外包框，format 对抽查瓦片的
    文件头魔数（见 `_FORMAT_SAMPLE_TILES`）。

    不抛异常是刻意的：校验的调用场景是「产物做完了，给用户一份体检报告」，
    第一个问题就抛出去等于只能看见一个问题，而用户需要的是一次看全。
    """
    path = Path(path)
    problems: List[str] = []
    report: Dict[str, Any] = {
        'ok': False, 'tile_count': 0, 'minzoom': None, 'maxzoom': None,
        'format': '', 'bounds': None, 'vector_layers': [], 'problems': problems,
    }

    try:
        conn = _connect_ro(path)
    except MBTilesError as exc:
        problems.append(str(exc))
        return report

    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        if 'tiles' not in tables:
            problems.append("missing 'tiles' table")
        if 'metadata' not in tables:
            problems.append("missing 'metadata' table")
        if 'tiles' not in tables:
            # 没有瓦片表就无从校验其余任何一项，早退比给一堆连带报错有用。
            return report

        meta: Dict[str, str] = {}
        if 'metadata' in tables:
            meta = {str(name): '' if value is None else str(value)
                    for name, value in conn.execute("SELECT name, value FROM metadata")}

        # -- 必填 metadata（§5.3）------------------------------------------
        for key in ('name', 'format', 'bounds', 'minzoom', 'maxzoom'):
            if not meta.get(key, '').strip():
                problems.append(f"metadata is missing required key '{key}'")

        declared_format = meta.get('format', '').strip().lower()
        declared_format = _FORMAT_ALIASES.get(declared_format, declared_format)
        report['format'] = declared_format
        if declared_format and declared_format not in SUPPORTED_FORMATS:
            problems.append(
                f"metadata.format {declared_format!r} is not one of {'/'.join(SUPPORTED_FORMATS)}")

        # 写入端从不写 scheme；见到它就说明这个库要么不是本模块产的，要么被人
        # 改过 —— 而 scheme=xyz + TMS 行号会让读取端把地图上下颠倒。
        if 'scheme' in meta:
            problems.append(
                f"metadata declares scheme={meta['scheme']!r}; TMS is the spec default and "
                "writing 'xyz' alongside flipped rows renders the map upside down")

        declared_bounds = _parse_bounds(meta.get('bounds'))
        if meta.get('bounds', '').strip() and declared_bounds is None:
            problems.append(f"metadata.bounds is not four numbers: {meta.get('bounds')!r}")
        report['bounds'] = declared_bounds

        # -- 瓦片表事实 ------------------------------------------------------
        tile_count, actual_min, actual_max = conn.execute(
            "SELECT COUNT(*), MIN(zoom_level), MAX(zoom_level) FROM tiles").fetchone()
        tile_count = int(tile_count or 0)
        report['tile_count'] = tile_count
        report['minzoom'] = None if actual_min is None else int(actual_min)
        report['maxzoom'] = None if actual_max is None else int(actual_max)
        if tile_count == 0:
            problems.append("tiles table is empty")

        west = south = east = north = None
        for zoom, min_col, max_col, min_row, max_row in conn.execute(
                "SELECT zoom_level, MIN(tile_column), MAX(tile_column), "
                "MIN(tile_row), MAX(tile_row) FROM tiles GROUP BY zoom_level"):
            zoom = int(zoom)
            if not MIN_ZOOM <= zoom <= MAX_ZOOM:
                problems.append(f"zoom level {zoom} is outside [{MIN_ZOOM}, {MAX_ZOOM}]")
                continue
            limit = (1 << zoom) - 1
            if int(min_row) < 0 or int(max_row) > limit:
                problems.append(
                    f"z{zoom} has tile_row outside [0, {limit}] "
                    f"(found {int(min_row)}..{int(max_row)})")
            if int(min_col) < 0 or int(max_col) > limit:
                problems.append(
                    f"z{zoom} has tile_column outside [0, {limit}] "
                    f"(found {int(min_col)}..{int(max_col)})")
                continue
            # 外包框按 XYZ 还原：tile_row 是 TMS 行号，翻回去才能喂 tile_lon_lat_bounds。
            top = _tms_row(zoom, min(limit, max(0, int(max_row))))
            bottom = _tms_row(zoom, min(limit, max(0, int(min_row))))
            w, _, _, n = tile_lon_lat_bounds(zoom, int(min_col), top)
            _, s, e, _ = tile_lon_lat_bounds(zoom, int(max_col), bottom)
            west = w if west is None else min(west, w)
            south = s if south is None else min(south, s)
            east = e if east is None else max(east, e)
            north = n if north is None else max(north, n)

        # -- 声明 vs 事实 ----------------------------------------------------
        for key, actual in (('minzoom', report['minzoom']), ('maxzoom', report['maxzoom'])):
            raw = meta.get(key, '').strip()
            if not raw or actual is None:
                continue
            try:
                declared = int(raw)
            except ValueError:
                problems.append(f"metadata.{key} is not an integer: {raw!r}")
                continue
            if declared != actual:
                problems.append(
                    f"metadata.{key}={declared} but the tiles table has {actual}")

        if declared_bounds is not None and west is not None:
            if (declared_bounds[0] > west + _BOUNDS_EPSILON
                    or declared_bounds[1] > south + _BOUNDS_EPSILON
                    or declared_bounds[2] < east - _BOUNDS_EPSILON
                    or declared_bounds[3] < north - _BOUNDS_EPSILON):
                problems.append(
                    "metadata.bounds does not cover the tiles present "
                    f"(tiles span {_format_bounds((west, south, east, north))})")

        # -- 声明格式 vs 实际瓦片字节 ------------------------------------------
        # 这一段补的是校验端**比写入端还松**的那个洞：`add_tile` 见到魔数与
        # `fmt` 不符当场拒收，而校验端过去只看 metadata 里那个字符串 —— 把一个
        # PNG 库的 metadata.format 改成 jpeg，validate 照样报 ok:true。
        #
        # 这恰恰是校验端最该管的一格：它跑在**导出成品**上，也跑在**不是本程序
        # 写的文件**上（用户从别处拿来的库、被工具改过的库）。而 format 正是那
        # 个「决定库能不能被别人打开」的字段 —— 读取端拿它决定怎么解码每一块
        # 瓦片，声明成 jpeg 的 PNG 库在 QGIS / MapLibre / tileserver 里就是一片
        # 解不开的灰。声明与事实不符必须报出来，这与上面 minzoom/maxzoom、
        # bounds 的口径是同一条。
        #
        # 只报告不抛：与本函数其余部分同一个契约（体检报告要一次看全）。
        # 抽查条数与它抽不到的场景见 _FORMAT_SAMPLE_TILES。
        if declared_format in _FORMAT_MAGIC and tile_count:
            mismatched = []
            sampled = 0
            for zoom, column, row, blob in conn.execute(
                    "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles "
                    "LIMIT ?", (_FORMAT_SAMPLE_TILES,)):
                sampled += 1
                # NULL / 非 BLOB 的 tile_data 在这里也算不符：它同样是一块读取端
                # 解不开的瓦片，而 bytes() 之外的类型没有 startswith。
                data = blob if isinstance(blob, (bytes, bytearray)) else b''
                if _sniff_mismatch(declared_format, bytes(data)):
                    mismatched.append((int(zoom), int(column), int(row), bytes(data[:8])))
            if mismatched:
                zoom, column, row, magic = mismatched[0]
                problems.append(
                    f"metadata.format is {declared_format!r} but {len(mismatched)} of the "
                    f"{sampled} tiles sampled do not have that format's magic bytes "
                    f"(first: z{zoom} column {column} tile_row {row}, magic {magic!r}); "
                    f"readers decode every tile by metadata.format, so this library "
                    f"is unreadable outside TerraForge")

        # -- 矢量库 -----------------------------------------------------------
        if declared_format in VECTOR_FORMATS:
            raw_json = meta.get('json', '').strip()
            if not raw_json:
                problems.append("vector MBTiles is missing the 'json' metadata key "
                                "(MapLibre and tileserver cannot identify layers)")
            else:
                try:
                    parsed = json.loads(raw_json)
                except ValueError as exc:
                    problems.append(f"metadata.json is not valid JSON: {exc}")
                    parsed = None
                layers = (parsed or {}).get('vector_layers') if isinstance(parsed, dict) else None
                if not isinstance(layers, list) or not layers:
                    problems.append("metadata.json has no non-empty 'vector_layers' array")
                else:
                    report['vector_layers'] = layers
                    for index, layer in enumerate(layers):
                        if not isinstance(layer, dict) or not str(layer.get('id') or '').strip():
                            problems.append(f"vector_layers[{index}] has an empty 'id'")
    except sqlite3.Error as exc:
        # 损坏的库在这里表现为 DatabaseError；它同样是一条「体检结论」，不是
        # 调用方的编程错误，所以进 problems 而不是抛。
        problems.append(f"sqlite error while validating: {exc}")
    finally:
        conn.close()

    report['ok'] = not problems
    return report
