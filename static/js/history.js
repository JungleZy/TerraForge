let historyViewer;
let currentPage = 1;
// 状态筛选 chips 的当前取值（'' = 全部）。作用于整个时间流：原样透传给
// /api/history_all 的 ?status= 参数。服务端只对两个取值做展开 ——
// 'active' = ACTIVE_STATE_VALUES 五态，'completed' = completed +
// completed_with_gaps；其余（'failed' / 'completed_with_gaps'）按精确等值。
// 前端**不复制**那两张展开表：复制出来的第二份迟早与后端漂移，而漂移的表现
// 是「点了 chip 少几行」，没有任何报错。
let currentStatusFilter = '';

async function initHistory() {
    // 时间流的渲染层（Vue）挂到 #historyTableBody。幂等，首页由 initTasks
    // 也调一次——两个入口哪个先跑都行。必须在 loadHistory 之前：晚于它的话
    // 首屏那批数据写进 store 时还没有消费者。
    if (window.TaskList) window.TaskList.mount();

    // 接线必须排在下面启动异步工作之前：底图描述符、健康探测或时间流任一请求
    // 挂起期间，搜索框和状态 chip 都不能是死的。
    document.getElementById('searchInput').addEventListener('input', function(e) {
        filterTasks(e.target.value);
    });

    // 状态筛选 chips：全部 / 进行中 / 失败 / 已完成 / 有缺块。取值原样透传给
    // /api/history_all 的 ?status=（展开规则在服务端，见文件头的说明）。
    // 「有缺块」与「已完成」故意重叠 —— 带洞的成品两边都能找到（§13-3）。
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

    // 地图（含底图描述符与瓦片健康探测）和首批时间流同时启动，避免探测期间
    // 统计卡与表格闲等。初次 loadHistory 禁止自行绘图；等 map + data 的共同
    // 屏障通过后统一补一次最终渲染，哪一边先完成都不会画空 store 或重复绘制。
    // 小地图起不来是它一个人的事，不能连表格一起赔进去 —— 但也**不能只写
    // console**：审查点名的正是这里，Cesium 起不来时页面上只有一块空白，
    // 用户看不出是坏了还是本来就没有区域。原因留给控制台，结论给用户一句。
    const mapReady = initHistoryMap().catch(function (error) {
        console.error('Failed to init history map:', error);
        showToast(t('js.history.map_failed'), 'warning');
    });
    const historyReady = loadHistory(1, false);
    await Promise.all([mapReady, historyReady]);
    try {
        renderHistoryMap();
    } catch (error) {
        console.error('Failed to render history map:', error);
        showToast(t('js.history.map_failed'), 'warning');
    }
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
    // 永远等于 'undefined'。删掉而不是让服务端补一个真全局：补全局等于给同一
    // 份数据开第二个出口（模板内联 + 接口）；现在这次请求与统计、时间流并行，
    // 不再阻塞数据首屏。
    try {
        const r = await fetch('/api/basemap', { cache: 'no-store' });
        const j = await r.json();
        if (j && j.success && j.basemap && j.basemap.url) return j.basemap;
    } catch (e) {
        console.error('Failed to load basemap descriptor:', e);
        // 回退能用，但**不能不说**：下面那份内置描述符的层级上限与署名都可能
        // 与配置里的源不同，小地图于是「看着正常、其实不是你配的那个源」。
        showToast(t('js.history.basemap_fallback'), 'warning');
    }
    // 接口失败也只回退到同源路径：瓦片能不能取到是服务端的事，前端不该
    // 因为一次接口失败就绕过代理去直连外网。
    return HISTORY_BASEMAP_FALLBACK;
}

async function initHistoryMap() {
    // 历史区域小地图：Cesium 只读视图（地图系统已从 Leaflet 切到 CesiumJS）。
    // 描述符确定后先完成瓦片源健康探测，再创建会立即发瓦片请求的 Viewer。
    const bm = await _resolveHistoryBasemap();
    await initTileOrigin(bm.tile_port);
    historyViewer = new Cesium.Viewer('historyMap', {
        baseLayer: new Cesium.ImageryLayer(new Cesium.UrlTemplateImageryProvider({
            // 与主视图同一口径：瓦片走页面级瓦片 origin（ui.js tileUrl /
            // src/core/tile_server.py）。origin 由上面那次 initTileOrigin()
            // 一次性定下，探测失败时它保留同源路径 —— 这里只交路径。
            url: tileUrl(bm.url),
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

// 统计卡失败只提示一次，成功后复位。loadStats 的调用者不止首屏：每个终态
// 事件都会（去抖后）再拉一次，服务端一直不通的话「每次失败弹一条」就是右上角
// 一串同样的黄条。静默不是选项（审查点名的四处之一），刷屏也不是。
let _statsFailureNotified = false;

async function loadStats() {
    try {
        const r = await fetch('/api/history_stats', { cache: 'no-store' });
        const j = await r.json();
        // success=false 与抛异常是同一件事的两种形状：卡片上的四个数字都停在
        // 上一次的值，而它已经不对了。两条路都得说。
        if (!j.success) throw new Error(j.error || 'history_stats: success=false');
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
        _statsFailureNotified = false;
    } catch (e) {
        console.error('Failed to load stats:', e);
        if (!_statsFailureNotified) {
            _statsFailureNotified = true;
            showToast(t('js.history.stats_failed'), 'warning');
        }
    }
}

// L7：请求序号。状态筛选 chip 连点（无防抖、无禁用、无 in-flight 标志）时，
// 先发的响应可能后返回 —— chip 高亮与 currentStatusFilter 已是新值，表格和
// store 却是旧筛选集合。currentPage 的赋值也必须挪到守卫之后：它是
// panels.js / tasks.js 读取的那个全局，先写会被过期响应污染。
let _historyReqSeq = 0;

async function loadHistory(page = 1, renderMap = true) {
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
        if (renderMap) renderHistoryMap();
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
            : mapSourceText(task);
        const zoom = (task.zoom_min != null && task.zoom_max != null)
            ? `${task.zoom_min}~${task.zoom_max}`
            : '';
        return [styleText, zoom].filter(Boolean).join(' ');
    }
    if (task.task_type === 'plugin') {
        // 插件 id。两个来源字段名不同：/api/history_all 的 UNION 把
        // plugin_tasks.plugin_id 别名进 style 列（第五段要与前四段列序对齐），
        // socket 推送与 /api/plugins/tasks 带的是 plugin_id 原文。
        // 不经 getStyleText —— 那张表是地图底图样式，插件 id 不在里面，
        // 查不到会原样回落，等于白绕一圈。
        return task.style || task.plugin_id || '';
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

// 地图任务行1 的来源文案。**不能无条件走 getStyleText(task.style)**：选了
// 插件源的任务，style 列存的仍是提交那一刻样式下拉的值（后端只用它算了个
// 缓存前缀就丢开，取哪张瓦片全看快照里的 url_template），于是一个天地图任务
// 会显示成「路线图」——下拉没动时的默认值，与真实来源毫无关系。
//
// source_id 是行上唯一的真身份，两条路都发它（/api/history_all 的 map 段从
// source_snapshot 里取，/api/tasks 由 Task.to_dict 输出）：内置源是 style 名
// （'satellite'），插件源是 'plugin:<plugin_id>:<source_id>'。
//
// 尾巴原样显示插件 id + 源 id，与插件任务行的口径一致（那里直接显示
// plugin_id）—— 插件源没有内置那五个样式那样的固定词表可查。
function mapSourceText(task) {
    const sourceId = task.source_id || '';
    if (sourceId.indexOf('plugin:') === 0) {
        const tail = sourceId.slice('plugin:'.length);
        const label = t('js.history.style.plugin_source');
        return tail ? `${label} ${tail}` : label;
    }
    // 存量行（source_id 为空串或缺列）与内置源一字不变地走原路。
    return task.style ? getStyleText(task.style) : '';
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

// 本地地形的 maxzoom 那一列存的是「自动挡」哨兵时的取值，与后端
// src/services/geo_validation.py 的 AUTO_MAXZOOM_SENTINEL 逐字一致
// （tests/test_tasks_js_contract.py 有相等性断言防漂移，与 ui.js 的
// TILE_PATH_PREFIXES / TILE_HEALTH_PATH 同一套写法）。自动挡是出厂默认，
// 认不出它就等于把 `-1` 当层级印在详情面板上。
const TERRAIN_AUTO_MAXZOOM_SENTINEL = -1;

async function viewTaskDetails(taskId, taskType = 'map') {
    try {
        const url = taskType === 'dem' ? `/api/dem/tasks/${taskId}`
                  : taskType === 'local_terrain' ? `/api/terrain/local/tasks/${taskId}`
                  : taskType === 'contour' ? `/api/contour/tasks/${taskId}`
                  : taskType === 'plugin' ? `/api/plugins/tasks/${taskId}`
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
            // 那一列存的是提交时的**基准**层级（自动挡下存的是哨兵，见下）——
            // 精细/快速两档下它与产物实际的最深层级差一级，直接显示就是错数字
            // （precision 档写 0 - 14、layer.json 里是 15）。切完之前
            // effective_maxzoom 为 NULL，只能回落到基准值 —— 那就必须让**文字
            // 本身**说出这是基准值。
            //
            // 这一格的标签写死在 templates/base.html 里换不了，所以把限定词
            // 缀在值后面。只靠下面那句 title 不够：悬停在触摸设备上根本不存在、
            // 键盘也够不着，而且 `0 - 14` 与 `0 - 14` 长得一模一样，用户连
            // 「这里有话要说」都看不出来。
            const localTerrainActualMaxzoom = task.effective_maxzoom;
            // 回退分支自己还有两态：基准层级是「自动」挡时，库里那一列存的是
            // 哨兵，直接显示就是 `0 - -1`；只有手填的作业才有一个数可显示。
            const localTerrainBaseMaxzoom = task.maxzoom === TERRAIN_AUTO_MAXZOOM_SENTINEL
                ? t('js.history.terrain.maxzoom_auto')
                : `${task.maxzoom} (${t('js.history.terrain.maxzoom_base_label')})`;
            document.getElementById('detailZoom').textContent =
                localTerrainActualMaxzoom != null
                    ? `0 - ${localTerrainActualMaxzoom}`
                    : `0 - ${localTerrainBaseMaxzoom}`;
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
        } else if (taskType === 'plugin') {
            // 计数列名是 plugin_tasks 自己的（total_items / downloaded_items /
            // failed_items），**不是** tasks 表那套 *_tiles —— 走 else 分支的
            // 后果是这三格全显示 undefined。
            document.getElementById('detailStyle').textContent = task.plugin_id || '-';
            document.getElementById('detailFormat').textContent = '-';
            // 层级是可选的：插件不一定是瓦片管线，zoom_min/zoom_max 允许 NULL。
            document.getElementById('detailZoom').textContent =
                (task.zoom_min != null && task.zoom_max != null)
                    ? `${task.zoom_min} - ${task.zoom_max}`
                    : '-';
            document.getElementById('detailTotal').textContent = task.total_items;
            document.getElementById('detailDownloaded').textContent = task.downloaded_items;
            document.getElementById('detailFailed').textContent = task.failed_items;
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
        //
        // 说明也得跟着基准值的**来源**走：base_hint 整句是围绕「这是你提交时
        // 填的那个数」写的，自动挡下它逐字都不成立 —— 那一挡的基准是切片时按
        // 源数据分辨率现算的，提交时根本没有这个数。
        document.getElementById('detailZoom').title =
            (taskType === 'local_terrain' && task.effective_maxzoom == null)
                ? (task.maxzoom === TERRAIN_AUTO_MAXZOOM_SENTINEL
                    ? t('js.history.terrain.maxzoom_auto_hint')
                    : t('js.history.terrain.maxzoom_base_hint'))
                : '';

        const total = taskType === 'dem' ? (task.total_files || 0)
                    : taskType === 'local_terrain' ? (task.total_files || 0)
                    : taskType === 'plugin' ? (task.total_items || 0)
                    : (task.total_tiles || 0);
        const done = taskType === 'dem' ? (task.downloaded_files || 0)
                   : taskType === 'local_terrain' ? (task.uploaded_files || 0)
                   : taskType === 'contour' ? (task.rendered_tiles || 0)
                   : taskType === 'plugin' ? (task.downloaded_items || 0)
                   : (task.downloaded_tiles || 0);
        const progress = total > 0
            ? Math.round((done / total) * 100)
            : 0;

        document.getElementById('detailProgress').innerHTML = `
            <div class="progress detail-progress">
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
        // 详情档（ui.js 的 formatCoordExact，6 位）：任务详情是**记录**，
        // 这四个数要能原样贴回去复现选区，与「复制选区」按钮同一档。
        const f = window.formatCoordExact;
        document.getElementById('detailNorth').textContent = hasBbox ? f(task.north) : '-';
        document.getElementById('detailSouth').textContent = hasBbox ? f(task.south) : '-';
        document.getElementById('detailEast').textContent = hasBbox ? f(task.east) : '-';
        document.getElementById('detailWest').textContent = hasBbox ? f(task.west) : '-';

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

        // 地形切片信息区只对本地地形任务开放：它的切片参数就是任务自己的属性，
        // 借这块地方回显档位与法线，数据直接取自 task（local_terrain_tasks 行
        // 本身就带这两列），不发请求。高程下载任务不再显示这一块 —— 切片已收敛
        // 成独立任务（任务行「处理」按钮转出 local_terrain 任务），详情里再挂一份
        // dem_terrain_jobs 的状态/起切按钮只会让人以为它是下载任务的一部分。
        const terrainRow = document.getElementById('detailTerrainRow');
        if (taskType === 'local_terrain') {
            terrainRow.hidden = false;
            document.getElementById('detailTerrainStatus').textContent = '';
            document.getElementById('detailTerrainInfo').innerHTML =
                terrainPresetRowsHtml(task);
        } else {
            terrainRow.hidden = true;
        }

        // 缺口明细（§13-3）：这一块只对地图管线开放。它的三段文案里有两段
        // （「全部缺块都是上游无数据」/「含可重试的缺块 —— 补漏有机会补回
        // 一部分」，以及样本清单）全靠 `explained` 与 `samples`，而插件的
        // `/gaps` 两个都不给 —— 拿 undefined 渲染就会对着一个连补漏端点都
        // 没有的插件任务说「补漏有机会补回一部分」。插件任务的缺块决策面在
        // **行上**（task_list.js 的 .task-gap-line：分档计数 + 「接受并
        // 导出」），那里说的每一句都成立。
        renderDetailGaps(task, taskType);

        // 产物清单（§13-3 / §5.3）：五条管线都有，pipeline 名与 task_type 同名
        // （'plugin' 自 T1 起就在 contracts/artifact.PIPELINES 里）。
        renderDetailArtifacts(taskId, taskType);

        // 任务日志（REST 轮询）。五条管线都有，pipeline 名与 task_type 同名
        // （contracts/artifact.PIPELINES）—— 插件任务走 /api/logs/plugin/<id>。
        openTaskLogPanel(taskType, taskId, task.status);

        // 显示模态框。getOrCreateInstance 与全站一致：重复 new bootstrap.Modal
        // 同一元素会叠出多个实例（每次打开多一层遮罩，关一层还剩一层）。
        bootstrap.Modal.getOrCreateInstance(document.getElementById('taskDetailModal')).show();
    } catch (error) {
        showToast(t('js.history.detail.load_failed'), 'danger');
    }
}

// ---------------------------------------------------------------------------
// 详情弹窗里的缺口明细（§13-3）
// ---------------------------------------------------------------------------
//
// 行上那条决策行只放得下「总数 + 分档 + 两颗按钮」。用户真要判断「补漏值不值得
// 跑」时需要的是**样本**：缺的是哪几块、报的是什么错。GET /api/tasks/<id>/gaps
// 最多回 20 条样本，够看出「是整片没数据」还是「散着几十个超时」——
// 这两件事对应的决定完全相反。

function renderDetailGaps(task, taskType) {
    const row = document.getElementById('detailGapRow');
    const box = document.getElementById('detailGaps');
    if (!row || !box) return;
    // 缺块是瓦片级概念，只有地图管线有这条接口。没有缺口记录的任务也不显示
    // 这一块 —— 一个写着「缺口 0」的区块只会让人怀疑自己是不是漏看了什么。
    const gapTiles = task.gap_tiles || 0;
    const hasGaps = gapTiles > 0
        || (window.TaskStore && window.TaskStore.GAP_STATUSES.includes(task.status));
    if (taskType !== 'map' || !hasGaps) {
        row.hidden = true;
        box.innerHTML = '';
        return;
    }
    row.hidden = false;
    _renderDetailGapsBody(box, task, null);
    if (typeof fetchGapSummary !== 'function') return;
    // 样本必须现拉：/api/tasks/<id> 的任务行只有总数（gap_tiles），分档与样本
    // 都在 /gaps 里。失败时上面那一版（只有总数）留在界面上，并把理由说出来。
    fetchGapSummary(task.id)
        .then(function (summary) { _renderDetailGapsBody(box, task, summary); })
        .catch(function (error) {
            const note = document.createElement('div');
            note.className = 'detail-gap-note';
            note.textContent = t('js.gaps.load_failed', { error: error.message });
            box.appendChild(note);
        });
}

// 逐节点建 DOM：样本行里的 error 是后端异常的字符串化结果（URL、路径、
// 第三方库报错原文都可能在里面），与 .task-error 同一条约定 —— 不进 innerHTML。
function _renderDetailGapsBody(box, task, summary) {
    box.innerHTML = '';
    const total = summary && summary.total != null ? summary.total : (task.gap_tiles || 0);

    const head = document.createElement('div');
    head.className = 'detail-gap-head';
    const badge = document.createElement('span');
    badge.className = 'task-gap-chip';
    badge.textContent = t('js.gaps.chip', { n: total });
    head.appendChild(badge);
    if (summary && typeof gapBreakdownText === 'function') {
        const breakdown = document.createElement('span');
        breakdown.className = 'detail-gap-note';
        breakdown.textContent = gapBreakdownText(summary.by_outcome) || t('js.gaps.none');
        head.appendChild(breakdown);
    }
    box.appendChild(head);

    if (summary) {
        // 「全部缺口都是上游无数据」是**决定性**的一句话：那时补漏一张也补不回来
        // （no_data 不在 RETRYABLE_OUTCOMES 里），用户该点的是「接受并导出」。
        const note = document.createElement('div');
        note.className = summary.explained
            ? 'detail-gap-note detail-gap-note--explained'
            : 'detail-gap-note';
        note.textContent = summary.explained
            ? t('js.gaps.explained')
            : t('js.gaps.unexplained');
        box.appendChild(note);

        if (summary.decision) {
            const decided = document.createElement('div');
            decided.className = 'detail-gap-note';
            decided.textContent = t('js.gaps.decided', { decision: summary.decision });
            box.appendChild(decided);
        }

        const samples = summary.samples || [];
        if (samples.length) {
            const list = document.createElement('div');
            list.className = 'detail-gap-samples';
            samples.forEach(function (s) {
                const line = document.createElement('span');
                line.className = 'detail-gap-sample';
                line.textContent = t('js.gaps.sample', {
                    zoom: s.zoom, x: s.x, y: s.y,
                    outcome: typeof gapOutcomeLabel === 'function'
                        ? gapOutcomeLabel(s.outcome) : s.outcome,
                    error: s.error || '',
                });
                list.appendChild(line);
            });
            box.appendChild(list);
        }
    }
}

// ---------------------------------------------------------------------------
// 详情弹窗里的产物清单（§13-3 / §5.3）
// ---------------------------------------------------------------------------
//
// GET /api/tasks/<id>/artifacts。这一块在 2026-08 之前**不存在**，那条接口
// 全站零调用方 —— 后果不是「少一个功能」：`artifacts.has_gaps` 才是「这份
// 成果有洞」的权威落点（artifacts 表刻意没有外键，就是为了让产物行比任务行
// 活得久），而它在界面上一个字都不显示。§13-3 要的「成果与历史永久带缺块
// 标记」于是只写在库里给自己看。MBTiles 的体检判决（meta.validation）同理：
// 它不阻断导出，所以不透出来就等于没做。
//
// 形态名逐个写成完整键字面量、不做前缀拼接：tests/test_i18n.py 的双向闭合
// 按字面量扫源码（同 GAP_OUTCOME_LABELS / TERRAIN_QUALITY_KEYS）。认不出的
// 形态原样显示后端的值，不静默吞掉 —— 后端加一档时界面会露出机器码，
// 那比少一行好。
const ARTIFACT_KIND_LABELS = {
    xyz_dir: 'js.artifacts.kind.xyz_dir',
    geotiff: 'js.artifacts.kind.geotiff',
    mbtiles: 'js.artifacts.kind.mbtiles',
    terrain_dir: 'js.artifacts.kind.terrain_dir',
    contour_dir: 'js.artifacts.kind.contour_dir',
    dem_dir: 'js.artifacts.kind.dem_dir',
};

function artifactKindLabel(kind) {
    const key = Object.prototype.hasOwnProperty.call(ARTIFACT_KIND_LABELS, kind)
        && ARTIFACT_KIND_LABELS[kind];
    return key ? t(key) : String(kind || '');
}

/**
 * 拉产物清单并渲染。整块在拿到响应之前就先显出来（占位文案），拉失败时
 * 把理由说出来 —— 静默隐藏会让「这个任务没产物」和「清单没读到」长得一样。
 *
 * **不检查文件是否还在**：用户删任务时可以选「保留文件」，也可以在文件管理器
 * 里自己删掉产物而留着这行记录。产物行比文件活得久是设计如此，把「文件不在了」
 * 渲染成错误是在报告一个正常状态；真去 stat 一遍更糟（几十万文件的目录）。
 */
function renderDetailArtifacts(taskId, taskType) {
    const row = document.getElementById('detailArtifactRow');
    const box = document.getElementById('detailArtifacts');
    if (!row || !box) return;
    row.hidden = false;
    box.textContent = t('js.artifacts.loading');
    fetch(`/api/tasks/${taskId}/artifacts?pipeline=${encodeURIComponent(taskType)}`)
        .then(function (response) {
            return response.json().then(function (data) {
                if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
                return data;
            });
        })
        .then(function (data) { _renderDetailArtifactsBody(box, data.artifacts || []); })
        .catch(function (error) {
            box.textContent = t('js.artifacts.load_failed', { error: error.message });
        });
}

// 逐节点建 DOM：path 是绝对路径、validation.problems 是校验器的英文原文
// （URL、键名、路径都可能在里面），与 .task-error / .detail-gap-sample 同一条
// 约定 —— 不进 innerHTML。
function _renderDetailArtifactsBody(box, artifacts) {
    box.textContent = '';
    if (!artifacts.length) {
        const empty = document.createElement('div');
        empty.className = 'detail-artifact-note';
        empty.textContent = t('js.artifacts.none');
        box.appendChild(empty);
        return;
    }
    artifacts.forEach(function (a, i) {
        const item = document.createElement('div');
        // 第 2 件起画分隔线。用修饰类而不是 CSS 的 `+` 兄弟组合符：
        // tests/test_css_contract.py 的层叠模型只认后代组合符（先例是
        // map.js 给 .tif-info__file 加 --sep 的同一条约定）。
        item.className = i === 0 ? 'detail-artifact' : 'detail-artifact detail-artifact--sep';

        const head = document.createElement('div');
        head.className = 'detail-artifact-head';
        const kind = document.createElement('span');
        kind.className = 'detail-artifact-kind';
        kind.textContent = artifactKindLabel(a.kind);
        head.appendChild(kind);

        // 规模三段各自可缺（非瓦片产物没有层级，老行没统计过大小），
        // 过滤掉空段再连 —— 不过滤会连出「 ·  · z10-13」这种断头串。
        const facts = [
            a.format || '',
            a.bytes_total > 0 && typeof formatBytes === 'function'
                ? formatBytes(a.bytes_total) : '',
            a.tile_count > 0
                ? t('js.artifacts.tiles', { n: Number(a.tile_count).toLocaleString() }) : '',
            a.minzoom != null && a.maxzoom != null
                ? t('js.artifacts.zooms', { min: a.minzoom, max: a.maxzoom }) : '',
        ].filter(Boolean);
        if (facts.length) {
            const factsEl = document.createElement('span');
            factsEl.className = 'detail-artifact-facts';
            factsEl.textContent = facts.join(' · ');
            head.appendChild(factsEl);
        }

        // 缺块标记复用行上那颗徽章的长相（.task-gap-chip）：同一件事在界面上
        // 只该有一个符号。文案不同是因为数据不同 —— 行上有具体块数，
        // 产物上只有一个布尔。
        if (a.has_gaps) {
            const chip = document.createElement('span');
            chip.className = 'task-gap-chip';
            chip.textContent = t('js.artifacts.gapped');
            chip.title = t('js.artifacts.gapped_title');
            head.appendChild(chip);
        }
        item.appendChild(head);

        const path = document.createElement('div');
        path.className = 'detail-artifact-path';
        path.textContent = a.path || '';
        item.appendChild(path);

        _appendArtifactValidation(item, a.meta && a.meta.validation);
        box.appendChild(item);
    });
}

// MBTiles 的体检判决。**没有这个键就什么都不渲染** —— 本次改造之前导出的行
// 没跑过校验，把「没查过」画成任何一种结论都是撒谎，而画成「有问题」尤其糟：
// 那些库绝大多数是好的。
function _appendArtifactValidation(item, validation) {
    if (!validation || typeof validation !== 'object') return;
    const problems = validation.problems || [];
    const note = document.createElement('div');
    note.className = validation.ok
        ? 'detail-artifact-note'
        : 'detail-artifact-note detail-artifact-note--problem';
    note.textContent = validation.ok
        ? t('js.artifacts.validation.ok')
        : t('js.artifacts.validation.problems', { n: problems.length });
    item.appendChild(note);
    if (validation.ok || !problems.length) return;
    // 问题原文是校验器给开发者/报障用的英文诊断句（src/services/mbtiles.py），
    // **刻意不翻译**：它们是技术细节，不是界面文案。中文那一层由上面那句
    // 「发现 N 个问题」承担，这里原样照抄，方便直接贴进 issue。
    const list = document.createElement('div');
    list.className = 'detail-artifact-problems';
    problems.forEach(function (p) {
        const line = document.createElement('span');
        line.className = 'detail-artifact-problem';
        line.textContent = String(p);
        list.appendChild(line);
    });
    item.appendChild(list);
}

// ---------------------------------------------------------------------------
// 详情弹窗里的任务日志（§4.5）
// ---------------------------------------------------------------------------
//
// GET /api/logs/<pipeline>/<id> 轮询，**不是** socket 流。
// 理由（后端 docstring 里写的同一条）：这个应用没有 room 也没有 namespace，
// 每一发 emit 都广播给所有连着的浏览器 —— 逐行推送日志等于把一个任务的日志
// 发给所有开着页面的人，其中绝大多数没在看这个任务。
//
// 轮询只在**弹窗开着且任务还活着**时进行：终态任务的日志不会再长，一直轮它
// 是白烧一次请求；弹窗关了更不用说。停轮的两个出口都接上了（模态的
// hidden.bs.modal，以及下一次 openTaskLogPanel 的重入）。

let _taskLogTimer = null;
// 当前面板绑的是哪个任务。轮询回调回来时要对一下：用户可能已经关掉弹窗又开了
// 另一个任务，晚到的响应会把别人的日志画进来。
let _taskLogTarget = null;
let _taskLogErrorsOnly = false;
// 轮询间隔。2s 是「看得出在动」与「不白烧请求」之间的取值：日志行由后端按事件
// 写，跑得最快的下载任务也就每秒几十行，2s 一批读起来正好是一屏。
const TASK_LOG_POLL_MS = 2000;
const TASK_LOG_LIMIT = 500;

function stopTaskLogPolling() {
    clearInterval(_taskLogTimer);
    _taskLogTimer = null;
    _taskLogTarget = null;
}

/** 打开（或重开）日志面板。status 决定要不要轮询。 */
function openTaskLogPanel(pipeline, taskId, status) {
    const row = document.getElementById('detailLogRow');
    const host = document.getElementById('detailLog');
    if (!row || !host) return;
    stopTaskLogPolling();
    row.hidden = false;
    _taskLogTarget = `${pipeline}:${taskId}`;
    _taskLogErrorsOnly = false;
    _renderTaskLogShell(host, pipeline, taskId);
    refreshTaskLog(pipeline, taskId);
    // 活动态才轮。判据走 store 的活动集清单（后端 ACTIVE_TASK_STATES 的镜像），
    // 不在这里抄一份状态字面量。
    const active = window.TaskStore
        && window.TaskStore.ACTIVE_STATUSES.includes(status);
    if (active) {
        _taskLogTimer = setInterval(function () {
            refreshTaskLog(pipeline, taskId);
        }, TASK_LOG_POLL_MS);
    }
}

// 工具条（仅错误开关 + 复制诊断 + 下载诊断）与正文容器。工具条只建一次，
// 正文由 refreshTaskLog 反复重写 —— 不这么分的话每次轮询都会把「仅错误」
// 那个复选框重建一遍，用户刚勾上就被抹掉。
function _renderTaskLogShell(host, pipeline, taskId) {
    host.innerHTML = '';

    const toolbar = document.createElement('div');
    toolbar.className = 'task-log__toolbar';

    const toggleWrap = document.createElement('div');
    toggleWrap.className = 'form-check';
    const toggle = document.createElement('input');
    toggle.className = 'form-check-input';
    toggle.type = 'checkbox';
    toggle.id = 'taskLogErrorsOnly';
    const toggleLabel = document.createElement('label');
    toggleLabel.className = 'form-check-label';
    toggleLabel.setAttribute('for', 'taskLogErrorsOnly');
    // 「仅错误」包含 WARNING（后端 task_logging._ERROR_LEVELS）：重试、429、
    // 无覆盖恰好都是 WARNING，而它们正是「为什么只下到一半」的答案。
    toggleLabel.textContent = t('js.tasklog.errors_only');
    toggle.addEventListener('change', function () {
        _taskLogErrorsOnly = toggle.checked;
        refreshTaskLog(pipeline, taskId);
    });
    toggleWrap.appendChild(toggle);
    toggleWrap.appendChild(toggleLabel);
    toolbar.appendChild(toggleWrap);

    const tools = document.createElement('div');
    tools.className = 'task-log__tools';

    const copyBtn = document.createElement('button');
    copyBtn.type = 'button';
    copyBtn.className = 'btn btn-sm btn-outline-secondary';
    copyBtn.textContent = t('js.tasklog.copy');
    copyBtn.addEventListener('click', function () {
        copyTaskDiagnostics(pipeline, taskId, copyBtn);
    });
    tools.appendChild(copyBtn);

    // 下载走 <a download> 而不是 fetch + Blob：端点已经带
    // Content-Disposition: attachment，浏览器自己就会存盘，中间过一手 Blob
    // 只是把一份可能几百 KB 的文本白读进内存。
    const dl = document.createElement('a');
    dl.className = 'btn btn-sm btn-outline-secondary';
    dl.href = `/api/logs/${pipeline}/${taskId}/diagnostics`;
    dl.setAttribute('download', '');
    dl.textContent = t('js.tasklog.download');
    tools.appendChild(dl);

    toolbar.appendChild(tools);
    host.appendChild(toolbar);

    const body = document.createElement('div');
    body.className = 'task-log__body';
    body.id = 'taskLogBody';
    host.appendChild(body);
}

async function refreshTaskLog(pipeline, taskId) {
    const body = document.getElementById('taskLogBody');
    if (!body) return;
    const target = `${pipeline}:${taskId}`;
    try {
        const params = new URLSearchParams({ limit: String(TASK_LOG_LIMIT) });
        if (_taskLogErrorsOnly) params.set('errors_only', '1');
        const response = await fetch(
            `/api/logs/${pipeline}/${taskId}?${params.toString()}`);
        const data = await response.json().catch(() => ({}));
        // 弹窗已经换了任务（或关了）：这一发是别人的日志，丢掉。
        if (_taskLogTarget !== target) return;
        if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
        _renderTaskLogEntries(body, data);
    } catch (error) {
        if (_taskLogTarget !== target) return;
        _renderTaskLogNote(body, t('js.tasklog.load_failed', { error: error.message }));
    }
}

function _renderTaskLogNote(body, text) {
    body.innerHTML = '';
    const note = document.createElement('span');
    note.className = 'task-log__empty';
    note.textContent = text;
    body.appendChild(note);
}

// 后端把「空」的三种来源分成了三个字段，因为要说的话完全不同 ——
// 「日志已关闭」（去配置页开 task_log_enabled）、「这个任务还没跑过」、
// 「这一段确实没有错误行」。合成一句「暂无日志」会让第一种情况下的用户
// 永远等不到日志，也永远不知道去哪儿开它。
function _renderTaskLogEntries(body, data) {
    const entries = data.entries || [];
    if (!data.enabled) {
        _renderTaskLogNote(body, t('js.tasklog.disabled'));
        return;
    }
    if (!data.has_log) {
        _renderTaskLogNote(body, t('js.tasklog.no_file'));
        return;
    }
    if (!entries.length) {
        _renderTaskLogNote(body, data.errors_only
            ? t('js.tasklog.no_errors')
            : t('js.tasklog.empty'));
        return;
    }
    body.innerHTML = '';
    entries.forEach(function (entry) {
        const line = document.createElement('span');
        const level = String(entry.level || '').toUpperCase();
        line.className = 'task-log__line'
            + (level === 'ERROR' || level === 'CRITICAL' ? ' task-log__line--error' : '')
            + (level === 'WARNING' ? ' task-log__line--warning' : '');
        // textContent 一次写整行：message 是任务日志原文（路径、URL、traceback
        // 片段），任何时候都不进 innerHTML。
        line.textContent = t('js.tasklog.line', {
            ts: entry.ts || '',
            level: level,
            message: entry.message || '',
        });
        body.appendChild(line);
    });
}

/**
 * 复制脱敏诊断包到剪贴板。
 *
 * 拿的是 /diagnostics 那份纯文本（含环境头 + 日志尾部，脱敏在服务端做），
 * 不是把界面上这几百行拼起来 —— 界面上是**过滤后**的尾部，而用户复制它的
 * 用途是贴进 issue，缺了环境信息那份贴上去也没人能看。
 */
async function copyTaskDiagnostics(pipeline, taskId, button) {
    if (button) button.disabled = true;
    try {
        const response = await fetch(`/api/logs/${pipeline}/${taskId}/diagnostics`);
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const text = await response.text();
        await navigator.clipboard.writeText(text);
        showToast(t('js.tasklog.copied'), 'success');
    } catch (error) {
        // 剪贴板 API 在非安全上下文里会直接抛（http:// 且非 localhost）。
        // 那时「复制失败」不够 —— 用户还有「下载」那颗按钮可用，说清楚。
        showToast(t('js.tasklog.copy_failed', { error: error.message }), 'danger');
    } finally {
        if (button) button.disabled = false;
    }
}

// 关弹窗即停轮。挂在 document 上做委托而不是给 #taskDetailModal 直接挂：
// 本文件在 /history 与首页都加载，而这一段在解析期跑（模态元素来自
// base.html，那时已经在 DOM 里，但委托对「元素被替换」也免疫）。
document.addEventListener('hidden.bs.modal', function (e) {
    if (e.target && e.target.id === 'taskDetailModal') stopTaskLogPolling();
});

// 后端存的档位是枚举字面量（geo_validation.TILING_QUALITY_OFFSETS 的键）。
// 这里逐档写成完整的键字面量、不做字符串拼接：tests/test_i18n.py 的双向闭合
// 是按「key 形状的字面量」扫源码的，拼出来的键会被当成无人引用而判死。
const TERRAIN_QUALITY_KEYS = {
    precision: 'js.history.terrain.quality_precision',
    balanced: 'js.history.terrain.quality_balanced',
    speed: 'js.history.terrain.quality_speed',
};

// 档位与法线两行，给本地地形任务的详情回显 —— 上传表单/「处理」弹窗是用户
// 唯一能亲手选档位的入口，几十分钟切完回来查不到自己当时选了什么更说不过去。
// 数据取自 local_terrain_tasks 行（quality / vertex_normals 与 dem_terrain_jobs
// 同名同义）。
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

// 活动状态 -> 确认文案的键。终态（completed / completed_with_gaps / failed）
// 不在表里，走通用文案。
// 每个活动态各说各的，不合并成一句「该任务尚未结束」：用户按下删除前要判断的
// 是「我会失去什么」，pending 什么都还没跑（只是排队），running / retrying /
// paused 有已下载的进度会丢，pending_decision 还攒着一份等他决定的缺块清单
// —— 这几件事对决策的分量不一样。
//
// ⚠️ 键集合必须等于后端 contracts.outcome.ACTIVE_STATE_VALUES（五态）。
// 少一个的表现不是文案错，是**没有警告**：那个状态掉进通用文案「记录不可
// 恢复」，用户看不出自己正在杀掉一个还在跑的任务。缺块改造新增 retrying /
// pending_decision 时这里就漏过一次。
const DELETE_CONFIRM_KEYS = {
    running: 'js.history.confirm.delete_task_running',
    retrying: 'js.history.confirm.delete_task_retrying',
    pending: 'js.history.confirm.delete_task_pending',
    paused: 'js.history.confirm.delete_task_paused',
    pending_decision: 'js.history.confirm.delete_task_pending_decision',
};

// trigger：触发这次删除的那颗按钮（行上的 🗑 由 task_list.js 的 act 自己上锁，
// 所以那条路不传）。整段都在守卫里，确认框也算：连点会叠出两个确认框，两次
// 回车就是两发 DELETE，第二发撞 404 再弹一条红字 —— 用户以为自己删错了东西。
async function deleteTask(taskId, taskType = 'map', trigger = null) {
    return guard(trigger, async function () {
        // 状态取自 store 而不是 allTasks：socket 增量插进来的行只进了 store
        // （同 previewHistoryTask 的说明）。查不到就退回通用文案 —— 宁可少一句
        // 警告，也不能对着一个不知道状态的任务瞎说「正在运行」。
        const store = window.TaskStore;
        const task = store && store.get(`${taskType}:${taskId}`);
        // hasOwnProperty 同 terrainPresetRowsHtml 的档位查表：`task.status` 若是
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

        // 删除进度框：只在勾了「同时删除磁盘产物」时开。没勾的话请求只是一条
        // DELETE + 一次 stat，毫秒级返回，弹个框反而是闪一下的噪声。
        //
        // 勾了的那条路是**同步**的：后端在请求线程里 rmtree 整个瓦片金字塔，
        // 大任务几万到上百万个文件，Windows 上几十秒到几分钟 fetch 才返回。
        // 改造前这段时间里界面完全没有反馈（确认框一关就没动静），用户以为没点上。
        //
        // 进度经 socket 的 task_delete_progress 推来（services/task_deletion 的
        // _make_progress_emitter，5 次/秒）。socket 拿不到时（库没加载）照样开框，
        // 只是停在「正在删除…」——退化成一个忙碌指示，仍然好过一片死寂。
        //
        // ⚠️ get() 之后必须在同一个同步块里 socket.on（socket.io 不重放错过的事件，
        // 见 socket.js 文件头）。这里中间没有 await，注册紧跟 get()。
        const socket = deleteFiles && window.TerraSocket ? window.TerraSocket.get() : null;
        const progressBox = deleteFiles
            ? showProgressDialog({
                title: t('js.history.progress.delete_title'),
                message: t('js.history.progress.delete_row'),
            })
            : null;
        function onDeleteProgress(data) {
            // 广播是全局的（本项目的 socket 没有房间），必须自己按任务过滤 ——
            // 否则另一个客户端删别的任务时，这个框会跟着跳。
            if (!data || data.task_id !== taskId || data.task_type !== taskType) return;
            if (data.phase === 'scan') {
                progressBox.update({
                    text: t('js.history.progress.delete_scanning', {count: data.removed}),
                    percent: null,
                });
                return;
            }
            progressBox.update({
                text: t('js.history.progress.delete_removing',
                        {done: data.removed, total: data.total}),
                percent: data.total ? (data.removed / data.total) * 100 : null,
            });
        }
        if (socket && progressBox) socket.on('task_delete_progress', onDeleteProgress);

        try {
            const deleteUrl = taskType === 'dem' ? `/api/dem/tasks/${taskId}`
                            : taskType === 'local_terrain' ? `/api/terrain/local/tasks/${taskId}`
                            : taskType === 'contour' ? `/api/contour/tasks/${taskId}`
                            : taskType === 'plugin' ? `/api/plugins/tasks/${taskId}`
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
                    // 明确忽略：四条 DELETE 端点都回 JSON；解析不了也只是少一句
                    // 提示，不能因此把后面的摘行/刷新整串跳过。
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
        } finally {
            // 无条件收框 —— 包括后端 500、断网、页面被切走导致 fetch 直接 reject 的
            // 那几条路。这个框不响应 ESC，也没有取消按钮：这里漏一次就是一个再也
            // 关不掉的全屏遮罩，整页从此点不动。
            if (socket && progressBox) socket.off('task_delete_progress', onDeleteProgress);
            if (progressBox) progressBox.close();
        }
    });
}
