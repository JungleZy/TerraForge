# diagrams/ —— 两个 README 的七张示意图

> **状态：当前实现。** 画的是架构、流程与状态机，不是界面截图 —— 界面改了不用动它，**管线契约、端口划分、`TaskOutcome`/`TaskState` 词表、切片顺序或插件扩展点改了才要改**。中文版在本目录，英文版在 [`en/`](en/)，两套**坐标逐像素相同**，只有文字不同（改图时两边一起改，否则会漂）。

## 这是什么

| 图源（事实源） | 渲染产物 | 画的是什么 | 类型 |
|---|---|---|---|
| `architecture{,-dark}.html` | `architecture*.png` 2560×1600 | 组件与端口：浏览器三条入口（`:5000` / `:5001` / `/basemap`）、四条管线管理器与插件宿主、全局调度与预算、引擎、SQLite、磁盘产物、外部上游 | 架构 |
| `pipelines{,-dark}.html` | `pipelines*.png` 2560×1280 | 四条管线的泳道：每条的输入、处理、产物与服务端点，以及唯一的跨管线交接（DEM 产物被地形 / 等高线复用） | 泳道 |
| `tile-request{,-dark}.html` | `tile-request*.png` 2560×1760 | 一张瓦片的两条路：缓存命中走 backfill 线程补拷，未命中过信号量取上游 → 魔数校验 → 写缓存 → 镜像；浏览器再经瓦片端口读产物 | 时序 |
| `terrain-tiling{,-dark}.html` | `terrain-tiling*.png` 2560×2256 | 地形切片流水线八步，含两处反直觉：多幅必须物化成单幅 GeoTIFF、底座切完才植入 | 流程 |
| `task-state{,-dark}.html` | `task-state*.png` 2560×1056 | `TaskState` 八个状态与十二条转移，补漏可从三个状态发起 | 状态机 |
| `tile-gaps{,-dark}.html` | `tile-gaps*.png` 2560×1632 | `TileOutcome` 五分类记账与「只有 `no_data` 已解释」那处不对称，以及补漏 / 接受缺口两条出路 | 流程 |
| `plugins{,-dark}.html` | `plugins*.png` 2560×1600 | 插件的三道准入闸、四个扩展点与运行期唯一门面 `TaskContext` | 架构 |

**HTML 是事实源，PNG 是产物。** 改图请改 HTML（单文件、内联 SVG + CSS、无外部请求），再重渲 PNG。深浅两版是两份文件，README 用 `<picture>` + `prefers-color-scheme` 选一份 —— GitHub 两种主题下都不会出现白板或黑板。

## 重渲

```bash
python3 docs/assets/diagrams/render.py                 # 全部 28 张（7 图 × 中英 × 深浅）
python3 docs/assets/diagrams/render.py task-state      # 只渲这一组（中英、深浅共 4 张）
```

脚本递归扫本目录与 `en/`，抠出 HTML 里的第一个 `<svg>`、按 `viewBox` 定尺寸、用无头 Chrome 出 @2x 图，**不含**页面上的标题与页脚（那些话 README 正文已经说了）。

## 三条约定

- **配色取自应用自己的 token**，不是绘图工具的默认皮肤：`--color-bg-primary` `#0c0d10`、`--color-accent` `#38bdf8`（浅色版换成同色系的 `#0284c7` 以满足非文本 3:1 对比）、文字色取 `--color-text-*`。改了 `static/css/style.css` 的品牌色，这里也该跟。
- **强调色每张图只给 1–2 个元素**。七张图的焦点依次是：全局调度与预算、`quantized-mesh` 切片、镜像到产物那两条消息、合并物化为单幅 GeoTIFF、`pending_decision`、待决策、`TaskContext`。再多几处就没有焦点了。
- **中文只出现在 12px 的名称与 10px 的图例里**，箭头标注一律 ASCII 等宽（`QUOTA`、`NO_DATA ONLY`）—— 规范字号 8px，中文在 10px 以下会糊。

仓库根的 `.diagram-design`（内容 `profile: terraforge`）是给 `diagram-design` 技能用的皮肤选择器，profile 本体在各人自己的 `~/.diagram-design/profiles/terraforge.md`。没有这份 profile 时技能会问你要用哪套皮肤 —— 那正是想要的行为：别让默认皮肤的图混进来。照上面那条「配色取自应用 token」重建一份即可。

字体走本仓 vendor 的 Inter / JetBrains Mono（`@font-face` 相对路径指向 `static/vendor/fonts/`，符合仓库的离线不变量）；两者都不含 CJK，中文字形按系统回退，渲染机器需要装 Noto Sans CJK 一类的字体。
