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
    // createTaskCard 只吐一个空的 .task-error 容器（错误原文不能进 innerHTML），
    // 文本在这里补。漏掉这一步的话，失败当场看得见原因，之后随便来一个新任务
    // 触发整体重绘，红框就变空了。
    tasks.forEach(applyTaskErrorText);
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
                    ${task.status !== 'failed' ? `
                        <button class="btn btn-danger" onclick="cancelTask(${task.id}, '${task.task_type}')" title="取消任务">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <line x1="18" y1="6" x2="6" y2="18"></line>
                                <line x1="6" y1="6" x2="18" y2="18"></line>
                            </svg>
                        </button>
                    ` : ''}
                    ${task.status === 'failed' ? `
                        <button class="btn btn-secondary" onclick="dismissTask(${task.id}, '${task.task_type}')"
                                title="从列表中移除这张失败卡片" aria-label="移除失败任务卡片">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <polyline points="3 6 5 6 21 6"></polyline>
                                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                            </svg>
                            移除
                        </button>
                    ` : ''}
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
                     aria-valuemax="100"></div>
                <span class="progress__label" aria-hidden="true">${progress}%</span>
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

            ${task.status === 'failed' ? `
            <div class="task-error" role="alert"></div>
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
        progressBar.className = `progress-bar bg-${getStatusColor(task.status)}`;
    }

    // 百分比在覆盖层里，不在条里。这行原本是 progressBar.textContent = ...，
    // 是这条路径最容易漏改的一处：卡片初次渲染看不出问题，第一个
    // task_progress 事件一到就会在同一条进度条上多出第二个百分比。
    const progressLabel = card.querySelector('.progress__label');
    if (progressLabel) {
        progressLabel.textContent = `${progress}%`;
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

// 后端没给原因时的兜底文案。空字符串会渲染成一个空红框，比没有红框更让人困惑。
const UNKNOWN_ERROR_TEXT = '任务失败，但后端没有返回失败原因。请查看服务端日志。';

// taskKey -> showToast 返回的句柄。失败 toast 是常驻的（duration: 0），
// 同一个任务重复发 task_failed（等高线任务下载阶段与渲染阶段各有失败出口）
// 会让永不消失的提示白白堆高，所以按 key 合并，新的替掉旧的。
// ⚠️ 只按 key 合并：不同任务的 toast 必须各留一条，失败原因不一样。
const failureToasts = new Map();

function closeFailureToast(key) {
    const t = failureToasts.get(key);
    if (t) {
        t.close();
        failureToasts.delete(key);
    }
}

function handleTaskFailed(taskId, taskType, errorMessage) {
    const key = `${taskType}:${taskId}`;
    const task = activeTasks.get(key);
    if (!task) return;

    task.status = 'failed';
    task.error_message = errorMessage || UNKNOWN_ERROR_TEXT;
    task._key = key;   // applyTaskErrorText 靠它定位卡片；normalizeTask 之外的路径不一定设过

    // 既不 activeTasks.delete 也不删卡片：卡片必须留在页面上。
    // 原实现两件事一起做，于是用户盯着 63% 的进度条，卡片突然消失、零提示，
    // 分不清是失败、被别人取消、还是自己看花了眼。
    // 清理改由用户点卡片上的「移除」按钮触发（dismissTask）。
    activeTasks.set(key, task);

    const card = document.getElementById(`task-${key}`);
    if (card) {
        // 整卡重建而不是逐个改 class：进度条转红、徽章改「失败」、动作按钮
        // 换成「移除」这几件事，createTaskCard 里都已经按 status 分支写好了，
        // 手工同步 DOM 只会多一份会走偏的真相。
        card.outerHTML = createTaskCard(task);
        applyTaskErrorText(task);
    } else {
        renderActiveTasks(Array.from(activeTasks.values()));
    }

    console.error(`Task ${taskId} failed: ${task.error_message}`);
    // duration: 0 → ui.js 里 `if (duration > 0)` 不成立，不挂定时器，
    // toast 一直留到用户自己点 ×。默认的 3500ms 在这里没用：用户离座一趟
    // 回来照样什么都看不到。
    closeFailureToast(key);   // 同一任务只留最新的一条
    failureToasts.set(key, showToast(`任务失败：${task.error_message}`, 'danger', { duration: 0 }));
}

// 把错误文本填进卡片里那个**空的** .task-error 容器。
//
// 为什么不直接拼进 createTaskCard 的模板：那个返回值最终进 innerHTML，
// 而 error_message 是后端异常的字符串化结果（URL、路径、第三方库报错原文
// 都可能在里面），拼进去等于把它当 HTML 解析。ui.js 的 toast 里是同一条规矩。
function applyTaskErrorText(task) {
    if (!task || task.status !== 'failed') return;
    const card = document.getElementById(`task-${task._key}`);
    if (!card) return;
    const box = card.querySelector('.task-error');
    if (!box) return;
    box.textContent = task.error_message || UNKNOWN_ERROR_TEXT;  // textContent 防 XSS
}

// 「移除」按钮：只把失败卡片从界面上拿走，不碰后端。
//
// 失败任务在后端已经是终态，再 POST /cancel 最好的情况也只是白跑一趟。
// 也刻意**没有**「重试」：三个 manager 的 start_task 都要求
// status in ('pending','paused')，对 failed 调用直接抛 ValueError，
// 重试得先改后端状态机。
function dismissTask(taskId, taskType = 'map') {
    const key = `${taskType}:${taskId}`;
    activeTasks.delete(key);
    closeFailureToast(key);   // 卡片都不要了，那条常驻 toast 也别留着占地方
    renderActiveTasks(Array.from(activeTasks.values()));
}

function getStatusColor(status) {
    const colors = {
        'pending': 'secondary',
        // running 用 'info' 而不是 'primary'：徽章侧 .status-badge.running /
        // .badge.bg-primary / .badge.bg-info 是同一条声明块，渲染完全一致；
        // 而进度条侧 .progress-bar.bg-info 已经存在，不必再写 .bg-primary 覆盖。
        'running': 'info',
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
