# 任务按钮状态切换问题修复

> **归档文档 · 非当前实现**
> **记录时间**：2026-05 ｜ **状态**：已实施（方案仍是现行做法，细节已失准）
> 仍成立：方案本身——前端不做乐观更新，后端改完数据库立即通过 Socket.IO 推送。正文用现在时描述的是当时的故障，不是今天的行为。
> **必须保住**：「前端为什么不做乐观更新」的根因链条（点暂停 → 本地置 `paused` → 在途 `running` 推送覆盖 → 按钮横跳）只写在这份文档里，代码里 grep 不到，而这个不变量今天仍带电——删掉 `pause_task` 里那发 emit 或重新加乐观更新，故障立刻复活。
> 三处会把读者带到错地方：(a) 示例写死 `/api/tasks` 前缀，实际 `static/js/tasks.js:781` 的 `apiPrefixForType` 按 taskType 分发四个前缀（map / dem / local_terrain / contour），照抄会把 DEM、等高线、本地地形的暂停请求打到地图管线；(b)「每下载一个瓦片都推送」已改为时间窗节流——地图 0.5s（`services/task_manager.py:45`），DEM 与等高线各 1.0s（`dem_task_manager.py:30`、`contour_task_manager.py:60`）；(c)「相关文件」列的 `routes/socketio_events.py` 今天只有连接/断开日志，所有 `task_progress` emit 都在四个 manager 内部。
> *正文保持原样未回改。*

---

## 问题描述

在任务运行时点击暂停按钮，按钮状态会疯狂切换，导致暂停功能无效。

## 问题原因

### 1. 前端立即更新状态
当用户点击暂停按钮时，前端代码会立即：
- 更新本地缓存中的任务状态为 `paused`
- 重新渲染任务卡片，显示恢复按钮

### 2. 后端延迟更新
后端在暂停任务时：
- 设置停止标志
- 更新数据库状态
- 但**没有立即**通过 Socket.IO 推送状态更新

### 3. 进度推送覆盖状态
任务下载过程中，每下载一个瓦片都会通过 Socket.IO 推送进度更新：
- 推送的数据包含任务状态
- 由于后端状态更新有延迟，推送的仍是 `running` 状态
- 前端接收到 `running` 状态后，又重新渲染为暂停按钮

### 4. 状态冲突循环
```
用户点击暂停 → 前端显示 paused → Socket.IO 推送 running → 前端显示 running
→ 用户再次点击暂停 → 前端显示 paused → Socket.IO 推送 running → ...
```

## 解决方案

### 方案 1：移除前端立即更新（已采用）

**前端修改** (static/js/tasks.js)：
- 移除任务操作后的立即状态更新
- 完全依赖 Socket.IO 推送的状态更新

```javascript
async function pauseTask(taskId) {
    try {
        const response = await fetch(`/api/tasks/${taskId}/pause`, {
            method: 'POST'
        });
        if (!response.ok) {
            throw new Error('暂停任务失败');
        }
        // 不再立即更新本地状态，等待 Socket.IO 推送
    } catch (error) {
        alert('暂停任务失败: ' + error.message);
    }
}
```

**后端修改** (services/task_manager.py)：
- 在 `pause_task` 方法中，更新数据库后立即通过 Socket.IO 推送状态
- 在 `start_task` 方法中，更新数据库后立即通过 Socket.IO 推送状态

```python
def pause_task(self, task_id: int):
    # ... 更新数据库状态 ...
    
    # 立即推送状态更新
    cursor.execute('SELECT * FROM tasks WHERE id = ?', (task_id,))
    task_row = cursor.fetchone()
    
    if task_row and self.socketio:
        self.socketio.emit('task_progress', {
            'task_id': task_id,
            'status': task_row['status'],  # 'paused'
            # ... 其他字段 ...
        })
```

## 优势

1. **单一数据源**：状态更新只来自后端，避免前后端状态不一致
2. **实时同步**：后端状态改变后立即推送，前端快速响应
3. **简化逻辑**：前端不需要维护复杂的状态同步逻辑
4. **多客户端同步**：多个浏览器标签页都能同步看到状态变化

## 测试验证

1. 创建一个下载任务并启动
2. 等待任务开始下载（状态变为 `running`）
3. 点击暂停按钮
4. 观察按钮状态：
   - ✅ 应该平滑切换为恢复按钮
   - ✅ 不应该出现疯狂切换
   - ✅ 任务状态应该变为 `paused`
5. 点击恢复按钮
6. 观察按钮状态：
   - ✅ 应该平滑切换为暂停按钮
   - ✅ 任务状态应该变为 `running`

## 相关文件

- `static/js/tasks.js` - 前端任务操作函数
- `services/task_manager.py` - 后端任务管理器
- `routes/socketio_events.py` - Socket.IO 事件处理

## 未来改进

- 可以添加按钮禁用状态，在操作进行中禁用按钮，防止重复点击
- 添加操作反馈动画，提升用户体验
- 考虑添加乐观更新（optimistic update）+ 回滚机制
