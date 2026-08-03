# 背景色问题排查和解决方案

> **归档文档 · 非当前实现**
> **记录时间**：2026-05-14 入库（文内自述 2026-05-10）｜ **状态**：一次性排查笔记，前提已作废
> ⚠️ **不要照本文排查今天的白底**。全文的大前提是「白底 = CSS 没生效」，而明暗主题上线（5c4cbefe7，2026-08-01）后，浅色背景是 `[data-bs-theme="light"]` 的**合法状态**：`--color-bg-primary: #eef0f3`、`--color-bg-secondary: #ffffff`。按本文流程会把亮色模式误判成 bug，去清缓存、重启 Flask、甚至加 `!important` 把深色压回来——那会打坏主题系统。
> 文内色值也已过期：暗色 `--color-bg-primary` 现为 `#0c0d10`（非 #0a0e1a），`--color-text-primary` 为 `#e8eaed`（非 #e5e7eb），琥珀强调色早已废弃。
> 仍然成立的部分：它描述的 CSS 机制没变——`static/css/style.css:349-375` 的 `html`/`body` 背景 `!important`、`.container`/`.row` 透明化今天仍在。作为「明暗主题上线前的准确快照」有保留价值。
> 当前主题事实源：`CLAUDE.md` 的 Theming 节 + `static/css/style.css` 内联注释 + `tests/test_css_contract.py`。
> *正文保持原样未回改。*

---

## 问题描述
用户反馈背景仍然显示为白色，而不是预期的深色主题。

## 已实施的修复

### 1. CSS样式设置 ✅
```css
:root {
    --color-bg-primary: #0a0e1a;  /* 深蓝黑色 */
}

html {
    background: var(--color-bg-primary) !important;
}

body {
    background: var(--color-bg-primary) !important;
    color: var(--color-text-primary) !important;
}

.container,
.container-fluid {
    background: transparent !important;
}

.row {
    background: transparent !important;
}
```

### 2. CSS加载顺序 ✅
```html
<!-- Bootstrap CSS (先加载) -->
<link href="bootstrap.min.css" rel="stylesheet">

<!-- 自定义CSS (后加载，可以覆盖Bootstrap) -->
<link rel="stylesheet" href="style.css">
```

### 3. 使用 !important 标记 ✅
确保自定义样式优先级高于Bootstrap默认样式。

## 可能的原因

### 1. 浏览器缓存 ⚠️
**最常见的原因**：浏览器缓存了旧的CSS文件。

**解决方案**：
- **Chrome/Edge**: `Ctrl + Shift + R` (Windows) 或 `Cmd + Shift + R` (Mac)
- **Firefox**: `Ctrl + F5` (Windows) 或 `Cmd + Shift + R` (Mac)
- **Safari**: `Cmd + Option + R`

或者：
1. 打开开发者工具 (F12)
2. 右键点击刷新按钮
3. 选择"清空缓存并硬性重新加载"

### 2. CSS文件未正确加载 ⚠️
**检查方法**：
1. 打开浏览器开发者工具 (F12)
2. 切换到 Network (网络) 标签
3. 刷新页面
4. 查找 `style.css` 文件
5. 确认状态码为 200 (成功加载)

### 3. Flask静态文件缓存 ⚠️
**解决方案**：
```bash
# 重启Flask应用
python app.py
```

## 验证步骤

### 步骤 1: 检查CSS是否加载
1. 打开页面
2. 按 F12 打开开发者工具
3. 切换到 Elements (元素) 标签
4. 选择 `<body>` 元素
5. 查看右侧 Styles (样式) 面板
6. 确认看到：
   ```css
   body {
       background: #0a0e1a !important;
       color: #e5e7eb !important;
   }
   ```

### 步骤 2: 检查CSS变量
在 Console 中运行：
```javascript
// 检查CSS变量
console.log(getComputedStyle(document.documentElement).getPropertyValue('--color-bg-primary'));

// 检查body背景色
console.log(getComputedStyle(document.body).backgroundColor);
```

应该看到：
```
#0a0e1a
rgb(10, 14, 26)
```

## 预期效果

**正确显示时应该看到**：
- 深蓝黑色背景 (#0a0e1a)
- 浅灰色文字 (#e5e7eb)
- 琥珀色强调元素 (#f59e0b)
- 深色卡片和组件
- 网格纹理背景

---

**最后更新**: 2026年5月10日  
**状态**: 样式已正确设置，等待用户清除缓存验证
