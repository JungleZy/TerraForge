/**
 * 任务中心：任务时间流的**页面无关**那一半。
 *
 * ## 为什么要有这个文件（§6.1，phase-3 的闸门）
 *
 * 独立页 /history 曾经是个残废，而残废的原因**只是脚本加载顺序**：
 * tasks.js 与 map.js 挂在 index.html 的 extra_js 里（首页专属），而
 * task_list.js 的行组件用 `typeof startTask === 'function'` 之类的探测决定
 * 渲染什么 —— 于是 /history 上：
 *   · 没有启动 / 暂停 / 恢复按钮（startTask 等不存在）；
 *   · 没有任何实时更新（socket 监听全在 initTasks 里）；
 *   · 行1 右侧的「已运行 / 预计剩余」恒为 `—`（calculateTimeInfo 不存在）；
 *   · 行2 没有速度（formatSpeed 不存在）。
 * 同一个任务、同一份数据，在两个页面上能力完全不同。
 *
 * 修法是**抽取**而不是复制：凡是与「首页那张地图」无关的东西全搬到这里，
 * 由 base.html 在 task_list.js 之后加载（/config 把那个 block 整块覆盖成空，
 * 所以配置页不白付）。tasks.js 只留首页专属的状态栏胶水。
 * 复制是不能接受的：两份 socket 处理器会各自往 TaskStore 写一遍，同一条
 * 推送被 commit 两次；两份 1s tick 会让 bumpTick 每秒跳两格 —— 都不是
 * 「多花点性能」，是**同一份状态由两段代码决定**，正是这次要拆掉的东西。
 *
 * ## 闸门（本文件必须成立的三条）
 *   1. 同一个任务永远不会同时出现在两个互相矛盾的列表里 —— 时间流与活动集
 *      的写入全部经 TaskStore.commit 一次写两个集合，本文件不另存任何缓存；
 *   2. 所有恢复类命令幂等 —— startTask/pauseTask/resumeTask 只发一次 POST
 *      并把服务端的拒绝原因原文透出，前端不猜、不重试、不本地改状态；
 *   3. socket 状态与一次全新 fetch 一致 —— 断线重连分支（hasConnectedOnce）
 *      同时补拉活动集、时间流与统计卡，不留任何只靠事件维持的状态。
 *
 * ## 为什么是普通全局脚本而不是 IIFE
 * task_list.js 的行组件靠 `typeof startTask === 'function'` 与
 * `window[fnName]` 两条路径取动作函数，history.js / map.js 也直接调本文件的
 * 全局（closeFailureToast / loadActiveTasks / firstActiveTasksLoad …）。
 * 顶层 `function` 声明同时满足这两条（它会在 window 上留同名属性），而 IIFE
 * 里再手写一遍 `window.x = x` 只是把同一件事写两遍。搬家因此是**逐字移动**，
 * 函数体一个字都没改 —— 这是刻意的：能力回归靠加载位置，不靠重写。
 *
 * ⚠️ 顶层的 `let` / `const` 会**遮蔽**同名的 window 属性（见 socket.js 的
 * 文件头），所以本文件搬走的那些声明必须从 tasks.js 里删干净，不能两边都留。
 */

// socket 实例。map.js 的 initContourPreview 用 `typeof socket` 探测它 ——
// 这里必须是顶层 `let`（与搬家前 tasks.js 的形态一致）。
let socket;
let timeUpdateInterval = null;
// 标记是否已经完成首次 socket 连接。initTaskCenter 末尾会直接调一次
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
// 本文件是否已经接线。首页的 initTasks() 与本文件末尾的自举都会调
// initTaskCenter，两个入口哪个先跑都行，第二次是空操作 —— 重复注册 socket
// 监听会让每条推送被处理两遍（store 写两次、统计卡去抖被顶两次）。
let _taskCenterReady = false;

/**
 * 接线：渲染层挂载 + socket 监听 + 首屏拉取 + 1s 心跳。**幂等**。
 *
 * 在解析期（本文件末尾）直接调用，不等 DOMContentLoaded。两个理由：
 *   1. socket.io **不重放**错过的事件，get() 之后必须在同一个同步块里
 *      注册监听（完整理由见 socket.js 的文件头）；
 *   2. base.html 的脚本在 <body> 末尾，#historyTableBody 早已解析完 ——
 *      TaskList.mount() 此刻就能挂上，比等 DOMContentLoaded 少一帧空表。
 */
function initTaskCenter() {
    if (_taskCenterReady) return;
    _taskCenterReady = true;

    // 时间流的渲染层（Vue）挂到 #historyTableBody。幂等——首页的 initTasks
    // 与独立页的 initHistory 也各调一次，谁先谁后都行。
    if (window.TaskList) window.TaskList.mount();
    socket = window.TerraSocket.get();
    // 必须在 get() 之后的**同一个同步块**里就把监听挂上（见 socket.js 的说明）：
    // socket.io 不重放错过的事件，中间一让出事件循环就可能漏掉 connect。
    // 这一行兜的是「连接不是本页建的」那种情况：base_terrain_status.js 在本
    // 文件之前就 get() 过、此刻连接已经建好，本页的 connect 回调就永远不会
    // 触发，hasConnectedOnce 会卡在 false —— 于是断线重连后不补拉数据
    // （那正是它存在的唯一理由）。
    if (socket.connected) hasConnectedOnce = true;

    socket.on('connect', function() {
        console.log('Connected to server');
        if (hasConnectedOnce) {
            loadActiveTasks();
            // 断线窗口内的终态变化不会补发 socket 事件：只补拉活动列表的话，
            // 时间流里的行（历史流）和统计卡会永久停在断线前的状态——一并刷新。
            // loadHistory/loadStats/currentPage 是 history.js 的全局
            // （两个页面都加载它，typeof 守卫兜底）。
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
        refreshStatusBar();
    });

    socket.on('task_completed', function(data) {
        // 终态状态从载荷里取，**不能**在这里假定 'completed'：地图管线收官发的
        // 是 final_status（task_manager._complete_task），有缺块时它是
        // completed_with_gaps。写死的后果实测过 —— task_gap_decision 与
        // task_completed 是同一毫秒里前后脚发的两发，后到的这一发把行上刚落
        // 地的 completed_with_gaps 又覆盖回 completed，缺块状态一直要到整页
        // 刷新才回来（§13-3 要求这个标记永久跟着任务走，「刷新才对」不算数）。
        handleTaskCompleted(data.task_id, data.task_type || 'map', data.warning, data.status);
        emitStatusEvent(t('js.tasks.event.completed', {id: data.task_id}));
        refreshStatusBar();
    });

    socket.on('task_failed', function(data) {
        handleTaskFailed(data.task_id, data.task_type || 'map', data.error_message);
        emitStatusEvent(t('js.tasks.event.failed', {id: data.task_id}));
        refreshStatusBar();
    });

    // 缺口决策（§13-3）。两个时机各发一次：任务进 pending_decision，以及
    // 决定被应用之后。刻意**没有**逐条日志的 socket 事件 —— 本应用没有 room
    // 也没有 namespace，每一发 emit 都广播给所有客户端，日志尾随因此走 REST 轮询。
    socket.on('task_gap_decision', function(data) {
        handleTaskGapDecision(data);
        refreshStatusBar();
    });

    socket.on('task_stitch_progress', function(data) {
        emitStatusEvent(t('js.tasks.event.stitching', {id: data.task_id}));
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
        emitStatusEvent(t('js.tasks.event.copying', {id: data.task_id}));
        updateTaskStageText(
            data.task_id,
            t('js.tasks.stage.copying', {done: data.processed_tiles, total: data.total_tiles}),
            'map'
        );
    });

    // 插件任务（第五条管线）。事件名带 plugin_ 前缀而不是复用 task_progress：
    // 插件宿主是可选组件，它的推送必须能被单独识别。载荷里 task_type 恒为
    // 'plugin'（PluginTaskManager._emit），所以三个处理器都能直接复用四条
    // 核心管线那三个函数 —— 归一化按 task_type 查表，本来就不认识「管线」
    // 这个概念，多一条分支就是多一份会漂的抄写。
    socket.on('plugin_task_progress', function(data) {
        updateTaskProgress(data);
        refreshStatusBar();
    });

    // 非 failed 的每个终态都走这一发（PluginTaskManager 只有 failed / 非
    // failed 两个出口），**含 pending_decision** —— 所以状态一定要从载荷里
    // 取，且 handleTaskCompleted 不能无条件把任务摘出活动集（见那里）。
    socket.on('plugin_task_completed', function(data) {
        handleTaskCompleted(data.task_id, data.task_type || 'plugin',
                            data.warning, data.status);
        emitStatusEvent(t('js.tasks.event.completed', {id: data.task_id}));
        refreshStatusBar();
    });

    socket.on('plugin_task_failed', function(data) {
        handleTaskFailed(data.task_id, data.task_type || 'plugin',
                         data.error_message);
        emitStatusEvent(t('js.tasks.event.failed', {id: data.task_id}));
        refreshStatusBar();
    });

    // 首屏这次拉取的 Promise 挂到模块级：map.js 的 initContourPreview 等它
    // resolve 后共享 contour 数据，首屏 /api/contour/tasks 只拉一遍。
    firstActiveTasksLoad = loadActiveTasks();
    refreshStatusBar();

    // 每秒更新一次时长显示。clearInterval 对 null 是空操作（WHATWG timers），
    // 不需要守卫 —— initTaskCenter 幂等，重复调时这一行负责不留下两个 interval。
    clearInterval(timeUpdateInterval);
    timeUpdateInterval = setInterval(updateTimeDisplay, 1000);
}

// 底部状态栏的两个读数（活动任务聚合 / 最近事件）只有首页有元素，实现留在
// tasks.js（首页专属）。本文件不能直接调那两个全局：/history 不加载 tasks.js，
// 裸调是 ReferenceError，而它发生在 socket 处理器里 —— 整条实时链会静默断掉。
// 包一层而不是每个调用点各写一遍 typeof：调用点有 5 处。
function refreshStatusBar() {
    if (typeof updateStatusTasks === 'function') updateStatusTasks();
}

function emitStatusEvent(msg) {
    if (typeof pushStatusEvent === 'function') pushStatusEvent(msg);
}

async function loadActiveTasks() {
    try {
        // 三路带 ?status=active：服务端只回活动态（不传行为不变——后端
        // 未上线该参数时返回全量，下面白名单照样滤），completed
        // 不再随每次补拉往返。contour 路刻意不带：这份响应同时是地图预览
        // 面板的数据源（initContourPreview 要从里面筛 completed 任务，
        // 见 map.js），带上的话首屏还得再拉一遍全量。
        const [mapResp, demResp, localResp, contourResp, pluginResp] = await Promise.all([
            fetch('/api/tasks?status=active'),
            fetch('/api/dem/tasks?status=active'),
            fetch('/api/terrain/local/tasks?status=active'),
            fetch('/api/contour/tasks'),
            // 插件路刻意**留在同一个 Promise.all 里**而不是另起一条独立
            // fetch：下面的 setActive 是整体替换，一条晚到的独立请求会被它
            // 当场抹掉（或反过来抹掉别人）。而它同时**不进**下面那道
            // badResp 快速失败、自带 catch —— 插件宿主是可选组件（可以没装
            // 载、可以全被禁用），它一次 500 不该把另外四条管线的任务从界面
            // 上一起清空。
            fetch('/api/plugins/tasks?active=1').catch(function () { return null; })
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
        // 非 2xx / 请求本身失败都退化成「没有插件任务」，理由同上。
        const pluginData = pluginResp && pluginResp.ok
            ? await pluginResp.json().catch(function () { return {}; })
            : {};
        // 全量（含 completed）共享给 map.js 的等高线预览面板，首屏只拉这一遍
        latestContourTasks = contourData.tasks || [];
        // 每次拉取都顺带对齐预览注册表（幂等）。断线重连必须走这一步：
        // socket.io 不重放错过的事件，断线窗口内完成的等高线任务收不到
        // task_completed，不在这里补的话要到整页刷新才会出现预览按钮。
        // 独立页不加载 map.js，typeof 守卫兜底。
        if (typeof syncContourPreviewFromLatest === 'function') {
            syncContourPreviewFromLatest();
        }

        const mapTasks = (mapData.tasks || []).map(t => normalizeTask(t, 'map'));
        const demTasks = (demData.tasks || []).map(t => normalizeTask(t, 'dem'));
        const localTasks = (localData.tasks || []).map(t => normalizeTask(t, 'local_terrain'));
        const contourTasks = (contourData.tasks || []).map(t => normalizeTask(t, 'contour'));
        const pluginTasks = (pluginData.tasks || []).map(t => normalizeTask(t, 'plugin'));
        const all = [...mapTasks, ...demTasks, ...localTasks, ...contourTasks,
                     ...pluginTasks].filter(t =>
            // completed 由服务端 ?status=active 挡掉（contour 路拉的是
            // 全量，终态在这里被白名单丢弃——它只需要活动态进这个 Map）。
            // failed 仍保留：失败行的「删除」（deleteTask）与 socket 失败事件
            // 都按 key 在这个 Map 里找任务；状态栏聚合自己会再滤掉非活动态。
            //
            // 白名单不在这里再抄一份状态清单：TaskStore.ACTIVE_STATUSES 是
            // 全站唯一一份（后端 ACTIVE_TASK_STATES 的镜像），加状态时只有
            // 一处要改。pending_decision 也在里面 —— 它占着产物目录、等着
            // 用户决定，不进活动集就等于「任务不见了」。
            window.TaskStore.ACTIVE_STATUSES.includes(t.status) || t.status === 'failed'
        );

        // 活动任务集进 store：状态栏聚合（updateStatusTasks）与行1 耗时
        // 每秒刷新都读它。时间流（渲染源）是另一个集合，由 history.js 的
        // loadHistory 从 /api/history_all 分页拉——两者的区别见 task_store.js
        // 里 state.active 的注释。
        if (window.TaskStore) window.TaskStore.setActive(all);
        refreshStatusBar();
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
    if (type === 'plugin') {
        // 计数字段两个来源、两套名字：socket 推送与 /api/plugins/tasks 给的是
        // plugin_tasks 的原始列名（total_items / downloaded_items），而
        // /api/history_all 的 UNION 把它们别名成了 total / downloaded（五段
        // 要列序对齐，见 api.py 的第五个 SELECT）。两者都要吃得下 —— 少一半
        // 的表现是历史流里的插件行进度恒为 0%，而 JS 读不到的字段是静默
        // undefined，没有任何报错。
        return {
            ...task,
            task_type: 'plugin',
            id: task.id,
            _key: `plugin:${task.id}`,
            total_items: task.total_items || task.total || 0,
            downloaded_items: task.downloaded_items || task.downloaded || 0,
            failed_items: task.failed_items || 0,
            items_label: t('js.tasks.unit.tile')
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
    if (!window.TaskStore) return;

    let tilingText;
    // 切片阶段的百分比口径（null = 无，行退回自己的计数）。行自己的
    // total_items/downloaded_items 在这一段已经**恒为满**：本地地形任务的
    // 计数是上传的文件数，上传（或 dem_task 来源的零拷贝）秒级结束就写满，
    // 而切片要跑几十分钟 —— 不给这个字段，进度条与「NN%」整段显示 100%。
    // 等高线管线是同一个问题，那边的解法是阶段切换（contourPhaseCounts）；
    // 这里不切计数字段，因为切片的分母是**瓦片**而行上的单位是文件，混用会
    // 让 countText 说谎（「已上传 120/2000 个文件」）。
    let tilingProgress = null;
    // 「预计剩余」用的锚：这一段（逐瓦片）自己的起点、以及起点处的百分比。
    // 不能拿整任务的墙钟时长去除切片百分比 —— elapsed 里含上传与物化，切片
    // 刚起步时外推出来的是天文数字。三个字段一起写，缺一不算数。
    let tilingPhase = null;
    let tilingStartedAt = null;
    let tilingAnchorPct = null;
    if (data.status && data.status !== 'running') {
        tilingText = '';
    } else if (data.stage_label) {
        // 物化 / 建金字塔：这一段跑在 total 算出来之前，没有分母，只能报比例。
        // 刻意不给锚 —— 这一段之后还压着整个切片，给 ETA 等于说「快好了」。
        const pct = Math.round((Number(data.stage_fraction) || 0) * 100);
        tilingText = t('js.tasks.terrain_stage', { stage: data.stage_label, pct: pct });
        tilingProgress = pct;
        tilingPhase = 'stage';
    } else if (Number(data.total_tiles) > 0) {
        const done = Number(data.rendered_tiles) || 0;
        const total = Number(data.total_tiles);
        tilingText = t('js.tasks.terrain_tiling', { done: done, total: total });
        // 钳到 100：切片器的 total 是估算值，实测末尾会出现 done > total。
        tilingProgress = Math.min(100, Math.round((done / total) * 100));
        tilingPhase = 'tiles';
        const prev = window.TaskStore.get(key) || window.TaskStore.getActive(key) || {};
        // 同一段继续跑：锚不动。倒退（并行 worker 崩溃会回退串行、计数从 0
        // 重来，见 cesium_terrain 的 BrokenProcessPool 分支）就重新锚定，
        // 否则 ETA 要等进度爬回旧锚点才重新出现。
        const sameRun = prev.tiling_phase === 'tiles'
            && prev.tiling_started_at != null
            && prev.tiling_anchor_pct != null
            && tilingProgress >= prev.tiling_anchor_pct;
        tilingStartedAt = sameRun ? prev.tiling_started_at : Date.now();
        tilingAnchorPct = sameRun ? prev.tiling_anchor_pct : tilingProgress;
    } else {
        return;
    }
    // commit 而不是 commitTaskUpdate（= TaskStore.patch）：patch 只写时间流，
    // 而底部状态栏的汇总读数读的是**活动集**，只写时间流的话行上对了、状态栏
    // 仍恒显 100%。commit 两个集合都写，且切片任务落在第 2 页之后（时间流里
    // 没有它）时这一发也不会整个丢掉。载荷不带 status，不会凭空建活动条目
    // （见 task_store.js commit 的 isLive 分支）。
    window.TaskStore.commit(key, {
        tiling_text: tilingText, tiling_progress: tilingProgress,
        tiling_phase: tilingPhase, tiling_started_at: tilingStartedAt,
        tiling_anchor_pct: tilingAnchorPct });
}

// 新任务到达时插到时间流顶部。
// 条件：当前在第 1 页且筛选 chip 是 全部/进行中——其它页码/其它筛选下硬插
// 会破坏「按创建时间倒序 + 状态筛选」的语义（任务会出现在它不该出现的页里）。
// 不满足时不插：翻页/切 chip 会从 /api/history_all 重拉，任务自然出现。
// currentPage / currentStatusFilter 是 history.js 的全局（typeof 守卫兜底：
// 本文件在解析期就接线，那时 history.js 还是 defer 状态、尚未执行）。
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
    // prependStreamRow 才会真插 —— 见它自己开头那道「已在流里就原地更新」的
    // 守卫)。**插没插都
    // 要继续 commit** —— 活动集必须写:
    //   1. 不写状态栏就漏算这个任务(进行中数量、汇总进度全少一份);
    //   2. 更糟的是 handleTaskFailed 的 known 守卫(它开头那句
    //      `if (!known) return;`)会为 false,
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

function handleTaskCompleted(taskId, taskType, warning, status) {
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
        //
        // status 用载荷给的那个。这里**不枚举**「哪些终态算数」—— 枚举出来
        // 就是第二份状态机，后端加一档终态时它会把新状态静默改判成
        // completed，而这种降级没有任何报错，只有用户某天发现产物少了标记。
        // 回落只兜**不带 status 的老载荷**（早期的 map 收官 emit 没有这个字段）。
        window.TaskStore.commit(key, { status: status || 'completed' });
        // 出活动集的判据是**新状态**，不是「收到了终态事件」：插件管线的
        // pending_decision 正是走 plugin_task_completed 这一发送来的
        // （PluginTaskManager 只有 failed / 非 failed 两个出口），无条件摘掉
        // 会让状态栏少算一个还占着产物目录、还在等用户决定的任务。判据走
        // store 那份活动态清单（后端 ACTIVE_TASK_STATES 的镜像），不在这里
        // 抄第二份状态字面量。
        if (!window.TaskStore.ACTIVE_STATUSES.includes(status || 'completed')) {
            window.TaskStore.dropActive(key);
        }
    }

    // 统计卡（总任务/已完成/失败/累计下载量）跟着终态走。loadStats 是
    // history.js 的全局（两个页面都加载它，typeof 守卫兜底）。
    // 批量收官时每个任务各发一次终态事件，立即刷新会 N 连发——300ms 去抖
    // 合并成一次（字面量 loadStats() 保留在调用处：契约测试按正则点名它）。
    if (typeof loadStats === 'function') {
        clearTimeout(_loadStatsDebounceTimer);
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
    // 清理改由用户点行上的「删除」按钮（deleteTask）触发。
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
        clearTimeout(_loadStatsDebounceTimer);
        _loadStatsDebounceTimer = setTimeout(() => {
            _loadStatsDebounceTimer = null;
            loadStats();
        }, 300);
    }
}

// 错误原文的渲染已经交给组件：`{{ errorText }}` 走 Vue 插值，自动 HTML
// 转义。改造前必须先渲染一个空的 .task-error 容器、再用 textContent 事后
// 回填，唯一的理由就是 error_message（后端异常的字符串化结果，URL、路径、
// 第三方库报错原文都可能在里面）绝不能进 innerHTML。这条路没了。

// getStatusColor / getStatusText 已收口到 static/js/task_status.js（全站唯一
// 一份，base.html 全局加载）。

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

// 字节 → 人类可读的 formatBytes 在 static/js/ui.js（window.formatBytes）。
// **不能**放在本文件：/config 独立页把 base.html 的 vendor_task_list_js 块
// 覆盖成空（省掉 Vue + 三个任务脚本约 160 KB），本文件在那一页根本不加载，
// 而配置页的缓存卡要用它 —— 放这儿就是一页 ReferenceError。ui.js 是无条件
// 全局加载的那一档。
//
// 它与上面的 formatSpeed 刻意是**两个**函数：取整规则不一样，而那个差别有
// 理由。速度是每秒重画的活读数，≥100 取整是为了不让字宽在 99.9 ↔ 100.0
// 之间来回跳；文件大小是静态读数，没有这个问题，一律留一位小数反而更准。

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
        // 如果任务正在运行，加上当前这一段的时间。补漏重跑（retrying）与
        // 首次下载同样在推进，同样要叠当前段 —— 判据交给 store 的活动态
        // 清单，不在这里再写一份状态字面量。
        if (window.TaskStore && window.TaskStore.isRunning(task) && task.started_at) {
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

    if (window.TaskStore && window.TaskStore.isRunning(task)
        && task.downloaded_items > 0 && task.total_items > 0) {
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
//
// 没有活动任务时直接跳过：那时没有任何 computed 依赖 tick，改它等于白唤醒
// 一次 Vue 的调度器。页面开着不动时这个 interval 是永久运行的。
function updateTimeDisplay() {
    if (!window.TaskStore) return;
    if (window.TaskStore.liveTasks().length === 0) return;
    window.TaskStore.bumpTick();
}

function apiPrefixForType(taskType) {
    if (taskType === 'dem') return '/api/dem/tasks';
    if (taskType === 'local_terrain') return '/api/terrain/local/tasks';
    if (taskType === 'contour') return '/api/contour/tasks';
    if (taskType === 'plugin') return '/api/plugins/tasks';
    return '/api/tasks';
}

async function startTask(taskId, taskType = 'map') {
    try {
        const response = await fetch(`${apiPrefixForType(taskType)}/${taskId}/start`, {
            method: 'POST'
        });
        if (!response.ok) {
            // 与 pause/resume 同口径：透出服务端给的具体原因，不只报"失败"
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

// ---------------------------------------------------------------------------
// 缺口决策（§13-3）
// ---------------------------------------------------------------------------
//
// 「默认严格不产出」如果没有一个承载「等你决定」的状态，用户拿到的就是一个
// 静默卡住的任务。所以后端有 pending_decision，前端必须给出它的两条出路：
// 补漏（只重跑可重试的那些块）与接受并导出（永久带缺块标记）。
//
// ⚠️ 这四个结局名是后端 TileOutcome 的值（src/contracts/outcome.py），这里
// 只有**文案表**的键抄了它们一份。抄不掉：t() 的键必须是完整字面量
// （tests/test_i18n.py 的双向闭合按字面量扫源码，拼接出来的键会被判成
// 「无人引用」而删掉文案）。同一形态的先例是 history.js 的
// TERRAIN_QUALITY_KEYS。数量表（by_outcome）本身**不抄** —— 它按服务端
// 返回的键遍历，后端加一档结局这里不用改。
const GAP_OUTCOME_LABELS = {
    'no_data': t('js.gaps.outcome.no_data'),
    'retryable_failure': t('js.gaps.outcome.retryable_failure'),
    'permanent_failure': t('js.gaps.outcome.permanent_failure'),
    'cache_failure': t('js.gaps.outcome.cache_failure'),
};

/** 结局名 -> 界面文案。认不出的结局原样显示，不静默吞掉。 */
function gapOutcomeLabel(outcome) {
    // hasOwnProperty 同 getStatusColor 的理由：裸下标下 outcome ===
    // 'constructor' 会取到构造函数，`||` 兜不到，一坨函数源码进界面。
    return (Object.prototype.hasOwnProperty.call(GAP_OUTCOME_LABELS, outcome)
        && GAP_OUTCOME_LABELS[outcome]) || outcome;
}

/**
 * `by_outcome` -> 「无数据 1,200 · 可重试 30」。
 *
 * 按服务端返回的键遍历、且**跳过 0** —— 四档里通常只有一两档非零，把
 * 「永久失败 0」也印出来会让一条本可一眼看完的读数变成四段噪声。
 */
function gapBreakdownText(byOutcome) {
    if (!byOutcome) return '';
    return Object.keys(byOutcome)
        .filter(k => Number(byOutcome[k]) > 0)
        .map(k => t('js.gaps.pair', {
            label: gapOutcomeLabel(k),
            count: Number(byOutcome[k]).toLocaleString(),
        }))
        .join(' · ');
}

// 正在飞的 gap 摘要请求（key 集合）。行组件在 mounted 与状态变化时都会调
// ensureGapSummary，不去重的话一次翻页能对同一个任务打出十几个请求。
const _gapSummaryInFlight = new Set();

/**
 * GET <前缀>/<id>/gaps。失败抛出（调用方自己决定要不要 toast）。
 *
 * 两条管线各有一份：地图的在 /api/tasks/<id>/gaps，插件的在
 * /api/plugins/tasks/<id>/gaps。**回的形状不一样** —— 插件那份少
 * `explained` 与 `samples`（宿主不知道插件的哪种洞算「已交代」，也不存样本），
 * 总数叫 `gap_tiles`、决策叫 `gap_decision`。消费者按字段在不在决定渲染
 * 什么，不在这里补假字段。
 */
async function fetchGapSummary(taskId, taskType = 'map') {
    const response = await fetch(`${apiPrefixForType(taskType)}/${taskId}/gaps`);
    if (!response.ok) {
        const result = await response.json().catch(() => ({}));
        throw new Error(result.error || ('HTTP ' + response.status));
    }
    return response.json();
}

/**
 * 把缺口摘要拉回来挂到 store 上的任务对象（字段 `gap_summary`）。
 *
 * 只对有瓦片级缺块记录的管线有意义（地图与插件各有一份 `/gaps`）。
 * **幂等**且带在飞去重。
 *
 * 失败必须**落进 store**（`gap_summary_error`），不能像从前那样 console 一句
 * 就返回 null：本函数的调用方只有行组件那三个触发点（mounted 与
 * task.status / task.gap_tiles 两条 watch），一次超时之后它们谁都不会再响
 * —— keyed diff 复用组件实例所以不 mount，两条 watch 的值也没变。于是那一行
 * 永久停在「正在读取缺块明细…」，只有整页刷新救得回来（实测）。记下失败让行
 * 切到错误态并给出「重试」，把决定权交回用户。
 *
 * 这里**刻意不装**退避重试：那等于给每一条带缺块的行挂一个后台轮询，而缺块
 * 明细是读一次就够的静态数据，为一次超时付上永久的定时请求不成比例。
 */
async function ensureGapSummary(key, taskId, taskType) {
    if ((taskType !== 'map' && taskType !== 'plugin') || !window.TaskStore) {
        return null;
    }
    if (_gapSummaryInFlight.has(key)) return null;
    _gapSummaryInFlight.add(key);
    // 起手就清掉上一次的失败标记：行立刻回到「正在读取…」，用户按「重试」
    // 看得见反应。不清的话失败文案会一直挂着，按下去与没按毫无区别。
    window.TaskStore.commit(key, { gap_summary_error: '' });
    try {
        const summary = await fetchGapSummary(taskId, taskType);
        window.TaskStore.commit(key, { gap_summary: summary });
        return summary;
    } catch (error) {
        console.error(`Failed to load gap summary for task ${taskId}:`, error);
        window.TaskStore.commit(key, { gap_summary_error: error.message || String(error) });
        return null;
    } finally {
        _gapSummaryInFlight.delete(key);
    }
}

/**
 * 补漏：只重跑记录在案、且结局可重试的那些块。
 *
 * 状态由后端判定（completed_with_gaps / pending_decision / failed 之外一律
 * 400），前端不复制那张状态表 —— 复制出来的第二份迟早与后端漂移，而漂移的
 * 表现是「按钮点了没反应」。这里只负责把服务端的拒绝原因原文透出。
 */
async function refillTask(taskId, taskType = 'map') {
    try {
        const response = await fetch(`/api/tasks/${taskId}/refill`, { method: 'POST' });
        if (!response.ok) {
            const result = await response.json().catch(() => ({}));
            throw new Error(result.error || ('HTTP ' + response.status));
        }
        // 状态改由后端广播（task_progress / task_gap_decision）落地，这里
        // 不本地改 —— 本地先改会与随后到达的权威推送打架，且补漏被拒时
        // 界面已经翻成 retrying 了。
        showToast(t('js.gaps.toast.refill_started', { id: taskId }), 'info');
    } catch (error) {
        showToast(t('js.gaps.toast.refill_failed', { error: error.message }), 'danger');
    }
}

/**
 * 接受缺口并导出：pending_decision -> completed_with_gaps，跑严格模式拒绝
 * 过的拼接/复制阶段。
 *
 * 必须二次确认：这是一条**不可撤销**的决定，产物与历史都会永久带缺块标记。
 */
async function acceptTaskGaps(taskId, taskType = 'map') {
    const key = `${taskType}:${taskId}`;
    const row = window.TaskStore
        ? (window.TaskStore.get(key) || window.TaskStore.getActive(key)) : null;
    const total = (row && row.gap_tiles) || 0;
    const ok = await showConfirm(t('js.gaps.confirm_accept', { n: total.toLocaleString() }), {
        title: t('js.gaps.action.accept'),
        confirmText: t('js.gaps.action.accept'),
        type: 'warning',
    });
    if (!ok) return;
    try {
        // 两条管线的端点名不同（地图 accept_gaps，插件 accept-gaps），回的
        // 东西也不同 —— 分开写，不硬凑成一条 URL 模板。
        const isPlugin = taskType === 'plugin';
        const url = isPlugin
            ? `/api/plugins/tasks/${taskId}/accept-gaps`
            : `/api/tasks/${taskId}/accept_gaps`;
        const response = await fetch(url, { method: 'POST' });
        if (!response.ok) {
            const result = await response.json().catch(() => ({}));
            throw new Error(result.error || ('HTTP ' + response.status));
        }
        // 插件管线的「接受缺块」是**回写参数 + 重跑**（PluginTaskManager
        // .accept_gaps 末尾直接 start_task），端点只回 {success:true}。在这里
        // 本地写一个终态就是撒谎：行会翻成「已完成（有缺口）」并被摘出活动
        // 集，而后端下一刻就开始发 plugin_task_progress —— 那些推送又会把一个
        // 刚被摘掉的任务塞回来，两边打架。权威状态交给推送，这里只补一次活动
        // 列表（补拉是幂等的，重跑那一发进度比本次响应更早到也不影响）。
        if (isPlugin) {
            showToast(t('js.gaps.toast.accepted', {
                n: Number(total).toLocaleString(),
            }), 'warning');
            loadActiveTasks();
            return;
        }
        // 地图管线的端点回的就是新的 gap_summary（含 status / decision）——
        // 直接落地，不再多打一次 GET /gaps。
        const summary = await response.json();
        if (window.TaskStore) {
            window.TaskStore.commit(key, {
                status: summary.status || 'completed_with_gaps',
                gap_decision: summary.decision || 'accept',
                gap_tiles: summary.total != null ? summary.total : total,
                gap_summary: summary,
            });
            window.TaskStore.dropActive(key);
        }
        showToast(t('js.gaps.toast.accepted', {
            n: Number(summary.total || total).toLocaleString(),
        }), 'warning');
    } catch (error) {
        showToast(t('js.gaps.toast.accept_failed', { error: error.message }), 'danger');
    }
}

/**
 * 这个任务能导出成哪些格式。抛异常给调用方，**不在这里吞**：拉不到格式表
 * 就没有正确的下一步（写死 mbtiles 是在猜，静默不做是让按钮看着没反应）。
 *
 * 为什么必须问服务端：格式表 = 宿主自带的 mbtiles + 已启用插件注册的
 * Exporter，而「这个任务有没有那种格式吃得下的产物」还要拿产物登记行对照
 * 每个导出器的 accepts()。两半都在后端，前端一半都推不出来。
 */
async function fetchExportFormats(taskType, taskId) {
    const response = await fetch(`/api/export/${taskType}/${taskId}/formats`);
    if (!response.ok) {
        const result = await response.json().catch(() => ({}));
        throw new Error(result.error || ('HTTP ' + response.status));
    }
    const body = await response.json();
    return Array.isArray(body.formats) ? body.formats : [];
}

/**
 * 导出成品：把任务的产物打成单文件容器并登记成 Artifact。
 *
 * §5.3 的决定是「容器是**通用产物**，不是第四种 output_format」：同一个任务
 * 可以同时持有 XYZ 目录、逐层 GeoTIFF、一个 MBTiles 和一个 GeoPackage。所以
 * 端点是 `POST /api/export/<pipeline>/<id>` —— 影像、等高线与插件注册的导出器
 * 走**同一条**路由（与只读侧的 /mbtiles/<pipeline>/... 单路由同一条原则：
 * §5.3 明确禁止按数据类型各开一条）。pipeline 由调用方给，服务端按
 * src.contracts.artifact.PIPELINES 校验，不支持的管线回 400。
 *
 * 格式**先问再导**（GET .../formats），按可用种数分三条路：
 *   0 种 —— 说一句就走，不发 POST。dem / local_terrain 一件产物都不登记，
 *           它们的按钮压根不该出现，真出现了也不能让用户去撞一个 400。
 *   1 种 —— 直接导，不弹框。改造前的手感一字不差（当时唯一那种就是 mbtiles）。
 *   多种 —— 弹选择框。改造前 body 写死 `{format:'mbtiles'}`，后果不是「默认值
 *           选得不好」，是插件注册的导出器**在界面上没有任何入口** ——
 *           后端把 gpkg 接进这条路由了，用户点不到。
 *
 * 耗时与瓦片数成正比（几万张要几十秒），所以按钮当场上锁 —— 不锁的话用户会
 * 连点，后端每一发都真的重打一遍同一个文件。锁一直持续到选择框关掉之后
 * （finally 在 await 链的末尾）：框开着时按钮还能点就能开出第二个框。
 */
async function exportTask(taskId, taskType = 'map', button = null) {
    if (button) button.disabled = true;
    try {
        const formats = await fetchExportFormats(taskType, taskId);
        if (!formats.length) {
            showToast(t('js.export.toast.nothing_to_export'), 'warning');
            return null;
        }
        let format = formats[0];
        if (formats.length > 1) {
            const answer = await showConfirm(t('js.export.confirm.message'), {
                title: t('js.export.confirm.title'),
                confirmText: t('js.export.confirm.ok'),
                select: {
                    label: t('js.export.confirm.format_label'),
                    // 选项文案就是格式 id 本身，不做美化查表：这些 id 一半来自
                    // 插件的 format_id()，宿主给 'gpkg' 配一个显示名就等于把插件
                    // 的清单抄进宿主，下一个插件注册的格式又会变成一串没查到表的
                    // 回落。id 同时是 POST 上去的那个值，看到什么就是导什么。
                    options: formats.map(f => ({ value: f, label: f })),
                    value: format,
                },
            });
            // 带 select 时 showConfirm resolve 的是**对象**（恒为真），必须判
            // .confirmed —— 直接 `if (!answer)` 会让取消也照导（ui.js 里那段
            // 说明讲的就是这个静默失效）。
            if (!answer.confirmed) return null;
            format = answer.selected;
        }
        const response = await fetch(`/api/export/${taskType}/${taskId}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ format }),
        });
        if (!response.ok) {
            const result = await response.json().catch(() => ({}));
            throw new Error(result.error || ('HTTP ' + response.status));
        }
        const result = await response.json();
        // 两条成功文案不是重复：mbtiles 的响应带 tile_count 与 has_gaps（打包器
        // 数过每一块瓦片），插件导出器的响应只有 {path, format} —— 协议里没有
        // 让第三方报块数的地方。拿一份带 {count} 的文案去套插件分支，界面上就是
        // 「已导出 MBTiles（0 块瓦片）」，两处都在撒谎。
        if (result.tile_count != null) {
            showToast(t('js.gaps.toast.exported', {
                path: result.path,
                count: Number(result.tile_count).toLocaleString(),
            }), result.has_gaps ? 'warning' : 'success');
        } else {
            showToast(t('js.export.toast.exported', {
                format: result.format || format,
                path: result.path,
            }), 'success');
        }
        return result;
    } catch (error) {
        showToast(t('js.gaps.toast.export_failed', { error: error.message }), 'danger');
        return null;
    } finally {
        if (button) button.disabled = false;
    }
}

/**
 * `task_gap_decision` 推送 -> store。
 *
 * 载荷：{task_id, task_type, status, gap_tiles, by_outcome}。两个时机各发
 * 一次（进 pending_decision、决定被应用），所以处理必须幂等。
 *
 * by_outcome 直接拼成 gap_summary 的形状塞进任务对象：行上的分档读数因此
 * **不必**再打一次 GET /gaps —— 推送里已经有全部数字了。samples 只有详情
 * 弹窗要，那条路自己拉。
 */
function handleTaskGapDecision(data) {
    if (!window.TaskStore || !data) return;
    const taskType = data.task_type || 'map';
    const key = `${taskType}:${data.task_id}`;
    const patch = {
        status: data.status,
        gap_tiles: data.gap_tiles || 0,
        gap_summary: Object.assign(
            {},
            window.TaskStore.get(key) && window.TaskStore.get(key).gap_summary,
            {
                task_id: data.task_id,
                total: data.gap_tiles || 0,
                by_outcome: data.by_outcome || {},
                status: data.status,
            }
        ),
    };
    window.TaskStore.commit(key, patch);
    // pending_decision 不再推进：出活动集，否则状态栏会把它当成一个永远停在
    // N% 的进行中任务，「N 个活动任务」的读数从此不会归零。它仍留在时间流里
    // （用户要在那儿做决定），也仍占着产物目录 —— 后端的 ACTIVE_TASK_STATES
    // 把它算作活动是另一回事（缓存清理、退出确认），与状态栏的进度聚合无关。
    if (data.status === 'pending_decision' || data.status === 'completed_with_gaps') {
        window.TaskStore.dropActive(key);
    }
    if (data.status === 'pending_decision') {
        emitStatusEvent(t('js.gaps.event.pending_decision', { id: data.task_id }));
    }
}

// 解析期自举。等 DOMContentLoaded 是错的：socket.io 不重放错过的事件，
// get() 与 socket.on 之间不许让出事件循环（见 socket.js 的文件头）。
// 首页的 initTasks() 会再调一次，幂等空转。
initTaskCenter();
