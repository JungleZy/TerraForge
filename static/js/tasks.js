let socket;

function initTasks() {
    socket = io();

    socket.on('connect', function() {
        console.log('Connected to server');
    });

    socket.on('task_progress', function(data) {
        updateTaskCard(data);
    });

    loadActiveTasks();
    setInterval(loadActiveTasks, 5000);
}

async function loadActiveTasks() {
    try {
        const response = await fetch('/api/tasks');
        const tasks = await response.json();

        const activeTasks = tasks.filter(t =>
            ['pending', 'running', 'paused'].includes(t.status)
        );

        renderActiveTasks(activeTasks);
    } catch (error) {
        console.error('Failed to load tasks:', error);
    }
}

function renderActiveTasks(tasks) {
    const container = document.getElementById('activeTasks');

    if (tasks.length === 0) {
        container.innerHTML = '<p class="text-muted">暂无活动任务</p>';
        return;
    }

    container.innerHTML = tasks.map(task => createTaskCard(task)).join('');
}

function createTaskCard(task) {
    const progress = task.total_tiles > 0
        ? Math.round((task.downloaded_tiles / task.total_tiles) * 100)
        : 0;

    return `
        <div class="task-card ${task.status}" id="task-${task.id}">
            <div class="d-flex justify-content-between align-items-start">
                <div>
                    <h6>${task.name}</h6>
                    <span class="badge bg-${getStatusColor(task.status)}">${getStatusText(task.status)}</span>
                </div>
                <div class="btn-group btn-group-sm">
                    ${task.status === 'pending' ? `
                        <button class="btn btn-success" onclick="startTask(${task.id})">启动</button>
                    ` : ''}
                    ${task.status === 'running' ? `
                        <button class="btn btn-warning" onclick="pauseTask(${task.id})">暂停</button>
                    ` : ''}
                    ${task.status === 'paused' ? `
                        <button class="btn btn-success" onclick="resumeTask(${task.id})">恢复</button>
                    ` : ''}
                    <button class="btn btn-danger" onclick="cancelTask(${task.id})">取消</button>
                </div>
            </div>

            <div class="progress mt-2" style="height: 25px;">
                <div class="progress-bar" role="progressbar"
                     style="width: ${progress}%"
                     aria-valuenow="${progress}"
                     aria-valuemin="0"
                     aria-valuemax="100">
                    ${progress}%
                </div>
            </div>

            <div class="progress-detail">
                已下载: ${task.downloaded_tiles} / ${task.total_tiles} 瓦片
                ${task.failed_tiles > 0 ? `<span class="text-danger">| 失败: ${task.failed_tiles}</span>` : ''}
            </div>
        </div>
    `;
}

function updateTaskCard(task) {
    const card = document.getElementById(`task-${task.id}`);
    if (card) {
        const parent = card.parentElement;
        card.outerHTML = createTaskCard(task);
    }
}

function getStatusColor(status) {
    const colors = {
        'pending': 'secondary',
        'running': 'primary',
        'paused': 'warning',
        'completed': 'success',
        'failed': 'danger',
        'cancelled': 'dark'
    };
    return colors[status] || 'secondary';
}

function getStatusText(status) {
    const texts = {
        'pending': '等待中',
        'running': '运行中',
        'paused': '已暂停',
        'completed': '已完成',
        'failed': '失败',
        'cancelled': '已取消'
    };
    return texts[status] || status;
}

async function startTask(taskId) {
    try {
        const response = await fetch(`/api/tasks/${taskId}/start`, {
            method: 'POST'
        });
        if (response.ok) {
            loadActiveTasks();
        }
    } catch (error) {
        alert('启动任务失败: ' + error.message);
    }
}

async function pauseTask(taskId) {
    try {
        const response = await fetch(`/api/tasks/${taskId}/pause`, {
            method: 'POST'
        });
        if (response.ok) {
            loadActiveTasks();
        }
    } catch (error) {
        alert('暂停任务失败: ' + error.message);
    }
}

async function resumeTask(taskId) {
    try {
        const response = await fetch(`/api/tasks/${taskId}/resume`, {
            method: 'POST'
        });
        if (response.ok) {
            loadActiveTasks();
        }
    } catch (error) {
        alert('恢复任务失败: ' + error.message);
    }
}

async function cancelTask(taskId) {
    if (!confirm('确定要取消这个任务吗？')) {
        return;
    }

    try {
        const response = await fetch(`/api/tasks/${taskId}/cancel`, {
            method: 'POST'
        });
        if (response.ok) {
            loadActiveTasks();
        }
    } catch (error) {
        alert('取消任务失败: ' + error.message);
    }
}
