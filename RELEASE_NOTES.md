## v0.3.2 —— 补齐许可证与第三方声明；地形切片器内部重写，产物逐字节不变

**先说结论：这一版程序功能一处没动，不必重做任何东西 —— 已下载的瓦片、已切好的地形、配置与历史全部照旧。** 它做的是两件此前欠着的事：把 MIT 许可证和第三方组件声明真正补齐（此前 README 徽章声明了 MIT，仓库里却没有 LICENSE 文件），以及把地形切片器里一段来源不清的代码重写掉。**地形切片的输出经过逐字节比对，与上一版完全相同**，所以已有地形瓦片不失效、不必重切。

**发行包里现在带着许可证了**
- 解压后能在程序目录里看到 `LICENSE`（MIT）与 `THIRD_PARTY_NOTICES.md`。此前两个都没有 —— 程序内嵌了 CesiumJS（Apache-2.0，要求随附许可证与 NOTICE）、Inter 与 JetBrains Mono 字体（OFL 1.1，要求随附全文）、随包的 167 MB 全球底图（GEBCO 2024 派生品，要求署名），以及一批带原生库的 Python 依赖，而这些声明一个都没跟着发出来。
- 打包脚本此前**根本不会**把根目录的许可证收进产物，等于写了也白写。现在它们被列进构建期的必需文件清单，漏收会让构建直接失败，而不是静默发出一个缺声明的包。
- **如果你在二次分发这个程序，或者分发它产出的地形成果**，请读一下 `THIRD_PARTY_NOTICES.md`。其中最容易漏的一条：随包的全球底图来自 GEBCO 2024，使用它出的成果需要保留一句 `GEBCO Compilation Group (2024) GEBCO 2024 Grid.`；而它会被自动植入每一个地形任务的输出目录，也就是说你拷给别人的任务目录里就有它。
- `static/vendor/` 下五个内嵌前端组件此前一个许可证文件都没有，现已从各自上游取回全文放在组件目录旁。

**地形切片器重写了一部分内部实现（对你没有任何影响）**
- 这个文件当初是从一个第三方产品的安装目录里取来的，仓库里没有留下任何授权凭据。这一版把其中确实属于原创表达的部分重写掉了：与原件逐字重合的代码从 210 行压到 125 行，最大连续相同段落从 44 行压到 10 行，剩下的都是 quantized-mesh 规范规定的编码方式、标准大地测量公式和函数签名这类换不掉的东西。
- **为什么可以放心升级**：重写前后用合成 DEM 跑了 5 组切片配置（两种三角化后端、开关顶点法线、带层级偏移），共 **2396 个产物**逐个比对 SHA-256，**全部逐字节相同**；采样器与切片方案也单独取证对账。全量测试 2263 项通过 / 3 项跳过，与改动前逐项一致。
- **需要如实告知的一点**：那个 vendored 起点的授权状态**没有取得书面确认**，本版的决定是维持现状（继续随 MIT 分发）。这是一个被明确记录下来的已知风险，来龙去脉、逐块判定理由与后续选项都写在仓库的 [`docs/reference/cesium-terrain-provenance.md`](https://github.com/JungleZy/TerraForge/blob/master/docs/reference/cesium-terrain-provenance.md) 里。如果你要把本程序用于商业发行或需要做许可证尽调，请先读它。

**给排障和构建的人**
- 构建期新增四个必需文件哨兵：根目录 `LICENSE` / `THIRD_PARTY_NOTICES.md`、`static/vendor/cesium/*/LICENSE.md`、`static/vendor/fonts/LICENSE-Inter.txt`。这是那份哨兵列表里唯一一组**法律**义务 —— 漏收之后程序功能完全正常，只是每一份发出去的拷贝都缺了它必须携带的声明，没有任何运行期信号，只能在构建期挡。
- `CLAUDE.md` 里关于切片器来源的描述此前有两处错：把一个商业产品误作 CesiumJS，且与重写后的现状不符，已改正并指向溯源文档。
- `tests/test_terrain_normals.py` 里有一段是 CesiumJS 算法的逐字转写（用于逐字节等价断言，不进发行产物），已就地标注 Apache-2.0 出处。

**验证**
- 全量测试 **2263 项通过 / 3 项跳过**（开发机 Linux；跳过的只在特定平台上有意义），与改动前基线逐项相同。
- 地形产物字节对账见上；应用真实启动、各接口返回正常。
- 打包参数不是靠猜的：读了 Nuitka 4.1.3 的 `IncludedDataFiles.py`，确认单文件 `SRC=DEST` 形式落到产物根目录，且默认忽略清单（`py.typed` / `.DS_Store` / 代码类扩展名）不会吞掉 `LICENSE`、`.md` 与 `.txt`。

---

## 通用说明

- **下载安装**：从下方 Assets 下载对应平台压缩包（`terraforge-windows.zip` / `terraforge-linux.tar.gz` / `terraforge-macos.tar.gz`），解压即用，无需安装 Python 环境。
- **下载体积**：每个平台仍包含 167 MB 的全球底图分卷（自 v0.2.8 起）。
- **首次运行**：启动可执行文件后，浏览器访问 http://localhost:5000 ；代理、并发、缓存管理等在「配置」页修改。程序另会监听 5001 出瓦片，不放行也能用。
- **许可证与第三方声明**：程序目录下的 `LICENSE`（MIT）与 `THIRD_PARTY_NOTICES.md`。MIT 只覆盖软件代码，**不授予**任何数据与在线服务的使用权。
- **历史版本**：完整更新历史见仓库 [CHANGELOG.md](https://github.com/JungleZy/TerraForge/blob/master/CHANGELOG.md)。
- **使用文档**：见仓库 [README.md](https://github.com/JungleZy/TerraForge/blob/master/README.md) 与 [docs/guides/QUICKSTART.md](https://github.com/JungleZy/TerraForge/blob/master/docs/guides/QUICKSTART.md)。
