# 第三方组件声明 / Third-Party Notices

TerraForge 本体采用 MIT（见根目录 [`LICENSE`](LICENSE)）。本文件列出随本项目分发的第三方组件及其许可证。

分发形态有两种，义务范围不同：

| 形态 | 内容 | 覆盖章节 |
|---|---|---|
| **源码分发**（Git 仓库） | `static/vendor/` 下的前端组件、`assets/terrain/` 下的地形数据、`docs/assets/badges/` 下的文档徽章 | §1、§2、§3 |
| **二进制分发**（Nuitka standalone 包） | 前两项，外加 Python 运行时依赖与它们链接的原生库（徽章不入包：发行产物不带 README 与 `docs/`） | §1、§2、§4 |

许可证全文的存放约定：**能随组件落地的就落在组件目录旁**，本文件只做索引与说明。仅在无法落地时（原生库藏在 wheel 里、数据集只有使用条款）才在此转述。

---

## 1. 前端组件（`static/vendor/`）

全部为离线内嵌，运行期不联网取用。引用点见 `templates/base.html`。

| 组件 | 版本 | 许可证 | 全文 |
|---|---|---|---|
| CesiumJS | 1.143.0 | Apache-2.0（**另含分别授权的第三方部分**） | [`static/vendor/cesium/1.143.0/LICENSE.md`](static/vendor/cesium/1.143.0/LICENSE.md) |
| Bootstrap（CSS + JS bundle） | 5.3.0 | MIT | [`static/vendor/bootstrap/5.3.0/LICENSE`](static/vendor/bootstrap/5.3.0/LICENSE) |
| Popper.js（`@popperjs/core`） | 随 Bootstrap 5.3.0 bundle 内置 | MIT | 见下方说明 |
| Vue.js | 3.5.13 | MIT | [`static/vendor/vue/3.5.13/LICENSE`](static/vendor/vue/3.5.13/LICENSE) |
| Socket.IO Client（含内联的 Engine.IO Client） | 4.5.4 | MIT | [`static/vendor/socket.io/4.5.4/LICENSE`](static/vendor/socket.io/4.5.4/LICENSE) |
| Inter | Google Fonts css2 快照（400/500/600/700） | SIL OFL 1.1 | [`static/vendor/fonts/LICENSE-Inter.txt`](static/vendor/fonts/LICENSE-Inter.txt) |
| JetBrains Mono | Google Fonts css2 快照（400/600） | SIL OFL 1.1 | [`static/vendor/fonts/LICENSE-JetBrainsMono.txt`](static/vendor/fonts/LICENSE-JetBrainsMono.txt) |

### CesiumJS —— 注意「Portions licensed separately」

`Cesium.js` 头部的 `@license` 块声明 Apache-2.0，并附一句 `Portions licensed separately`。上游 `LICENSE.md` 的 `# Third-Party Code` 一节逐条列出了被打包进发行版的第三方代码（Sean O'Neil 的大气散射、zip.js、Autolinker.js、tween.js、Knockout、Draco、earcut、KTX2/basis、topojson、protobufjs、meshoptimizer 等）。该文件已**原样落到组件目录**，请以它为准，不要以本表的一行「Apache-2.0」为准。

Cesium 同时声明了专利：`Columbus View (Pat. Pend.)`、`Patents US9153063B2 US9865085B1 US10592242`、`Patents pending US15/829,786 US16/850,266 US16/851,958`。原文见落地的 `LICENSE.md`。

### Popper.js —— 无独立版权头

`bootstrap.bundle.min.js` 按定义是 Bootstrap + Popper 的合并产物（`templates/base.html:203` 的注释亦如此写），但该文件头部**只有 Bootstrap 自己的 `@license` 块**。Popper 是独立版权人（Federico Zivolo）与独立包，不被 Bootstrap 的声明覆盖。

> **待补**：Popper 的确切版本与版权行在本仓内无证据。Bootstrap 5.3.0 的上游依赖为 `@popperjs/core ^2.11.8`；升级 Bootstrap 或做正式发行前，应按实际内置版本补上其 MIT 声明。

### 字体子集说明

`static/vendor/fonts/fonts.css` 是本项目**手写**的 `@font-face` 表，不是上游产物，无需署名。上游 40 个 `@font-face` 指向 13 个 woff2，本副本只保留 latin / latin-ext 两个子集（4 个文件）。

woff2 为二进制子集且元数据已剥离，仓库内无从证实其许可证；上面两份 OFL 全文取自各自上游仓库（Inter → `rsms/inter`，JetBrains Mono → `JetBrains/JetBrainsMono`）。OFL 1.1 要求保留版权声明与许可证全文，且**禁止单独售卖字体文件**、要求衍生字体改名。

---

## 2. 随仓库分发的数据集

### 全球基础地形 `base_z8`（GEBCO 2024 派生品）

- 文件：`assets/terrain/base_z8.tar.gz.partaa`、`base_z8.tar.gz.partab`（合计约 167 MB）
- 来源：**GEBCO 2024 Grid**（15 弧秒，含海底地形，全球无洞），经本项目切片为 quantized-mesh 地形瓦片
- 构建记录：`docs/reference/terrain/global-base-build.md`
- 运行期由 `base_terrain.ensure_base_unpacked()` 解压，并**植入每个地形任务的输出目录**，因此会随用户产物二次分发

GEBCO Grid 置于公共领域、可免费使用，但**要求注明来源**。使用本项目产出的地形成果时，请一并保留：

> GEBCO Compilation Group (2024) GEBCO 2024 Grid.

### 运行期下载的数据（不随本仓分发）

Copernicus DEM GLO-30、ASTER GDEM v3、各家瓦片底图由用户在运行期自行获取，其许可证、配额与服务条款义务在使用者一侧。本项目不内置也不代为授权。详见 `README.md` 的免责声明一节。

---

## 3. 文档里的徽章图标（`docs/assets/badges/`）

两份 README 的徽章与国旗是**烤成 PNG 入库**的，不在运行期从第三方取（理由与生成参数见 [`docs/assets/badges/README.md`](docs/assets/badges/README.md)，生成脚本 `scripts/build_readme_badges.py`）。它们随 Git 仓库分发，不进二进制产物。

| 内容 | 来源 | 许可 |
|---|---|---|
| 徽章底板与文字 | shields.io 按 URL 渲染 | 生成图形，上游不主张版权 |
| 徽章内的品牌图标 | shields.io 内置的 simple-icons 集合 | CC0-1.0（**仅素材文件**） |
| 中国 / 英国国旗 | flagcdn.com | 国旗图案属公共领域 |
| Windows 四窗格图标 | 本项目自绘（`WIN_LOGO_SVG`，因 simple-icons 已下架 Microsoft 全家） | 图形随本项目 MIT；Windows 商标权归 Microsoft |

simple-icons 的 CC0 覆盖的是素材文件，**不覆盖商标本身**。Google、NASA、Apple、Linux、Microsoft、Esri、OpenStreetMap、Copernicus、Cesium、Flask、SQLite、Bootstrap、Vue、pytest、uv 等名称与标识归各自权利人；徽章仅用于标识 TerraForge 对接的技术与数据源，不表示对方背书或存在合作关系。另见 §6。

---

## 4. Python 运行时依赖

仅在**二进制分发**（Nuitka standalone）时构成分发义务；从源码运行时这些包由使用者自行安装，义务在使用者一侧。版本以 `requirements.txt` 为准。

| 包 | 版本 | 许可证 | 备注 |
|---|---|---|---|
| Flask | 2.3.3 | BSD-3-Clause | |
| Flask-SocketIO | 5.3.4 | MIT | |
| python-socketio | 5.9.0 | MIT | 传递依赖 `bidict` 为 **MPL-2.0** |
| python-engineio | 4.7.1 | MIT | |
| simple-websocket | 1.1.0 | MIT | 传递依赖 `wsproto`、`h11` 均为 MIT |
| aiohttp | 3.9.1 | Apache-2.0 | 见下方 NOTICE 义务 |
| aiofiles | 23.2.1 | Apache-2.0 | 上游带 `NOTICE` 文件 |
| certifi | >=2024.2.2 | MPL-2.0 | 见下方弱 copyleft 说明 |
| GDAL（Python 绑定） | >=3.8,<4 | MIT | 见下方原生库说明 |
| NumPy | 1.26.4 | BSD-3-Clause | 内置 OpenBLAS；`numpy/random` 另有独立许可证 |
| Matplotlib | 3.11.1 | Matplotlib License（PSF 派生，BSD 兼容） | 见下方字体义务 |
| Pillow | 10.1.0 | HPND | 内置多个原生图像库 |
| Nuitka | 4.1.3 | AGPL-3.0 + Runtime Library Exception | 见下方专门说明 |
| pytest | 7.4.3 | MIT | **仅测试期**，不进任何发行产物 |

### Apache-2.0 的 NOTICE 义务

Apache-2.0 第 4(d) 条要求转录上游 `NOTICE` 文件。`aiofiles` 自带 `NOTICE`；`aiohttp` 的 vendored 加速扩展中 `yarl`、`propcache` 各自带 `NOTICE`。

> **待补**：这三份 NOTICE 的正文需在打包发行流程中从已安装的 `dist-info` 抽取并随包附上。当前仓库不内嵌它们（`.venv/` 不入库），源码分发不受影响。

### certifi —— 唯一的弱 copyleft 依赖

MPL-2.0 要求以源码形式提供被修改文件，且不能仅列名字，必须给出许可证全文或其获取地址：<https://www.mozilla.org/en-US/MPL/2.0/>。本项目未修改 certifi，仅原样依赖；它分发的是 Mozilla CA 根证书库（`cacert.pem`），数据本身同样在 MPL-2.0 下。

### GDAL —— 最重的原生依赖

Python 绑定本身是 MIT（版权人 Frank Warmerdam、Howard Butler、Even Rouault），但它链接的 `libgdal` 聚合了数十个不同许可证的驱动与第三方库（PROJ、GEOS、libtiff、libgeotiff、libjpeg、libpng、zlib、expat、SQLite、OpenJPEG、netCDF、HDF5、Xerces 等），上游源码树的 `LICENSE.TXT` 是一份逐组件清单。

`nuitka_build.py` / `src/core/bundle.py` 会把 GDAL 与 PROJ 打进发行包，因此**对二进制发行这是硬义务**。

> **待补**：按实际打包进产物的 `libgdal` 版本，从上游取回 `LICENSE.TXT` 并随包附上。

### Matplotlib —— 被打包脚本放大的字体义务

`requirements.txt` 的注释写明打包时用 `--include-package-data=matplotlib` 把 `mpl-data` 一起打进产物，而 `mpl-data/fonts/ttf/` 下带有两份独立许可证：`LICENSE_DEJAVU`（DejaVu，Bitstream Vera 派生）与 `LICENSE_STIX`（STIX）；另有 Computer Modern（`cm*.ttf`）、`LastResortHE-Regular.ttf`（Unicode Inc.）、`pdfcorefonts/*.afm`（Adobe Core 字体度量）与 `afm/*.afm`（URW 度量）。

> **待补**：二进制发行时随包附上 `LICENSE_DEJAVU` 与 `LICENSE_STIX`。

### NumPy / Pillow 的内置原生库

- NumPy 的 wheel 内置 **OpenBLAS**（BSD-3-Clause，静态链接 LAPACK 与 GFortran runtime）；`numpy/ma`、`numpy/random`（randomkit / Mersenne Twister / PCG64）各有独立许可证文件。
- Pillow 的 wheel 内置 libjpeg-turbo、zlib、libtiff、libwebp、libopenjp2、freetype、littlecms2、libxcb —— 各有独立许可证（IJG、zlib、BSD 类、FTL/GPLv2 双授权、MIT 等）。Pillow 自身的 `LICENSE` 同时含 PIL 原作者（Secret Labs AB / Fredrik Lundh）与 Pillow fork（Jeffrey A. Clark）两段版权，不能只保留一段。

### Nuitka —— AGPL 但不传染

Nuitka 编译器本体是 AGPL-3.0，**但这不影响 TerraForge 采用 MIT**，理由有二：

1. Nuitka 是**构建期**工具，不被链接进 TerraForge 的源码分发；
2. 其 `LICENSE-RUNTIME.txt` 是 AGPLv3 第 7 条下的附加许可，明文允许「编译非 AGPL（含专有）Python 程序并使用其头文件与运行时库，**而不要求最终可执行文件按 AGPLv3 授权**」。

产物中确实含 Nuitka 的静态 C 代码与运行时，因此其 `NOTICE.txt` 属于必须转录的内容。Nuitka 自身在 `nuitka/build/inline_copy/` 下还 vendored 了 markupsafe、jinja2、zlib、yaml、python_hacl、appdirs、zstd 等，若进入产物则同样需要署名。

> **待补**：二进制发行时随包附上 Nuitka 的 `NOTICE.txt` 与 `LICENSE-RUNTIME.txt`。

---

## 5. 本项目自有代码中的算法出处

以下均为本项目独立实现，**无法律署名义务**，列出仅为消除溯源疑问。

- **`src/services/terrain_tiling/rtin.py`** —— RTIN 自适应三角网的 numpy 实现（约 160 行）。算法出自 Evans et al. 1997，Mapbox Martini 是其广为人知的现代实现（ISC）。本文件不含任何上游代码，是按算法描述独立编写的；设计决策记录在 `docs/superpowers/specs/2026-08-04-terrain-triangulation-design.md`。
- **`src/services/terrain_tiling/cesium_terrain.py`** —— quantized-mesh 1.0 编码器与切片流程。该文件有 vendored 起点，处置过程与当前状态记录在 [`docs/reference/cesium-terrain-provenance.md`](docs/reference/cesium-terrain-provenance.md)，发行前请先读那份文档。
- **`tests/test_terrain_normals.py`** 中的 `_cesium_oct_encode_scalar` 是 CesiumJS `AttributeCompression.octEncodeInRange` 的逐字转写，用于逐字节等价断言。位于测试代码、不进发行产物；出处注释已就地标注，许可证为 Apache-2.0（见 §1）。

---

## 6. 商标

本文件授予的是各组件的**代码**许可，不包含名称、Logo 与品牌资产的使用许可。Cesium、Bootstrap、Vue、Socket.IO、JetBrains、GEBCO 等名称与标识归各自权利人所有。README 徽章里出现的其余商标（Google、NASA、Apple、Linux、Microsoft、Esri、OpenStreetMap、Copernicus 等）同理，见 §3。
