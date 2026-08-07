let socket;
// 任务状态与时间流都在 window.TaskStore（static/js/task_store.js）。
// 这里曾有一个裸 `activeTasks = new Map()`：它只是缓存，不驱动渲染，
// 每次写它都得再手动 getElementById 改一次 DOM —— 11 处这样的配对。
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
    // 时间流的渲染层（Vue）挂到 #historyTableBody。幂等——独立页由
    // initHistory 调，首页两个入口都会走到，谁先谁后都行。
    if (window.TaskList) window.TaskList.mount();
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

        // 活动任务集进 store：状态栏聚合（updateStatusTasks）与行1 耗时
        // 每秒刷新都读它。时间流（渲染源）是另一个集合，由 history.js 的
        // loadHistory 从 /api/history_all 分页拉——两者的区别见 task_store.js
        // 里 state.active 的注释。
        if (window.TaskStore) window.TaskStore.setActive(all);
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
// 没有下载阶段的来源（dataset='upload' 上传、'dem_task' 复用已下载的 DEM 任务，
// 与后端 is_upload / 目录直取同一判定）：文件计数在创建时就已记满
// （downloaded_files == total_files），拿它当进度会让 pending 任务一出现就显示
// 100%「下载 DEM」——直接按渲染阶段显示。
function contourPhaseCounts(task) {
    const totalTiles = task.total_tiles || 0;
    const renderStarted = task.dataset === 'upload' || task.dataset === 'dem_task'
        || (task.phase === 'render') || totalTiles > 0;
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
    const live = window.TaskStore ? window.TaskStore.liveTasks() : [];
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

// 2026-08 单一时间流定稿：行渲染整体收口到 history.js 的 createTaskRow。
// 2026-08 Vue 化：渲染再次搬家，这次搬进 task_list.js 的 TaskRow 组件，
// 本文件**一行 DOM 都不再写**。
//
// 改造前这里有四条几乎一样的分支（map / dem / contour / local_terrain），
// 每条都干同样五件事：
//   1. 手写脏检查算 statusChanged / progressChanged
//   2. 把 data 的字段一个个抄进 task 对象
//   3. activeTasks.set(key, task)
//   4. document.getElementById(`task-${key}`)
//   5. 按脏检查结果分派「整行 outerHTML 重建」还是「querySelector 逐节点写」
// 第 3 步和第 4 步的配对在本文件里出现过 11 次 —— 漏掉后半截就是「数据变了
// 界面不动」，没有任何机制会报错。第 1 步和第 5 步是人肉实现的 diff。
//
// 现在只剩第 2 步（字段归一，normalizeTask 的份内事）和一次 store 写入，
// 其余四步全部由 Vue 的响应式 + keyed diff 负责。

/** 把一条 socket 推送合并进时间流。不存在的任务按 prepend 策略插入。 */
function commitTaskUpdate(key, patch) {
    if (!window.TaskStore) return null;
    return window.TaskStore.patch(key, patch);
}

// 拼接/复制阶段的行内阶段提示：下载 100% 后这两个阶段还要跑几分钟到几小时，
// 旧界面行上只有「已下载 N/N」，看起来就是卡死（「卡 100%」）。
// taskType 参数化：原先写死 `map:${taskId}`，地形/等高线管线复用不了。
// stageText 传 null/'' 表示**清除**——这条清除路径不是可选的：进度更新只
// 覆盖计数、不动 stage_text，过期的阶段文字会永久顶掉计数。地图管线侥幸
// 无事，只因它的拼接/复制发生在下载彻底结束之后，两类事件永不交错；新阶段
// 夹在两个会发 task_progress 的阶段中间（地形：物化 → 逐瓦片；等高线：
// warp → 渲染），不清就必然显形。
function updateTaskStageText(taskId, stageText, taskType) {
    const key = `${taskType || 'map'}:${taskId}`;
    // 传 '' 而不是 delete：组件里 stage_text 参与 `||` 取值，空串即视为无。
    commitTaskUpdate(key, { stage_text: stageText || '' });
}

// 地形切片（DEM / 本地地形）的行内进度。
//
// 与 stage_text 分开的原因：DEM 的切片作业跑在**下载任务已经 completed 之后**，
// 那时行落在组件的终态分支，既没有进度条也没有 stage 位置。所以另开一个
// 字段，组件在活动态与终态两个分支都渲染它。
function updateTerrainJobProgress(data) {
    const taskType = data.task_type === 'local_terrain' ? 'local_terrain' : 'dem';
    const key = `${taskType}:${data.task_id}`;
    if (!window.TaskStore || !window.TaskStore.has(key)) return;

    let tilingText;
    if (data.status && data.status !== 'running') {
        tilingText = '';
    } else if (data.stage_label) {
        // 物化 / 建金字塔：这一段跑在 total 算出来之前，没有分母，只能报比例
        const pct = Math.round((Number(data.stage_fraction) || 0) * 100);
        tilingText = t('js.tasks.terrain_stage', { stage: data.stage_label, pct: pct });
    } else if (Number(data.total_tiles) > 0) {
        tilingText = t('js.tasks.terrain_tiling', {
            done: Number(data.rendered_tiles) || 0,
            total: Number(data.total_tiles)});
    } else {
        return;
    }
    commitTaskUpdate(key, { tiling_text: tilingText });
}

// 新任务到达时插到时间流顶部。
// 条件：当前在第 1 页且筛选 chip 是 全部/进行中——其它页码/其它筛选下硬插
// 会破坏「按创建时间倒序 + 状态筛选」的语义（任务会出现在它不该出现的页里）。
// 不满足时不插：翻页/切 chip 会从 /api/history_all 重拉，任务自然出现。
// currentPage / currentStatusFilter 是 history.js 的全局（首页两个文件都
// 加载，typeof 守卫兜底）。
//
// 改造前这里还要自己防重复插行：先 getElementById 查重、命中就改走整行
// 重建，再手动清掉空态/spinner 占位，最后 insertAdjacentHTML。现在
// store.upsert 按 key 合并、组件是 keyed v-for，同一个 key 在结构上不可能
// 渲染出两行；空态也由组件自己按数组长度决定。
function prependStreamRow(task) {
    if (!window.TaskStore) return;
    const key = task._key || window.TaskStore.keyOf(task);
    // 已在流里（被分页窗口挤掉后又收到推送、或与首屏 loadHistory 竞态）：
    // 合并进去，不新增。
    if (window.TaskStore.has(key)) {
        window.TaskStore.patch(key, task);
        return;
    }
    if (typeof currentPage === 'undefined' || currentPage !== 1) return;
    if (typeof currentStatusFilter !== 'undefined'
        && currentStatusFilter !== '' && currentStatusFilter !== 'active') return;
    window.TaskStore.upsert(key, task);
}

// 一条 socket 推送 → 一次 store 写入。
//
// 改造前这里是 154 行、四条管线（map/dem/contour/local_terrain）各自把
// data 的字段一个个抄进 task 对象，再各自算脏检查、各自 getElementById、
// 各自分派整行重建还是逐节点增量。四份几乎一样的代码。
//
// 现在归一交给 normalizeTask（它本来就是干这个的），合并与渲染交给 store
// 和 Vue。字段兜底也不用手写了：normalizeTask 用 `...task` 展开，推送里
// 没有的元信息字段（name / style / output_path / zoom_* 等）根本不会出现在
// 结果里，Object.assign 自然不会把它们覆盖成 undefined —— 这正是改造前那
// 一长串 `task.x = data.x || task.x` 的语义。
function updateTaskProgress(data) {
    if (!window.TaskStore) return;
    const taskType = data.task_type || 'map';
    const taskId = data.task_id || data.id;
    const key = `${taskType}:${taskId}`;
    // 推送里任务主键叫 task_id，normalizeTask 读的是 id
    const normalized = normalizeTask(Object.assign({}, data, { id: taskId }), taskType);

    // 速度是**瞬时量**，不能像计数那样在行上长期挂着：
    //   - 带 download_speed_bps 的推送 = 这一发来自下载阶段。记下到达时刻，
    //     task_list.js 靠它判断「这个数还新鲜吗」（网断了但任务没判失败时，
    //     推送会停，行上的速度必须归零而不是冻在最后那个高值）。
    //   - 不带的推送 = 后端明说这一发不是下载阶段（等高线渲染、地形切片）。
    //     必须**显式清掉**：store 是 Object.assign 合并，不清的话旧速度会
    //     一直留在任务对象上。
    const hasSpeed = typeof data.download_speed_bps === 'number';
    normalized.download_speed_bps = hasSpeed ? data.download_speed_bps : null;
    normalized.speed_at = hasSpeed ? Date.now() : null;

    // 首次见到这个任务:试着插进时间流(只有在第 1 页、且状态筛选允许时
    // prependStreamRow 才会真插 —— 见它自己的守卫 :385-387)。**插没插都
    // 要继续 commit** —— 活动集必须写:
    //   1. 不写状态栏就漏算这个任务(进行中数量、汇总进度全少一份);
    //   2. 更糟的是 handleTaskFailed 的 known 守卫(:486)会为 false,
    //      直接 return —— 没 toast、没红行,任务失败得完全静默。
    // 时间流里没这一行时 commit 只写活动集,这正是它的既定语义。
    if (!window.TaskStore.has(key) && !window.TaskStore.getActive(key)) {
        prependStreamRow(normalized);
    }

    window.TaskStore.commit(key, normalized);

    // 等高线两阶段计数必须基于**合并后**的对象重算：renderStarted 要看
    // dataset === 'upload'，而 dataset 不在 task_progress 的推送里，
    // 只在合并后的任务对象上才有。
    if (taskType === 'contour') {
        const merged = window.TaskStore.get(key) || window.TaskStore.getActive(key);
        if (merged) {
            const counts = contourPhaseCounts(merged);
            window.TaskStore.commit(key, {
                total_items: counts.total,
                downloaded_items: counts.done,
                failed_items: counts.failed,
                items_label: counts.label,
                progress_verb: counts.verb,
            });
        }
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
    if (window.TaskStore) {
        // 单一时间流：任务留在流里，只是换一行的形态（**不是删除**）。
        // 上一版「删实时行 + loadHistory(1) 重拉」是活动/历史分区时代的做法。
        window.TaskStore.commit(key, { status: 'completed' });
        // 终态出活动集：状态栏聚合与耗时刷新只看活动任务
        window.TaskStore.dropActive(key);
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
    if (!window.TaskStore) return;
    const known = window.TaskStore.has(key) || window.TaskStore.getActive(key);
    if (!known) return;

    const errorText = errorMessage || UNKNOWN_ERROR_TEXT;
    // 既不出活动集也不删行：失败行必须留在页面上（转红 + 错误行）。
    // 原实现两件事一起做，于是用户盯着 63% 的进度条，行突然消失、零提示，
    // 分不清是失败、被别人取消、还是自己看花了眼。
    // 清理改由用户点行上的「移除」按钮触发（dismissTask）。
    window.TaskStore.commit(key, { status: 'failed', error_message: errorText, _key: key });

    console.error(`Task ${taskId} failed: ${errorText}`);

    // duration: 0 → ui.js 里 `if (duration > 0)` 不成立，不挂定时器，
    // toast 一直留到用户自己点 ×。默认的 3500ms 在这里没用：用户离座一趟
    // 回来照样什么都看不到。
    closeFailureToast(key);   // 同一任务只留最新的一条
    failureToasts.set(key, showToast(t('js.tasks.toast.failed', {message: errorText}), 'danger', { duration: 0 }));

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

// 错误原文的渲染已经交给组件：`{{ errorText }}` 走 Vue 插值，自动 HTML
// 转义。改造前必须先渲染一个空的 .task-error 容器、再用 textContent 事后
// 回填，唯一的理由就是 error_message（后端异常的字符串化结果，URL、路径、
// 第三方库报错原文都可能在里面）绝不能进 innerHTML 模板。这条路没了。

// 「移除」按钮：只把失败行从界面上拿走，不碰后端。
//
// 失败任务在后端已经是终态，再 POST /cancel 最好的情况也只是白跑一趟。
// 也刻意**没有**「重试」：三个 manager 的 start_task 都要求
// status in ('pending','paused')，对 failed 调用直接抛 ValueError，
// 重试得先改后端状态机。
function dismissTask(taskId, taskType = 'map') {
    const key = `${taskType}:${taskId}`;
    closeFailureToast(key);   // 行都不要了，那条常驻 toast 也别留着占地方
    // 纯前端删行：任务仍在后端（要彻底删除用行上的 🗑），下次翻页/刷新
    // 从 /api/history_all 重拉时它会回来——这正是「移除」与「删除」的区别。
    if (window.TaskStore) window.TaskStore.remove(key);
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

// 字节/秒 → 人类可读。1024 进制，与全站文件大小口径一致。
// 单位不翻译（B/s、MB/s 中英通用），只有「速度: 」前缀走 i18n。
// ≥100 时取整、否则保留一位小数：避免 99.9 → 100.0 之间字宽来回跳。
function formatSpeed(bps) {
    if (!(bps > 0)) return '0 B/s';
    const units = ['B/s', 'KB/s', 'MB/s', 'GB/s'];
    let value = bps;
    let i = 0;
    while (value >= 1024 && i < units.length - 1) {
        value /= 1024;
        i += 1;
    }
    const shown = (i === 0 || value >= 100) ? Math.round(value) : value.toFixed(1);
    return `${shown} ${units[i]}`;
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

// 行1 右侧的「已运行 / 预计剩余」每秒重算。
//
// 改造前这里遍历 activeTasks、逐个 getElementById 找行、再写 .task-time 的
// textContent。现在只推一下 store 的 tick，依赖它的 timeText computed 自己
// 失效重算 —— 组件里 `void store.state.tick` 那行建立的就是这个依赖。
function updateTimeDisplay() {
    if (window.TaskStore) window.TaskStore.bumpTick();
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
            // 单一时间流：取消的任务留在流里，只换形态（与 completed/failed
            // 同一处理），不删行。终态出活动集。
            if (window.TaskStore) {
                window.TaskStore.commit(key, { status: 'cancelled' });
                window.TaskStore.dropActive(key);
            }
        } else {
            const result = await response.json().catch(() => ({}));
            showToast(t('js.tasks.toast.cancel_failed', {error: result.error || response.status}), 'danger');
        }
    } catch (error) {
        showToast(t('js.tasks.toast.cancel_failed', {error: error.message}), 'danger');
    }
}
