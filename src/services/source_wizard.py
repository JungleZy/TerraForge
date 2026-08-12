"""瓦片源向导：把用户粘到输入框里的**一条真实瓦片 URL**变成可复用的 SourceSnapshot。

## 它堵的洞

自定义源的老流程是让用户自己写模板：把 `https://a.tile.example/12/3410/1655.png`
手工改成 `https://a.tile.example/{z}/{x}/{y}.png`。三种错法天天发生：

- 改错位置（`/{z}/{y}/{x}` 或把 `.png` 一起吃掉）—— 校验能过，下载全 404，
  而 404 在下载引擎里只是「这块失败」，用户看到的是一个跑完但大面积空洞的成品；
- 漏改一个占位符 —— `validate_server_entry` 会拒（缺 `{z}/{x}/{y}` 之一），
  但报错时用户手里已经没有原始 URL 了，只能重新去抓一次；
- 顺手把 `?key=xxx` 一起粘进来 —— 那串凭据会**原样**进 `SourceSnapshot.url_template`，
  于是进 tasks 表、进 fingerprint 的输入、进诊断包。没人提醒过他。

`analyze_tile_url` 把这三件事变成机器判断：从 URL 里找出所有整数槽位，穷举
(z, x, y) 的指派，用 `x, y < 2**z` 这条硬约束筛掉不可能的组合，再按「命名参数 >
连续路径段」排序取第一名。**筛不出来就报错，绝不猜**——猜错的代价是一个跑完
才发现是空的任务。

## 边界：报告，不代劳

`a.tile.example` 这种单字母首标签几乎肯定是子域名轮换（a/b/c），但本模块
**不会**把它改写成 `{s}`：下载引擎 `download_engine.py:494-497` 只替换
`{z}/{x}/{y}`，而 `tile_url_probe._ALLOWED_PLACEHOLDERS` 直接拒绝 `{s}`。
吐一个 `{s}` 模板出去，等于生成一条**必定过不了自身校验**的配置。所以
`subdomains` 只作为候选报出来，模板保持单主机 —— 轮换要用户自己填服务器列表。

同理，这里报告的每一条都必须是 `SourceSnapshot` 真的装得下、或者用户真的能动手
改的东西。GeoDownloader 的 README 承诺 per-source 的 Referer / API-key 配置，
而它的数据模型（`settings.rs:9-22`）只有 `{id, name, url, subdomains, max_zoom}`
—— 文档写了实现没有，用户照着配一场空。不重复那个坑。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from src.contracts.source import SourceSnapshot
from src.services.geo_validation import MAX_ZOOM, MIN_ZOOM
from src.services.source_registry import (
    DEFAULT_STYLE_CODE,
    STYLE_CODES,
    STYLE_NAMES,
    style_code_for,
)
from src.services.tile_url_probe import (
    is_link_local_host,
    should_bypass_proxy,
    validate_server_entry,
)

logger = logging.getLogger(__name__)

__all__ = ['TemplateDetectionError', 'analyze_tile_url', 'snapshot_from_wizard']


class TemplateDetectionError(ValueError):
    """URL 里认不出 (z, x, y)。

    继承 ValueError 是接口约定：路由层把 ValueError 映射成 HTTP 400，
    这是用户输入错误（粘了一个不是瓦片的地址），不是服务端故障。
    """


# 缩放上限直接用 geo_validation 的 MIN_ZOOM/MAX_ZOOM（0..21），不另立一份：
# 这个上限同时是穷举的剪枝条件 —— 没有它，任意一个大整数都能当 z，x/y 的
# 范围约束（< 2**z）就完全失去筛选力。两处不一致的后果是向导认出一个层级，
# 下载器却拒绝它，用户看到的是「明明检测成功却建不了任务」。

# 整数槽位的最大位数。z/x/y 在 z<=21 时最多 7 位；放宽到 12 位是为了让
# 时间戳（10 位）之类的整数也进候选集**并被范围约束正常淘汰**，从而能出现在
# 报错信息里告诉用户「我看到了这些整数但都不成立」。再长的就是哈希/ID，
# 连报都不用报。
_MAX_INT_DIGITS = 12

# 输入长度上限。本函数跑在同步 Flask 处理器里且**不可取消**，而它的入参正是
# §13-5 里那个现实攻击者：从网页上随手复制、粘进向导输入框的一段文本。
# 真实瓦片 URL（含签名参数的 WMTS/腾讯/高德地址）都在 1 KB 以内，2 KB 已经
# 是宽松两倍。超了直接报错而不是截断：截断会拿一条**半截 URL** 去检测，
# 生成的模板逐字节看着像那么回事、却指向一个不存在的地址，用户要等整个任务
# 跑完全是 404 才发现。
_MAX_URL_CHARS = 2048

# 落进「穷举」分支的槽位数上限。指派搜索的最坏情形是三重循环（O(n^3)）：
# 实测 81 个整数槽 0.34 s、150 个 2.71 s、200 个 6.31 s、300 个（一条 2 KB 的
# URL）21.45 s —— 一次粘贴就能把一个 worker 线程占死几十秒，而且没有取消点。
# 结构化的三类候选（命名参数三元组、连续路径段 z/x/y 与 z/y/x）都是 O(n) 扫描
# 且排序上**一定**优于穷举分支（见 `_rank` 的 category），所以只要它们命中就
# 直接收敛，根本不进三重循环；只有全部落空时才退到穷举，此时再用这个上限封顶。
# 48^3 ≈ 11 万次，几十毫秒量级。
_MAX_BRUTE_SLOTS = 48

# 穷举分支之外，命名参数三元组搜索的迭代预算。正常 URL 里每个角色最多一两个
# 命名参数，这个预算永远用不到；它防的是 `?z=1&z=2&...&x=..&y=..` 这种把同名
# 参数刷上百遍的构造 —— 那同样是三重循环。
_NAMED_SEARCH_BUDGET = 50_000

# 报错信息里最多回显几个整数。检测失败的那条消息要把「我看到了哪些整数」摆给
# 用户看，但它会被路由原样回给浏览器、也会进日志 —— 几百个整数全糊进去，
# 消息本身就成了一条几 KB 的日志行，而用户看前十个就够判断了。
_MAX_LISTED_SLOTS = 12

# 拆 URL 但**不重组**。模板要求「除三个槽位外逐字节一致」，任何
# urlunsplit/urlencode 的往返都可能改写编码（`%2F`、`+` 与空格、空 query 的
# `?`），所以全程按字符区间在原串上做替换，这个正则只负责给出区间边界。
_URL_RE = re.compile(
    r'^(?P<prefix>[A-Za-z][A-Za-z0-9+.\-]*://[^/?#]*)?'
    r'(?P<path>[^?#]*)'
    r'(?P<query>\?[^#]*)?'
    r'(?P<frag>#.*)?$',
    re.DOTALL,
)

# 路径段里的整数：整段是数字（`/12/`），或数字后跟一个扩展名（`1655.png`）。
# 扩展名必须以字母开头，`1.5` 这种小数才不会被当成 `1` + 后缀。
_PATH_INT_RE = re.compile(r'^([0-9]+)(\.[A-Za-z][A-Za-z0-9]*)?$')
_PURE_INT_RE = re.compile(r'^[0-9]+$')

# 命名参数。WMTS 用 tilematrix/tilecol/tilerow，TMS/XYZ 风格的查询串用 x/y/z，
# 一些国产服务用 level/col/row。命中名字比位置可靠得多 —— 位置只能靠
# 数值范围反推，而 `x=12&y=9` 与 `x=9&y=12` 在范围上是对称的，猜不出来。
_Z_NAMES = frozenset(('z', 'zoom', 'level', 'tilematrix'))
_X_NAMES = frozenset(('x', 'tilecol', 'col'))
_Y_NAMES = frozenset(('y', 'tilerow', 'row'))
_ROLE_NAMES = {'z': _Z_NAMES, 'x': _X_NAMES, 'y': _Y_NAMES}

# 凭据参数名。命中就必须警告：这一段会**原样**留在 url_template 里。
_CREDENTIAL_RE = re.compile(
    r'^(key|apikey|api_key|token|access_token|secret|sig|signature)$', re.I)

# style 只接受 source_registry 认识的名字或码。**不**直接调 style_code_for：
# 它对未知值静默回退 'm'（为的是一行脏数据不打爆历史列表），而这里是用户
# 输入的入口，静默回退等于把 satelite 这种拼写错误变成一个悄悄下错图层的
# 任务。所以先用这两张表判成员资格，再用它做归一。

# 可从字符串逗号展开的列表字段。
_LIST_FIELDS = ('server_list', 'subdomains', 'header_names')


class _Slot(NamedTuple):
    """URL 里一个「整数值」的位置。

    start/end 是**原始字符串**的字符区间，替换成占位符时按它切；
    kind/order 决定排序（同类里的先后），name 是查询参数名（路径段为空串）。
    """

    kind: str          # 'path' | 'query'
    order: int         # path: 段下标；query: 参数下标
    name: str
    value: int
    start: int
    end: int


def _collect_slots(url: str) -> Tuple[List[_Slot], List[str], Dict[str, str]]:
    """扫出所有整数槽位，顺带返回路径段与查询参数表。

    返回 (slots, path_segments, query_params)。query_params 保持原始顺序，
    同名参数后者覆盖前者（只用于报告与凭据检查，模板替换走 slot 的字符区间，
    不受这个覆盖影响）。
    """
    m = _URL_RE.match(url)
    if not m:
        # 正则各段都可为空，理论上只有含裸 NUL 之类的畸形串到得了这里。
        raise TemplateDetectionError(f'无法解析的 URL: {url!r}')

    slots: List[_Slot] = []
    segments: List[str] = []
    params: Dict[str, str] = {}

    path = m.group('path') or ''
    pos = m.start('path')
    for index, seg in enumerate(path.split('/')):
        segments.append(seg)
        hit = _PATH_INT_RE.match(seg)
        if hit and len(hit.group(1)) <= _MAX_INT_DIGITS:
            digits = hit.group(1)
            slots.append(_Slot('path', index, '', int(digits),
                               pos, pos + len(digits)))
        pos += len(seg) + 1        # +1 = 被 split 吃掉的 '/'

    query = m.group('query') or ''
    if query:
        pos = m.start('query') + 1  # 跳过 '?'
        for index, pair in enumerate(query[1:].split('&')):
            name, sep, value = pair.partition('=')
            if sep:
                params[name] = value
                if _PURE_INT_RE.match(value) and len(value) <= _MAX_INT_DIGITS:
                    vstart = pos + len(name) + 1
                    slots.append(_Slot('query', index, name, int(value),
                                       vstart, vstart + len(value)))
            elif pair:
                params[pair] = ''
            pos += len(pair) + 1    # +1 = 被 split 吃掉的 '&'

    return slots, segments, params


def _assignment_ok(z: int, x: int, y: int) -> bool:
    """(z, x, y) 是否落在 Web 墨卡托格网内。

    这是整个检测的唯一硬约束：z 层有 2**z × 2**z 块瓦片，超界的组合在任何
    XYZ/TMS 服务上都取不到。它同时否掉绝大多数误判 —— 一个 10 位时间戳
    要成立需要 z >= 34，而 z 被 MAX_ZOOM 卡死。
    """
    if not MIN_ZOOM <= z <= MAX_ZOOM:
        return False
    n = 1 << z
    return 0 <= x < n and 0 <= y < n


def _consecutive(a: _Slot, b: _Slot, c: _Slot) -> bool:
    """三个槽位是否是**连续**的路径段（按给定顺序）。"""
    return (a.kind == b.kind == c.kind == 'path'
            and b.order == a.order + 1 and c.order == b.order + 1)


def _rank(z: _Slot, x: _Slot, y: _Slot) -> Tuple[int, int, int, int, int]:
    """候选指派的排序键，越小越优先。**全序，无随机**。

    1. 三个槽位都是命名查询参数（zoom/tilecol/tilerow …）—— 名字是服务方自己
       写的语义，比任何位置推断都可信；
    2. 连续路径段且顺序为 z, x, y —— XYZ 的事实标准；
    3. 连续路径段且顺序为 z, y, x —— ArcGIS/部分 WMTS REST 的排法；
    4. 其余。

    同档内先比命中的命名参数个数（部分命名也是证据），再比槽位在原串里的
    出现位置 —— 位置是原始输入的固有属性，同一个 URL 永远排出同一个结果。
    """
    named = sum(1 for role, slot in (('z', z), ('x', x), ('y', y))
                if slot.kind == 'query' and slot.name.lower() in _ROLE_NAMES[role])
    if named == 3:
        category = 1
    elif _consecutive(z, x, y):
        category = 2
    elif _consecutive(z, y, x):
        category = 3
    else:
        category = 4
    return (category, -named, z.start, x.start, y.start)


def _apply_template(url: str, chosen: Dict[str, _Slot]) -> str:
    """把三个槽位换成占位符，其余逐字节保留。

    倒序替换：先改后面的区间，前面的 start/end 才不会失效。
    """
    for placeholder, slot in sorted(
            (('{%s}' % role, slot) for role, slot in chosen.items()),
            key=lambda item: item[1].start, reverse=True):
        url = url[:slot.start] + placeholder + url[slot.end:]
    return url


def _subdomain_candidates(host: str) -> List[str]:
    """首标签是单个字母/数字 → 大概率是轮换子域。

    `a.tile.example` / `1.base.example` 是 OSM 系与国内瓦片服务的通用写法。
    只报候选，**不改模板**，理由见模块头。判据要求至少两级标签：裸主机 `a`
    不是子域轮换，是一个内网机器名。
    """
    labels = (host or '').split('.')
    if len(labels) < 2 or len(labels[0]) != 1:
        return []
    first = labels[0]
    if first.isalpha():
        return ['a', 'b', 'c']
    if first.isdigit():
        return ['1', '2', '3']
    return []


def _infer_scheme(segments: Sequence[str]) -> str:
    """判定 xyz / tms。**只认路径里字面的 `tms` 段。**

    数值上区分不了，这一点要写死在这里免得有人再试一次：TMS 的行号自南向北，
    XYZ 自北向南，互为 `y' = 2**z - 1 - y`。这个映射是 [0, 2**z) 到自身的
    **双射** —— 一个合法的 XYZ 行号翻转后仍然合法，反之亦然。所以「只有按
    翻转解释才落在范围内」这种情形根本不存在，任何声称靠数值认出 TMS 的判据
    都是恒假分支。真正的信号只有两个：地址里字面写着 `/tms/`，或者用户自己
    知道。判错的代价不是报错而是**上下颠倒的成品**（写 MBTiles 时 tile_row
    翻不翻正是按它分支的）。
    """
    return 'tms' if any(seg.lower() == 'tms' for seg in segments) else 'xyz'


def _tms_hint(segments: Sequence[str]) -> str:
    """路径里像 TMS 但又不是字面 `tms` 段的痕迹，没有则返回空串。

    存在的理由是**别让警告变成噪音**：既然数值区分不了，对每一条 XYZ 地址都
    喊一句「可能是 tms」等于没喊 —— 用户会学会忽略全部警告。只有出现真实痕迹
    时才提醒：`1.0.0` 是 TMS 1.0.0 规范的资源路径固定段（GeoServer/GWC 的
    tms 端点就长这样），`tms-cache`、`tmsservice` 这类段名同理。
    """
    for seg in segments:
        low = seg.lower()
        if low == '1.0.0':
            return seg
        if 'tms' in low and low != 'tms':
            return seg
    return ''


def _detection_failure(slots: Sequence[_Slot], url: str) -> TemplateDetectionError:
    """凑一条说得清「为什么认不出来」的错误。

    只说「检测失败」没用 —— 用户不知道是该换个带坐标的 URL，还是自己手写模板。
    把看到的整数和淘汰理由都摆出来。
    """
    if not slots:
        return TemplateDetectionError(
            f'这个地址里没有任何整数槽位，认不出 z/x/y：{url} —— '
            f'请粘一条**真实的瓦片**地址（地图页面里右键某张瓦片「复制图片地址」），'
            f'而不是首页/示例图片地址；或者直接手写 {{z}}/{{x}}/{{y}} 模板。')
    # 只列前 _MAX_LISTED_SLOTS 个：整数多到要靠省略号的地址本来就不是瓦片地址，
    # 把几百个数字全糊进报错框既没人看，又把这条消息推成日志里的一整段。
    shown = ', '.join(
        f'{s.name}={s.value}' if s.name else str(s.value)
        for s in slots[:_MAX_LISTED_SLOTS])
    found = shown if len(slots) <= _MAX_LISTED_SLOTS else f'{shown}, …（共 {len(slots)} 个）'
    return TemplateDetectionError(
        f'认不出 z/x/y：{url} —— 找到的整数是 [{found}]，但它们的任何一种 '
        f'(z, x, y) 指派都不成立（要求 {MIN_ZOOM} <= z <= {MAX_ZOOM} 且 x, y < 2**z）。'
        f'常见原因：这是一条 z 很小的示例瓦片被别的大整数（时间戳/版本号）干扰，'
        f'或者服务用的是 BBOX（WMS）而不是瓦片编号。请手写 {{z}}/{{x}}/{{y}} 模板。')


def _named_triple(slots: Sequence[_Slot]) -> Optional[Dict[str, _Slot]]:
    """`_rank` 的第 1 档：三个槽位都是**命名查询参数**。命中即最优。

    只在三个角色各自的命名参数里选，所以候选集通常是 1×1×1。按 start 升序
    三重遍历、第一个成立的就返回：`_rank` 在本档内的次序键正是
    `(z.start, x.start, y.start)` 的字典序（named 恒为 3，无需再比），
    所以「先找到的」就是「排序最优的」，不必枚举完再取最小值。
    """
    def named(role: str) -> List[_Slot]:
        allowed = _ROLE_NAMES[role]
        return sorted((s for s in slots
                       if s.kind == 'query' and s.name.lower() in allowed),
                      key=lambda s: s.start)

    zs, xs, ys = named('z'), named('x'), named('y')
    if not (zs and xs and ys):
        return None
    budget = _NAMED_SEARCH_BUDGET
    for z in zs:
        for x in xs:
            for y in ys:
                budget -= 1
                if budget < 0:
                    # 预算耗尽只可能是刻意刷出来的同名参数海。放弃这一档，
                    # 让后面的结构化扫描/穷举分支去处理（多半是报错），
                    # 而不是在这里继续转。
                    return None
                if _assignment_ok(z.value, x.value, y.value):
                    return {'z': z, 'x': x, 'y': y}
    return None


def _consecutive_triple(slots: Sequence[_Slot]) -> Optional[Dict[str, _Slot]]:
    """`_rank` 的第 2/3 档：连续三个路径段，先试 z/x/y 再试 z/y/x。

    路径槽位的 `order` 是段下标，一段最多产生一个槽位，所以「连续三元组」只有
    O(n) 个 —— 按起始段升序扫一遍即可。三个槽位都是 path，`_rank` 里的
    `named` 恒为 0，本档内的次序键退化成 `z.start`，即起始段下标，
    所以第一个成立的同样就是最优的。

    两档必须**分两轮**扫完：第 2 档（z/x/y，XYZ 事实标准）整体优于第 3 档
    （z/y/x，ArcGIS REST 排法），不能在同一轮里谁先命中算谁。
    """
    by_order = {s.order: s for s in slots if s.kind == 'path'}
    starts = sorted(by_order)
    triples = [(by_order[o], by_order[o + 1], by_order[o + 2])
               for o in starts
               if o + 1 in by_order and o + 2 in by_order]
    for a, b, c in triples:
        if _assignment_ok(a.value, b.value, c.value):
            return {'z': a, 'x': b, 'y': c}
    for a, b, c in triples:
        if _assignment_ok(a.value, c.value, b.value):
            return {'z': a, 'x': c, 'y': b}
    return None


def _brute_force_triple(slots: Sequence[_Slot]) -> Optional[Dict[str, _Slot]]:
    """`_rank` 的第 4 档：任意三个不同槽位，取排序键最小的那组。

    这一档是 O(n^3) 且没有可利用的结构，所以**只有前三档全部落空时才会走到**，
    并且由调用方用 `_MAX_BRUTE_SLOTS` 把 n 封死。不能像前三档那样「第一个命中
    就返回」：本档的次序键里 `-named` 会变（0/1/2 个命名参数都可能落在这一档），
    先出现的不一定最优，必须比完。
    """
    best: Optional[Tuple[Tuple[int, int, int, int, int], Dict[str, _Slot]]] = None
    total = len(slots)
    for i in range(total):
        for j in range(total):
            if j == i:
                continue
            for k in range(total):
                if k == i or k == j:
                    continue
                z, x, y = slots[i], slots[j], slots[k]
                if not _assignment_ok(z.value, x.value, y.value):
                    continue
                key = _rank(z, x, y)
                if best is None or key < best[0]:
                    best = (key, {'z': z, 'x': x, 'y': y})
    return None if best is None else best[1]


def _detect_slots(slots: Sequence[_Slot], url: str) -> Dict[str, _Slot]:
    """选出 (z, x, y) 三个槽位，选不出就抛 `TemplateDetectionError`。

    分档求解而不是一把穷举，理由是**可终止性**：`_rank` 的第一个分量是档位，
    所以第 1~3 档一旦命中，第 4 档里再好的组合也排在它后面 —— 结果已经确定，
    继续枚举三元组纯属白烧 CPU。前三档都是 O(n) 扫描，正常瓦片地址在这里就
    收敛了，三重循环根本不会执行。
    """
    for finder in (_named_triple, _consecutive_triple):
        chosen = finder(slots)
        if chosen is not None:
            return chosen
    if len(slots) > _MAX_BRUTE_SLOTS:
        # 走到这里说明没有任何结构化线索，只剩下 O(n^3) 的盲猜，而 n 已经大到
        # 会把请求线程占死几十秒。这种输入也不可能猜对：一条真实瓦片地址的整数
        # 不会有几十个。明确报错，不截断、不硬算。
        raise TemplateDetectionError(
            f'这个地址里有 {len(slots)} 个整数（上限 {_MAX_BRUTE_SLOTS}），'
            f'而其中没有任何一组能按命名参数或连续路径段认出 z/x/y —— '
            f'剩下的只能靠穷举组合，代价随整数个数的三次方增长，不予尝试。'
            f'请粘一条**真实的瓦片**地址，或者直接手写 {{z}}/{{x}}/{{y}} 模板。')
    chosen = _brute_force_triple(slots)
    if chosen is None:
        raise _detection_failure(slots, url)
    return chosen


def analyze_tile_url(url: str) -> Dict[str, Any]:
    """一条真实瓦片 URL → 模板 + 检测结果 + 警告。

    返回 dict：
      template      —— 三个槽位换成 {z}/{x}/{y} 的模板，其余逐字节不变；
      detected      —— {'z': int, 'x': int, 'y': int}，即原 URL 里那张瓦片；
      subdomains    —— 子域轮换候选（**未**写进模板，见模块头）；
      scheme        —— 'xyz' | 'tms'；
      query_params  —— 原始查询参数（同名后者覆盖，仅供展示与凭据检查）；
      warnings      —— 给用户看的字符串列表，可能为空。

    认不出 (z, x, y) 时抛 TemplateDetectionError，不返回半成品。
    """
    text = (url or '').strip()
    if not text:
        raise TemplateDetectionError('URL 不能为空')
    if len(text) > _MAX_URL_CHARS:
        raise TemplateDetectionError(
            f'URL 太长：{len(text)} 字符，上限 {_MAX_URL_CHARS}。'
            f'真实瓦片地址（含签名参数的也算）都在 1 KB 以内 —— 这么长的一段'
            f'多半不是一条瓦片 URL，而是整页复制下来的文本。'
            f'请只粘瓦片地址本身。')

    slots, segments, params = _collect_slots(text)
    chosen = _detect_slots(slots, text)
    template = _apply_template(text, chosen)
    detected = {role: slot.value for role, slot in chosen.items()}

    warnings: List[str] = []

    host = (urlsplit(text).hostname or '').lower()
    subdomains = _subdomain_candidates(host)
    if subdomains:
        warnings.append(
            f'主机首段 "{host.split(".")[0]}" 看起来是轮换子域，候选 '
            f'{"/".join(subdomains)}；模板**没有**改成 {{s}} —— 下载引擎只替换 '
            f'{{z}}/{{x}}/{{y}}（download_engine.py:494-497），'
            f'tile_url_probe 的占位符白名单也直接拒绝 {{s}}，'
            f'生成带 {{s}} 的模板等于生成一条过不了校验的配置。'
            f'要轮换请把这几个主机分别填进服务器列表。')

    # x/y 顺序歧义。这是本模块**最重要**的一条警告，所以它不挂任何前提条件：
    # `_assignment_ok` 对 x 与 y 是对称的（两者都只要求 < 2**z），所以只要
    # 两个值不相等，把它们对调一定同样成立 —— 排序只能按「z,x,y 连续」这个
    # 事实标准优先，不可能真的判出来。判错的代价最恶劣：转置的模板照样过
    # validate_server_entry、照样返回 200 的真瓦片，只是内容对不上位置，
    # 要等整张图下完拼出来才看得出错位。本仓库自己就有一个 z/y/x 的例子：
    # `basemap_source.BASEMAP_PRESETS['esri']` 的 ArcGIS REST 地址是
    # `/tile/{z}/{y}/{x}`，而它既没有 tms 也没有 1.0.0 这种痕迹。
    # 唯一能免掉这条的情形是参数名自己说了话（tilecol/tilerow 之类）。
    x_named = chosen['x'].name.lower() in _X_NAMES
    y_named = chosen['y'].name.lower() in _Y_NAMES
    if chosen['x'].value != chosen['y'].value and not (x_named and y_named):
        swapped = _apply_template(
            text, {'z': chosen['z'], 'x': chosen['y'], 'y': chosen['x']})
        warnings.append(
            f'x/y 顺序是**按惯例猜的**，数值上分不出来：{detected["x"]} 与 '
            f'{detected["y"]} 互换后同样落在 z{detected["z"]} 的格网里。'
            f'这里按 XYZ 事实标准取 z/x/y；若该服务是 z/y/x（ArcGIS REST 就是，'
            f'见 basemap_source 的 esri 预设），正确的模板是：{swapped}。'
            f'猜错**不会**报错：转置的模板一样过校验、一样返回 200 的真瓦片，'
            f'只是内容与位置对不上，要等整张图拼完才看得出来。'
            f'验证办法：拿模板换一个已知位置的瓦片比对一下再建任务。')

    scheme = _infer_scheme(segments)
    hint = _tms_hint(segments) if scheme == 'xyz' else ''
    if hint:
        warnings.append(
            f'路径里有 "{hint}" 段，像 TMS 端点，但没有字面的 /tms/ 段，'
            f'所以 tile_scheme 仍按 xyz 给出 —— 数值上区分不了：xyz 与 tms 的'
            f'行号取值域完全相同（互为 2**z-1-y 的双射）。请自行确认；'
            f'判错不会报错，只会让成品南北颠倒，而这要等整张图拼完才看得出来。')

    for name in params:
        if _CREDENTIAL_RE.match(name):
            warnings.append(
                f'查询参数 "{name}" 像是凭据：它会**原样**留在 url_template 里，'
                f'因而进 tasks 表、进缓存命名空间的计算输入、进诊断包。'
                f'本工具不支持把它拆成单独的密钥字段（SourceSnapshot 只存凭据的'
                f'**引用**，不存值），要么接受这一点，要么换一个不带密钥的地址。')

    if is_link_local_host(host):
        warnings.append(
            f'主机 {host} 落在链路本地段（169.254/16、fe80::/10）—— '
            f'那里住的是云厂商实例元数据端点，不是瓦片服务。这条地址会被拒绝。')
    elif should_bypass_proxy(text):
        warnings.append(
            f'主机 {host} 是本机/内网地址，下载时**不走**配置的代理（直连）。'
            f'自建镜像这样是对的；如果你以为它在走代理，那就是配错了。')

    ok, err = validate_server_entry(template)
    if not ok:
        warnings.append(f'生成的模板过不了服务器条目校验：{err} —— '
                        f'直接填进配置页会被拒。')

    logger.info('source wizard: %s -> %s (scheme=%s, detected=%s, warnings=%d)',
                text, template, scheme, detected, len(warnings))

    return {
        'template': template,
        'detected': detected,
        'subdomains': subdomains,
        'scheme': scheme,
        'query_params': params,
        'warnings': warnings,
    }


def _coerce_list(value: Any, field: str) -> Tuple[str, ...]:
    """列表字段：接受 list/tuple，也接受逗号分隔字符串。空 → 空元组。

    表单只能提交字符串，JSON 客户端习惯提交数组，两边都得收。

    这里**故意不用** `tile_url_probe.parse_server_list` 拆串，虽然它做的正是
    逗号拆分：它在拆完为空时回退到 mts0-3（Google）。那个回退对配置项是对的
    （下载器总得有服务器），对向导是错的 —— 用户把服务器列表留空的意思是
    「不轮换，就用模板里那个主机」，回退会让快照描述一个他从没配过的源，
    而且 fingerprint 会跟着变。`' , '` 这种全是分隔符的输入正好踩中它。
    """
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    elif isinstance(value, str):
        items = [s.strip() for s in value.split(',')]
    else:
        raise ValueError(f'{field} 必须是列表或逗号分隔的字符串，'
                         f'收到 {type(value).__name__}')
    return tuple(s for s in items if s)


def snapshot_from_wizard(payload: Dict[str, Any]) -> SourceSnapshot:
    """向导表单 → SourceSnapshot。

    必填 source_id、url_template（必须仍含 {z}/{x}/{y} —— 少一个就是一个
    下载时才会暴露的死模板）。其余给默认值：style='m'、tile_scheme='xyz'。

    列表字段（server_list / subdomains / header_names）接受列表或逗号串。
    header 只收**名字**、credential_reference 只收**配置键名**，这两条由
    `SourceSnapshot.__post_init__` 自己把关，这里不重复实现 —— 它的报错信息
    已经说清了为什么（值会进 fingerprint 和日志）。

    快照里不放任何时间/顺序相关的东西，所以同样的输入调多少次，
    fingerprint 都一样 —— 这是缓存命名空间稳定的前提。
    """
    if not isinstance(payload, dict):
        raise ValueError(f'向导载荷必须是对象，收到 {type(payload).__name__}')

    source_id = str(payload.get('source_id') or '').strip()
    if not source_id:
        raise ValueError('source_id 不能为空：它是这个源在任务列表与日志里的名字')

    template = str(payload.get('url_template') or '').strip()
    if not template:
        raise ValueError('url_template 不能为空')
    missing = [p for p in ('{z}', '{x}', '{y}') if p not in template]
    if missing:
        raise ValueError(
            f'url_template 缺占位符 {" ".join(missing)}：{template} —— '
            f'缺了的那一维会在每次请求里被写死成同一个值，'
            f'表现为整张图都是同一块瓦片或全部 404。'
            f'用 analyze_tile_url 从一条真实瓦片地址生成模板。')

    raw_style = str(payload.get('style') or DEFAULT_STYLE_CODE).strip()
    if raw_style not in STYLE_CODES and raw_style not in STYLE_NAMES:
        known = '/'.join(sorted(STYLE_NAMES)) + ' 或 ' + '/'.join(sorted(STYLE_CODES))
        raise ValueError(
            f'style {raw_style!r} 不认识，只能是 {known} —— 它会归一成单字符码，'
            f'直接当缓存目录名的前缀（cache/{{码}}-{{fingerprint}}/）。'
            f'这里不做静默回退：拼错一个字母就悄悄换成路网图，'
            f'要等下完看图才发现下错了图层。')
    style = style_code_for(raw_style)

    scheme = str(payload.get('tile_scheme') or 'xyz').strip().lower()

    lists = {field: _coerce_list(payload.get(field), field)
             for field in _LIST_FIELDS}

    for entry in lists['server_list']:
        ok, err = validate_server_entry(entry)
        if not ok:
            raise ValueError(f'服务器条目 {entry!r} 非法：{err}')

    snapshot = SourceSnapshot(
        source_id=source_id,
        url_template=template,
        server_list=lists['server_list'],
        style=style,
        tile_scheme=scheme,
        subdomains=lists['subdomains'],
        header_names=lists['header_names'],
        credential_reference=str(payload.get('credential_reference') or '').strip(),
        attribution=str(payload.get('attribution') or '').strip(),
        usage_policy=str(payload.get('usage_policy') or '').strip(),
    )
    logger.info('source wizard: snapshot %s', snapshot.summary())
    return snapshot
