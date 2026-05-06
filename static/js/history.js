let historyMap;
let currentPage = 1;
let allTasks = [];

function initHistory() {
    initHistoryMap();
    loadHistory(1);

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

async function loadHistory(page = 1) {
    try {
        const response = await fetch(`/api/history?page=${page}&per_page=20`);
        const data = await response.json();

        allTasks = data.tasks;
        renderHistoryTable(data.tasks);
        renderPagination(data.page, Math.ceil(data.total / data.per_page));
        renderHistoryMap(data.tasks);
    } catch (error) {
        console.error('Failed to load history:', error);
        document.getElementById('historyTableBody').innerHTML =
            '<tr><td colspan="9" class="text-center text-danger">加载失败</td></tr>';
    }
}

function renderHistoryTable(tasks) {
    const tbody = document.getElementById('historyTableBody');

    if (tasks.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" class="text-center">暂无历史记录</td></tr>';
        return;
    }

    tbody.innerHTML = tasks.map(task => `
        <tr>
            <td>${task.id}</td>
            <td>${task.name}</td>
            <td><span class="badge bg-${getStatusColor(task.status)}">${getStatusText(task.status)}</span></td>
            <td>
                <small>
                    N:${task.north.toFixed(4)}, S:${task.south.toFixed(4)}<br>
                    E:${task.east.toFixed(4)}, W:${task.west.toFixed(4)}
                </small>
            </td>
            <td>${task.zoom_min}-${task.zoom_max}</td>
            <td>${getStyleText(task.style)}</td>
            <td>${task.downloaded_tiles}/${task.total_tiles}</td>
            <td><small>${formatDate(task.completed_at)}</small></td>
            <td>
                <button class="btn btn-sm btn-info" onclick="viewTaskDetails(${task.id})">详情</button>
                <button class="btn btn-sm btn-danger" onclick="deleteTask(${task.id})">删除</button>
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

    tasks.forEach(task => {
        const bounds = [[task.south, task.west], [task.north, task.east]];
        const color = task.status === 'completed' ? 'green' :
                     task.status === 'failed' ? 'red' : 'orange';

        const rectangle = L.rectangle(bounds, {
            color: color,
            weight: 2,
            fillOpacity: 0.2
        }).addTo(historyMap);

        rectangle.bindPopup(`
            <strong>${task.name}</strong><br>
            状态: ${getStatusText(task.status)}<br>
            瓦片: ${task.downloaded_tiles}/${task.total_tiles}
        `);
    });

    if (tasks.length > 0) {
        const allBounds = tasks.map(t => [[t.south, t.west], [t.north, t.east]]);
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
        'm': '标准',
        's': '卫星',
        'y': '卫星+标注',
        'h': '道路',
        't': '地形'
    };
    return styles[style] || style;
}

function formatDate(dateStr) {
    if (!dateStr) return '-';
    const date = new Date(dateStr);
    return date.toLocaleString('zh-CN');
}

async function viewTaskDetails(taskId) {
    try {
        const response = await fetch(`/api/tasks/${taskId}`);
        const task = await response.json();

        alert(`任务详情:\n\n` +
              `ID: ${task.id}\n` +
              `名称: ${task.name}\n` +
              `状态: ${getStatusText(task.status)}\n` +
              `总瓦片: ${task.total_tiles}\n` +
              `已下载: ${task.downloaded_tiles}\n` +
              `失败: ${task.failed_tiles}\n` +
              `输出路径: ${task.output_path}`);
    } catch (error) {
        alert('获取任务详情失败');
    }
}

async function deleteTask(taskId) {
    if (!confirm('确定要删除这个任务吗？')) {
        return;
    }

    try {
        const response = await fetch(`/api/tasks/${taskId}`, {
            method: 'DELETE'
        });

        if (response.ok) {
            alert('任务已删除');
            loadHistory(currentPage);
        } else {
            alert('删除失败');
        }
    } catch (error) {
        alert('删除失败: ' + error.message);
    }
}
