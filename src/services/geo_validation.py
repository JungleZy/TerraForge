"""bbox / zoom 边界校验 —— 地图瓦片、DEM、等高线三条管线共用。

背景:三条管线各自做(或不做)四至校验,缺口各异 —— DEM/等高线过去完全不查
范围(north=999 会生成一串不存在的颗粒名,任务跑到全失败);地图瓦片管线不查
east < west(反经线式输入被静默交换,下载的是完全错误的区域)。统一到这里,
所有创建任务的入口都用同一套规则、同一套报错。

规则(与 Leaflet 页面选区、Web Mercator 的输入语义一致):
  - 纬度 [-90, 90] 且 north > south(85.0511 的投影截断由瓦片计算层负责);
  - 经度 [-180, 180] 且 east > west;
  - 不支持跨反经线选区(east < west,如 170..-170)—— 静默交换会下载完全错误
    的区域,直接拒绝,让用户修正输入;
  - NaN / inf / 非数字一律拒绝。API 传入的是 JSON 值,None、列表会让 float()
    抛 TypeError(在 Flask 层变成 500),这里统一转成带字段名的 ValueError(400)。
"""

import math

MIN_ZOOM = 0   # 与 services/download_engine.py 的 MIN_ZOOM 一致
MAX_ZOOM = 21  # 与 services/download_engine.py 的 MAX_ZOOM 一致


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


def validate_bbox(north, south, east, west):
    """校验四至,返回 (north, south, east, west) 四个 float。"""
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
    if not (-180 <= e <= 180):
        raise ValueError(f"east ({e}) must be between -180 and 180")
    if e <= w:
        raise ValueError(f"east ({e}) must be greater than west ({w})")
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
        from core.config import Config

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
            f"保存路径必须是绝对路径（收到 {raw!r}），可点输入框旁的「浏览」选择"
        )
    resolved = p.resolve()
    # parts 含根（'/' 或 'C:\\'）：('/','a','b') = 3 即两级目录
    if len(resolved.parts) < 3:
        raise ValueError(
            f"保存路径至少需要两级目录（收到 {raw!r}），不要直接选根目录/盘符根"
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
