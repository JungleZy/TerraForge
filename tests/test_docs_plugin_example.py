"""`docs/examples/plugin-hello/` 这个示例插件必须一直能跑。

## 为什么值得一条测试

`docs/guides/PLUGINS.md` 是第三方插件作者的唯一事实源，而那份文档的可信度
全押在这个示例上——它是文档里唯一「拷进去就能跑」的完整承诺。示例只要腐烂
一次，读文档的人第一步就卡住，而且**没有任何现有测试会红**：它不在 `src/`
下、不被任何生产代码 import、`registry._BUILTIN` 也不含它。

腐烂路径是具体的，不是假想：`TaskContext` 的公开面、`PluginDefinition` 的
字段、`ParamSpec` 的语义、`ArtifactKind` 的取值、`iter_region_tile_spans`
的签名——示例全都在用。本分支自己就改过其中四样（`config_schema` 从
pipeline 提到 `PluginDefinition`、`close()` 语义、产物归属校验、NETWORK 配额
要 manifest 声明），任何一次同类改动都可能悄悄废掉示例。

## 钉的是什么

**可观察契约，不是实现细节。** 这里断言的每一条都是文档里对作者的承诺：
装进去能被发现、启用后能建任务、跑完落终态、产物真在盘上且登记进 `artifacts`、
缺块语义按 §13-3 走。示例内部怎么画 PNG、循环怎么写，一概不管——那些改了
不该让这条红。

反过来，这条**不是**给示例插件做单元测试：它跑的是宿主的真实装载路径
（`load_all` → `set_enabled` → `PluginTaskManager.start_task`），所以宿主
契约变了它会红，而这正是要的。
"""

import os
import shutil
import sqlite3
import sys
import time

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.plugins import registry  # noqa: E402

#: 示例插件源目录与它在 manifest 里声明的 id。
EXAMPLE_DIR = os.path.join(PROJECT_ROOT, "docs", "examples", "plugin-hello")
EXAMPLE_ID = "hello"

#: 示例声明的参数键全集（`params_schema()`）。文档的「plugin.toml 全字段表」
#: 与四类扩展点那几节都按这份写，改了名字文档就对不上了。
EXAMPLE_PARAM_KEYS = {"zoom", "color", "demo_gap", "note"}


@pytest.fixture(autouse=True)
def restore_registry():
    """注册表是进程全局单例，启用状态不许漏给别的测试文件。"""
    yield
    registry.reset_for_tests()


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """把示例插件按文档说的方式装好：一张真库 + `<BASE_DIR>/plugins/hello`。

    夹具形状照抄 `tests/test_plugin_acceptance.py:44-65`（conftest 没有 `db`）。
    `BASE_DIR` 指到 tmp_path 顺带把 `_plugins_root()` 指空，仓库根真有
    `plugins/` 时结果也不会跟着变。
    """
    from src.core import config as config_mod

    path = tmp_path / "data" / "map_downloader.db"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(config_mod.Config, "DATABASE_PATH", path)
    monkeypatch.setattr(config_mod.Config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config_mod.Config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config_mod.Config, "CACHE_DIR", tmp_path / "cache")

    from src.core.database import init_database

    init_database()

    # 这一步就是文档「三分钟跑通」的第一句：把目录拷进 plugins/。
    shutil.copytree(EXAMPLE_DIR, tmp_path / "plugins" / EXAMPLE_ID)
    registry.reset_for_tests()
    registry.load_all()
    return tmp_path


def _wait_terminal(mgr, task_id, timeout=30.0):
    """轮询到非活动态。超时返回最后一次读到的行，让断言给出真实状态。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = mgr.get_task(task_id)
        if row and row["status"] not in ("pending", "running"):
            return row
        time.sleep(0.05)
    return mgr.get_task(task_id)


def test_example_plugin_loads_without_error(installed):
    """文档第一步：拷进去就该被发现，且 `load_error` 为空。

    `load_error` 非空时插件在面板上照样看得见（隔离铁律的另一半），所以这里
    必须断言它为空串而不是只断言「记录存在」——把失败的错误原文带进断言消息，
    腐烂时一眼能看出是签名闸、路径闸还是 API 版本闸拦的。
    """
    rec = registry.get_record(EXAMPLE_ID)
    assert rec is not None, "示例插件没被发现：检查 plugin.toml 的 id 与目录布局"
    assert rec.load_error == "", f"示例插件加载失败：{rec.load_error}"
    assert rec.origin == "external"
    assert rec.definition is not None


def test_example_plugin_is_disabled_by_default(installed):
    """§13-4 契约第 1 条：缺省关闭。文档据此告诉作者「装完还要去点启用」。"""
    rec = registry.get_record(EXAMPLE_ID)
    assert rec.enabled is False
    assert registry.get_pipeline(EXAMPLE_ID) is None, "未启用就不该暴露管线"


def test_example_plugin_param_schema_matches_docs(installed):
    """参数键与文档写的一致，且 enum 的 choices 真能用。

    只钉键名与 enum 取值这两样——它们是文档里逐字出现、作者会照抄的部分。
    label 文案、默认值调整不该让这条红。
    """
    registry.set_enabled(EXAMPLE_ID, True)
    schema = registry.get_pipeline(EXAMPLE_ID).params_schema()
    assert set(schema.keys()) == EXAMPLE_PARAM_KEYS

    color = next(s for s in schema.specs if s.key == "color")
    assert color.type == "enum" and color.default in color.choices


def test_example_plugin_rejects_unknown_param(installed):
    """未知参数键必须报错而不是静默吞掉。

    这条钉的是文档「schema 校验真的在工作」那句承诺。静默接受的后果是作者
    拼错键名后到运行期才发现——与 `PUT /api/config` 的 known_keys 闸门同一个
    理由。
    """
    from src.plugins.task_manager import PluginTaskManager

    registry.set_enabled(EXAMPLE_ID, True)
    mgr = PluginTaskManager(socketio=None)
    with pytest.raises(ValueError, match="unknown param"):
        mgr.create_task(EXAMPLE_ID, {
            "name": "bad", "bbox": [40.0, 39.0, 117.0, 116.0],
            "greeting": "hi",
        })


def test_example_plugin_runs_end_to_end(installed):
    """文档承诺的完整闭环：建任务 → 跑完 → 产物落盘 → 登记进 artifacts。

    断言覆盖四件对作者有意义的事：
      1. 终态是 `completed_with_gaps`——示例故意留一块 `NO_DATA`，而 §13-3 说
         「已解释的缺块」不需要问用户，直接落这个终态（不是 pending_decision）；
      2. `gap_tiles` 跟 `plugin_task_tiles` 的行数走，缺块记账真的生效；
      3. 产物文件真在盘上（不是只写了一行登记）；
      4. `artifacts` 表里那一行的 `pipeline` 是 `'plugin'`、`has_gaps` 为真——
         §13-3 要的「成果与历史永久带缺块标记」跟着产物走。
    """
    from src.plugins.task_manager import PluginTaskManager
    from src.services import artifact_store

    tmp_path = installed
    registry.set_enabled(EXAMPLE_ID, True)
    mgr = PluginTaskManager(socketio=None)
    task_id = mgr.create_task(EXAMPLE_ID, {
        "name": "示例核验",
        "bbox": [40.0, 39.0, 117.0, 116.0],
        "output_path": str(tmp_path / "downloads"),
        "zoom": 10,
        "color": "green",
        "demo_gap": True,
    })
    mgr.start_task(task_id)
    row = _wait_terminal(mgr, task_id)

    assert row["status"] == "completed_with_gaps", (
        f"示例没跑到预期终态：status={row['status']} "
        f"error={row['error_message']!r}")
    assert row["downloaded_items"] == row["total_items"] > 0
    assert row["gap_tiles"] == 1, "示例声明 demo_gap=True 时应恰好留一块 NO_DATA"
    assert row["failed_items"] == 0

    artifacts = artifact_store.list_artifacts("plugin", task_id)
    assert len(artifacts) == 1, "示例应登记恰好一件产物（瓦片目录）"
    art = artifacts[0]
    assert art.pipeline == "plugin"
    assert art.kind.value == "xyz_dir"
    assert art.fmt == "png"
    assert art.has_gaps is True, "带缺块的产物必须永久带标记（§13-3）"
    assert os.path.isdir(art.path), f"登记了却不在盘上：{art.path}"

    # 缺块记账真的落库了，不是只改了任务行上的计数。
    conn = sqlite3.connect(tmp_path / "data" / "map_downloader.db")
    try:
        rows = conn.execute(
            "SELECT status FROM plugin_task_tiles WHERE task_id = ?",
            (task_id,)).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == ["no_data"]


def test_example_plugin_writes_task_log(installed):
    """每任务日志真的接上了（§4.5：任何终态都能从日志解释原因）。

    示例用了 `ctx.log` 与 `ctx.log_event`，文档据此告诉作者「排错去看
    `logs/tasks/plugin_<id>.log`」——这条路径不通那句话就是假的。
    """
    from src.plugins.task_manager import PluginTaskManager

    tmp_path = installed
    registry.set_enabled(EXAMPLE_ID, True)
    mgr = PluginTaskManager(socketio=None)
    task_id = mgr.create_task(EXAMPLE_ID, {
        "name": "日志核验",
        "bbox": [40.0, 39.8, 116.2, 116.0],
        "output_path": str(tmp_path / "downloads"),
        "zoom": 8,
    })
    mgr.start_task(task_id)
    _wait_terminal(mgr, task_id)

    log_path = tmp_path / "logs" / "tasks" / f"plugin_{task_id}.log"
    assert log_path.exists(), "插件任务没有每任务日志"
    text = log_path.read_text(encoding="utf-8")
    assert "hello_start" in text, "ctx.log_event 的结构化事件没落进任务日志"
