# site/ —— TerraForge 官网

线上地址：<https://terraforge-gis.pages.dev/>（中文） · <https://terraforge-gis.pages.dev/en/>（英文）

两个静态页，**没有构建步骤**。`site/` 目录里是什么，线上就是什么。英文页是生成物，
由 `scripts/build_site_en.py` 从中文页 + 译文字典产出并提交进仓库 —— 部署侧仍然只是
上传目录，不跑任何命令。

## 为什么不用框架

本仓库是纯 Python 项目，没有 Node 工具链。为一个落地页引入 Next/Astro 等于额外养一套 `node_modules` 与构建流水线，而这个页面用不上它们提供的任何能力。字体与样式全部本地 vendor，不依赖 CDN —— 与主应用 `static/vendor/` 的既有约定一致。

## 目录

```
site/
├── index.html            # 中文页，唯一手写的那份
├── en/index.html         # 英文页（生成物，头两行写着 DO NOT EDIT）
├── _headers              # Cloudflare Pages 响应头（缓存策略与安全头）
├── robots.txt            # 全站可抓 + 生成式引擎 UA 明确允许
├── sitemap.xml           # 两个页面 + 双向 hreflang
├── llms.txt              # 给生成式引擎的索引页（llmstxt.org 格式）
├── llms-full.txt         # 给生成式引擎的全文事实表
└── assets/
    ├── style.css         # 全部样式；设计 token 在文件顶部，动效在文件末尾
    ├── reveal.js         # 滚动揭示（双向，rAF 位置扫描）
    ├── terrain-fx.js     # 地形场动效：全屏背景 + 首屏项目名
    ├── favicon.ico       # 取自 static/img/favicon.ico
    ├── fonts/            # Inter + JetBrains Mono，从 static/vendor/fonts/ 复制
    └── img/              # 界面截图（webp）与社交卡片（og.jpg）
```

译文字典不在 `site/` 里：它是构建输入，不该被部署出去，放在 `scripts/site_i18n.json`。

## 中英文：两个真实页面

原来是一份 HTML + `assets/i18n.js` 运行期换文案。那套对用户没问题，对机器是净损失，
两个原因：

1. 英文内容没有自己的 URL。`?lang=en` 只是同一个 URL 的运行期状态，hreflang 指过去
   拿回来的仍是中文 HTML —— 这组语言关系搜索引擎不会认。
2. 语言判定看 `navigator.language`，而 **Googlebot 的 navigator.language 是 en-US**。
   抓取时脚本把页面换成英文，源码、`<html lang>`、canonical 却都说中文：同一个 URL
   两副面孔，这正是「内容不一致」类信号。

现在是 `/` 中文、`/en/` 英文两个静态页，语言切换是两条 `<a>`（选中态用
`aria-current="page"`，链接的正确写法；`aria-pressed` 属于按钮）。

### 改文案的流程

1. 改 `index.html` 里那句中文。
2. 改 `scripts/site_i18n.json` 的 **`zh` 与 `en` 两侧**。`zh` 的值必须与 `index.html`
   里的原文一致（空白可以不同，其余逐字一致）。
3. 跑 `uv run python scripts/build_site_en.py` 重新生成英文页。

漏掉任何一步，`tests/test_site_seo_contract.py` 都会红：它用 zh 字典把中文页翻译一遍，
要求还原出页面本身（往返等式），并要求 `site/en/index.html` 与当前源逐字节一致。

### 新增一句可翻译文案

在 `index.html` 给元素挂标注 —— 纯文本用 `data-i18n="key"`，内部含 `<code>` /
`<strong>` / `<br>` 时**必须**用 `data-i18n-html="key"`，属性用
`data-i18n-attr="alt:key"`（多个属性逗号分隔，写法 `属性名:键名`）。`<title>` 与各
`meta` 同样直接挂 `data-i18n-attr="content:key"`。

这些属性在运行期不做任何事，页面上没有 i18n 脚本 —— 它们是「这句话是可翻译文案、
键名是 X」的标注，只有 `scripts/build_site_en.py` 会读。字典里有页面不用的键、或者
页面用了字典没有的键，测试都会红。

`TerraForge` 是专有名词，任何位置都不翻译；带 `data-noi18n` 的元素跳过。

### 生成器怎么工作

`scripts/build_site_en.py` 分两步，都在文件头的 docstring 里写了：

- `translate()`：只按标注替换文案，其余字节原样透传（起始标签直接回写
  `get_starttag_text()`，属性顺序与引号写法都不变）。它对语言无感 —— 拿 zh 字典跑就
  该还原出源文件。
- `localize_en()`：页面级差异 —— `<html lang>`、canonical / og:url 换成 `/en/`、资源
  路径转根绝对（`/en/` 深一层，相对路径会解析到 `/en/assets/`）、语言切换的选中态换边、
  JSON-LD 里 `#app` 那个节点（url / @id / description / inLanguage / featureList）、
  以及**丢掉全部 HTML 注释**（注释是中文的，不该混进英文页被抓走）。

`--check` 只校验不写盘，测试走的就是这条路径。

## SEO / GEO：机器读到的那一份

改这块之前先想清楚它会不会和别处打架 —— 下面每一条都有测试钉着。

| 事实 | 谁说了算 | 谁跟着它 |
|---|---|---|
| 版本号 | `src/core/config.py` 的 `Config.APP_VERSION` | 两个页面上写着版本号的那几处（hero 按钮、下载区标题、图例说明、三个 Release 下载链接、footer colophon）、JSON-LD 的 `softwareVersion` / `downloadUrl`、`llms.txt`、`llms-full.txt` |
| 站点描述 | `index.html` 的 `<meta name="description">` | JSON-LD 的 `description`（两处必须逐字一致） |
| 能力清单 | 页面上四条管线的 `<h3>` + 插件一节的 `<h2>` | JSON-LD 的 `featureList`（键在 `build_site_en.py` 的 `FEATURE_KEYS`） |
| 语言关系 | 每页 `<head>` 里的三条 hreflang | `sitemap.xml` 里每条 `<url>` 的 `xhtml:link` |

其余几件事：

- **JSON-LD**（`SoftwareApplication` + `WebSite` + `Person`）。`WebSite` 与 `Person`
  两页共用同一个 `@id`，只有 `#app` 跟着页面语言分叉 —— 站点与作者的身份不该有两份。
- **`llms.txt` / `llms-full.txt`**：给生成式引擎的纯文本。前者是索引（llmstxt.org 的
  H1 + 摘要 + 链接分组格式），后者是全文事实表，写的时候就是奔着「被原样引用」去的：
  版本号、格式名、限制、许可与署名，一条不含糊。它们**不是**页面的复制品，页面改版不用
  跟着改，但事实变了必须改。
- **`robots.txt`** 把生成式引擎的 UA 单列一组明确 `Allow`。不是因为通配符不够，而是
  `Google-Extended`、`Applebot-Extended` 这类令牌只认自己的名字，通配符对它们不表态。
- **`_headers`** 给两个 `.txt` 写死 `text/plain; charset=utf-8`（正文里有 `°`、`×`）。

## 动效（assets/terrain-fx.js + assets/reveal.js + style.css 末尾）

三块，分开的：

- **CSS 动效**（`style.css` 末尾那个 `@media (prefers-reduced-motion: no-preference)`
  块）：首屏逐级上浮、高程色带逐档展开、滚动揭示错峰、悬停微交互。
- **滚动揭示**（`reveal.js`）：双向，rAF 节流的位置扫描。
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
   刷新时恢复滚动位置都会踩到）。现在是 rAF 节流的位置扫描，理由与滞回带的取值写在
   `assets/reveal.js` 的文件头。

## 本地预览

```bash
python3 -m http.server 8899 --directory site
# 中文 http://127.0.0.1:8899/   英文 http://127.0.0.1:8899/en/
```

语言链接写的是根绝对路径（`/` 与 `/en/`），所以必须以 `site/` 为根起服务，直接
`file://` 打开会点不动。

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
| 发版（`Config.APP_VERSION` 变了） | `index.html` 里所有旧版本号（hero 按钮、下载区标题、图例说明、三个 Release 下载链接、JSON-LD 的 `softwareVersion` / `downloadUrl`、footer colophon）、`scripts/site_i18n.json` 里含版本号的那几条 key（zh 与 en 都要）、`llms.txt` 与 `llms-full.txt`，然后重新生成英文页。`tests/test_site_seo_contract.py` 会比对全部这些位置与 `APP_VERSION` |
| 任何一句可见文案 | `scripts/site_i18n.json` 的 `zh` **和** `en` 两侧，然后 `uv run python scripts/build_site_en.py` |
| 站点域名 | `index.html` 的 `canonical` / 三条 `hreflang` / `og:url` / `og:image` / `twitter:image` / JSON-LD 里全部 `@id` 与 URL、`robots.txt`、`sitemap.xml`、`llms.txt`、`llms-full.txt`、`wrangler.jsonc` 的 `name`、`tests/test_site_seo_contract.py` 的 `BASE`、本文件顶部 |
| 产品事实（新增管线、加插件、换数据源、平台支持变化） | `llms-full.txt` 与 `llms.txt` —— 它们是给模型读的那份事实表，页面改了不会自己跟着变；`sitemap.xml` 的两条 `<lastmod>` 也顺手改成当天 |
| 界面改版 | `assets/img/` 下的截图（英文页用 `img/en/` 那一份），以及字典里对应的 `*.alt` key（两种语言）。截图尺寸变了要同步 `<img>` 上的 `width`/`height`，测试会按文件真实像素比对 |

截图里出现的本机路径与代理地址已在截取时替换成占位值（`/data/terraforge/downloads`、空代理），重拍时记得照做。

## 设计约定

- 配色直接取自主应用 `static/css/style.css` 的暗色主题 token（`--color-bg-*` / `--color-text-*` / `--color-accent`），站与产品同色系。
- 首屏地图下方那条色带**不是装饰**：它是等高线功能的默认分层设色配色（`#5e8c61` … `#8e6246`）与其真实高程断点（0/200/500/1000/2000/3000/4000/5000 m），当图例用。改配色默认值时这条也要跟着改。
- 等宽字体（JetBrains Mono）只用于坐标、版本号、文件名、参数标签这类「仪表盘」信息；正文用 Inter + 系统中文字体。中文标题不会走等宽 —— 这两款字体都不含 CJK 字形。
