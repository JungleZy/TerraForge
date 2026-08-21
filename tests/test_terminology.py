"""一个概念一个名字：术语归一 + 标签与行为对齐的契约。

2026-08-14 的设计审查（`docs/reviews/2026-08-14-frontend-design-audit.md` §D）在
文案层抓到三类缺陷，它们的共同点是**没有任何机制覆盖**：`tests/test_i18n.py`
的五道闸门全是结构级的（两语种齐全、占位符对等、en 不含汉字、键↔引用双向
闭合），它们对「同一个东西在四个地方叫四个名字」一无所知。本文件补的就是
判断层。

三类：
1. **同语言内术语冲突 12 组** —— 路网样式的中文有四个名字、缺口与缺块同现
   一句、瓦片量词「块」与「张」同现一句、任务与作业混用……
2. **标签与行为不符 6 处** —— 按钮说「下载」实际打开表单、说「删除」实际清
   选区、「测速推荐」跑完 30 秒只把数字填进输入框、「重置为默认值」立刻 POST
   落库并刷新页面。
3. **EN 面缺陷 15 处** —— `/history` 四张统计卡在英文界面上渲染成
   `Total tasks / Completed / Failed / Total`（指标名丢了，且与另外两处 `Total`
   撞名）、一条管线有四个英文名、`js.gaps.outcome.*` 是小写句子片段当标签用。

## 判据为什么是「包含典范词」而不是「等于某个字符串」

同一个概念在不同位置需要不同的修饰（`Google 路网` / `OpenStreetMap 路网` /
`路网`），钉全等会逼出一堆例外表。钉**典范词出现**加上**变体词不出现**两侧，
既允许修饰又能抓住换词：把「路网」写成「路线图」时前一侧红，把「路网」和
「道路图」混在一起时后一侧红。

## 为什么这些改动全是改值、没有一个改键

`src/i18n/catalog/__init__.py:52-58` 的两条不变式在 **import 期**抛异常（键全局
唯一、两语种齐全），所以一个键名写错会让整个测试套件**收集失败**，而不是红
一条。改值只触发 `tests/test_i18n.py` 的占位符对等（`:40-47`）与 en 不含汉字
（`:50-60`）两道；改键会额外触发双向闭合（`:280-326`），必须同步 `templates/`、
`static/js/`、`src/` 里所有键字面量。本文件覆盖的 100 余处全部是改值。

## 值级断言不止 test_i18n.py 一处

改文案前必须读完这七处，它们钉的是**具体的词**：
- `tests/test_tasks_js_contract.py:1124-1133` 每个状态的 zh 必须含特定词，其中
  `completed_with_gaps` 必须含**「缺块」** —— 所以「缺块」是典范侧，把「缺口」
  改过去，不是反向。
- `tests/test_tasks_js_contract.py:2001-2013` 删除确认的关键词表（正在运行 /
  补漏 / 排队 / 已暂停 / 等你决定）。
- `tests/test_tasks_js_contract.py:2306` `maxzoom_auto_hint` 的 zh 不许含「填」。
- `tests/test_tasks_js_contract.py:2382-2392` 三个档位的 zh 不许是枚举字面量本身。
- `tests/test_terrain_lighting_frontend.py:503-517` 档位文案必须含「基准层级」/
  `base level`，说明必须含 0/21 钳位那句。
- `tests/test_i18n.py:466-477` 档位说明不许把基准层级钉死成上面填的那个数。
- `tests/test_cache_management.py:74` 缓存分类标签的全等断言 —— 本次把
  `api.cache.category.dem` 从「DEM 缓存」改成「高程缓存」，那一行同步改了。
  计划只点名了前六处，这是第七处。

## 与并行任务的边界

选区那一格与工具条上那些按钮的文案，本文件只**验收**它们：可见文案不许承诺它
不做的动作（下面那条「不许说下载」按区域扫 markup，不是抄一份键名清单），清除钮
的可见文案与 `title` 必须同一个动词。`js.plugins.*` / `tpl.plugins.*` 属插件面，
不在本文件的任何键表里（另一条工作线在改）。
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.i18n.catalog import MESSAGES  # noqa: E402

# 复用既有的「按 div 深度切一段 markup」与「按花括号切函数体 / 剥 JS 注释」，
# **不另写一份**（同 test_formatters.py 顶部那条理由）。本文件绝大多数断言只读
# catalog 的值，唯一要读源码的是下面那条「哪些键真的是按钮的可见文案」——
# 那个问题只有 markup 能回答，抄一份键名清单就退化成白名单，新按钮扫不到。
from test_create_panel import _div_span, _js, _template  # noqa: E402
from test_css_contract import _js_function_body, _strip_js_comments  # noqa: E402

_HAN = re.compile(r'[\u4e00-\u9fff]')


# ---------------------------------------------------------------------------
# 12 组概念的典范词表
#
# 每条记录：概念名、zh 典范词、en 典范词（大小写不敏感的子串）、该概念的**全部**
# 键、以及被逐出典范的变体词（zh / en 各一张）。变体表是这套断言的牙齿：只钉
# 「含典范词」的话，「路网图」与「道路图」并存时两条都绿。
# ---------------------------------------------------------------------------

class _Concept:
    __slots__ = ('name', 'zh', 'en', 'keys', 'zh_banned', 'en_banned')

    def __init__(self, name, zh, en, keys, zh_banned=(), en_banned=()):
        self.name = name
        self.zh = zh
        self.en = en
        self.keys = keys
        self.zh_banned = zh_banned
        self.en_banned = en_banned


_CONCEPTS = (
    # 路网样式（Google `lyrs=m`）。审查报告把它记成「zh 有 4 个名字（路线图/
    # 道路/道路图/路网图）」，但实测 `m` 与 `h` 是**两个图层**
    # （`src/services/download_engine.py:1712` 的注释按 Google 的口径列的是
    # roadmap / hybrid / roads / terrain 四种），报告把两者混成了一条。所以这里
    # 拆成两个概念：`m` 是完整底图，典范「路网」；`h` 是只有道路的叠加层，典范
    # 「道路」。两侧互斥 —— 「路网」不许出现在道路层的键里，反之亦然。
    _Concept(
        '路网样式（lyrs=m）', '路网', 'roadmap',
        (
            'js.history.style.roadmap',
            'js.history.style.m',
            'tpl.index.download.style_standard',
            'tpl.config.basic.style_standard',
            'js.map.basemap.src_google_roadmap',
            'js.map.basemap.src_osm',
            'tpl.config.download.basemap_google_map',
        ),
        zh_banned=('路线图', '道路', '标准'),
        # 'road map' 分写是 OpenStreetMap 那条独有的第八种写法。
        en_banned=('road map', 'standard'),
    ),
    # 只有道路的叠加层（Google `lyrs=h`）。三个键，两个已经是「道路」，把
    # 「道路图」收过来。
    _Concept(
        '道路叠加层（lyrs=h）', '道路', 'roads',
        (
            'tpl.index.download.style_roads',
            'tpl.config.basic.style_roads',
            'js.history.style.h',
        ),
        zh_banned=('路网', '道路图'),
        en_banned=('roadmap',),
    ),
    # 卫星样式。`y`（Google 的 hybrid）与「卫星+标注」是同一个图层的两个名字，
    # 所以 `js.history.style.hybrid` 也在这张表里：它当前叫「混合图」，与另外
    # 三处的「卫星+标注」指同一个东西。
    _Concept(
        '卫星影像样式', '卫星影像', 'satellite imagery',
        (
            'tpl.index.download.style_satellite',
            'tpl.index.download.style_satellite_labels',
            'tpl.config.basic.style_satellite',
            'tpl.config.basic.style_satellite_labels',
            'tpl.config.download.basemap_esri',
            'tpl.config.download.basemap_google_sat',
            'tpl.config.download.basemap_hint',
            'js.map.basemap.src_esri',
            'js.map.basemap.src_google_satellite',
            'js.history.style.satellite',
            'js.history.style.s',
            'js.history.style.y',
            'js.history.style.hybrid',
        ),
        zh_banned=('卫星图', '混合图'),
        # 'World Imagery' 是 Esri 的产品名，但同一个源在 js_map.py 里叫
        # 'Esri satellite imagery' —— 一个源两个英文名，用户对不上。
        en_banned=('world imagery', 'hybrid'),
    ),
    _Concept(
        '地形样式', '地形', 'terrain',
        (
            'tpl.index.download.style_terrain',
            'tpl.config.basic.style_terrain',
            'js.history.style.terrain',
            'js.history.style.t',
            'tpl.base.detail.terrain_label',
        ),
        zh_banned=('地形图', '地势', '地貌'),
    ),
    # 缺块。典范侧由 tests/test_tasks_js_contract.py:1131 锁死（那条要求
    # `completed_with_gaps` 的 zh 含「缺块」），所以是「缺口」→「缺块」。
    _Concept(
        '缺块', '缺块', 'gap',
        (
            'tpl.base.detail.gaps_label',
            'tpl.history.filter.gaps',
            'tpl.history.filter.gaps_title',
            'js.tasks.status.completed_with_gaps',
            'js.history.confirm.delete_task_pending_decision',
            'js.gaps.chip_title',
            'js.gaps.loading',
            'js.gaps.none',
            'js.gaps.explained',
            'js.gaps.unexplained',
            'js.gaps.load_failed',
            'js.gaps.action.accept',
            'js.gaps.action.accept_title',
            'js.gaps.action.retry_title',
            'js.gaps.confirm_accept',
            'js.gaps.toast.accept_failed',
            'js.gaps.event.pending_decision',
            'js.artifacts.gapped',
            'js.artifacts.gapped_title',
            'api.gaps.not_found',
        ),
        zh_banned=('缺口',),
    ),
    # 任务。「作业」只剩两处（两条 maxzoom 说明），en 面的 `job` 有四处。
    # 全局禁令见 test_no_catalog_value_calls_a_task_a_job。
    _Concept(
        '任务', '任务', 'task',
        (
            'js.history.terrain.maxzoom_base_hint',
            'js.history.terrain.maxzoom_auto_hint',
            'js.map.tile_estimate.over',
            'js.map.download.confirm_large_title',
        ),
        zh_banned=('作业',),
        en_banned=('job',),
    ),
    # 缩放级别。「最大级别留空…也可手动填更高层级」一句里就有两种叫法。
    # en 侧钉 'level' 而不是 'zoom level'：`js.history.terrain.maxzoom_base_label`
    # 的 'Base level' 被 tests/test_terrain_lighting_frontend.py:506 按
    # **'base level' 连写**钉死，插一个 'zoom' 进去会把那条弄红。
    _Concept(
        '缩放级别', '层级', 'level',
        (
            'tpl.base.detail.zoom',
            # `tpl.index.form.section_range` 曾在这里，Task 5 的面板合并把它删了
            # （分区标题不再需要）。留个注脚，免得下一个人以为是漏了。
            'tpl.index.form.zoom_min',
            'tpl.index.form.zoom_max',
            'tpl.index.process.local_terrain_maxzoom',
            'tpl.index.process.zoom_hint',
            'js.map.tifinfo.recommended_maxzoom',
            'js.history.terrain.maxzoom_base_label',
        ),
        zh_banned=('级别',),
    ),
    # 瓦片量词。「预计 {count} 块瓦片 · 按 10 张/秒」—— 一句话里两个量词。
    # zh 侧的禁令是正则（见 _ZH_BANNED_TILE_MEASURE）：不能裸禁「块」，
    # 「缺块」是另一个概念的典范词，它合法地含这个字。
    _Concept(
        '瓦片量词', '张', 'tile',
        (
            'js.map.tile_estimate.count',
            'js.map.tile_estimate.over',
            'js.map.download.confirm_large',
            'js.gaps.chip_title',
            'js.gaps.confirm_accept',
            'js.gaps.toast.accepted',
            'js.gaps.toast.exported',
            'js.artifacts.tiles',
            'val.tile_url.recommend.note_rising',
            'val.tile_url.recommend.note_knee',
        ),
    ),
    # 产物。zh 有 成品 / 产出 / 输出 三个变体混入，en 面几乎全是 output，而
    # `js.artifacts.*` 一族用的是 artifact —— 同一个东西两个英文名。
    #
    # 不在表内且**刻意**不改的三组，理由各自不同：
    #   `*.output_format`（3 处）—— 「输出格式」说的是产物的格式，是另一个名词
    #       短语，且三处已经完全一致。
    #   `tpl.index.download.section_output` —— 它是「输出格式 + 保存路径」这
    #       两个字段的分组标题，不是产物本身。
    #   `*.output_path` —— 路径不是产物；这一组自己的冲突（详情面板叫「输出
    #       路径」、表单与校验都叫「保存路径」）由
    #       test_the_save_path_is_called_the_same_thing_everywhere 单独钉。
    _Concept(
        '产物', '产物', 'artifact',
        (
            'tpl.base.detail.artifacts_label',
            'tpl.history.filter.gaps_title',
            'js.history.confirm.delete_files_checkbox',
            'js.history.progress.delete_scanning',
            'js.history.progress.delete_removing',
            'js.history.toast.deleted_files_deferred',
            'js.history.terrain.normals_off_hint',
            'js.history.terrain.maxzoom_base_hint',
            'js.map.terrain.estimate_hint',
            'js.gaps.chip_title',
            'js.gaps.action.accept',
            'js.gaps.action.accept_title',
            'js.gaps.action.export',
            'js.gaps.confirm_accept',
            'js.gaps.toast.accepted',
            'js.export.confirm.message',
            'js.export.toast.nothing_to_export',
            'js.artifacts.loading',
            'js.artifacts.none',
            'js.artifacts.load_failed',
            'js.artifacts.gapped_title',
            'js.base_unpack.failed_title',
            'api.tasks.files_kept_unsafe_dir',
            'api.tasks.cache_clear_blocked',
        ),
        zh_banned=('成品',),
        en_banned=('output',),
    ),
    # 高程数据。审查判词是「DEM 只在数据集名里出现」—— 数据集名（ASTER GDEM
    # v3 / Copernicus GLO-30）是产品全称，改了就对不上供应商文档；除此之外
    # 一律「高程」/ elevation，包括磁盘上那些 .tif（「高程文件」）。
    # 允许 DEM 的键表由 test_dem_survives_only_in_dataset_names 锁住。
    _Concept(
        '高程数据', '高程', 'elevation',
        (
            'tpl.index.download.type_dem',
            'tpl.index.process.source_dem_task',
            'tpl.index.process.dem_task',
            'tpl.index.process.dem_task_hint',
            'tpl.index.process.upload_dem',
            'tpl.index.process.contour_source_hint',
            'tpl.index.process.zoom_hint',
            'tpl.index.process.terrain_shade_option',
            'tpl.index.process.tint_colors',
            'js.map.process.need_dem_task',
            'js.map.process.no_completed_dem_task',
            'js.map.process.dem_task_load_failed',
            'js.map.process.terrain_started_dem_task',
            'js.map.process.contour_started',
            'js.map.process.contour_started_dem_task',
            'js.map.preview.hillshade_fallback',
            'js.map.preview.dem_no_tiles',
            'js.map.tifinfo.elevation',
            'js.history.meta.dem',
            'js.tasks.verb.download_dem',
            'js.artifacts.kind.dem_dir',
            'js.config.cache.dem_warning',
            'js.drop.hint',
            'js.drop.unsupported',
            'api.tasks.pipeline.dem',
            'api.cache.category.dem',
        ),
        zh_banned=('DEM', '海拔'),
        en_banned=('DEM',),
    ),
    # 设置。保存钮说「配置」、API 错误说「设置」、六个分区标题说「设置」。
    # 中文典范是「配置」（配置页、配置表、配置键都是这个词），英文典范是
    # settings（页面标题、导航都已经是 Settings）。
    #
    # 不在表内：`tpl.config.download.proxy_auto_hint` 的「系统代理设置」说的是
    # **操作系统自己的**设置项，不是 TerraForge 的配置；`js.plugins.*` 与
    # `js.map.download.credential_missing` 指的是插件面板上那颗「配置」按钮
    # （插件面由另一条工作线维护）。
    _Concept(
        '设置', '配置', 'setting',
        (
            'tpl.config.page_title',
            'tpl.config.basic.title',
            'tpl.config.download.title',
            'tpl.config.cache.title',
            'tpl.config.gdal.title',
            'tpl.config.misc.title',
            'tpl.config.earthdata.title',
            'tpl.config.actions.save',
            'tpl.config.download.tile_servers_hint',
            'tpl.config.download.basemap_hint',
            'tpl.config.geocoder.url_hint',
            'tpl.index.toolbar.config',
            'tpl.index.panel.config',
            'js.config.save.ok',
            'js.config.reset.title',
            'js.config.reset.ok',
            'js.config.reset.confirm',
            'js.cmdk.open_config',
            'api.config.invalid_values_not_saved',
        ),
        zh_banned=('设置',),
        en_banned=('configuration', 'config panel', 'config page'),
    ),
    # 导出。审查记的是「现有三种语义」，判词是**按语义拆键而非统一**：
    #   ① 把已有产物重打包成另一种格式（`js.export.*` / `api.export.*` /
    #      `tpl.index.download.export_mbtiles*` / 缺块面板那颗导出钮）—— 真正的
    #      「导出」，这个词留给它；
    #   ② 「接受缺块并导出」的那个「导出」—— 实测 `acceptTaskGaps`
    #      （`static/js/task_center.js:991-1045`）POST `/accept_gaps`，跑的是严格
    #      模式拒绝过的拼接/复制阶段，**没有任何导出**。改成「生成产物」。
    #   ③ 同一句话在删除确认里的复述（`delete_task_pending_decision`）。
    # 于是这条概念有两侧：表内必须含「导出」，表**外**一个都不许含。
    _Concept(
        '导出', '导出', 'export',
        (
            'tpl.index.download.export_mbtiles',
            'tpl.index.download.export_mbtiles_hint',
            'js.export.confirm.title',
            'js.export.confirm.message',
            'js.export.confirm.format_label',
            'js.export.confirm.ok',
            'js.export.toast.exported',
            'js.export.toast.nothing_to_export',
            'js.gaps.action.export',
            'js.gaps.toast.exported',
            'js.gaps.toast.export_failed',
            'api.export.unsupported_format',
            'api.export.no_tiles',
            'api.export.unsupported_pipeline',
        ),
    ),
)

# 概念数锁死：删掉一整组概念（连同它的断言）是这套契约最容易被悄悄削弱的方式。
_CONCEPT_COUNT = 12

# 覆盖的键数（去重后，2026-08-15 实测）。缩表同样是削弱，所以连总数一起锁。
_COVERED_KEY_COUNT = 135


def test_the_canonical_word_table_still_covers_twelve_concepts():
    """概念数与覆盖键数都锁住 —— 删一组概念、或从某组里摘掉几个键，这里响亮地红。

    审查报告数出的是 12 组同语言术语冲突。报告把 `lyrs=m`（路网）与 `lyrs=h`
    （道路叠加层）混成了一条，本文件拆成两条；作为交换，「一控件一动词」那条
    不占概念位 —— 它钉的是同一颗控件的两个属性，由
    test_the_clear_selection_control_uses_one_verb_in_label_and_title 单独断言。
    """
    assert len(_CONCEPTS) == _CONCEPT_COUNT, (
        f'典范词表现在有 {len(_CONCEPTS)} 组概念，锁的是 {_CONCEPT_COUNT} 组：'
        f'{[c.name for c in _CONCEPTS]}'
    )
    covered = {key for concept in _CONCEPTS for key in concept.keys}
    assert len(covered) == _COVERED_KEY_COUNT, (
        f'典范词表覆盖 {len(covered)} 个键，锁的是 {_COVERED_KEY_COUNT} 个 —— '
        '缩表等于把冲突放回去'
    )
    # 每张键表自身不许有重复项（重复不会让断言变弱，但会让上面的计数骗人）。
    for concept in _CONCEPTS:
        assert len(set(concept.keys)) == len(concept.keys), (
            f'{concept.name} 的键表里有重复项：{concept.keys}')


@pytest.mark.parametrize('concept', _CONCEPTS, ids=[c.name for c in _CONCEPTS])
def test_one_concept_uses_one_word(concept):
    """一个概念一个名字：该概念的每个键，zh 含同一个典范词、en 含同一个典范词。

    失败信息里逐条列出键、当前值、以及是缺典范词还是撞了变体词 —— 修的人不用
    再去 grep 一遍 catalog。
    """
    problems = []
    for key in concept.keys:
        entry = MESSAGES.get(key)
        if entry is None:
            problems.append(f'{key}: catalog 里没有这个键 —— 本用例的键表已失效')
            continue
        zh, en = entry['zh'], entry['en']
        if concept.zh not in zh:
            problems.append(f'{key} [zh] 缺典范词「{concept.zh}」: {zh!r}')
        for bad in concept.zh_banned:
            if bad in zh:
                problems.append(
                    f'{key} [zh] 用的是变体词「{bad}」而不是「{concept.zh}」: {zh!r}')
        if concept.en not in en.lower():
            problems.append(f'{key} [en] 缺典范词 {concept.en!r}: {en!r}')
        for bad in concept.en_banned:
            if re.search(r'\b%s\b' % re.escape(bad), en, re.I):
                problems.append(
                    f'{key} [en] 用的是变体词 {bad!r} 而不是 {concept.en!r}: {en!r}')
    assert not problems, (
        f'「{concept.name}」这一个概念在界面上有多个名字 —— 用户看到的是两样'
        f'东西：\n  ' + '\n  '.join(problems)
    )


# ---------------------------------------------------------------------------
# 全局禁令：变体词在整本目录里归零
#
# 上面那批是「表内的键必须用典范词」，单向。往 catalog 里新加一个说「缺口」的
# 键，只要不写进键表，上面全绿。下面这几条从**整本目录**这一头再钉一次。
# ---------------------------------------------------------------------------

def _offenders(pattern, locale, allowed=()):
    rx = re.compile(pattern)
    return [
        f'{key} [{locale}]: {MESSAGES[key][locale]!r}'
        for key in sorted(MESSAGES)
        if key not in allowed and rx.search(MESSAGES[key][locale])
    ]


def test_the_whole_catalog_says_gap_one_way():
    """「缺口」在整本目录里归零 —— 它与「缺块」曾经同现一句（js_region.py）。"""
    bad = _offenders('缺口', 'zh')
    assert not bad, (
        '这些文案还在说「缺口」。典范是「缺块」（tests/test_tasks_js_contract.py:'
        '1131 把 completed_with_gaps 的 zh 锁在这个词上）：\n  ' + '\n  '.join(bad))


def test_the_whole_catalog_says_artifact_one_way():
    """「成品」在整本目录里归零 —— 产物/成品/产出 三个词曾指同一个东西。"""
    bad = _offenders('成品', 'zh')
    assert not bad, (
        '这些文案还在说「成品」。典范是「产物」（`js.artifacts.*` 一族与详情面板'
        '的分区标题都用它）：\n  ' + '\n  '.join(bad))


def test_no_catalog_value_calls_a_task_a_job():
    """「作业」/ `job` 在整本目录里归零。

    两条 maxzoom 说明里「作业切完后…」与同一句的「任务」并存；en 面有四处
    `job`（含「大任务确认」的 'Confirm large job'）。后端只有 task 一个词
    （`src/contracts/outcome.py`），界面上多出来的「作业」是凭空第二个概念。
    """
    bad = _offenders('作业', 'zh') + _offenders(r'\bjobs?\b', 'en')
    assert not bad, '这些文案把任务叫成了作业 / job：\n  ' + '\n  '.join(bad)


# 「块」当瓦片量词用的三种形态：紧跟占位符（`{n} 块`）、跟在「瓦片」前
# （`块瓦片`）、跟在速率里（`块/秒`）。裸禁「块」是不行的 —— 「缺块」是另一个
# 概念的典范词，它合法地含这个字。
_ZH_BANNED_TILE_MEASURE = r'\}\s*块|块瓦片|块\s*/\s*秒|块缺失'


def test_tiles_are_counted_with_one_measure_word():
    """瓦片量词只有「张」。

    改前 `js.map.tile_estimate.over` 一句里两个量词：「预计 {count} 块瓦片 ·
    按 10 张/秒…」。
    """
    bad = _offenders(_ZH_BANNED_TILE_MEASURE, 'zh')
    assert not bad, (
        '这些文案用「块」数瓦片，典范量词是「张」：\n  ' + '\n  '.join(bad))


# DEM 允许出现的地方：数据集/产品全称。改了这些就对不上供应商文档。
_DEM_DATASET_KEYS = frozenset({
    'tpl.index.download.dem_aster',       # ASTER GDEM v3
})


def test_dem_survives_only_in_dataset_names():
    """DEM 只在数据集名里出现，其余一律「高程」/ elevation。

    审查报告的原话。改前 DEM 出现在缓存分类、产物目录名、拖放提示、任务阶段
    动词、四条 toast 里 —— 那是实现术语泄漏给用户，而中文界面上同一个东西
    另有「高程」这个名字。
    """
    bad = _offenders(r'\bG?DEM\b', 'zh', allowed=_DEM_DATASET_KEYS)
    bad += _offenders(r'\bG?DEM\b', 'en', allowed=_DEM_DATASET_KEYS)
    assert not bad, (
        '这些文案把 DEM 露给用户，而它不是数据集名。允许的键只有 '
        f'{sorted(_DEM_DATASET_KEYS)}：\n  ' + '\n  '.join(bad))


def test_export_is_only_used_for_repackaging_an_artifact():
    """「导出」不许扩散到「接受缺块」那条链路上。

    反向断言：典范词表里「导出」那组之外，整本目录一个「导出」/ export 都不许有。
    改前 `js.gaps.action.accept` 叫「接受缺口并导出」、
    `js.history.confirm.delete_task_pending_decision` 复述成「接受并导出」，
    而 `acceptTaskGaps`（static/js/task_center.js:991）POST 的是 `/accept_gaps`，
    跑的是拼接/复制阶段 —— 一次导出都没有。真正的导出是另一条路由
    （`/api/tasks/<id>/export`，`js.export.*`）。
    """
    export_keys = next(c for c in _CONCEPTS if c.name == '导出').keys
    bad = _offenders('导出', 'zh', allowed=export_keys)
    bad += _offenders(r'\bexport', 'en', allowed=export_keys)
    assert not bad, (
        '这些文案在说「导出」，但它们不属于「把产物重打包成另一种格式」那条'
        '语义 —— 拆键，不要共用这个词：\n  ' + '\n  '.join(bad))


def test_the_save_path_is_called_the_same_thing_everywhere():
    """产物落地的那个路径只有一个名字。

    改前：详情面板叫「输出路径」，表单叫「保存路径」，两条校验错误也叫「保存
    路径」。用户按错误提示去找「保存路径」，详情面板里没有这一行。
    """
    keys = (
        'tpl.base.detail.output_path',
        'tpl.index.download.output_path',
        'val.geo.output_path.must_be_absolute',
        'val.geo.output_path.min_depth',
    )
    problems = []
    for key in keys:
        entry = MESSAGES[key]
        if '保存路径' not in entry['zh']:
            problems.append(f'{key} [zh]: {entry["zh"]!r}')
        if 'save path' not in entry['en'].lower():
            problems.append(f'{key} [en]: {entry["en"]!r}')
    assert not problems, (
        '这几处说的是同一个路径，名字必须一致（典范「保存路径」/ "Save path"）：'
        '\n  ' + '\n  '.join(problems))


# ---------------------------------------------------------------------------
# 标签与行为对齐（6 处）
#
# 每条钉的都是「这颗控件实际干什么」与「文案说它干什么」之间的那道缝，判据尽量
# 落在**行为的证据**上（路由、是否落库、是否只写输入框），而不是主观措辞。
# ---------------------------------------------------------------------------

# 选区那一格与工具条：可见文案的扫查范围。
#
# 2026-08-17 迁移登记。**改前钉什么**：单键 `js.map.bounds.create_task` —— 地图
# 右上角选区浮层上那颗主按钮的可见文案。它改前逐字写着「下载」，而点下去走的是
# `openCreatePanel`（更早叫 `openDownloadModal`）：只是把新建面板打开并预选管线，
# 一个瓦片都不下。同时钉反向一侧：`_title` 那个键必须仍说「下载任务」（后果要
# 交代），可见文案与 title 分两个键正是为了这个区别。
#
# **为什么换**：工具条瘦身把 `#boundsInfo` 整块搬进「新建任务」面板的选区段，
# 那颗主按钮（`#boundsCreateBtn`）连同 `js.map.bounds.create_task` /
# `create_task_title` 两个键一起退役 —— 搬进面板之后它是一颗「在新建任务面板里
# 打开新建任务面板」的钮，唯一入口收回工具条那颗「新建」。判据的宿主没了：
# `MESSAGES.get(...)` 拿到 None，用例只会一直红。
#
# **现在钉什么**：同一个不变量 —— **标签不许承诺它不做的动作** —— 宿主从「一个
# 键」换成「两块区域里所有按钮的可见文案」：
#   · `#mapToolbar`（工具条六颗）
#   · `#selectionField`（选区那一格；读数那几颗由 JS 全量重建，见下面的取法）
# 这两块里没有一颗钮会下载任何东西：打开面板 / 导入区域 / 光照 / 三个面板 toggle /
# 去框选 / 手动输入四至（确定·取消只写输入框）/ 清除选区。真正会去拉瓦片的是
# 提交那一步，而提交钮在面板**页脚**，不在这两块里。
#
# 为什么不是删掉用例：删了就等于把「浮层主按钮写下载却只开表单」这个踩出来的坑
# 从测试里抹掉，下一颗新按钮照样可以写「下载」。换成区域扫查比原来的单键还严 ——
# 原来只管一颗钮，现在管这两块里的每一颗，包括以后新加的。
_BUTTON_RE = re.compile(r'<button\b([^>]*)>(.*?)</button>', re.S)
_T_CALL_RE = re.compile(r"t\(\s*'([^']+)'")

# 反向的防空网：这三颗是实测（2026-08-17）扫到的、确定在范围内的按钮。扫查范围
# 一旦和产品对不上（换了容器 id、读数换了渲染函数），本条会退化成「因为空所以
# 过」——那种断言比没有断言更坏，它占着一条测试名让人以为这块还有人看着。
#
# 实测（2026-08-21 复核）四块区域各扫到几个可见文案键：
#   #mapToolbar               0（工具条 2026-08-21 起纯图标化，六颗按钮的文字
#                                标签按用户要求取消，名义由 aria-label/title 承担；
#                                原先的 tpl.index.toolbar.create 从 _MUST_BE_SWEPT
#                                移除）
#   #selectionField           2（去框选 / 手动输入范围）
#   updateBoundsInfo()        1（清除选区）
#   _renderManualBounds()     2（确定 / 取消）
# 去重后 5 个。`js.map.bounds.sr_` 那个拼接前缀不在其中（不是一条文案）。
_MUST_BE_SWEPT = (
    'tpl.index.create.draw_rect',    # 选区段「去框选」
    'js.map.bounds.clear',           # 读数里「清除选区」（JS 重建出来的那颗）
)


def _visible_label_keys(markup):
    """`markup` 里每颗 `<button>` 的**可见文案**用到的 i18n 键。

    只取开标签那个 `>` 之后的内容：`title` / `aria-label` 落在属性区里，**刻意**
    排除 —— 可见文案与 title 分成两个键就是为了各说一件事（文案说动作，title 说
    后果），把属性一起扫进来会把「后果里提下载」误判成「文案承诺下载」。
    """
    keys = []
    for m in _BUTTON_RE.finditer(markup):
        keys += _T_CALL_RE.findall(m.group(2))
    return keys


def _selection_and_toolbar_markup():
    """{区域名: markup}，覆盖选区那一格与工具条里全部按钮。

    模板里 `#createBoundsReadout` 只是个空壳 `<div>` —— 四至读数、手动输入面板、
    「清除选区」都是 `updateBoundsInfo()` / `_renderManualBounds()` 用模板字面量
    全量重建的，所以那几颗钮的 markup 只能从这两个函数体里取。
    """
    html = _template('index.html')
    out = {}
    for el_id in ('mapToolbar', 'selectionField'):
        span = _div_span(html, el_id)
        assert span, (
            f'templates/index.html 里找不到 #{el_id} 的 <div> —— 本测试已失效，'
            '扫查范围必须跟着宿主改，不许静默跳过')
        out[f'#{el_id}'] = html[span[0]:span[1]]
    map_js = _strip_js_comments(_js('map.js'))
    for fn in ('updateBoundsInfo', '_renderManualBounds'):
        out[f'map.js::{fn}()'] = _js_function_body(map_js, fn)
    return out


def test_no_button_in_the_selection_field_or_toolbar_promises_a_download():
    """选区那一格与工具条里，没有一颗按钮的可见文案说「下载」。

    这两块里一颗下载的钮都没有（逐颗行为见上面那段登记）：全是画框 / 改四至 /
    清选区 / 开关面板。谁在这里写「下载」，就是在承诺一个点下去不会发生的动作 ——
    这正是改前 `#boundsCreateBtn` 干的事（文案「下载」，行为 `openCreatePanel`）。

    本条是那条单键断言的迁移版（原名
    `test_the_selection_overlay_primary_button_does_not_promise_a_download`，
    宿主浮层与那个键都已退役，沿革与判据变更写在上面那段登记里）。
    """
    problems = []
    swept = set()
    for region, markup in _selection_and_toolbar_markup().items():
        for key in _visible_label_keys(markup):
            entry = MESSAGES.get(key)
            if entry is None:
                # `t('js.map.bounds.sr_' + field)` 这种拼接出来的键，命中的是前缀
                # 片段而不是一条文案。方位词自己另有断言（sr_north/... 四条）。
                continue
            swept.add(key)
            if '下载' in entry['zh'] or 'download' in entry['en'].lower():
                problems.append(
                    f'{region}: {key} = {entry["zh"]!r} / {entry["en"]!r}')
    assert not problems, (
        '选区那一格 / 工具条里有按钮的可见文案在说「下载」，而这两块里没有一颗钮'
        '会下载任何东西（真正拉瓦片的是面板页脚的提交钮）：\n  '
        + '\n  '.join(problems))
    missing = [k for k in _MUST_BE_SWEPT if k not in swept]
    assert not missing, (
        f'这些按钮没被扫到：{missing} —— 扫查范围已经和产品对不上，本条正在变成'
        f'「因为空所以过」（实测 2026-08-21 共扫到 5 个可见文案键，'
        f'这次只扫到 {len(swept)} 个）')
    assert 'js.map.bounds.download' not in MESSAGES, (
        '旧键 js.map.bounds.download（「下载」/"Download"）还在 —— 它就是那句'
        '与行为矛盾的文案本身，退役了就不许回来'
    )


def test_the_clear_selection_control_uses_one_verb_in_label_and_title():
    """一控件一动词：清选区那颗钮的可见文案与 `title` 必须同一个动词。

    改前它的可见文案是「删除」、`title` 是「清除选区」——同一颗按钮上两个动词，
    而「删除」在这个界面的别处指的是删任务（不可逆、要过确认框）。清选区既不
    删任务也不动磁盘。

    Task 5 把两个键并成一个（`js.map.bounds.clear`，可见文案与 title 同一个
    字符串），所以「同动词」在结构上就成立了；本条钉的是那个结构别退回去。
    """
    entry = MESSAGES.get('js.map.bounds.clear')
    assert entry, (
        'js.map.bounds.clear 不在 catalog 里 —— 清选区按钮的合并键还没落地'
        '（Task 5 负责，同时删掉 js.map.bounds.delete 与 js.map.bounds.clear_title）'
    )
    assert '清除' in entry['zh'] and '删除' not in entry['zh'], (
        f'清选区的文案要么不含典范动词「清除」，要么还在说「删除」：{entry["zh"]!r}')
    assert 'clear' in entry['en'].lower() and 'delete' not in entry['en'].lower(), (
        f'英文侧同理：{entry["en"]!r}')
    for dead in ('js.map.bounds.delete', 'js.map.bounds.clear_title'):
        assert dead not in MESSAGES, (
            f'{dead} 还在 —— 可见文案与 title 又回到两个键、可以各写一个动词')
    # 命令面板里的同一个动作也得是同一个动词（它不是浮层上那颗钮，但干的是同
    # 一件事，用户在两处看到两个动词一样会以为是两回事）。
    assert '清除' in MESSAGES['js.cmdk.clear_bounds']['zh'], (
        f"命令面板的清选区命令换了动词：{MESSAGES['js.cmdk.clear_bounds']['zh']!r}")


def test_the_benchmark_button_says_it_only_fills_the_field():
    """「测速推荐」跑完 30 秒只把数字填进输入框，不落库 —— 标签必须说出「填」。

    行为证据：`static/js/config.js:222-229` POST
    `/api/config/recommend_concurrency`，拿到 `data.recommended` 之后只做一件事
    —— `document.getElementById('concurrent_downloads').value = data.recommended`。
    是否落库仍由「保存配置」决定（同文件 `:208` 的注释就是这么写的）。

    旁边的 hint 已经诚实（「只填入数值，保存后生效」），但一个在配置页上按了
    按钮的人默认会以为按钮生效了 —— 缝在标签上，所以补在标签上。
    """
    entry = MESSAGES['tpl.config.download.concurrency_recommend']
    assert '填' in entry['zh'], (
        f'按钮文案没说它只是把数字填进输入框：{entry["zh"]!r}')
    assert 'fill' in entry['en'].lower(), (
        f'英文侧没说它只是填入：{entry["en"]!r}')
    # 反向：hint 那一侧必须继续交代「保存后生效」，否则把话都挪到按钮上、
    # hint 变成一句空话时这条也该红。
    hint = MESSAGES['tpl.config.download.concurrency_hint']
    assert '保存后生效' in hint['zh'] and 'save' in hint['en'].lower(), (
        f'hint 不再交代要保存才生效：{hint!r}')


def test_the_reset_confirm_says_it_is_immediate_and_irreversible():
    """「重置为默认值」的确认框必须写明「立即」与「不可恢复」。

    行为证据：`static/js/config.js:756-770` —— 确认之后立刻 POST
    `/api/config/reset`，成功就 600ms 后 `location.reload()`。改前的确认文案是
    「确定要重置所有配置为默认值吗？」，一个字都没提这是立刻落库且不可逆的。

    对照：清一类缓存要过**两个**对话框，第二个明写「不可恢复」
    （`static/js/config.js:137-153`）。重置**全部**配置同样不可逆、同样落库，
    却只有一个对话框 —— 而 `showConfirm` 的静息焦点就在「确认」上
    （`static/js/ui.js:250-253`），回车即执行。
    """
    entry = MESSAGES['js.config.reset.confirm']
    for word in ('立即', '不可恢复'):
        assert word in entry['zh'], (
            f'重置确认没写「{word}」—— 它立刻落库并刷新页面：{entry["zh"]!r}')
    low = entry['en'].lower()
    for word in ('immediate', 'cannot be undone'):
        assert word in low, f'英文侧没写 {word!r}：{entry["en"]!r}'


def test_accepting_gaps_does_not_claim_to_export():
    """「接受缺块」那颗钮不许说「导出」—— 它不导出任何东西。

    行为证据：`static/js/task_center.js:991-1045` 的 `acceptTaskGaps` POST
    `/api/tasks/<id>/accept_gaps`（插件管线是 `/accept-gaps`），后端把状态推到
    `completed_with_gaps` 并跑严格模式拒绝过的拼接/复制阶段。导出是另一条路由
    （`/api/tasks/<id>/export`）、另一颗按钮（`js.gaps.action.export`）、而且
    `js.export.confirm.message` 明说导出是「追加一份产物」。

    两处说同一句假话，一起钉：按钮本身、和删除确认里对它的复述。
    """
    for key in ('js.gaps.action.accept',
                'js.history.confirm.delete_task_pending_decision'):
        entry = MESSAGES[key]
        assert '导出' not in entry['zh'], (
            f'{key} 还在说「导出」，而接受缺块只是让管线出片：{entry["zh"]!r}')
        assert 'export' not in entry['en'].lower(), (
            f'{key} 的英文还在说 export：{entry["en"]!r}')
    # 反向：它必须说出自己**真正**干的事，否则删掉「导出」二字就算过关了。
    accept = MESSAGES['js.gaps.action.accept']
    assert '生成产物' in accept['zh'], (
        f'接受缺块的按钮没说它会生成产物：{accept["zh"]!r}')
    assert 'produce the artifact' in accept['en'].lower(), (
        f'英文侧同理：{accept["en"]!r}')


def test_the_single_submit_button_does_not_name_one_pipeline():
    """合并后的新建面板只有一颗提交钮，它不许只说「下载」那一条管线。

    Task 5 把两个弹窗（下载 / 处理）并成一个面板、四条管线共用一颗提交钮，
    `tpl.index.download.submit` 是活下来的那个键。它原来叫「创建下载任务」——
    用户选了「等高线」或「本地地形切片」时，那句话是假的。
    """
    entry = MESSAGES['tpl.index.download.submit']
    assert '下载' not in entry['zh'], (
        f'唯一的提交钮只提「下载」，但它也创建等高线与地形切片任务：'
        f'{entry["zh"]!r}')
    assert 'download' not in entry['en'].lower(), f'英文侧同理：{entry["en"]!r}'
    assert '创建' in entry['zh'] and 'create' in entry['en'].lower(), (
        f'提交钮不再说「创建」：{entry!r}')
    # 与它一起消失的那颗（处理弹窗的提交钮）不许还在 catalog 里：留着就是死键，
    # 而且下一个人会照它把第二颗提交钮加回来。
    assert 'tpl.index.process.submit' not in MESSAGES, (
        'tpl.index.process.submit（「创建处理任务」）还在 —— 合并后只有一颗提交钮')


# ---------------------------------------------------------------------------
# EN 面
# ---------------------------------------------------------------------------

def test_no_english_label_is_a_bare_total():
    """裸 `Total` 归零 —— 它曾经是三个不同指标在英文界面上的同一个名字。

    实测渲染：`/history` 的四张统计卡在英文下是
    `Total tasks / Completed / Failed / Total`，第四张的指标名（累计下载量）
    整个丢了；而另外两处（任务详情的「总数量」、配置页缓存的「总计」）也叫
    `Total`。三处撞名，其中两处还同屏可见。
    """
    bad = [
        f'{key}: zh={MESSAGES[key]["zh"]!r}'
        for key in sorted(MESSAGES)
        if MESSAGES[key]['en'].strip() == 'Total'
    ]
    assert not bad, (
        '这些键的英文是裸 `Total` —— 指标名丢了，且彼此撞名。每个都要带上量的'
        '名字（Total tasks / Total tiles / Total size / Total downloaded）：\n  '
        + '\n  '.join(bad))


def test_the_history_stat_cards_have_four_distinct_english_names():
    """四张统计卡同屏，英文名必须两两不同 —— 上一条只禁裸 `Total`，四张全叫
    `Count` 照样满足它。"""
    keys = ('tpl.history.stats.total', 'tpl.history.stats.completed',
            'tpl.history.stats.failed', 'tpl.history.stats.downloaded')
    names = {key: MESSAGES[key]['en'] for key in keys}
    assert len(set(names.values())) == len(keys), (
        f'统计卡的英文名有重复，四张卡同屏：{names}')
    # 中文侧同理（改前中文是对的，别在修英文时把它弄坏）。
    zh = {key: MESSAGES[key]['zh'] for key in keys}
    assert len(set(zh.values())) == len(keys), f'统计卡的中文名有重复：{zh}'


# 四条管线在 catalog 里各有若干处名字。en 侧钉**全等**而不是「含典范词」：
# 'Local terrain' / 'Local terrain tiling' / 'Local DEM tiling' / 'Terrain tiling'
# 四个英文名指同一条管线，而它们互相之间都是子串关系，钉包含关系抓不住。
_PIPELINE_EN_NAMES = {
    'map': ('Map tiles', (
        'api.tasks.pipeline.map',
        'tpl.index.download.type_map',
    )),
    'dem': ('Elevation', (
        'api.tasks.pipeline.dem',
        'tpl.index.download.type_dem',
        'js.history.meta.dem',
    )),
    'local_terrain': ('Local terrain tiling', (
        'api.tasks.pipeline.local_terrain',
        'tpl.index.process.type_local_terrain',
        'js.history.meta.local_terrain',
        'js.map.process.local_terrain_default_name',
    )),
}

_PIPELINE_ZH_NAMES = {
    'map': '地图瓦片',
    'dem': '高程',
    'local_terrain': '本地地形切片',
}


@pytest.mark.parametrize('pipeline', sorted(_PIPELINE_EN_NAMES))
def test_one_pipeline_has_one_english_name(pipeline):
    """一条管线一个英文名，四处 catalog 逐字相同。

    最刺眼的是 `local_terrain`：`Local DEM tiling`（新建表单）/
    `Local terrain tiling`（任务行、默认任务名）/ `Local terrain`（API 消息）
    —— 三个英文名，用户在三个地方以为是三条管线。中文侧同样有两个名字
    （「本地高程切片」/「本地地形」）。

    `contour` 不在表里：它的四处已经都含「等高线」/ Contour，且各处的修饰
    （「等高线」vs「等高线瓦片」）是有意的粒度差别，由典范词表那条覆盖。
    """
    expected_en, keys = _PIPELINE_EN_NAMES[pipeline]
    expected_zh = _PIPELINE_ZH_NAMES[pipeline]
    problems = []
    for key in keys:
        entry = MESSAGES.get(key)
        if entry is None:
            problems.append(f'{key}: catalog 里没有这个键 —— 本用例的键表已失效')
            continue
        if entry['en'] != expected_en:
            problems.append(f'{key} [en] = {entry["en"]!r}，期望 {expected_en!r}')
        if entry['zh'] != expected_zh:
            problems.append(f'{key} [zh] = {entry["zh"]!r}，期望 {expected_zh!r}')
    assert not problems, (
        f'管线 {pipeline} 在 catalog 里不止一个名字：\n  ' + '\n  '.join(problems))


def test_gap_outcome_labels_are_labels_not_sentence_fragments():
    """`js.gaps.outcome.*` 当标签用（`js.gaps.pair` = `{label} {count}`），
    英文必须是标签形态。

    改前渲染出来是 `no data upstream 12` —— 小写句子片段后面跟一个数字。中文侧
    没有这个问题（「上游无数据 12」读得通），所以只钉英文首字母。
    """
    outcome_keys = sorted(k for k in MESSAGES if k.startswith('js.gaps.outcome.'))
    assert len(outcome_keys) == 4, (
        f'缺块分档从 4 档变成了 {len(outcome_keys)} 档 —— 本用例的假设已失效：'
        f'{outcome_keys}')
    bad = [f'{key}: {MESSAGES[key]["en"]!r}' for key in outcome_keys
           if not MESSAGES[key]['en'][:1].isupper()]
    assert not bad, (
        '这些分档名当标签用，英文却是小写句子片段（渲染成 `no data upstream 12`）'
        '：\n  ' + '\n  '.join(bad))
    # 别处引用这些标签时必须逐字引用，否则用户按提示里的字样在界面上找不到。
    quoted = MESSAGES['js.gaps.outcome.no_data']['en']
    for key in ('js.gaps.action.refill_title', 'js.gaps.explained'):
        assert f'"{quoted}"' in MESSAGES[key]['en'], (
            f'{key} 里引用的分档名与标签本身不一致（标签是 {quoted!r}）：'
            f'{MESSAGES[key]["en"]!r}')


def test_english_values_stay_free_of_chinese():
    """本文件改了 100 余处英文值，顺手在这里再钉一次 en 不含汉字。

    与 `tests/test_i18n.py:50-60` 同口径，重复是有意的：那条是全局闸门，这条让
    「改术语时把中文粘进 en 栏」在本文件里当场红，不用跨文件定位。
    """
    allowed = {'app.suffix', 'app.language.zh', 'app.language.en'}
    bad = [key for key in sorted(MESSAGES)
           if key not in allowed and _HAN.search(MESSAGES[key]['en'])]
    assert not bad, f'这些键的英文里有汉字: {bad}'


# ---------------------------------------------------------------------------
# 引号里的界面名词必须真的存在
#
# 触发这条的缺陷：`tpl.index.process.contour_source_hint` 让用户「在「下载数据」
# 里做」远程高程下载 —— 「下载数据」是旧下载弹窗的标题 tpl.index.download.title，
# 59459b1 把两个弹窗并成 #createPanel 时把键一起删了。文案里指向一个界面上根本
# 不存在的名字，用户只能挨个点过去找。上面那批术语断言全抓不到它：它不是「同
# 一个东西两个名字」，是「一个名字零个东西」。
#
# 判据：值里被引号包住的词，必须**逐字**等于某个现存键的同语种值。逐字是关键
# —— 用户是拿这几个字去界面上对着找的，差一个字就找不到。
#
# 扫 「」“”（zh）与 ""“”（en）。**不扫《》**：书名号在中文里指作品/文档名，
# 而文档名本来就不该是 catalog 里的键，纳进来等于给一个正当用法预埋一条必红的
# 规则。实测当前目录里 《》 零处，纳不纳都不改变现在的结果。
# ---------------------------------------------------------------------------

_QUOTED_PATTERNS = {
    'zh': (re.compile(r'「([^「」]+)」'), re.compile(r'“([^“”]+)”'),
           re.compile(r'"([^"]+)"')),
    'en': (re.compile(r'"([^"]+)"'), re.compile(r'“([^“”]+)”')),
}

# 整段就是一个插值占位符的引号不是界面名词，是运行时才知道的名字（数据集名、
# 缓存分类名）。这些走规则排除而不是白名单：白名单是逐条例外，占位符是一整类。
_PLACEHOLDER_ONLY = re.compile(r'^\{[A-Za-z_][A-Za-z0-9_]*\}$')

# 逐条带理由的例外。键是 (catalog 键, 语种, 引号里的词)，不是整条值 —— 同一条
# 值里的其它引用照样受管。
_QUOTED_TERM_WHITELIST = {
    ('js.config.proxy.none', 'zh', '允许局域网连接'):
        'Clash/v2rayN 自己的选项名，不是本项目的界面标签；本目录永远不会有这个键',
    ('js.config.proxy.none', 'en', 'Allow LAN'):
        '同上，第三方客户端的选项名',
    ('tpl.index.process.terrain_quality_hint', 'zh', '自动'):
        '复选框标签是 tpl.index.process.local_terrain_maxzoom_auto ='
        '「自动（按源数据分辨率决定）」，括号里是解释不是名字，正文引的是词头',
    ('tpl.index.process.terrain_quality_hint', 'en', 'Auto'):
        '同上，标签是 `Auto (from source resolution)`',
    ('js.history.terrain.maxzoom_auto_hint', 'zh', '自动'):
        '同上；这里引的是 js.history.terrain.maxzoom_auto ='
        '「自动（按源数据分辨率）」的词头',
}


def _quoted_terms():
    """产出整本目录里 (键, 语种, 引号里的词)，纯占位符已排除。"""
    for key in sorted(MESSAGES):
        for locale in ('zh', 'en'):
            value = MESSAGES[key][locale]
            for pattern in _QUOTED_PATTERNS[locale]:
                for term in pattern.findall(value):
                    if _PLACEHOLDER_ONLY.match(term):
                        continue
                    yield key, locale, term


def _unmatched_quoted_terms():
    """引号里的词在同语种的所有键值里找不到逐字相同的那一份 —— 违规集合。"""
    labels = {locale: {MESSAGES[key][locale] for key in MESSAGES}
              for locale in ('zh', 'en')}
    return {(key, locale, term) for key, locale, term in _quoted_terms()
            if term not in labels[locale]}


def test_quoted_ui_terms_name_a_real_label():
    """文案里引号引的界面名词，必须逐字等于某个现存键的值。

    抓的是「指向一个不存在的界面」这类缺陷：contour_source_hint 曾把用户指到
    「下载数据」，而那个标签随旧弹窗一起被删了。把那句改回去，这里立刻红。

    实测面：整本目录 35 处引用，30 处当场逐字命中；剩下 5 处进白名单，理由写在
    `_QUOTED_TERM_WHITELIST` 里（两处第三方客户端的选项名、三处引「自动（…）」
    这类带括号解释的标签词头）。误报面小到能收成一条干净契约，所以钉的是全量
    引用，不是「不许出现某几个死标签词」那种黑名单。
    """
    bad = sorted(entry for entry in _unmatched_quoted_terms()
                 if entry not in _QUOTED_TERM_WHITELIST)
    assert not bad, (
        '这些文案引用了界面上不存在的名字（用户拿这几个字去界面上找不到东西）：\n'
        + '\n'.join(
            f'  {key} [{locale}] 引用了 {term!r}，但没有任何键的 {locale} 值'
            f'逐字等于它\n      整句：{MESSAGES[key][locale]!r}'
            for key, locale, term in bad)
        + '\n\n修法：改成真实标签的逐字文案；确属例外（第三方界面、词头引用）'
        '就进 _QUOTED_TERM_WHITELIST 并写明理由。')


def test_quoted_term_whitelist_has_no_stale_entries():
    """白名单里每一条都必须仍然是一处「引号里的词找不到对应标签」。

    白名单会腐烂：文案改掉了、或者那个标签后来真的建了键，豁免还留着，下一个
    人照抄它就把一处真缺陷放行了。这条让腐烂的豁免自己响。
    """
    live = _unmatched_quoted_terms()
    stale = sorted(entry for entry in _QUOTED_TERM_WHITELIST if entry not in live)
    assert not stale, (
        '白名单里这些条目已经不再是违规了，删掉它们：\n'
        + '\n'.join(f'  {key} [{locale}] {term!r} —— '
                    f'{_QUOTED_TERM_WHITELIST[(key, locale, term)]}'
                    for key, locale, term in stale))
