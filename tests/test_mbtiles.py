"""MBTiles 1.3 容器 —— 写入端、读取端、校验器。

这个文件盯的是「文件写出去之后没人再看得见」的那一类缺陷：TMS 行号翻错会让
地图上下颠倒、`scheme=xyz` 元数据会让读取端按错误的约定解释行号、pbf 缺
`vector_layers` 会让库能打开、瓦片能取出、地图上什么都不显示。这些都不会在
写入时报错，只会在**下游用户**那里表现成「你们的产物是坏的」。

所有断言都打在可观测契约上：库里的原始 `tile_row` 值、metadata 的键集合、
最终路径上有没有文件、validate 报告里的 problems —— 不碰任何私有函数。
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.services.mbtiles import (  # noqa: E402
    MBTilesError,
    MBTilesWriter,
    read_metadata,
    read_tile,
    validate_mbtiles,
)

PNG = b'\x89PNG\r\n\x1a\n' + b'fake-png-body'
JPG = b'\xff\xd8\xff' + b'fake-jpg-body'
PBF = b'\x1a\x0bfake-vector-tile'


# ---------------------------------------------------------------------------
# 往返
# ---------------------------------------------------------------------------

def test_round_trip_writes_reads_and_reports_the_same_tiles(tmp_path):
    """写进去的瓦片按 XYZ 坐标原样读得回来，metadata 的必填键齐全。"""
    out = tmp_path / 'sat.mbtiles'
    with MBTilesWriter(out, fmt='png', name='satellite') as writer:
        writer.add_tile(3, 5, 2, PNG)
        writer.add_tile(3, 6, 2, PNG + b'2')
        result = writer.finalize()

    assert result['tile_count'] == 2
    assert result['minzoom'] == 3 and result['maxzoom'] == 3
    assert read_tile(out, 3, 5, 2) == PNG
    assert read_tile(out, 3, 6, 2) == PNG + b'2'
    assert read_tile(out, 3, 7, 2) is None

    meta = read_metadata(out)
    assert meta['name'] == 'satellite'
    assert meta['format'] == 'png'
    for key in ('bounds', 'minzoom', 'maxzoom'):
        assert meta.get(key, '').strip(), f'metadata 缺必填键 {key}'


def test_duplicate_coordinates_replace_instead_of_multiplying(tmp_path):
    """重跑导出（补块、续传后重打包）必须幂等：同坐标覆盖，不撞唯一索引。"""
    out = tmp_path / 'dup.mbtiles'
    with MBTilesWriter(out, fmt='png') as writer:
        writer.add_tile(2, 1, 1, PNG)
        writer.add_tile(2, 1, 1, PNG + b'newer')
        result = writer.finalize()
    assert result['tile_count'] == 1
    assert read_tile(out, 2, 1, 1) == PNG + b'newer'


# ---------------------------------------------------------------------------
# TMS 行号
# ---------------------------------------------------------------------------

def test_tile_row_is_stored_flipped_to_tms(tmp_path):
    """断言库里**原始的 tile_row 数值**，不是「读回来能对上」。

    读写共用一次翻转时，两边一起翻错的结果仍然能自洽地读回来 —— QGIS /
    tileserver 打开却是上下颠倒的。所以这里必须直接查表。
    """
    out = tmp_path / 'tms.mbtiles'
    with MBTilesWriter(out, fmt='png') as writer:
        writer.add_tile(3, 5, 2, PNG)      # XYZ y=2 → TMS row 2^3-1-2 = 5
        writer.finalize()

    conn = sqlite3.connect(str(out))
    try:
        rows = conn.execute(
            'SELECT zoom_level, tile_column, tile_row FROM tiles').fetchall()
    finally:
        conn.close()
    assert rows == [(3, 5, 5)]


def test_tms_input_scheme_is_taken_as_already_flipped(tmp_path):
    """`scheme='tms'` 表示**调用方给的就是行号**，不能再翻一次。"""
    out = tmp_path / 'tms-in.mbtiles'
    with MBTilesWriter(out, fmt='png', scheme='tms') as writer:
        writer.add_tile(3, 5, 5, PNG)
        writer.finalize()
    assert read_tile(out, 3, 5, 2) == PNG      # XYZ 视角看到的是 y=2


def test_metadata_never_declares_a_scheme(tmp_path):
    """写入端**永不**产出 `scheme` 键。

    TMS 是规范默认；写一个 `scheme=xyz` 在翻转过的行号旁边，读取端按声明解释
    就会把地图上下颠倒 —— 而库本身自洽，问题只在别人的软件里出现。
    """
    out = tmp_path / 'noscheme.mbtiles'
    with MBTilesWriter(out, fmt='png') as writer:
        writer.add_tile(1, 0, 0, PNG)
        writer.finalize()
    assert 'scheme' not in read_metadata(out)


# ---------------------------------------------------------------------------
# 格式自洽
# ---------------------------------------------------------------------------

def test_vector_mbtiles_without_vector_layers_is_rejected_at_construction(tmp_path):
    """pbf 缺 vector_layers 在**构造时**就炸，不拖到 finalize。

    那是不可修复的元数据缺陷，让调用方先写完几十万块瓦片再发现纯属浪费；
    而且失败在构造期意味着磁盘上一个残件都不会留。
    """
    out = tmp_path / 'vec.mbtiles'
    with pytest.raises(MBTilesError, match='vector_layers'):
        MBTilesWriter(out, fmt='pbf')
    assert not out.exists()
    assert not list(tmp_path.glob('*.part*'))


def test_vector_layers_are_rejected_for_raster_formats(tmp_path):
    with pytest.raises(MBTilesError, match='vector_layers'):
        MBTilesWriter(tmp_path / 'r.mbtiles', fmt='png',
                      vector_layers=[{'id': 'road'}])


def test_vector_mbtiles_with_layers_writes_the_json_metadata_key(tmp_path):
    """规范要求 `json` 是一个**对象**（含 vector_layers 数组），不是裸数组。"""
    import json

    out = tmp_path / 'vec.mbtiles'
    with MBTilesWriter(out, fmt='pbf',
                       vector_layers=[{'id': 'road', 'fields': {'name': 'String'}}]) as w:
        w.add_tile(4, 3, 2, PBF)
        w.finalize()
    parsed = json.loads(read_metadata(out)['json'])
    assert isinstance(parsed, dict)
    assert [layer['id'] for layer in parsed['vector_layers']] == ['road']
    assert validate_mbtiles(out)['ok'] is True


def test_declared_format_and_magic_bytes_must_agree(tmp_path):
    """一个库只装一种格式 —— png 库里混进 jpg 必须当场拒绝。

    `looks_like_image` 那种「是不是某种图片」的判断在这里不够用：它回答不了
    「是不是**声明的那一种**」，而混格式的库会让读取端按 Content-Type 解错。
    """
    out = tmp_path / 'mixed.mbtiles'
    with MBTilesWriter(out, fmt='png') as writer:
        writer.add_tile(2, 1, 1, PNG)
        with pytest.raises(MBTilesError, match='does not look like png'):
            writer.add_tile(2, 2, 1, JPG)


def test_empty_tile_is_refused(tmp_path):
    """0 字节瓦片写进去比不写更糟：读取端会当成「有这块、但解不开」。"""
    with MBTilesWriter(tmp_path / 'e.mbtiles', fmt='png') as writer:
        with pytest.raises(MBTilesError):
            writer.add_tile(2, 1, 1, b'')


@pytest.mark.parametrize('zoom, x, y', [(2, 4, 0), (2, 0, 4), (2, -1, 0), (99, 0, 0)])
def test_out_of_grid_coordinates_are_refused(tmp_path, zoom, x, y):
    """越界坐标写进去不报错，只会在读取端变成永远取不到的死行。挡在入口最便宜。"""
    with MBTilesWriter(tmp_path / 'oob.mbtiles', fmt='png') as writer:
        with pytest.raises(MBTilesError):
            writer.add_tile(zoom, x, y, PNG)


# ---------------------------------------------------------------------------
# 原子落位
# ---------------------------------------------------------------------------

def test_nothing_appears_at_the_final_path_until_finalize(tmp_path):
    """写入期间最终路径必须是空的，只有 `.part.<pid>` 在。

    自动 finalize（或者直接写目标文件）会把「循环中途抛异常、只写了一半」
    变成一个看起来完整、metadata 也自洽的库 —— 最难发现的一类产物缺陷。
    """
    out = tmp_path / 'atomic.mbtiles'
    writer = MBTilesWriter(out, fmt='png')
    writer.add_tile(2, 1, 1, PNG)
    assert not out.exists()
    assert writer.part_path.exists()
    writer.finalize()
    assert out.exists()
    assert not writer.part_path.exists()


def test_leaving_the_context_without_finalize_leaves_no_artifact(tmp_path):
    """忘了 finalize / 中途抛异常 → 最终路径上什么都不会出现，残件也清掉。"""
    out = tmp_path / 'aborted.mbtiles'
    with pytest.raises(RuntimeError):
        with MBTilesWriter(out, fmt='png') as writer:
            writer.add_tile(2, 1, 1, PNG)
            part = writer.part_path
            raise RuntimeError('模拟导出中途失败')
    assert not out.exists()
    assert not part.exists()


def test_finalizing_an_empty_library_is_refused(tmp_path):
    """空库推不出 bounds / minzoom / maxzoom 这三个必填键，写出来不合规。"""
    out = tmp_path / 'empty.mbtiles'
    writer = MBTilesWriter(out, fmt='png')
    with pytest.raises(MBTilesError, match='empty'):
        writer.finalize()
    assert not out.exists()
    assert not writer.part_path.exists()


def test_add_dir_packs_an_existing_xyz_pyramid(tmp_path):
    """已经下载好的 XYZ 目录不必重下就能变成 MBTiles；残件后缀天然被过滤掉。"""
    root = tmp_path / 'tiles'
    (root / '5' / '17').mkdir(parents=True)
    (root / '5' / '17' / '9.png').write_bytes(PNG)
    (root / '5' / '17' / '10.png').write_bytes(PNG)
    (root / '5' / '17' / '11.png.part.4242').write_bytes(PNG)   # 原子写残件
    (root / '5' / '17' / '12.png').write_bytes(b'')             # 已知的 0 字节残件
    (root / 'junk').mkdir()

    out = tmp_path / 'packed.mbtiles'
    with MBTilesWriter(out, fmt='png') as writer:
        added = writer.add_dir(root, extension='png')
        result = writer.finalize()
    assert added == 2
    assert result['tile_count'] == 2


# ---------------------------------------------------------------------------
# validate_mbtiles
# ---------------------------------------------------------------------------

def _good_library(tmp_path, name='ok.mbtiles'):
    out = tmp_path / name
    with MBTilesWriter(out, fmt='png', name='ok') as writer:
        writer.add_tile(3, 5, 2, PNG)
        writer.add_tile(4, 10, 5, PNG)
        writer.finalize()
    return out


def test_validate_accepts_a_library_this_module_wrote(tmp_path):
    """写入端与校验端必须自洽 —— 否则每一份自产产物都会被自己判为坏的。"""
    report = validate_mbtiles(_good_library(tmp_path))
    assert report['ok'] is True
    assert report['problems'] == []
    assert report['tile_count'] == 2
    assert (report['minzoom'], report['maxzoom']) == (3, 4)
    assert report['format'] == 'png'


def test_validate_reports_a_metadata_zoom_that_contradicts_the_tiles(tmp_path):
    """声明与事实不符要如实报出来：maxzoom 撒谎会让读取端不去请求最高层。"""
    out = _good_library(tmp_path)
    conn = sqlite3.connect(str(out))
    with conn:
        conn.execute("UPDATE metadata SET value='9' WHERE name='maxzoom'")
    conn.close()

    report = validate_mbtiles(out)
    assert report['ok'] is False
    assert report['maxzoom'] == 4          # 事实基准仍然报实际值
    assert any('maxzoom' in p for p in report['problems'])


def test_validate_reports_a_declared_format_the_tiles_contradict(tmp_path):
    """把 PNG 库的 metadata.format 改成 jpeg，校验必须报出来。

    校验端过去只看 metadata 里那个字符串，于是它在「决定库能不能被别人打开」
    的那一个字段上**比写入端还松** —— `add_tile` 见到魔数不符当场拒收，
    validate 却回 ok:true。而 validate 跑的正是导出成品与「不是本程序写的
    文件」，读取端拿 format 决定怎么解码每一块瓦片，声明成 jpeg 的 PNG 库在
    QGIS / MapLibre / tileserver 里就是一片解不开的灰。
    """
    out = _good_library(tmp_path)
    conn = sqlite3.connect(str(out))
    with conn:
        conn.execute("UPDATE metadata SET value='jpeg' WHERE name='format'")
    conn.close()

    report = validate_mbtiles(out)
    assert report['ok'] is False
    assert any('magic bytes' in p for p in report['problems']), report['problems']


def test_validate_does_not_invent_a_format_problem_on_a_consistent_library(tmp_path):
    """反方向：声明与瓦片一致时一个 problem 都不许冒出来。

    这条挡的是「抽查逻辑把每一份自产产物判成坏的」那种回归 —— 那比不检查更糟。
    """
    out = tmp_path / 'jpg.mbtiles'
    with MBTilesWriter(out, fmt='jpg', name='jpeg-library') as writer:
        writer.add_tile(3, 5, 2, JPG)
        writer.add_tile(3, 6, 2, JPG + b'2')
        writer.finalize()

    report = validate_mbtiles(out)
    assert report['ok'] is True, report['problems']
    assert report['format'] == 'jpg'


def test_validate_does_not_sniff_formats_without_a_reliable_magic(tmp_path):
    """pbf 没有可靠魔数（规范允许 gzip 压缩），不许在它身上编一个不符出来。"""
    out = tmp_path / 'v.mbtiles'
    with MBTilesWriter(out, fmt='pbf', name='vector',
                       vector_layers=[{'id': 'road'}]) as writer:
        writer.add_tile(3, 5, 2, PBF)
        writer.finalize()

    report = validate_mbtiles(out)
    assert not any('magic bytes' in p for p in report['problems']), report['problems']


def test_validate_flags_an_injected_scheme_key(tmp_path):
    """见到 `scheme` 就说明库不是本模块产的、或者被人改过 —— 地图会上下颠倒。"""
    out = _good_library(tmp_path)
    conn = sqlite3.connect(str(out))
    with conn:
        conn.execute("INSERT INTO metadata (name, value) VALUES ('scheme','xyz')")
    conn.close()
    report = validate_mbtiles(out)
    assert report['ok'] is False
    assert any('scheme' in p for p in report['problems'])


def test_validate_reports_missing_required_metadata(tmp_path):
    out = _good_library(tmp_path)
    conn = sqlite3.connect(str(out))
    with conn:
        conn.execute("DELETE FROM metadata WHERE name='bounds'")
    conn.close()
    report = validate_mbtiles(out)
    assert report['ok'] is False
    assert any("'bounds'" in p for p in report['problems'])


def test_validate_reports_bounds_that_do_not_cover_the_tiles(tmp_path):
    """声明的 bounds 比实际瓦片小 → 读取端按 bounds 裁剪会切掉真实存在的像素。"""
    out = _good_library(tmp_path)
    conn = sqlite3.connect(str(out))
    with conn:
        conn.execute("UPDATE metadata SET value='0,0,0.1,0.1' WHERE name='bounds'")
    conn.close()
    report = validate_mbtiles(out)
    assert report['ok'] is False
    assert any('bounds' in p for p in report['problems'])


def test_validate_reports_a_damaged_file_instead_of_raising(tmp_path):
    """损坏 / 根本不是 sqlite 的文件也只报告，不抛。

    校验的调用场景是「产物做完了，给用户一份体检报告」；第一个问题就抛出去
    等于只能看见一个问题。
    """
    broken = tmp_path / 'broken.mbtiles'
    broken.write_bytes(b'this is definitely not a sqlite database' * 32)
    report = validate_mbtiles(broken)
    assert report['ok'] is False
    assert report['problems']


def test_validate_reports_a_missing_file_instead_of_raising(tmp_path):
    report = validate_mbtiles(tmp_path / 'nope.mbtiles')
    assert report['ok'] is False
    assert report['problems']


def test_validate_reports_a_vector_library_without_layers(tmp_path):
    """外部来源的 pbf 库缺 json/vector_layers：能打开、能取瓦片、地图上空白。"""
    out = _good_library(tmp_path, name='faux-vector.mbtiles')
    conn = sqlite3.connect(str(out))
    with conn:
        conn.execute("UPDATE metadata SET value='pbf' WHERE name='format'")
    conn.close()
    report = validate_mbtiles(out)
    assert report['ok'] is False
    assert any('vector' in p or 'json' in p for p in report['problems'])
