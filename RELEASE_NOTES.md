## v0.2.4 —— 下载后阶段可见（修「卡 100%」）+ 边下边复制 + 保存目录全盘可选

**下载后阶段不再像卡死**
- 下载到 100% 后还要走拼接/复制，此前任务行只显示「已下载 N/N」，大任务一停就是几十分钟到几小时，看起来像卡死。现在任务行实时显示「拼接中（zoom x）…」「复制瓦片中 n/m …」，拼接每个 zoom 开始时就有事件（旧版只在拼完才发，大单层期间连状态栏心跳都没有）。

**边下边复制**
- 瓦片落 cache 成功后立即镜像到产物目录（下载回调即时复制）；cache 命中的瓦片（续跑/重复选区）由独立补拷线程与下载并行复制。产物目录 = 已下载内容的实时镜像，下载结束 ≈ 产物就绪，结尾复制退化为秒级对账。取消任务的产物目录保留已下载部分（与 cache 状态一致）。

**保存目录全盘可选**
- 建任务/默认保存路径接受任意绝对路径（不再限制在下载根目录内），「浏览」弹窗全盘可选（Windows 根级列盘符）。底线：相对路径拒绝、深度不足两级（根目录/盘符根）拒绝。
- 删除产物（`delete_files=true`）对全盘路径同样生效，护栏：符号链接任一层、浅层路径、家目录本身、下载根或其祖先、cache 相关目录一律拒删。
- 全盘任务的预览（瓦片/地形/晕渲）与拼接输出白名单同步放开。

**缓存管理重做**
- 配置页新增「缓存管理」：按分类（各瓦片 style / DEM 缓存）查看占用与文件数，可单类或全部手动清理（二次确认）。此前的自动 LRU 上限清理移除，缓存不再自动删除，全由用户掌控。

---

## 通用说明

- **下载安装**：从下方 Assets 下载对应平台压缩包（`terraforge-windows.zip` / `terraforge-linux.tar.gz` / `terraforge-macos.tar.gz`），解压即用，无需安装 Python 环境。
- **首次运行**：启动可执行文件后，浏览器访问 http://localhost:5000 ；代理、并发、缓存管理等在「配置」页修改。
- **历史版本**：完整更新历史见仓库 [CHANGELOG.md](https://github.com/JungleZy/map-download/blob/master/CHANGELOG.md)。
- **使用文档**：见仓库 [README.md](https://github.com/JungleZy/map-download/blob/master/README.md) 与 [docs/guides/QUICKSTART.md](https://github.com/JungleZy/map-download/blob/master/docs/guides/QUICKSTART.md)。
