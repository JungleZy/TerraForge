let historyViewer;
let currentPage = 1;
let allTasks = [];
// 状态筛选 chips 的当前取值（'' = 全部，'active' = 进行中三态）。
// 作用于整个时间流：透传给 /api/history_all 的 ?status= 参数（后端已支持）。
let currentStatusFilter = '';

function initHistory() {
    // 时间流的渲染层（Vue）挂到 #historyTableBody。幂等，首页由 initTasks
    // 也调一次——两个入口哪个先跑都行。必须在 loadHistory 之前：晚于它的话
    // 首屏那批数据写进 store 时还没有消费者。
    if (window.TaskList) window.TaskList.mount();
    initHistoryMap();
    loadHistory(1);
    loadStats();

    document.getElementById('searchInput').addEventListener('input', function(e) {
        filterTasks(e.target.value);
    });

    // 状态筛选 chips（2026-08 单一时间流定稿）：全部/进行中/失败/已完成/已取消。
    // 作用于整个时间流（活动任务也在流里）：取值透传给 /api/history_all
    // 的 ?status= 参数，「进行中」对应特殊值 active（pending/running/paused）。
    document.querySelectorAll('#statusChips .status-chip').forEach(function(chip) {
        chip.addEventListener('click', function() {
            document.querySelectorAll('#statusChips .status-chip').forEach(function(c) {
                c.classList.remove('active');
            });
            this.classList.add('active');
            currentStatusFilter = this.dataset.status || '';
            loadHistory(1);
        });
    });
}

// 历史小地图底图与主视图同一份配置（tile_servers 第一条，语法与
// map.js _baseMapUrl 一致），不再硬编码外网 OSM——断网/内网部署时
// 主视图可用而小地图白屏是矛盾的。拿不到配置时保持 OSM 回退。
// 与 map.js 是刻意重复的两份（无构建工具、独立历史页不加载 map.js，
// 收敛到公共文件属于第三档）。
function _historyBaseMapUrl(serversRaw) {
    // 两种配置形态都认：首页内联 config 的 tile_servers 是裸逗号串；
    // 独立页 /history 走 /api/config，配置项是 {"updated_at":..., "value": ...}
    // 包装对象——不拆包的话 (serversRaw||'').split 直接 TypeError，
    // initHistoryMap 整个挂掉、小地图白屏（2026-08 实测）。
    if (serversRaw && typeof serversRaw === 'object') {
        serversRaw = serversRaw.value || '';
    }
    const first = (serversRaw || '').split(',').map(s => s.trim()).filter(Boolean)[0];
    if (!first) return 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
    if (first.startsWith('http://') || first.startsWith('https://')) {
        return first.replace('{style}', 'm');
    }
    const host = first.includes('.') ? first : first + '.googleapis.com';
    return `//${host}/vt?lyrs=m&x={x}&y={y}&z={z}`;
}

async function _resolveHistoryTileServers() {
    // 首页（index.html）内联了全局 config；独立历史页没有，走 /api/config 拿。
    if (typeof config !== 'undefined' && config && config.tile_servers) {
        return config.tile_servers;
    }
    try {
        const r = await fetch('/api/config', { cache: 'no-store' });
        const j = await r.json();
        return (j.config && j.config.tile_servers) || '';
    } catch (e) {
        return '';      // 接口失败：_historyBaseMapUrl 回退 OSM
    }
}

async function initHistoryMap() {
    // 历史区域小地图：Cesium 只读视图（地图系统已从 Leaflet 切到 CesiumJS）
    const tileUrl = _historyBaseMapUrl(await _resolveHistoryTileServers());
    historyViewer = new Cesium.Viewer('historyMap', {
        baseLayer: new Cesium.ImageryLayer(new Cesium.UrlTemplateImageryProvider({
            url: tileUrl,
            tilingScheme: new Cesium.WebMercatorTilingScheme(),
            credit: '© OpenStreetMap contributors',
        })),
        baseLayerPicker: false,
        geocoder: false,
        homeButton: false,
        sceneModePicker: false,
        navigationHelpButton: false,
        animation: false,
        timeline: false,
        fullscreenButton: false,
        selectionIndicator: false,
        infoBox: true,      // 点矩形弹任务摘要（替代 Leaflet bindPopup）
        // 与主视图同一口径：星空盒（864 KB 贴图 + 1.8 MB IAU2006_XYS）关掉，
        // 大气辉光 skyAtmosphere 保留（shader 算的，不吃贴图）。
        skyBox: false,
    });
    historyViewer.scene.moon = undefined;
    // 按需渲染：小地图非交互主视图，常开会空转渲染循环。画面更新点
    // （renderHistoryMap 增删实体、panels.js 重开面板）各自显式 requestRender 兜底。
    historyViewer.scene.requestRenderMode = true;
    historyViewer.scene.maximumRenderTimeChange = Infinity;
    historyViewer.camera.setView({
        destination: Cesium.Cartesian3.fromDegrees(104.0, 35.0, 14000000),
    });
}

async function loadStats() {
    try {
        const r = await fetch('/api/history_stats', { cache: 'no-store' });
        const j = await r.json();
        if (!j.success) return;
        const s = j.stats;
        document.getElementById('statTotal').textContent = s.total_tasks;
        document.getElementById('statCompleted').textContent = s.completed;
        document.getElementById('statFailed').textContent = s.failed;
        document.getElementById('statDownloaded').textContent = s.total_downloaded.toLocaleString();
    } catch (e) {
        console.error('Failed to load stats:', e);
    }
}

// L7：请求序号。状态筛选 chip 连点（无防抖、无禁用、无 in-flight 标志）时，
// 先发的响应可能后返回 —— chip 高亮与 currentStatusFilter 已是新值，表格和
// allTasks 却是旧筛选集合。currentPage 的赋值也必须挪到守卫之后：它是
// panels.js / tasks.js 读取的那个全局，先写会被过期响应污染。
let _historyReqSeq = 0;

async function loadHistory(page = 1) {
    const seq = ++_historyReqSeq;
    try {
        const statusParam = currentStatusFilter
            ? `&status=${encodeURIComponent(currentStatusFilter)}`
            : '';
        const response = await fetch(`/api/history_all?page=${page}&per_page=20${statusParam}`, { cache: 'no-store' });
        const data = await response.json();
        if (seq !== _historyReqSeq) return;   // 已有更新的请求发出，本次结果作废

        if (!data.success) {
            throw new Error(data.error || 'Failed to load history');
        }

        currentPage = page;
        allTasks = data.tasks || [];
        renderHistoryTable(allTasks);
        const p = data.pagination || {};
        renderPagination(p.page || 1, p.total_pages || 1);
        renderHistoryMap(allTasks);
    } catch (error) {
        if (seq !== _historyReqSeq) return;   // 过期请求的错误不该覆盖新结果
        console.error('Failed to load history:', error);
        // 错误提示走 store，由组件渲染 —— #historyTableBody 已经被 Vue 接管，
        // 直接写它的 innerHTML 会在下一次 patch 时被覆盖掉。
        if (window.TaskStore) window.TaskStore.setLoadError(t('js.history.load_failed'));
    }
}

// 时间流失败行的兜底文案（与 tasks.js 的 UNKNOWN_ERROR_TEXT 同语义，
// 但**不能**同名：首页两个文件共享全局作用域，const 重名会直接
// SyntaxError 让整个文件失效——函数声明可以重复，const 不行）。
const HISTORY_UNKNOWN_ERROR = t('js.history.unknown_error');

// 时间流的数据入口。渲染由 task_list.js 的 Vue 组件负责——这里只把数据
// 交给 store，DOM 跟着变。
//
// 改造前这个函数干三件事：拼 innerHTML、按行 getElementById 找失败行、
// 用 textContent 补错误原文。后两件是因为 error_message 是后端异常的字符串化
// 结果，不能进 innerHTML 模板；现在组件用 `{{ }}` 插值，Vue 自动转义，
// 那两步在结构上不再需要。
//
// 空态（.task-empty）也搬进了组件的模板：数组为空时它自己渲染。
function renderHistoryTable(tasks) {
    if (!window.TaskStore) return;
    window.TaskStore.replaceAll(tasks);
}

// 行1 的元信息（#类型:id 之后）：地图/等高线是「样式 缩放」，高程是数据源名，
// 本地高程切片固定文案。返回值会经 escapeHtml 进模板。
function historyMetaText(task) {
    if (task.task_type === 'map' || task.task_type === 'contour') {
        const styleText = task.task_type === 'contour'
            ? t('js.history.style.contour')
            : (task.style ? getStyleText(task.style) : '');
        const zoom = (task.zoom_min != null && task.zoom_max != null)
            ? `${task.zoom_min}~${task.zoom_max}`
            : '';
        return [styleText, zoom].filter(Boolean).join(' ');
    }
    if (task.task_type === 'dem') {
        // 两个来源字段名不同：/api/history_all 的 UNION 把 dem_tasks.dataset
        // 映射进 style 列；tasks.js 实时任务对象带的是 dataset 原文。
        return task.style || task.dataset || t('js.history.meta.dem');
    }
    return t('js.history.meta.local_terrain');
}

// 行模板已迁到 static/js/task_list.js 的 TaskRow 组件（Vue）。
//
// 这里曾是全站唯一的行实现（一个返回 170 行模板字符串的 createTaskRow），
// tasks.js 的实时更新靠 outerHTML 整行调它重建。迁走的直接收益：
//   1. 转义不再靠人。改造前行1 有 5 处必须手写 escapeHtml(...)，漏一处就是
//      一个 XSS 注入面；组件全走 `{{ }}`，Vue 自动转义。
//   2. 失败行的 error_message 不用再走「渲染空容器 → 事后 textContent 补」
//      这条两步路（它存在的唯一理由就是不能进 innerHTML）。
//   3. 增量更新不用再手写。改造前 tasks.js 要自己判断「状态变了就整行
//      outerHTML 重建、只是进度变了就 querySelector 逐节点写」，并手写脏
//      检查避免高频事件下白付 layout；现在这是 Vue diff 的份内事。

function renderPagination(currentPage, totalPages) {
    const pagination = document.getElementById('pagination');

    // 只有一页（或没有数据）时不渲染分页条：孤零零一个「1」按钮没有
    // 交互价值，是纯视觉噪音（2026-08 实测反馈）。
    if (totalPages <= 1) {
        pagination.innerHTML = '';
        return;
    }

    let html = '';

    if (currentPage > 1) {
        html += `<li class="page-item"><a class="page-link" href="#" onclick="loadHistory(${currentPage - 1}); return false;">${t('js.history.pagination.prev')}</a></li>`;
    }

    for (let i = Math.max(1, currentPage - 2); i <= Math.min(totalPages, currentPage + 2); i++) {
        html += `<li class="page-item ${i === currentPage ? 'active' : ''}">
            <a class="page-link" href="#" onclick="loadHistory(${i}); return false;">${i}</a>
        </li>`;
    }

    if (currentPage < totalPages) {
        html += `<li class="page-item"><a class="page-link" href="#" onclick="loadHistory(${currentPage + 1}); return false;">${t('js.history.pagination.next')}</a></li>`;
    }

    pagination.innerHTML = html;
}

function renderHistoryMap(tasks) {
    if (!historyViewer) return;
    historyViewer.entities.removeAll();

    // Local-terrain tasks have no bbox; only map/dem tasks appear on the map.
    const geoTasks = tasks.filter(t => t.north != null && t.south != null && t.east != null && t.west != null);
    let west = Infinity, south = Infinity, east = -Infinity, north = -Infinity;

    geoTasks.forEach(task => {
        const color = Cesium.Color.fromCssColorString(getStatusStroke(task.status));
        west = Math.min(west, task.west);
        south = Math.min(south, task.south);
        east = Math.max(east, task.east);
        north = Math.max(north, task.north);

        historyViewer.entities.add({
            rectangle: {
                coordinates: Cesium.Rectangle.fromDegrees(task.west, task.south, task.east, task.north),
                material: color.withAlpha(0.15),
                outline: true,
                outlineColor: color,
                outlineWidth: 3,
            },
            name: task.name,
            description: `
                <strong style="color: var(--color-accent-hover); font-size: 1.05rem;">${escapeHtml(task.name)}</strong><br>
                <strong>${t('js.history.map.status_label')}</strong> ${escapeHtml(getStatusText(task.status))}<br>
                <strong>${task.task_type === 'dem' ? t('js.history.map.files_label') : t('js.history.map.tiles_label')}:</strong>
                <span style="font-family: var(--font-mono);">${task.downloaded}/${task.total}</span>
            `,
        });
    });

    if (geoTasks.length > 0) {
        const padLng = Math.max((east - west) * 0.1, 0.01);
        const padLat = Math.max((north - south) * 0.1, 0.01);
        historyViewer.camera.setView({
            destination: Cesium.Rectangle.fromDegrees(
                west - padLng, south - padLat, east + padLng, north + padLat),
        });
    }
    // requestRenderMode 下实体增删/setView 不一定自动触发重绘，显式请求一帧
    historyViewer.scene.requestRender();
}

// 客户端搜索：只写 store 的 searchTerm，过滤由组件的 visibleTasks 派生。
// 改造前是 renderHistoryTable(filtered) —— 拿过滤结果**覆盖整个列表**，
// 于是搜索期间到达的 socket 增量会写进一个已经被顶掉的数组，清空搜索框
// 才看得到。现在数据真相始终完整。
function filterTasks(searchTerm) {
    if (window.TaskStore) window.TaskStore.setSearchTerm(searchTerm);
}

// A7 / Task 12：这两张表原先只映射 completed / failed / cancelled 三态。
// （cancelled 已随「取消任务」一并退出状态机，见 models/task.py 的 TaskStatus。）
// /api/history_all 默认不带 status 过滤（路由的 ?status= 是可选参数，
// 状态筛选 chips 选中时才传），pending / running / paused 的任务照样可能
// 进历史流（例如独立页 /history 全量渲染）。落在表外的状态会走
// `|| status` 兜底，把后端的**英文字面量**直接渲染进中文界面
// —— 这就是历史页里 `paused` 与「✓ 已完成」中英混杂的根源。
// 现在与 tasks.js 的同名函数逐字对齐，覆盖 models/task.py 的 TaskStatus 全部五态。
// 两份实现仍然重复（没有构建工具、没有 ES module，两个页面不会同时加载），
// 收敛到公共文件属于第三档，本次只对齐行为。
function getStatusColor(status) {
    const colors = {
        'pending': 'secondary',
        // running 用 'info' 而不是 'primary'：`.status-badge.running /
        // .badge.bg-primary / .badge.bg-info` 是同一条声明块，渲染完全一致。
        'running': 'info',
        'paused': 'warning',
        'completed': 'success',
        'failed': 'danger'
    };
    return colors[status] || 'secondary';
}

// 历史地图上矩形的描边色。这是**第四处**状态映射点（前三处是 getStatusColor /
// getStatusText / statusIcons），A7 / Task 12 一并补齐。
//
// 改前是内联三元阶梯，只认 completed / failed，其余三态（pending / running /
// paused）全折叠成同一个蓝色 —— 与徽章那三张表是完全同型的缺陷。
// 而且三个色号 #10b981 / #ef4444 / #60a5fa 是**硬编码且离调色板**的：
// #10b981 是 emerald-500，本项目的 --color-success 是 emerald-400 #34d399，
// 改调色板时这里会静默漂移。
//
// 现在读 CSS 自定义属性，与徽章/进度条/卡片边条走同一套语义令牌：
//   pending -> --color-text-secondary（与 .badge.bg-secondary 同色）
// Leaflet 要的是真实色值字符串，不认 var()，所以必须在这里求值。
// 状态色惰性缓存：getComputedStyle 每次调用都强制样式计算，renderHistoryMap
// 逐任务调用时成本放大；调色板运行期不变，首次调用把 6 个令牌求值后查表。
// 缓存按令牌名键控、放模块级变量：history.js 在独立页和首页都会加载，
// 但同一页面只加载一次。
let _statusStrokeCache = null;

// U5：主题切换后缓存必须失效并重画 —— 缓存的前提「调色板运行期不变」在
// 主题开关落地后已经不成立（亮色块覆盖了这 6 个令牌全部）。getStatusStroke
// 只在 renderHistoryMap 里被调用，切主题本身不会触发重渲染，所以要显式重画。
document.addEventListener('terraforge:themechange', function () {
    _statusStrokeCache = null;
    if (typeof allTasks !== 'undefined' && Array.isArray(allTasks) && allTasks.length) {
        try { renderHistoryMap(allTasks); } catch (e) { /* 地图未就绪时忽略 */ }
    }
});

function getStatusStroke(status) {
    const vars = {
        'pending': '--color-text-secondary',
        'running': '--color-info',
        'paused': '--color-warning',
        'completed': '--color-success',
        'failed': '--color-danger'
    };
    const name = vars[status] || '--color-text-secondary';
    if (!_statusStrokeCache) {
        const style = getComputedStyle(document.documentElement);
        _statusStrokeCache = {};
        Object.keys(vars).forEach(function (key) {
            const token = vars[key];
            _statusStrokeCache[token] = style.getPropertyValue(token).trim();
        });
    }
    return _statusStrokeCache[name];
}

function getStatusText(status) {
    const texts = {
        'pending': t('js.history.status.pending'),
        'running': t('js.history.status.running'),
        'paused': t('js.history.status.paused'),
        'completed': t('js.history.status.completed'),
        'failed': t('js.history.status.failed')
    };
    // 未知状态不把英文字面量原样渲染进中文界面（与 tasks.js 同一约定）
    return texts[status] || t('js.history.status.unknown');
}

function getStyleText(style) {
    const styles = {
        'roadmap': t('js.history.style.roadmap'),
        'satellite': t('js.history.style.satellite'),
        'hybrid': t('js.history.style.hybrid'),
        'terrain': t('js.history.style.terrain'),
        // 兼容旧的缩写格式
        'm': t('js.history.style.m'),
        's': t('js.history.style.s'),
        'y': t('js.history.style.y'),
        'h': t('js.history.style.h'),
        't': t('js.history.style.t'),
        'contour': t('js.history.style.contour')
    };
    return styles[style] || style;
}

function formatDate(dateStr) {
    const date = parseTaskDate(dateStr);
    return date ? date.toLocaleString('zh-CN') : '-';
}

// 时间流行1 右侧的终态时间：创建时间的短日期（M-D HH:mm）。
// 列表按创建时间倒序，展示创建时间才自洽；完整创建/开始/完成时间
// 都在详情模态里。手写格式化而不是 toLocaleString：后者在 zh-CN 下
// 带年份与秒，行1 右端放不下。
function formatShortDate(dateStr) {
    const date = parseTaskDate(dateStr);
    if (!date) return '-';
    const pad = n => String(n).padStart(2, '0');
    return `${date.getMonth() + 1}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

async function viewTaskDetails(taskId, taskType = 'map') {
    try {
        const url = taskType === 'dem' ? `/api/dem/tasks/${taskId}`
                  : taskType === 'local_terrain' ? `/api/terrain/local/tasks/${taskId}`
                  : taskType === 'contour' ? `/api/contour/tasks/${taskId}`
                  : `/api/tasks/${taskId}`;
        const response = await fetch(url);
        const data = await response.json();
        const task = data.task;

        // 填充模态框数据
        document.getElementById('detailId').textContent = task.id;
        document.getElementById('detailName').textContent = task.name;
        document.getElementById('detailStatus').innerHTML = `<span class="badge bg-${getStatusColor(task.status)}">${escapeHtml(getStatusText(task.status))}</span>`;
        if (taskType === 'dem') {
            document.getElementById('detailStyle').textContent = task.dataset || 'ASTGTM.003';
            document.getElementById('detailFormat').textContent = '-';
            document.getElementById('detailZoom').textContent = '-';
            document.getElementById('detailTotal').textContent = task.total_files;
            document.getElementById('detailDownloaded').textContent = task.downloaded_files;
            document.getElementById('detailFailed').textContent = task.failed_files;
        } else if (taskType === 'local_terrain') {
            document.getElementById('detailStyle').textContent = t('js.history.meta.local_terrain');
            document.getElementById('detailFormat').textContent = '-';
            document.getElementById('detailZoom').textContent = `0 - ${task.maxzoom}`;
            document.getElementById('detailTotal').textContent = task.total_files;
            document.getElementById('detailDownloaded').textContent = task.uploaded_files;
            document.getElementById('detailFailed').textContent = task.failed_files;
        } else if (taskType === 'contour') {
            document.getElementById('detailStyle').textContent = t('js.history.detail.contour_tiles');
            document.getElementById('detailFormat').textContent = '-';
            document.getElementById('detailZoom').textContent = `${task.zoom_min} - ${task.zoom_max}`;
            document.getElementById('detailTotal').textContent = task.total_tiles;
            document.getElementById('detailDownloaded').textContent = task.rendered_tiles;
            document.getElementById('detailFailed').textContent = task.failed_tiles;
        } else {
            document.getElementById('detailStyle').textContent = getStyleText(task.style);
            document.getElementById('detailFormat').textContent = task.output_format;
            document.getElementById('detailZoom').textContent = `${task.zoom_min} - ${task.zoom_max}`;
            document.getElementById('detailTotal').textContent = task.total_tiles;
            document.getElementById('detailDownloaded').textContent = task.downloaded_tiles;
            document.getElementById('detailFailed').textContent = task.failed_tiles;
        }

        const total = taskType === 'dem' ? (task.total_files || 0)
                    : taskType === 'local_terrain' ? (task.total_files || 0)
                    : (task.total_tiles || 0);
        const done = taskType === 'dem' ? (task.downloaded_files || 0)
                   : taskType === 'local_terrain' ? (task.uploaded_files || 0)
                   : taskType === 'contour' ? (task.rendered_tiles || 0)
                   : (task.downloaded_tiles || 0);
        const progress = total > 0
            ? Math.round((done / total) * 100)
            : 0;

        document.getElementById('detailProgress').innerHTML = `
            <div class="progress" style="margin-top: 0.5rem;">
                <div class="progress-bar bg-${getStatusColor(task.status)}" role="progressbar"
                     style="width: ${progress}%"
                     aria-valuenow="${progress}"
                     aria-valuemin="0"
                     aria-valuemax="100"></div>
                <span class="progress__label" aria-hidden="true">${progress}%</span>
            </div>
        `;

        // Local-terrain tasks have no bounding box.
        const hasBbox = task.north != null && task.south != null && task.east != null && task.west != null;
        document.getElementById('detailNorth').textContent = hasBbox ? task.north.toFixed(6) : '-';
        document.getElementById('detailSouth').textContent = hasBbox ? task.south.toFixed(6) : '-';
        document.getElementById('detailEast').textContent = hasBbox ? task.east.toFixed(6) : '-';
        document.getElementById('detailWest').textContent = hasBbox ? task.west.toFixed(6) : '-';

        document.getElementById('detailPath').textContent = task.output_path;
        document.getElementById('detailCreated').textContent = formatDate(task.created_at);
        document.getElementById('detailStarted').textContent = formatDate(task.started_at);
        document.getElementById('detailCompleted').textContent = formatDate(task.completed_at);

        // 显示错误信息（如果有）
        if (task.error_message) {
            document.getElementById('detailError').textContent = task.error_message;
            document.getElementById('detailErrorRow').style.display = 'block';
        } else {
            document.getElementById('detailErrorRow').style.display = 'none';
        }

        // DEM: 地形切片入口
        const terrainRow = document.getElementById('detailTerrainRow');
        if (taskType === 'dem') {
            terrainRow.style.display = 'block';
            initTerrainDetailActions(taskId);
            await refreshTerrainDetail(taskId);
        } else {
            terrainRow.style.display = 'none';
        }

        // 显示模态框。getOrCreateInstance 与全站一致：重复 new bootstrap.Modal
        // 同一元素会叠出多个实例（每次打开多一层遮罩，关一层还剩一层）。
        bootstrap.Modal.getOrCreateInstance(document.getElementById('taskDetailModal')).show();
    } catch (error) {
        showToast(t('js.history.detail.load_failed'), 'danger');
    }
}

function initTerrainDetailActions(taskId) {
    const startBtn = document.getElementById('detailTerrainStartBtn');
    const refreshBtn = document.getElementById('detailTerrainRefreshBtn');

    startBtn.onclick = async () => {
        startBtn.disabled = true;
        try {
            const r = await fetch(`/api/terrain/dem/${taskId}/start`, { method: 'POST' });
            const j = await r.json().catch(() => ({}));
            if (!r.ok) {
                throw new Error(j.error || t('js.history.terrain.start_failed'));
            }
        } catch (e) {
            showToast(String(e.message || e), 'danger');
        } finally {
            startBtn.disabled = false;
            await refreshTerrainDetail(taskId);
        }
    };

    refreshBtn.onclick = async () => {
        refreshBtn.disabled = true;
        try {
            await refreshTerrainDetail(taskId);
        } finally {
            refreshBtn.disabled = false;
        }
    };
}

async function refreshTerrainDetail(taskId) {
    const statusEl = document.getElementById('detailTerrainStatus');
    const infoEl = document.getElementById('detailTerrainInfo');
    const errRow = document.getElementById('detailTerrainErrorRow');
    const errEl = document.getElementById('detailTerrainError');

    errRow.style.display = 'none';
    errEl.textContent = '';

    // 固定 URL 约定（后端 terrain_static_bp）
    const baseUrl = `${location.origin}/terrain/base/layer.json`;
    const localUrl = `${location.origin}/terrain/dem/${taskId}/layer.json`;

    try {
        const r = await fetch(`/api/terrain/dem/${taskId}`);
        const j = await r.json();
        const job = j.job;

        if (!job) {
            statusEl.innerHTML = `<span class="badge bg-secondary">${t('js.history.terrain.not_started')}</span>`;
            infoEl.innerHTML = `
                <div>Base: <a href="${baseUrl}" target="_blank" rel="noopener noreferrer">${baseUrl}</a></div>
                <div>Local: <a href="${localUrl}" target="_blank" rel="noopener noreferrer">${localUrl}</a></div>
            `;
            return;
        }

        // A7 / Task 12：地形切片作业的状态词表（running / completed / failed）
        // 是任务状态的子集，直接复用上面两个函数，不再写一份内联三元阶梯 ——
        // 原来这里把 `job.status` **原样**插进徽章，中文界面里显示英文
        // `running`，和历史表格的老毛病是同一个。
        const status = job.status || 'unknown';
        const label = escapeHtml(status === 'unknown' ? t('js.history.terrain.status_unknown') : getStatusText(status));
        statusEl.innerHTML = `<span class="badge bg-${getStatusColor(status)}">${label}</span>`;

        const outDir = job.output_dir || '-';
        const maxzoom = job.maxzoom ?? '-';
        infoEl.innerHTML = `
            <div>MaxZoom: ${maxzoom}</div>
            <div>Out: ${escapeHtml(outDir)}</div>
            <div>Base: <a href="${baseUrl}" target="_blank" rel="noopener noreferrer">${baseUrl}</a></div>
            <div>Local: <a href="${localUrl}" target="_blank" rel="noopener noreferrer">${localUrl}</a></div>
        `;

        if (job.error_message) {
            errEl.textContent = job.error_message;
            errRow.style.display = 'block';
        }
    } catch (e) {
        statusEl.innerHTML = `<span class="badge bg-danger">${t('js.history.terrain.load_failed')}</span>`;
        errEl.textContent = String(e.message || e);
        errRow.style.display = 'block';
        infoEl.innerHTML = `
            <div>Base: <a href="${baseUrl}" target="_blank" rel="noopener noreferrer">${baseUrl}</a></div>
            <div>Local: <a href="${localUrl}" target="_blank" rel="noopener noreferrer">${localUrl}</a></div>
        `;
    }
}

// 「在地图上预览」：把任务的可视化输出叠加到主视图（map.js 的 previewTask），
// 并关掉历史面板让用户直接看到。数据来自当前页已加载的行（含 bbox / zoom）。
function previewHistoryTask(taskId, taskType) {
    // 必须读 store,不能读 allTasks —— allTasks 只是 loadHistory 的响应快照,
    // socket 增量(tasks.js prependStreamRow)插进来的新行只进了 store,不在
    // 这个数组里。对那些行点「预览」会走进下面的 `if (!task) return`,按钮
    // 看起来就是坏的(无提示、无动作)。store.state.tasks 是渲染的真相,且
    // 是 allTasks 的超集 —— renderHistoryTable 就是 replaceAll 的包装(:169)。
    const store = window.TaskStore;
    const task = store && store.get(`${taskType}:${taskId}`);
    if (!task) return;
    previewTask(Object.assign({}, task, { task_type: taskType }));
    if (typeof closePanel === 'function') closePanel();
}

async function deleteTask(taskId, taskType = 'map') {
    if (!await showConfirm(t('js.history.confirm.delete_task'), { title: t('js.history.confirm.delete_task_title'), danger: true })) {
        return;
    }

    // 第二步确认：是否连磁盘上的下载产物一起删。后端 DELETE 端点按
    // ?delete_files=true/false 决定是否清理产物目录，缺省 false（保留）。
    const deleteFiles = await showConfirm(t('js.history.confirm.delete_files'), {
        title: t('js.history.confirm.delete_files_title'),
        confirmText: t('js.history.confirm.delete_files_confirm'),
        cancelText: t('js.history.confirm.delete_files_cancel'),
        danger: true
    });

    try {
        const deleteUrl = taskType === 'dem' ? `/api/dem/tasks/${taskId}`
                        : taskType === 'local_terrain' ? `/api/terrain/local/tasks/${taskId}`
                        : taskType === 'contour' ? `/api/contour/tasks/${taskId}`
                        : `/api/tasks/${taskId}`;
        const response = await fetch(`${deleteUrl}?delete_files=${deleteFiles ? 'true' : 'false'}`, { method: 'DELETE' });

        if (response.ok) {
            showToast(t('js.history.toast.deleted'), 'success');
            // 预览中的正是被删任务时联动关闭（map.js 的预览管理器）；
            // 独立页 /history 不加载 map.js，typeof 守卫兜底。
            if (typeof stopTaskPreviewForTask === 'function') {
                stopTaskPreviewForTask(taskType, taskId);
            }
            // 删掉的是失败任务时，它那条常驻失败 toast（tasks.js 按 key 合并的）
            // 一并关掉——记录都没了，提示不该留在右上角。
            // 独立页 /history 不加载 tasks.js，typeof 守卫兜底。
            if (typeof closeFailureToast === 'function') {
                closeFailureToast(`${taskType}:${taskId}`);
            }
            // L6：删掉任意活动任务后（四条 DELETE 端点现在连 running 也收 ——
            // 置停止标志后当场删行，见 routes/api.py 的 delete_task），
            // 底部状态栏「N 个活动任务（M 运行中）X%」会继续把它算进去 ——
            // loadHistory 不调 updateStatusTasks，文本就原地冻结，唯一纠正点是
            // loadActiveTasks 里的 setActive（只在新建任务、socket 断线重连
            // 或整页刷新时发生）。独立页 /history 不加载 tasks.js，用 typeof
            // 守卫兜底（与上面两处同一写法）。
            if (window.TaskStore) {
                window.TaskStore.remove(`${taskType}:${taskId}`);
            }
            if (typeof updateStatusTasks === 'function') {
                updateStatusTasks();
            }
            // 删掉的是当前页最后一条时本页已空，停在原页会看到空白页——回退一页
            if (allTasks.length <= 1 && currentPage > 1) {
                loadHistory(currentPage - 1);
            } else {
                loadHistory(currentPage);
            }
            loadStats();
        } else {
            showToast(t('js.history.toast.delete_failed'), 'danger');
        }
    } catch (error) {
        showToast(t('js.history.toast.delete_failed_reason', {error: error.message}), 'danger');
    }
}
