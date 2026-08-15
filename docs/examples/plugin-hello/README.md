# plugin-hello —— 可直接跑的 TerraForge 示例插件

**一句话：把这个目录整个拷进 `plugins/hello/`，重启程序，在插件面板启用它，就能建出一个离线跑完、产出一小片占位瓦片的任务。**

完整的插件开发指南在 [`docs/guides/PLUGINS.md`](../../guides/PLUGINS.md)。这里只讲怎么把这个示例跑起来、跑起来该看到什么。

## 装

插件目录是 `<BASE_DIR>/plugins/<id>/`：

- 源码运行：仓库根的 `plugins/`（`src/core/config.py:62` 的 `BASE_DIR` = 仓库根）
- 打包运行：exe 旁的 `plugins/`（`src/core/config.py:57`）

```bash
mkdir -p plugins
cp -r docs/examples/plugin-hello plugins/hello
```

目录名叫什么都行（发现靠的是目录里那份 `plugin.toml`），但和 `id` 保持一致最省事。装完重启程序——**发现只在启动时做一次**（`registry.load_all()`）。

## 启用

插件缺省全部关闭。面板：左侧工具条 → 插件 → 找到「Hello 示例插件」→ 启用。

REST 等价物：

```bash
curl -s localhost:5000/api/plugins | python -m json.tool     # 应看到 id=hello、load_error 为空
curl -s -X POST localhost:5000/api/plugins/hello/enable
```

## 建任务

面板上开启后可以直接在插件面板里建任务（表单按 `params_schema()` 现渲染）。REST 等价物：

```bash
curl -s -X POST localhost:5000/api/plugins/hello/tasks \
  -H 'Content-Type: application/json' \
  -d '{"name":"hello 示例任务",
       "bbox":[39.95, 39.90, 116.45, 116.38],
       "zoom":10, "color":"blue", "demo_gap":true, "note":"试一下"}'
# → {"success": true, "task_id": 1}
curl -s -X POST localhost:5000/api/plugins/tasks/1/start
```

`bbox` 的序是 `[north, south, east, west]`（宿主解释），其余四个键是本插件自己声明的参数。加 `"auto_start": true` 可以建完就跑。

## 预期看到什么

用上面那组参数在本机实测（隔离的 `BASE_DIR`，`uv run`）：

| 观察点 | 实测值 |
| --- | --- |
| 任务终态 | `completed_with_gaps` |
| 进度 | `downloaded_items=2 / total_items=2` |
| 缺块 | `gap_tiles=1`、`failed_items=0`（`no_data` 是**已解释**的缺块，不算失败） |
| 产物目录 | `downloads/plugins/hello/plugin_task_<id>/` |
| 落盘 | `tiles/10/843/388.png`、`summary.json` |
| `artifacts` 表 | 一行：`pipeline=plugin`、`kind=xyz_dir`、`format=png`、`tile_count=1`、`minzoom=maxzoom=10`、`has_gaps=1` |
| 任务日志 | `logs/tasks/plugin_<id>.log`，含 `EVENT hello_start zoom=10 tiles=2 color=blue demo_gap=True` |

把 `demo_gap` 设成 `false` 再跑一次，终态就是干净的 `completed`、`gap_tiles=0`。

## 这个示例演示了什么

- **声明式参数**：`int`（层级，带 min/max）、`enum`（颜色，带 choices）、`bool`、可选 `str`。表单和后端校验共用这一份 schema。
- **进度**：`ctx.progress(done, total, phase)`。广播由宿主按 2 Hz 节流。
- **日志**：`ctx.log()` 写人话，`ctx.log_event()` 写结构化事件，都进 `logs/tasks/plugin_<id>.log`。
- **缺块记账**：`ctx.record_tile_outcome()`。示例故意留一块 `NO_DATA`，让你看到「已解释的缺块 → `completed_with_gaps`」这条路。
- **产物登记**：`ctx.register_artifact()`，路径必须落在 `ctx.output_dir` 内。
- **返回值**：`PluginOutcome`。返回别的东西（包括忘了 `return`）任务一律判 `failed`。

不演示的：联网（示例刻意全程离线，也因此 `plugin.toml` 里没有 `permissions = ["network"]`）、凭据、UI 资产、导出器与钩子。这些在 `docs/guides/PLUGINS.md` 里各有一节。

## 卸载

把 `plugins/hello/` 删掉再重启。**它在数据库里的开关与配置会被一并清掉**（`registry._prune_stale_rows`），重新装回来是干净的初始状态。已经跑完的任务与产物不受影响。
