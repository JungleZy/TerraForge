let historyViewer;
let currentPage = 1;
let allTasks = [];
// 状态筛选 chips 的当前取值（'' = 全部）。只作用于历史流：
// 透传给 /api/history_all 的 ?status= 参数（后端已支持）。
let currentStatusFilter = '';

function initHistory() {
    initHistoryMap();
    loadHistory(1);
    loadStats();

    document.getElementById('searchInput').addEventListener('input', function(e) {
        filterTasks(e.target.value);
    });

    // 状态筛选 chips（2026-08 统一流式列表）：全部/已完成/失败/已取消。
    // 只筛选历史流，不影响上方活动/失败分组（那是 tasks.js 的实时区）。
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
    });
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

async function loadHistory(page = 1) {
    try {
        currentPage = page;
        const statusParam = currentStatusFilter
            ? `&status=${encodeURIComponent(currentStatusFilter)}`
            : '';
        const response = await fetch(`/api/history_all?page=${page}&per_page=20${statusParam}`, { cache: 'no-store' });
        const data = await response.json();

        if (!data.success) {
            throw new Error(data.error || 'Failed to load history');
        }

        allTasks = data.tasks || [];
        renderHistoryTable(allTasks);
        const p = data.pagination || {};
        renderPagination(p.page || 1, p.total_pages || 1);
        renderHistoryMap(allTasks);
    } catch (error) {
        console.error('Failed to load history:', error);
        // 错误提示挂在 .text-danger 上（CSS 类），不写内联 color。
        // ⚠️ 内联 style 里不许带分号：tests/test_css_contract.py 的
        // _history_error_div 用 `(.*?);` 非贪婪切这段模板，分号会让它提前收尾。
        document.getElementById('historyTableBody').innerHTML =
            '<div class="text-center text-danger" style="padding: 1.5rem 1rem">加载失败</div>';
    }
}

// 历史流失败行的兜底文案（与 tasks.js 的 UNKNOWN_ERROR_TEXT 同语义，
// 但**不能**同名：首页两个文件共享全局作用域，const 重名会直接
// SyntaxError 让整个文件失效——函数声明可以重复，const 不行）。
const HISTORY_UNKNOWN_ERROR = '任务失败，但没有记录失败原因。';

function renderHistoryTable(tasks) {
    const container = document.getElementById('historyTableBody');

    // 精确去重（2026-08 定稿）：历史流排除「当前还挂在实时区里的任务」——
    // 活动组/失败组里显示着的，历史区就不显示；dismiss 之后下次刷新才在
    // 历史出现。比对键是 tasks.js 全局 activeTasks Map 的 _key（`类型:id`）。
    // 这替代旧的「按状态跳过 pending/running/paused」规则：旧规则会把
    // 从未进过实时区的历史非终态任务也一并藏掉，而失败任务又在实时区和
    // 历史区各出现一次。
    // 独立页 /history 不加载 tasks.js（activeTasks 未定义）、也没有
    // #activeTasksBody，两个条件都不满足，照旧全量渲染。
    const dedup = (typeof activeTasks !== 'undefined' && activeTasks instanceof Map)
        && document.getElementById('activeTasksBody');
    const rows = dedup
        ? tasks.filter(t => !activeTasks.has(`${t.task_type}:${t.id}`))
        : tasks;

    if (rows.length === 0) {
        container.innerHTML = `
            <div class="task-empty">
                <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="opacity: 0.3; margin-bottom: 1rem;">
                    <circle cx="12" cy="12" r="10"></circle>
                    <line x1="12" y1="8" x2="12" y2="12"></line>
                    <line x1="12" y1="16" x2="12.01" y2="16"></line>
                </svg>
                <p style="margin: 0;">暂无历史记录</p>
            </div>
        `;
        return;
    }

    container.innerHTML = rows.map(createHistoryRow).join('');
    // 失败历史行的错误原文与活动失败行同一条规矩：不进 innerHTML 模板
    // （error_message 是后端异常的字符串化结果），渲染后按行用
    // textContent 补——与 tasks.js 的 applyTaskErrorText 同一形态。
    rows.forEach(task => {
        if (task.status !== 'failed') return;
        const row = document.getElementById(`hist-${task.task_type}:${task.id}`);
        const box = row && row.querySelector('.task-error');
        if (box) {
            box.textContent = task.error_message || HISTORY_UNKNOWN_ERROR;
        }
    });
}

// 历史流行1 的元信息（与 tasks.js taskMetaText 同语言；两份是刻意重复，
// 无构建工具、两个页面不会同时加载的另一个文件不可见）。
function historyMetaText(task) {
    if (task.task_type === 'map' || task.task_type === 'contour') {
        const styleText = task.task_type === 'contour'
            ? '等高线'
            : (task.style ? getStyleText(task.style) : '');
        const zoom = (task.zoom_min != null && task.zoom_max != null)
            ? `${task.zoom_min}~${task.zoom_max}`
            : '';
        return [styleText, zoom].filter(Boolean).join(' ');
    }
    if (task.task_type === 'dem') {
        // /api/history_all 的 UNION 把 dem_tasks.dataset 映射进 style 列
        return task.style || '高程';
    }
    return '本地高程切片';
}

// 统一流式行（与 tasks.js createTaskRow 同一种行语言）：
//   行1 状态点 + 名称 + #类型:id + 元信息 …… 完成时间 + 动作组（预览/详情/删除）
//   行2 completed/cancelled → `已完成 · 54 / 60 瓦片 · 区域 …` 单行弱化；
//       failed → 引文式错误（空 .task-error 容器，文本由 renderHistoryTable 补）
function createHistoryRow(task) {
    const key = `${task.task_type}:${task.id}`;
    const isFailed = task.status === 'failed';

    const itemLabel = (task.task_type === 'map' || task.task_type === 'contour') ? '瓦片' : '文件';
    const hasBbox = task.north != null && task.south != null
                 && task.east != null && task.west != null;
    // 坐标是后端数值（无注入面），单行排版，超宽由 CSS 省略号截断。
    const bboxText = hasBbox
        ? ` · 区域 ${task.north.toFixed(2)}, ${task.south.toFixed(2)}, ${task.east.toFixed(2)}, ${task.west.toFixed(2)}`
        : '';
    const line2 = isFailed
        ? '<div class="task-error" role="alert"></div>'
        : `<div class="task-line2">${escapeHtml(getStatusText(task.status))} · ${task.downloaded} / ${task.total} ${itemLabel}${bboxText}</div>`;

    // 预览按钮只在首页覆盖面板里有意义（主视图在旁边）；独立页没有主视图。
    const canPreview = typeof previewTask === 'function';

    return `
        <div class="task-row status-${task.status}" id="hist-${key}">
            <div class="task-line1">
                <span class="task-dot" aria-hidden="true"></span>
                <span class="task-name">${escapeHtml(task.name)}</span>
                <span class="task-id">#${escapeHtml(key)}</span>
                <span class="task-meta">${escapeHtml(historyMetaText(task))}</span>
                <span class="task-time progress-detail">${formatDate(task.completed_at)}</span>
                <div class="btn-group btn-group-sm">
                    ${(canPreview && task.status === 'completed') ? `
                    <button class="btn btn-icon btn-sm btn-success" onclick="previewHistoryTask(${task.id}, '${task.task_type}')" title="在地图上预览" aria-label="在地图上预览">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path>
                            <circle cx="12" cy="12" r="3"></circle>
                        </svg>
                    </button>` : ''}
                    <button class="btn btn-icon btn-sm btn-info" onclick="viewTaskDetails(${task.id}, '${task.task_type}')" title="查看详情" aria-label="查看任务详情">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <circle cx="12" cy="12" r="10"></circle>
                            <line x1="12" y1="16" x2="12" y2="12"></line>
                            <line x1="12" y1="8" x2="12.01" y2="8"></line>
                        </svg>
                    </button>
                    <button class="btn btn-icon btn-sm btn-danger" onclick="deleteTask(${task.id}, '${task.task_type}')" title="删除任务" aria-label="删除任务">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <polyline points="3 6 5 6 21 6"></polyline>
                            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                        </svg>
                    </button>
                </div>
            </div>
            ${line2}
        </div>
    `;
}

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
        html += `<li class="page-item"><a class="page-link" href="#" onclick="loadHistory(${currentPage - 1}); return false;">上一页</a></li>`;
    }

    for (let i = Math.max(1, currentPage - 2); i <= Math.min(totalPages, currentPage + 2); i++) {
        html += `<li class="page-item ${i === currentPage ? 'active' : ''}">
            <a class="page-link" href="#" onclick="loadHistory(${i}); return false;">${i}</a>
        </li>`;
    }

    if (currentPage < totalPages) {
        html += `<li class="page-item"><a class="page-link" href="#" onclick="loadHistory(${currentPage + 1}); return false;">下一页</a></li>`;
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
                <strong>状态:</strong> ${escapeHtml(getStatusText(task.status))}<br>
                <strong>${task.task_type === 'dem' ? '文件' : '瓦片'}:</strong>
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
}

function filterTasks(searchTerm) {
    const filtered = allTasks.filter(task =>
        task.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
        task.id.toString().includes(searchTerm)
    );
    renderHistoryTable(filtered);
}

// A7 / Task 12：这两张表原先只映射 completed / failed / cancelled 三态。
// /api/history_all 默认不带 status 过滤（路由的 ?status= 是可选参数，
// 状态筛选 chips 选中时才传），pending / running / paused 的任务照样可能
// 进历史流（例如独立页 /history 全量渲染）。落在表外的状态会走
// `|| status` 兜底，把后端的**英文字面量**直接渲染进中文界面
// —— 这就是历史页里 `paused` 与「✓ 已完成」中英混杂的根源。
// 现在与 tasks.js 的同名函数逐字对齐，覆盖 models/task.py 的 TaskStatus 全部六态。
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
        'failed': 'danger',
        'cancelled': 'dark'
    };
    return colors[status] || 'secondary';
}

// 历史地图上矩形的描边色。这是**第四处**状态映射点（前三处是 getStatusColor /
// getStatusText / statusIcons），A7 / Task 12 一并补齐。
//
// 改前是内联三元阶梯，只认 completed / failed，其余四态（pending / running /
// paused / cancelled）全折叠成同一个蓝色 —— 与徽章那三张表是完全同型的缺陷。
// 而且三个色号 #10b981 / #ef4444 / #60a5fa 是**硬编码且离调色板**的：
// #10b981 是 emerald-500，本项目的 --color-success 是 emerald-400 #34d399，
// 改调色板时这里会静默漂移。
//
// 现在读 CSS 自定义属性，与徽章/进度条/卡片边条走同一套语义令牌：
//   pending -> --color-text-secondary（与 .badge.bg-secondary 同色）
//   cancelled -> --color-neutral（与 .progress-bar.bg-dark 同色）
// Leaflet 要的是真实色值字符串，不认 var()，所以必须在这里求值。
function getStatusStroke(status) {
    const vars = {
        'pending': '--color-text-secondary',
        'running': '--color-info',
        'paused': '--color-warning',
        'completed': '--color-success',
        'failed': '--color-danger',
        'cancelled': '--color-neutral'
    };
    const name = vars[status] || '--color-text-secondary';
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
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
    // 未知状态不把英文字面量原样渲染进中文界面（与 tasks.js 同一约定）
    return texts[status] || '未知';
}

function getStyleText(style) {
    const styles = {
        'roadmap': '路线图',
        'satellite': '卫星图',
        'hybrid': '混合图',
        'terrain': '地形图',
        // 兼容旧的缩写格式
        'm': '标准',
        's': '卫星',
        'y': '卫星+标注',
        'h': '道路',
        't': '地形',
        'contour': '等高线'
    };
    return styles[style] || style;
}

function formatDate(dateStr) {
    const date = parseTaskDate(dateStr);
    return date ? date.toLocaleString('zh-CN') : '-';
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
            document.getElementById('detailStyle').textContent = '本地高程切片';
            document.getElementById('detailFormat').textContent = '-';
            document.getElementById('detailZoom').textContent = `0 - ${task.maxzoom}`;
            document.getElementById('detailTotal').textContent = task.total_files;
            document.getElementById('detailDownloaded').textContent = task.uploaded_files;
            document.getElementById('detailFailed').textContent = task.failed_files;
        } else if (taskType === 'contour') {
            document.getElementById('detailStyle').textContent = '等高线瓦片';
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

        // 显示模态框
        const modal = new bootstrap.Modal(document.getElementById('taskDetailModal'));
        modal.show();
    } catch (error) {
        showToast('获取任务详情失败', 'danger');
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
                throw new Error(j.error || '启动切片失败');
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
            statusEl.innerHTML = `<span class="badge bg-secondary">未开始</span>`;
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
        const label = escapeHtml(status === 'unknown' ? '状态未知' : getStatusText(status));
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
        statusEl.innerHTML = `<span class="badge bg-danger">加载失败</span>`;
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
    const task = allTasks.find(t => t.id === taskId && t.task_type === taskType);
    if (!task) return;
    previewTask(Object.assign({}, task, { task_type: taskType }));
    if (typeof closePanel === 'function') closePanel();
}

async function deleteTask(taskId, taskType = 'map') {
    if (!await showConfirm('确定要删除这个任务吗？', { title: '删除任务', danger: true })) {
        return;
    }

    // 第二步确认：是否连磁盘上的下载产物一起删。后端 DELETE 端点按
    // ?delete_files=true/false 决定是否清理产物目录，缺省 false（保留）。
    const deleteFiles = await showConfirm('是否同时删除磁盘上的下载产物？', {
        title: '清理下载产物',
        confirmText: '删除产物',
        cancelText: '保留产物',
        danger: true
    });

    try {
        const deleteUrl = taskType === 'dem' ? `/api/dem/tasks/${taskId}`
                        : taskType === 'local_terrain' ? `/api/terrain/local/tasks/${taskId}`
                        : taskType === 'contour' ? `/api/contour/tasks/${taskId}`
                        : `/api/tasks/${taskId}`;
        const response = await fetch(`${deleteUrl}?delete_files=${deleteFiles ? 'true' : 'false'}`, { method: 'DELETE' });

        if (response.ok) {
            showToast('任务已删除', 'success');
            // 删掉的是当前页最后一条时本页已空，停在原页会看到空白页——回退一页
            if (allTasks.length <= 1 && currentPage > 1) {
                loadHistory(currentPage - 1);
            } else {
                loadHistory(currentPage);
            }
            loadStats();
        } else {
            showToast('删除失败', 'danger');
        }
    } catch (error) {
        showToast('删除失败: ' + error.message, 'danger');
    }
}
