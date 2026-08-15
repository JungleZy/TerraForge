# TerraForge 插件开发指南

**先说结论：一个插件就是一个目录，里面一份 `plugin.toml` 和一份 `plugin.py`；`plugin.py` 里的 `register()` 返回一个 `PluginDefinition`，宿主按你声明的能力把它接到四个入口上（数据源 / 下载管线 / 导出器 / 任务钩子）。没有沙箱、没有打包步骤、不需要改宿主一行代码。**

这份文档是插件作者的事实源。每条断言都对着 `src/plugins/` 的实现核过；示例代码都真跑过。跟规格文档（`docs/superpowers/specs/2026-08-12-plugin-system-design.md`）冲突时，以本文与代码为准。

---

## 1. 三分钟跑通第一个插件

```bash
# ① 装：把示例整个拷进插件目录（源码运行时就是仓库根的 plugins/）
mkdir -p plugins && cp -r docs/examples/plugin-hello plugins/hello

# ② 重启程序（发现只在启动时做一次）
uv run python app.py

# ③ 启用（面板：左侧工具条 → 插件 → 启用；或走 REST）
curl -s -X POST localhost:5000/api/plugins/hello/enable

# ④ 建任务并启动
curl -s -X POST localhost:5000/api/plugins/hello/tasks \
  -H 'Content-Type: application/json' \
  -d '{"name":"hello","bbox":[39.95,39.90,116.45,116.38],
       "zoom":10,"color":"blue","demo_gap":true}'
curl -s -X POST localhost:5000/api/plugins/tasks/1/start
```

跑完应看到：任务终态 `completed_with_gaps`、`gap_tiles=1`、产物目录 `downloads/plugins/hello/plugin_task_1/` 下有 `tiles/10/843/388.png` 与 `summary.json`、`artifacts` 表多一行 `pipeline=plugin, kind=xyz_dir`。

示例源码与逐行注释在 [`docs/examples/plugin-hello/`](../examples/plugin-hello/)。把它拷成自己的目录、改 `id`、改 `run()`，就是你的插件了。

**开始写之前必须知道的三件事**：

1. **没有沙箱。** 插件在宿主进程里执行任意代码——与用户自己 `python xxx.py` 等价。`permissions` 不是权限沙箱（见 §4）。
2. **发现即加载。** 只要目录里有 `plugin.toml`，启动时就会 import 你的 `plugin.py` 并调 `register()`，**哪怕插件是禁用的**。启用只控制「能力是否对外暴露」。所以模块级别不要做慢动作或网络请求：它直接拖住整个程序的启动。
3. **插件缺省关闭。** 装上不等于启用，用户得去面板打开。

---

## 2. 目录布局与安装位置

```
<BASE_DIR>/plugins/
└── hello/                  ← 目录名随意，发现靠的是里面的 plugin.toml
    ├── plugin.toml         必需。清单
    ├── plugin.py           必需。entry，缺省就叫这个名字
    ├── vendor/             可选。第三方依赖，加载时进 sys.path 末尾
    └── panel.js            可选。UI 资产，必须在 ui.assets 里声明才对外可读
```

`BASE_DIR` 的取值只有两种（`src/core/config.py:56-62`）：

| 运行方式 | BASE_DIR | 插件目录 |
| --- | --- | --- |
| 源码（`uv run python app.py`） | 仓库根 | `<仓库根>/plugins/` |
| 打包产物 | exe 所在目录 | `<exe 目录>/plugins/` |

扫描规则（`registry._external_dirs`）：`plugins/` 下**直接子目录**里有 `plugin.toml` 的才算插件，按目录名排序加载。嵌套两层不会被发现。目录整个读不动（权限、盘掉线）时只记一条 warning，宿主照常启动。

产物目录不是你选的：宿主给每个任务算一个 `<output_path>/plugin_task_<id>/`，缺省 `output_path` 是 `downloads/plugins/<plugin_id>`（`task_manager.create_task` + `_task_output_dir`）。你在 `run()` 里拿到的 `ctx.output_dir` 就是它，已经 `mkdir -p` 过。

---

## 3. 加载顺序与四道闸

`registry.load_all()` 在启动时跑一次，顺序是：内置插件 → 外部插件（目录名排序）。每个插件依次过：

1. **清单解析**（`manifest.load_manifest_toml`）：TOML 语法、id 形状、白名单、路径字段。
2. **id 冲突**：先到者赢。撞了内置插件 id 的外部插件不加载，错误登记在「目录名」这把 key 上。
3. **`api_version` major 闸** → **`requires_abi` 闸**（`registry._check_api_version` / `_check_abi`）。
4. **entry 落地闸**（`registry._resolve_entry`）：`resolve()` 之后必须仍在插件目录内，且必须是个已存在的文件。
5. **import + `register()`** → 返回值必须是 `PluginDefinition`。
6. **签名闸**（`registry._check_definition`）：对 definition 里每个扩展点对象，逐方法比**位置参数个数**，不符拒载。

任何一步失败都只落成这个插件自己的 `load_error`：宿主不受影响，其他插件不受影响，插件仍然出现在面板列表里（带红色原因）。连插件模块级的 `sys.exit()` 都被拦住了（`_load_one` 同时捕获 `SystemExit`）。

---

## 4. plugin.toml 全字段表

约束逐字来自 `src/plugins/manifest.py`。错误消息是实测抄录的。

| 字段 | 必填 | 类型/约束 | 写错会得到什么 |
| --- | --- | --- | --- |
| `id` | ✅ | 正则 `^[a-z][a-z0-9_\-]{0,63}$`（小写字母开头，字母/数字/`_`/`-`，最长 64） | `ManifestError: 非法插件 id：'bad-ID'（小写字母/数字/中划线/下划线，字母开头）` |
| `name` | ✅ | 非空字符串，面板上的显示名 | `ManifestError: 必填字段为空：['name']` |
| `version` | ✅ | 非空字符串，宿主不解析语义，只存库与展示 | 同上 |
| `api_version` | ✅ | 非空。**只比 major**：`"1"` / `"1.0"` / `"1.7"` 都能载 | `ManifestError: api_version '2.0' 与宿主 1.x 不兼容` |
| `capabilities` | ❌ | 字符串数组，白名单 `sources` / `pipeline` / `exporter` / `hook` | `ManifestError: 未知 capabilities：['pipelines']` |
| `permissions` | ❌ | 字符串数组，白名单 `network` / `filesystem` / `subprocess` | `ManifestError: 未知 permissions：['gpu']` |
| `entry` | ❌ | 缺省 `plugin.py`。只许插件目录内的相对路径 | `ManifestError: entry 只许插件目录内的相对路径（每段限字母/数字/点/下划线/中划线，不许空段、纯点号段、空白、盘符或协议前缀）：'../evil.py'`；文件不存在则 `ManifestError: entry 文件不存在：/…/main.py` |
| `requires_abi` | ❌ | 形如 `cp312-linux-x86_64`（`cp<major><minor>-<sys.platform>-<machine>`）。空 = 纯 Python，不比 | `ManifestError: ABI 不匹配：插件需要 cp311-linux-x86_64，宿主是 cp312-linux-x86_64` |
| `description` | ❌ | 字符串，面板上的一句话说明 | —— |
| `[ui] assets` | ❌ | 字符串数组，每项同 `entry` 的路径约束 | 写成字符串：`ManifestError: ui.assets 必须是字符串数组，实际是 str`（字符串会被逐字符拆成资产，所以必须拦） |

一份完整清单：

```toml
id = "hello"
name = "Hello 示例插件"
version = "1.0.0"
api_version = "1"
capabilities = ["pipeline"]
permissions = ["filesystem"]
entry = "plugin.py"
description = "最简管线示例"

[ui]
assets = ["panel.js"]
```

**关于 `capabilities`：它是声明，不是校验依据。** 宿主只校验拼写，不检查「声明了 `pipeline` 却没在 `register()` 里给 pipeline」。它的实际消费者只有 `GET /api/plugins`（面板展示，`plugins_api.list_plugins`）。请如实填，用户按它判断这个插件是干什么的。

**关于 `permissions`：只有 `network` 有真实作用。** 声明了它，宿主才会在跑任务时向调度器申请 NETWORK 配额；不声明就是 0（详见 §7.4）。`filesystem` 与 `subprocess` 今天纯属声明——**没有任何机制阻止一个没声明 `filesystem` 的插件写文件**。没有沙箱，别把它读成权限模型。

---

## 5. `register()` 与 `PluginDefinition`

`plugin.py` 必须有一个模块级 `register()`，返回 `PluginDefinition`（`src/plugins/protocols.py`）：

```python
from src.plugins.protocols import PluginDefinition

def register() -> PluginDefinition:
    return PluginDefinition(pipeline=MyPipeline())
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `sources` | `Tuple[SourceDescriptor, ...]` | 纯数据的瓦片源声明，零代码 |
| `source_provider` | `SourceProvider \| None` | 需要代码算快照/鉴权的源 |
| `pipeline` | `PipelinePlugin \| None` | 一个插件**最多一条**管线 |
| `exporters` | `Tuple[Exporter, ...]` | 产物格式转换器 |
| `hooks` | `Tuple[TaskHook, ...]` | 任务事件旁路 |
| `config_schema` | `ParamSchema \| None` | **插件配置**（不是任务参数）的 schema，`set_config` 用它校验 |

全部可选：纯数据源插件就只有 `sources` 一个字段。

`register()` 缺席或不可调用 → `ManifestError: plugin.py 缺少 register() 函数`；返回别的东西 → `ManifestError: register() 必须返回 PluginDefinition`。

---

## 6. 声明式参数（`ParamSpec` / `ParamSchema`）

任务表单与后端校验共用同一份 schema，**后端权威**（`src/plugins/params.py`）。

```python
ParamSpec(key='zoom', type='int', label='层级', default=4, min=0, max=21)
```

| 成员 | 说明 |
| --- | --- |
| `key` | 参数名，进 `ctx.params` |
| `type` | `region` / `zoom_range` / `path` / `int` / `float` / `str` / `bool` / `enum` / `credential` |
| `label` | 显示文案。**纯字符串**——插件 i18n 还没接（§13） |
| `default` | 缺省值 |
| `required` | 见下面的坑 |
| `min` / `max` | 只对 `int` / `float` 生效 |
| `choices` | 只对 `enum` 生效，元组 |
| `depends_on` | dict，今天宿主**不消费**它 |

校验语义（`validate_params`）：

- **未知键报错**：`参数非法：nope=unknown param`。别指望宿主静默忽略多余的键。
- **JSON `null` 等同未提供**：有 default 就回填 default，没有且 `required` 才报 `required`。
- **`required` 与 `default` 是死组合**：判定是 `if spec.required and spec.default is None`——**给了 default，`required` 永远不触发**。想要必填就别给 default。
- `int` / `float` 拒 bool（`True` 不是 1），拒 NaN / inf，越界即错。
- `bool` 只收 `True/False/'true'/'false'/1/0`，其余一律 `invalid bool`。
- `enum` 值必须在 `choices` 里。
- `str` / `path` / `credential` / `region` / `zoom_range` 只做 str 化与非空检查；结构化校验（如果需要）你自己做。

**宿主自己解释的键**（`task_manager._HOST_PARAM_KEYS`）：`name`、`bbox`、`output_path`、`zoom_min`、`zoom_max`、`source_id`、`_gap_accepted`。这些不进你的 schema 闸门；`bbox` 是 `[north, south, east, west]`，必填，宿主据它造 `RegionSpec`。你的 schema 声明了同名键时，那个键归你（就得由你校验）。

**面板怎么渲染**（`static/js/plugins.js`）：`bool` → 复选框，`enum` → 下拉，`int`/`float` → number 输入框（带 min/max/step），**其余全是普通文本框**——包括 `region`、`zoom_range`、`path`。没有地图框选、没有文件选择器（§13）。

`ParamSpec` **不可哈希**（`depends_on` 是 dict，`frozen` 生成的 `__hash__` 一调就 `TypeError`）：别 `set(specs)`、别拿它当 dict key。

---

## 7. 四类扩展点

### 7.1 数据源（`sources` / `source_provider`）

**什么时候用**：你想给下载弹窗的数据源下拉里加一个瓦片源。URL 是固定模板的，用 `SourceDescriptor`（零代码）；需要按配置算模板、要先鉴权换 token 的，实现 `SourceProvider`。

```python
from src.plugins.protocols import (ParamSchema, ParamSpec, PluginDefinition,
                                   SourceDescriptor)

def register() -> PluginDefinition:
    return PluginDefinition(
        sources=(SourceDescriptor(
            source_id='demo',                       # 插件内唯一
            name='示例影像',
            url_template='https://example.com/{z}/{x}/{y}.png?key={credential}',
            max_zoom=18,
            attribution='© Example',
            usage_policy='每日 10 万次，禁止转售',    # 会随产物走，如实写
            subdomains=('a', 'b', 'c'),             # 模板里用 {s}
            credential_key='token',                 # 对应 config_json 里的键名
        ),),
        config_schema=ParamSchema((
            ParamSpec(key='token', type='credential', label='API Key'),
        )),
    )
```

`url_template` 必须含 `{z}` `{x}` `{y}`；可选 `{s}`（子域轮换）与 `{credential}`（凭据，见 §9）。

启用后 `registry.build_source_snapshot` 把描述符转成 `SourceSnapshot`：`source_id` 变成 `plugin:<插件 id>:<源 id>`，`credential_reference` 变成 `plugin:<插件 id>:<credential_key>`，`style` 恒为 `'p'`。

`SourceProvider` 要实现三个方法，参数个数必须逐字对上（签名闸）：

```python
class MyProvider:
    def list_sources(self): ...                    # → Sequence[SourceDescriptor]
    def snapshot(self, source_id, cfg): ...        # → SourceSnapshot；cfg 是 config_json
    def authorize(self, headers, cfg): ...         # 就地改 headers，加鉴权头
```

**真范本**：`src/plugins/builtin/tianditu_source.py`（天地图影像 + 注记，纯描述符 + 凭据）。

### 7.2 下载管线（`pipeline`）

**什么时候用**：你要的是一条完整任务——用户框个区域、填几个参数、点开始，跑出一份产物。这是最常用的扩展点。

```python
from src.plugins.protocols import (ParamSchema, ParamSpec, PluginDefinition,
                                   PluginOutcome)
from src.contracts.artifact import ArtifactKind
from src.contracts.outcome import TileOutcome
from src.services.disk_budget import DiskEstimate


class MyPipeline:
    def params_schema(self) -> ParamSchema:
        return ParamSchema(specs=(
            ParamSpec(key='level', type='int', label='强度', default=1, min=1, max=9),
        ))

    def estimate(self, params, region) -> DiskEstimate:
        # 只用于磁盘预算与 UI；抛异常宿主按「没有估算」处理，任务照跑。
        return DiskEstimate(network_bytes=0, cache_bytes=0, temp_bytes=0,
                            output_bytes=1 << 20, peak_bytes=1 << 20,
                            tile_count=0, detail={'assumptions': ['固定 1 MiB']})

    def run(self, ctx) -> PluginOutcome:
        # 产物用 XYZ 目录形态：ArtifactKind 是封闭枚举，挑一个真的对得上的
        tiles = ctx.output_dir / 'tiles' / '0' / '0'
        tiles.mkdir(parents=True, exist_ok=True)
        (tiles / '0.png').write_bytes(b'')
        ctx.record_tile_outcome(0, 0, 0, TileOutcome.SUCCESS)
        ctx.progress(1, 1, 'done')
        ctx.register_artifact(ctx.output_dir / 'tiles',
                              kind=ArtifactKind.XYZ_DIR, fmt='png')
        return PluginOutcome.COMPLETED


def register() -> PluginDefinition:
    return PluginDefinition(pipeline=MyPipeline())
```

三个方法一个都不能少，参数个数一个都不能错（`params_schema(self)` / `estimate(self, params, region)` / `run(self, ctx)`）。

`run()` 的返回值决定任务终态（`task_manager._status_for`）：

| 返回 | 任务状态 | 什么时候用 |
| --- | --- | --- |
| `PluginOutcome.COMPLETED` | `completed` | 干净跑完 |
| `PluginOutcome.COMPLETED_WITH_GAPS` | `completed_with_gaps` | 有洞但都被解释了（`no_data`），或用户已接受缺块 |
| `PluginOutcome.PENDING_DECISION` | `pending_decision` | 有没交代的洞，等用户决定「补漏 / 接受」 |
| 其它任何值（含忘了 `return`） | `failed` | —— |

抛异常也是 `failed`，`error_message` 是 `类型: 消息`，落库前过脱敏（`mask_text_secrets`）。

**产物形态**：`ArtifactKind` 是封闭枚举——`xyz_dir` / `geotiff` / `mbtiles` / `terrain_dir` / `contour_dir` / `dem_dir`。没有「任意文件」这一档，不匹配的辅助文件就放在 `output_dir` 里不登记（示例插件的 `summary.json` 就是这么处理的）。

**真范本**：`src/plugins/builtin/mvt_pipeline.py`（TileJSON → 并发下载 PBF → MBTiles，含完整的缺块决策与配额用法）。

### 7.3 导出器（`exporters`）

**什么时候用**：某个已完成任务的产物需要换个格式落盘。

```python
from pathlib import Path
from src.contracts.artifact import Artifact, ArtifactKind
from src.core.database import utc_now_iso
from src.plugins.protocols import ExportContext, PluginDefinition


class MyExporter:
    def format_id(self) -> str:
        return 'myfmt'                      # 出现在导出格式表里的字符串

    def accepts(self, kind) -> bool:
        return kind is ArtifactKind.GEOTIFF  # 只接自己处理得了的形态

    def export(self, artifact: Artifact, dest: Path, ctx: ExportContext) -> Artifact:
        ctx.log(f'导出 {artifact.path} → {dest}', 'info')
        ctx.progress(0, 1)
        dest.write_bytes(Path(artifact.path).read_bytes())
        ctx.progress(1, 1)
        return Artifact(pipeline=artifact.pipeline, task_id=artifact.task_id,
                        kind=artifact.kind, path=str(dest), fmt='myfmt',
                        bytes_total=dest.stat().st_size,
                        created_at=utc_now_iso())


def register() -> PluginDefinition:
    return PluginDefinition(exporters=(MyExporter(),))
```

入口是**核心那条导出路由**：`POST /api/export/<pipeline>/<task_id>`，body `{"format": "myfmt"}`；插件导出器只是往格式表里加一行（`src/routes/api.py::_export_formats`）。目标路径由宿主算（源产物同级、追加后缀），返回的 `Artifact` 里 `pipeline`/`task_id`/`path` 三项会被宿主强制改写成自己的取值。

`ExportContext` 只有三样东西：`task_id`、`log(message, level)`、`progress(done, total)`。

隔离：`format_id()` 抛异常只当这个导出器不存在（不会 500 掉核心导出路由），`accepts()` 抛异常只当这件产物不被接受。

**界面上目前选不到自定义格式**——任务中心的导出按钮写死发 `mbtiles`，要用别的格式得走 REST（§13）。

**真范本**：`src/plugins/builtin/gpkg_exporter.py`。

### 7.4 任务钩子（`hooks`）

**什么时候用**：任务成功收尾后做点旁路的事——写 sidecar、发通知、触发下游。

```python
from src.plugins.protocols import PluginDefinition


class MyHook:
    def on_event(self, event) -> None:
        if event.kind != 'task_completed':
            return
        print(event.pipeline, event.task_id, event.plugin_id)


def register() -> PluginDefinition:
    return PluginDefinition(hooks=(MyHook(),))
```

`TaskEvent` 有四个字段：`kind`、`pipeline`、`task_id`、`plugin_id`。**v1 只有一个 kind `task_completed`，且只在插件任务落到 `completed` / `completed_with_gaps` 时发**（`task_manager._run_task` 末尾）。核心四条管线不发事件（§13）。

旁路铁律：钩子抛异常只记一条 warning，绝不影响任务。但请自己也包一层——你的钩子实例是公开的。

**真范本**：`src/plugins/builtin/artifact_meta.py`。

---

## 8. `TaskContext` API

`run(ctx)` 拿到的 `ctx` 是插件在运行期能碰到的**全部**宿主能力（`src/plugins/task_context.py`）。拿不到 manager、拿不到 socketio、拿不到数据库连接——这是物理约束，不是约定。

### 属性

| 属性 | 类型 | 说明 |
| --- | --- | --- |
| `task_id` | `int` | 任务行 id |
| `plugin_id` | `str` | 你的 id |
| `region` | `RegionSpec` | 区域。四至是 `region.bbox` → `(north, south, east, west)`；`region.bounds` → GDAL 序 |
| `params` | `Mapping` | **只读**（`MappingProxyType`），已过 schema 校验与 default 回填，含宿主键 |
| `output_dir` | `Path` | 本次任务的产物目录，构造时已建好 |
| `snapshot` | `SourceSnapshot \| None` | 任务参数带 `source_id` 且解析成功时才有 |

### 方法

| 方法 | 语义 | 禁忌 |
| --- | --- | --- |
| `stop_requested() -> bool` | 删除即取消：用户删任务时置位。长循环里必须定期查 | 查到了就尽快收尾，别再产出成品 |
| `progress(done, total, phase='') -> None` | 进度落库 + 广播（宿主按 2 Hz 节流，`done>=total && total>0` 那发必发） | 回调异常被宿主吞掉并记 warning；`total=0` 会让每次调用都穿透节流 |
| `log(message, level='info') -> None` | 写本任务日志。`level` 白名单：`debug/info/warning/error/exception`，其余**静默降级**成 `info` | 别传 `critical`/`fatal`（TaskLogger 没这两个方法） |
| `log_event(kind, **fields) -> None` | 结构化事件，日志里长成 `EVENT <kind> k=v` | —— |
| `granted(kind) -> int` | 本次运行拿到的配额。`kind` 是 `ResourceKind`（`NETWORK` / `TASK_SLOT` / `DISK_BYTES`） | **没在 manifest 声明 `permissions=["network"]` 就恒为 0**，见下 |
| `check_url(url, allow_private=False) -> str` | SSRF 闸，返回规范化 URL。**发任何请求前必须过** | 不通过抛 `UrlNotAllowed`（`ValueError` 子类）——你不接就是任务 failed |
| `proxy_url() -> str` | 生效代理，一次运行内只解析一次 | **阻塞**（探测可能超时）。async 里必须 `await asyncio.to_thread(ctx.proxy_url)` |
| `cache_path(z, x, y) -> Path` | 源命名空间下的共享缓存路径 | `snapshot is None` 时抛 `RuntimeError('该任务没有绑定数据源，无缓存命名空间')`。不绑源的插件请把中间文件放 `output_dir` 下 |
| `record_tile_outcome(z, x, y, outcome, error=None)` | 缺块记账，攒够 200 条自动落库。`SUCCESS` 的语义是**消除**该格的缺块行 | 线程安全（有锁）；`close()` 之后调用会被丢弃并记 warning，不抛；`error` 文本落库前脱敏 |
| `flush_outcomes() -> None` | 立刻落库缓冲 | 读 `gap_summary` 或做判定前先 flush |
| `register_artifact(path, kind, has_gaps=False, fmt='', meta=None)` | 登记产物 | `path` 必须落在 `output_dir` 内（`resolve()` 之后判），越界抛 `ValueError`。目录型产物由宿主 `measure_dir` 统计字节/瓦片数/层级；单文件只量字节数——要展示瓦片数/层级就自己塞进 `meta` |
| `close() -> None` | 收尾：flush + 之后拒收 outcome。**不碰日志句柄** | 宿主在 `run()` 返回后自动调，你不需要调 |

`TileOutcome` 五种（`src/contracts/outcome.py`）：

| 值 | 含义 | 算不算「已解释」 |
| --- | --- | --- |
| `SUCCESS` | 拿到了 | —— （不落行） |
| `NO_DATA` | 上游明确说这里没有（404、空覆盖） | ✅ 已解释 |
| `RETRYABLE_FAILURE` | 超时 / 5xx / 429 / 连接错 | ❌ |
| `PERMANENT_FAILURE` | 4xx（除 429）、响应不合法且重试耗尽 | ❌ |
| `CACHE_FAILURE` | 拿到了但写不进去（盘满 / 权限） | ❌ |

**缺块决策的正确写法**（§13-3 的口径，`mvt_pipeline` 是范本）：只要有一个**非** `NO_DATA` 的洞，就别产出成品，返回 `PENDING_DECISION`。用户在界面点「接受缺块」后，宿主把 `params['_gap_accepted'] = True` 回写任务行并**重跑一遍你的 `run()`**——你要在开头读这个键，跳过重下、直接用暂存的数据收尾。

### NETWORK 配额：最容易踩的坑

宿主只给**在 manifest 里声明了 `permissions = ["network"]`** 的插件申请 NETWORK 配额（`task_manager._network_request`）。实测：

```
无 network 权限 → _network_request() 返回 None → ctx.granted(ResourceKind.NETWORK) == 0
有 network 权限 → ResourceRequest(kind=NETWORK, requested=<concurrent_downloads 配置>, minimum=1)
```

所以：**要联网就声明 `network`**，并且在 `granted()` 返回 0 时自己兜底一个保守并发（`mvt_pipeline._concurrency` 就是这么写的：兜底 4 并在日志里喊出来，因为那时你的连接不在全局账本里）。

---

## 9. 配置与凭据

插件配置存在 `plugins.config_json`（每插件一份 JSON 对象），与任务参数是**两件事**：

- **任务参数**（`params_schema()`）：每次建任务填一遍，落进任务行。
- **插件配置**（`PluginDefinition.config_schema`）：填一次长期有效，凭据只能走这里。

`config_schema` 不声明的话 `set_config` 原样收下任何键——`{"tokn": "..."}` 拼错也照存，然后解析出空串、每块瓦片 401，而没有任何地方告诉用户键名拼错了。所以**只要你要读配置，就声明 schema**。声明之后：

```
set_config({'tokn': 'abc'})  → {'tokn': 'unknown param', 'token': 'required'}   # 逐键报错
set_config({'token': 'X'})   → {}                                              # 空表 = 已存
```

### 凭据链路（走完整一遍）

作者要做的只有两件事：**URL 模板里写 `{credential}`**、**`config_schema` 里声明 `ParamSpec(type='credential')`**。其余是宿主的：

1. 你在 `SourceDescriptor` 写 `credential_key='token'`；
2. `registry.build_source_snapshot` 转成 `credential_reference='plugin:<插件 id>:token'`——**存的是键名，不是值**；
3. 用户在插件面板的配置表单填真值，存进 `plugins.config_json`；
4. 下载时 `download_engine.get_tile_url` 把模板里的 `{credential}` 换成 `credentials.resolve_reference('plugin:<id>:token')` 解出来的值（60 秒进程内缓存，改配置立即失效）。

实测：

```
credential_reference: plugin:netsrc:token
url_template:         https://example.com/{z}/{x}/{y}.png?key={credential}
resolve_reference:    SECRET123
```

铁律与它们的边界：

- **凭据不进任务参数。** 参数会原样落进 `params_json`，那是任务行、诊断包与备份都读得到的地方。凭据只走 `config_json`。
- **`type='credential'` 只是纵深防御**，不是保险：它让面板用密码框、让已存值不回显（下发 `__TF_UNCHANGED__` 哨兵）、让任务参数序列化时剔除该键（`plugins_api._public_params`）。但宿主**无法判断一个字符串是不是密码**——你把 token 塞进一个 `type='str'` 的任务参数，它就会一路进任务行和接口响应。这条只能靠你自己守。
- `resolve_reference` 任何失败都返回 `''`（不抛）：凭据缺失应该落成瓦片 401 记账，而不是打死整条管线。
- 值是 bool 或结构化对象时一律解析成 `''`（拼进 URL 的 `'False'` 比空串更难查）。

---

## 10. 依赖与 vendor

**打包产物里没有 pip、没有 site-packages。** 用户装了你的插件之后不会（也没法）跑 `pip install`。所以：

- 只用标准库 + 宿主已有的依赖（`src.*`、GDAL/osgeo、aiohttp、Flask……）→ 什么都不用做。
- 需要别的第三方库 → 把它们**整个拷进** `plugins/<id>/vendor/`。加载时该目录会被 append 到 `sys.path` **末尾**（`registry._add_vendor_path`）。

`append` 而不是 `insert(0)` 是刻意的：**宿主必须赢**。你 vendor 一个同名包不会顶替宿主或标准库的模块——vendor 只用于补你自己缺的库。同一目录只插一次，重扫不会让 `sys.path` 线性膨胀。

**纯 Python 依赖无限制**；带二进制扩展（`.so` / `.pyd`）的必须在清单里声明 `requires_abi`，形如 `cp312-linux-x86_64`（`cp<major><minor>-<sys.platform>-<platform.machine()>`）。不匹配的机器上插件拒载并显示原因——那比让用户读一段 ImportError 栈强。不声明就是「我保证纯 Python」，装到别的 Python/平台上炸了是你的责任。

想知道目标机器的 ABI 标签：

```bash
uv run python -c "from src.plugins.manifest import current_abi_tag; print(current_abi_tag())"
# cp312-linux-x86_64
```

**离线不变量**：UI 资产只从插件目录本地服务（`GET /api/plugins/<id>/assets/<file>`，两道门：落地包含判断 + `ui.assets` 白名单），**不许引 CDN**。整个程序的前提是断网可用。

---

## 11. 调试与排错

### 加载失败去哪看

三处，同一份字符串：

1. **面板**：插件卡片上的红色失败原因。加载失败的插件**仍然在列表里**——这是有意的。
2. **REST**：`GET /api/plugins` → 每条记录的 `load_error` 字段（空串 = 正常）。
3. **数据库**：`plugins` 表的 `load_error` 列。

### 启动日志长什么样

```
INFO  加载插件：src.plugins.builtin.tianditu_source
INFO  加载插件：/path/to/plugins/hello/plugin.toml
WARNING 插件加载失败：/path/to/plugins/broken/plugin.toml   （带 traceback）
INFO  插件注册表就绪：5 个（可用 5 / 失败 0，启用 1）
```

最后那行有失败时是 **WARNING** 而不是 INFO（`registry.load_all`）：可用数才是重点。逐个报名字是为了定位「卡在谁身上」——插件在模块级做无超时网络请求会让整个程序起不来，日志会停在那个插件的名字上。

### 每任务日志

`<BASE_DIR>/logs/tasks/plugin_<task_id>.log`（`task_logging.task_log_path`，形制 `<pipeline>_<task_id>.log`，插件的 pipeline 恒为 `plugin`）。你 `ctx.log()` / `ctx.log_event()` 写的东西都在这里，宿主的准入判决、终态、异常栈也在这里。**异常栈只写这里**（有脱敏 filter）；`logs/terraforge.log` 只留一句掩过的摘要。

### 常见拒载原因逐条（实测消息）

| 症状 | `load_error` 原文 | 怎么修 |
| --- | --- | --- |
| id 有大写/非法字符 | `ManifestError: 非法插件 id：'bad-ID'（小写字母/数字/中划线/下划线，字母开头）` | 改成小写字母开头 |
| API 大版本不符 | `ManifestError: api_version '2.0' 与宿主 1.x 不兼容` | 宿主是 `1.x` 就写 `"1"` |
| entry 越界 | `ManifestError: entry 只许插件目录内的相对路径（每段限字母/数字/点/下划线/中划线，不许空段、纯点号段、空白、盘符或协议前缀）：'../evil.py'` | 用目录内相对路径 |
| entry 名字对不上文件 | `ManifestError: entry 文件不存在：/…/plugins/noentry/main.py` | 建文件或改 `entry` |
| **签名写错** | `ManifestError: 插件 'sig' 的 pipeline.run 签名不符：宿主按 run(self, ctx) 调用，实际是 run()` | 按宿主的调用形状写；比的是**位置参数个数**，`*args` 视为无上限 |
| 少了协议方法 | `ManifestError: 插件 'missing' 的 pipeline 不满足 PipelinePlugin 协议：缺 params_schema, estimate` | 三个方法补齐 |
| capabilities 拼错 | `ManifestError: 未知 capabilities：['pipelines']` | 白名单只有四个词 |
| permissions 拼错 | `ManifestError: 未知 permissions：['gpu']` | 白名单只有三个词 |
| ABI 不匹配 | `ManifestError: ABI 不匹配：插件需要 cp311-linux-x86_64，宿主是 cp312-linux-x86_64` | 换匹配的二进制，或删掉 `requires_abi`（纯 Python） |
| `ui.assets` 写成字符串 | `ManifestError: ui.assets 必须是字符串数组，实际是 str` | 写成 `["panel.js"]` |
| 没有 `register()` | `ManifestError: plugin.py 缺少 register() 函数` | 加模块级 `register()` |
| `register()` 返回错东西 | `ManifestError: register() 必须返回 PluginDefinition` | —— |
| 模块级抛异常 | `RuntimeError: 模块级炸了`（原样带类型名） | 别在 import 期做会失败的事 |
| 坏 TOML | `ManifestError: plugin.toml 读取/解析失败：…` | 注意编码必须 UTF-8（记事本存的 GBK 会炸在 decode） |
| 撞了别人的 id | `ManifestError: 插件 id 'x' 已被 builtin 插件占用（先到者赢），本插件未加载` | 换个 id |

### 其它常见现象

| 现象 | 原因 |
| --- | --- |
| 装了但面板里没有 | 目录层级不对（必须是 `plugins/<dir>/plugin.toml`）、或没重启 |
| 在册、无错误，但建任务报 `插件管线不可用` | 没启用，或 `register()` 里没给 `pipeline` |
| 建任务报 `参数非法：xxx=unknown param` | 传了 schema 没声明的键（宿主键除外） |
| 任务秒失败，错误是 `资源配额不足（任务槽/磁盘预留），请稍后重试` | 任务槽满。插件任务**没有排队**，拿不到名额直接 failed |
| 任务 `failed`，日志写「进程在任务运行期间退出」 | 上次跑到一半程序被杀。插件任务没有断点续跑，重跑即可 |
| `ctx.granted(NETWORK)` 恒为 0 | manifest 没声明 `permissions = ["network"]` |
| `cache_path()` 抛 `RuntimeError` | 任务没绑数据源。中间文件放 `output_dir` 下 |
| `register_artifact` 抛 `ValueError: 产物必须落在任务产物目录（…）内` | 路径越界（含符号链接指出去） |

---

## 12. 版本兼容

`PLUGIN_API_VERSION = '1.0'`（`src/plugins/protocols.py`）。加载时**只比 major**：`api_version = "1"` / `"1.0"` / `"1.9"` 都能载，`"2"` 一律拒载。

这条闸的意思是：**宿主升到 2.x 时你的插件会明确坏掉，而不是静默行为错乱。** 到那时你需要按新协议改代码、改 `api_version`，而不是把版本号往上抬。

major 内的兼容承诺：本文档列出的协议方法签名、`PluginDefinition` 字段、`TaskContext` 公开面、`plugin.toml` 字段在 1.x 内只增不改。私有名（下划线开头）随时会变——别 import 它们。

---

## 13. 限制与已知欠账

如实列，都是今天的真实状态（对应 `RELEASE_NOTES.md` v0.4.0 的欠账清单）：

- **无沙箱。** 插件 = 宿主进程内任意代码。信任边界是本机 / 局域网，用户只应启用自己看过的插件。`permissions` 不阻止任何事（`network` 只影响配额）。
- **打包后没有 pip。** 依赖只能 vendor（§10）。
- **插件缺省关闭**，装上不等于能用。
- **卸载即清配置。** 插件目录消失后重启，它在 `plugins` 表里那一行（`enabled` + `config_json`，**含用户填的 token**）会被删掉（`registry._prune_stale_rows`）；重装是干净的初始状态，需要重填。这样做是为了防「删掉插件 A、装一个同 id 的插件 B，B 直接继承 A 的开关与凭据」。两道保险：目录整个扫不动时不清；本轮**只要有任何一个插件加载失败**就整趟不清（避免一次写坏的 `plugin.toml` 把用户的真配置删掉）。已跑完的任务与产物不受影响。
- **没有「只重跑缺块」。** 插件任务只能整趟重跑；`PipelinePlugin.run()` 收不到「只跑这些格子」的入参（协议变更，不进 1.0）。
- **区域只能手填四至。** 插件任务表单里是四个数字输入框，接不上地图框选。
- **i18n 未接。** `ParamSpec.label` 是纯字符串，不跟界面语言走；插件的 `i18n.toml` 运行时合并推迟了。
- **钩子 v1 只覆盖插件任务。** 唯一的事件是插件任务成功终态的 `task_completed`；核心四条管线（map / dem / contour / local_terrain）不发事件。
- **导出格式选择器没做。** 后端已并进 `POST /api/export/<pipeline>/<id>`，但任务中心的导出按钮写死发 `mbtiles`，自定义格式只能走 REST。
- **凭据缓存 60 秒（进程内）。** 今天瓦片端口与主服务同进程，改配置立即 `invalidate`，所以是即时生效；将来拆进程才会变成最坏 60 秒延迟。
- **面板样式借 Bootstrap 通用类**，与配置页不完全同款。

---

## 14. 一页速查

```
plugins/<id>/plugin.toml        id / name / version / api_version 必填
plugins/<id>/plugin.py          def register() -> PluginDefinition
plugins/<id>/vendor/            依赖，进 sys.path 末尾

PipelinePlugin   params_schema(self) / estimate(self, params, region) / run(self, ctx)
SourceProvider   list_sources(self) / snapshot(self, source_id, cfg) / authorize(self, headers, cfg)
Exporter         format_id(self) / accepts(self, kind) / export(self, artifact, dest, ctx)
TaskHook         on_event(self, event)

ctx.params / ctx.region / ctx.output_dir / ctx.snapshot
ctx.stop_requested() ctx.progress() ctx.log() ctx.log_event()
ctx.granted() ctx.check_url() ctx.proxy_url() ctx.cache_path()
ctx.record_tile_outcome() ctx.flush_outcomes() ctx.register_artifact()

要联网 → permissions = ["network"]，否则配额恒 0
要凭据 → url 模板写 {credential} + config_schema 声明 type='credential'
出问题 → GET /api/plugins 的 load_error + logs/tasks/plugin_<id>.log
```

代码事实源：`src/plugins/protocols.py`（协议）、`manifest.py`（清单）、`registry.py`（加载与闸门）、`task_context.py`（运行期门面）、`task_manager.py`（任务生命周期）、`params.py`（参数校验）、`credentials.py`（凭据）、`builtin/`（四个真范本）。
