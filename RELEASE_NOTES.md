## v0.2.3 —— 历史预览全链路修复 + 保存路径绝对化与目录浏览

**任务预览（修「只定位到区域、看不到内容」）**
- `image_only`（含遗留 png/jpg）任务的瓦片现在也会复制到产物目录——此前只拼接不出瓦片，已完成任务预览空白。UI 的「仅拼接图」选项随之移除（与「瓦片+拼接图」产出一致，后端枚举仍兼容存量任务）。
- DEM 地形预览按任务保存路径服务：自定义保存路径（默认 `<下载目录>/dem` 之外）的切片此前读不到，预览必 404。dem 瓦片路由同时补上任务存在性校验，删除任务（保留文件）后瓦片 URL 立即失效，与其他三路一致。
- 没有地形切片的 dem / 本地高程任务：预览不再只定位——后端按需把源 DEM（`*_dem.tif`）渲染成晕渲图叠加显示（VRT 马赛克 → hillshade，限宽 1600px，磁盘缓存，首次约秒级）。
- local_terrain 预览会用 layer.json 的 `valid_bounds` 飞到数据区（此前相机不动）；等高线预览面板同样带 bbox 定位。
- 删除正在预览的任务时，前端联动关闭预览（不再残留空白叠加层和「预览中」提示条）。

**保存路径与目录选择**
- 建任务/默认保存路径只接受绝对路径（相对值拒绝并指路「浏览」按钮）；存量相对 default_save_path 启动时一次性归一为绝对值。
- 新增目录浏览弹窗：任务表单与配置页的保存路径都可以点「浏览」选择（`GET /api/fs/browse`，仅限下载根目录内）。
- 下载类型、输出格式等下拉框改为单选组，选择更直观。

---

## 通用说明

- **下载安装**：从下方 Assets 下载对应平台压缩包（`terraforge-windows.zip` / `terraforge-linux.tar.gz` / `terraforge-macos.tar.gz`），解压即用，无需安装 Python 环境。
- **首次运行**：启动可执行文件后，浏览器访问 http://localhost:5000 ；代理、并发、缓存上限等在「配置」页修改。
- **历史版本**：完整更新历史见仓库 [CHANGELOG.md](https://github.com/JungleZy/map-download/blob/master/CHANGELOG.md)。
- **使用文档**：见仓库 [README.md](https://github.com/JungleZy/map-download/blob/master/README.md) 与 [docs/QUICKSTART.md](https://github.com/JungleZy/map-download/blob/master/docs/QUICKSTART.md)。
