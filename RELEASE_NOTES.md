## v0.2.6 —— 只改了构建流水线，程序本身与 v0.2.5 完全一致

**先说结论：已经在用 v0.2.5 的不需要升级。** 这一版没有动任何应用代码，三个平台的可执行文件与 v0.2.5 功能逐项相同，只是打包它们的 CI 变快了。

**发布构建从 41 分钟压到十几分钟**
- 此前每次构建，三个平台都要把 Nuitka 生成的 690~705 个 C 文件从零编译一遍，而这些文件（绝大多数来自 numpy/matplotlib/Pillow/aiohttp/Flask 这些不随版本变化的第三方库）在两次构建之间逐字节相同。日志里三条证据摆着：Linux 直接警告「没有在用 ccache」，macOS 是 690 个文件全部 cache miss，Windows 是 705 个文件 0 命中。
- 原因是编译缓存目录跟着 CI 机器一起销毁，每次都是空的。现在把它固定下来并跨构建保留，重复的部分直接跳过。实测耗时构成：Windows 41 分钟里有 34 分钟是 Nuitka（其中纯 C 编译 28.7 分钟），Linux/macOS 19 分钟里有 16~17 分钟是 Nuitka。
- 顺带修掉一个会让上面这件事白做的坑：GitHub 的构建缓存按分支/标签隔离，标签上写的缓存下一个标签读不到，因此发布构建自己永远热不起来，需要在主分支上预先铺一次。

**日常提交的反馈变快**
- 推送后测试与打包拆成两步：测试只要 1 分钟量级，此前却要等打包跑完十几分钟才知道挂没挂，现在测试挂了两分钟内就报红。「测试不过就不打包」的约束不变。
- 同一分支上新提交到来时，自动取消还在跑的上一次构建（结果已经没人看，却占满十几分钟机时）。发布构建不受此影响——它一旦开始就不会被中途取消，避免产生只挂了部分平台产物的残缺 Release。

---

## 通用说明

- **下载安装**：从下方 Assets 下载对应平台压缩包（`terraforge-windows.zip` / `terraforge-linux.tar.gz` / `terraforge-macos.tar.gz`），解压即用，无需安装 Python 环境。
- **首次运行**：启动可执行文件后，浏览器访问 http://localhost:5000 ；代理、并发、缓存管理等在「配置」页修改。
- **历史版本**：完整更新历史见仓库 [CHANGELOG.md](https://github.com/JungleZy/map-download/blob/master/CHANGELOG.md)。
- **使用文档**：见仓库 [README.md](https://github.com/JungleZy/map-download/blob/master/README.md) 与 [docs/guides/QUICKSTART.md](https://github.com/JungleZy/map-download/blob/master/docs/guides/QUICKSTART.md)。
