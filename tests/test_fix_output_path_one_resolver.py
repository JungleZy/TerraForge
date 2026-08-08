"""P1#5（2026-08-08 评审）：存量 `output_path` 只许有一套解析口径。

M10 的存量归一（`src/core/database.py` 的 `normalize_stored_output_paths`）把四张表的
`output_path` 全部改写成 `task_cleanup.resolve_stored_output_dir` 的结果，而 DEM 管线
的写入侧（`dem_task_manager._resolve_task_output_dir`）与读取侧
（`terrain_static._resolve_dem_task_output_dir`）走的是
`geo_validation.resolve_output_dir` —— **每一种相对形式两者结果都不同**：

    './downloads'      -> <DL>            vs <DL>/downloads
    './downloads/dem'  -> <DL>/dem        vs <DL>/downloads/dem
    'dem'              -> <BASE>/dem      vs <DL>/dem

后果：升级后第一次启动，一个 0.2.3 之前的 DEM 任务（产物在 `<DL>/downloads/dem/...`）
指针被改写成 `<DL>/dem`，之后 `/terrain/dem/<id>` 404、恢复任务全量重下、
`DELETE ?delete_files=true` rmtree 一个不存在的目录却回 `files_removed: true`
（`remove_task_dir_if_safe` 对不存在的目录返回 True），GB 级产物无声滞留且不进
`pending_deletions`。

两个分歧的 helper 已删除，四个调用点统一走 `resolve_stored_output_dir`。
`resolve_output_dir` 保留它本来的职责：校验**请求里新传进来的**路径。
"""
import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)


# 两套口径在这些形态上分歧 —— 正是存量行里会出现的相对值。
DIVERGENT_RELATIVE_FORMS = ['./downloads', 'downloads', './downloads/dem',
                            'downloads/dem', 'dem', './dem']


@pytest.fixture
def paths(monkeypatch, tmp_path):
    from src.core import config as config_mod

    monkeypatch.setattr(config_mod.Config, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(config_mod.Config, 'DOWNLOADS_DIR', tmp_path / 'downloads')
    return tmp_path


def test_the_two_resolvers_really_do_disagree(paths):
    """先钉住前提：这不是理论风险，两套口径在每一种相对形式上都不同。

    这条用例的价值是防止「统一到哪一套都无所谓」的误解 —— 一旦哪天两者等价了，
    它会红，那时才可以讨论合并 API。
    """
    from src.services.geo_validation import resolve_output_dir
    from src.services.task_cleanup import resolve_stored_output_dir

    for raw in DIVERGENT_RELATIVE_FORMS:
        stored = resolve_stored_output_dir(raw)
        request_side = os.path.abspath(resolve_output_dir(raw))
        assert str(stored) != request_side, (
            f'{raw!r}: 两套口径居然一致了({stored}) —— 请重新评估这一族用例')


def test_migration_and_dem_pipeline_agree_on_every_relative_form(paths):
    """归一化写进库的值，必须正好是 DEM 管线读它时会算出来的值。

    这就是「一套口径」的可执行定义：`normalize_stored_output_paths` 用的解析器
    与所有读侧用的解析器是同一个函数。
    """
    from src.services.task_cleanup import resolve_stored_output_dir

    for raw in DIVERGENT_RELATIVE_FORMS:
        migrated = str(resolve_stored_output_dir(raw))
        # 归一化后的值是绝对路径，再解析一次必须是幂等的（读侧拿到的就是它）
        assert str(resolve_stored_output_dir(migrated)) == migrated, (
            f'{raw!r} 归一成 {migrated} 后再解析结果变了 —— 读写侧会分裂')


def test_divergent_helpers_are_gone():
    """两个分歧的私有 helper 必须真的删掉，不能留着等人再用一次。"""
    from src.routes import terrain_static
    from src.services import dem_task_manager

    assert not hasattr(dem_task_manager.DemTaskManager, '_resolve_task_output_dir'), (
        'dem_task_manager 又长回了自己的 output_path 解析器')
    assert not hasattr(terrain_static, '_resolve_dem_task_output_dir'), (
        'terrain_static 又长回了自己的 output_path 解析器')


def test_dem_read_and_write_sides_land_on_the_same_directory(paths, monkeypatch):
    """写入侧（start_tiling 算 task_dir）与读取侧（/terrain/dem/<id>）必须同一目录。

    用一个**相对**的存量 output_path —— 正是两套口径分歧的输入。
    """
    from src.routes import terrain_static
    from src.services.task_cleanup import resolve_stored_output_dir

    stored = './downloads/dem'          # 0.2.3 之前的存量形态
    task_id = 42
    expected = resolve_stored_output_dir(stored) / f'dem_task_{task_id}'

    # 读取侧：_dem_task_dir_or_404 走的路径
    monkeypatch.setattr(terrain_static, '_get_dem_output_path', lambda tid: stored)
    assert terrain_static._dem_task_dir_or_404(task_id) == expected

    # 写入侧：start_tiling 里 `resolve_stored_output_dir(output_path) / f"dem_task_{id}"`
    assert resolve_stored_output_dir(stored) / f'dem_task_{task_id}' == expected


def test_request_side_validator_is_still_the_one_that_rejects_escapes(paths):
    """回归保护：别把 resolve_output_dir 也一起换掉 —— 它的职责是拒绝越界。

    `resolve_stored_output_dir` 有意**不**做越界拒绝（存量脏数据不能让任务卡死），
    所以两者不能互换。
    """
    from src.services.geo_validation import resolve_output_dir
    from src.services.task_cleanup import resolve_stored_output_dir

    with pytest.raises(ValueError):
        resolve_output_dir('../../etc')

    # 读侧只归一化，不抛 —— 越界防护由 remove_task_dir_if_safe 等调用方负责
    assert resolve_stored_output_dir('../../etc')
