# site/ —— TerraForge 官网

线上地址：<https://terraforge.pages.dev/>

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

首次接入在 Cloudflare 控制台点一次即可，之后 push 到 `master` 会自动重新部署。

1. <https://dash.cloudflare.com/> → **Workers & Pages** → **Create** → **Pages** → **Connect to Git**
2. 授权 GitHub、选 `JungleZy/TerraForge`
3. 构建设置按下表填，**不要留默认值**：

   | 字段 | 值 |
   |---|---|
   | Production branch | `master` |
   | Framework preset | `None` |
   | Build command | *（留空）* |
   | Build output directory | `site` |

4. **Save and Deploy**，等约一分钟

项目名决定域名：填 `terraforge` 就是 `terraforge.pages.dev`，被占用时换一个名字，然后同步改掉下面「改了域名要跟着改的地方」。

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
