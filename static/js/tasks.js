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
        const data = await response.json();

        const activeTasks = data.tasks.filter(t =>
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
        container.innerHTML = `
            <div style="text-align: center; padding: 2rem;">
                <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="opacity: 0.3; margin-bottom: 1rem;">
                    <circle cx="12" cy="12" r="10"></circle>
                    <line x1="12" y1="8" x2="12" y2="12"></line>
                    <line x1="12" y1="16" x2="12.01" y2="16"></line>
                </svg>
                <p class="text-muted" style="margin: 0;">暂无活动任务</p>
            </div>
        `;
        return;
    }

    container.innerHTML = tasks.map(task => createTaskCard(task)).join('');
}

function createTaskCard(task) {
    const progress = task.total_tiles > 0
        ? Math.round((task.downloaded_tiles / task.total_tiles) * 100)
        : 0;

    const statusIcons = {
        'pending': '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>',
        'running': '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>',
        'paused': '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="4" width="4" height="16"></rect><rect x="14" y="4" width="4" height="16"></rect></svg>',
        'completed': '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"></polyline></svg>',
        'failed': '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line></svg>'
    };

    return `
        <div class="task-card status-${task.status}" id="task-${task.id}">
            <div class="d-flex justify-content-between align-items-start" style="margin-bottom: 0.75rem;">
                <div>
                    <h6 style="margin-bottom: 0.5rem;">${task.name}</h6>
                    <span class="badge bg-${getStatusColor(task.status)}" style="display: inline-flex; align-items: center; gap: 4px;">
                        ${statusIcons[task.status] || ''}
                        ${getStatusText(task.status)}
                    </span>
                </div>
                <div class="btn-group btn-group-sm">
                    ${task.status === 'pending' ? `
                        <button class="btn btn-success" onclick="startTask(${task.id})" title="启动任务">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <polygon points="5 3 19 12 5 21 5 3"></polygon>
                            </svg>
                        </button>
                    ` : ''}
                    ${task.status === 'running' ? `
                        <button class="btn btn-warning" onclick="pauseTask(${task.id})" title="暂停任务">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <rect x="6" y="4" width="4" height="16"></rect>
                                <rect x="14" y="4" width="4" height="16"></rect>
                            </svg>
                        </button>
                    ` : ''}
                    ${task.status === 'paused' ? `
                        <button class="btn btn-success" onclick="resumeTask(${task.id})" title="恢复任务">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <polygon points="5 3 19 12 5 21 5 3"></polygon>
                            </svg>
                        </button>
                    ` : ''}
                    <button class="btn btn-danger" onclick="cancelTask(${task.id})" title="取消任务">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <line x1="18" y1="6" x2="6" y2="18"></line>
                            <line x1="6" y1="6" x2="18" y2="18"></line>
                        </svg>
                    </button>
                </div>
            </div>

            <div class="progress" style="height: 28px; margin-bottom: 0.75rem;">
                <div class="progress-bar bg-${getProgressColor(progress)}" role="progressbar"
                     style="width: ${progress}%"
                     aria-valuenow="${progress}"
                     aria-valuemin="0"
                     aria-valuemax="100">
                    ${progress}%
                </div>
            </div>

            <div class="progress-detail">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display: inline-block; vertical-align: middle; margin-right: 4px;">
                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                    <polyline points="7 10 12 15 17 10"></polyline>
                    <line x1="12" y1="15" x2="12" y2="3"></line>
                </svg>
                已下载: ${task.downloaded_tiles} / ${task.total_tiles} 瓦片
                ${task.failed_tiles > 0 ? `<span style="color: var(--color-danger); margin-left: 8px;">| 失败: ${task.failed_tiles}</span>` : ''}
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

function getProgressColor(progress) {
    if (progress >= 100) return 'success';
    if (progress >= 75) return 'info';
    if (progress >= 50) return 'primary';
    if (progress >= 25) return 'warning';
    return 'danger';
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
