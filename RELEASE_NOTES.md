## v0.2.7 —— 只整理了源码目录，程序行为与 v0.2.6 完全一致

**先说结论：已经在用 v0.2.6 的不需要升级。** 这一版没有加功能、没有修 bug，三个平台的可执行文件与 v0.2.6 逐项相同。改的是仓库里源码的摆放位置，只影响改代码的人。

**源码集中到 src/**
- 此前根目录平铺着 `core/`、`models/`、`routes/`、`services/` 四个包，与模板、静态资源、文档、脚本、配置文件混在同一层。现在四个包整体移入 `src/`（43 个文件、13405 行），根目录只剩 `app.py` 与打包驱动 `nuitka_build.py` 两个 Python 入口。
- 导入写法随之改变：`from core.config import Config` → `from src.core.config import Config`。
- 刻意没有采用「标准 src 布局 + 可编辑安装」：那条路依赖 PEP 660 的导入钩子，而打包用的 Nuitka 做的是静态分析，跟不住这类钩子。改为让 `src/` 自身就是一个可导入包 —— 根目录本来就在模块搜索路径上，于是打包配置、CI、测试三边一行都不用改。

**这次改动的风险集中在哪**
- 真正有语义的改动只有一处：配置里推导项目根目录的层级要跟着加一层。少加一层程序照样启动，但会把数据库、下载目录、缓存目录建到 `src/` 里面 —— 等于换了一份空数据，且不抛任何异常。
- 另有三类是机械替换抓不到、必须手改的：subprocess 内嵌代码串里的 import、包对象的裸属性链、以及测试隔离工具里不带点的裸包名。

**验证**
- 968 项测试全部通过（迁移前后同一数字）。
- 打包链未改动，Linux 本地实测：Nuitka 完整解析出源码包，产物启动后首页返回 200。

---

## 通用说明

- **下载安装**：从下方 Assets 下载对应平台压缩包（`terraforge-windows.zip` / `terraforge-linux.tar.gz` / `terraforge-macos.tar.gz`），解压即用，无需安装 Python 环境。
- **首次运行**：启动可执行文件后，浏览器访问 http://localhost:5000 ；代理、并发、缓存管理等在「配置」页修改。
- **历史版本**：完整更新历史见仓库 [CHANGELOG.md](https://github.com/JungleZy/map-download/blob/master/CHANGELOG.md)。
- **使用文档**：见仓库 [README.md](https://github.com/JungleZy/map-download/blob/master/README.md) 与 [docs/guides/QUICKSTART.md](https://github.com/JungleZy/map-download/blob/master/docs/guides/QUICKSTART.md)。
