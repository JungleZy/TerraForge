# site/ —— TerraForge 官网

线上地址：<https://terraforge-gis.pages.dev/>

一个静态单页站，**没有构建步骤**。`site/` 目录里是什么，线上就是什么。

## 为什么不用框架

本仓库是纯 Python 项目，没有 Node 工具链。为一个落地页引入 Next/Astro 等于额外养一套 `node_modules` 与构建流水线，而这个页面用不上它们提供的任何能力。字体与样式全部本地 vendor，不依赖 CDN —— 与主应用 `static/vendor/` 的既有约定一致。

## 目录

```
site/
├── index.html            # 整个页面（含滚动揭示脚本）
├── _headers              # Cloudflare Pages 响应头（缓存策略与安全头）
├── robots.txt
├── sitemap.xml
└── assets/
    ├── style.css         # 全部样式；设计 token 在文件顶部，动效在文件末尾
    ├── i18n.js           # 中英文案字典 + 语言判定/切换（133 key）
    ├── terrain-fx.js     # 地形场动效：全屏背景 + 首屏项目名
    ├── favicon.ico       # 取自 static/img/favicon.ico
    ├── fonts/            # Inter + JetBrains Mono，从 static/vendor/fonts/ 复制
    └── img/              # 界面截图（webp）与社交卡片（og.jpg）
```

## 国际化（assets/i18n.js）

运行时切换，不生成第二份 HTML、不引构建步骤。**代价要清楚**：搜索引擎只索引默认
的中文内容（`?lang=en` 是同一个 URL 的运行时状态）。这个站主要靠 GitHub About 与
README 徽章导流而不是自然搜索，所以换来的「两种语言并排放在同一张字典里、不会各自
漂移」更值。

加或改一条文案：

1. 在 `index.html` 给元素挂属性 —— 纯文本用 `data-i18n="key"`，内部含
   `<code>` / `<strong>` / `<br>` 时**必须**用 `data-i18n-html="key"`，属性用
   `data-i18n-attr="alt:key"`（多个属性逗号分隔）。
2. 在 `i18n.js` 的 `DICT.zh` 与 `DICT.en` 两边都加同名 key。`zh` 的值必须与
   `index.html` 里的原文**逐字一致**，否则中→英→中切回来对不上。
3. `<title>` 与各 `meta` 不挂 DOM 属性，走 `i18n.js` 顶部的 `META` 映射表。

两处刻意的重复，改判定规则时必须同时改：`index.html` 的 `<head>` 里有一段同步内联
脚本做语言判定（非默认语言先给 `<html>` 挂 `.i18n-pending` 把 body 藏起来防闪烁），
它和 `i18n.js` 里的 `pickLang()` 是同一套逻辑 —— head 里不能依赖外部文件，只能重复。

`TerraForge` 是专有名词，任何位置都不翻译；带 `data-noi18n` 的元素及其后代一律跳过。

## 动效（assets/terrain-fx.js + style.css 末尾）

两类，分开的：

- **CSS 动效**（`style.css` 末尾那个 `@media (prefers-reduced-motion: no-preference)`
  块）：首屏逐级上浮、高程色带逐档展开、滚动揭示错峰、悬停微交互。
- **画布动效**（`terrain-fx.js`）：全屏背景的等值线场，与首屏项目名。

画布这套是标量场 + marching squares：值噪声在 `(x, y, t)` 上取样，`t` 缓慢推进，
等值线因此像地形被慢慢抬升而不是整体平移。项目名是把 `TerraForge` 字形当遮罩
（`destination-in`），里面填分层设色渐变 + 同一片等值线，再用一道扫描线揭示。

调参的位置：

| 想改什么 | 改哪里 |
|---|---|
| 背景线的疏密 | `Background` 里 `field(..., 0.115, 0.115, ...)` 的频率，与 `CELL`（网格边长） |
| 背景线的深浅 | `Background.draw` 里两条 `rgba(232,234,237,…)`；实测 alpha 上限约 26/255 是「看得出线形又不抢正文对比度」的档位 |
| 演化快慢 | `t * 0.055`（背景）与 `t * 0.07`（字内）。0.055 是「10 秒能看出变了、扫一眼看不出在动」 |
| 等值线条数 | 文件顶部的 `LEVELS`。每 5 条加粗一档是计曲线，制图学惯例 |
| 项目名字号 | **改 `style.css` 的 `.brandmark-fallback`**，不要改 JS —— 画布读的是这个元素的计算样式，CSS 是字号唯一真源 |

三条不变量，改的时候别破坏：

1. `prefers-reduced-motion: reduce` 下 `terrain-fx.js` 直接 return，两块画布都不创建；
   页面拿到的是 `.brandmark-fallback` 那个静态渐变标题，本身就是成品，不是降级残骸。
2. `.fx-on` 只在**第一帧真的画完**之后才挂。挂早了会留下「回退文本已隐藏、画布还空着」
   的窗口 —— 后台标签页加载时那个窗口能长到几秒，首屏标题整块空白。
3. 滚动揭示**不用 IntersectionObserver**。它只在阈值被跨越时回调，一帧内从视口下方
   跳到上方不产生跨越，被跳过的区块会永久停在 `opacity:0`（点锚点后往回滚、拖滚动条、
   刷新时恢复滚动位置都会踩到）。现在是 rAF 节流的位置扫描，见 `index.html` 底部注释。

## 本地预览

```bash
python3 -m http.server 8899 --directory site
# 打开 http://127.0.0.1:8899/
```

## 部署（Cloudflare Pages）

两条路，选一条。仓库根目录的 `wrangler.jsonc` 两条都用得上（项目名 `terraforge`、
输出目录 `site`、无构建命令）。

### A. 控制台接 Git（推荐：push 即自动部署）

首次点一次即可，之后 push 到 `master` 会自动重新部署。

1. <https://dash.cloudflare.com/?to=/:account/workers-and-pages> —— 新版侧边栏把
   这一页收进了 **Compute** 分组，直接用这个链接不用找
2. **Create** → **Pages** → **Connect to Git**，授权 GitHub、选 `JungleZy/TerraForge`
3. 构建设置按下表填，**不要留默认值**：

   | 字段 | 值 |
   |---|---|
   | Production branch | `master` |
   | Framework preset | `None` |
   | Build command | *（留空）* |
   | Build output directory | `site` |

4. **Save and Deploy**，等约一分钟

### B. 命令行直传（Direct Upload：不碰 GitHub 授权，但不会自动部署）

```bash
export CLOUDFLARE_API_TOKEN=...      # 权限见下
export CLOUDFLARE_ACCOUNT_ID=...     # 不是密钥，dash.cloudflare.com/<这一串>
npx wrangler pages deploy
```

**Token 权限必须是这一条，少一样都会以 `code 10000 Authentication error` 失败：**

| 类型 | 组 | 级别 |
|---|---|---|
| **Account** | **Cloudflare Pages** | **Edit** |

另外 **Account Resources 要 Include 到这个账号**（或 All accounts）。用「Edit
Cloudflare Workers」之类的模板不行 —— 那套模板不含 Pages。

> **WSL2 下的坑**：wrangler 走 Node 的 `fetch`（undici），在 WSL2 里会因为
> happy-eyeballs 直接 `ETIMEDOUT`，而同一台机器 `curl` 和 Node 的 `https` 模块
> 都正常 —— 报错只说 "fetch failed / connectivity issue"，很容易误判成断网。
> 加这个环境变量绕过：
>
> ```bash
> export NODE_OPTIONS=--no-network-family-autoselection
> ```

### C. GitHub Actions 自动部署（当前采用；workflow 已就位，只差一个 Secret）

`.github/workflows/deploy-site.yml` 已经写好：push 到 `master` 且改动落在
`site/**` 或 `wrangler.jsonc` 时自动直传，也可以在 Actions 页手动 `Run workflow`
补发。它跑的就是 B 那条命令，wrangler 版本钉死 `4.121.0`。

**激活只需要加一个仓库 Secret：**

1. <https://github.com/JungleZy/TerraForge/settings/secrets/actions> → **New repository secret**
2. Name 填 `CLOUDFLARE_API_TOKEN`，Secret 填 B 里那个 token（权限要求同上）
3. Save

没配这个 Secret 时部署那步会被跳过并留一条 notice，**不会**在仓库首页留失败的红叉。
账号 ID 直接写在 workflow 里 —— 它不是密钥。

项目名决定域名，而 `*.pages.dev` 子域名是**全局唯一**的，不是每个账号一份。本项目
叫 `terraforge-gis` 就是因为 `terraforge` 已被别的账号占了 —— 用它建项目 Cloudflare
不会报错，而是默默给一个带随机后缀的 `terraforge-9pr.pages.dev`。想换名字先建了看
`result.subdomain` 是否干净，不干净就删掉重来；换定之后，`wrangler.jsonc` 的 `name`
与下面「改了域名要跟着改的地方」都要同步。

## 改内容时要一起改的地方

页面里有几处硬编码事实，改动时必须同步，否则站上写的和实际发出去的对不上：

| 改了什么 | 还要改哪里 |
|---|---|
| 发版（`Config.APP_VERSION` 变了） | `index.html` 里所有 `v0.3.3`（hero 按钮、下载区标题、三个 Release 下载链接、footer colophon）**以及 `i18n.js` 里含 `v0.3.3` 的那几条 key（zh 与 en 都要）** |
| 站点域名 | `index.html` 的 `canonical` / 三条 `hreflang` / `og:url` / `og:image` / `twitter:image`、`robots.txt`、`sitemap.xml`、`wrangler.jsonc` 的 `name`、本文件顶部 |
| 任何一句可见文案 | `i18n.js` 的 `DICT.zh` **和** `DICT.en` 两边同时改。只改 HTML 不改字典，切一次语言就被覆盖回去了 |
| 界面改版 | `assets/img/` 下的截图，以及 `i18n.js` 里对应的 `*.alt` key（两种语言） |

截图里出现的本机路径与代理地址已在截取时替换成占位值（`/data/terraforge/downloads`、空代理），重拍时记得照做。

## 设计约定

- 配色直接取自主应用 `static/css/style.css` 的暗色主题 token（`--color-bg-*` / `--color-text-*` / `--color-accent`），站与产品同色系。
- 首屏地图下方那条色带**不是装饰**：它是等高线功能的默认分层设色配色（`#5e8c61` … `#8e6246`）与其真实高程断点（0/200/500/1000/2000/3000/4000/5000 m），当图例用。改配色默认值时这条也要跟着改。
- 等宽字体（JetBrains Mono）只用于坐标、版本号、文件名、参数标签这类「仪表盘」信息；正文用 Inter + 系统中文字体。中文标题不会走等宽 —— 这两款字体都不含 CJK 字形。
