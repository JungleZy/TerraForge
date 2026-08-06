let socket;
let activeTasks = new Map();
let timeUpdateInterval = null;
// 标记是否已经完成首次 socket 连接。initTasks 末尾会直接调一次
// loadActiveTasks 负责首屏，connect 回调只在**断线重连**时补拉——
// 否则首次连接会重复拉一遍 4 个列表接口。
let hasConnectedOnce = false;
// 首屏 loadActiveTasks 的 Promise + 该次拿到的 contour 全量列表（未过滤，
// 含 completed）。map.js 的等高线预览面板首屏要从中筛 completed 任务——
// contour 路因此刻意不带 ?status=active（见 loadActiveTasks 的注释）。
let firstActiveTasksLoad = null;
let latestContourTasks = [];
// 终态事件（task_completed/task_failed）统计卡刷新的去抖定时器：
// 批量收官时每个任务各发一次，300ms 内合并成一次 loadStats()。
let _loadStatsDebounceTimer = null;

function initTasks() {
    socket = window.TerraSocket.get();
    // 必须在 get() 之后的**同一个同步块**里就把监听挂上（见 socket.js 的说明）：
    // socket.io 不重放错过的事件，中间一让出事件循环就可能漏掉 connect。
    // 这一行兜的是「连接不是本页建的」那种情况：若已有别的消费者先 get() 过、
    // 此刻连接已经建好，本页的 connect 回调就永远不会触发，hasConnectedOnce 会
    // 卡在 false —— 于是断线重连后不补拉数据（那正是它存在的唯一理由）。
    if (socket.connected) hasConnectedOnce = true;

    socket.on('connect', function() {
        console.log('Connected to server');
        if (hasConnectedOnce) {
            loadActiveTasks();
            // 断线窗口内的终态变化不会补发 socket 事件：只补拉活动列表的话，
            // 时间流里的行（历史流）和统计卡会永久停在断线前的状态——一并刷新。
            // loadHistory/loadStats/currentPage 是 history.js 的全局
            // （首页两个文件都加载，typeof 守卫兜底）。
            if (typeof loadHistory === 'function') {
                loadHistory(typeof currentPage !== 'undefined' ? currentPage : 1);
            }
            if (typeof loadStats === 'function') {
                loadStats();
            }
        }
        hasConnectedOnce = true;
    });

    socket.on('disconnect', function() {
        console.log('Disconnected from server');
    });

    socket.on('task_progress', function(data) {
        // 高频事件，不 console.log 整个 data（DevTools 打开时是主线程开销）
        updateTaskProgress(data);
        updateStatusTasks();
    });

    socket.on('task_completed', function(data) {
        handleTaskCompleted(data.task_id, data.task_type || 'map', data.warning);
        pushStatusEvent(t('js.tasks.event.completed', {id: data.task_id}));
        updateStatusTasks();
    });

    socket.on('task_failed', function(data) {
        handleTaskFailed(data.task_id, data.task_type || 'map', data.error_message);
        pushStatusEvent(t('js.tasks.event.failed', {id: data.task_id}));
        updateStatusTasks();
    });

    socket.on('task_stitch_progress', function(data) {
        pushStatusEvent(t('js.tasks.event.stitching', {id: data.task_id}));
        updateTaskStageText(data.task_id,
                            t('js.tasks.stage.stitching', {zoom: data.zoom_level}), 'map');
    });

    // 地形切片作业的进度。后端一直在广播它（dem_task_manager /
    // local_terrain_task_manager），但在此之前**没有任何前端监听者** —— 切片
    // 进度只能靠详情弹窗手动点刷新才看得到。
    socket.on('terrain_job_progress', function(data) {
        updateTerrainJobProgress(data);
    });

    // 某个缩放级别拼接失败。任务可能仍在跑(其余级别继续),所以这里只报,不动行。
    // 最终判定在后端:全失败 → task_failed;部分失败 → task_completed 带 warning,
    // 同时写进 tasks.error_message。
    socket.on('task_stitch_failed', function(data) {
        console.error(`Task ${data.task_id} zoom ${data.zoom_level} 拼接失败:`, data.error_message);
    });

    // 复制瓦片阶段的心跳。下载进度条此时已经 100%,没有这个事件界面会静止若干分钟。
    socket.on('task_copy_progress', function(data) {
        pushStatusEvent(t('js.tasks.event.copying', {id: data.task_id}));
        updateTaskStageText(
            data.task_id,
            t('js.tasks.stage.copying', {done: data.processed_tiles, total: data.total_tiles}),
            'map'
        );
    });

    // 首屏这次拉取的 Promise 挂到模块级：map.js 的 initContourPreview 等它
    // resolve 后共享 contour 数据，首屏 /api/contour/tasks 只拉一遍。
    firstActiveTasksLoad = loadActiveTasks();
    updateStatusTasks();

    // 每秒更新一次时长显示
    if (timeUpdateInterval) {
        clearInterval(timeUpdateInterval);
    }
    timeUpdateInterval = setInterval(updateTimeDisplay, 1000);
}

async function loadActiveTasks() {
    try {
        // 三路带 ?status=active：服务端只回活动三态（不传行为不变——后端
        // 未上线该参数时返回全量，下面白名单照样滤），completed/cancelled
        // 不再随每次补拉往返。contour 路刻意不带：这份响应同时是地图预览
        // 面板的数据源（initContourPreview 要从里面筛 completed 任务，
        // 见 map.js），带上的话首屏还得再拉一遍全量。
        const [mapResp, demResp, localResp, contourResp] = await Promise.all([
            fetch('/api/tasks?status=active'),
            fetch('/api/dem/tasks?status=active'),
            fetch('/api/terrain/local/tasks?status=active'),
            fetch('/api/contour/tasks')
        ]);
        // 四路任何一路非 2xx 都不能接着解析渲染——失败响应的 body 不是任务
        // 列表，会被当成「没有活动任务」把整页卡片清空，看起来就像任务全没了。
        const badResp = [mapResp, demResp, localResp, contourResp].find(r => !r.ok);
        if (badResp) {
            throw new Error(t('js.tasks.load.http_error', {status: badResp.status}));
        }
        const mapData = await mapResp.json();
        const demData = await demResp.json();
        const localData = await localResp.json();
        const contourData = await contourResp.json();
        // 全量（含 completed）共享给 map.js 的等高线预览面板，首屏只拉这一遍
        latestContourTasks = contourData.tasks || [];

        const mapTasks = (mapData.tasks || []).map(t => normalizeTask(t, 'map'));
        const demTasks = (demData.tasks || []).map(t => normalizeTask(t, 'dem'));
        const localTasks = (localData.tasks || []).map(t => normalizeTask(t, 'local_terrain'));
        const contourTasks = (contourData.tasks || []).map(t => normalizeTask(t, 'contour'));
        const all = [...mapTasks, ...demTasks, ...localTasks, ...contourTasks].filter(t =>
            // completed/cancelled 由服务端 ?status=active 挡掉（contour 路拉的是
            // 全量，终态在这里被白名单丢弃——它只需要活动态进这个 Map）。
            // failed 仍保留：失败行的「移除」（dismissTask）与 socket 失败事件
            // 都按 key 在这个 Map 里找任务；状态栏聚合自己会再滤掉非活动态。
            ['pending', 'running', 'paused', 'failed'].includes(t.status)
        );

        activeTasks.clear();
        all.forEach(task => {
            activeTasks.set(task._key, task);
        });

        // 2026-08 单一时间流定稿：activeTasks 不再驱动列表渲染——时间流由
        // history.js 从 /api/history_all 一次拉全（活动任务也在流里，
        // 没有独立分区，也没有去重）。这个 Map 的消费者只剩三个：
        // 状态栏聚合（updateStatusTasks）、行1 耗时每秒刷新（updateTimeDisplay）、
        // socket 事件按 `类型:id` 找任务（updateTaskProgress 等）。
        updateStatusTasks();
    } catch (error) {
        console.error('Failed to load tasks:', error);
        showToast(t('js.tasks.load.failed', {error: error.message}), 'danger');
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
            label: t('js.tasks.unit.tile'),
            verb: t('js.tasks.verb.render_contour_tiles')
        };
    }
    return {
        total: task.total_files || 0,
        done: task.downloaded_files || 0,
        failed: task.failed_files || 0,
        label: 'DEM',
        verb: t('js.tasks.verb.download_dem')
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
            items_label: t('js.tasks.unit.file')
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
            items_label: t('js.tasks.unit.file')
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
        items_label: t('js.tasks.unit.tile')
    };
}

// --- 底部状态栏：活动任务聚合 + 最近事件 ----------------------------------------
// 状态栏元素只在首页存在（#statusTasksText 等），独立页不加载 tasks.js，
// 这里仍全部做 null 守卫，避免未来被其它页引入时报错。

// 活动任务读数：进行中（pending/running/paused）任务数 + 汇总进度条。
// failed 行留在 activeTasks 里等用户移除，但不算「活动」。
// task_progress 每个事件都调这里：文本/宽度算出来后先比对再写——无条件
// 写 textContent/style.width 每次都会触发 DOM 变更（高频事件下白付 layout）。
function updateStatusTasks() {
    const textEl = document.getElementById('statusTasksText');
    if (!textEl) return;
    const barWrap = document.getElementById('statusTasksProgress');
    const barFill = document.getElementById('statusTasksBar');
    const live = Array.from(activeTasks.values())
        .filter(t => ['pending', 'running', 'paused'].includes(t.status));
    if (live.length === 0) {
        const idle = t('js.tasks.status_bar.idle');
        if (textEl.textContent !== idle) {
            textEl.textContent = idle;
        }
        if (barWrap && !barWrap.hidden) {
            barWrap.hidden = true;
        }
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
    const text = t('js.tasks.status_bar.active', {n: live.length, running: running, pct: pct});
    if (textEl.textContent !== text) {
        textEl.textContent = text;
    }
    if (barWrap && barFill) {
        if (barWrap.hidden) {
            barWrap.hidden = false;
        }
        const width = pct + '%';
        if (barFill.style.width !== width) {
            barFill.style.width = width;
        }
    }
}

// 最近事件单行读数：任务完成/失败/拼接/复制阶段的文字心跳。
// 这些 socket 事件原本只 console.log，状态栏是唯一让消费者。
function pushStatusEvent(msg) {
    const el = document.getElementById('statusEvent');
    if (!el) return;
    el.textContent = msg;
}

// 2026-08 单一时间流定稿：行渲染整体收口到 history.js 的 createTaskRow
// （全站唯一行实现，活动/失败/历史任务共用）。本文件原先那套
// taskMetaText / createTaskRow / createTaskErrorRow 随之删除——同一套行结构
// 两份实现必然漂移，「两种行语言」正是当初「太乱」的根因之一。
// tasks.js 只剩实时更新职责：socket 事件 → 找到时间流里的行 →
// 原地重建（调 history.js 的 createTaskRow）或增量更新。

// 原地重建时间流里的一行。失败行的 .task-error 容器是空的
// （错误原文不能进 innerHTML），重建后顺手用 textContent 回填。
function rebuildStreamRow(row, task) {
    row.outerHTML = createTaskRow(task);
    applyTaskErrorText(task);
}

// 拼接/复制阶段的行内阶段提示：下载 100% 后这两个阶段还要跑几分钟到几小时，
// 旧界面行上只有「已下载 N/N」，看起来就是卡死（「卡 100%」）。stage_text
// 写进行模型（history.js 的 line2 渲染）并就地重建该行。事件频率低——
// 拼接按 zoom 一次、复制每 COPY_PROGRESS_INTERVAL 块一次，重建一行开销可忽略。
// taskType 参数化：原先写死 `map:${taskId}`，地形/等高线管线复用不了。
// stageText 传 null/'' 表示**清除**——这条清除路径不是可选的：
// updateTaskProgressPartial 只覆盖 DOM 里的 .task-count，不动行模型上的
// stage_text，于是任何后续 rebuildStreamRow（状态变化、等高线的 phaseChanged）
// 都会让过期的阶段文字复活并永久顶掉计数。地图管线侥幸无事，只是因为它的
// 拼接/复制发生在下载彻底结束之后，两类事件永不交错；新阶段夹在两个会发
// task_progress 的阶段中间（地形：物化 → 逐瓦片；等高线：warp → 渲染），
// 不清就必然显形。
function updateTaskStageText(taskId, stageText, taskType) {
    const key = `${taskType || 'map'}:${taskId}`;
    const task = activeTasks.get(key);
    if (!task) return;   // 任务不在活动窗口里时，状态栏事件仍然可见
    if (stageText) {
        task.stage_text = stageText;
    } else {
        delete task.stage_text;
    }
    const row = document.getElementById(`task-${key}`);
    if (row) rebuildStreamRow(row, task);
}

// 地形切片（DEM / 本地地形）的行内进度。
//
// 与 stage_text 分开的原因：DEM 的切片作业跑在**下载任务已经 completed 之后**，
// 那时行落在 history.js 的纯文本分支，既没有进度条也没有 stage 位置。所以另开
// 一个字段，由 history.js 在两个分支都渲染。
function updateTerrainJobProgress(data) {
    const taskType = data.task_type === 'local_terrain' ? 'local_terrain' : 'dem';
    const key = `${taskType}:${data.task_id}`;
    const task = activeTasks.get(key);
    if (!task) return;

    if (data.status && data.status !== 'running') {
        delete task.tiling_text;
    } else if (data.stage_label) {
        // 物化 / 建金字塔：这一段跑在 total 算出来之前，没有分母，只能报比例
        const pct = Math.round((Number(data.stage_fraction) || 0) * 100);
        task.tiling_text = t('js.tasks.terrain_stage', {
            stage: data.stage_label, pct: pct});
    } else if (Number(data.total_tiles) > 0) {
        task.tiling_text = t('js.tasks.terrain_tiling', {
            done: Number(data.rendered_tiles) || 0,
            total: Number(data.total_tiles)});
    } else {
        return;
    }
    const row = document.getElementById(`task-${key}`);
    if (row) rebuildStreamRow(row, task);
}

// 新任务到达（updateTaskProgress 的未知 key 分支）时插到时间流顶部。
// 条件：当前在第 1 页且筛选 chip 是 全部/进行中——其它页码/其它筛选下
// 硬插会破坏「按创建时间倒序 + 状态筛选」的语义（任务会出现在它不该
// 出现的页里）。不满足时不插：翻页/切 chip 会从 /api/history_all 重拉，
// 任务自然出现。currentPage / currentStatusFilter / createTaskRow 都是
// history.js 的全局（首页两个文件都加载，typeof 守卫兜底）。
function prependStreamRow(task) {
    // 查重兜底：行已在流里就原地重建，绝不插第二行。两条重复路径：
    //   1. 活动任务被列表接口的 100 条窗口挤掉后，task_progress 到达时
    //      activeTasks 里查不到，被当成「新任务」；
    //   2. 启动竞态：loadHistory 已经渲染了该行，首个 task_progress 又到达。
    if (task._key) {
        const existing = document.getElementById(`task-${task._key}`);
        if (existing) {
            rebuildStreamRow(existing, task);
            return;
        }
    }
    if (typeof currentPage === 'undefined' || currentPage !== 1) return;
    if (typeof currentStatusFilter !== 'undefined'
        && currentStatusFilter !== '' && currentStatusFilter !== 'active') return;
    const container = document.getElementById('historyTableBody');
    if (!container) return;
    // 空态/加载中占位与行不能共存，先清掉再插
    if (container.querySelector('.task-empty') || container.querySelector('.spinner-container')) {
        container.innerHTML = '';
    }
    container.insertAdjacentHTML('afterbegin', createTaskRow(task));
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
                    rebuildStreamRow(row, normalized);
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
            task.items_label = t('js.tasks.unit.file');
            task.output_path = data.output_path || task.output_path;
            task.started_at = data.started_at || task.started_at;
            task.created_at = data.created_at || task.created_at;

            activeTasks.set(key, task);

            const row = document.getElementById(`task-${key}`);
            if (row) {
                if (statusChanged) {
                    rebuildStreamRow(row, task);
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
                    rebuildStreamRow(row, task);
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
        task.items_label = t('js.tasks.unit.tile');
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
                rebuildStreamRow(row, task);
            } else if (progressChanged) {
                updateTaskProgressPartial(row, task);
            }
        }
    } else {
        // 新任务：进 activeTasks（状态栏/耗时刷新/socket 查找用），
        // 并按条件 prepend 到时间流顶部（见 prependStreamRow 的条件注释）。
        const normalized = normalizeTask(data, taskType);
        activeTasks.set(key, normalized);
        prependStreamRow(normalized);
    }
}

function updateTaskProgressPartial(row, task) {
    const progress = task.total_items > 0
        ? Math.round((task.downloaded_items / task.total_items) * 100)
        : 0;

    // 更新进度条（行2 的 5px 发丝条，进度条/百分比/计数同一行）
    const progressBar = row.querySelector('.progress-bar');
    if (progressBar) {
        // 数值没变就不重写 width/aria：写 style.width 必然触发 layout，
        // task_progress 高频事件下值得挡掉（aria-valuenow 即 DOM 里的现值）。
        if (progressBar.getAttribute('aria-valuenow') !== String(progress)) {
            progressBar.style.width = `${progress}%`;
            progressBar.setAttribute('aria-valuenow', progress);
        }
        // 状态色同理：className 不变时重写也会触发 restyle，先比再写。
        // 赋值保持这行字面量——契约测试用正则点名钉住它（Socket.IO 增量主路径）。
        if (progressBar.className !== `progress-bar bg-${getStatusColor(task.status)}`) {
            progressBar.className = `progress-bar bg-${getStatusColor(task.status)}`;
        }
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
            ? ` <span style="color: var(--color-danger);">${t('js.tasks.failed_count', {n: task.failed_items})}</span>`
            : '';

        const detail = t('js.tasks.progress_detail', {
            verb: task.progress_verb || t('js.tasks.verb.downloaded'),
            done: task.downloaded_items,
            total: task.total_items,
            unit: task.items_label
        });
        downloadDetail.innerHTML = `${detail}${failedText}`;
    }
}

function handleTaskCompleted(taskId, taskType, warning) {
    // I18：部分 zoom 拼接失败时任务仍判 completed，但事件里带 warning
    // （同时写进了 tasks.error_message）。原实现直接删卡片、零提示——
    // 「任务成功但 GeoTIFF 缺层级」用户无从得知。toast 在删行之前弹，
    // 且任务不在 activeTasks（页面中途加载）时也照样提示。
    // showToast 内部走 textContent，warning 原文无需再转义。
    if (warning) {
        showToast(t('js.tasks.toast.completed_with_warning', {warning: warning}), 'warning');
    }
    const key = `${taskType}:${taskId}`;
    const task = activeTasks.get(key);
    if (task) {
        task.status = 'completed';
        activeTasks.delete(key);   // 终态出 Map：状态栏/耗时刷新只看活动任务

        // 单一时间流（2026-08 定稿）：原地重建为 completed 态——任务就留在
        // 时间流里，**不是删除**。上一版「删实时行 + loadHistory(1) 重拉」
        // 是活动/历史分区时代的做法（完成的任务要从活动区搬进历史区）；
        // 流里没有分区，换状态只是换一行的形态。
        const row = document.getElementById(`task-${key}`);
        if (row) {
            rebuildStreamRow(row, task);
        }
    }

    // 统计卡（总任务/已完成/失败/累计下载量）跟着终态走。loadStats 是
    // history.js 的全局（首页两个文件都加载，typeof 守卫兜底）。
    // 批量收官时每个任务各发一次终态事件，立即刷新会 N 连发——300ms 去抖
    // 合并成一次（字面量 loadStats() 保留在调用处：契约测试按正则点名它）。
    if (typeof loadStats === 'function') {
        if (_loadStatsDebounceTimer) clearTimeout(_loadStatsDebounceTimer);
        _loadStatsDebounceTimer = setTimeout(() => {
            _loadStatsDebounceTimer = null;
            loadStats();
        }, 300);
    }
}

// 后端没给原因时的兜底文案。空字符串会渲染成一个空红框，比没有红框更让人困惑。
const UNKNOWN_ERROR_TEXT = t('js.tasks.unknown_error');

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

    // 单一时间流（2026-08 定稿）：原地重建为 failed 态（含引文式错误行），
    // 不再整体重绘——流里没有「失败」分组，分组头计数/折叠裁剪都不存在了，
    // 换状态只是换这一行的形态。rebuildStreamRow 内部会用
    // applyTaskErrorText 以 textContent 回填错误原文。
    const row = document.getElementById(`task-${key}`);
    if (row) {
        rebuildStreamRow(row, task);
    }

    console.error(`Task ${taskId} failed: ${task.error_message}`);
    // duration: 0 → ui.js 里 `if (duration > 0)` 不成立，不挂定时器，
    // toast 一直留到用户自己点 ×。默认的 3500ms 在这里没用：用户离座一趟
    // 回来照样什么都看不到。
    closeFailureToast(key);   // 同一任务只留最新的一条
    failureToasts.set(key, showToast(t('js.tasks.toast.failed', {message: task.error_message}), 'danger', { duration: 0 }));

    // 统计卡的「失败」计数跟着走（与 handleTaskCompleted 同一去抖：
    // 批量失败时 N 个事件合并成一次刷新，字面量 loadStats() 同样被契约测试点名）
    if (typeof loadStats === 'function') {
        if (_loadStatsDebounceTimer) clearTimeout(_loadStatsDebounceTimer);
        _loadStatsDebounceTimer = setTimeout(() => {
            _loadStatsDebounceTimer = null;
            loadStats();
        }, 300);
    }
}

// 把错误文本填进失败行里那个**空的** .task-error 容器。
//
// 为什么不直接拼进行模板：createTaskRow 的返回值最终进 innerHTML，
// 而 error_message 是后端异常的字符串化结果（URL、路径、第三方库报错原文
// 都可能在里面），拼进去等于把它当 HTML 解析。ui.js 的 toast 里是同一条规矩。
function applyTaskErrorText(task) {
    if (!task || task.status !== 'failed') return;
    const row = document.getElementById(`task-${task._key}`);
    if (!row) return;
    const box = row.querySelector('.task-error');
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
    // 纯前端删行：任务仍在后端（要彻底删除用行上的 🗑），下次翻页/刷新
    // 从 /api/history_all 重拉时它会回来——这正是「移除」与「删除」的区别。
    const row = document.getElementById(`task-${key}`);
    if (row) {
        row.remove();
    }
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
        'pending': t('js.tasks.status.pending'),
        'running': t('js.tasks.status.running'),
        'paused': t('js.tasks.status.paused'),
        'completed': t('js.tasks.status.completed'),
        'failed': t('js.tasks.status.failed'),
        'cancelled': t('js.tasks.status.cancelled')
    };
    // 未知状态不把英文字面量原样渲染进中文界面（A7 修过的中英混杂问题）
    return texts[status] || t('js.tasks.status.unknown');
}

function formatDuration(seconds) {
    if (seconds < 60) {
        return t('js.tasks.duration.seconds', {s: Math.round(seconds)});
    } else if (seconds < 3600) {
        const minutes = Math.floor(seconds / 60);
        const secs = Math.round(seconds % 60);
        return secs > 0
            ? t('js.tasks.duration.min_sec', {m: minutes, s: secs})
            : t('js.tasks.duration.minutes', {m: minutes});
    } else {
        const hours = Math.floor(seconds / 3600);
        const minutes = Math.floor((seconds % 3600) / 60);
        return minutes > 0
            ? t('js.tasks.duration.hour_min', {h: hours, m: minutes})
            : t('js.tasks.duration.hours', {h: hours});
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

    // 使用后端列累计的总运行时长（**不含当前段**）。三个来源同一口径：
    // /api/tasks 与 /api/history_all 本来就是 tasks 列的累计值；socket 载荷
    // 曾经发「列累计 + 当前段」的瞬时值，前端又叠加一次当前段导致双计——
    // 后端改发列值后，当前段统一由前端按 started_at 在下方叠加（仅 running），
    // 全前端只剩这一处计算。dem/contour/local_terrain 的 manager 不写该列，
    // 字段缺失时回退按 started_at 的墙钟时长显示——否则恒显示"已运行: 0秒"。
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
            ? [timeInfo.elapsed ? t('js.tasks.time.elapsed', {value: timeInfo.elapsed}) : '',
               timeInfo.estimated ? t('js.tasks.time.remaining', {value: timeInfo.estimated}) : '']
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
        showToast(t('js.tasks.toast.start_failed', {error: error.message}), 'danger');
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
        showToast(t('js.tasks.toast.pause_failed', {error: error.message}), 'danger');
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
        showToast(t('js.tasks.toast.resume_failed', {error: error.message}), 'danger');
    }
}

async function cancelTask(taskId, taskType = 'map') {
    if (!await showConfirm(t('js.tasks.confirm.cancel_message'),
                           { title: t('js.tasks.confirm.cancel_title'), danger: true })) {
        return;
    }

    try {
        const response = await fetch(`${apiPrefixForType(taskType)}/${taskId}/cancel`, {
            method: 'POST'
        });
        if (response.ok) {
            const key = `${taskType}:${taskId}`;
            const task = activeTasks.get(key);
            activeTasks.delete(key);
            // 单一时间流：取消的任务留在流里，原地重建为 cancelled 态
            // （与 task_completed/task_failed 的原地重建同一形态），不删行。
            if (task) {
                task.status = 'cancelled';
                const row = document.getElementById(`task-${key}`);
                if (row) {
                    rebuildStreamRow(row, task);
                }
            }
        } else {
            const result = await response.json().catch(() => ({}));
            showToast(t('js.tasks.toast.cancel_failed', {error: result.error || response.status}), 'danger');
        }
    } catch (error) {
        showToast(t('js.tasks.toast.cancel_failed', {error: error.message}), 'danger');
    }
}
