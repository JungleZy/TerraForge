function initConfig() {
    document.getElementById('configForm').addEventListener('submit', saveConfig);
    initTileServerEditor();
    initThemeSwitcher();
    initConcurrencyRecommend();
}

// --- 并发下载数：测速推荐 -----------------------------------------------------
// 后端按已保存的 tile_servers / proxy 做真实瓦片阶梯测速（约 30 秒），
// 返回膝点并发。推荐值只填进输入框 —— 是否落库仍由「保存配置」决定，
// 与配置页其它字段的语义一致。

function initConcurrencyRecommend() {
    const btn = document.getElementById('concurrencyRecommend');
    const hint = document.getElementById('concurrencyRecommendHint');
    if (!btn || !hint) return;

    btn.addEventListener('click', async function () {
        const originalText = btn.textContent;
        btn.disabled = true;
        btn.textContent = '测速中…';
        hint.textContent = '正在按当前网络环境实测吞吐，约 30 秒…';
        try {
            const response = await fetch('/api/config/recommend_concurrency', { method: 'POST' });
            const data = await response.json();
            if (response.ok && data.recommended) {
                document.getElementById('concurrent_downloads').value = data.recommended;
                hint.textContent = (data.note || ('推荐 ' + data.recommended)) + '（已填入，保存后生效）';
            } else {
                hint.textContent = '推荐失败：' + (data.error || ('HTTP ' + response.status));
            }
        } catch (error) {
            hint.textContent = '推荐失败：' + error.message;
        } finally {
            btn.disabled = false;
            btn.textContent = originalText;
        }
    });
}

// --- 外观：主题分段开关 -------------------------------------------------------
// 三枚带文字的 chip（暗黑 / 明亮 / 跟随系统），当前值高亮（.status-chip.active）。
// 只调 TerraTheme.set —— 偏好存 localStorage、立即全站生效，不随表单提交。

function initThemeSwitcher() {
    const group = document.getElementById('themeModeGroup');
    if (!group || !window.TerraTheme) return;
    const chips = [...group.querySelectorAll('[data-theme-mode]')];

    function refresh() {
        const mode = TerraTheme.get();
        chips.forEach(chip => chip.classList.toggle('active', chip.dataset.themeMode === mode));
    }

    group.addEventListener('click', function (e) {
        const chip = e.target.closest('[data-theme-mode]');
        if (!chip) return;
        TerraTheme.set(chip.dataset.themeMode);
        refresh();
    });

    refresh();
}

// --- 瓦片服务器列表编辑器 -----------------------------------------------------
// 每行一个条目：Google 别名（mts0–mts3）、主机（mts0.google.cn）或完整
// XYZ 模板。行可以增删，各自带「验证」按钮（验证当前输入，无需先保存）。
// 保存时把所有非空行按逗号合并写回 tile_servers。

const TILE_SERVER_ROW_HTML = `
    <div class="d-flex gap-2 align-items-center">
        <input type="text" class="form-control flex-grow-1 tile-server-input">
        <button type="button" class="btn btn-outline-primary tile-server-verify">验证</button>
        <button type="button" class="btn btn-icon btn-outline-danger tile-server-remove"
                aria-label="删除该服务器" title="删除该服务器">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
        </button>
    </div>
    <div class="tile-url-verify-result" hidden></div>
`;

function addTileServerRow(value) {
    const rows = document.getElementById('tileServerRows');
    if (!rows) return null;
    const row = document.createElement('div');
    row.className = 'tile-server-row';
    row.innerHTML = TILE_SERVER_ROW_HTML;
    row.querySelector('.tile-server-input').value = value || '';
    rows.appendChild(row);
    return row;
}

function initTileServerEditor() {
    const rows = document.getElementById('tileServerRows');
    if (!rows) return;

    // 空列表（用户删光过）也至少留一行空输入，避免编辑器消失
    if (!rows.querySelector('.tile-server-row')) addTileServerRow('');

    const addBtn = document.getElementById('tileServerAdd');
    if (addBtn) {
        addBtn.addEventListener('click', function () {
            const row = addTileServerRow('');
            if (row) row.querySelector('.tile-server-input').focus();
        });
    }

    // 事件代理：行的增删/验证都是动态生成的
    rows.addEventListener('click', function (e) {
        const removeBtn = e.target.closest('.tile-server-remove');
        if (removeBtn) {
            removeBtn.closest('.tile-server-row').remove();
            if (!rows.querySelector('.tile-server-row')) addTileServerRow('');
            return;
        }
        const verifyBtn = e.target.closest('.tile-server-verify');
        if (verifyBtn) verifyTileServerRow(verifyBtn.closest('.tile-server-row'));
    });
}

async function verifyTileServerRow(row) {
    if (!row) return;
    const input = row.querySelector('.tile-server-input');
    const btn = row.querySelector('.tile-server-verify');
    const result = row.querySelector('.tile-url-verify-result');
    const server = input.value.trim();

    result.hidden = false;
    result.className = 'tile-url-verify-result';
    result.textContent = '正在验证…';
    btn.disabled = true;

    try {
        const response = await fetch('/api/config/verify_tile_url', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ server })
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

function collectTileServers() {
    return [...document.querySelectorAll('#tileServerRows .tile-server-input')]
        .map(el => el.value.trim())
        .filter(Boolean)
        .join(',');
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
        tile_servers: collectTileServers(),
        cache_enabled: document.getElementById('cache_enabled').checked ? 'true' : 'false',
        cache_max_size_mb: document.getElementById('cache_max_size_mb').value,
        gdal_compression: document.getElementById('gdal_compression').value,
        gdal_resampling: document.getElementById('gdal_resampling').value,
        history_retention_days: document.getElementById('history_retention_days').value,
        map_center_lat: document.getElementById('map_center_lat').value,
        map_center_lng: document.getElementById('map_center_lng').value,
        map_initial_zoom: document.getElementById('map_initial_zoom').value,
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
