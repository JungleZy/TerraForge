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
        map_initial_zoom: document.getElementById('map_initial_zoom').value,
        map_tile_url: document.getElementById('map_tile_url').value.trim(),
        earthdata_username: document.getElementById('earthdata_username').value,
        earthdata_password: document.getElementById('earthdata_password').value
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
            showToast('配置保存成功！', 'success');
        } else {
            const detail = (result.errors && result.errors.length)
                ? result.errors.join('；') : result.error;
            showToast('保存失败: ' + detail, 'danger');
        }
    } catch (error) {
        showToast('保存失败: ' + error.message, 'danger');
    }
}

// 「验证通联」按钮：校验当前输入框里的底图瓦片地址（不需要先保存）。
// 结果内联显示在输入框下方，成功绿色 / 失败红色，不打断表单编辑。
async function verifyTileUrl() {
    const btn = document.getElementById('verifyTileUrlBtn');
    const result = document.getElementById('tileUrlVerifyResult');
    const url = document.getElementById('map_tile_url').value.trim();

    result.hidden = false;
    result.className = 'tile-url-verify-result';
    result.textContent = '正在验证…';
    btn.disabled = true;

    try {
        const response = await fetch('/api/config/verify_tile_url', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url })
        });
        const data = await response.json();

        if (response.ok && data.success) {
            result.classList.add('tile-url-verify-result--ok');
            result.textContent =
                `通联正常 · HTTP ${data.status_code} · ${data.content_type || '未知类型'}` +
                ` · ${data.elapsed_ms}ms（样例瓦片 ${data.tile}）`;
        } else {
            result.classList.add('tile-url-verify-result--fail');
            result.textContent = '验证失败：' + (data.error || ('HTTP ' + response.status));
        }
    } catch (error) {
        result.classList.add('tile-url-verify-result--fail');
        result.textContent = '验证失败：' + error.message;
    } finally {
        btn.disabled = false;
    }
}

async function resetConfig() {
    if (!await showConfirm('确定要重置所有配置为默认值吗？', { title: '重置配置', danger: true })) {
        return;
    }

    try {
        const response = await fetch('/api/config/reset', { method: 'POST' });
        const result = await response.json().catch(() => ({}));
        if (response.ok) {
            showToast('已重置为默认配置', 'success');
            // 略等一下让用户看到提示，再刷新（服务端会用默认值重渲染表单）。
            // 首页的配置是覆盖面板：挂上 hash，刷新后 panels.js 自动重开面板。
            location.hash = '#config';
            setTimeout(() => location.reload(), 600);
        } else {
            showToast('重置失败: ' + (result.error || response.status), 'danger');
        }
    } catch (error) {
        showToast('重置失败: ' + error.message, 'danger');
    }
}
