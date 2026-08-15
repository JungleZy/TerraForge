# readme/ —— 根 README 用的当前界面截图

> **拍摄时间：2026-08-13** ｜ **版本：v0.3.5**（commit `967d498`）｜ **状态：当前 UI，界面改了就该重拍。**

## 这是什么

仓库根 [`README.md`](../../../../README.md) 与 [`README.en.md`](../../../../README.en.md) 里，官网截图（`site/assets/img/`）覆盖不到的两个场景。中英各一套，`en/` 下的四至、相机与主题与中文版逐项相同，只有界面语言不同。

| 文件 | 场景 | 拍摄参数 |
|---|---|---|
| `home-light.webp` · `en/home-light.webp` | 浅色主题下的主界面，贡嘎山选区 N30 / S29.2 / E102.3 / W101.5 | 视口 1600×1000 @2x，相机 101.90°E 28.80°N 78 km、pitch −32° |
| `process-contour.webp` · `en/process-contour.webp` | 「数据处理」弹窗，处理类型 = 等高线瓦片 | 视口 1280×1440 @2x，只截 `#processModal .modal-content` |

## 与同级两套的区别

`phase2-baseline/` 与 `ui-review-2026-07/` 是**时点留档**，正文引用它们的文档遵循「不回改」约定，所以那两套永远停在 2026-07。本目录相反：它服务的是根 README，**界面变了就应当重拍并覆盖**，不需要保留旧版。

## 已知瑕疵

`process-contour.webp` 里的文件选择控件显示英文 `Choose Files / No file chosen` —— 那是浏览器原生控件，跟随浏览器 UI 语言而不是页面语言，截图环境是 headless Chrome（en-US）。真实用户机器上会显示系统语言的文案。

## 重拍方法

启动应用后用无头浏览器拍即可，两张图的参数见上表：主题走 `localStorage` 的 `tf-theme`（`dark` / `light` / `system`），界面语言走 cookie `tf-lang`（`zh` / `en`），选区用主界面的「手动输入范围」填四至，相机用 `viewer.camera.flyTo()` 定位。
