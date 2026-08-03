# 暂停状态时间显示修复

> **归档文档 · 非当前实现**
> **记录时间**：2026-05 ｜ **状态**：已实施（结论仍成立，成因描述已缩窄）
> 仍成立：结论就是 `static/js/tasks.js:763-766` 那段 guard（`updateTimeDisplay` 里 `if (task.status !== 'running') return;`）本身。
> 成因已缩窄：正文描述的「总是用当前时间减去开始时间」对地图管线已不准——地图任务的运行时长走 `total_running_seconds` 持久化列。**该成因今日仅适用于 `total_running_seconds` 缺失的 dem / contour / local_terrain 路径**：这三条管线的 manager 不写该列，`calculateTimeInfo` 必走 `started_at` 墙钟回退分支（`tests/test_tasks_js_contract.py:1159` 钉死该回退），而 dem 与 contour 都支持暂停。
> **必须保住**：这段 guard 的存在理由只记录在本文档里——代码只有 what 注释没有 why，测试也没有断言保护。删掉 guard，暂停中的 dem / contour 任务「已运行」会每秒继续增长，原 bug 一字未变复活。
> *正文保持原样未回改。*

---

## 问题描述

任务暂停后，"已运行时长"仍在继续增加，这是不正确的。暂停状态下时间应该停止计数。

## 问题原因

`updateTimeDisplay` 函数每秒都会更新所有 `running` 和 `paused` 状态的任务，而 `calculateTimeInfo` 函数总是用当前时间减去开始时间，导致暂停时时间仍在增加。

## 解决方案

只更新 `running` 状态的任务时间，暂停状态的任务不更新：

```javascript
function updateTimeDisplay() {
    activeTasks.forEach((task, taskId) => {
        // 只更新运行中的任务时间
        if (task.status === 'running') {
            // 更新时间显示
        }
    });
}
```

## 效果

### 修复前
- 任务暂停后，时间继续增加（错误）

### 修复后
- 任务暂停后，时间停止更新（正确）
- 恢复任务后，时间继续计数

## 相关文件

- `static/js/tasks.js` - 时间更新逻辑
