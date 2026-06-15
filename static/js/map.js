let map;
let drawnItems;
let currentBounds = null;

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
        document.getElementById('createTaskBtn').disabled = false;

        const btn = document.getElementById('createTaskBtn');
        btn.style.animation = 'pulse 0.5s ease-in-out';
        setTimeout(() => {
            btn.style.animation = '';
        }, 500);
    });

    map.on(L.Draw.Event.DELETED, function() {
        currentBounds = null;
        updateBoundsInfo();
        document.getElementById('createTaskBtn').disabled = true;
    });
}

function initDownloadTypeToggle() {
    const typeEl = document.getElementById('downloadType');
    if (!typeEl) return;

    const mapFields = [
        document.getElementById('zoomMin')?.closest('.row'),
        document.getElementById('outputFormat')?.closest('.mb-3'),
    ].filter(Boolean);

    const mapStyleField = document.getElementById('mapStyleField');

    const demOptions = document.getElementById('demOptions');

    const localOptions = document.getElementById('localTerrainOptions');

    function apply() {
        const t = typeEl.value;
        const isMap = t === 'map';
        const isDem = t === 'dem';
        const isLocal = t === 'local_terrain';
        mapFields.forEach(el => el.style.display = (isDem || isLocal) ? 'none' : '');
        if (mapStyleField) mapStyleField.style.display = isMap ? '' : 'none';
        if (demOptions) demOptions.style.display = isDem ? '' : 'none';
        if (localOptions) localOptions.style.display = isLocal ? '' : 'none';

        const boundsInfo = document.getElementById('boundsInfo');
        if (boundsInfo) boundsInfo.style.display = isLocal ? 'none' : '';

        const outputPath = document.getElementById('outputPath');
        if (outputPath) {
            outputPath.closest('.mb-3').style.display = isLocal ? 'none' : '';
            if (!outputPath.dataset.userEdited) {
                outputPath.value = isDem ? './downloads/dem' : './downloads/map';
            }
        }

        const btn = document.getElementById('createTaskBtn');
        if (btn && isLocal) btn.disabled = false;
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

function updateBoundsInfo() {
    const boundsInfo = document.getElementById('boundsInfo');
    if (currentBounds) {
        boundsInfo.innerHTML = `
            <small style="font-family: var(--font-mono); line-height: 1.6;">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display: inline-block; vertical-align: middle; margin-right: 4px;">
                    <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline>
                </svg>
                <strong>选中区域：</strong><br>
                <span style="color: var(--color-accent-warm);">▲</span> 北: ${currentBounds.north.toFixed(6)}<br>
                <span style="color: var(--color-accent-warm);">▼</span> 南: ${currentBounds.south.toFixed(6)}<br>
                <span style="color: var(--color-accent-warm);">▶</span> 东: ${currentBounds.east.toFixed(6)}<br>
                <span style="color: var(--color-accent-warm);">◀</span> 西: ${currentBounds.west.toFixed(6)}
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

    let taskData;
    let apiUrl;
    if (downloadType === 'dem') {
        taskData = {
            name: document.getElementById('taskName').value,
            north: currentBounds.north,
            south: currentBounds.south,
            east: currentBounds.east,
            west: currentBounds.west,
            dataset: 'ASTGTM.003',
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
            document.getElementById('downloadForm').reset();
            drawnItems.clearLayers();
            currentBounds = null;
            updateBoundsInfo();
            document.getElementById('createTaskBtn').disabled = true;
            loadActiveTasks();
        } else {
            showNotification('创建任务失败: ' + result.error, 'danger');
        }
    } catch (error) {
        showNotification('创建任务失败: ' + error.message, 'danger');
    } finally {
        btn.disabled = false;
        btn.innerHTML = originalText;
    }
});

function showNotification(message, type = 'info') {
    const notification = document.createElement('div');
    notification.className = `alert alert-${type}`;
    notification.style.position = 'fixed';
    notification.style.top = '80px';
    notification.style.right = '20px';
    notification.style.zIndex = '9999';
    notification.style.minWidth = '300px';
    notification.style.animation = 'fadeInUp 0.3s ease-out';
    notification.innerHTML = message;

    document.body.appendChild(notification);

    setTimeout(() => {
        notification.style.animation = 'fadeOut 0.3s ease-out';
        setTimeout(() => {
            document.body.removeChild(notification);
        }, 300);
    }, 3000);
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
            document.getElementById('downloadForm').reset();
            loadActiveTasks();
        } else {
            showNotification('上传失败: ' + (result.error || resp.status), 'danger');
        }
    } catch (err) {
        showNotification('上传失败: ' + err.message, 'danger');
    } finally {
        btn.disabled = false;
        btn.innerHTML = original;
    }
}
