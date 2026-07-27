let socket;
let activeTasks = new Map();
let timeUpdateInterval = null;

function initTasks() {
    socket = io();

    socket.on('connect', function() {
        console.log('Connected to server');
        loadActiveTasks();
    });

    socket.on('disconnect', function() {
        console.log('Disconnected from server');
    });

    socket.on('task_progress', function(data) {
        console.log('Task progress update:', data);
        updateTaskProgress(data);
    });

    socket.on('task_completed', function(data) {
        console.log('Task completed:', data);
        handleTaskCompleted(data.task_id, data.task_type || 'map');
    });

    socket.on('task_failed', function(data) {
        console.log('Task failed:', data);
        handleTaskFailed(data.task_id, data.task_type || 'map', data.error_message);
    });

    socket.on('task_stitch_progress', function(data) {
        console.log('Task stitch progress:', data);
    });

    // 某个缩放级别拼接失败。任务可能仍在跑(其余级别继续),所以这里只报,不动卡片。
    // 最终判定在后端:全失败 → task_failed;部分失败 → task_completed 带 warning,
    // 同时写进 tasks.error_message。
    socket.on('task_stitch_failed', function(data) {
        console.error(`Task ${data.task_id} zoom ${data.zoom_level} 拼接失败:`, data.error_message);
    });

    // 复制瓦片阶段的心跳。下载进度条此时已经 100%,没有这个事件界面会静止若干分钟。
    socket.on('task_copy_progress', function(data) {
        console.log('Task copy progress:', data);
    });

    loadActiveTasks();

    // 每秒更新一次时长显示
    if (timeUpdateInterval) {
        clearInterval(timeUpdateInterval);
    }
    timeUpdateInterval = setInterval(updateTimeDisplay, 1000);
}

async function loadActiveTasks() {
    try {
        const [mapResp, demResp, localResp, contourResp] = await Promise.all([
            fetch('/api/tasks'),
            fetch('/api/dem/tasks'),
            fetch('/api/terrain/local/tasks'),
            fetch('/api/contour/tasks')
        ]);
        const mapData = await mapResp.json();
        const demData = await demResp.json();
        const localData = await localResp.json();
        const contourData = await contourResp.json();

        const mapTasks = (mapData.tasks || []).map(t => normalizeTask(t, 'map'));
        const demTasks = (demData.tasks || []).map(t => normalizeTask(t, 'dem'));
        const localTasks = (localData.tasks || []).map(t => normalizeTask(t, 'local_terrain'));
        const contourTasks = (contourData.tasks || []).map(t => normalizeTask(t, 'contour'));
        const all = [...mapTasks, ...demTasks, ...localTasks, ...contourTasks].filter(t =>
            ['pending', 'running', 'paused'].includes(t.status)
        );

        activeTasks.clear();
        all.forEach(task => {
            activeTasks.set(task._key, task);
        });

        renderActiveTasks(all);
    } catch (error) {
        console.error('Failed to load tasks:', error);
    }
}

// Contour tasks run two phases: download DEM, then render contour tiles.
// `phase` ("download"/"render") comes from the backend; we fall back to the
// render counts once tiles have started so a single progress bar tracks the
// currently-active phase.
function contourPhaseCounts(task) {
    const totalTiles = task.total_tiles || 0;
    const renderStarted = (task.phase === 'render') || totalTiles > 0;
    if (renderStarted) {
        return {
            total: totalTiles,
            done: task.rendered_tiles || 0,
            failed: task.failed_tiles || 0,
            label: '瓦片',
            verb: '渲染等高线瓦片'
        };
    }
    return {
        total: task.total_files || 0,
        done: task.downloaded_files || 0,
        failed: task.failed_files || 0,
        label: 'DEM',
        verb: '下载 DEM'
    };
}

function normalizeTask(task, type) {
    if (type === 'dem') {
        return {
            ...task,
            task_type: 'dem',
            id: task.id,
            _key: `dem:${task.id}`,
            total_items: task.total_files || 0,
            downloaded_items: task.downloaded_files || 0,
            failed_items: task.failed_files || 0,
            items_label: '文件'
        };
    }
    if (type === 'local_terrain') {
        const total = task.total_files || 0;
        const done = task.status === 'completed' ? total : (task.uploaded_files || 0);
        return {
            ...task,
            task_type: 'local_terrain',
            id: task.id,
            _key: `local_terrain:${task.id}`,
            total_items: total,
            downloaded_items: done,
            failed_items: task.failed_files || 0,
            items_label: '文件'
        };
    }
    if (type === 'contour') {
        const counts = contourPhaseCounts(task);
        return {
            ...task,
            task_type: 'contour',
            id: task.id,
            _key: `contour:${task.id}`,
            total_items: counts.total,
            downloaded_items: counts.done,
            failed_items: counts.failed,
            items_label: counts.label,
            progress_verb: counts.verb
        };
    }
    return {
        ...task,
        task_type: 'map',
        id: task.id,
        _key: `map:${task.id}`,
        total_items: task.total_tiles || 0,
        downloaded_items: task.downloaded_tiles || 0,
        failed_items: task.failed_tiles || 0,
        items_label: '瓦片'
    };
}

function renderActiveTasks(tasks) {
    const container = document.getElementById('activeTasks');

    if (tasks.length === 0) {
        container.innerHTML = `
            <div style="text-align:center; padding:2rem 1rem; color:var(--color-text-muted);">
              <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="opacity:0.4; margin-bottom:0.75rem;">
                <line x1="22" y1="12" x2="18" y2="12"></line><line x1="6" y1="12" x2="2" y2="12"></line>
                <line x1="12" y1="2" x2="12" y2="6"></line><line x1="12" y1="18" x2="12" y2="22"></line>
              </svg>
              <p style="margin:0;">暂无活动任务</p>
            </div>
        `;
        return;
    }

    container.innerHTML = tasks.map(task => createTaskCard(task)).join('');
}

function createTaskCard(task) {
    const progress = task.total_items > 0
        ? Math.round((task.downloaded_items / task.total_items) * 100)
        : 0;

    const timeInfo = calculateTimeInfo(task);

    const statusIcons = {
        'pending': '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>',
        'running': '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>',
        'paused': '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="4" width="4" height="16"></rect><rect x="14" y="4" width="4" height="16"></rect></svg>',
        'completed': '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"></polyline></svg>',
        'failed': '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line></svg>'
    };

    const supportsPauseResume = task.task_type !== 'local_terrain';

    return `
        <div class="task-card status-${task.status}" id="task-${task._key}">
            <div class="d-flex justify-content-between align-items-center" style="margin-bottom: 0.75rem; gap: 0.5rem;">
                <h6 style="margin-bottom: 0; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${task.name}</h6>
                <span class="badge bg-${getStatusColor(task.status)}" style="display: inline-flex; align-items: center; gap: 4px; flex-shrink: 0;">
                    ${statusIcons[task.status] || ''}
                    ${getStatusText(task.status)}
                </span>
            </div>
            <div class="d-flex justify-content-end" style="margin-bottom: 0.75rem;">
                <div class="btn-group btn-group-sm">
                    ${supportsPauseResume && task.status === 'pending' ? `
                        <button class="btn btn-success" onclick="startTask(${task.id}, '${task.task_type}')" title="启动任务">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <polygon points="5 3 19 12 5 21 5 3"></polygon>
                            </svg>
                        </button>
                    ` : ''}
                    ${supportsPauseResume && task.status === 'running' ? `
                        <button class="btn btn-warning" onclick="pauseTask(${task.id}, '${task.task_type}')" title="暂停任务">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <rect x="6" y="4" width="4" height="16"></rect>
                                <rect x="14" y="4" width="4" height="16"></rect>
                            </svg>
                        </button>
                    ` : ''}
                    ${supportsPauseResume && task.status === 'paused' ? `
                        <button class="btn btn-success" onclick="resumeTask(${task.id}, '${task.task_type}')" title="恢复任务">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <polygon points="5 3 19 12 5 21 5 3"></polygon>
                            </svg>
                        </button>
                    ` : ''}
                    <button class="btn btn-danger" onclick="cancelTask(${task.id}, '${task.task_type}')" title="取消任务">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <line x1="18" y1="6" x2="6" y2="18"></line>
                            <line x1="6" y1="6" x2="18" y2="18"></line>
                        </svg>
                    </button>
                </div>
            </div>

            <div class="progress-detail" style="margin-bottom: 0.5rem;">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display: inline-block; vertical-align: middle; margin-right: 4px;">
                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                    <polyline points="7 10 12 15 17 10"></polyline>
                    <line x1="12" y1="15" x2="12" y2="3"></line>
                </svg>
                ${task.progress_verb || '已下载'}: ${task.downloaded_items} / ${task.total_items} ${task.items_label}
                ${task.failed_items > 0 ? `<span style="color: var(--color-danger); margin-left: 8px;">| 失败: ${task.failed_items}</span>` : ''}
            </div>

            <div class="progress" style="height: 28px; margin-bottom: 0.75rem;">
                <div class="progress-bar bg-${getStatusColor(task.status)}" role="progressbar"
                     style="width: ${progress}%"
                     aria-valuenow="${progress}"
                     aria-valuemin="0"
                     aria-valuemax="100">
                    ${progress}%
                </div>
            </div>

            ${timeInfo.show ? `
            <div class="progress-detail" style="margin-top: 0.5rem;">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display: inline-block; vertical-align: middle; margin-right: 4px;">
                    <circle cx="12" cy="12" r="10"></circle>
                    <polyline points="12 6 12 12 16 14"></polyline>
                </svg>
                ${timeInfo.elapsed ? `已运行: ${timeInfo.elapsed}` : ''}
                ${timeInfo.elapsed && timeInfo.estimated ? ' | ' : ''}
                ${timeInfo.estimated ? `预计剩余: ${timeInfo.estimated}` : ''}
            </div>
            ` : ''}
        </div>
    `;
}

function updateTaskProgress(data) {
    const taskType = data.task_type || 'map';
    const taskId = data.task_id || data.id;
    const key = `${taskType}:${taskId}`;
    let task = activeTasks.get(key);

    if (task) {
        const statusChanged = data.status && data.status !== task.status;

        if (taskType === 'local_terrain') {
            const normalized = normalizeTask(data, 'local_terrain');
            const progressChanged = normalized.downloaded_items !== task.downloaded_items ||
                                   normalized.failed_items !== task.failed_items;

            normalized._key = key;
            activeTasks.set(key, normalized);

            const card = document.getElementById(`task-${key}`);
            if (card) {
                if (statusChanged) {
                    card.outerHTML = createTaskCard(normalized);
                } else if (progressChanged) {
                    updateTaskProgressPartial(card, normalized);
                }
            }
            return;
        }

        if (taskType === 'dem') {
            const progressChanged = data.downloaded_files !== task.downloaded_files ||
                                   data.failed_files !== task.failed_files;

            task.id = taskId;
            task.task_type = 'dem';
            task._key = key;
            task.name = data.name || task.name;
            task.status = data.status || task.status;
            task.downloaded_files = data.downloaded_files;
            task.failed_files = data.failed_files;
            task.total_files = data.total_files;
            task.total_items = data.total_files || 0;
            task.downloaded_items = data.downloaded_files || 0;
            task.failed_items = data.failed_files || 0;
            task.items_label = '文件';
            task.output_path = data.output_path || task.output_path;
            task.started_at = data.started_at || task.started_at;
            task.created_at = data.created_at || task.created_at;

            activeTasks.set(key, task);

            const card = document.getElementById(`task-${key}`);
            if (card) {
                if (statusChanged) {
                    card.outerHTML = createTaskCard(task);
                } else if (progressChanged) {
                    updateTaskProgressPartial(card, task);
                }
            }
            return;
        }

        if (taskType === 'contour') {
            // Phase-aware: download counts until rendering starts, then tiles.
            const phaseChanged = (data.phase || task.phase) !== task.phase;
            const progressChanged = phaseChanged ||
                                   data.downloaded_files !== task.downloaded_files ||
                                   data.failed_files !== task.failed_files ||
                                   data.rendered_tiles !== task.rendered_tiles ||
                                   data.failed_tiles !== task.failed_tiles;

            task.id = taskId;
            task.task_type = 'contour';
            task._key = key;
            task.name = data.name || task.name;
            task.status = data.status || task.status;
            task.phase = data.phase || task.phase;
            task.total_files = data.total_files;
            task.downloaded_files = data.downloaded_files;
            task.failed_files = data.failed_files;
            task.total_tiles = data.total_tiles;
            task.rendered_tiles = data.rendered_tiles;
            task.failed_tiles = data.failed_tiles;
            task.zoom_min = data.zoom_min !== undefined ? data.zoom_min : task.zoom_min;
            task.zoom_max = data.zoom_max !== undefined ? data.zoom_max : task.zoom_max;
            task.contour_interval = data.contour_interval !== undefined ? data.contour_interval : task.contour_interval;
            task.started_at = data.started_at || task.started_at;
            task.created_at = data.created_at || task.created_at;

            const counts = contourPhaseCounts(task);
            task.total_items = counts.total;
            task.downloaded_items = counts.done;
            task.failed_items = counts.failed;
            task.items_label = counts.label;
            task.progress_verb = counts.verb;

            activeTasks.set(key, task);

            const card = document.getElementById(`task-${key}`);
            if (card) {
                if (statusChanged || phaseChanged) {
                    card.outerHTML = createTaskCard(task);
                } else if (progressChanged) {
                    updateTaskProgressPartial(card, task);
                }
            }
            return;
        }

        const progressChanged = data.downloaded_tiles !== task.downloaded_tiles ||
                               data.failed_tiles !== task.failed_tiles;

        task.id = taskId;
        task.task_type = 'map';
        task._key = key;
        task.name = data.name || task.name;
        task.status = data.status || task.status;
        task.downloaded_tiles = data.downloaded_tiles;
        task.failed_tiles = data.failed_tiles;
        task.total_tiles = data.total_tiles;
        task.total_items = data.total_tiles || 0;
        task.downloaded_items = data.downloaded_tiles || 0;
        task.failed_items = data.failed_tiles || 0;
        task.items_label = '瓦片';
        task.north = data.north !== undefined ? data.north : task.north;
        task.south = data.south !== undefined ? data.south : task.south;
        task.east = data.east !== undefined ? data.east : task.east;
        task.west = data.west !== undefined ? data.west : task.west;
        task.zoom_min = data.zoom_min !== undefined ? data.zoom_min : task.zoom_min;
        task.zoom_max = data.zoom_max !== undefined ? data.zoom_max : task.zoom_max;
        task.style = data.style || task.style;
        task.output_format = data.output_format || task.output_format;
        task.output_path = data.output_path || task.output_path;
        task.started_at = data.started_at || task.started_at;
        task.created_at = data.created_at || task.created_at;
        task.total_running_seconds = data.total_running_seconds !== undefined ? data.total_running_seconds : task.total_running_seconds;

        activeTasks.set(key, task);

        const card = document.getElementById(`task-${key}`);
        if (card) {
            if (statusChanged) {
                card.outerHTML = createTaskCard(task);
            } else if (progressChanged) {
                updateTaskProgressPartial(card, task);
            }
        }
    } else {
        // New task - normalize and render
        activeTasks.set(key, normalizeTask(data, taskType));
        renderActiveTasks(Array.from(activeTasks.values()));
    }
}

function updateTaskProgressPartial(card, task) {
    const progress = task.total_items > 0
        ? Math.round((task.downloaded_items / task.total_items) * 100)
        : 0;

    // 更新进度条
    const progressBar = card.querySelector('.progress-bar');
    if (progressBar) {
        progressBar.style.width = `${progress}%`;
        progressBar.setAttribute('aria-valuenow', progress);
        progressBar.textContent = `${progress}%`;
        progressBar.className = `progress-bar bg-${getStatusColor(task.status)}`;
    }

    // 更新下载数量
    const progressDetails = card.querySelectorAll('.progress-detail');
    if (progressDetails.length > 0) {
        const downloadDetail = progressDetails[0];
        const failedText = task.failed_items > 0
            ? `<span style="color: var(--color-danger); margin-left: 8px;">| 失败: ${task.failed_items}</span>`
            : '';

        downloadDetail.innerHTML = `
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display: inline-block; vertical-align: middle; margin-right: 4px;">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                <polyline points="7 10 12 15 17 10"></polyline>
                <line x1="12" y1="15" x2="12" y2="3"></line>
            </svg>
            ${task.progress_verb || '已下载'}: ${task.downloaded_items} / ${task.total_items} ${task.items_label}
            ${failedText}
        `;
    }
}

function handleTaskCompleted(taskId, taskType) {
    const key = `${taskType}:${taskId}`;
    const task = activeTasks.get(key);
    if (task) {
        task.status = 'completed';
        activeTasks.delete(key);

        const card = document.getElementById(`task-${key}`);
        if (card) {
            card.remove();
        }

        renderActiveTasks(Array.from(activeTasks.values()));
    }
}

function handleTaskFailed(taskId, taskType, errorMessage) {
    const key = `${taskType}:${taskId}`;
    const task = activeTasks.get(key);
    if (task) {
        task.status = 'failed';
        task.error_message = errorMessage;
        activeTasks.delete(key);

        const card = document.getElementById(`task-${key}`);
        if (card) {
            card.remove();
        }

        renderActiveTasks(Array.from(activeTasks.values()));

        if (errorMessage) {
            console.error(`Task ${taskId} failed: ${errorMessage}`);
        }
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

function calculateTimeInfo(task) {
    const result = {
        show: false,
        elapsed: null,
        estimated: null
    };

    if (!task.started_at || task.status === 'pending') {
        return result;
    }

    result.show = true;

    // 使用后端计算的总运行时长
    let elapsedSeconds = task.total_running_seconds || 0;

    // 如果任务正在运行，加上当前这一段的时间
    if (task.status === 'running' && task.started_at) {
        const startTime = new Date(task.started_at);
        const now = new Date();
        const currentSegment = (now - startTime) / 1000;
        elapsedSeconds += currentSegment;
    }

    result.elapsed = formatDuration(elapsedSeconds);

    if (task.status === 'running' && task.downloaded_items > 0 && task.total_items > 0) {
        const progress = task.downloaded_items / task.total_items;
        const estimatedTotalSeconds = elapsedSeconds / progress;
        const remainingSeconds = estimatedTotalSeconds - elapsedSeconds;

        if (remainingSeconds > 0) {
            result.estimated = formatDuration(remainingSeconds);
        }
    }

    return result;
}

function updateTimeDisplay() {
    activeTasks.forEach((task, taskId) => {
        // 只更新运行中的任务时间
        if (task.status === 'running') {
            const card = document.getElementById(`task-${taskId}`);
            if (card) {
                const timeInfo = calculateTimeInfo(task);
                const timeDetailDiv = card.querySelector('.progress-detail:last-child');

                if (timeInfo.show && timeDetailDiv) {
                    const timeHtml = `
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display: inline-block; vertical-align: middle; margin-right: 4px;">
                            <circle cx="12" cy="12" r="10"></circle>
                            <polyline points="12 6 12 12 16 14"></polyline>
                        </svg>
                        ${timeInfo.elapsed ? `已运行: ${timeInfo.elapsed}` : ''}
                        ${timeInfo.elapsed && timeInfo.estimated ? ' | ' : ''}
                        ${timeInfo.estimated ? `预计剩余: ${timeInfo.estimated}` : ''}
                    `;

                    if (timeDetailDiv.innerHTML.includes('已运行') || timeDetailDiv.innerHTML.includes('预计剩余')) {
                        timeDetailDiv.innerHTML = timeHtml;
                    }
                }
            }
        }
    });
}

function apiPrefixForType(taskType) {
    if (taskType === 'dem') return '/api/dem/tasks';
    if (taskType === 'local_terrain') return '/api/terrain/local/tasks';
    if (taskType === 'contour') return '/api/contour/tasks';
    return '/api/tasks';
}

async function startTask(taskId, taskType = 'map') {
    try {
        const response = await fetch(`${apiPrefixForType(taskType)}/${taskId}/start`, {
            method: 'POST'
        });
        if (!response.ok) {
            throw new Error('启动任务失败');
        }
    } catch (error) {
        showToast('启动任务失败: ' + error.message, 'danger');
    }
}

async function pauseTask(taskId, taskType = 'map') {
    try {
        const response = await fetch(`${apiPrefixForType(taskType)}/${taskId}/pause`, {
            method: 'POST'
        });
        if (!response.ok) {
            throw new Error('暂停任务失败');
        }
    } catch (error) {
        showToast('暂停任务失败: ' + error.message, 'danger');
    }
}

async function resumeTask(taskId, taskType = 'map') {
    try {
        const response = await fetch(`${apiPrefixForType(taskType)}/${taskId}/resume`, {
            method: 'POST'
        });
        if (!response.ok) {
            throw new Error('恢复任务失败');
        }
    } catch (error) {
        showToast('恢复任务失败: ' + error.message, 'danger');
    }
}

async function cancelTask(taskId, taskType = 'map') {
    if (!await showConfirm('确定要取消这个任务吗？', { title: '取消任务', danger: true })) {
        return;
    }

    try {
        const response = await fetch(`${apiPrefixForType(taskType)}/${taskId}/cancel`, {
            method: 'POST'
        });
        if (response.ok) {
            const key = `${taskType}:${taskId}`;
            activeTasks.delete(key);
            const card = document.getElementById(`task-${key}`);
            if (card) {
                card.remove();
            }
            renderActiveTasks(Array.from(activeTasks.values()));
        } else {
            const result = await response.json().catch(() => ({}));
            showToast('取消任务失败: ' + (result.error || response.status), 'danger');
        }
    } catch (error) {
        showToast('取消任务失败: ' + error.message, 'danger');
    }
}
