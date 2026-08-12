"""区域文件导入 —— GeoJSON / KML / KMZ / Shapefile → RegionSpec（§5.1 阶段 2）。

## 职责边界

几何合法性（环闭合、自相交、零面积、四至值域、跨反经线归一、绕向归一）**全部**
在 `RegionSpec.from_polygons` / `from_geojson` 里。本模块只做两件事：**解码**
（把五种容器格式拆成 `[(外环, 洞环, ...), ...]` 的顶点表）与**重投影**（只有
shapefile 会带非 WGS84 的坐标）。在这里再写一遍几何校验就是第二套规则，和合同
迟早分叉 —— 那正是改造前四条管线各写一套 bbox 校验的老毛病。

## 借鉴与不重蹈的覆辙（docs/notes/external-projects-takeaways.md §5.1）

- **洞环必须贯通**。GeoDownloader 的洞环丢失不在掩膜层：它的 Rust 掩膜本来就是
  奇偶扫描线（`merger.rs:270-295`），传内环就能挖对洞；丢信息的是提取端只取了
  外环（`region-selector.tsx:42,45`）。所以这里 KML 的 `innerBoundaryIs`、
  GeoJSON 的第 2..n 个环、OGR 的第 2..n 个 ring 一律原样带进 `from_polygons`，
  本模块不提供任何「只要外环」的分支。
- **裸 `.shp` 缺 `.prj` 不许静默**。GeoD 把原始坐标直接当经纬度用
  （`geo-import.ts:95`），一份 UTM 的省界会静静地落到几内亚湾外的空海上，用户
  只看到「下载出来全是海」。这里保留「按原样读取」的行为（一律拒绝会把只有裸
  `.shp` 的用户整类挡在门外），但打 WARNING 说清楚，并且投影坐标（动辄几十万）
  会撞上 `RegionSpec` 的经纬度值域校验而被拒 —— 静默错位是不可能发生的。
- **CRS 不匹配宁可报错**。GeoJSON 按 RFC 7946 的定义就是 WGS84；文件里还写着一个
  别的 `crs` 成员只有两种可能：要么是 2008 版遗留的 CRS84 / EPSG:4326 别名（无害，
  放行），要么坐标真的不是经纬度（静默接受 = 整个区域位移到别的半球）。

## 安全

输入是**用户上传的任意字节**，五条拒绝服务面必须在解析前 / 解析中堵死：

1. `MAX_IMPORT_BYTES` —— 在任何解析动作之前封顶，避免先把几百 MB 读进内存再拒；
2. zip / kmz 的条目数与解压后总量封顶 —— 42 KB 的 zip 炸弹能解出 4.5 PB；
3. XML 的 DOCTYPE 直接拒 —— billion laughs：十层嵌套实体在几 KB 的文档里就能让
   ElementTree 展开出几 GB 字符串，进程 OOM。没有 DOCTYPE 就没有自定义实体，
   剩下的五个预定义实体（&lt; 等）不会递归。这道守卫**必须按文档自己的编码**
   比对（见 `_xml_encoding`）：按死的 UTF-8 字节找 `<!DOCTYPE`，一份另存为
   UTF-16 的同样文件就能整个绕过去，而 expat 认 BOM、照样展开（实测 8 MB
   上传把 RSS 顶到 1.2 GB）；
4. 顶点数在**读取过程中**递减封顶（`_VertexBudget`）—— 只在读完之后按
   `spec.vertex_count` 判等于闸门装在墙倒之后：一个 443 KB 的恶意 zip 能在到达
   那一行之前吃掉 631 MB；
5. 报错消息里的用户内容一律走 `_echo` 截短 —— 一个 20 MB 的 `<coordinates>`
   token 会变成一条 20 MB 的异常消息，而路由层会把它当**一行**写进轮转日志。

zip 与 kmz 都**不解包到磁盘**（kmz 走内存、shapefile 走 GDAL 的 `/vsizip/`），
所以 `../../etc/passwd` 这类条目名穿越在本模块里没有落点。
"""

from __future__ import annotations

import codecs
import io
import json
import logging
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

from src.contracts.region import RegionSpec, RegionValidationError

logger = logging.getLogger(__name__)

__all__ = [
    'MAX_IMPORT_BYTES',
    'RegionImportError',
    'RegionImportResult',
    'SUPPORTED_EXTENSIONS',
    'WARNING_CODES',
    'import_region',
]


#: 导入成功但「结果可能不是你以为的那样」时回给调用方的**机器码**。
#:
#: 为什么是码而不是句子：这些警告最终要显示在界面上，而界面是双语的。服务层
#: 回中文就把一份文案钉死在服务层，回英文就在中文界面上甩生英文 —— 路由层
#: `/api/region/import` 早就为「跨反经线」定下了口径（回码，`js.region.*`
#: 归前端），这里沿用同一条，不另立第二套。
#:
#: 为什么必须回而不是只写日志：其中 `unreadable_crs` / `missing_crs` 正是本模块
#: docstring 里指名不能重蹈的那个坑 —— GeoDownloader 把没有 CRS 的坐标当经纬度
#: 用，用户只看到「下载出来全是海」。只写 `logger.warning` 等于坑还在，只是
#: 换了个没人看的地方摆着。
WARNING_CODES = (
    'missing_crs',                   # 压根没有 .prj，坐标按 WGS84 原样使用
    'unreadable_crs',                # 有 .prj 但 GDAL 解不出坐标系，同样原样使用
    'skipped_non_polygon_features',  # 点/线要素被跳过
    'encoding_fallback_gb18030',     # 文本不是 UTF-8，按 GB18030 解出来的
    'extension_content_mismatch',    # 扩展名与魔数不符，按内容解析
)


class RegionImportResult(NamedTuple):
    """`import_region` 的返回值：区域本身 + 一串警告码。

    用 NamedTuple 而不是让 `import_region` 继续只返回 RegionSpec：警告是
    **每次导入独有**的，挂模块级变量在多线程 Flask 里会串台，塞进 RegionSpec
    则会一路跟着序列化进 tasks 表（那是几何契约，不是一次导入的过程信息）。
    """

    spec: RegionSpec
    warnings: Tuple[str, ...]


#: 报错消息里回显用户内容时的长度上限。
#:
#: 这些消息会被路由原样写进轮转日志（`routes/api.py` 的 `logger.warning`），
#: 而「用户内容」可以是一个 20 MB 的 `<coordinates>` token 或一个任意长的文件名
#: —— 把它整段插进异常消息，等于一行日志 20 MB，日志轮转直接失去意义，
#: 界面上的错误框也变成一堵墙。80 个字符足够让用户认出是哪一段出了问题。
_MAX_ECHO_CHARS = 80


def _echo(value: Any) -> str:
    """把用户内容截短后再放进报错消息，附上原始长度。

    附长度是必须的：只给前 80 个字符会让「这个 token 有 2000 万字符」这个
    **最关键的事实**消失，用户会以为是那 80 个字符本身写错了。
    """
    text = value if isinstance(value, str) else str(value)
    if len(text) <= _MAX_ECHO_CHARS:
        return repr(text)
    return f'{text[:_MAX_ECHO_CHARS]!r}…（共 {len(text)} 字符）'


#: 转述下层异常消息时的长度上限。比 `_MAX_ECHO_CHARS` 宽：那些消息是我们自己
#: 或 stdlib 写的说明文字，值得完整读到；封顶只为堵住其中**嵌了用户内容**的
#: 那几条（`float()` 的 "could not convert string to float: '<整个 token>'"
#: 就会原样带上一个 3 MB 的字符串）。
_MAX_NESTED_CHARS = 400


def _clip(value: Any, limit: int = _MAX_NESTED_CHARS) -> str:
    """截短一段消息文本，不加引号（`_echo` 加引号，那是回显用户内容用的）。"""
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text
    return f'{text[:limit]}…（共 {len(text)} 字符）'


class RegionImportError(ValueError):
    """上传的区域文件读不出来（格式、大小、CRS、几何任一环节）。

    继承 `ValueError`：路由层统一 `except ValueError -> HTTP 400`，导入失败是
    用户输入问题而不是服务端故障，不该进 500 也不该惊动告警。消息面向用户，
    所以是可直接展示的大白话英文（与 `geo_validation` 的报错同口径），说清楚
    「哪一步失败」和「怎么改」，不要只写一个异常类名。
    """


# 受理的扩展名。真正的分派以魔数为准（见 `_sniff`），这个元组是给路由做前置
# 过滤和给用户看的清单 —— 扩展名从来只是提示，浏览器上传的 .zip 里装的是 kmz
# 还是 shapefile 只有看内容才知道。
SUPPORTED_EXTENSIONS = ('.geojson', '.json', '.kml', '.kmz', '.zip', '.shp')

# 单个上传的硬上限。区域边界不是栅格：一个省级行政区的 GeoJSON 通常几 MB，
# 全国县级面也就二十来 MB。32 MiB 之上基本只有两种东西 —— 误传的栅格，和刻意
# 构造的解析炸弹。全局的 `Config.MAX_CONTENT_LENGTH`（2 GiB）是给本地地形上传
# 留的，套在这里等于允许对方先让服务端把 2 GiB 缓进内存再被拒。
MAX_IMPORT_BYTES = 32 * 1024 * 1024

# zip / kmz 的两道解压闸门。条目数挡「上万个空文件」型的目录炸弹，解压总量挡
# 经典的高压缩比炸弹。shapefile 包正常是 3~8 个文件（shp/shx/dbf/prj/cpg/...），
# kmz 里除 doc.kml 外还可能带一批图标，256 个条目足够宽松。
MAX_ZIP_ENTRIES = 256
MAX_ZIP_UNCOMPRESSED_BYTES = 128 * 1024 * 1024

# 顶点总数上限。文件大小挡不住这一面：32 MiB 的二进制 shapefile 能装两百万个
# 点，构造出来的 RegionSpec 合法但没法用 —— 每一步瓦片枚举、掩膜、面积估算都
# 要遍历它，UI 上的每次预览都会卡住整个进程。宁可在这里明确让用户先做简化。
MAX_TOTAL_VERTICES = 1_000_000

# 扩展名 → 容器类型。zip 与 kmz 归一到 'zip'：两者都是 zip 容器，里面装的是
# .kml 还是 .shp 由 `_import_zip` 看条目决定，不靠扩展名猜。
_EXT_KIND = {
    '.geojson': 'json',
    '.json': 'json',
    '.kml': 'xml',
    '.kmz': 'zip',
    '.zip': 'zip',
    '.shp': 'shp',
}

_BOM_UTF8 = b'\xef\xbb\xbf'

# shapefile 主文件头的前 4 字节是大端 9994（ESRI Shapefile Technical
# Description, July 1998, 第 4 页的 File Code）。
_SHP_MAGIC = b'\x00\x00\x27\x0a'

_ZIP_MAGIC = b'PK\x03\x04'

# KML 的 <coordinates> 规范写法是「逗号分隔分量、空白分隔点」，但导出器在逗号
# 两侧塞空格是常见现象（Google Earth 手工编辑、部分 GIS 的换行排版）。直接
# split() 会把 "-122.0, 37.0" 拆成两个 token，一个缺分量、一个只有纬度，
# 于是整个环报错。先把逗号周围的空白吃掉，语义不变而容错大一截。
_COORD_COMMA = re.compile(r'\s*,\s*')

# 展示名的长度上限。名字来自文件名或 KML 的 <name>，两者都可以是任意长；它会
# 进任务列表、进日志摘要（`RegionSpec.summary`），不封顶就是让人往界面里塞一
# 整段文本。120 字符足够放下「浙江省杭州市余杭区行政区划」这类真实名字。
_MAX_DISPLAY_NAME = 120


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def import_region(filename: str, data: bytes) -> RegionImportResult:
    """上传的区域文件 → `RegionImportResult(spec, warnings)`。

    `filename` 只用来取展示名和做**第一轮**格式猜测；真正的分派看魔数，扩展名
    说谎（把 kmz 存成 .zip、把 GeoJSON 存成 .txt、把 shapefile 的 zip 存成 .kmz）
    在真实上传里比想象中多。两者不一致时以内容为准并打一条 WARNING —— 按扩展名
    硬走会给出「invalid GeoJSON: Expecting value」这种指错方向的报错。

    多个要素合并成一个 MultiPolygon：用户选了一个文件，要的就是这个文件覆盖的
    整个范围，而不是「第一个要素」。非面要素（点、线）跳过；一个面都没有则抛错，
    绝不静默产出空区域 —— 空区域会一路走到下载阶段才暴露成「0 张瓦片」。

    `warnings` 是 `WARNING_CODES` 里的机器码，按发现顺序、去重。它们**不是**
    错误：文件导进来了，但结果可能不是用户以为的那个（最典型的是坐标系读不出来
    而坐标被当成经纬度）。调用方有义务显示它们 —— 只写日志就等于没告诉用户。
    """
    if data is None:
        raise RegionImportError("no file content was uploaded")
    if isinstance(data, (bytearray, memoryview)):
        data = bytes(data)
    if not isinstance(data, bytes):
        raise RegionImportError("file content must be bytes")

    # 顺序要紧：先量大小再碰内容。放到解析里面去检查等于这道闸门不存在。
    if not data:
        raise RegionImportError("the uploaded file is empty")
    if len(data) > MAX_IMPORT_BYTES:
        raise RegionImportError(
            f"file is too large: {len(data) / 1048576:.1f} MiB exceeds the "
            f"{MAX_IMPORT_BYTES // 1048576} MiB limit for region files. "
            f"Simplify the boundary or clip it to the area you need.")

    warnings: List[str] = []
    name = _base_name(filename)
    ext = _extension(filename)
    declared = _EXT_KIND.get(ext)
    sniffed = _sniff(data)
    if declared and sniffed and declared != sniffed:
        logger.warning("区域文件 %s 的扩展名(%s)与内容(%s)不符，按内容解析",
                       _echo(filename), declared, sniffed)
        _warn(warnings, 'extension_content_mismatch')
    kind = sniffed or declared
    if kind is None:
        raise RegionImportError(
            f"unrecognised region file {_echo(filename)}: expected one of "
            f"{', '.join(SUPPORTED_EXTENSIONS)} (GeoJSON, KML, KMZ, "
            f"zipped shapefile, or a bare .shp)")

    if kind == 'json':
        spec = _import_geojson(data, name, warnings)
    elif kind == 'xml':
        spec = _import_kml(data, name)
    elif kind == 'zip':
        spec = _import_zip(data, name, warnings)
    else:
        spec = _import_bare_shp(data, name, warnings)

    # shapefile 那条路径已经在读取过程中按顶点数增量掐过一次（见 `_VertexBudget`）；
    # 这里这道是 GeoJSON / KML 的闸门，也是最终口径的唯一事实源。
    if spec.vertex_count > MAX_TOTAL_VERTICES:
        raise RegionImportError(
            f"region has {spec.vertex_count} vertices, more than the "
            f"{MAX_TOTAL_VERTICES} supported. Simplify the geometry "
            f"(QGIS: Vector > Geometry Tools > Simplify) and import again.")
    logger.info("导入区域 %s：%s（警告 %s）", _echo(filename), spec.summary(),
                ', '.join(warnings) or '无')
    return RegionImportResult(spec, tuple(warnings))


def _warn(warnings: List[str], code: str) -> None:
    """登记一个警告码，去重。

    去重是因为同一个原因可以在多个图层上各触发一次（一个 shapefile 包里三个
    图层都没有 .prj），界面上把同一句话说三遍只会让人以为是三个不同的问题。
    """
    if code not in warnings:
        warnings.append(code)


# ---------------------------------------------------------------------------
# 文件名 / 魔数
# ---------------------------------------------------------------------------

def _base_name(filename: str) -> str:
    """取不带扩展名的文件名，去路径、去控制字符、封长度。

    上传的 filename 是**客户端给的字符串**，不是路径：IE/部分客户端会给完整的
    `C:\\Users\\x\\a.kml`，`Path().name` 在 POSIX 上不认反斜杠，会把整串当文件名。
    这里只当它是展示文本用，不做任何文件系统操作，所以统一替换分隔符即可。
    """
    if not filename:
        return ''
    raw = str(filename).replace('\\', '/').rsplit('/', 1)[-1]
    stem = raw[:-len(_extension(raw))] if _extension(raw) else raw
    # 控制字符会把日志行截断、把 JSON 打脏（\r 在终端里能把上一行盖掉）。
    cleaned = ''.join(ch for ch in stem if ch.isprintable()).strip()
    return cleaned[:_MAX_DISPLAY_NAME]


def _extension(filename: str) -> str:
    if not filename:
        return ''
    tail = str(filename).replace('\\', '/').rsplit('/', 1)[-1]
    dot = tail.rfind('.')
    return tail[dot:].lower() if dot > 0 else ''


#: BOM → 编码名。**长的排前面**：UTF-32 的 BOM 以 UTF-16 的 BOM 开头
#: （`FF FE 00 00` vs `FF FE`），先比短的会把 UTF-32LE 认成 UTF-16LE。
#: 一律用带端序的具体名（`utf-16-le` 而不是 `utf-16`）：`'x'.encode('utf-16')`
#: 会自己再加一个 BOM，拿它去和文件里的字节比对必然对不上。
_XML_BOMS = (
    (b'\xff\xfe\x00\x00', 'utf-32-le'),
    (b'\x00\x00\xfe\xff', 'utf-32-be'),
    (_BOM_UTF8, 'utf-8'),
    (b'\xff\xfe', 'utf-16-le'),
    (b'\xfe\xff', 'utf-16-be'),
)

#: 没有 BOM 时按首字符 `<`（U+003C）的字节形状反推编码（XML 1.0 附录 F）。
#: 同样是长的在前：UTF-32LE 的 `3C 00 00 00` 以 UTF-16LE 的 `3C 00` 开头。
_XML_LEADING = (
    (b'\x00\x00\x00\x3c', 'utf-32-be'),
    (b'\x3c\x00\x00\x00', 'utf-32-le'),
    (b'\x00\x3c', 'utf-16-be'),
    (b'\x3c\x00', 'utf-16-le'),
)

#: XML 声明里的 encoding 伪属性。只在**头部**扫，声明按规范必须紧贴文档开头。
_XML_DECL_ENC = re.compile(rb'''<\?xml[^>]{0,200}?encoding\s*=\s*["']([A-Za-z0-9_.\-]{1,40})["']''')


def _xml_encoding(data: bytes) -> Tuple[str, int]:
    """判定 XML 字节流的编码，返回 `(编码名, BOM 之后的起点)`。

    存在的唯一理由是 `_reject_doctype` 必须和 expat **看同一份文档**。
    expat 会按 BOM 自动识别 UTF-16，而按裸字节找 `b'<!DOCTYPE'` 的守卫只认
    UTF-8 形状 —— UTF-16 里它是 `3C 00 21 00 44 00 ...`，守卫静悄悄放行，
    解析器却老老实实展开 DTD。实测一个 8 MB 的 UTF-16 KML 能把 RSS 顶到 1.2 GB。
    换句话说：**换一种编码重存一遍就绕过了守卫**，这不是守卫，是装饰。

    ASCII 兼容族（UTF-8 / GB18030 / latin-1 …）统一按 latin-1 处理：`<!DOCTYPE`
    这九个字符在它们里面逐字节相同，分不分得清具体是哪一个对本判定没有影响，
    而 latin-1 永不抛错，不会让一个编码问题变成一次误拒。
    """
    for bom, enc in _XML_BOMS:
        if data.startswith(bom):
            return enc, len(bom)
    for lead, enc in _XML_LEADING:
        if data.startswith(lead):
            return enc, 0
    # 既没有 BOM 也不是 UTF-16/32 的字节形状，但声明里可能写着一个
    # **非 ASCII 兼容**的编码（EBCDIC 一类）。expat 会照声明走，我们也必须跟着，
    # 否则又是一次「守卫看到的和解析器看到的不是同一份文档」。
    m = _XML_DECL_ENC.match(data[:256])
    if m:
        try:
            declared = codecs.lookup(m.group(1).decode('ascii')).name
        except (LookupError, UnicodeDecodeError):
            declared = ''
        # utf-16/32 已经被上面两轮覆盖；声明说是它们而字节不是，属于文件自相
        # 矛盾，交给 expat 去报错即可。
        if declared and not declared.startswith(('utf-16', 'utf-32')):
            try:
                if '<'.encode(declared) != b'<':
                    return declared, 0
            except (LookupError, UnicodeEncodeError):
                pass
    return 'latin-1', 0


def _sniff(data: bytes) -> Optional[str]:
    """看头几个字节判断容器类型；认不出返回 None（此时才退回扩展名）。

    文本类（json / xml）走 `_xml_encoding` 而不是直接切字节：UTF-16 的 KML
    第一个字节是 `3C` 或 `00`，按 UTF-8 看要么对不上要么是个 NUL，`_sniff`
    返回 None，分派就退回扩展名 —— 一份存成 `.txt` 的 UTF-16 KML 于是根本
    进不了 KML 分支。和 DOCTYPE 守卫是同一个病根：识别口径必须和解析器一致。
    """
    if data.startswith(_ZIP_MAGIC):
        return 'zip'
    if data.startswith(_SHP_MAGIC):
        return 'shp'
    enc, offset = _xml_encoding(data)
    # 512 字节是给「BOM + 声明 + 一段空白/注释」留的余量；切在多字节字符中间
    # 由 errors='replace' 兜住，只影响最后一个字符，不影响首个非空白字符的判定。
    head = data[offset:offset + 512].decode(enc, 'replace').lstrip()
    if head[:1] in ('{', '['):
        return 'json'
    if head[:1] == '<':
        return 'xml'
    return None


# ---------------------------------------------------------------------------
# GeoJSON
# ---------------------------------------------------------------------------

# 合法的 CRS 写法。RFC 7946 已经删掉了 crs 成员（坐标一律 WGS84 经纬度），但
# 2008 版的文件仍然在流通，QGIS 至今还会写 urn:ogc:def:crs:OGC:1.3/CRS84。
# 只要归一化后落在这个集合里就是 WGS84 的同义词，放行；其它一律拒。
_WGS84_CRS_TOKENS = frozenset({
    'crs84', 'ogc:crs84', 'urn:ogc:def:crs:ogc:1.3/crs84',
    'urn:ogc:def:crs:ogc:2:84', 'urn:ogc:def:crs:ogc::crs84',
    'epsg:4326', 'urn:ogc:def:crs:epsg::4326', 'epsg::4326', '4326',
    'urn:ogc:def:crs:epsg:6.6:4326', 'wgs84', 'wgs 84',
    'http://www.opengis.net/def/crs/ogc/1.3/crs84',
    'http://www.opengis.net/gml/srs/epsg.xml#4326',
})


def _import_geojson(data: bytes, name: str, warnings: List[str]) -> RegionSpec:
    text = _decode_text(data, warnings)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RegionImportError(f"invalid GeoJSON: {_clip(exc)}") from None
    if not isinstance(obj, dict):
        raise RegionImportError(
            "invalid GeoJSON: the top level must be an object "
            "(FeatureCollection, Feature, or a geometry)")
    _check_geojson_crs(obj)
    try:
        return RegionSpec.from_geojson(obj, source='imported', display_name=name)
    except ValueError as exc:
        # catch 的是 ValueError 而不是只 catch RegionValidationError：契约层在
        # 顶点上直接调 `float()`，一个非数值坐标抛的是**裸 ValueError**，消息里
        # 原样带着整个 token（实测一份 3 MB 的坏坐标 → 3 MB 的异常消息）。
        # 它会绕过这里、落到蓝图的通用 `except ValueError -> 400`，那条分支回的
        # 是 `str(e)` —— 一次上传换来一条 3 MB 的响应和一行 3 MB 的日志。
        # 「几何合法性归契约层」说的是判定归它，不是**消息形制**也归它。
        raise RegionImportError(
            f"GeoJSON region is not usable: {_clip(exc)}") from exc


def _check_geojson_crs(obj: Dict[str, Any]) -> None:
    """遗留 `crs` 成员指向非 WGS84 时直接拒。

    这是本模块唯一一处「宁可让用户重导一次也不放行」的判断。理由是错误代价不
    对称：坐标当成经纬度用，一份 EPSG:3857 的面（坐标量级 1e7）会被 RegionSpec
    的值域校验挡下来，用户至少看得见报错；而一份 EPSG:4490（CGCS2000 经纬度）
    或 EPSG:4214（北京 54）的面**数值上完全像 WGS84**，静默接受的结果是整个区
    域平移几十到几百米，下载完成、图看着也对，错位要到叠加时才发现。
    """
    crs = obj.get('crs')
    if crs is None:
        return
    if not isinstance(crs, dict):
        raise RegionImportError(
            "invalid GeoJSON: the legacy 'crs' member must be an object")
    props = crs.get('properties')
    props = props if isinstance(props, dict) else {}
    # 两种遗留写法：{"type":"name","properties":{"name":...}} 与
    # {"type":"EPSG","properties":{"code":4326}}；href 型（link）无法离线解析。
    token = props.get('name') or props.get('code') or props.get('href')
    if token is None:
        # crs 成员在但没写清是哪一个：按 RFC 7946 的默认（WGS84）走，因为拒绝
        # 一个只是多写了个空壳的合法文件比放行更糟。
        return
    normalised = str(token).strip().lower().replace(' ', '')
    if normalised in _WGS84_CRS_TOKENS or normalised.endswith(':4326'):
        return
    raise RegionImportError(
        f"GeoJSON declares CRS {_echo(token)}, but GeoJSON coordinates must be "
        f"WGS84 longitude/latitude (RFC 7946). Reproject the file to "
        f"EPSG:4326 before importing — importing it as-is would silently "
        f"place the region in the wrong location.")


def _decode_text(data: bytes, warnings: List[str]) -> str:
    """字节 → 文本。UTF-8（含 BOM）优先，其次 GB18030。

    JSON 按 RFC 8259 必须是 UTF-8，但国内导出的 GeoJSON 里带 GBK 中文名字段的
    情况真实存在，而字段值只是属性、几何本身是 ASCII 数字 —— 因为一个用不上的
    属性名解不开而拒绝整个文件不值当。GB18030 是 GBK/GB2312 的超集。
    不做 latin-1 兜底：那个永远成功，等于把乱码当成合法内容往下传。
    """
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        pass
    try:
        text = data.decode('gb18030')
    except UnicodeDecodeError:
        raise RegionImportError(
            "cannot decode the file as text: it is neither UTF-8 nor GB18030. "
            "Re-export it as UTF-8.") from None
    logger.warning("区域文件不是 UTF-8，按 GB18030 解码")
    _warn(warnings, 'encoding_fallback_gb18030')
    return text


# ---------------------------------------------------------------------------
# KML / KMZ
# ---------------------------------------------------------------------------

def _import_kml(data: bytes, name: str) -> RegionSpec:
    _reject_doctype(data)
    try:
        # 直接喂 bytes：XML 声明里的 encoding 由 expat 自己认（KML 有 GBK 导出），
        # 先 decode 再 fromstring 反而会因为声明与实际编码不一致而报错。
        # 不传 parser=：默认解析器不解析外部实体，自定义解析器才有引狼入室的风险。
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise RegionImportError(f"invalid KML: {_clip(exc)}") from None

    polygons: List[List[List[Tuple[float, float]]]] = []
    placemarks = [el for el in root.iter() if _local(el.tag) == 'Placemark']
    for placemark in placemarks:
        # 直接在 Placemark 的**后代**里找 Polygon，一举覆盖三种嵌套：裸 Polygon、
        # MultiGeometry 下的多个 Polygon、以及 MultiGeometry 套 MultiGeometry。
        for poly_el in (el for el in placemark.iter() if _local(el.tag) == 'Polygon'):
            rings = _kml_polygon_rings(poly_el)
            if rings:
                polygons.append(rings)
    if not polygons:
        # 少数导出器（含部分 WMS GetFeatureInfo 的 KML 响应）把几何直接挂在
        # Document 下而不套 Placemark。只在 Placemark 一无所获时才这样兜底，
        # 避免把同一个多边形数两遍。
        for poly_el in (el for el in root.iter() if _local(el.tag) == 'Polygon'):
            rings = _kml_polygon_rings(poly_el)
            if rings:
                polygons.append(rings)
    if not polygons:
        raise RegionImportError(
            "the KML contains no Polygon geometry (points, paths and overlays "
            "cannot define a download region)")

    display = _kml_display_name(root, placemarks) or name
    try:
        return RegionSpec.from_polygons(polygons, source='imported',
                                        display_name=display)
    except ValueError as exc:
        # 与 `_import_geojson` 同理：契约层的裸 ValueError 会带上整段用户内容。
        raise RegionImportError(f"KML region is not usable: {_clip(exc)}") from exc


def _find_aligned(data: bytes, needle: bytes, start: int, width: int, base: int) -> int:
    """在 `data` 里找 `needle`，但只接受落在编码单元边界上的命中。

    错位命中在单字节编码里不可能出现，在 UTF-16/32 里则是一条现成的绕过路径：
    构造出一个横跨两个字符的假 `-->`（UTF-16LE 下就是字符 U+2D00、U+3E2D 的
    某种排列），扫描器会以为注释在这里结束，从注释**内部**继续往下读，看到的
    是半个字符的乱码，于是判定「已经到根元素了」直接放行 —— 而真正的 DOCTYPE
    还在后面等着 expat 去展开。
    """
    at = data.find(needle, start)
    while at >= 0 and (at - base) % width:
        at = data.find(needle, at + 1)
    return at


def _reject_doctype(data: bytes) -> None:
    """序言里出现 DOCTYPE 就拒 —— billion laughs（XML 实体展开炸弹）。

    十来层互相引用的内部实体（`<!ENTITY a "&b;&b;...">`）能让几 KB 的文档在
    展开后变成几 GB 的字符串，ElementTree 会老老实实展开到内存耗尽；这不是
    「解析慢」，是**进程被一个上传打死**。KML 用不到 DTD，直接整类拒绝最干净。
    没有 DOCTYPE 就无法定义自定义实体，剩下五个预定义实体不会递归。

    只扫**序言**而不是全文搜 `<!DOCTYPE`：`<description>` 里贴着一段 HTML 教程、
    正文里出现这九个字符的合法文件是存在的，全文搜会把它们误杀。序言的结构是
    确定的（BOM、空白、`<?...?>`、`<!--...-->`），逐段跳过即可精确停在根元素。

    **所有比对都在 `_xml_encoding` 判出来的编码里做**，而不是写死 UTF-8 字节。
    写死的版本可以用「另存为 UTF-16」一步绕过：`<!DOCTYPE` 在 UTF-16LE 里是
    `3C 00 21 00 44 00 ...`，`b'<!DOCTYPE'` 一个字节也对不上，守卫无声放行，
    而 `ET.fromstring` 认得 BOM、照常展开 DTD（实测 8 MB 上传 → RSS 1.2 GB）。
    编码是 expat 自己判的，守卫就必须按同一套判据来判，否则它守的是另一份文档。

    按编码单元走字节而不是先把整份文件 decode 成 str：正常 KML 的序言只有几十
    个字节，为了看这几十个字节把一份 32 MiB 的上传整体解成字符串是纯浪费。
    """
    enc, base = _xml_encoding(data)
    width = 4 if enc.startswith('utf-32') else 2 if enc.startswith('utf-16') else 1
    doctype = '<!DOCTYPE'.encode(enc)
    comment_open, comment_close = '<!--'.encode(enc), '-->'.encode(enc)
    pi_open, pi_close = '<?'.encode(enc), '?>'.encode(enc)

    i = base
    n = len(data)
    while i < n:
        while i + width <= n and data[i:i + width].decode(enc, 'replace').isspace():
            i += width
        if data[i:i + len(doctype)] == doctype:
            raise RegionImportError(
                "the KML declares a DOCTYPE, which is rejected: XML entity "
                "expansion can be used to exhaust server memory. Re-export "
                "the file without a DTD.")
        if data[i:i + len(comment_open)] == comment_open:
            end = _find_aligned(data, comment_close, i + len(comment_open), width, base)
            if end < 0:
                return          # 注释不闭合 —— 交给解析器去报 ParseError
            i = end + len(comment_close)
            continue
        if data[i:i + len(pi_open)] == pi_open:
            end = _find_aligned(data, pi_close, i + len(pi_open), width, base)
            if end < 0:
                return
            i = end + len(pi_close)
            continue
        return                  # 到根元素了，序言里没有 DOCTYPE


def _local(tag: Any) -> str:
    """去掉 `{namespace}` 前缀。

    KML 在野外至少有四种命名空间 URI（opengis 2.2、google earth 2.0/2.1、
    kml 2.2 的各种拼法），还有相当一部分导出器**根本不写** xmlns。按全名匹配
    就是给自己挖坑：文件能在 Google Earth 里打开，在我们这儿却「没有多边形」。
    """
    if not isinstance(tag, str):
        return ''
    return tag.rsplit('}', 1)[-1]


def _find_local(parent: ET.Element, name: str) -> Optional[ET.Element]:
    for el in parent.iter():
        if el is not parent and _local(el.tag) == name:
            return el
    return None


def _kml_polygon_rings(poly_el: ET.Element) -> Optional[List[List[Tuple[float, float]]]]:
    """一个 `<Polygon>` → `[外环, 洞环...]`；取不到外环返回 None。"""
    rings: List[List[Tuple[float, float]]] = []
    outer = _find_local(poly_el, 'outerBoundaryIs')
    if outer is not None:
        coords_el = _find_local(outer, 'coordinates')
    else:
        # outerBoundaryIs 是 KML 2.2 的必选项，但确实有导出器把 LinearRing 直接
        # 挂在 Polygon 下。能读就读，读不出来再放弃。
        coords_el = _find_local(poly_el, 'coordinates')
    if coords_el is None:
        return None
    rings.append(_parse_kml_coordinates(coords_el.text, 'outer boundary'))

    # 洞环：一个 Polygon 可以有任意多个 innerBoundaryIs。这一段就是 GeoD 丢掉的
    # 信息，`from_polygons` 收到之后会把洞环绕向归一成 CW，掩膜按奇偶规则挖洞。
    for inner in poly_el.iter():
        if _local(inner.tag) != 'innerBoundaryIs':
            continue
        inner_coords = _find_local(inner, 'coordinates')
        if inner_coords is None:
            # 空的洞环元素不该让整个多边形作废：外环仍然是用户要的范围，
            # 少挖一个洞比整个文件导不进来轻得多。
            logger.warning("KML 的 innerBoundaryIs 里没有 coordinates，跳过该洞环")
            continue
        rings.append(_parse_kml_coordinates(inner_coords.text, 'inner boundary'))
    return rings


def _parse_kml_coordinates(text: Optional[str], where: str) -> List[Tuple[float, float]]:
    """KML 的 `lon,lat[,alt]` 坐标串 → `[(lon, lat), ...]`。

    坏点一律抛错而不是跳过：跳过一个解不开的顶点等于悄悄改了边界形状，用户拿到
    的是一个「导入成功」但和源文件不一样的区域 —— 比报错难查得多。

    报错里的 token 一律走 `_echo` 截短。`<coordinates>` 的内容**没有空白**时
    整段就是一个 token：一份 20 MB 的 `<coordinates>` 会生成一条 20 MB 的异常
    消息，而 `routes/api.py` 把它 `logger.warning` 成日志里的**一行** ——
    一次上传就能把轮转日志的整个额度吃光，界面上的错误框也变成一堵墙。
    """
    if not text or not text.strip():
        raise RegionImportError(f"KML {where} has an empty <coordinates> element")
    points: List[Tuple[float, float]] = []
    for token in _COORD_COMMA.sub(',', text.strip()).split():
        parts = token.split(',')
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise RegionImportError(
                f"KML {where} has a malformed coordinate {_echo(token)}: "
                f"expected 'longitude,latitude[,altitude]'")
        try:
            points.append((float(parts[0]), float(parts[1])))
        except ValueError:
            raise RegionImportError(
                f"KML {where} has a non-numeric coordinate {_echo(token)}") from None
    if len(points) < 3:
        raise RegionImportError(
            f"KML {where} has only {len(points)} point(s); a ring needs at least 3")
    return points


def _kml_display_name(root: ET.Element, placemarks: Sequence[ET.Element]) -> str:
    """文档级 `<name>`（Document / Folder / kml 的直接子节点）。

    只认直接子节点：`root.iter('name')` 会先撞上 `<Style>` 或第一个 Placemark
    里的名字，把「某某省」变成「未命名路径 1」。文档没写名字、而整份文件只有
    一个 Placemark 时，那个 Placemark 的名字就是用户心里的区域名，可以用。
    """
    containers = [root]
    containers.extend(el for el in root if _local(el.tag) in ('Document', 'Folder'))
    for parent in containers:
        for child in parent:
            if _local(child.tag) == 'name' and (child.text or '').strip():
                return child.text.strip()[:_MAX_DISPLAY_NAME]
    if len(placemarks) == 1:
        for child in placemarks[0]:
            if _local(child.tag) == 'name' and (child.text or '').strip():
                return child.text.strip()[:_MAX_DISPLAY_NAME]
    return ''


# ---------------------------------------------------------------------------
# zip 容器（kmz / shapefile 包）
# ---------------------------------------------------------------------------

def _import_zip(data: bytes, name: str, warnings: List[str]) -> RegionSpec:
    """zip 容器：里面是 .kml 就走 KML，是 .shp 就走 shapefile。

    不按扩展名（.kmz vs .zip）分派：两者都是 zip，用户把 kmz 改名成 zip、或者
    把 shapefile 包命名成 .kmz 都不影响能不能读出区域，看内容才是稳的。
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        # BadZipFile 不是 ValueError，漏出去就是 HTTP 500 —— 而截断的上传、
        # 分卷压缩包的第二卷都会走到这里，它们全是用户输入问题。
        raise RegionImportError(
            f"the archive is corrupt or truncated: {_clip(exc)}") from None
    with zf:
        entries = _guard_zip(zf)
        kml_member = _pick_kml_member(entries)
        if kml_member is not None:
            # KMZ 的展示名优先用 KML 文档名（_import_kml 内部已处理），不覆盖。
            return _import_kml(_read_zip_member(zf, kml_member), name)
        shp_member = _pick_shp_member(entries)
        # 「有没有 .prj」必须在这里判：进了 GDAL 之后，`layer.GetSpatialRef()`
        # 对「压根没有 .prj」和「.prj 在但解不出坐标系」返回的都是 None，
        # 而这两件事对用户是完全不同的动作（重新打包 vs 修坐标系定义）。
        prj_present = _has_prj_sibling(entries, shp_member) if shp_member else False
    if shp_member is None:
        raise RegionImportError(
            "the archive contains neither a .kml (KMZ) nor a .shp (zipped "
            "shapefile); nothing in it defines a region")
    return _import_zipped_shapefile(data, shp_member, name, warnings,
                                    prj_present=prj_present)


def _has_prj_sibling(entries: Sequence[zipfile.ZipInfo], shp_member: str) -> bool:
    """压缩包里有没有和这个 .shp 同名的 .prj。

    按**大小写不敏感**比：shapefile 的兄弟文件扩展名大小写在野外什么都有
    （ArcGIS 导出常写成 `.PRJ`），而 zip 的条目名是区分大小写的。
    """
    want = shp_member[:-4].lower() + '.prj'
    return any(e.filename.lower() == want for e in entries)


def _guard_zip(zf: zipfile.ZipFile) -> List[zipfile.ZipInfo]:
    """条目数与解压后总量的闸门，返回真正的文件条目（去目录、去垃圾）。"""
    entries = [
        info for info in zf.infolist()
        if not info.is_dir()
        and not info.filename.startswith('__MACOSX/')      # macOS 的资源分叉副本
        and not info.filename.rsplit('/', 1)[-1].startswith('._')
    ]
    if not entries:
        raise RegionImportError("the archive is empty")
    if len(entries) > MAX_ZIP_ENTRIES:
        raise RegionImportError(
            f"the archive has {len(entries)} files, more than the "
            f"{MAX_ZIP_ENTRIES} allowed; this does not look like a region file")
    total = sum(max(0, info.file_size) for info in entries)
    if total > MAX_ZIP_UNCOMPRESSED_BYTES:
        raise RegionImportError(
            f"the archive expands to {total / 1048576:.0f} MiB, more than the "
            f"{MAX_ZIP_UNCOMPRESSED_BYTES // 1048576} MiB allowed")
    return entries


def _read_zip_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    """读一个条目，读取时再按 MAX_IMPORT_BYTES 截断一次。

    `info.file_size` 来自中央目录，是**攻击者可控的自述值**：炸弹可以声称自己
    解压后只有 1 KB。`_guard_zip` 的总量检查因此只是第一道；真正花内存的是这里
    的 read，必须自己封顶而不是相信声明值。
    """
    limit = MAX_IMPORT_BYTES
    with zf.open(info) as fh:
        payload = fh.read(limit + 1)
    if len(payload) > limit:
        raise RegionImportError(
            f"{_echo(info.filename)} inside the archive expands beyond the "
            f"{limit // 1048576} MiB limit (declared {info.file_size} bytes) — "
            f"refusing to decompress it")
    if not payload:
        raise RegionImportError(f"{_echo(info.filename)} inside the archive is empty")
    return payload


def _pick_kml_member(entries: Sequence[zipfile.ZipInfo]) -> Optional[zipfile.ZipInfo]:
    """KMZ 的主文档：优先 doc.kml（OGC KMZ 规范的约定名），否则第一个 .kml。

    排序按条目名而不是按 zip 内的物理顺序：同一份 kmz 在不同打包器下条目顺序不同，
    「第一个」必须是可复现的，否则同一个文件两次导入可能得到不同区域。
    """
    kmls = sorted((e for e in entries if e.filename.lower().endswith('.kml')),
                  key=lambda e: e.filename.lower())
    if not kmls:
        return None
    for entry in kmls:
        if entry.filename.rsplit('/', 1)[-1].lower() == 'doc.kml':
            return entry
    if len(kmls) > 1:
        logger.warning("KMZ 里有 %d 个 .kml，使用 %s", len(kmls), kmls[0].filename)
    return kmls[0]


def _pick_shp_member(entries: Sequence[zipfile.ZipInfo]) -> Optional[str]:
    shps = sorted((e.filename for e in entries if e.filename.lower().endswith('.shp')),
                  key=str.lower)
    if not shps:
        return None
    if len(shps) > 1:
        logger.warning("压缩包里有 %d 个 .shp，使用 %s", len(shps), shps[0])
    return shps[0]


# ---------------------------------------------------------------------------
# Shapefile（OGR）
# ---------------------------------------------------------------------------

# 临时目录前缀。本模块的临时目录是**请求级**的（with 块内建、出块即删），和
# download_engine / contour_engine 那些跨分钟的作业目录不是一回事，所以没有登记
# 进 task_cleanup 的启动清扫表。真被 SIGKILL 打断最多残留一份 ≤32 MiB 的上传副本；
# 若将来把导入改成异步作业，这个前缀必须同步登记到 task_cleanup（见那里的 ⚠️）。
_TMP_PREFIX = 'region_import_'


def _import_zipped_shapefile(data: bytes, member: str, name: str,
                             warnings: List[str], *,
                             prj_present: bool) -> RegionSpec:
    """zip 包里的 shapefile：落一份临时 zip，用 GDAL 的 `/vsizip/` 直接读。

    不解包：shapefile 是**一组**文件（.shp 几何 / .shx 索引 / .dbf 属性 /
    .prj 坐标系），OGR 靠同名兄弟文件自己找；解包一遍除了给条目名穿越留口子，
    没有任何好处。`/vsizip/` 让 GDAL 在压缩包里按需寻址，兄弟文件照样找得到。
    """
    with tempfile.TemporaryDirectory(prefix=_TMP_PREFIX) as tmp:
        archive = Path(tmp) / 'region.zip'
        archive.write_bytes(data)
        # /vsizip/ 的路径分隔符固定是 '/'（GDAL 虚拟文件系统语法，不是 OS 路径）。
        vsi_path = f"/vsizip/{archive.as_posix()}/{member}"
        return _read_ogr_polygons(vsi_path, name, warnings,
                                  inside_archive=True, prj_present=prj_present)


def _import_bare_shp(data: bytes, name: str, warnings: List[str]) -> RegionSpec:
    """裸 `.shp`：没有 .prj，也就没有坐标系可言。

    保留读取能力（拒绝会把只拿到 .shp 的用户整类挡在外面），但坐标只能原样当
    经纬度用 —— 这正是 GeoD 的静默失败点（`geo-import.ts:95`）。区别在于我们
    会明确警告，而且投影坐标（米级，量级 1e5~1e7）会撞上 RegionSpec 的经纬度
    值域校验被拒，不会像 GeoD 那样静静地把区域挪到别的地方。
    """
    logger.warning(
        "导入裸 .shp（%s）：没有 .prj，坐标按 WGS84 经纬度原样使用；"
        "若源数据是投影坐标系，结果会落在错误的位置", name or 'unnamed')
    with tempfile.TemporaryDirectory(prefix=_TMP_PREFIX) as tmp:
        # 文件名随意但扩展名必须是 .shp：OGR 的 ESRI Shapefile 驱动按扩展名识别。
        shp = Path(tmp) / 'region.shp'
        shp.write_bytes(data)
        # restore_shx：裸 .shp 按定义就是「兄弟文件都没了」，其中 .shx（要素
        # 偏移索引）缺失会让 OGR 直接开不了数据源 —— 实测 GDAL 3.11 的报错是
        # 「Unable to open ...shx, set SHAPE_RESTORE_SHX to YES」。索引本来就能
        # 从 .shp 逐要素扫出来，不打开这个开关，这条路径等于根本不存在。
        return _read_ogr_polygons(str(shp), name, warnings,
                                  inside_archive=False, prj_present=False,
                                  restore_shx=True)


def _ogr_open(path: str, ogr, *, restore_shx: bool):
    """`ogr.Open`，按需临时打开 SHAPE_RESTORE_SHX。

    用 `SetThreadLocalConfigOption` 而不是 `SetConfigOption`：后者是**进程全局**
    的，四条管线共用一个 Flask 进程，一次导入把全局开关掀开再关上，中间任何一个
    并发的 GDAL 调用都会看到不同的配置 —— 这种竞态出问题时根本查不出来。
    用完还原成原值（而不是无脑设空），避免踩掉外层调用方自己设的值。
    """
    if not restore_shx:
        return ogr.Open(path)
    from osgeo import gdal
    previous = gdal.GetThreadLocalConfigOption('SHAPE_RESTORE_SHX')
    gdal.SetThreadLocalConfigOption('SHAPE_RESTORE_SHX', 'YES')
    try:
        return ogr.Open(path)
    finally:
        gdal.SetThreadLocalConfigOption('SHAPE_RESTORE_SHX', previous)


def _read_ogr_polygons(path: str, name: str, warnings: List[str], *,
                       inside_archive: bool, prj_present: bool,
                       restore_shx: bool = False) -> RegionSpec:
    """OGR 数据源 → RegionSpec，带 `.prj` 时重投影到 EPSG:4326。"""
    try:
        from osgeo import ogr, osr
    except ImportError:
        # 模块级 import 会让**整个模块**在没装 GDAL 的机器上导入失败，连
        # GeoJSON / KML 这两条纯 stdlib 的路径都跟着废掉（测试机就常常没有 GDAL）。
        raise RegionImportError(
            "shapefile import requires GDAL/OGR, which is not installed in "
            "this environment. Convert the shapefile to GeoJSON or KML "
            "(QGIS: right-click layer > Export), or install GDAL.") from None

    from src.core.gdal_mode import pin_gdal_exception_mode
    pin_gdal_exception_mode()   # 下面靠 `Open(...) is None` 判错，见 src/core/gdal_mode.py

    ds = _ogr_open(path, ogr, restore_shx=restore_shx)
    if ds is None:
        raise RegionImportError(
            "GDAL could not open the shapefile"
            + (" inside the archive" if inside_archive else "")
            + ". It may be corrupt, or the .shx index file may be missing "
              "(a shapefile is a set of files: .shp, .shx, .dbf and .prj — "
              "zip the whole set together).")
    budget = _VertexBudget(MAX_TOTAL_VERTICES)
    try:
        polygons: List[List[List[Tuple[float, float]]]] = []
        skipped = 0
        for i in range(ds.GetLayerCount()):
            layer = ds.GetLayerByIndex(i)
            if layer is None:
                continue
            transform = _build_transform(layer, ogr, osr, warnings,
                                         inside_archive=inside_archive,
                                         prj_present=prj_present)
            layer.ResetReading()
            for feature in layer:
                if feature is None:
                    continue
                geom = feature.GetGeometryRef()
                if geom is None:
                    continue
                if transform is not None:
                    geom = geom.Clone()
                    if geom.Transform(transform) != 0:
                        raise RegionImportError(
                            "reprojecting the shapefile to WGS84 failed; the "
                            "source CRS may be unsupported or the coordinates "
                            "may fall outside its area of use")
                skipped += _collect_ogr_polygons(geom, polygons, ogr, depth=0,
                                                 budget=budget)
    finally:
        # 显式释放：GDAL 的 Dataset 在 /vsizip/ 上持有临时 zip 的句柄，
        # Windows 上不放手，外层 TemporaryDirectory 的删除就会失败。
        ds = None

    if not polygons:
        raise RegionImportError(
            "the shapefile contains no polygon features"
            + (f" ({skipped} non-polygon feature(s) skipped)" if skipped else "")
            + "; point and line layers cannot define a download region")
    if skipped:
        logger.warning("shapefile 里跳过了 %d 个非面要素", skipped)
        _warn(warnings, 'skipped_non_polygon_features')
    try:
        return RegionSpec.from_polygons(polygons, source='imported', display_name=name)
    except ValueError as exc:
        raise RegionImportError(
            f"shapefile region is not usable: {_clip(exc)}") from exc


def _build_transform(layer, ogr, osr, warnings: List[str], *,
                     inside_archive: bool, prj_present: bool):
    """图层坐标系 → EPSG:4326 的变换；已经是 4326 或读不出坐标系时返回 None。

    ⚠️ 轴序：GDAL 3 起，`ImportFromEPSG(4326)` 得到的 SRS **按官方权威定义是
    (纬度, 经度)** 序，而不是 GIS 惯用的 (x=经度, y=纬度)。不显式声明
    `OAMS_TRADITIONAL_GIS_ORDER`，`TransformPoint` 的返回值就会经纬互换 ——
    北京 (116.4, 39.9) 变成 (39.9, 116.4)，落在印度洋里。这是 shapefile 导入
    最常见的一个 bug，而且它**不报错**，只是把区域搬到地球另一边。
    两侧 SRS 都要设：源是 EPSG:4490 之类的地理坐标系时，源侧同样有轴序问题。
    """
    srs = layer.GetSpatialRef()
    if srs is None:
        # None 有两种成因，对用户是两个不同的动作，所以分开报：
        #   - 压根没有 .prj（打包时漏了 / 裸 .shp）  → 重新打包整套文件；
        #   - .prj 在，但 GDAL 解不出坐标系（WKT 写坏了、是个空文件、
        #     用了本机 PROJ 数据库里没有的自定义基准面）→ 修坐标系定义。
        # 无论哪种，接下来都会把原始坐标**当成经纬度**用 —— 这正是模块
        # docstring 里点名不能重蹈的 GeoDownloader 静默错位。以前这里只有一条
        # `logger.warning`，而 HTTP 响应的 warnings 列表只由 crosses_antimeridian
        # 一个条件构成，用户什么都看不到：等于坑还在，只是挪进了日志。
        if prj_present:
            logger.warning("shapefile 的 .prj 解析不出坐标系，坐标按 WGS84 经纬度"
                           "原样使用；若源数据是投影坐标系，结果会落在错误的位置")
            _warn(warnings, 'unreadable_crs')
        else:
            if inside_archive:
                logger.warning("压缩包里没有 .prj，坐标按 WGS84 经纬度原样使用；"
                               "若源数据是投影坐标系，结果会落在错误的位置")
            _warn(warnings, 'missing_crs')
        return None
    source = srs.Clone()
    source.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    target = osr.SpatialReference()
    target.ImportFromEPSG(4326)
    target.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    if source.IsSame(target):
        # 已经是 WGS84：跳过变换省下每个顶点一次调用，也避免往返浮点噪声把
        # 「整数度的行政区边界」变成 39.99999999996。
        return None
    logger.info("shapefile 坐标系 %s → EPSG:4326", source.GetName() or 'unknown')
    return osr.CoordinateTransformation(source, target)


class _VertexBudget:
    """递减式顶点预算，用尽即抛。

    为什么不能只在 `import_region` 末尾按 `spec.vertex_count` 判：那道检查在
    **整份几何已经在内存里**之后才执行。实测一个 443 KB 的恶意 zip 能在到达
    那一行之前吃掉 631 MB —— 闸门在墙倒之后才关。预算必须跟着读取过程递减，
    并且在调 `GetPoints()` **之前**先按 `GetPointCount()` 扣：GetPoints 本身
    就是那次分配（一个环两百万个点 = 两百万个 Python 元组）。

    上限与 `MAX_TOTAL_VERTICES` 是同一个数，口径只有一份。
    """

    __slots__ = ('remaining',)

    def __init__(self, limit: int) -> None:
        self.remaining = limit

    def take(self, count: int) -> None:
        self.remaining -= count
        if self.remaining < 0:
            raise RegionImportError(
                f"the shapefile has more than {MAX_TOTAL_VERTICES} vertices. "
                f"Simplify the geometry (QGIS: Vector > Geometry Tools > "
                f"Simplify) and import again.")


def _collect_ogr_polygons(geom, out: List, ogr, depth: int,
                          budget: '_VertexBudget') -> int:
    """递归收集面几何，返回跳过的非面要素个数。

    depth 上限防御病态嵌套的 GeometryCollection（与
    `region._collect_geojson_polygons` 同一理由：一个畸形文件不该能把栈打穿）。
    """
    if geom is None or depth > 8:
        return 0
    gtype = ogr.GT_Flatten(geom.GetGeometryType())
    if gtype == ogr.wkbPolygon:
        rings: List[List[Tuple[float, float]]] = []
        for i in range(geom.GetGeometryCount()):
            ring = geom.GetGeometryRef(i)
            if ring is None:
                continue
            count = ring.GetPointCount()
            if count < 3:
                continue
            # 先扣预算再 GetPoints：反过来的话这一行就已经把几百 MB 建出来了。
            budget.take(count)
            pts = ring.GetPoints()      # 一次拿全，比逐点 GetX/GetY 快一个量级
            if not pts:
                continue
            # 3D shapefile（PolygonZ）的点是三元组，高程对区域没有意义，丢掉。
            # **原地**改写而不是列表推导：推导会在旧列表还活着的时候把新列表
            # 整个建出来，峰值内存翻倍 —— 上百万顶点时那是几百 MB 的差别。
            # 2D 数据（绝大多数）连改写都省了，GetPoints 给的就是 (x, y) 元组。
            if len(pts[0]) > 2:
                for idx, p in enumerate(pts):
                    pts[idx] = (p[0], p[1])
            rings.append(pts)
        # 第 2..n 个 ring 就是洞环，原样带上 —— 见模块 docstring 的「洞环必须贯通」。
        if rings:
            out.append(rings)
        return 0
    if gtype in (ogr.wkbMultiPolygon, ogr.wkbGeometryCollection):
        skipped = 0
        for i in range(geom.GetGeometryCount()):
            skipped += _collect_ogr_polygons(geom.GetGeometryRef(i), out, ogr,
                                             depth + 1, budget)
        return skipped
    if gtype in (ogr.wkbCurvePolygon, ogr.wkbMultiSurface):
        # shapefile 本身没有曲线几何，但 /vsizip/ 里塞的可能是别的 OGR 能认的
        # 格式；线性化一次就能按普通面处理，比整类跳过友好。
        return _collect_ogr_polygons(geom.GetLinearGeometry(), out, ogr,
                                     depth + 1, budget)
    return 1
