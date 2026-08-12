## v0.3.5 —— 磁盘空间不足不再拦任务：估算照给，启动照常，跑到底

**先说结论：这一版只有一处行为变化 —— 「预估下载量超过磁盘剩余空间」不再拒绝启动任务，也不再在任务跑到一半时把它自动暂停。磁盘估算本身完整保留：框选区域后仍显示「需要约 X、可用 Y」，每次判决的四个数字（可用 / 需要 / 保留 / 缺口）照样写进任务日志。没有数据库迁移，已下载的数据、配置、历史全部照旧。**

**磁盘不足时现在会发生什么**
- **启动照常**。此前点「开始」会被直接拒绝，必须先去腾空间；现在照常排队开工，判决数字记进任务日志（不通过是 warning，通过是 info）。
- **跑到底，不再中途自动暂停**。此前下载/渲染途中磁盘变紧，任务会被自动暂停并写一句原因；现在不再干预 —— 磁盘估算是估算，估错不该由任务陪葬。
- **真的写满盘时**，任务以 I/O 错误收场。此时打开任务日志，最后一条 `disk_recheck` 事件就是现场：「剩下的活要多少、盘上还剩多少、还差多少」，而不是一句没头没尾的 I/O error。

**保留的东西**
- 框选区域时的用量提示：「需要约 X，可用 Y」与不足时的「还差 Z」，一个数字没少。
- 配置页的「磁盘预算检查」开关还在，语义变成纯提示开关：关掉后不再弹「磁盘不足」提示，估算数字照常出。
- 多任务并发时，各自估算仍会先扣掉其他任务已预留的空间 —— 你看到的「可用」不是乐观值。

**给排障和构建的人**

- `disk_budget.RunningRecheck.blocking_verdict()` 改名 `poll()`，`blocked` 属性删除；它现在纯观测，返回值不该被用来叫停任何循环。四条管线的 5 处启动 `raise` 与 3 处「判死 → 落 paused」处理块全部移除，`task_manager._check_disk_admission` 随之改名 `_estimate_disk_verdict`。
- `DISK_BYTES` 调度器预留保留：没有它，并发任务看到的「可用」会各自偏乐观。

**验证**

- 全量测试 **2639 项通过 / 3 项跳过**（开发机 Linux）。`tests/test_disk_recheck_inflight.py` 按新语义重写：钉住「判负照常跑完、数字进日志」，旧的「判死落 paused」用例全部翻面。

---

## 通用说明

- **下载安装**：从下方 Assets 下载对应平台压缩包（`terraforge-windows.zip` / `terraforge-linux.tar.gz` / `terraforge-macos.tar.gz`），解压即用，无需安装 Python 环境。
- **下载体积**：每个平台仍包含 167 MB 的全球底图分卷（自 v0.2.8 起）。
- **首次运行**：启动可执行文件后，浏览器访问 http://localhost:5000 ；代理、并发、缓存管理等在「配置」页修改。程序另会监听 5001 出瓦片，不放行也能用。
- **许可证与第三方声明**：程序目录下的 `LICENSE`（MIT）与 `THIRD_PARTY_NOTICES.md`。MIT 只覆盖软件代码，**不授予**任何数据与在线服务的使用权。
- **历史版本**：完整更新历史见仓库 [CHANGELOG.md](https://github.com/JungleZy/TerraForge/blob/master/CHANGELOG.md)。
- **使用文档**：见仓库 [README.md](https://github.com/JungleZy/TerraForge/blob/master/README.md) 与 [docs/guides/QUICKSTART.md](https://github.com/JungleZy/TerraForge/blob/master/docs/guides/QUICKSTART.md)。
