# 任务状态同步问题深度分析与修复

> **归档文档 · 非当前实现**
> **记录时间**：2026-05 ｜ **状态**：已实施（结论仍成立，示例代码已作废）
> 仍成立：两条结论——Socket.IO 推送的数据必须全字段同步到前端缓存；后端状态改完库必须立即推送。已作废：正文所有代码示例里的符号今天都不存在了——`activeTasks` 现按 `${taskType}:${taskId}` 复合键索引（`static/js/tasks.js:345`，正文写的 `activeTasks.get(taskId)` 单键会取不到任务），`createTaskCard` 全仓无定义，无条件 `outerHTML` 重建正是 `docs/PARTIAL_DOM_UPDATE.md` 明确否掉的老做法。当前事实源：`static/js/tasks.js` 的 `updateTaskProgress`、`services/task_manager.py`。
> **必须保住的约束**：状态变更（start / pause）后必须**立即 emit，不走 0.5s 节流**——`services/task_manager.py:455`（`start_task`）与 `:591`（`pause_task`）两处 emit。该约束无代码注释、无测试守卫，只在这份文档里以文字形式存在；将来若把这两发 emit 并入 `PROGRESS_EMIT_MIN_INTERVAL`（`task_manager.py:45`，0.5s）统一节流，按钮横跳会无声复活。
> *正文保持原样未回改。*

---

## 问题现象

1. **点击开始按钮后**：
   - ✗ 按钮没有变成暂停按钮
   - ✗ 运行时间和预计剩余时间不显示
   - ✓ 已下载数量正常更新

2. **刷新页面后**：
   - ✓ 所有信息显示正常
   - ✗ 点击暂停按钮没有反应

## 根本原因分析

### 问题 1：状态字段未同步

**核心问题**：`updateTaskProgress` 函数只更新了部分字段，导致状态不一致。

```javascript
// 旧代码 - 只更新了 3 个字段
function updateTaskProgress(data) {
    const task = activeTasks.get(taskId);
    if (task) {
        task.downloaded_tiles = data.downloaded_tiles;  // ✓ 更新
        task.failed_tiles = data.failed_tiles;          // ✓ 更新
        task.total_tiles = data.total_tiles;            // ✓ 更新
        
        // ✗ 缺失：status, started_at, name 等字段未更新
        // data 中包含完整信息，但没有同步到 task 对象
    }
}
```

**数据流分析**：

1. 用户点击开始按钮
2. 前端调用 `/api/tasks/{id}/start`
3. 后端更新数据库：
   ```python
   UPDATE tasks SET status = 'running', started_at = NOW()
   ```
4. 后端通过 Socket.IO 推送完整任务信息：
   ```python
   socketio.emit('task_progress', {
       'task_id': 1,
       'status': 'running',        # ← 包含新状态
       'started_at': '2026-05-14 11:30:00',  # ← 包含开始时间
       'downloaded_tiles': 0,
       # ... 其他字段
   })
   ```
5. 前端接收到数据，调用 `updateTaskProgress(data)`
6. **问题发生**：只更新了下载数量，`task.status` 仍是 `'pending'`，`task.started_at` 仍是 `null`
7. 重新渲染卡片时：
   - `task.status === 'pending'` → 显示开始按钮（错误）
   - `task.started_at === null` → 不显示时间信息（错误）

### 问题 2：刷新后暂停按钮无效

**原因**：刷新页面后，`loadActiveTasks()` 从 API 加载完整数据，所以显示正常。但暂停按钮无效的原因是：

1. 后端 `pause_task` 方法虽然推送了状态更新
2. 但前端 `updateTaskProgress` 没有正确更新 `status` 字段
3. 导致本地缓存中的状态仍是 `'running'`
4. 下次进度推送时，又用旧状态覆盖了

## 修复方案

### 修复 1：完整同步所有字段

```javascript
function updateTaskProgress(data) {
    const taskId = data.task_id;
    let task = activeTasks.get(taskId);

    if (task) {
        // 更新所有字段，确保状态完全同步
        task.status = data.status || task.status;              // ← 关键：更新状态
        task.started_at = data.started_at || task.started_at;  // ← 关键：更新开始时间
        // ... 其他所有字段
        
        activeTasks.set(taskId, task);
        card.outerHTML = createTaskCard(task);
    }
}
```

### 修复 2：后端立即推送状态变化

已在 `start_task` 和 `pause_task` 方法中添加立即推送。

## 测试验证

### 测试场景 1：启动任务
1. 创建一个新任务（状态：pending）
2. 点击开始按钮
3. **预期结果**：
   - ✓ 按钮立即变为暂停按钮
   - ✓ 显示"已运行: X秒"
   - ✓ 状态徽章变为"运行中"
   - ✓ 下载数量开始增加

### 测试场景 2：暂停任务
1. 任务正在运行
2. 点击暂停按钮
3. **预期结果**：
   - ✓ 按钮立即变为恢复按钮
   - ✓ 状态徽章变为"已暂停"
   - ✓ 下载数量停止增加

### 测试场景 3：恢复任务
1. 任务已暂停
2. 点击恢复按钮
3. **预期结果**：
   - ✓ 按钮立即变为暂停按钮
   - ✓ 状态徽章变为"运行中"
   - ✓ 下载数量继续增加

## 关键要点

1. **完整同步**：Socket.IO 推送的数据必须完整同步到本地缓存
2. **立即推送**：后端状态改变后必须立即推送
3. **单一数据源**：前端完全依赖后端推送
4. **字段完整性**：确保所有字段都在推送数据中

## 相关文件

- `static/js/tasks.js` - 前端任务状态管理
- `services/task_manager.py` - 后端任务管理和状态推送
