# 任务行显示下载速度（MB/s）

## 结论

任务运行时，在任务行上显示**真实网络带宽**（`2.3 MB/s`）。速度由后端计算并随
`task_progress` 推送，前端只负责格式化和「停滞归零」。

## 背景：现在缺什么

任务行现有：进度条 + `N / M 瓦片` + `已运行: X · 预计剩余: Y`
（`static/js/task_list.js:63,129-131`）。没有速度。

且现有 ETA 用的是**全程平均**（`static/js/tasks.js:627-634`，`elapsed / progress`），
起步几秒抖得厉害，中途网络变慢反应迟钝。本次不改 ETA，只加速度。

## 决策 1：后端算，不是前端算

前端算会出错。emit 节流是 0.5s（`task_manager.py:45`）/ 1.0s（DEM、等高线），
但 Socket.IO 送达有抖动，且**浏览器后台标签页会节流事件循环** —— 切回前台时
积压事件一次性涌入，按到达时间算出的速率会瞬间飙到几百 MB/s。

后端用 `time.monotonic()` 采样不受影响，三条下载路径共用一份实现，页面刷新后
也不必等两发事件才有数。

## 决策 2：只统计真正走网络的字节

磁盘缓存命中的字节**不计入**。理由：那是本地读盘，算进去会让「网速」虚高一个
数量级，用户拿它判断"网络是不是卡了"就完全失真。

三条路径各自的识别方式不同：

**map 瓦片** —— `task_manager.py:874-916` 在下载开始前就枚举出缓存命中并直接
计入 `base_downloaded`，它们根本不进 `progress_callback`。但 `download_engine.py:703-718`
的引擎内缓存分支**仍然可达**（两个 bbox 重叠的任务并发时，枚举后、下载前有
瓦片被另一个任务写进缓存），所以该分支显式传 `size_bytes=None`。

**DEM** —— 这里有个坑：`size_bytes` 是**双重用途**的，它要写进 `dem_files.size_bytes`
列，所以缓存命中 / 文件已存在时（`dem_download_engine.py:172,177`）必须继续上报
真实大小，不能改成 `None`，否则破坏库里的文件大小记录。

解决办法不改引擎签名：引擎只在**真正要发起网络请求之前**发一次
`status="downloading"`（`dem_download_engine.py:197`），缓存命中路径不发这一下。
manager 记下发过 `downloading` 的 granule 集合，只有这些 granule 的 `completed`
才计入速度。

**等高线下载阶段** —— 同 DEM（共用 `DemDownloadEngine`）。渲染阶段不加：本地
渲染没有网络字节。

## 组件

### `src/services/download_speed.py`（新增）

```python
class SpeedMeter:
    def __init__(self, window: float = 3.0, clock=time.monotonic)
    def record(self, n_bytes: int) -> None   # 累加字节 + 打时间样本
    def bps(self) -> float                   # 窗口内 Δbytes / Δt
```

内部 `deque[(时刻, 累计字节)]`。`record` 驱逐超窗样本，但**至少保留两个**、
左端只留一个窗外样本 —— 慢速下载时样本稀疏，全驱逐会只剩一个样本、跨度为 0，
算不出速率。

**每次进度回调都要 `record`**，没有网络字节时传 0。传 0 让时间窗照常前进，
速率才会在下载变慢或停滞时如实回落；漏调会让速率冻在最后那个高值上。

时钟可注入，单测不依赖 `sleep`。非线程安全 —— 每任务一个实例，只在该任务的
下载事件循环里调用。

### 接线

| 文件 | 改动 |
|---|---|
| `download_engine.py` | `progress_callback` 加 `size_bytes=None` **带默认值**的第四参。下载成功传 `len(data)`；缓存命中、失败传 `None`。带默认值 → `tests/` 里约 20 处 3 参替身不受影响 |
| `task_manager.py` | 建 meter，回调里 `record(size_bytes or 0)`，emit 载荷加 `download_speed_bps` |
| `dem_task_manager.py` | 同上 + `downloading` 集合过滤；emit 前 `row["download_speed_bps"] = ...` |
| `contour_task_manager.py` | 同 DEM。仅下载阶段，渲染阶段不加 |

`local_terrain` 不加：用户上传，没有下载。

## 数据流

```
download_engine  --size_bytes-->  progress_callback
                                        |
                                  SpeedMeter.record()
                                        |
                            emit task_progress {download_speed_bps}
                                        |
                                  TaskStore 合并 (+ 到达时间)
                                        |
                              task_list.js  formatSpeed()
```

字段名 `download_speed_bps`，单位字节/秒，`round()` 成整数进载荷。

## 前端

- `tasks.js` 加 `formatSpeed(bps)` → `0 B/s` / `123 B/s` / `1.2 MB/s`
- `task_list.js` 在第二行 `task-pct` 之后加 `.task-speed`，仅 `running` 且有值时显示
- CSS 用等宽字体 + `font-variant-numeric: tabular-nums`（同 `.task-pct`），防数字抖动
- i18n `js.history.row.speed`，中英都要（`tests/test_i18n.py` 强制两个 locale 齐全）

### 停滞归零

网络断了但任务没判失败时，回调不再触发 → 不再 emit → 界面会永远停在最后那个
`2.3 MB/s`，看着像还在跑。

前端记下速度的到达时间，靠 `task_list.js:234` 已有的 1 秒 tick，超过 5 秒没有
新事件就显示 `0 B/s`。纯前端，不需要后端心跳。

## 不做

- **不落库**。速度是瞬时量，`tasks` / `dem_tasks` / `contour_tasks` 都不加列。
  页面刷新后等下一发 `task_progress`（≤1s）就有了。
- 不显示累计已下载体积。
- 不改 ETA 算法（全程平均的迟钝是另一件事）。

## 验证

- `SpeedMeter` 单测（注入假时钟）：稳定速率、变速、样本稀疏、零时长、窗口驱逐
- 回归：`test_sparse_task_tiles.py`、`test_task_lifecycle_state.py`、
  `test_contour_execute.py`、`test_fix_dem_*` —— 确认 3 参 / 4 参替身都没炸
- 冒烟：跑一个真实小任务，对照 MB/s 与 `N/M` 推进是否吻合；停掉网络看 5 秒内归零
