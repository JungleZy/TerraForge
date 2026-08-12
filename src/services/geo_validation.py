"""bbox / zoom 边界校验 —— 地图瓦片、DEM、等高线三条管线共用。

背景:三条管线各自做(或不做)四至校验,缺口各异 —— DEM/等高线过去完全不查
范围(north=999 会生成一串不存在的颗粒名,任务跑到全失败);地图瓦片管线不查
east < west(反经线式输入被静默交换,下载的是完全错误的区域)。统一到这里,
所有创建任务的入口都用同一套规则、同一套报错。

规则(与 Leaflet 页面选区、Web Mercator 的输入语义一致):
  - 纬度 [-90, 90] 且 north > south(85.0511 的投影截断由瓦片计算层负责);
  - 经度 [-180, 180] 且 east > west;
  - 裸四角输入不支持跨反经线(east < west,如 170..-170)—— 静默交换会下载完全
    错误的区域,直接拒绝,让用户修正输入。跨界的规范写法是 east > 180,由
    `validate_bbox(..., allow_unwrapped_east=True)` 接受,前提是调用方能证明
    这四个数来自一个真的跨界的 RegionSpec(详见 validate_bbox 上方那段注释);
  - NaN / inf / 非数字一律拒绝。API 传入的是 JSON 值,None、列表会让 float()
    抛 TypeError(在 Flask 层变成 500),这里统一转成带字段名的 ValueError(400)。
"""

import math

from src.i18n import t

MIN_ZOOM = 0   # 与 src/services/download_engine.py 的 MIN_ZOOM 一致
MAX_ZOOM = 21  # 与 src/services/download_engine.py 的 MAX_ZOOM 一致


def coerce_number(value, name):
    """把输入转成 float 并保证是有限数,失败抛带字段名的 ValueError。"""
    # JSON 布尔是 int 子类（float(True)=1.0），坐标/层级不含义布尔，显式拒绝。
    if isinstance(value, bool):
        raise ValueError(f"{name} ({value!r}) must be a number")
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} ({value!r}) must be a number") from None
    if not math.isfinite(f):
        raise ValueError(f"{name} ({value!r}) must be a finite number")
    return f

# ---------------------------------------------------------------------------
# ⚠️ 反经线：**不要**把下面 validate_bbox 的 `east <= west` 改成「静默交换」。
#
# 这四个数是 tasks / dem_tasks / contour_tasks / local_terrain_tasks 四张表的
# north/south/east/west 四列的合同。裸四角输入（用户在地图上拖了个框、没有
# 任何 region）的语义仍然是「两个经度都落在 ±180 内、east 严格大于 west 的
# 矩形」：这种输入里 `east < west` 唯一可能的来源是用户填反了，静默交换会下载
# 一块完全错误的区域 —— 那正是本文件当初存在的理由（见上面的规则第三条）。
#
# 跨反经线的**规范写法不是 east < west，而是 east > 180**：
# `src/contracts/region.py` 的 `RegionSpec` 把 `west=170, east=-170` 归一成
# `west=170, east=190`，`RegionSpec.antimeridian_parts` 再把它拆成 1~2 段各自
# 落在 ±180 内的 (n, s, e, w)，每一段都能原样喂给本函数与下游。
#
# `allow_unwrapped_east=True` 就是为这条规范写法开的口子，**只有**在调用方能
# 证明这四个数派生自一个真的跨界的 RegionSpec 时才可以传（今天唯一的调用点是
# `src/models/task.py` 的 `Task.__post_init__`，它拿任务自己的 `region_spec`
# 列当判据）。dem_tasks 早就在库里存着 `east=181.0` 这种未回绕值，四列合同
# 本来就容得下这个形状；地图管线是最后一个还在拒的，这个开关是来抹平它的。
#
# 判据必须是「region 真的跨界」而不是「有 region」：后者会让一个 east=250 的
# 垃圾值搭着一个普通多边形混进库里，而 250 在下游会被当成未回绕坐标 —— 那是
# 一块横跨大半个地球的错误下载区，正是本函数要挡的东西。
# ---------------------------------------------------------------------------


def validate_bbox(north, south, east, west, *, allow_unwrapped_east=False):
    """校验四至,返回 (north, south, east, west) 四个 float。

    `allow_unwrapped_east=True` 时 east 的上界放宽到 360，用来接受
    `RegionSpec` 归一后的跨反经线写法（west 仍在 ±180 内，east 落在
    (180, 360]）。开关的使用前提见上面那段注释：调用方必须已经证明这四个数
    来自一个 `crosses_antimeridian` 为真的 RegionSpec。
    """
    n = coerce_number(north, 'north')
    s = coerce_number(south, 'south')
    e = coerce_number(east, 'east')
    w = coerce_number(west, 'west')

    if n <= s:
        raise ValueError(f"north ({n}) must be greater than south ({s})")
    if not (-90 <= s <= 90):
        raise ValueError(f"south ({s}) must be between -90 and 90")
    if not (-90 <= n <= 90):
        raise ValueError(f"north ({n}) must be between -90 and 90")
    if not (-180 <= w <= 180):
        raise ValueError(f"west ({w}) must be between -180 and 180")
    # 上界跟着开关走，报错文案照旧带出实际上界 —— 裸四角路径拿到的仍然逐字是
    # "east (181.0) must be between -180 and 180"（API 合同，测试盯着它）。
    east_upper = 360.0 if allow_unwrapped_east else 180.0
    if not (-180 <= e <= east_upper):
        raise ValueError(f"east ({e}) must be between -180 and {east_upper:g}")
    if e <= w:
        raise ValueError(f"east ({e}) must be greater than west ({w})")
    # 只在放开上界之后才够得着：west 最低 -180、east 最高 360，跨度能到 540°。
    # 超过 360° 意味着同一条经线被绕了两遍,枚举出来的瓦片会重复。
    if e - w > 360.0:
        raise ValueError(f"east ({e}) minus west ({w}) must not exceed 360 degrees")
    return n, s, e, w


def validate_zoom(zoom, name):
    """校验缩放级别,返回 int。接受 12 和 "12";拒绝 12.5、-1、22、NaN。"""
    f = coerce_number(zoom, name)
    if not f.is_integer():
        raise ValueError(f"{name} ({zoom!r}) must be an integer")
    z = int(f)
    if not (MIN_ZOOM <= z <= MAX_ZOOM):
        raise ValueError(f"{name} ({z}) must be between {MIN_ZOOM} and {MAX_ZOOM}")
    return z


# 切片档位 -> 相对基准层级的偏移。**这是全项目唯一的取值表**：config_manager
# 的校验规则、管理器的缺省、路由的收参都从这里取，不要抄第二份。
# 三档为什么是「基准 ±1」而不是换三角化后端：实测层级旋钮的性价比是简化后端
# 的 2.4~3.9 倍，且它省时间、后端花时间。
# 依据：docs/reference/terrain/tiling-presets-measured.md 第四节。
TILING_QUALITY_OFFSETS = {
    'precision': 1,   # 基准 +1：约 3.3 倍体积换 2.8 倍精度
    'balanced': 0,    # 基准，默认
    'speed': -1,      # 基准 -1：约 1/3.3 体积、1/2.5 耗时
}
DEFAULT_TILING_QUALITY = 'balanced'


def validate_tiling_quality(value, name='quality'):
    """校验切片档位,返回规范化后的 str。只接受取值表里的三个字面量。

    刻意不做大小写归一、不做前后空白裁剪、不静默退回默认档:
    build_terrain 早年有过「triangulator 拼错静默走 else 分支、作业照样
    completed」的坑,这里当场报错、错误直指病因。
    """
    if not isinstance(value, str) or value not in TILING_QUALITY_OFFSETS:
        allowed = ', '.join(sorted(TILING_QUALITY_OFFSETS))
        raise ValueError(f"{name} ({value!r}) must be one of: {allowed}")
    return value


def coerce_vertex_normals(value, name='vertex_normals'):
    """把请求里的法线开关收成三态：True / False / None。None = 未传，走配置默认。

    两种形态都收：JSON body 给的是真布尔 `true`，multipart 表单给的是字符串
    `'true'`/`'false'`（布尔在本仓一律以这两个字面量传递，见 `database.DEFAULT_CONFIGS`
    里的 terrain_vertex_normals）。
    `None` 与空串都算未传 —— 表单里没填的控件送上来就是空串。

    **这是 vertex_normals 唯一的把关点。** 两个管理器拿到值以后只做
    `bool()`（`dem_task_manager.start_tiling` 与
    `local_terrain_task_manager.create_task_with_files` 里各一句），
    而 `bool('false')` 和 `bool('on')` 都是 True —— 校验没有第二道网。

    为什么不像本仓另外五处布尔收参（api.py、contour_api.py、dem_api.py 等）
    那样把 `'on'`/`'1'`/`'yes'` 也认成 True：那五处是两态开关，这里是**三态**。
    原生 checkbox 没勾时**根本不发这个字段**，而本 API 把「不发」定义成「走
    配置默认」。于是收下 `'on'` 就等于承认「勾了=on、没勾=不发字段」这套编码，
    「用户取消勾选」和「用户没表态」被压成同一态：配置默认是开的话，用户明明
    取消了勾选、瓦片照样烘法线，全程零报错。而法线烘进瓦片，事后想关只能重切
    （见 `database.DEFAULT_CONFIGS` 里 terrain_vertex_normals 上方那条 ⚠️ 注释）。
    三态要表达「显式关闭」，前端就必须显式发 `'false'`。

    ⚠️ 给前端：`static/js/map.js` 收参的既有写法是 `el?.value || '默认值'`，
    照抄到 checkbox 上会每次都 400 —— checkbox 的 `.value` 恒为 `'on'`，与
    checked 无关。必须写 `String(el.checked)`。
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    # 白名单用元组不用 set：JSON 送得进不可哈希的值（`{"vertex_normals": []}`），
    # `in {...}` 会抛 TypeError -> 路由漏成 500；元组比较走 == ,照常 ValueError -> 400。
    if value in ('true', 'false'):
        return value == 'true'
    raise ValueError(
        f"{name} ({value!r}) must be one of: true, false "
        f"(for a checkbox send String(el.checked), not its .value)")


# 「自动」层级的对外字面量与落库哨兵。
#
# 为什么落库要用哨兵而不是 NULL：dem_terrain_jobs.maxzoom 与
# local_terrain_tasks.maxzoom 都是 `INTEGER NOT NULL` 无默认，SQLite 去掉
# NOT NULL 要走 12 步重建表，而本仓的迁移约定是「幂等 ALTER ADD COLUMN」
# （见 CLAUDE.md「Database conventions」）。validate_zoom 的值域是 0..21，
# 用户输入永远到不了 -1，哨兵不存在撞车。
#
# ⚠️ 别和 effective_maxzoom 的 DEFAULT NULL 记混：那里的 NULL 是「还不知道
# 切到了第几级」，这里的 -1 是「基准不是一个数字」。两个列语义正交 ——
# 自动挡下 maxzoom = -1，effective_maxzoom 照常记录实际切到的层级。
AUTO_MAXZOOM = 'auto'
AUTO_MAXZOOM_SENTINEL = -1


def coerce_maxzoom(value, name='maxzoom'):
    """把请求里的最大切片层级收成三态：int / 'auto' / None。

    - `'auto'` = 按源数据像素尺寸现算基准层级（`build_terrain` 收到
      `max_level=None` 时走 `GeographicTilingScheme.estimate_max_level`）；
    - `int` = 用户指定的基准层级，值域仍由 `validate_zoom` 把关；
    - `None`（`None` 与空串）= 未表态，调用方回落到配置 `terrain_local_maxzoom`。

    **这是 maxzoom 唯一的把关点。** 两个管理器过了这里就直接落库/构造
    TileParams，没有第二道网。

    刻意不做大小写归一、不裁前后空白（`validate_tiling_quality` 定的同一条
    规矩）：拼错的档位静默走 else 分支、作业照样 completed，是本仓栽过的坑。
    `'AUTO'` 当场 ValueError → 400。

    那条规矩之所以站得住，是因为 `validate_tiling_quality` 的报错枚举了白名单
    （`must be one of: ...`）—— 用户拼错了也看得见合法取值。这里的数字分支委托
    给 `validate_zoom`，它只会说「不是数字」，`'AUTO'` 拿到的暗示会变成「auto
    根本不被支持」。所以把它的报错原样接住、补一句合法字面量，理由和兑现理由的
    机制才对得上。取值表仍然只有 AUTO_MAXZOOM 一份。

    `-1` 从外部传进来同样是 ValueError —— 它是内部落库表示，不是输入格式。
    这条由 validate_zoom 的下界天然保证，不需要额外分支。
    """
    if value is None or value == "":
        return None
    # 与 coerce_vertex_normals 同款：用 == 比较而不是 `in {...}`，JSON 送得进
    # 不可哈希的值（`{"maxzoom": []}`），集合成员判定会抛 TypeError → 500。
    if value == AUTO_MAXZOOM:
        return AUTO_MAXZOOM
    try:
        return validate_zoom(value, name)
    except ValueError as e:
        raise ValueError(f"{e} (or the literal {AUTO_MAXZOOM!r})") from None


def maxzoom_to_db(maxzoom):
    """归一后的 maxzoom → 落库整数。`'auto'` 存成哨兵，数字原样。

    只接受 `coerce_maxzoom` 的非 None 返回值；调用方不许自己写 -1。
    """
    return AUTO_MAXZOOM_SENTINEL if maxzoom == AUTO_MAXZOOM else int(maxzoom)


def maxzoom_from_db(value):
    """落库整数 → `TileParams.maxzoom` 的形态。哨兵还原成 None，其余是 int。

    `None` 正是 `build_terrain(max_level=None)` 触发按源分辨率估算的那一态，
    所以这个函数的返回值可以直接进 TileParams，不需要调用方再判一次。
    """
    v = int(value)
    return None if v == AUTO_MAXZOOM_SENTINEL else v


def resolve_output_dir(raw, base_dir=None):
    """把请求里的 output_path 解析成绝对路径，并强制落在 base_dir 之内。

    - base_dir 缺省为 Config.DOWNLOADS_DIR（惰性 import，避免 services→config 的
      模块级依赖影响测试 monkey-patch 顺序）。
    - 相对路径一律相对 base_dir 解析——不依赖进程 CWD，冻结 exe 换目录运行后
      行为不变。
    - 解析结果必须等于 base_dir 或位于其内部；`../` 逃逸、指向别处的绝对路径
      抛 ValueError（调用方在 API 层转成 400）。
    返回 str 绝对路径（不创建目录）。

    注意：这是「读历史数据」的兼容入口（存量任务行可能是相对路径）。
    新的用户输入走 require_absolute_output_dir —— 相对值直接拒绝。
    """
    from pathlib import Path

    if base_dir is None:
        from src.core.config import Config

        base_dir = Config.DOWNLOADS_DIR
    base = Path(base_dir).resolve()
    p = Path(str(raw)).expanduser()
    resolved = (base / p).resolve() if not p.is_absolute() else p.resolve()
    if resolved != base and base not in resolved.parents:
        raise ValueError(
            f"output_path ({raw!r}) resolves outside the downloads directory"
        )
    return str(resolved)


def require_absolute_output_dir(raw, base_dir=None):
    """校验用户输入的保存路径：绝对路径 + 至少两级目录深度。

    边界沿革：0.2.3 起要求绝对路径（相对值直接拒绝，不再按 CWD 解析）；
    0.2.4 起放开全盘 —— 保存目录可任选（「浏览」弹窗全盘可选），不再强制
    落在 DOWNLOADS_DIR 内。保留的两条底线：

    - 相对路径拒绝：exe 换目录启动后落盘位置会漂移；
    - 深度不足两级拒绝（如 `/`、`C:\\`、`/home`）：产物写成 <path>/task_<id>/，
      浅层路径删除保护（remove_task_dir_if_safe）挡不住手滑选根目录的风险。

    base_dir 形参保留只为调用点签名兼容，已不再参与校验。
    返回 str 绝对路径（不创建目录）。
    """
    from pathlib import Path

    p = Path(str(raw)).expanduser()
    if not p.is_absolute():
        raise ValueError(
            t('val.geo.output_path.must_be_absolute', path=repr(raw))
        )
    resolved = p.resolve()
    # parts 含根（'/' 或 'C:\\'）：('/','a','b') = 3 即两级目录
    if len(resolved.parts) < 3:
        raise ValueError(
            t('val.geo.output_path.min_depth', path=repr(raw))
        )
    return str(resolved)


def sanitize_filename(name, default="task"):
    """把任务名消毒成可安全拼进文件名的字符串。

    路径分隔符、`..`、控制字符全部换成下划线；首尾空白/点去掉；结果为空时
    返回 default。不保证唯一性，只保证不能逃逸出父目录。
    """
    import re

    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(name))
    s = s.replace("..", "_").strip(" ._")
    return s or default
