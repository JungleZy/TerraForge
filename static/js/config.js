function initConfig() {
    document.getElementById('configForm').addEventListener('submit', saveConfig);
}

async function saveConfig(e) {
    e.preventDefault();

    const configData = {
        default_save_path: document.getElementById('default_save_path').value,
        default_style: document.getElementById('default_style').value,
        default_zoom_min: document.getElementById('default_zoom_min').value,
        default_zoom_max: document.getElementById('default_zoom_max').value,
        concurrent_downloads: document.getElementById('concurrent_downloads').value,
        request_timeout: document.getElementById('request_timeout').value,
        max_retries: document.getElementById('max_retries').value,
        proxy_url: document.getElementById('proxy_url').value,
        tile_servers: document.getElementById('tile_servers').value,
        cache_enabled: document.getElementById('cache_enabled').checked ? 'true' : 'false',
        cache_max_size_mb: document.getElementById('cache_max_size_mb').value,
        gdal_compression: document.getElementById('gdal_compression').value,
        gdal_resampling: document.getElementById('gdal_resampling').value,
        history_retention_days: document.getElementById('history_retention_days').value,
        map_center_lat: document.getElementById('map_center_lat').value,
        map_center_lng: document.getElementById('map_center_lng').value,
        map_initial_zoom: document.getElementById('map_initial_zoom').value
    };

    try {
        const response = await fetch('/api/config', {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(configData)
        });

        const result = await response.json();

        if (response.ok) {
            alert('配置保存成功！');
        } else {
            alert('保存失败: ' + result.error);
        }
    } catch (error) {
        alert('保存失败: ' + error.message);
    }
}

async function resetConfig() {
    if (!confirm('确定要重置所有配置为默认值吗？')) {
        return;
    }

    location.reload();
}
