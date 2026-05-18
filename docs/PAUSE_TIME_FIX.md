# 暂停状态时间显示修复

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
