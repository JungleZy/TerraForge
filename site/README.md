# site/ —— TerraForge 官网

线上地址：<https://terraforge-gis.pages.dev/>

一个静态单页站，**没有构建步骤**。`site/` 目录里是什么，线上就是什么。

## 为什么不用框架

本仓库是纯 Python 项目，没有 Node 工具链。为一个落地页引入 Next/Astro 等于额外养一套 `node_modules` 与构建流水线，而这个页面用不上它们提供的任何能力。字体与样式全部本地 vendor，不依赖 CDN —— 与主应用 `static/vendor/` 的既有约定一致。

## 目录

```
site/
├── index.html            # 整个页面
├── _headers              # Cloudflare Pages 响应头（缓存策略与安全头）
├── robots.txt
├── sitemap.xml
└── assets/
    ├── style.css         # 全部样式；设计 token 在文件顶部
    ├── favicon.ico       # 取自 static/img/favicon.ico
    ├── fonts/            # Inter + JetBrains Mono，从 static/vendor/fonts/ 复制
    └── img/              # 界面截图（webp）与社交卡片（og.jpg）
```

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

项目名决定域名，而 `*.pages.dev` 子域名是**全局唯一**的，不是每个账号一份。本项目
叫 `terraforge-gis` 就是因为 `terraforge` 已被别的账号占了 —— 用它建项目 Cloudflare
不会报错，而是默默给一个带随机后缀的 `terraforge-9pr.pages.dev`。想换名字先建了看
`result.subdomain` 是否干净，不干净就删掉重来；换定之后，`wrangler.jsonc` 的 `name`
与下面「改了域名要跟着改的地方」都要同步。

## 改内容时要一起改的地方

页面里有几处硬编码事实，改动时必须同步，否则站上写的和实际发出去的对不上：

| 改了什么 | 还要改哪里 |
|---|---|
| 发版（`Config.APP_VERSION` 变了） | `index.html` 里所有 `v0.3.3`：hero 按钮、下载区标题、三个 Release 下载链接、footer colophon |
| 站点域名 | `index.html` 的 `canonical` 与 `og:url` / `og:image` / `twitter:image`、`robots.txt`、`sitemap.xml`、本文件顶部 |
| 平台支持范围 | 下载区三张平台卡片与其下的注意事项列表（现状：macOS 仅 arm64） |
| 界面改版 | `assets/img/` 下的截图。截图取自 v0.3.3 实际运行界面，视口 1600×1000 @2x，深色主题 |

截图里出现的本机路径与代理地址已在截取时替换成占位值（`/data/terraforge/downloads`、空代理），重拍时记得照做。

## 设计约定

- 配色直接取自主应用 `static/css/style.css` 的暗色主题 token（`--color-bg-*` / `--color-text-*` / `--color-accent`），站与产品同色系。
- 首屏地图下方那条色带**不是装饰**：它是等高线功能的默认分层设色配色（`#5e8c61` … `#8e6246`）与其真实高程断点（0/200/500/1000/2000/3000/4000/5000 m），当图例用。改配色默认值时这条也要跟着改。
- 等宽字体（JetBrains Mono）只用于坐标、版本号、文件名、参数标签这类「仪表盘」信息；正文用 Inter + 系统中文字体。中文标题不会走等宽 —— 这两款字体都不含 CJK 字形。
