# badges/ —— 两份 README 用的徽章与国旗

> **生成物，不是素材。** 事实源是 [`scripts/build_readme_badges.py`](../../../scripts/build_readme_badges.py)：改文字、配色、图标一律改那份清单再重跑，别用图像编辑器直接改 PNG，下一次重跑会把手改静默盖掉。

```bash
uv run python scripts/build_readme_badges.py    # 需要本机有 Chrome/Chromium
```

## 这是什么

[`README.md`](../../../README.md) 与 [`README.en.md`](../../../README.en.md) 里的全部**静态**徽章（页头、平台下载按钮、四条管线的数据源、技术栈、上游凭据表）和语言切换用的两张国旗。40 个文件，合计 74 KB。

落地成文件的理由：shields.io 与 flagcdn 都是第三方服务，停服 / 限流 / 被墙时 README 就是一排碎图（GitHub 的 camo 只是缓存，过期照样回源）。落地之后整份 README 除两个动态徽章外零外链 —— 与「前端第三方库全部本地 vendor，运行期不碰 CDN」同一条口径。

## 两个**故意留在远程**的徽章

| 徽章 | 为什么不烤成文件 |
|---|---|
| `img.shields.io/github/v/release/...` | 内容是当前版本号，烤下来就冻住：发了新版 README 还写着旧号 |
| `img.shields.io/github/actions/workflow/status/...` | 内容是构建状态，烤下来构建红了徽章还是绿的 |

冻结不是「少一个外链」，是让文档撒谎。这两个坏掉最多是碎图，不会给出错的事实。

## 生成参数

- **2× 光栅化**：Chrome 无头 `--force-device-scale-factor=2`，`--default-background-color=00000000` 给真透明底（圆角在 GitHub 深色主题下不露白角）。README 里用 `height="20"`（flat）/ `height="28"`（for-the-badge）指定逻辑尺寸；国旗用既有的 `width="18"`。
- **128 色量化**（Pillow `FASTOCTREE`，连半透明抗锯齿边缘一起量化）：徽章只有色块、白字与一层 10% 渐变，3× 放大与原图目视无差别，体积从 257 KB 降到 74 KB。
- **不用 cairosvg**：shields 的 SVG 靠 `textLength` 把文字拉到服务端算好的宽度，cairosvg 的 toy 字体 API 会逐字符挤压，中英文一律糊成墨团（「官网」两个汉字直接不可读）。

## 命名与语言

同一枚徽章中英两版文案相同的只烤一份；文案不同的带语言后缀，各 5 个：

| 只在 `README.md` | 只在 `README.en.md` |
|---|---|
| `website-zh` `xyz-tiles-zh` `download-{windows,macos,linux}-zh` | `website-en` `xyz-tiles-en` `download-{windows,macos,linux}-en` |

其余 30 个（`python` `license-mit` `os-*` `google-maps` `gdal` `flask` …）两份共用。

## 图标来源

来自 shields.io 的 simple-icons 集合（素材 CC0）。**Windows 那枚四窗格图标是脚本自绘的** —— simple-icons 已下架 Microsoft 全家，`logo=windows`、`logo=microsoft` 都返回无图标的徽章（`cdn.simpleicons.org/windows` 现在是 404）。

徽章里出现的名称与标识归各自权利人所有，仅用于标识对应的技术与数据源，不表示对方背书；见 [`THIRD_PARTY_NOTICES.md`](../../../THIRD_PARTY_NOTICES.md) §3 与 §6。
