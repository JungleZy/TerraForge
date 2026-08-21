## v0.5.0 —— 界面整体换成液态玻璃：Bootstrap 整个清退，控件全部自绘；搜索、面板、工具条顺手修了一轮

**先说结论：这一版没有任何功能变化，改的全是界面长相与手感。全站控件从 Bootstrap 换成自绘的液态玻璃组件 —— Bootstrap 的 CSS/JS 文件已从仓库整个删除，按钮、输入框、卡片、对话框、面板全部是自己的实现。不支持折射滤镜的浏览器不会看到坏画面：玻璃效果分三档，`@supports` 天然降级，最差那档也是一块正常的半透明浮层。没有数据库迁移，已下载的数据、配置、历史全部照旧，四条管线的行为一个像素没动。**

**液态玻璃上线**
- 新增三档玻璃基类 `.tf-glass`（令牌层 + 三档料）：最重的 glass-3 带 SVG 折射滤镜，Chromium 上还有一档增强；浏览器不支持滤镜时按 `@supports` 逐档降级，不会白屏也不会裸奔。
- 地图页悬浮 chrome（工具条、搜索栏、状态栏胶囊）、config 页、history 页全部迁移到玻璃组件；表单控件 `.tf-field` 与卡片 `.tf-card` 是新的公共件。history 页的长列表行**保持实色** —— 几百行玻璃同时折射的性能账不划算，这是有意的。
- 快捷键速查表、解压胶囊、地图浮层、搜索结果/最近搜索面板、路径弹窗、缓存清除钮，逐块挂上玻璃；config 分区卡在亮色主题下换了专用薄料，不再是一块纯白。
- 滑出面板从贴边抽屉改成**悬浮玻璃卡**：四周内缩 12px，统计卡与网格密度一并收紧。

**Bootstrap 清退**
- 仅剩的两处 `bootstrap.Modal` 先换成自研 `TfModal`（接口与 `bootstrap.Modal` 同形，含焦点环与 `data-bs-dismiss` 兼容），随后 `static/vendor/bootstrap/` 整个删除。全站样式现在只有一套体系，不再有「Bootstrap 通用类」与「项目令牌」两套来源打架 —— 0.4.0 欠账清单里「插件面板借 Bootstrap 类、暗色对比度 3.82:1」那一条随之结清。
- 玻璃 chrome 上的文字锐度专门修过：料加深一档 + 文字按轮廓抠形投影，半透明底上的字不再发虚。

**搜索一轮修复**
- 点历史搜索条目后**没有加载提示**、结果面板**闪开即关** —— 两个都修了。
- 「搜索中」指示改成三点呼吸动画：居中、纯动画，去掉了转圈。
- 顶部搜索栏升到 36px 主操作档，与同级控件同高。

**工具条与其他**
- 左侧地图工具条纯图标化，取消按钮文字标签（悬停仍有提示）。
- 非地图页（config / history）补上等高线环境背景，与地图页同一套氛围。
- 修了一个代理问题：自动探测的候选端口补上 **7892** —— 此前 Clash 系只认 7890/7897，宿主机 mixed-port 配在 7892 时探测结果恒为 0 个候选。
- README 双语版式重做，徽章与国旗全部落地成仓库内 PNG，不再依赖外部图片服务；官网补上插件系统一节（中英两页 + 两份给生成式引擎读的事实表）。

---

**给排障和构建的人**

- Bootstrap 是真删不是停用：`static/vendor/bootstrap/` 已不在仓库里，`templates/` 零引用，`tests/test_fix_static_vendor_cache.py` 的 vendor 清单同步改写。少数沿用下来的类名（`.text-muted` / `.text-secondary` / `.list-group-item` 等工具类）是**有意保留的名字**，定义已收进 `static/css/style.css` 并全部指向项目令牌 —— 它们不再是 Bootstrap。
- 玻璃三档降级靠 `@supports` 链，不需要 UA 嗅探；折射滤镜**没有**配 `prefers-reduced-motion` 禁用块 —— 这是用户明确裁决过的，不是漏写。
- 滑出面板悬浮化之后，提交按钮的 `bottom` 公式从「满高」改为「视口 − 面板下内缩 12 − 底边框 1 − 底条下内边距 12」，契约测试按 `_resolve_length_px` 跟令牌算，浏览器实测 1366×768 → 743.00 复核通过。
- 新增回归测试 `test_hidden_attribute_actually_hides_the_capsule`：解压胶囊的 `[hidden]` 此前压不住 UA 样式规则，兜底 `display:none` 已补。
- 官网版本号契约（`tests/test_site_seo_contract.py`）要求站上只出现一个版本号，因此插件系统一节的眉题从「0.4.0 新增」改为不带版本的「插件系统」，截图说明不再标注拍摄版本 —— 不是事实变了，是契约不许两个版本号同框。

**验证**

- 全量测试 **3240 项通过 / 3 项跳过**（开发机 Linux，3 分 45 秒）。
- 液态玻璃改造按 9 个 Task 四阶段计划执行，每轮挂起项清零后才进下一阶段；视觉复核在真实浏览器里逐元素量过。

---

## 通用说明

- **下载安装**：从下方 Assets 下载对应平台压缩包（`terraforge-windows.zip` / `terraforge-linux.tar.gz` / `terraforge-macos.tar.gz`），解压即用，无需安装 Python 环境。
- **下载体积**：每个平台仍包含 167 MB 的全球底图分卷（自 v0.2.8 起）。
- **首次运行**：启动可执行文件后，浏览器访问 http://localhost:5000 ；代理、并发、缓存管理等在「配置」页修改。程序另会监听 5001 出瓦片，不放行也能用。
- **许可证与第三方声明**：程序目录下的 `LICENSE`（MIT）与 `THIRD_PARTY_NOTICES.md`。MIT 只覆盖软件代码，**不授予**任何数据与在线服务的使用权。
- **历史版本**：完整更新历史见仓库 [CHANGELOG.md](https://github.com/JungleZy/TerraForge/blob/master/CHANGELOG.md)。
- **使用文档**：见仓库 [README.md](https://github.com/JungleZy/TerraForge/blob/master/README.md) 与 [docs/guides/QUICKSTART.md](https://github.com/JungleZy/TerraForge/blob/master/docs/guides/QUICKSTART.md)。
