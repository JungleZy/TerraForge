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
            rectangle: true,
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
    });

    map.on(L.Draw.Event.DELETED, function() {
        currentBounds = null;
        updateBoundsInfo();
        document.getElementById('createTaskBtn').disabled = true;
    });
}

function updateBoundsInfo() {
    const boundsInfo = document.getElementById('boundsInfo');
    if (currentBounds) {
        boundsInfo.innerHTML = `
            <small>
                <strong>选中区域：</strong><br>
                北: ${currentBounds.north.toFixed(6)}<br>
                南: ${currentBounds.south.toFixed(6)}<br>
                东: ${currentBounds.east.toFixed(6)}<br>
                西: ${currentBounds.west.toFixed(6)}
            </small>
        `;
    } else {
        boundsInfo.innerHTML = '<small>请在地图上框选下载区域</small>';
    }
}

document.getElementById('downloadForm').addEventListener('submit', async function(e) {
    e.preventDefault();

    if (!currentBounds) {
        alert('请先在地图上框选下载区域');
        return;
    }

    const taskData = {
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

    try {
        const response = await fetch('/api/tasks', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(taskData)
        });

        const result = await response.json();

        if (response.ok) {
            alert('任务创建成功！ID: ' + result.task_id);
            document.getElementById('downloadForm').reset();
            drawnItems.clearLayers();
            currentBounds = null;
            updateBoundsInfo();
            document.getElementById('createTaskBtn').disabled = true;
            loadActiveTasks();
        } else {
            alert('创建任务失败: ' + result.error);
        }
    } catch (error) {
        alert('创建任务失败: ' + error.message);
    }
});
