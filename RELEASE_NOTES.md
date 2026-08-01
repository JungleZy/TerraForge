## v0.2.2 —— 记录面板重开刷新 + 测速推荐按钮紧凑化

**界面修复**
- 新建任务后记录面板看不到任务：面板时间流此前只在首次打开时拉取，重开停在旧内容上（新建的 pending 任务没有进度事件可触发实时插入，若先前还点过「进行中」筛选则显示为空列表）。现在每次重开记录面板都会重新拉取时间流和统计，保留当前页码与筛选。
- 配置页「测速推荐」按钮过大、与整体风格不协调：改用与「验证」按钮同款的紧凑样式（28px 高度令牌、小字号），说明文字独立一行。

---

## 通用说明

- **下载安装**：从下方 Assets 下载对应平台压缩包（`terraforge-windows.zip` / `terraforge-linux.tar.gz` / `terraforge-macos.tar.gz`），解压即用，无需安装 Python 环境。
- **首次运行**：启动可执行文件后，浏览器访问 http://localhost:5000 ；代理、并发、缓存上限等在「配置」页修改。
- **历史版本**：完整更新历史见仓库 [CHANGELOG.md](https://github.com/JungleZy/map-download/blob/master/CHANGELOG.md)。
- **使用文档**：见仓库 [README.md](https://github.com/JungleZy/map-download/blob/master/README.md) 与 [docs/QUICKSTART.md](https://github.com/JungleZy/map-download/blob/master/docs/QUICKSTART.md)。
