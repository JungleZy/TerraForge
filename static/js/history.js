let historyMap;
let currentPage = 1;
let allTasks = [];

function initHistory() {
    initHistoryMap();
    loadHistory(1);
    loadStats();

    document.getElementById('searchInput').addEventListener('input', function(e) {
        filterTasks(e.target.value);
    });
}

function initHistoryMap() {
    historyMap = L.map('historyMap').setView([39.9, 116.4], 5);

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '© OpenStreetMap contributors'
    }).addTo(historyMap);
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
        const response = await fetch(`/api/history_all?page=${page}&per_page=20`, { cache: 'no-store' });
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
        document.getElementById('historyTableBody').innerHTML =
            '<tr><td colspan="9" class="text-center text-danger">加载失败</td></tr>';
    }
}

function renderHistoryTable(tasks) {
    const tbody = document.getElementById('historyTableBody');

    if (tasks.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="9" class="text-center" style="padding: 3rem;">
                    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="opacity: 0.3; margin-bottom: 1rem;">
                        <circle cx="12" cy="12" r="10"></circle>
                        <line x1="12" y1="8" x2="12" y2="12"></line>
                        <line x1="12" y1="16" x2="12.01" y2="16"></line>
                    </svg>
                    <p style="color: var(--color-text-muted); margin: 0;">暂无历史记录</p>
                </td>
            </tr>
        `;
        return;
    }

    const statusIcons = {
        'completed': '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display: inline-block; vertical-align: middle; margin-right: 4px;"><polyline points="20 6 9 17 4 12"></polyline></svg>',
        'failed': '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display: inline-block; vertical-align: middle; margin-right: 4px;"><circle cx="12" cy="12" r="10"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line></svg>',
        'cancelled': '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display: inline-block; vertical-align: middle; margin-right: 4px;"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>'
    };

    tbody.innerHTML = tasks.map(task => `
        <tr>
            <td style="font-family: var(--font-mono);">${task.id}</td>
            <td style="font-weight: 500;">${task.name}</td>
            <td>
                <span class="badge bg-${getStatusColor(task.status)}" style="display: inline-flex; align-items: center;">
                    ${statusIcons[task.status] || ''}
                    ${getStatusText(task.status)}
                </span>
            </td>
            <td>
                <small style="font-family: var(--font-mono); font-size: 0.8rem; line-height: 1.4;">
                    ${task.north == null ? '<span style="color: var(--color-text-muted);">本地文件</span>' : `
                    <span style="color: var(--color-accent-warm);">▲</span> ${task.north.toFixed(4)},
                    <span style="color: var(--color-accent-warm);">▼</span> ${task.south.toFixed(4)}<br>
                    <span style="color: var(--color-accent-warm);">▶</span> ${task.east.toFixed(4)},
                    <span style="color: var(--color-accent-warm);">◀</span> ${task.west.toFixed(4)}`}
                </small>
            </td>
            <td style="font-family: var(--font-mono);">${(task.task_type === 'map' || task.task_type === 'contour') ? `${task.zoom_min}-${task.zoom_max}` : '-'}</td>
            <td>${(task.task_type === 'map' || task.task_type === 'contour') ? getStyleText(task.style) : (task.style || '-')}</td>
            <td style="font-family: var(--font-mono);">${task.downloaded}/${task.total}</td>
            <td><small style="font-family: var(--font-mono); font-size: 0.85rem;">${formatDate(task.completed_at)}</small></td>
            <td>
                <div style="display: flex; gap: 0.5rem;">
                    <button class="btn btn-sm btn-info" onclick="viewTaskDetails(${task.id}, '${task.task_type}')" title="查看详情">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <circle cx="12" cy="12" r="10"></circle>
                            <line x1="12" y1="16" x2="12" y2="12"></line>
                            <line x1="12" y1="8" x2="12.01" y2="8"></line>
                        </svg>
                    </button>
                    <button class="btn btn-sm btn-danger" onclick="deleteTask(${task.id}, '${task.task_type}')" title="删除任务">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <polyline points="3 6 5 6 21 6"></polyline>
                            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                        </svg>
                    </button>
                </div>
            </td>
        </tr>
    `).join('');
}

function renderPagination(currentPage, totalPages) {
    const pagination = document.getElementById('pagination');

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
    historyMap.eachLayer(layer => {
        if (layer instanceof L.Rectangle) {
            historyMap.removeLayer(layer);
        }
    });

    // Local-terrain tasks have no bbox; only map/dem tasks appear on the map.
    const geoTasks = tasks.filter(t => t.north != null && t.south != null && t.east != null && t.west != null);

    geoTasks.forEach(task => {
        const bounds = [[task.south, task.west], [task.north, task.east]];
        const color = task.status === 'completed' ? '#10b981' :
                     task.status === 'failed' ? '#ef4444' : '#60a5fa';

        const rectangle = L.rectangle(bounds, {
            color: color,
            weight: 3,
            fillOpacity: 0.15,
            fillColor: color
        }).addTo(historyMap);

        rectangle.bindPopup(`
            <div style="font-family: var(--font-display); min-width: 200px;">
                <strong style="color: var(--color-accent-warm); font-size: 1.1rem;">${task.name}</strong><br>
                <div style="margin-top: 0.5rem; font-size: 0.9rem;">
                    <strong>状态:</strong> ${getStatusText(task.status)}<br>
                    <strong>${task.task_type === 'dem' ? '文件' : '瓦片'}:</strong>
                    <span style="font-family: var(--font-mono);">${task.downloaded}/${task.total}</span>
                </div>
            </div>
        `);
    });

    if (geoTasks.length > 0) {
        const allBounds = geoTasks.map(t => [[t.south, t.west], [t.north, t.east]]);
        const group = L.featureGroup(allBounds.map(b => L.rectangle(b)));
        historyMap.fitBounds(group.getBounds());
    }
}

function filterTasks(searchTerm) {
    const filtered = allTasks.filter(task =>
        task.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
        task.id.toString().includes(searchTerm)
    );
    renderHistoryTable(filtered);
}

function getStatusColor(status) {
    const colors = {
        'completed': 'success',
        'failed': 'danger',
        'cancelled': 'dark'
    };
    return colors[status] || 'secondary';
}

function getStatusText(status) {
    const texts = {
        'completed': '已完成',
        'failed': '失败',
        'cancelled': '已取消'
    };
    return texts[status] || status;
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
    if (!dateStr) return '-';
    const date = new Date(dateStr);
    return date.toLocaleString('zh-CN');
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
        document.getElementById('detailStatus').innerHTML = `<span class="badge bg-${getStatusColor(task.status)}">${getStatusText(task.status)}</span>`;
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

        const progressColor = progress >= 100 ? 'success' :
                             progress >= 75 ? 'info' :
                             progress >= 50 ? 'primary' :
                             progress >= 25 ? 'warning' : 'danger';

        document.getElementById('detailProgress').innerHTML = `
            <div class="progress" style="height: 28px; margin-top: 0.5rem;">
                <div class="progress-bar bg-${progressColor}" role="progressbar" style="width: ${progress}%">
                    ${progress}%
                </div>
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

        const status = job.status || 'unknown';
        const color = status === 'completed' ? 'success'
                    : status === 'running' ? 'primary'
                    : status === 'failed' ? 'danger'
                    : 'secondary';
        statusEl.innerHTML = `<span class="badge bg-${color}">${status}</span>`;

        const outDir = job.output_dir || '-';
        const maxzoom = job.maxzoom ?? '-';
        infoEl.innerHTML = `
            <div>MaxZoom: ${maxzoom}</div>
            <div>Out: ${outDir}</div>
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

async function deleteTask(taskId, taskType = 'map') {
    if (!await showConfirm('确定要删除这个任务吗？', { title: '删除任务', danger: true })) {
        return;
    }

    try {
        const deleteUrl = taskType === 'dem' ? `/api/dem/tasks/${taskId}`
                        : taskType === 'local_terrain' ? `/api/terrain/local/tasks/${taskId}`
                        : taskType === 'contour' ? `/api/contour/tasks/${taskId}`
                        : `/api/tasks/${taskId}`;
        const response = await fetch(deleteUrl, { method: 'DELETE' });

        if (response.ok) {
            showToast('任务已删除', 'success');
            loadHistory(currentPage);
            loadStats();
        } else {
            showToast('删除失败', 'danger');
        }
    } catch (error) {
        showToast('删除失败: ' + error.message, 'danger');
    }
}
