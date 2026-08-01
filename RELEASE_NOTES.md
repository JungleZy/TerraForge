## v0.2.1 —— 瓦片下载提速 + 缓存上限生效 + 并发测速推荐

**下载性能**
- 修复 connector `limit_per_host` 恒为 4 的隐性瓶颈：4 台服务器最多 16 条连接，并发调到 20+ 也被压死。改为跟随 `concurrent_downloads`（与 DEM 引擎同款），实测同网络吞吐 5.0 → 11.7 块/秒。
- 默认并发 10 → 50（高延迟代理链路下吞吐近似线性增长，50 并发无 429）。

**瓦片缓存**
- `cache_max_size_mb` 配置此前没有任何消费方，瓦片 cache 可无限增长。现在每次任务下载结束后按 LRU 自动清理到上限内：最久未用先删；`cache/dem`、原子写临时件、一小时内新瓦片不动；上限设 0 = 不限制。

**界面**
- 配置页「并发下载数」旁新增「测速推荐」按钮：用已保存的瓦片服务器和代理实测 10/25/50 三档吞吐（约 30 秒），取膝点（达最高吞吐 90% 的最小并发）填入输入框，保存后生效；测速全失败回退保守值 20。

---

## 通用说明

- **下载安装**：从下方 Assets 下载对应平台压缩包（`terraforge-windows.zip` / `terraforge-linux.tar.gz` / `terraforge-macos.tar.gz`），解压即用，无需安装 Python 环境。
- **首次运行**：启动可执行文件后，浏览器访问 http://localhost:5000 ；代理、并发、缓存上限等在「配置」页修改。
- **历史版本**：完整更新历史见仓库 [CHANGELOG.md](https://github.com/JungleZy/map-download/blob/master/CHANGELOG.md)。
- **使用文档**：见仓库 [README.md](https://github.com/JungleZy/map-download/blob/master/README.md) 与 [docs/QUICKSTART.md](https://github.com/JungleZy/map-download/blob/master/docs/QUICKSTART.md)。
