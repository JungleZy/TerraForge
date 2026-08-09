let historyViewer;
let currentPage = 1;
// 状态筛选 chips 的当前取值（'' = 全部，'active' = 进行中三态）。
// 作用于整个时间流：透传给 /api/history_all 的 ?status= 参数（后端已支持）。
let currentStatusFilter = '';

async function initHistory() {
    // 时间流的渲染层（Vue）挂到 #historyTableBody。幂等，首页由 initTasks
    // 也调一次——两个入口哪个先跑都行。必须在 loadHistory 之前：晚于它的话
    // 首屏那批数据写进 store 时还没有消费者。
    if (window.TaskList) window.TaskList.mount();

    // 接线必须排在下面那句 await 之前：await 会把本函数挂起一次
    // /api/basemap 往返，期间搜索框和状态 chip 不能是死的。
    document.getElementById('searchInput').addEventListener('input', function(e) {
        filterTasks(e.target.value);
    });

    // 状态筛选 chips（2026-08 单一时间流定稿）：全部/进行中/失败/已完成。
    // 作用于整个时间流（活动任务也在流里）：取值透传给 /api/history_all
    // 的 ?status= 参数，「进行中」对应特殊值 active（pending/running/paused）。
    document.querySelectorAll('#statusChips .status-chip').forEach(function(chip) {
        chip.addEventListener('click', function() {
            document.querySelectorAll('#statusChips .status-chip').forEach(function(c) {
                c.classList.remove('active');
                // aria-pressed 与 .active 必须同步翻：只有 CSS class 时读屏
                // 用户听不出当前选中的是哪一档筛选（map.js 的 .map-panel-btn
                // 已经是这个写法）。
                c.setAttribute('aria-pressed', 'false');
            });
            this.classList.add('active');
            this.setAttribute('aria-pressed', 'true');
            currentStatusFilter = this.dataset.status || '';
            loadHistory(1);
        });
    });

    loadStats();

    // 必须 await：initHistoryMap 在 await _resolveHistoryBasemap() 处挂起，
    // 之后才给 historyViewer 赋值。不 await 时 loadHistory 往往先跑完，
    // renderHistoryMap 撞上 `if (!historyViewer) return` 空转 —— 独立
    // /history 页没有任何重渲染入口（不加载 tasks.js、无面板重开钩子），
    // 小地图会一直空白到用户点 chip / 翻页 / 删除为止。
    // 用 await 而不是「在 initHistoryMap 末尾补一次 renderHistoryMap」：后者
    // 在地图先就绪的那条时序上会白渲染一次空 store，await 则不可能重复渲染。
    // 异常就地吞掉再继续：Cesium 起不来是小地图一个人的事，不能连表格一起
    // 赔进去（history.html 外面那层 try/catch 也接不住 async 函数的 rejection）。
    try {
        await initHistoryMap();
    } catch (e) {
        console.error('Failed to init history map:', e);
    }
    loadHistory(1);
}

// 历史小地图与主视图共用**服务端解析**的底图描述符
// （src/services/basemap_source.py -> client_descriptor）：url 永远是同源的
// /basemap/{z}/{x}/{y}，真实上游地址不出服务端。
//
// 这里曾有一份 _historyBaseMapUrl 平行实现，在浏览器侧自己拼地址：拿不到
// 配置回退外网 OSM、拿到主机别名拼 `//host/vt?lyrs=m`。三条硬约束一次全破：
//   1. 离线 —— 断网/内网部署时小地图必白屏；
//   2. 同源代理 —— 浏览器直连上游会撞 CORS（上游 4xx 的错误页不带 CORS 头，
//      真实状态码被埋成一句 CORS 报错），而且浏览器**不吃** proxy_url，
//      底图与下载走两条不同出网路径，配好代理底图照样可能是蓝球；
//   3. WGS-84 —— lyrs=m 是路网图，中国区为 GCJ-02 偏移，而叠在上面的任务
//      矩形是 WGS-84 坐标，国内区域必然错位（项目只允许 lyrs=s）。
// 署名也曾写死 © OpenStreetMap 而实际加载的是 Google 瓦片。
const HISTORY_BASEMAP_FALLBACK = { url: '/basemap/{z}/{x}/{y}', max_level: 19, credit: '' };

async function _resolveHistoryBasemap() {
    // 一律走 /api/basemap 取服务端解析的同一份描述符。
    //
    // 这里曾有一条 `typeof basemap !== 'undefined'` 的「首页免一次请求」快
    // 路径，它在**每个页面**都是死的：唯一的生产者 index.html 是把描述符当
    // 实参传进 initMap(config, basemap) 的，函数参数不是全局，那个 typeof
    // 永远等于 'undefined'。而它注释里承诺省下的那次往返，正是 initHistory
    // 里竞态的成因。删掉而不是让服务端补一个真全局：补全局等于给同一份数据
    // 开第二个出口（模板内联 + 接口），而这一次同源请求现在已经被
    // initHistory 的 await 挡在竞态之外，没什么可省的了。
    try {
        const r = await fetch('/api/basemap', { cache: 'no-store' });
        const j = await r.json();
        if (j && j.success && j.basemap && j.basemap.url) return j.basemap;
    } catch (e) {
        console.error('Failed to load basemap descriptor:', e);
    }
    // 接口失败也只回退到同源路径：瓦片能不能取到是服务端的事，前端不该
    // 因为一次接口失败就绕过代理去直连外网。
    return HISTORY_BASEMAP_FALLBACK;
}

async function initHistoryMap() {
    // 历史区域小地图：Cesium 只读视图（地图系统已从 Leaflet 切到 CesiumJS）
    const bm = await _resolveHistoryBasemap();
    historyViewer = new Cesium.Viewer('historyMap', {
        baseLayer: new Cesium.ImageryLayer(new Cesium.UrlTemplateImageryProvider({
            url: bm.url,
            tilingScheme: new Cesium.WebMercatorTilingScheme(),
            // 超出这一层上游返回 404，Cesium 画成空白。与主视图同一口径。
            maximumLevel: bm.max_level == null ? undefined : bm.max_level,
            credit: bm.credit || '',
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
        // 逐个判空：统计卡是否在页面上取决于 _history_content.html 有没有被
        // include。缺元素时裸解引用会抛 TypeError，被下面的 catch 吞成一条
        // console.error —— 每个终态事件刷一条，而统计卡静默不更新。
        const set = function (id, value) {
            const el = document.getElementById(id);
            if (el) el.textContent = value;
        };
        set('statTotal', s.total_tasks);
        set('statCompleted', s.completed);
        set('statFailed', s.failed);
        set('statDownloaded', s.total_downloaded.toLocaleString());
    } catch (e) {
        console.error('Failed to load stats:', e);
    }
}

// L7：请求序号。状态筛选 chip 连点（无防抖、无禁用、无 in-flight 标志）时，
// 先发的响应可能后返回 —— chip 高亮与 currentStatusFilter 已是新值，表格和
// store 却是旧筛选集合。currentPage 的赋值也必须挪到守卫之后：它是
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
        renderHistoryTable(data.tasks || []);
        const p = data.pagination || {};
        renderPagination(p.page || 1, p.total_pages || 1);
        renderHistoryMap();
    } catch (error) {
        if (seq !== _historyReqSeq) return;   // 过期请求的错误不该覆盖新结果
        console.error('Failed to load history:', error);
        // 错误提示走 store，由组件渲染 —— #historyTableBody 已经被 Vue 接管，
        // 直接写它的 innerHTML 会在下一次 patch 时被覆盖掉。
        if (window.TaskStore) window.TaskStore.setLoadError(t('js.history.load_failed'));
    }
}

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

// 数据源是 store，不是 loadHistory 的响应快照：socket 增量（tasks.js）只
// 写 store，读快照会让运行中任务的矩形停在上一次拉取时的颜色，socket 新插
// 进来的行则连矩形都没有。previewHistoryTask / deleteTask 早就改读 store 了
// （见那两处的说明），这里是漏网的第三处。
function renderHistoryMap() {
    if (!historyViewer) return;
    const store = window.TaskStore;
    const tasks = store ? store.state.tasks : [];
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

// getStatusColor / getStatusText / getStatusStroke（以及主题切换时的描边色
// 缓存失效）已收口到 static/js/task_status.js，全站唯一一份、base.html 全局
// 加载。这里曾有一份与 tasks.js 并行的实现：两者在首页共享全局作用域，后
// 加载的本文件静默遮蔽 tasks.js 那份，而两份 getStatusText 查的是不同的
// i18n key 前缀 —— 详见那个文件开头的说明。

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
    // 查表走 hasOwnProperty，与 terrainPresetRowsHtml / confirmDelete 的两张表
    // 同一条约定：对象字面量继承 Object.prototype，style === 'constructor' 时
    // 裸下标取到的是构造函数本身（真值，`||` 兜底不了），这一格就渲染成
    // `function Object() { [native code] }` —— 一段函数源码冒充样式名。
    return (Object.prototype.hasOwnProperty.call(styles, style) && styles[style]) || style;
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
            // 实际切到的最深层级优先。`0 - N` 是一个自称精确的范围，而 maxzoom
            // 那一列存的是用户填的**基准**层级 —— 精细/快速两档下它与产物实际的
            // 最深层级差一级，直接显示就是错数字（precision 档写 0 - 14、
            // layer.json 里是 15）。切完之前 effective_maxzoom 为 NULL，只能回落
            // 到基准值 —— 那就必须让**文字本身**说出这是基准值。
            //
            // 这一格的标签写死在 templates/base.html 里（DEM 侧是自己拼的 HTML，
            // 可以像 terrainMaxzoomRowHtml 那样换标签，这里换不了），所以把限定词
            // 缀在值后面。只靠下面那句 title 不够：悬停在触摸设备上根本不存在、
            // 键盘也够不着，而且 `0 - 14` 与 `0 - 14` 长得一模一样，用户连
            // 「这里有话要说」都看不出来。
            const localTerrainActualMaxzoom = task.effective_maxzoom;
            document.getElementById('detailZoom').textContent =
                localTerrainActualMaxzoom != null
                    ? `0 - ${localTerrainActualMaxzoom}`
                    : `0 - ${task.maxzoom} (${t('js.history.terrain.maxzoom_base_label')})`;
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
        // 上一格只有本地地形会带「这是基准值、不是实际切到的层级」那句悬停说明。
        // 其余分支必须显式清空：模态复用同一个 DOM，不清的话上一个任务留下的
        // 提示会粘在下一个任务身上。
        document.getElementById('detailZoom').title =
            (taskType === 'local_terrain' && task.effective_maxzoom == null)
                ? t('js.history.terrain.maxzoom_base_hint')
                : '';

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
            document.getElementById('detailErrorRow').hidden = false;
        } else {
            document.getElementById('detailErrorRow').hidden = true;
        }

        // 地形切片信息区。DEM 是一条独立的切片作业（要拉 /api/terrain/dem/<id>，
        // 还带起切/刷新按钮）；本地地形没有独立作业行 —— 切片状态就是任务状态、
        // 上面已经显示过，也没有起切端点，所以只借这块地方回显档位与法线，
        // 数据直接取自 task（local_terrain_tasks 行本身就带这两列），不再发请求。
        const terrainRow = document.getElementById('detailTerrainRow');
        const terrainActions = document.getElementById('detailTerrainActions');
        if (taskType === 'dem') {
            terrainRow.hidden = false;
            terrainActions.hidden = false;
            initTerrainDetailActions(taskId);
            await refreshTerrainDetail(taskId);
        } else if (taskType === 'local_terrain') {
            terrainRow.hidden = false;
            terrainActions.hidden = true;
            document.getElementById('detailTerrainStatus').textContent = '';
            document.getElementById('detailTerrainErrorRow').hidden = true;
            document.getElementById('detailTerrainInfo').innerHTML =
                terrainPresetRowsHtml(task);
        } else {
            terrainRow.hidden = true;
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

// 后端存的档位是枚举字面量（geo_validation.TILING_QUALITY_OFFSETS 的键）。
// 这里逐档写成完整的键字面量、不做字符串拼接：tests/test_i18n.py 的双向闭合
// 是按「key 形状的字面量」扫源码的，拼出来的键会被当成无人引用而判死。
const TERRAIN_QUALITY_KEYS = {
    precision: 'js.history.terrain.quality_precision',
    balanced: 'js.history.terrain.quality_balanced',
    speed: 'js.history.terrain.quality_speed',
};

// 「层级」那一行。显示的必须是**产物事实**：effective_maxzoom 是 build_terrain
// 回报、切完才落库的实际最深层级，与 layer.json 的 maxzoom 同源；maxzoom 那一列
// 存的是用户填的基准层级，精细/快速两档下两者差一级。此前面板只有后者，于是
// precision 档的作业面板写 12、layer.json 里却是 13。
// 为 NULL（存量行 / 还没切完）时回落到基准值，但**换一个标签并挂上说明**，
// 不冒充实际值 —— 后端同理不能拿 0 当「未知」，0 是合法层级。
function terrainMaxzoomRowHtml(row) {
    const actual = row.effective_maxzoom;
    if (actual != null) {
        return `<div>${t('js.history.terrain.maxzoom_actual_label')}: `
            + `${escapeHtml(String(actual))}</div>`;
    }
    return `<div title="${escapeHtml(t('js.history.terrain.maxzoom_base_hint'))}">`
        + `${t('js.history.terrain.maxzoom_base_label')}: `
        + `${escapeHtml(String(row.maxzoom ?? '-'))}</div>`;
}

// 档位与法线两行，DEM 切片作业（dem_terrain_jobs 行）与本地地形任务
// （local_terrain_tasks 行）共用 —— 两张表的 quality / vertex_normals 同名同义。
// 两边都必须回显，理由各不相同：DEM 面板的起切按钮 POST 不带 body、走配置默认，
// 用户在那里没有选择权；本地地形则恰恰相反，上传表单是用户**唯一能亲手选档位**
// 的入口，几十分钟切完回来查不到自己当时选了什么更说不过去。
function terrainPresetRowsHtml(row) {
    // 查表必须走 hasOwnProperty：对象字面量继承 Object.prototype，
    // `constructor` / `__proto__` / `toString` 这几个值会命中原型上的成员、
    // 被当成「认得出的档位」，最后把 `function Object() { [native code] }`
    // 绕过下面那道 escapeHtml 插进界面。正常写入路径上 validate_tiling_quality
    // 挡得住，但「认不出的值原样显示」这条契约不该只对好输入成立。
    const qualityKey = Object.prototype.hasOwnProperty.call(TERRAIN_QUALITY_KEYS, row.quality)
        ? TERRAIN_QUALITY_KEYS[row.quality]
        : null;
    // 认不出的值（旧作业 / 手改过库）宁可原样显示，也好过悄悄说成「均衡」。
    const quality = qualityKey ? t(qualityKey) : escapeHtml(String(row.quality ?? '-'));
    // vertex_normals 是**三态**，不是布尔：NULL = 这一行没有记录过法线状态
    // （列是后加的，加列之前切的作业一律为 NULL），0 = 明确关闭，1 = 明确开启。
    // 原来写的是 `!!row.vertex_normals`，NULL 被压成 false，面板于是用
    // 「未开启（无光照数据）」这种确定语气，去描述一件这一行根本没记录的事；
    // 而且方向恰好说反 —— 加这一列之前法线是默认开着的。宁可说「未知」，
    // 也不能给一个看起来确定的错值。
    const normalsRecorded = row.vertex_normals != null;
    const normalsOff = normalsRecorded && !row.vertex_normals;
    const normals = t(!normalsRecorded
        ? 'js.history.terrain.normals_unknown'
        : normalsOff
            ? 'js.history.terrain.normals_off'
            : 'js.history.terrain.normals_on');
    // 关掉法线不是「少一个效果」：hasVertexNormals 是 provider 级的单一标志，
    // 这份地形没有法线，Cesium 的光照开关就对**整幅场景**退化成全球日夜渐变，
    // 随包底图自带的法线也一并作废。而且法线是烘焙进瓦片的，事后改配置救不回
    // 已经切完的产物 —— 用户在这里看到「未开启」时必须同时看到这个后果。
    // 只挂在**明确关闭**这一档：未知状态下挂上去，等于拿一件没记录的事吓用户。
    const normalsTitle = normalsOff
        ? ` title="${escapeHtml(t('js.history.terrain.normals_off_hint'))}"`
        : '';
    return `
            <div>${t('js.history.terrain.quality_label')}: ${quality}</div>
            <div${normalsTitle}>${t('js.history.terrain.normals_label')}: ${normals}</div>`;
}

async function refreshTerrainDetail(taskId) {
    const statusEl = document.getElementById('detailTerrainStatus');
    const infoEl = document.getElementById('detailTerrainInfo');
    const errRow = document.getElementById('detailTerrainErrorRow');
    const errEl = document.getElementById('detailTerrainError');

    errRow.hidden = true;
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
        infoEl.innerHTML = `
            ${terrainMaxzoomRowHtml(job)}
            ${terrainPresetRowsHtml(job)}
            <div>Out: ${escapeHtml(outDir)}</div>
            <div>Base: <a href="${baseUrl}" target="_blank" rel="noopener noreferrer">${baseUrl}</a></div>
            <div>Local: <a href="${localUrl}" target="_blank" rel="noopener noreferrer">${localUrl}</a></div>
        `;

        if (job.error_message) {
            errEl.textContent = job.error_message;
            errRow.hidden = false;
        }
    } catch (e) {
        statusEl.innerHTML = `<span class="badge bg-danger">${t('js.history.terrain.load_failed')}</span>`;
        errEl.textContent = String(e.message || e);
        errRow.hidden = false;
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
    // 是 allTasks 的超集 —— renderHistoryTable 就是 TaskStore.replaceAll 的包装。
    const store = window.TaskStore;
    const task = store && store.get(`${taskType}:${taskId}`);
    if (!task) return;
    previewTask(Object.assign({}, task, { task_type: taskType }));
    if (typeof closePanel === 'function') closePanel();
}

// 活动状态 -> 确认文案的键。终态（completed / failed）不在表里，走通用文案。
// 三态各说各的，不合并成一句「该任务尚未结束」：用户按下删除前要判断的是
// 「我会失去什么」，pending 什么都还没跑（只是排队），running / paused 有
// 已下载的进度会丢 —— 这三件事对决策的分量不一样。
const DELETE_CONFIRM_KEYS = {
    running: 'js.history.confirm.delete_task_running',
    pending: 'js.history.confirm.delete_task_pending',
    paused: 'js.history.confirm.delete_task_paused',
};

async function deleteTask(taskId, taskType = 'map') {
    // 状态取自 store 而不是 allTasks：socket 增量插进来的行只进了 store
    // （同 previewHistoryTask 的说明）。查不到就退回通用文案 —— 宁可少一句
    // 警告，也不能对着一个不知道状态的任务瞎说「正在运行」。
    const store = window.TaskStore;
    const task = store && store.get(`${taskType}:${taskId}`);
    // hasOwnProperty 同 refreshTerrainDetail 的档位查表：`task.status` 若是
    // `constructor` / `toString`，裸下标会取到原型上的成员并当成一个真键，
    // t() 拿到函数后原样回落，确认框上就是一坨 `function Object()...`。
    const confirmKey = (task
        && Object.prototype.hasOwnProperty.call(DELETE_CONFIRM_KEYS, task.status)
        && DELETE_CONFIRM_KEYS[task.status])
        || 'js.history.confirm.delete_task';

    // 单一确认框：任务删不删走确定/取消，产物删不删走勾选框（默认不勾）。
    // 原来是串起来的两个框，第二个框问的是产物 —— 它的取消位（ESC / 点遮罩 /
    // 「保留产物」）看起来像在撤销整个删除，实际上照样发 DELETE。现在取消就是
    // 取消：不发请求，什么都不做。
    const answer = await showConfirm(t(confirmKey), {
        title: t('js.history.confirm.delete_task_title'),
        danger: true,
        checkbox: {
            label: t('js.history.confirm.delete_files_checkbox'),
            checked: false,
        },
    });
    if (!answer.confirmed) {
        return;
    }
    const deleteFiles = answer.checked;

    try {
        const deleteUrl = taskType === 'dem' ? `/api/dem/tasks/${taskId}`
                        : taskType === 'local_terrain' ? `/api/terrain/local/tasks/${taskId}`
                        : taskType === 'contour' ? `/api/contour/tasks/${taskId}`
                        : `/api/tasks/${taskId}`;
        const response = await fetch(`${deleteUrl}?delete_files=${deleteFiles ? 'true' : 'false'}`, { method: 'DELETE' });

        if (response.ok) {
            // files_deferred 的语义是「有产物要延后删」，不是「任务在跑」
            // （后端判据是 artifact_dir is not None）。没要求删产物时这个字段
            // 根本不下发，所以只认「键为真」，不能写成 === false。
            // 不告诉用户的后果：删掉一个跑了两小时的任务并勾了删产物，看到
            // 「任务已删除」，转头去文件管理器发现几十 GB 还在 —— 他分不清
            // 该等还是该手删。
            let payload = null;
            try {
                payload = await response.json();
            } catch (e) {
                // 四条 DELETE 端点都回 JSON；解析不了也只是少一句提示，
                // 不能因此把后面的摘行/刷新整串跳过。
            }
            showToast(payload && payload.files_deferred
                ? t('js.history.toast.deleted_files_deferred')
                : t('js.history.toast.deleted'), 'success');
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
            // 删掉的是当前页最后一条时本页已空，停在原页会看到空白页——回退一页。
            // 判据是「删完就空了」而不是旧代码的 `<= 1`：上面的 store.remove 已经
            // 把这一行摘掉，这里读的是删除**后**的长度；旧代码读的 allTasks 是
            // loadHistory 的响应快照，删除不改它，所以那边才要留 1 条余量。
            const remaining = store ? store.state.tasks.length : 1;
            if (remaining === 0 && currentPage > 1) {
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
