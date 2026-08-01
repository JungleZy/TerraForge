let socket;
let activeTasks = new Map();
let timeUpdateInterval = null;

function initTasks() {
    socket = io();
    if (window.initConnectionStatus) window.initConnectionStatus(socket);

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
        updateStatusTasks();
    });

    socket.on('task_completed', function(data) {
        console.log('Task completed:', data);
        handleTaskCompleted(data.task_id, data.task_type || 'map', data.warning);
        pushStatusEvent('任务 #' + data.task_id + ' 已完成');
        updateStatusTasks();
    });

    socket.on('task_failed', function(data) {
        console.log('Task failed:', data);
        handleTaskFailed(data.task_id, data.task_type || 'map', data.error_message);
        pushStatusEvent('任务 #' + data.task_id + ' 失败');
        updateStatusTasks();
    });

    socket.on('task_stitch_progress', function(data) {
        console.log('Task stitch progress:', data);
        pushStatusEvent('任务 #' + data.task_id + ' 拼接瓦片中…');
    });

    // 某个缩放级别拼接失败。任务可能仍在跑(其余级别继续),所以这里只报,不动行。
    // 最终判定在后端:全失败 → task_failed;部分失败 → task_completed 带 warning,
    // 同时写进 tasks.error_message。
    socket.on('task_stitch_failed', function(data) {
        console.error(`Task ${data.task_id} zoom ${data.zoom_level} 拼接失败:`, data.error_message);
    });

    // 复制瓦片阶段的心跳。下载进度条此时已经 100%,没有这个事件界面会静止若干分钟。
    socket.on('task_copy_progress', function(data) {
        console.log('Task copy progress:', data);
        pushStatusEvent('任务 #' + data.task_id + ' 复制瓦片中…');
    });

    loadActiveTasks();
    updateStatusTasks();

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
        // 四路任何一路非 2xx 都不能接着解析渲染——失败响应的 body 不是任务
        // 列表，会被当成「没有活动任务」把整页卡片清空，看起来就像任务全没了。
        const badResp = [mapResp, demResp, localResp, contourResp].find(r => !r.ok);
        if (badResp) {
            throw new Error('任务列表接口返回 HTTP ' + badResp.status);
        }
        const mapData = await mapResp.json();
        const demData = await demResp.json();
        const localData = await localResp.json();
        const contourData = await contourResp.json();

        const mapTasks = (mapData.tasks || []).map(t => normalizeTask(t, 'map'));
        const demTasks = (demData.tasks || []).map(t => normalizeTask(t, 'dem'));
        const localTasks = (localData.tasks || []).map(t => normalizeTask(t, 'local_terrain'));
        const contourTasks = (contourData.tasks || []).map(t => normalizeTask(t, 'contour'));
        const all = [...mapTasks, ...demTasks, ...localTasks, ...contourTasks].filter(t =>
            // failed 也保留：失败行必须常驻（与 handleTaskFailed 的约定一致），
            // 否则刷新页面后失败任务无声消失，用户无从得知失败原因。
            // 移除行仍是用户点「移除」按钮（dismissTask）的事。
            ['pending', 'running', 'paused', 'failed'].includes(t.status)
        );

        activeTasks.clear();
        all.forEach(task => {
            activeTasks.set(task._key, task);
        });

        renderActiveTasks(all);

        // 精确去重的时序收尾：history.js 的去重读的是 activeTasks 这个 Map——
        // 若历史流先于本函数渲染完（两路 fetch 的竞争，dev 实测 map:216 两边
        // 各出现一次），实时区里的任务会在历史流里重复。activeTasks 就绪后
        // 让历史流按最新 Map 重排一次；带搜索词时走 filterTasks 保住过滤。
        // allTasks / renderHistoryTable / filterTasks 都是 history.js 的全局
        // （独立页不加载本文件，typeof 守卫兜底）。
        if (typeof renderHistoryTable === 'function'
                && typeof allTasks !== 'undefined' && allTasks.length) {
            const searchEl = document.getElementById('searchInput');
            const term = searchEl ? searchEl.value : '';
            if (typeof filterTasks === 'function') {
                filterTasks(term);
            } else {
                renderHistoryTable(allTasks);
            }
        }
    } catch (error) {
        console.error('Failed to load tasks:', error);
        showToast('加载任务列表失败: ' + error.message, 'danger');
    }
}

// Contour tasks run two phases: download DEM, then render contour tiles.
// `phase` ("download"/"render") comes from the backend; we fall back to the
// render counts once tiles have started so a single progress bar tracks the
// currently-active phase.
// 上传来源的任务（dataset='upload'，与后端 is_upload 同一判定）没有下载阶段：
// 文件计数在创建时就已记满（downloaded_files == total_files），拿它当进度
// 会让 pending 任务一出现就显示 100%「下载 DEM」——直接按渲染阶段显示。
function contourPhaseCounts(task) {
    const totalTiles = task.total_tiles || 0;
    const renderStarted = task.dataset === 'upload' || (task.phase === 'render') || totalTiles > 0;
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

// 失败组的折叠阈值与预览条数（2026-08 统一流式列表定稿）。
// dev 库实测 96 个 failed 任务全部钉在列表顶部、形成一面「红墙」——
// 这是用户连续反馈「太乱」的三条根因之一。治理方式：>5 个时默认折叠、
// 只显示最近 3 个（按 id 倒序近似最新），点分组头展开全部；≤5 个时全部
// 显示且分组头不可折叠。
const FAILED_GROUP_COLLAPSE_THRESHOLD = 5;
const FAILED_GROUP_PREVIEW_COUNT = 3;

// 折叠态默认 true（折叠），sessionStorage 记忆用户上一次的展开/收起。
// try/catch：file:// 或禁用存储的环境下 sessionStorage 会抛 SecurityError。
let failedGroupCollapsed = true;
try {
    failedGroupCollapsed = sessionStorage.getItem('taskFailedGroupCollapsed') !== '0';
} catch (e) { /* 存储不可用时用默认值 */ }

function toggleFailedTaskGroup() {
    failedGroupCollapsed = !failedGroupCollapsed;
    try {
        sessionStorage.setItem('taskFailedGroupCollapsed', failedGroupCollapsed ? '1' : '0');
    } catch (e) { /* 同上 */ }
    renderActiveTasks(Array.from(activeTasks.values()));
}

function renderActiveTasks(tasks) {
    // 活动任务渲染进记录面板列表顶部的实时区（#activeTasksBody，
    // 在历史流 #historyTableBody 之上）。2026-08 重设计：废掉 9 列表格，
    // 实时区改为「活动 / 失败」两个分组的统一流式行，与历史行同一种行语言。
    const container = document.getElementById('activeTasksBody');
    if (!container) return;

    // 空态（定稿设计）：无活动任务时实时区整个留空，不显示「活动」分组头，
    // 也不渲染「暂无活动任务」——列表区只有历史流的空态提示这一种空态。
    if (tasks.length === 0) {
        container.innerHTML = '';
        updateStatusTasks();
        return;
    }

    const live = tasks.filter(t => ['pending', 'running', 'paused'].includes(t.status));
    const failed = tasks.filter(t => t.status === 'failed')
        // id 近似时序：折叠时留下的「最近 3 个」按 id 倒序取前 3。
        .sort((a, b) => b.id - a.id);

    const parts = [];
    if (live.length > 0) {
        parts.push(`<div class="task-group-header">活动 (${live.length})</div>`);
        parts.push(live.map(createTaskRow).join(''));
    }
    if (failed.length > 0) {
        const collapsible = failed.length > FAILED_GROUP_COLLAPSE_THRESHOLD;
        const shown = (collapsible && failedGroupCollapsed)
            ? failed.slice(0, FAILED_GROUP_PREVIEW_COUNT)
            : failed;
        // 分组头可折叠时整头是一个 <button>（展开/收起全部），
        // 不可折叠（≤5 个）时是普通小字标题。
        parts.push(collapsible
            ? `<button type="button" class="task-group-header task-group-header--toggle" onclick="toggleFailedTaskGroup()" aria-expanded="${!failedGroupCollapsed}">失败 (${failed.length}) ${failedGroupCollapsed ? '▸' : '▾'}</button>`
            : `<div class="task-group-header">失败 (${failed.length})</div>`);
        parts.push(shown.map(task => createTaskRow(task) + createTaskErrorRow(task)).join(''));
    }
    container.innerHTML = parts.join('');
    // createTaskErrorRow 只吐一个空的 .task-error 容器（错误原文不能进 innerHTML），
    // 文本在这里补。漏掉这一步的话，失败当场看得见原因，之后随便来一个新任务
    // 触发整体重绘，红框就变空了。折叠时未渲染的行 applyTaskErrorText 找不到
    // 容器会直接返回，无副作用。
    tasks.forEach(applyTaskErrorText);
    updateStatusTasks();
}

// --- 底部状态栏：活动任务聚合 + 最近事件 ----------------------------------------
// 状态栏元素只在首页存在（#statusTasksText 等），独立页不加载 tasks.js，
// 这里仍全部做 null 守卫，避免未来被其它页引入时报错。

// 活动任务读数：进行中（pending/running/paused）任务数 + 汇总进度条。
// failed 行留在 activeTasks 里等用户移除，但不算「活动」。
function updateStatusTasks() {
    const textEl = document.getElementById('statusTasksText');
    if (!textEl) return;
    const barWrap = document.getElementById('statusTasksProgress');
    const barFill = document.getElementById('statusTasksBar');
    const live = Array.from(activeTasks.values())
        .filter(t => ['pending', 'running', 'paused'].includes(t.status));
    if (live.length === 0) {
        textEl.textContent = '无活动任务';
        if (barWrap) barWrap.hidden = true;
        return;
    }
    const running = live.filter(t => t.status === 'running').length;
    let total = 0;
    let done = 0;
    live.forEach(t => {
        total += t.total_items || 0;
        done += t.downloaded_items || 0;
    });
    const pct = total > 0 ? Math.round((done / total) * 100) : 0;
    textEl.textContent = `${live.length} 个活动任务（${running} 运行中） ${pct}%`;
    if (barWrap && barFill) {
        barWrap.hidden = false;
        barFill.style.width = pct + '%';
    }
}

// 最近事件单行读数：任务完成/失败/拼接/复制阶段的文字心跳。
// 这些 socket 事件原本只 console.log，状态栏是唯一让消费者。
function pushStatusEvent(msg) {
    const el = document.getElementById('statusEvent');
    if (!el) return;
    el.textContent = msg;
}

// 行1 的元信息片段（#类型:id 之后）：地图/等高线是「样式 缩放」，
// 高程是数据源名，本地高程切片固定文案。返回值会经 escapeHtml 进模板。
function taskMetaText(task) {
    if (task.task_type === 'map' || task.task_type === 'contour') {
        // getStyleText 定义在 history.js（首页两个文件都加载）；拿不到就显示原文。
        const styleText = task.task_type === 'contour'
            ? '等高线'
            : (task.style
                ? (typeof getStyleText === 'function' ? getStyleText(task.style) : task.style)
                : '');
        const zoom = (task.zoom_min != null && task.zoom_max != null)
            ? `${task.zoom_min}~${task.zoom_max}`
            : '';
        return [styleText, zoom].filter(Boolean).join(' ');
    }
    if (task.task_type === 'dem') {
        return task.dataset || '高程';
    }
    return '本地高程切片';
}

function createTaskRow(task) {
    const progress = task.total_items > 0
        ? Math.round((task.downloaded_items / task.total_items) * 100)
        : 0;

    const timeInfo = calculateTimeInfo(task);
    const timeText = timeInfo.show
        ? [timeInfo.elapsed ? `已运行: ${timeInfo.elapsed}` : '',
           timeInfo.estimated ? `预计剩余: ${timeInfo.estimated}` : '']
            .filter(Boolean).join(' · ')
        : '—';

    const isFailed = task.status === 'failed';
    const supportsPauseResume = task.task_type !== 'local_terrain';

    // 统一流式行（2026-08 重设计定稿，取代 2026-07 的「富行单格三行」）：
    // 活动任务与历史任务用**同一种**行结构——这是「太乱」三条根因之一的
    // 「两种行语言硬拼」的解法。行内两行 flex：
    //   行1 状态点(.task-dot) + 名称 + #类型:id + 元信息(样式/缩放) + 状态小字
    //       …… 耗时(mono, margin-left:auto 顶右端) + btn-icon 动作组
    //   行2 pending/running/paused → 5px 发丝进度条 + 条外百分比(.task-pct)
    //       + 计数(.task-count)；failed → 不渲染行2，引文式错误行紧随其后
    //       （createTaskErrorRow 的兄弟节点）
    // 不再有状态徽章 pill（状态识别 = 状态点配色 + 小字状态文本），不再有
    // 整行底色、不再有 4px 左条；行间只有发丝分隔线（CSS 承担）。
    // .progress-bar/.task-pct/.task-count/.task-time 是 Socket.IO 增量更新
    // 依赖的稳定类名（updateTaskProgressPartial / updateTimeDisplay），不能换。
    return `
        <div class="task-row status-${task.status}" id="task-${task._key}">
            <div class="task-line1">
                <span class="task-dot" aria-hidden="true"></span>
                <span class="task-name">${escapeHtml(task.name)}</span>
                <span class="task-id">#${escapeHtml(task._key)}</span>
                <span class="task-meta">${escapeHtml(taskMetaText(task))}</span>
                <span class="task-status-text">${escapeHtml(getStatusText(task.status))}</span>
                <span class="task-time progress-detail">${timeText}</span>
                <div class="btn-group btn-group-sm">
                    ${supportsPauseResume && task.status === 'pending' ? `
                        <button class="btn btn-icon btn-success" onclick="startTask(${task.id}, '${task.task_type}')" title="启动任务" aria-label="启动任务">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <polygon points="5 3 19 12 5 21 5 3"></polygon>
                            </svg>
                        </button>
                    ` : ''}
                    ${supportsPauseResume && task.status === 'running' ? `
                        <button class="btn btn-icon btn-warning" onclick="pauseTask(${task.id}, '${task.task_type}')" title="暂停任务" aria-label="暂停任务">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <rect x="6" y="4" width="4" height="16"></rect>
                                <rect x="14" y="4" width="4" height="16"></rect>
                            </svg>
                        </button>
                    ` : ''}
                    ${supportsPauseResume && task.status === 'paused' ? `
                        <button class="btn btn-icon btn-success" onclick="resumeTask(${task.id}, '${task.task_type}')" title="恢复任务" aria-label="恢复任务">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <polygon points="5 3 19 12 5 21 5 3"></polygon>
                            </svg>
                        </button>
                    ` : ''}
                    ${task.status !== 'failed' ? `
                        <button class="btn btn-icon btn-danger" onclick="cancelTask(${task.id}, '${task.task_type}')" title="取消任务" aria-label="取消任务">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <line x1="18" y1="6" x2="6" y2="18"></line>
                                <line x1="6" y1="6" x2="18" y2="18"></line>
                            </svg>
                        </button>
                    ` : ''}
                    ${task.status === 'failed' ? `
                        <button class="btn btn-icon btn-secondary" onclick="dismissTask(${task.id}, '${task.task_type}')"
                                title="从列表中移除这条失败记录" aria-label="移除失败任务行">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <polyline points="3 6 5 6 21 6"></polyline>
                                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                            </svg>
                        </button>
                    ` : ''}
                </div>
            </div>
            ${isFailed ? '' : `
            <div class="task-progress-line">
                <div class="task-progress">
                    <div class="progress-bar bg-${getStatusColor(task.status)}" role="progressbar"
                         style="width: ${progress}%"
                         aria-valuenow="${progress}"
                         aria-valuemin="0"
                         aria-valuemax="100"></div>
                </div>
                <span class="task-pct" aria-hidden="true">${progress}%</span>
                <span class="task-count progress-detail">${task.progress_verb || '已下载'}: ${task.downloaded_items} / ${task.total_items} ${task.items_label}${task.failed_items > 0 ? ` <span style="color: var(--color-danger);">| 失败: ${task.failed_items}</span>` : ''}</span>
            </div>`}
        </div>
    `;
}

// 失败任务的引文式错误行：紧跟主行之后的兄弟节点（不塞进主行，
// 错误原文长度不可控，独占一行才不挤压行1 的排版）。
// 文本由 applyTaskErrorText 用 textContent 补——error_message 是后端异常
// 的字符串化结果，绝不能进这里的 innerHTML 模板。
function createTaskErrorRow(task) {
    return `
        <div class="task-error-row" id="task-error-${task._key}">
            <div class="task-error" role="alert"></div>
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

            const row = document.getElementById(`task-${key}`);
            if (row) {
                if (statusChanged) {
                    row.outerHTML = createTaskRow(normalized);
                } else if (progressChanged) {
                    updateTaskProgressPartial(row, normalized);
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

            const row = document.getElementById(`task-${key}`);
            if (row) {
                if (statusChanged) {
                    row.outerHTML = createTaskRow(task);
                } else if (progressChanged) {
                    updateTaskProgressPartial(row, task);
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

            const row = document.getElementById(`task-${key}`);
            if (row) {
                if (statusChanged || phaseChanged) {
                    row.outerHTML = createTaskRow(task);
                } else if (progressChanged) {
                    updateTaskProgressPartial(row, task);
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

        const row = document.getElementById(`task-${key}`);
        if (row) {
            if (statusChanged) {
                row.outerHTML = createTaskRow(task);
            } else if (progressChanged) {
                updateTaskProgressPartial(row, task);
            }
        }
    } else {
        // New task - normalize and render
        activeTasks.set(key, normalizeTask(data, taskType));
        renderActiveTasks(Array.from(activeTasks.values()));
    }
}

function updateTaskProgressPartial(row, task) {
    const progress = task.total_items > 0
        ? Math.round((task.downloaded_items / task.total_items) * 100)
        : 0;

    // 更新进度条（行2 的 5px 发丝条，进度条/百分比/计数同一行）
    const progressBar = row.querySelector('.progress-bar');
    if (progressBar) {
        progressBar.style.width = `${progress}%`;
        progressBar.setAttribute('aria-valuenow', progress);
        progressBar.className = `progress-bar bg-${getStatusColor(task.status)}`;
    }

    // 百分比在条右边的 .task-pct，不在条里。这行原本是 progressBar.textContent = ...，
    // 是这条路径最容易漏改的一处：行初次渲染看不出问题，第一个
    // task_progress 事件一到就会在同一条进度条上多出第二个百分比。
    const progressLabel = row.querySelector('.task-pct');
    if (progressLabel) {
        progressLabel.textContent = `${progress}%`;
    }

    // 更新下载数量（数字与失败计数都是数值字段，没有注入面）
    const downloadDetail = row.querySelector('.task-count');
    if (downloadDetail) {
        const failedText = task.failed_items > 0
            ? ` <span style="color: var(--color-danger);">| 失败: ${task.failed_items}</span>`
            : '';

        downloadDetail.innerHTML =
            `${task.progress_verb || '已下载'}: ${task.downloaded_items} / ${task.total_items} ${task.items_label}${failedText}`;
    }
}

function handleTaskCompleted(taskId, taskType, warning) {
    // I18：部分 zoom 拼接失败时任务仍判 completed，但事件里带 warning
    // （同时写进了 tasks.error_message）。原实现直接删卡片、零提示——
    // 「任务成功但 GeoTIFF 缺层级」用户无从得知。toast 在删行之前弹，
    // 且任务不在 activeTasks（页面中途加载）时也照样提示。
    // showToast 内部走 textContent，warning 原文无需再转义。
    if (warning) {
        showToast('任务完成，但有警告：' + warning, 'warning');
    }
    const key = `${taskType}:${taskId}`;
    const task = activeTasks.get(key);
    if (task) {
        task.status = 'completed';
        activeTasks.delete(key);

        const row = document.getElementById(`task-${key}`);
        if (row) {
            row.remove();
        }

        renderActiveTasks(Array.from(activeTasks.values()));
    }

    // 完成的任务要立刻在历史区出现：实时行删掉了，如果历史表还停在旧数据，
    // 这个任务就在界面上「凭空消失」了。只在记录面板的历史已初始化过时才刷新
    // （historyViewer 存在，或历史表已有内容）——面板从没打开过的话，
    // 打开时 initHistory 本来就会拉最新数据，不必抢着刷。
    if (typeof loadHistory === 'function') {
        const historyBody = document.getElementById('historyTableBody');
        const historyReady = (typeof historyViewer !== 'undefined' && historyViewer)
            || (historyBody && historyBody.children.length > 0);
        if (historyReady) {
            loadHistory(1);
            loadStats();
        }
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
    task._key = key;   // applyTaskErrorText 靠它定位错误行；normalizeTask 之外的路径不一定设过

    // 既不 activeTasks.delete 也不删行：失败行必须留在页面上（转红 + 错误行）。
    // 原实现两件事一起做，于是用户盯着 63% 的进度条，卡片突然消失、零提示，
    // 分不清是失败、被别人取消、还是自己看花了眼。
    // 清理改由用户点行上的「移除」按钮触发（dismissTask）。
    activeTasks.set(key, task);

    // 整体重绘而不是就地改行：统一流式列表里失败任务属于「失败」分组
    // （分组头计数、折叠裁剪都跟着成员变），就地改行会让这条行滞留在
    // 「活动」分组里、分组头计数过期。renderActiveTasks 内部会对失败行
    // 调 createTaskRow（failed 变体：无进度条行2）+ createTaskErrorRow
    // 并用 applyTaskErrorText 回填错误文本，同一份真相只有一处。
    renderActiveTasks(Array.from(activeTasks.values()));

    console.error(`Task ${taskId} failed: ${task.error_message}`);
    // duration: 0 → ui.js 里 `if (duration > 0)` 不成立，不挂定时器，
    // toast 一直留到用户自己点 ×。默认的 3500ms 在这里没用：用户离座一趟
    // 回来照样什么都看不到。
    closeFailureToast(key);   // 同一任务只留最新的一条
    failureToasts.set(key, showToast(`任务失败：${task.error_message}`, 'danger', { duration: 0 }));
}

// 把错误文本填进错误行里那个**空的** .task-error 容器。
//
// 为什么不直接拼进 createTaskErrorRow 的模板：那个返回值最终进 innerHTML，
// 而 error_message 是后端异常的字符串化结果（URL、路径、第三方库报错原文
// 都可能在里面），拼进去等于把它当 HTML 解析。ui.js 的 toast 里是同一条规矩。
function applyTaskErrorText(task) {
    if (!task || task.status !== 'failed') return;
    const errRow = document.getElementById(`task-error-${task._key}`);
    if (!errRow) return;
    const box = errRow.querySelector('.task-error');
    if (!box) return;
    box.textContent = task.error_message || UNKNOWN_ERROR_TEXT;  // textContent 防 XSS
}

// 「移除」按钮：只把失败行从界面上拿走，不碰后端。
//
// 失败任务在后端已经是终态，再 POST /cancel 最好的情况也只是白跑一趟。
// 也刻意**没有**「重试」：三个 manager 的 start_task 都要求
// status in ('pending','paused')，对 failed 调用直接抛 ValueError，
// 重试得先改后端状态机。
function dismissTask(taskId, taskType = 'map') {
    const key = `${taskType}:${taskId}`;
    activeTasks.delete(key);
    closeFailureToast(key);   // 行都不要了，那条常驻 toast 也别留着占地方
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
    // 未知状态不把英文字面量原样渲染进中文界面（A7 修过的中英混杂问题）
    return texts[status] || '未知';
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

    // 使用后端计算的总运行时长。dem/contour/local_terrain 的 manager 不写
    // total_running_seconds（只有地图管线维护该列），字段缺失时回退按
    // started_at 的墙钟时长显示——否则这些任务恒显示"已运行: 0秒"。
    let elapsedSeconds;
    if (task.total_running_seconds != null) {
        elapsedSeconds = task.total_running_seconds;
        // 如果任务正在运行，加上当前这一段的时间
        if (task.status === 'running' && task.started_at) {
            const startTime = parseTaskDate(task.started_at);
            if (startTime) {
                const currentSegment = (Date.now() - startTime.getTime()) / 1000;
                elapsedSeconds += currentSegment;
            }
        }
    } else {
        const startTime = parseTaskDate(task.started_at);
        if (!startTime) {
            result.show = false;
            return result;
        }
        elapsedSeconds = (Date.now() - startTime.getTime()) / 1000;
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
        // 只更新运行中的任务时间（行1 右侧的 已运行/预计剩余）
        if (task.status !== 'running') return;
        const row = document.getElementById(`task-${taskId}`);
        if (!row) return;
        const timeCell = row.querySelector('.task-time');
        if (!timeCell) return;
        const timeInfo = calculateTimeInfo(task);
        // 时间文本由 formatDuration 的数字组成，无注入面；整格重写 textContent
        timeCell.textContent = timeInfo.show
            ? [timeInfo.elapsed ? `已运行: ${timeInfo.elapsed}` : '',
               timeInfo.estimated ? `预计剩余: ${timeInfo.estimated}` : '']
                .filter(Boolean).join(' · ')
            : '—';
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
            // 与 cancelTask 同口径：透出服务端给的具体原因，不只报"失败"
            const result = await response.json().catch(() => ({}));
            throw new Error(result.error || ('HTTP ' + response.status));
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
            const result = await response.json().catch(() => ({}));
            throw new Error(result.error || ('HTTP ' + response.status));
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
            const result = await response.json().catch(() => ({}));
            throw new Error(result.error || ('HTTP ' + response.status));
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
            const row = document.getElementById(`task-${key}`);
            if (row) {
                row.remove();
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
