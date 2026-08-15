"""下载缓存分类统计与手动清理测试(services/task_cleanup + /api/cache/*)。

缓存不做任何自动清理(旧的 LRU 强清已移除):get_cache_stats 只读统计,
clear_cache_category 由前端二次确认后手动触发;非法/越界/不存在的
分类一律 ValueError,绝不可误删 cache 之外的任何东西。

**分类 = 源命名空间目录。** 缓存一级目录已从裸样式码 `cache/m` 改成
`cache/<样式码>-<配置指纹8位>`(见 contracts/source.SourceSnapshot.cache_namespace
与 user_version 6 的改名迁移),所以:
  · 分类 key 是命名空间目录名本身(`m-1a2b3c4d`);
  · 标签由 _category_label 经 SourceSnapshot.style_of_namespace 翻成人话
    (`瓦片缓存（roadmap · 1a2b3c4d）`)—— 指纹留在括号里当消歧标识,换过源
    之后同一样式会有两个命名空间,它们的区别**只有**指纹;
  · 清理多了一道活动任务护栏(见文件末尾那一组)。
"""

import pytest

from src.core.config import Config
from src.services.task_cleanup import clear_cache_category, get_cache_stats

# 两个命名空间目录名。指纹是造出来的 8 位十六进制 —— 本文件测的是「目录名
# 怎么被解释」,不是指纹怎么算(那由 tests/test_source_registry 一类的用例钉)。
_NS_ROADMAP = "m-1a2b3c4d"
_NS_SATELLITE = "s-89abcdef"


def _make_cache(root):
    """构造两个源命名空间 + dem + 一个顶层散落文件。"""
    cache = root / "cache"
    (cache / _NS_ROADMAP / "10" / "1").mkdir(parents=True)
    (cache / _NS_ROADMAP / "10" / "1" / "2.png").write_bytes(b"x" * 100)
    (cache / _NS_ROADMAP / "10" / "1" / "3.png").write_bytes(b"x" * 50)
    (cache / _NS_SATELLITE / "10" / "1").mkdir(parents=True)
    (cache / _NS_SATELLITE / "10" / "1" / "2.png").write_bytes(b"x" * 200)
    (cache / "dem").mkdir(parents=True)
    (cache / "dem" / "N29E106.tif").write_bytes(b"x" * 1000)
    (cache / "stray.txt").write_bytes(b"x" * 7)
    return cache


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """沙箱缓存树 + 沙箱空库。

    库必须一起隔离:clear_cache_category 的活动任务护栏会经
    cache_exclusive.cache_usage_by_namespace() 查 `tasks` 表。不指开发机上
    真实的 data/map_downloader.db,这些用例的结果就取决于本机碰巧有没有
    在跑的任务(CI 上则是一条与用例无关的 OperationalError 被吞成放行)。
    """
    from src.core import config
    from src.core.database import init_database

    monkeypatch.setattr(config.Config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config.Config, "CACHE_DIR", tmp_path / "cache")
    init_database()
    return _make_cache(tmp_path)


# ---------- get_cache_stats ----------

def test_get_cache_stats_categorizes(cache):
    """一级目录 = 一个分类;标签把命名空间翻成「样式名 · 指纹」。"""
    stats = get_cache_stats(cache)
    by_key = {c["key"]: c for c in stats["categories"]}
    assert by_key[_NS_ROADMAP]["size_bytes"] == 150
    assert by_key[_NS_ROADMAP]["file_count"] == 2
    assert by_key[_NS_ROADMAP]["label"] == "瓦片缓存（roadmap · 1a2b3c4d）", (
        "缓存管理页上唯一的操作是「删掉哪一类」,直接印 `m-1a2b3c4d` 等于"
        "让用户凭运气删几十 GB"
    )
    assert by_key[_NS_SATELLITE]["size_bytes"] == 200
    assert by_key[_NS_SATELLITE]["label"] == "瓦片缓存（satellite · 89abcdef）"
    # 「高程缓存」而不是「DEM 缓存」：DEM 只保留在数据集全称里（ASTER GDEM v3），
    # 其余一律「高程」—— 由 tests/test_terminology.py 的
    # test_dem_survives_only_in_dataset_names 全局钉住。
    assert by_key["dem"]["label"] == "高程缓存"
    assert by_key["dem"]["size_bytes"] == 1000
    assert by_key["_root"]["file_count"] == 1
    assert by_key["_root"]["label"] == "其他"
    assert stats["total_bytes"] == 150 + 200 + 1000 + 7


def test_get_cache_stats_labels_unknown_style_verbatim(cache):
    """认不出的样式码原样显示 —— 自定义图源不在 STYLE_NAMES 表里。

    显示 `瓦片缓存（q-00000000）` 比显示一个猜出来的名字诚实。
    """
    (cache / "q-00000000" / "5").mkdir(parents=True)
    (cache / "q-00000000" / "5" / "1.png").write_bytes(b"x" * 9)

    by_key = {c["key"]: c for c in get_cache_stats(cache)["categories"]}
    assert by_key["q-00000000"]["label"] == "瓦片缓存（q-00000000）"


def test_get_cache_stats_missing_root(tmp_path):
    stats = get_cache_stats(tmp_path / "nope")
    assert stats == {"categories": [], "total_bytes": 0}


# ---------- clear_cache_category ----------

def test_clear_cache_category_removes_dir(cache):
    result = clear_cache_category(_NS_ROADMAP, cache)
    assert result == {"removed_bytes": 150, "removed_files": 2}
    assert not (cache / _NS_ROADMAP).exists()
    assert (cache / _NS_SATELLITE).exists()  # 其他命名空间不受影响


def test_clear_cache_category_root_only_files(cache):
    result = clear_cache_category("_root", cache)
    assert result == {"removed_bytes": 7, "removed_files": 1}
    assert not (cache / "stray.txt").exists()
    assert (cache / _NS_ROADMAP).exists()  # 子目录不碰


@pytest.mark.parametrize("bad", ["..", "../m", "m/x", "/abs", "", None, "m\\x"])
def test_clear_cache_category_rejects_bad_names(cache, bad):
    with pytest.raises(ValueError):
        clear_cache_category(bad, cache)
    assert (cache / _NS_ROADMAP).exists()  # 什么都没删


def test_clear_cache_category_rejects_missing(cache):
    with pytest.raises(ValueError):
        clear_cache_category("q-11111111", cache)


# ---------- 活动任务护栏（每分类粒度） ----------
#
# 此前 clear_cache_category 是**零**存活性检查的一次 rmtree。用户在下载途中点
# 「清理 satellite 缓存」，正在跑的任务立刻踩空，而且全程无声：
#   · 枚举阶段命中 cache 的瓦片已被移出待下清单并计进 downloaded_tiles，不会重下；
#   · 产物目录靠补拷线程从 cache 复制，源没了只吞成一条 warning；
#   · 完成判定只看 task_tiles 的缺块行，而 cache 命中瓦片从不在那张表里。
# 合起来：任务照报完成、计数满值、产物目录静默缺瓦片。
#
# 路由层那道「整库清理时列出全部未完成任务」的 409 闸（见
# tests/test_fix_cache_chain.py）是同一个危险的**另一种粒度**，两处都要有。
# ---------------------------------------------------------------------------


def _seed_map_task(status, style="satellite"):
    """播一条地图任务并返回 (task_id, 它的缓存命名空间)。"""
    from src.core.database import get_connection
    from src.services.source_registry import snapshot_for_task_row

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tasks (name, status, north, south, east, west, "
            "zoom_min, zoom_max, style, output_format, output_path) "
            "VALUES ('ns-guard', ?, 1, 0, 1, 0, 10, 10, ?, 'tiles_only', '/tmp/x')",
            (status, style),
        )
        task_id = cur.lastrowid
        conn.commit()
        cur.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
        return task_id, snapshot_for_task_row(cur.fetchone()).cache_namespace
    finally:
        conn.close()


def _make_namespace_dir(cache_root, namespace):
    d = cache_root / namespace / "10" / "1"
    d.mkdir(parents=True, exist_ok=True)
    (d / "2.png").write_bytes(b"x" * 42)
    return cache_root / namespace


def test_clear_cache_category_refuses_namespace_used_by_active_task(cache):
    """活动任务引用着的命名空间必须拒绝清理,且**一个字节都不能删**。

    「活动」的判据是 contracts/outcome.ACTIVE_STATE_VALUES,与调度、历史列表
    同一口径 —— 这里绝不手写状态字面量。
    """
    _task_id, namespace = _seed_map_task("running")
    target = _make_namespace_dir(cache, namespace)

    with pytest.raises(ValueError, match="正被运行中的任务使用"):
        clear_cache_category(namespace, cache)

    assert (target / "10" / "1" / "2.png").exists(), "拒绝就必须是原子的:一块都不许删"


@pytest.mark.parametrize("status", ["pending", "running", "paused", "retrying",
                                    "pending_decision"])
def test_clear_cache_category_refuses_for_every_active_state(cache, status):
    """五个活动态一个都不能漏。

    `pending_decision` 尤其要在里面:它是「跑完了但有没交代的缺块,等你决定
    补漏还是接受」,产物目录和缓存引用都还占着,清掉缓存就等于把「补漏」这条
    路悄悄堵死。`retrying` 同理。
    """
    _task_id, namespace = _seed_map_task(status)
    _make_namespace_dir(cache, namespace)

    with pytest.raises(ValueError):
        clear_cache_category(namespace, cache)


@pytest.mark.parametrize("status", ["completed", "completed_with_gaps", "failed"])
def test_clear_cache_category_allows_terminal_tasks(cache, status):
    """终态任务不挡清理 —— 否则用户永远清不掉缓存。"""
    _task_id, namespace = _seed_map_task(status)
    target = _make_namespace_dir(cache, namespace)

    result = clear_cache_category(namespace, cache)

    assert result["removed_files"] == 1
    assert not target.exists()


def test_clear_cache_category_force_overrides_the_active_guard(cache):
    """force=True 仍可强清(用户已被前端二次询问过),与路由层 force 同语义。"""
    _task_id, namespace = _seed_map_task("running")
    target = _make_namespace_dir(cache, namespace)

    result = clear_cache_category(namespace, cache, force=True)

    assert result["removed_files"] == 1
    assert not target.exists()


def test_clear_cache_category_guard_ignores_non_namespace_dirs(cache):
    """`dem` / `_root` 不在命名空间视野里,护栏不该把它们也锁死。

    cache_usage_by_namespace 只枚举 SourceSnapshot.is_namespace 认的目录,
    对 `dem` 查也是白查。DEM 缓存的活动引用由路由层那道整库 409 闸兜着
    (它按四张任务表判)。
    """
    _seed_map_task("running")

    assert clear_cache_category("dem", cache)["removed_files"] == 1
    assert not (cache / "dem").exists()


# ---------- /api/cache/* ----------

def test_api_cache_stats(isolated_app):
    _make_cache(Config.CACHE_DIR.parent)
    client = isolated_app.app.test_client()
    resp = client.get("/api/cache/stats")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    keys = {c["key"] for c in data["categories"]}
    assert {_NS_ROADMAP, _NS_SATELLITE, "dem", "_root"} <= keys
    assert data["total_bytes"] == 150 + 200 + 1000 + 7


def test_api_cache_clear_one(isolated_app):
    cache_dir = _make_cache(Config.CACHE_DIR.parent)
    client = isolated_app.app.test_client()
    resp = client.post("/api/cache/clear", json={"category": _NS_ROADMAP})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["total_removed_bytes"] == 150
    assert not (cache_dir / _NS_ROADMAP).exists()
    assert (cache_dir / "dem").exists()


def test_api_cache_clear_all(isolated_app):
    cache_dir = _make_cache(Config.CACHE_DIR.parent)
    client = isolated_app.app.test_client()
    resp = client.post("/api/cache/clear", json={"category": "__all__"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_removed_bytes"] == 150 + 200 + 1000 + 7
    assert not (cache_dir / _NS_ROADMAP).exists()
    assert not (cache_dir / _NS_SATELLITE).exists()
    assert not (cache_dir / "dem").exists()
    assert not (cache_dir / "stray.txt").exists()


@pytest.mark.parametrize("body", [{}, {"category": ".."}, {"category": "q-1"},
                                 {"category": 123}])
def test_api_cache_clear_rejects_bad_input(isolated_app, body):
    cache_dir = _make_cache(Config.CACHE_DIR.parent)
    client = isolated_app.app.test_client()
    resp = client.post("/api/cache/clear", json=body)
    assert resp.status_code == 400
    assert (cache_dir / _NS_ROADMAP).exists()  # 什么都没删


# ---------- clear_task_exclusive_cache（删除任务时的独占瓦片清理）----------
#
# 这一组钉的是 M5 的修复：遍历方向从「枚举独占矩形里的每个坐标」反转成
# 「走磁盘上真实存在的文件」。代价从 O(区域瓦片数) 变成 O(盘上文件数)，而
# **删掉的文件集合必须一模一样** —— 那才是这一组用例的主要内容。

def _snapshot():
    from src.contracts.source import SourceSnapshot
    return SourceSnapshot(source_id="probe", url_template="https://x/{z}/{x}/{y}.png",
                          style="m", server_list=("https://x",))


def _task_row(task_id, north, south, east, west, zmin, zmax):
    """一行足够 RegionSpec.from_row / snapshot_for_task_row 用的任务行。

    带 `source_snapshot` 是关键：没有它就要现推，会去读配置库。
    """
    return {"id": task_id, "north": north, "south": south, "east": east,
            "west": west, "zoom_min": zmin, "zoom_max": zmax, "style": "m",
            "region_spec": "", "source_snapshot": _snapshot().to_json(),
            "status": "completed"}


def _write_tile(root, zoom, x, y, name=None, size=10):
    d = root / _snapshot().cache_namespace / str(zoom) / str(x)
    d.mkdir(parents=True, exist_ok=True)
    p = d / (name or f"{y}.png")
    p.write_bytes(b"t" * size)
    return p


def _one_exclusive_coord(task_row, other_rows, zoom):
    """从独占矩形里取一个真实坐标 —— 别在用例里手算墨卡托。"""
    from src.services.cache_exclusive import exclusive_tile_rects
    for z, x0, _x1, y0, _y1 in exclusive_tile_rects(task_row, other_rows):
        if z == zoom:
            return x0, y0
    raise AssertionError(f"z{zoom} 上没有独占矩形，用例的区域参数选错了")


def _one_shared_coord(other_row, zoom):
    """邻居任务也覆盖的坐标 —— 它必须活下来。"""
    from src.contracts.region import RegionSpec
    from src.contracts.region_tiles import iter_region_tile_spans
    region = RegionSpec.from_row(other_row)
    y, x0, _x1 = next(iter(iter_region_tile_spans(region, zoom)))
    return x0, y


def test_exclusive_cleanup_deletes_only_unshared_tiles(cache):
    """只删「没有第二个存活任务引用」的瓦片，邻居共用的那块必须留下。"""
    from src.services.cache_exclusive import clear_task_exclusive_cache

    mine = _task_row(1, 30.5, 30.0, 114.5, 114.0, 12, 12)
    neighbour = _task_row(2, 30.3, 30.1, 114.3, 114.1, 12, 12)
    rows = [mine, neighbour]  # 快照含本任务自己那一行（真实调用形态）

    ex_x, ex_y = _one_exclusive_coord(mine, rows, 12)
    sh_x, sh_y = _one_shared_coord(neighbour, 12)
    exclusive = _write_tile(cache, 12, ex_x, ex_y, size=100)
    shared = _write_tile(cache, 12, sh_x, sh_y, size=40)

    result = clear_task_exclusive_cache(mine, rows)

    assert not exclusive.exists()
    assert shared.exists(), "邻居仍然引用这块瓦片,删掉它等于让邻居的缓存凭空失效"
    assert result == {"removed_bytes": 100, "removed_files": 1}


def test_exclusive_cleanup_never_touches_part_files(cache):
    """`.part.<pid>` 是**另一个活进程**正在写的原子临时件,一块都不能碰。

    反转遍历方向之前这条承诺在本路径上是空头支票:老写法拼出来的路径永远是
    `{y}.png`,名字里不可能出现 `.part.`,那个判据是死代码。现在直接读目录,
    临时件会真的出现在候选里 —— 删掉它,人家的 os.replace 就炸在半路。
    """
    from src.services.cache_exclusive import clear_task_exclusive_cache

    mine = _task_row(1, 30.2, 30.0, 114.2, 114.0, 12, 12)
    rows = [mine]
    x, y = _one_exclusive_coord(mine, rows, 12)
    part = _write_tile(cache, 12, x, y, name=f"{y}.png.part.4242.7", size=33)
    finished = _write_tile(cache, 12, x, y, size=11)

    result = clear_task_exclusive_cache(mine, rows)

    assert not finished.exists()
    assert part.exists(), "别的活进程正在写这个文件"
    assert result == {"removed_bytes": 11, "removed_files": 1}


def test_exclusive_cleanup_ignores_foreign_names_and_namespaces(cache):
    """只认 `{整数}.png`,其余一律不碰;别的命名空间目录更不参与。

    这是等价性的另一半:老写法只可能去 stat `{y}.png`,所以反转之后也只能
    认这一种文件名 —— 否则「按磁盘遍历」会顺手删掉老写法碰都碰不到的东西。
    """
    from src.services.cache_exclusive import clear_task_exclusive_cache

    mine = _task_row(1, 30.2, 30.0, 114.2, 114.0, 12, 12)
    rows = [mine]
    x, y = _one_exclusive_coord(mine, rows, 12)
    tile = _write_tile(cache, 12, x, y, size=7)
    stray = _write_tile(cache, 12, x, y, name="readme.txt", size=5)
    not_png = _write_tile(cache, 12, x, y, name=f"{y}.jpg", size=5)
    weird_dir = cache / _snapshot().cache_namespace / "12" / "notanint"
    weird_dir.mkdir(parents=True)
    (weird_dir / f"{y}.png").write_bytes(b"z" * 5)

    result = clear_task_exclusive_cache(mine, rows)

    assert not tile.exists()
    assert stray.exists() and not_png.exists()
    assert (weird_dir / f"{y}.png").exists()
    assert (cache / _NS_ROADMAP / "10" / "1" / "2.png").exists(), "别的命名空间不参与"
    assert result == {"removed_bytes": 7, "removed_files": 1}


def test_exclusive_cleanup_prunes_emptied_dirs_but_keeps_namespace(cache):
    """删空的 `x/` 与 `z/` 目录要收掉,命名空间目录本身留着。

    命名空间目录消失会让缓存管理页上那一行凭空不见,用户以为缓存被整个清了。
    """
    from src.services.cache_exclusive import clear_task_exclusive_cache

    mine = _task_row(1, 30.2, 30.0, 114.2, 114.0, 12, 12)
    rows = [mine]
    x, y = _one_exclusive_coord(mine, rows, 12)
    _write_tile(cache, 12, x, y)
    ns_dir = cache / _snapshot().cache_namespace

    clear_task_exclusive_cache(mine, rows)

    assert not (ns_dir / "12" / str(x)).exists()
    assert not (ns_dir / "12").exists()
    assert ns_dir.exists()


def test_exclusive_cleanup_cost_tracks_files_on_disk_not_region_size(cache, monkeypatch):
    """代价必须是 O(盘上文件数),不是 O(区域瓦片数)。

    这条不是「优化得不错」而是**正确性级别**的要求:这段代码跑在 Flask 的
    DELETE 处理器里(task_deletion 的快路径),老写法在这个区域上实测 37.1 秒
    (2,473,233 块坐标 × 15.25 μs),用户对着转圈等到超时,然后再点一次删除。

    判据取「矩形索引被问了几次」而不是墙钟时间:它精确等于「磁盘上有多少个
    候选文件」,与机器快慢无关。按坐标枚举的写法在这里会问两百多万次。
    """
    import time

    from src.services import cache_exclusive

    mine = _task_row(1, 31.5, 30.0, 116.0, 114.0, 0, 18)
    rows = [mine]
    x, y = _one_exclusive_coord(mine, rows, 18)
    tile = _write_tile(cache, 18, x, y, size=64)

    probes = []
    real = cache_exclusive._ZoomRectIndex.contains
    monkeypatch.setattr(cache_exclusive._ZoomRectIndex, "contains",
                        lambda self, px, py: (probes.append(1), real(self, px, py))[1])

    started = time.perf_counter()
    result = cache_exclusive.clear_task_exclusive_cache(mine, rows)
    elapsed = time.perf_counter() - started

    assert result == {"removed_bytes": 64, "removed_files": 1}
    assert not tile.exists()
    assert len(probes) == 1, (
        f"盘上只有 1 个候选文件,却问了 {len(probes)} 次 —— 遍历又变回按坐标枚举了")
    assert elapsed < 2.0, f"{elapsed:.1f}s:这段跑在 DELETE 请求里,用户在等它"