# 任务时长显示功能

## 概述

为活动任务添加了已运行时长和预估剩余时长的实时显示功能，让用户更清楚地了解任务进度。

## 功能特性

### 1. 已运行时长
- 显示任务从开始到当前的运行时间
- 对于 `running` 和 `paused` 状态的任务都会显示
- 每秒自动更新

### 2. 预估剩余时长
- 基于当前进度和已运行时间计算
- 仅对 `running` 状态且有下载进度的任务显示
- 动态调整，随着下载速度变化而更新

### 3. 时间格式化
- 小于 1 分钟：显示秒数（如 "45秒"）
- 1 分钟到 1 小时：显示分钟和秒（如 "5分30秒"）
- 超过 1 小时：显示小时和分钟（如 "2小时15分钟"）

## 实现细节

### 前端实现 (static/js/tasks.js)

#### 1. 时间格式化函数
```javascript
function formatDuration(seconds) {
    if (seconds < 60) {
        return `${Math.round(seconds)}秒`;
    } else if (seconds < 3600) {
        const minutes = Math.floor(seconds / 60);
        const secs = Math.round(seconds % 60);
        return secs > 0 ? `${minutes}分${secs}秒` : `${minutes}分钟`;
    } else {
        const hours = Math.floor(seconds / 3600);
        const minutes = Math.floor((seconds % 3600) / 60);
        return minutes > 0 ? `${hours}小时${minutes}分钟` : `${hours}小时`;
    }
}
```

#### 2. 时间信息计算
```javascript
function calculateTimeInfo(task) {
    // 计算已运行时长
    const startTime = new Date(task.started_at);
    const now = new Date();
    const elapsedSeconds = (now - startTime) / 1000;
    
    // 计算预估剩余时长
    if (task.status === 'running' && task.downloaded_tiles > 0) {
        const progress = task.downloaded_tiles / task.total_tiles;
        const estimatedTotalSeconds = elapsedSeconds / progress;
        const remainingSeconds = estimatedTotalSeconds - elapsedSeconds;
    }
}
```

#### 3. 定时更新机制
- 使用 `setInterval` 每秒更新一次时长显示
- 只更新时间部分，避免重新渲染整个任务卡片
- 减少 DOM 操作，提升性能

### 后端实现 (services/task_manager.py)

#### Socket.IO 数据推送
在任务进度更新时，包含 `started_at` 字段：

```python
self.socketio.emit('task_progress', {
    'task_id': task_id,
    'id': task_id,
    'name': task_row['name'],
    'status': task_row['status'],
    'downloaded_tiles': task_row['downloaded_tiles'],
    'failed_tiles': task_row['failed_tiles'],
    'total_tiles': task_row['total_tiles'],
    'started_at': task_row['started_at'],  # 新增
    'created_at': task_row['created_at'],  # 新增
    # ... 其他字段
})
```

## UI 显示

任务卡片中新增时间信息行：

```
┌─────────────────────────────────────┐
│ 任务名称              [运行中] [⏸][✕]│
│ ████████████░░░░░░░░░░ 60%          │
│ 📥 已下载: 600 / 1000 瓦片          │
│ ⏱ 已运行: 2分30秒 | 预计剩余: 1分40秒│
└─────────────────────────────────────┘
```

## 计算逻辑

### 已运行时长
```
已运行时长 = 当前时间 - 任务开始时间
```

### 预估剩余时长
```
进度百分比 = 已下载瓦片数 / 总瓦片数
预估总时长 = 已运行时长 / 进度百分比
预估剩余时长 = 预估总时长 - 已运行时长
```

## 使用场景

1. **任务监控**：实时了解任务运行情况
2. **时间规划**：根据预估时长安排其他工作
3. **性能评估**：通过时长判断下载速度是否正常

## 注意事项

1. **预估准确性**：
   - 预估时长基于当前下载速度
   - 网络波动会影响预估准确性
   - 随着任务进行，预估会越来越准确

2. **性能优化**：
   - 只更新时间文本，不重新渲染整个卡片
   - 使用高效的 DOM 查询和更新

3. **状态处理**：
   - `pending` 状态：不显示时长
   - `running` 状态：显示已运行时长和预估剩余时长
   - `paused` 状态：显示已运行时长，不显示预估剩余时长
   - `completed/failed/cancelled` 状态：任务已从活动列表移除

## 未来改进

- 添加平均下载速度显示（瓦片/秒）
- 支持暂停时长统计（排除暂停期间的时间）
- 添加历史任务的总耗时统计
- 支持时长格式自定义（12小时制/24小时制）
