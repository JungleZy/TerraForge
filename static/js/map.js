let map;
let drawnItems;
let currentBounds = null;

/**
 * 汉化 Leaflet.draw 的按钮标题与提示条。
 *
 * 页面是 lang="zh-CN"，但 Leaflet.draw 1.0.4 的文案全部硬编码在
 * L.drawLocal 里，出厂是英文。
 *
 * 两条约束：
 * 1. **必须在 `new L.Control.Draw(...)` 之前调用。** 按钮的 title 是在
 *    Toolbar.addToolbar() 里一次性读走的，控件建好之后再改 L.drawLocal
 *    不会回写到已有的 DOM 上。
 * 2. **键名写错不会报错，只会静默不生效**（给一个不存在的对象赋值属性是
 *    合法 JS）。下面每个键都对着 CDN 上 leaflet.draw 1.0.4 的
 *    L.drawLocal 原始定义逐条核对过；
 *    tests/test_css_contract.py::test_draw_locale_keys_exist_in_pinned_build
 *    钉住这份键名清单，防止后来改键时打错字。
 *
 * 只覆盖本项目真正会出现的文案：map.js 只启用了 rectangle，
 * polyline / polygon / circle / marker 那几组以及 draw.toolbar.finish /
 * draw.toolbar.undo（多点图形才用得到）在这里是不可达的，故意不翻。
 */
function localizeDrawControl() {
    if (!window.L || !L.drawLocal) {
        return false;
    }
    // 键路径一律写全 `L.drawLocal.x.y.z`，不用局部别名 —— 上面第 2 条说的
    // 那条测试是**静态**解析这些路径的，走别名它就看不见了。
    L.drawLocal.draw.toolbar.actions.title = '取消绘制';
    L.drawLocal.draw.toolbar.actions.text = '取消';
    L.drawLocal.draw.toolbar.buttons.rectangle = '绘制矩形选区';
    L.drawLocal.draw.handlers.rectangle.tooltip.start = '按住并拖动鼠标绘制矩形';
    L.drawLocal.draw.handlers.simpleshape.tooltip.end = '松开鼠标完成绘制';

    L.drawLocal.edit.toolbar.actions.save.title = '保存修改';
    L.drawLocal.edit.toolbar.actions.save.text = '保存';
    L.drawLocal.edit.toolbar.actions.cancel.title = '取消编辑，放弃所有修改';
    L.drawLocal.edit.toolbar.actions.cancel.text = '取消';
    L.drawLocal.edit.toolbar.actions.clearAll.title = '清除所有选区';
    L.drawLocal.edit.toolbar.actions.clearAll.text = '全部清除';

    // editDisabled / removeDisabled 是**首屏默认态**的按钮提示（还没画选区时
    // 「编辑」「删除」就是禁用的），漏了它们等于最常见的那个状态还是英文。
    L.drawLocal.edit.toolbar.buttons.edit = '编辑选区';
    L.drawLocal.edit.toolbar.buttons.editDisabled = '没有可编辑的选区';
    L.drawLocal.edit.toolbar.buttons.remove = '删除选区';
    L.drawLocal.edit.toolbar.buttons.removeDisabled = '没有可删除的选区';

    L.drawLocal.edit.handlers.edit.tooltip.text = '拖动顶点或图形以修改选区';
    L.drawLocal.edit.handlers.edit.tooltip.subtext = '点击「取消」放弃修改';
    L.drawLocal.edit.handlers.remove.tooltip.text = '点击选区将其删除';
    return true;
}

function initMap(config) {
    const centerLat = parseFloat(config.map_center_lat || 39.9);
    const centerLng = parseFloat(config.map_center_lng || 116.4);
    const initialZoom = parseInt(config.map_initial_zoom || 10);

    map = L.map('map').setView([centerLat, centerLng], initialZoom);

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '© OpenStreetMap contributors'
    }).addTo(map);

    drawnItems = new L.FeatureGroup();
    map.addLayer(drawnItems);

    // 必须在 new L.Control.Draw 之前——按钮 title 是建控件时一次性读走的
    localizeDrawControl();

    const drawControl = new L.Control.Draw({
        draw: {
            rectangle: {
                shapeOptions: {
                    color: '#2dd4bf',
                    weight: 3,
                    fillOpacity: 0.1
                }
            },
            polygon: false,
            circle: false,
            marker: false,
            polyline: false,
            circlemarker: false
        },
        edit: {
            featureGroup: drawnItems,
            remove: true
        }
    });
    map.addControl(drawControl);

    map.on(L.Draw.Event.CREATED, function(event) {
        drawnItems.clearLayers();
        const layer = event.layer;

        layer.setStyle({
            color: '#2dd4bf',
            weight: 3,
            fillOpacity: 0.15,
            fillColor: '#5eead4'
        });

        drawnItems.addLayer(layer);

        const bounds = layer.getBounds();
        currentBounds = {
            north: bounds.getNorth(),
            south: bounds.getSouth(),
            east: bounds.getEast(),
            west: bounds.getWest()
        };

        updateBoundsInfo();
        refreshSubmitButtonState();

        const btn = document.getElementById('createTaskBtn');
        btn.style.animation = 'pulse 0.5s ease-in-out';
        setTimeout(() => {
            btn.style.animation = '';
        }, 500);
    });

    map.on(L.Draw.Event.DELETED, function() {
        currentBounds = null;
        updateBoundsInfo();
        refreshSubmitButtonState();
    });

    // 拖角 / 整体拖动时实时同步，用户不必点保存就能看到四至变化
    map.on(L.Draw.Event.EDITRESIZE, syncBoundsFromDrawnItems);
    map.on(L.Draw.Event.EDITMOVE, syncBoundsFromDrawnItems);

    // 点「保存」后确认一次
    map.on(L.Draw.Event.EDITED, syncBoundsFromDrawnItems);

    // 退出编辑模式。leaflet.draw 1.0.4 在取消时先 revertLayers() 还原图形、
    // 之后才 fire EDITSTOP，所以这里重读 bounds 对保存和取消都正确。
    map.on(L.Draw.Event.EDITSTOP, syncBoundsFromDrawnItems);

    // 删除模式结束后同样重读（DELETED 只在真的删了东西时触发）
    map.on(L.Draw.Event.DELETESTOP, syncBoundsFromDrawnItems);
}

function initDownloadTypeToggle() {
    const typeEl = document.getElementById('downloadType');
    if (!typeEl) return;

    const zoomRow = document.getElementById('zoomMin')?.closest('.row');
    const outputFormatField = document.getElementById('outputFormat')?.closest('.mb-3');

    const mapStyleField = document.getElementById('mapStyleField');

    const demOptions = document.getElementById('demOptions');

    const localOptions = document.getElementById('localTerrainOptions');

    const contourOptions = document.getElementById('contourOptions');

    function apply() {
        const t = typeEl.value;
        const isMap = t === 'map';
        const isDem = t === 'dem';
        const isLocal = t === 'local_terrain';
        const isContour = t === 'contour';
        // Zoom range is needed by map and contour; hidden for dem/local_terrain.
        if (zoomRow) zoomRow.style.display = (isDem || isLocal) ? 'none' : '';
        // Output format (tiles/stitch) is map-only.
        if (outputFormatField) outputFormatField.style.display = isMap ? '' : 'none';
        if (mapStyleField) mapStyleField.style.display = isMap ? '' : 'none';
        if (demOptions) demOptions.style.display = isDem ? '' : 'none';
        if (localOptions) localOptions.style.display = isLocal ? '' : 'none';
        if (contourOptions) contourOptions.style.display = isContour ? '' : 'none';

        const boundsInfo = document.getElementById('boundsInfo');
        if (boundsInfo) boundsInfo.style.display = isLocal ? 'none' : '';

        // Contour writes tiles to downloads/dem by default; the path field is
        // hidden because the contour branch intentionally does NOT send it.
        const outputPath = document.getElementById('outputPath');
        if (outputPath) {
            outputPath.closest('.mb-3').style.display = (isLocal || isContour) ? 'none' : '';
            if (!outputPath.dataset.userEdited) {
                outputPath.value = isDem ? './downloads/dem' : './downloads/map';
            }
        }

        refreshSubmitButtonState();
    }

    typeEl.addEventListener('change', apply);

    const outputPath = document.getElementById('outputPath');
    if (outputPath) {
        outputPath.addEventListener('input', () => {
            outputPath.dataset.userEdited = '1';
        });
    }

    apply();
}

// 从 drawnItems 里当前的图层重新读取 bbox。
// 编辑（拖角/拖动/保存/取消）之后统一走这里，保证右侧四至和地图上看到的一致。
//
// 前提：eachLayer 遍历取的是**最后一个**有 getBounds 的图层，也就是隐含假设
// drawnItems 里最多只有一个选区。当前 L.Draw.Event.CREATED 分支会先
// clearLayers() 再 addLayer()，这个假设成立。将来若支持多选区，这里必须改成
// 合并所有图层的 bounds（或按选中态取）。
//
// 幂等：重复调用无副作用——DELETESTOP 和 DELETED 会都触发，两次读到同样的结果。
function syncBoundsFromDrawnItems() {
    let found = null;
    if (drawnItems) {
        drawnItems.eachLayer(function (layer) {
            if (typeof layer.getBounds === 'function') {
                found = layer.getBounds();
            }
        });
    }

    if (found) {
        currentBounds = {
            north: found.getNorth(),
            south: found.getSouth(),
            east: found.getEast(),
            west: found.getWest()
        };
    } else {
        currentBounds = null;
    }

    updateBoundsInfo();
    refreshSubmitButtonState();
}

// 提交按钮的启用条件集中在这里，避免各处只加不减导致状态残留。
// 本地高程切片模式没有 bbox，所以这里无条件启用（不检查文件）——
// 文件是否已选在提交时由 submitLocalTerrain() 校验。其余模式必须先框选。
function refreshSubmitButtonState() {
    const btn = document.getElementById('createTaskBtn');
    if (!btn) return;
    const type = document.getElementById('downloadType')?.value;
    if (type === 'local_terrain') {
        btn.disabled = false;
    } else {
        btn.disabled = !currentBounds;
    }
}

// 任务创建成功后复位表单。
// clearBounds=false 用于本地高程切片：该模式本来就没有 bbox，
// 清空 drawnItems 会把用户为下一个任务画好的框也一起删掉。
function resetForm({ clearBounds = true } = {}) {
    const form = document.getElementById('downloadForm');
    if (form) form.reset();

    const outputPath = document.getElementById('outputPath');
    if (outputPath) delete outputPath.dataset.userEdited;

    if (clearBounds) {
        if (drawnItems) drawnItems.clearLayers();
        currentBounds = null;
        updateBoundsInfo();
    }

    // 让 apply() 重新按当前类型摆好字段可见性和默认路径
    const typeEl = document.getElementById('downloadType');
    if (typeEl) typeEl.dispatchEvent(new Event('change'));

    refreshSubmitButtonState();
}

function updateBoundsInfo() {
    const boundsInfo = document.getElementById('boundsInfo');
    if (currentBounds) {
        boundsInfo.innerHTML = `
            <small style="font-family: var(--font-mono); line-height: 1.6;">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display: inline-block; vertical-align: middle; margin-right: 4px;">
                    <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline>
                </svg>
                <strong>选中区域：</strong><br>
                <span style="color: var(--color-accent-hover);">▲</span> 北: ${currentBounds.north.toFixed(6)}<br>
                <span style="color: var(--color-accent-hover);">▼</span> 南: ${currentBounds.south.toFixed(6)}<br>
                <span style="color: var(--color-accent-hover);">▶</span> 东: ${currentBounds.east.toFixed(6)}<br>
                <span style="color: var(--color-accent-hover);">◀</span> 西: ${currentBounds.west.toFixed(6)}
            </small>
        `;
        boundsInfo.style.background = 'rgba(59, 130, 246, 0.1)';
        boundsInfo.style.borderColor = 'var(--color-info)';
    } else {
        boundsInfo.innerHTML = `
            <small>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display: inline-block; vertical-align: middle; margin-right: 4px;">
                    <circle cx="12" cy="12" r="10"></circle>
                    <line x1="12" y1="16" x2="12" y2="12"></line>
                    <line x1="12" y1="8" x2="12.01" y2="8"></line>
                </svg>
                请在地图上框选下载区域
            </small>
        `;
        boundsInfo.style.background = 'rgba(59, 130, 246, 0.1)';
        boundsInfo.style.borderColor = 'var(--color-info)';
    }
}

document.getElementById('downloadForm').addEventListener('submit', async function(e) {
    e.preventDefault();

    const downloadType = document.getElementById('downloadType')?.value || 'map';

    // Local terrain uploads have no bbox; handle separately and return early.
    if (downloadType === 'local_terrain') {
        await submitLocalTerrain();
        return;
    }

    if (!currentBounds) {
        showNotification('请先在地图上框选下载区域', 'warning');
        return;
    }

    // Contour is a one-stop pipeline: create then immediately start (one click).
    if (downloadType === 'contour') {
        await submitContour();
        return;
    }

    let taskData;
    let apiUrl;
    if (downloadType === 'dem') {
        taskData = {
            name: document.getElementById('taskName').value,
            north: currentBounds.north,
            south: currentBounds.south,
            east: currentBounds.east,
            west: currentBounds.west,
            dataset: document.getElementById('demDataset')?.value || 'COP-DEM-GLO-30',
            output_path: document.getElementById('outputPath').value,
            download_num: document.getElementById('demDownloadNum')?.checked ? 'true' : 'false',
            download_swb: document.getElementById('demDownloadSwb')?.checked ? 'true' : 'false'
        };
        apiUrl = '/api/dem/tasks';
    } else {
        taskData = {
            name: document.getElementById('taskName').value,
            north: currentBounds.north,
            south: currentBounds.south,
            east: currentBounds.east,
            west: currentBounds.west,
            zoom_min: parseInt(document.getElementById('zoomMin').value),
            zoom_max: parseInt(document.getElementById('zoomMax').value),
            style: document.getElementById('mapStyle').value,
            output_format: document.getElementById('outputFormat').value,
            output_path: document.getElementById('outputPath').value
        };
        apiUrl = '/api/tasks';
    }

    const btn = document.getElementById('createTaskBtn');
    const originalText = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display: inline-block; vertical-align: middle; margin-right: 6px; animation: spin 1s linear infinite;">
            <circle cx="12" cy="12" r="10"></circle>
        </svg>
        创建中...
    `;

    try {
        const response = await fetch(apiUrl, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(taskData)
        });

        const result = await response.json();

        if (response.ok) {
            showNotification('任务创建成功！ID: ' + result.task_id, 'success');
            resetForm();
            loadActiveTasks();
        } else {
            showNotification('创建任务失败: ' + result.error, 'danger');
        }
    } catch (error) {
        showNotification('创建任务失败: ' + error.message, 'danger');
    } finally {
        btn.innerHTML = originalText;
        refreshSubmitButtonState();
    }
});

function showNotification(message, type = 'info') {
    // 委托给全局 showToast（ui.js）。保留函数名，map.js 内 13 处调用无需改动。
    if (window.showToast) {
        return window.showToast(message, type);
    }
    alert(message); // 兜底：ui.js 未加载时退回原生
}

const style = document.createElement('style');
style.textContent = `
    @keyframes spin {
        from { transform: rotate(0deg); }
        to { transform: rotate(360deg); }
    }
    @keyframes fadeOut {
        from { opacity: 1; transform: translateY(0); }
        to { opacity: 0; transform: translateY(-20px); }
    }
`;
document.head.appendChild(style);

// ---------------------------------------------------------------------------
// Contour: one-stop create -> start, plus a map preview overlay for finished
// contour tasks. The shared active-task list (tasks.js) removes cards on
// completion and has no contour branch, so the preview lives here in its own
// panel and listens to the shared socket independently.
// ---------------------------------------------------------------------------

// Mirror of the backend `count_tiles` slippy-map math, used purely client-side
// to warn before a heavy contour render. Contour rendering is much slower than a
// plain tile download, so a large bbox × high zoom can run for a very long time.
function estimateContourTiles(bounds, zMin, zMax) {
    const lon2x = (lon, z) => Math.floor(((lon + 180) / 360) * Math.pow(2, z));
    const lat2y = (lat, z) => {
        const r = (lat * Math.PI) / 180;
        return Math.floor(((1 - Math.asinh(Math.tan(r)) / Math.PI) / 2) * Math.pow(2, z));
    };
    let total = 0;
    for (let z = zMin; z <= zMax; z++) {
        const x0 = lon2x(bounds.west, z), x1 = lon2x(bounds.east, z);
        const y0 = lat2y(bounds.north, z), y1 = lat2y(bounds.south, z);
        total += (Math.abs(x1 - x0) + 1) * (Math.abs(y1 - y0) + 1);
    }
    return total;
}

// Expand the framed bbox to the union of whole 1° DEM granule tiles (mirror of
// services/dem_granules.coverage_bbox). Contours render over the whole
// downloaded DEM, not just the framed box, so the estimate must use this.
function coverageBounds(b) {
    const eps = 1e-9;
    return {
        south: Math.floor(b.south),
        north: Math.floor(b.north - eps) + 1,
        west: Math.floor(b.west),
        east: Math.floor(b.east - eps) + 1,
    };
}

async function submitContour() {
    const interval = parseFloat(document.getElementById('contourInterval').value) || 50;
    const zMin = parseInt(document.getElementById('zoomMin').value, 10);
    const zMax = parseInt(document.getElementById('zoomMax').value, 10);

    const bgTransparent = document.getElementById('contourBackgroundTransparent').checked;
    const background = bgTransparent ? 'transparent' : (document.getElementById('contourBackground').value || '#faf6ec');

    // Warn before a large render: contour rendering is slow, so confirm heavy jobs.
    // Estimate over the whole DEM coverage (what actually renders), not the box.
    const approx = estimateContourTiles(coverageBounds(currentBounds), zMin, zMax);
    if (approx > 20000) {
        const ok = await showConfirm(
            `预计渲染约 ${approx} 个等高线瓦片，等高线渲染较慢，可能耗时较久。确认继续？`,
            { title: '确认渲染', confirmText: '继续', danger: true }
        );
        if (!ok) return;
    }

    const body = {
        name: document.getElementById('taskName').value || '等高线瓦片',
        north: currentBounds.north,
        south: currentBounds.south,
        east: currentBounds.east,
        west: currentBounds.west,
        contour_interval: interval,
        background: background,
        dataset: document.getElementById('contourDataset')?.value || 'COP-DEM-GLO-30',
        terrain_shade: document.getElementById('contourTerrainShade')?.checked ?? true,
        water: document.getElementById('contourWater')?.checked ?? true,
        zoom_min: zMin,
        zoom_max: zMax,
        // NOTE: intentionally NO output_path — backend defaults to downloads/dem
        // so the /contour/<id>/{z}/{x}/{y}.png tile route can find the tiles.
    };

    const btn = document.getElementById('createTaskBtn');
    const original = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '创建中...';
    try {
        const createResp = await fetch('/api/contour/tasks', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const created = await createResp.json();
        if (!createResp.ok) {
            showNotification('创建失败: ' + (created.error || createResp.status), 'danger');
            return;
        }
        await fetch(`/api/contour/tasks/${created.task_id}/start`, { method: 'POST' });
        showNotification('等高线任务已开始（自动下 DEM → 渲染瓦片）', 'success');
        resetForm();
        loadActiveTasks();
    } catch (err) {
        showNotification('创建失败: ' + err.message, 'danger');
    } finally {
        btn.innerHTML = original;
        refreshSubmitButtonState();
    }
}

// --- Contour preview overlay ------------------------------------------------

let contourPreviewLayer = null;
let contourPreviewActiveId = null;

function toggleContourPreview(taskId, zoomMax) {
    // Same task toggled again -> turn it off.
    if (contourPreviewLayer && contourPreviewActiveId === taskId) {
        map.removeLayer(contourPreviewLayer);
        contourPreviewLayer = null;
        contourPreviewActiveId = null;
        updateContourPreviewButtons();
        return;
    }
    // Switching to a different task -> drop the old overlay first.
    if (contourPreviewLayer) {
        map.removeLayer(contourPreviewLayer);
        contourPreviewLayer = null;
        contourPreviewActiveId = null;
    }
    contourPreviewLayer = L.tileLayer(`/contour/${taskId}/{z}/{x}/{y}.png`, {
        opacity: 0.9,
        maxNativeZoom: zoomMax || undefined,
    }).addTo(map);
    contourPreviewActiveId = taskId;
    updateContourPreviewButtons();
}

// Completed contour tasks available for preview: id -> {name, zoom_max}.
const contourPreviewTasks = new Map();

function contourPreviewPanel() {
    let panel = document.getElementById('contourPreviewPanel');
    if (!panel) {
        const mapEl = document.getElementById('map');
        if (!mapEl) return null;
        panel = document.createElement('div');
        panel.id = 'contourPreviewPanel';
        panel.style.marginTop = '0.75rem';
        // Place the panel right under the map card body.
        mapEl.parentNode.appendChild(panel);
    }
    return panel;
}

function updateContourPreviewButtons() {
    const panel = contourPreviewPanel();
    if (!panel) return;
    if (contourPreviewTasks.size === 0) {
        panel.innerHTML = '';
        return;
    }
    const rows = [];
    contourPreviewTasks.forEach((info, id) => {
        const active = contourPreviewActiveId === id;
        rows.push(`
            <button type="button"
                    class="btn btn-sm ${active ? 'btn-primary' : 'btn-outline-primary'}"
                    style="margin: 0 6px 6px 0;"
                    onclick="toggleContourPreview(${id}, ${info.zoom_max || 'null'})">
                ${active ? '隐藏预览' : '在地图上预览'}：${info.name || ('等高线 #' + id)}
            </button>
        `);
    });
    panel.innerHTML = `
        <div class="alert alert-info" style="margin-bottom:0;">
            <small><strong>已完成的等高线瓦片</strong></small><br>
            ${rows.join('')}
        </div>
    `;
}

async function registerCompletedContourTask(taskId) {
    // Pull zoom_max + name for the just-finished task from the list endpoint.
    try {
        const resp = await fetch('/api/contour/tasks');
        const data = await resp.json();
        const task = (data.tasks || []).find(t => t.id === taskId);
        if (task) {
            contourPreviewTasks.set(taskId, { name: task.name, zoom_max: task.zoom_max });
        } else {
            contourPreviewTasks.set(taskId, { name: '等高线 #' + taskId, zoom_max: null });
        }
    } catch (e) {
        contourPreviewTasks.set(taskId, { name: '等高线 #' + taskId, zoom_max: null });
    }
    updateContourPreviewButtons();
}

// Hook the shared socket (created in tasks.js' initTasks). Called from the page
// init block after initTasks(), so `socket` is already assigned.
function initContourPreview() {
    if (typeof socket === 'undefined' || !socket) return;
    socket.on('task_completed', function(data) {
        if (data && data.task_type === 'contour') {
            registerCompletedContourTask(data.task_id);
        }
    });
    // On page load, surface any already-completed contour tasks for preview.
    fetch('/api/contour/tasks').then(r => r.json()).then(data => {
        (data.tasks || []).forEach(t => {
            if (t.status === 'completed') {
                contourPreviewTasks.set(t.id, { name: t.name, zoom_max: t.zoom_max });
            }
        });
        updateContourPreviewButtons();
    }).catch(() => {});
}

async function submitLocalTerrain() {
    const fileInput = document.getElementById('localTerrainFiles');
    const files = fileInput?.files;
    if (!files || files.length === 0) {
        showNotification('请先选择至少一个 .tif/.tiff 文件', 'warning');
        return;
    }

    const fd = new FormData();
    fd.append('name', document.getElementById('taskName').value || '本地高程切片');
    fd.append('maxzoom', document.getElementById('localTerrainMaxzoom')?.value || '14');
    for (const f of files) {
        fd.append('files', f);
    }

    const btn = document.getElementById('createTaskBtn');
    btn.disabled = true;
    const original = btn.innerHTML;
    btn.innerHTML = '上传中...';
    try {
        const resp = await fetch('/api/terrain/local/tasks', { method: 'POST', body: fd });
        const result = await resp.json();
        if (resp.ok) {
            showNotification('上传成功，已开始切片！ID: ' + result.task_id, 'success');
            resetForm({ clearBounds: false });
            loadActiveTasks();
        } else {
            showNotification('上传失败: ' + (result.error || resp.status), 'danger');
        }
    } catch (err) {
        showNotification('上传失败: ' + err.message, 'danger');
    } finally {
        btn.innerHTML = original;
        refreshSubmitButtonState();
    }
}
