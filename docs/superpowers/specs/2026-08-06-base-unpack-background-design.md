# 底图解压移到启动后台，进度显示在底部状态栏

> **状态**：设计已批准，待实施 ｜ **记录时间**：2026-08-06
> 前置版本：v0.2.9（底图随任务植入，`597d4ba3e`）
> 前置设计：`docs/superpowers/specs/2026-08-06-base-terrain-graft-design.md`

## 结论先行

随包底图的解压从「第一次切片时同步做」改成「**启动后立刻在后台做**」，进度实时显示在底部状态栏右侧。第一次切片不再有那段几分钟的无提示等待。

三条已定的取舍：

| 取舍 | 决定 | 理由 |
|---|---|---|
| 什么时候解压 | 启动就预热，不问用不用地形 | 只用地图下载的用户也被扣 224 MB，但换来「任何时候开始切片都是零等待」。分卷本来就随包分发了 167 MB，增量不是数量级差异 |
| 进度怎么送到前端 | socketio 广播 + connect 快照 | 与 `task_progress` 等既有进度同一套模式。代价是 socket 实例要从 `tasks.js` 提成全局单例 |
| 解压失败怎么表现 | 状态栏一直显示「底图不可用」，hover 出原因 | 这是持续性状态不是一次性事件。不显示的话，用户几小时后才会奇怪为什么地形产出不自包含 |

## 背景

### 现状：启动不解压，切片时才解压

`ensure_base_unpacked` 目前只有一个调用点 —— `dem_task_tiler.py:98`，切片任务开头。它跑在任务的后台线程里，**不阻塞 Flask 主线程**，但那几分钟里前端的任务进度条一动不动（`build_terrain` 还没开始，没有 `progress_cb` 可报），看起来就是卡住了。

`stage_cb('base_unpack', fraction)` 这条通道已经存在且已接到 `terrain_job_progress`，但它是**任务级**事件：任务行上会显示，底部状态栏不会，其它页面更看不到。

### 为什么改成启动预热

首次切片等几分钟这件事本身可以消掉：解压与切片没有数据依赖（底图独占 z0–z7，任务只出 z8+），解压完全可以提前到启动时做完。启动时做的额外好处是那几分钟通常与用户熟悉界面、框选范围重叠，感知成本接近零。

## 架构

```
create_app()
    │ start_warmup(socketio)
    ▼
base_terrain_warmup            ← 状态单例 {phase, fraction, error}
    │ socketio.start_background_task
    ▼
ensure_base_unpacked(stage_cb) ← base_terrain（纯文件操作，不认识 socketio）
    │ stage_cb → 更新状态 + 节流 emit
    ▼
socketio 'base_unpack_progress' ──┬─→ 已连接的客户端
                                  └─→ connect handler 推快照给新客户端
    ▼
static/js/base_terrain_status.js → 底部状态栏右侧元素
```

### 为什么新开一个模块

`src/services/base_terrain_warmup.py` 是新增的。**不塞进 `base_terrain.py`**：那个模块刻意不 import numpy / osgeo / flask，才能在没有 GDAL 的环境里单测（与 `layer_json.py` 同一定位，它自己的 docstring 写着这条）。让它认识 socketio 会毁掉这个性质，而且解压逻辑与「什么时候跑、进度报给谁」是两件事。

**也不放 `app_factory.py`**：那是组装根，只做接线不放业务逻辑（模块 docstring 明确了这个定位）。

## 接线与触发

`create_app()` 里，socketio 与四个 manager 构造完成之后调一次 `start_warmup(socketio)`。放在 manager 之后是因为解压与它们无关，但顺序上要保证 socketio 已经存在。

`create_app()` 本身只在 `StartupRole.should_create_app` 为真时被调用（spawn 平台的 multiprocessing worker、dev reloader 的观察者父进程都会跳过整个 app 初始化），所以**不需要任何额外的进程判断** —— 一次启动只有一个进程会预热。跨进程锁仍然兜住「用户同时开了两个实例」这种情况（v0.2.9 已实现）。

`async_mode` 实测是 `threading`（依赖里没有 eventlet / gevent），所以 `socketio.start_background_task` 就是起一个普通 `Thread`：可以在 `socketio.run()` 之前调用，跨线程 `socketio.emit()` 也是安全的 —— 四个 task manager 早就这么用了。

新模块要在 `app_factory.py` 顶部的预热 import 清单里加一行。那份清单同时是**打包的可达性清单**（模块 docstring 写明了）：凡是只在函数体内 import 的模块都要列出来，让 Nuitka 的静态分析看得见，否则进不了 dist。

## 组件：`src/services/base_terrain_warmup.py`

```python
_STATE = {"phase": "idle", "fraction": 0.0, "error": None}
_LOCK = threading.Lock()
_EMIT_MIN_INTERVAL = 0.5
```

`phase ∈ idle | running | ready | failed`。`fraction ∈ [0, 1]`。

状态**必须带锁**：后台解压线程写、socketio 的 `connect` handler 在请求线程里读，是真实的跨线程访问，不是形式主义。

### `snapshot() -> dict`

线程安全地返回状态副本。`connect` handler 与测试都用它。返回副本而不是内部 dict —— 调用方持有引用后被后台线程改写会读到撕裂的状态。

### `start_warmup(socketio) -> None`

- **底图已就位** → 直接把状态置成 `ready`，**连线程都不起**，立即返回。这是 99% 的启动路径，开销就是三次 `iterdir` 计数（`is_base_ready` 的探针）。
- **底图缺失** → 状态置 `running`，`socketio.start_background_task(_run, socketio)`。
- **幂等**：已经有线程在跑（`phase == running`）时直接返回，不起第二个。`create_app()` 只调用一次，但测试会反复调。
- `phase == failed` 时再次调用会**重新尝试**（`is_base_ready` 仍为 False → 起线程）。这是刻意的：`start_warmup` 的语义是「确保底图在解压或已就位」，不是「一辈子只跑一次」。生产上不会发生（`create_app` 只调一次），但测试里「失败 → 修好前提 → 重试成功」是要覆盖的路径。

### `_run(socketio)`（后台执行体）

调 `ensure_base_unpacked(stage_cb=<节流包装>)`：

- 成功 → `phase='ready'`，`fraction=1.0`，**立即 emit**（绕过节流）。
- 抛 `RuntimeError` → `phase='failed'`，`error=str(e)`，**立即 emit**。不自动重试。
- 返回 `None`（分卷缺失，有人删了 `assets/`）→ `phase='failed'`，error 写明「找不到随包分卷」。这与解压失败在用户视角是同一件事：底图不可用。

### `reset_state()`

只给测试用。模块级单例在同一个 pytest 进程里跨用例残留，没有它就得靠用例顺序，那是 flaky 的温床。

## 数据流：事件与节流

事件名 `base_unpack_progress`，载荷就是 `snapshot()` 的三个字段。

**节流 0.5 秒**：解压是流式读，`_extract_stream` 的 `readinto` 每次调用都触发一次 `stage_cb` —— 167 MB 下是上万次。不节流会把前端打爆。范本是 `dem_task_manager._PROGRESS_EMIT_MIN_INTERVAL`（那里的注释写明「严格时间窗，无『变化必发』豁免」，同样适用）。

取 0.5 而不是照抄那边的 1.0：解压是分钟级的一次性过程，而切片是小时级的长跑 —— 同样的绝对间隔在短过程上显得迟钝。0.5 秒下整个解压最多产生几百次 emit，量级上仍然安全。

**终态绕过节流**：`ready` / `failed` 必须立即 emit。被节流窗口吃掉就永远不发了 —— 这是「进度条卡在 97% 不动」那类 bug 的标准成因。

**`connect` handler 推快照**：`src/routes/socketio_events.py` 的 connect 里，除现有的 `connected` 事件外，再 emit 一次 `base_unpack_progress`（用 `snapshot()`）。

没有这一步，两个真实场景会失效：用户在解压跑到一半时才打开浏览器（要等到下一次节流窗口才看到，最多 0.5 秒，尚可）；以及用户在**解压失败几小时后**才打开浏览器 —— 终态事件早就发完了，他永远看不到那条失败标记，而「失败要一直显示」正是本设计的既定要求。

**不加 REST 端点**：connect 快照已经覆盖了页面加载时的起点，再加一个 `GET /api/terrain/base/status` 是第二处事实来源。

## 前端

### socket 单例化

现状：`static/js/tasks.js:1` 是脚本级 `let socket`，第 18 行 `socket = io()`；`map.js:1459` 直接引用这个变量（`typeof socket === 'undefined'` 守卫）。**只有首页创建 socket**，`/history` 与 `/config` 没有任何 WebSocket 连接（`base.html:71` 的连接状态指示器在那两页保持 hidden）。

改动：新增 `static/js/socket.js`，全局加载于 `base.html`（在 `ui.js` 之后 —— 它要调 `initConnectionStatus`），暴露 `window.TerraSocket.get()` 惰性单例，并在加载时立即建立连接 + 点亮连接状态指示器。

`tasks.js:18` 改成 `socket = window.TerraSocket.get()`。**`map.js` 一行不用改** —— 它读的还是 `tasks.js` 的那个脚本级变量，值已经是同一个实例。

⚠️ 不能在 `socket.js` 里写 `window.socket = io()`：`tasks.js` 顶部的 `let socket` 在全局作用域会**遮蔽** `window.socket`，两边看到的是不同的东西。用带命名空间的 `window.TerraSocket` 避开这个陷阱。

连带效果：`/history` 与 `/config` 从此也有 WebSocket 连接与点亮的连接状态指示器。这是「所有页面都要显示解压进度」的必然结果，不是副作用。

### ⚠️ socket.io 库要从 Cesium 的 block 里拆出来

`base.html` 现在把 Cesium（5.7 MB）与 socket.io（44 KB）放在同一个 `{% block vendor_js %}` 里，而 **`/config` 页覆盖该 block 为空** —— 它连 socket.io 库都不加载（那行注释写着「白付的解析/下载全省」，针对的是 Cesium）。不拆的话，`/config` 页拿不到任何实时推送，「所有页面都显示进度」在那一页直接落空。

拆法：Cesium 留在 `{% block vendor_map_js %}` 里，socket.io 的 `<script>` 移到 block **之外**（全局加载）。`config.html` 把覆盖目标从 `vendor_js` 改成 `vendor_map_js`，仍然省掉 5.7 MB。44 KB 与 5.7 MB 不是一个量级，原注释的意图不受影响。

### 状态栏元素

`base.html` 的 `{% block statusbar %}` **之后**加一个全局元素（因此排在最右）：

```html
<span class="statusbar-basemap" id="statusBaseUnpack" hidden>
    <span id="statusBaseUnpackText"></span>
    <span class="statusbar-progress" id="statusBaseUnpackProgress" hidden>
        <span class="statusbar-progress__bar" id="statusBaseUnpackBar"></span>
    </span>
</span>
```

复用现有的 `.statusbar-progress` / `.statusbar-progress__bar`（首页任务进度条已在用），不新造一套进度条样式。

显示规则：
- `idle` / `ready` → 整个元素 hidden。
- `running` → `底图解压 47%` + 进度条。
- `failed` → `底图不可用`，`title` 带具体原因，进度条 hidden。

新增 `static/js/base_terrain_status.js` 负责监听与渲染，全局加载。**不并进 `socket.js`**：一个管连接、一个管这块 UI，边界清楚。

### ⚠️ 必须一起改的窄屏 CSS

现有规则：

```css
.workbench-statusbar .statusbar-item:last-child { display: none; }
```

窄屏隐藏状态栏最后一项（首页是时钟）。新元素排在最后之后，这条规则会**一个都选不中**（新元素不是 `.statusbar-item`），于是时钟在窄屏不再隐藏 —— 既有行为被静默改掉。

改法：把它从依赖 DOM 顺序的 `:last-child` 换成按语义选中时钟本身（`.statusbar-clock`）。顺带把一条脆弱规则换掉 —— 它的正确性依赖「时钟恰好排最后」这个巧合，任何人往 statusbar 末尾加东西都会踩到。新元素在窄屏**保留显示**：它是要紧状态，窄屏更需要。

`tests/test_css_contract.py` 里若有钉住这条规则的用例，同步改；契约测试的既有约定是「断言源码引用了 key，且 catalog 的 zh 值是预期词」，两半都要改，只改一半是可绕过的。

## 与切片的关系

一行都不用改。

- 预热完成后，切片开头的 `ensure_base_unpacked` 幂等命中，秒返回。
- 预热还在跑时用户就启动了切片 → 跨进程锁让切片阻塞等待（v0.2.9 已实现，Windows 上是自旋轮询）。此时任务进度条不动，但底部状态栏的解压进度在跑，用户看得到原因。
- 预热失败后切片仍会自己重试一次 `ensure_base_unpacked`（比如磁盘腾出来了）；仍失败则走已有的 parentUrl 兜底（`dem_task_tiler.py` 的 `except RuntimeError`）。

两条进度走不同事件（`base_unpack_progress` 是全局状态，`terrain_job_progress` 是任务级），互不干扰。

## 错误处理

| 情况 | 行为 |
|---|---|
| `assets/` 不可写（Program Files、只读介质） | `phase=failed`，状态栏「底图不可用」+ 原因；切片走兜底，功能不受影响 |
| 磁盘满 | 同上。临时目录由 `ensure_base_unpacked` 的 finally 清掉 |
| 分卷缺失（有人删了 `assets/`） | `phase=failed`，error 写明找不到分卷 |
| emit 自身抛异常（客户端断开） | 吞掉。与 `base_terrain._emit`、`cesiumlab_terrain._gdal_stage_callback` 同一条既有约定：一次广播故障不该影响解压 |
| 解压中途关窗 | 临时目录 `.base_unpack_<pid>_*` 由启动清扫的第 6 类回收（v0.2.9 已实现） |

失败**不自动重试**：重试要么很快再失败一次（权限问题不会自己好），要么就是在无限循环里刷日志。下次启动、或下次切片会自然重试。

## 测试清单

护栏而不是覆盖率。每条都要能在改坏时变红：

| 用例 | 钉住什么 |
|---|---|
| 底图已就位 → `start_warmup` 不起线程且状态直接 `ready` | 99% 的启动路径零开销 |
| 底图缺失 → 起线程，状态从 `running` 走到 `ready` | 主路径 |
| 重复调 `start_warmup` → 只有一个线程在跑 | 幂等 |
| `ensure_base_unpacked` 抛 RuntimeError → `phase=failed` 且 `error` 非空 | 失败可见 |
| `ensure_base_unpacked` 返回 None（分卷缺失）→ 同样是 `failed` | 分卷缺失不能显示成「就绪」 |
| 连续 100 次 stage_cb 回调 → emit 次数远小于 100 | 节流生效 |
| 终态 emit 不受节流窗口影响（紧跟在一次节流 emit 之后也必发） | 进度条不会卡在 97% |
| `socketio.emit` 抛异常 → 状态仍正确流转 | 一个断开的客户端不该毁掉解压 |
| connect handler 返回当前快照 | 中途/事后连上的客户端能看到状态 |
| `snapshot()` 返回副本而非内部 dict | 调用方改不了内部状态 |
| `socket.js` 暴露单例、`tasks.js` 不再直接 `io()` | 源码契约：防止有人改回去开出两个连接 |
| `base.html` 有 `statusBaseUnpack` 元素且在 statusbar block 之后 | 位置契约（右侧） |
| 窄屏规则不再依赖 `:last-child` | CSS 契约：防止往末尾加元素时静默改掉窄屏行为 |
| 新增 i18n key 双语齐全 | `test_i18n.py` 自动兜住 |

## 排除的方案

### REST 轮询（状态单例 + `GET /api/terrain/base/status` + 前端按需轮）

不需要动 socket 的所有权结构，且 99% 的情况下一次请求拿到 `ready` 就收工，开销几乎为零。排除理由：与项目既有的「进度走 socketio」模式不一致，且要多维护一个端点。socket 单例化的一次性成本可控（新增一个文件、改 `tasks.js` 一行），换来长期的模式统一。

### 只在首页显示

改动最小 —— socket 实例与 statusbar block 现成都在 `index.html`。排除理由：用户启动后完全可能直接去配置页或历史页，那时依旧没有任何提示，与现在的问题一模一样。

### 把解压做成可关闭的配置项

排除理由：这个仓库已经有 `terrain_global_base_maxzoom` 那个「全项目零消费的假旋钮」的教训。真要关，删掉 `assets/terrain/*.part` 就是现成的、有明确语义的做法，且已有兜底路径覆盖。

## 已知代价

- **只用地图下载 / 等高线的用户也会被扣 224 MB 磁盘和几分钟 IO**，且没有开关。这是「启动就预热」的直接代价，已明确接受。
- `/history` 与 `/config` 从此各维持一个 WebSocket 连接。
- 启动后几分钟内磁盘 IO 偏高（4.3 万个小文件，Windows 上尤其），可能与用户同时进行的瓦片下载抢 IO。
- socket 单例化触及 `tasks.js` 与 `map.js` 之间那条靠脚本级变量传递的隐式约定 —— 改动本身只有一行，但那条约定是脆的，实施时要有源码契约测试钉住。
