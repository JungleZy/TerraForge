function initConfig() {
    document.getElementById('configForm').addEventListener('submit', saveConfig);
    initTileServerEditor();
    initThemeSwitcher();
    initLangSwitcher();
    initConcurrencyRecommend();
    initCacheManager();
}

// --- 缓存管理 -----------------------------------------------------------------
// 缓存不做任何自动清理：这里分类展示占用，手动清理走两次 showConfirm
// （第一次说明将删什么，第二次 danger 样式确认不可恢复）。

function formatCacheBytes(bytes) {
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let value = Number(bytes) || 0;
    let i = 0;
    while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
    return (i === 0 ? value : value.toFixed(1)) + ' ' + units[i];
}

function initCacheManager() {
    const body = document.getElementById('cacheStatsBody');
    if (!body) return;

    // 事件代理：行是 loadCacheStats 动态渲染的
    body.addEventListener('click', function (e) {
        const btn = e.target.closest('.cache-clear-btn');
        if (btn) clearCacheCategory(btn.dataset.key, btn.dataset.label, btn.dataset.size);
    });

    const refreshBtn = document.getElementById('cacheStatsRefresh');
    if (refreshBtn) refreshBtn.addEventListener('click', loadCacheStats);

    const clearAllBtn = document.getElementById('cacheClearAll');
    if (clearAllBtn) {
        clearAllBtn.addEventListener('click', function () {
            const total = document.getElementById('cacheStatsTotal').textContent || '—';
            clearCacheCategory('__all__', t('js.config.cache.all_label'), total);
        });
    }

    loadCacheStats();
}

async function loadCacheStats() {
    const body = document.getElementById('cacheStatsBody');
    if (!body) return;
    try {
        const response = await fetch('/api/cache/stats');
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || ('HTTP ' + response.status));
        renderCacheStats(data);
    } catch (error) {
        body.innerHTML = '<div class="text-danger">' +
            t('js.config.cache.load_failed', { error: window.escapeHtml(error.message) }) +
            '</div>';
    }
}

function renderCacheStats(data) {
    const body = document.getElementById('cacheStatsBody');
    const categories = data.categories || [];
    if (!categories.length) {
        body.innerHTML = '<div class="text-muted">' + t('js.config.cache.empty') + '</div>';
    } else {
        body.innerHTML = categories.map(function (c) {
            return '<div class="d-flex justify-content-between align-items-center py-1">' +
                '<span>' + window.escapeHtml(c.label) + '</span>' +
                '<span class="d-flex align-items-center gap-2">' +
                '<span class="text-muted">' +
                t('js.config.cache.size_files',
                    { size: formatCacheBytes(c.size_bytes), count: c.file_count }) +
                '</span>' +
                '<button type="button" class="btn btn-outline-danger btn-sm cache-clear-btn" ' +
                'data-key="' + window.escapeHtml(c.key) + '" data-label="' + window.escapeHtml(c.label) + '" ' +
                'data-size="' + formatCacheBytes(c.size_bytes) + '">' +
                t('js.config.cache.clear') + '</button>' +
                '</span></div>';
        }).join('');
    }
    document.getElementById('cacheStatsTotal').textContent = formatCacheBytes(data.total_bytes || 0);
    document.getElementById('cacheStatsTotalFiles').textContent =
        categories.reduce(function (sum, c) { return sum + (c.file_count || 0); }, 0);
}

async function clearCacheCategory(category, label, sizeText) {
    const demWarning = category === 'dem' || category === '__all__'
        ? ' ' + t('js.config.cache.dem_warning') : '';
    const first = await showConfirm(
        t('js.config.cache.clear_confirm', { label: label, size: sizeText, warning: demWarning }),
        {
            title: t('js.config.cache.clear_title'),
            confirmText: t('js.config.cache.continue'),
            danger: true
        });
    if (!first) return;

    const second = await showConfirm(
        t('js.config.cache.clear_confirm_again', { label: label }),
        {
            title: t('js.config.cache.confirm_again_title'),
            confirmText: t('js.config.cache.confirm_delete'),
            danger: true
        });
    if (!second) return;

    try {
        let result = await postCacheClear(category, false);
        // 409：有任务尚未结束。后端拦下而不是直接清，是因为运行中的地图任务
        // 已经把 cache 命中的瓦片算成「已完成」，清掉后它们既不重下、复制失败
        // 也只记 warning，任务照报 completed —— 产物目录静默缺瓦片（M8）。
        if (result.status === 409) {
            const forceOk = await showConfirm(
                t('js.config.cache.force_confirm',
                    { error: result.data.error || t('js.config.cache.tasks_running') }),
                {
                    title: t('js.config.cache.tasks_running_title'),
                    confirmText: t('js.config.cache.force_clear'),
                    danger: true
                });
            if (!forceOk) {
                showToast(t('js.config.cache.clear_cancelled'), 'info');
                loadCacheStats();
                return;
            }
            result = await postCacheClear(category, true);
        }
        if (result.ok && result.data.success) {
            showToast(t('js.config.cache.cleared', {
                label: label,
                size: formatCacheBytes(result.data.total_removed_bytes)
            }), 'success');
        } else {
            showToast(t('js.config.cache.clear_failed',
                { error: result.data.error || ('HTTP ' + result.status) }), 'danger');
        }
    } catch (error) {
        showToast(t('js.config.cache.clear_failed', { error: error.message }), 'danger');
    }
    loadCacheStats();
}

async function postCacheClear(category, force) {
    const response = await fetch('/api/cache/clear', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ category: category, force: !!force })
    });
    let data = {};
    try {
        data = await response.json();
    } catch (e) {
        data = {};
    }
    return { ok: response.ok, status: response.status, data: data };
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
        btn.textContent = t('js.config.concurrency.testing');
        hint.textContent = t('js.config.concurrency.testing_hint');
        try {
            const response = await fetch('/api/config/recommend_concurrency', { method: 'POST' });
            const data = await response.json();
            if (response.ok && data.recommended) {
                document.getElementById('concurrent_downloads').value = data.recommended;
                hint.textContent = t('js.config.concurrency.filled', {
                    note: data.note
                        || t('js.config.concurrency.recommended', { n: data.recommended })
                });
            } else {
                hint.textContent = t('js.config.concurrency.failed',
                    { error: data.error || ('HTTP ' + response.status) });
            }
        } catch (error) {
            hint.textContent = t('js.config.concurrency.failed', { error: error.message });
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

// --- 外观：语言分段开关 -------------------------------------------------------
// 两枚 chip（中文 / English）。语种写 cookie 后必须整页 reload —— 模板文案是
// 服务端渲染的，只改前端的 window.__I18N__ 换不掉已经渲染好的那半边界面。

function initLangSwitcher() {
    const group = document.getElementById('langModeGroup');
    if (!group || !window.TerraI18n) return;

    group.addEventListener('click', function (e) {
        const chip = e.target.closest('[data-lang]');
        if (!chip || chip.dataset.lang === TerraI18n.lang) return;
        TerraI18n.set(chip.dataset.lang);
        window.location.reload();
    });
}

// --- 瓦片服务器列表编辑器 -----------------------------------------------------
// 每行一个条目：Google 别名（mts0–mts3）、主机（mts0.google.cn）或完整
// XYZ 模板。行可以增删，各自带「验证」按钮（验证当前输入，无需先保存）。
// 保存时把所有非空行按逗号合并写回 tile_servers。

// 写成函数而不是模块级常量：t() 要等 i18n.js 把全局装好之后才求值。
function tileServerRowHtml() {
    return `
    <div class="d-flex gap-2 align-items-center">
        <input type="text" class="form-control flex-grow-1 tile-server-input">
        <button type="button" class="btn btn-outline-primary tile-server-verify">${t('js.config.tile.verify')}</button>
        <button type="button" class="btn btn-icon btn-outline-danger tile-server-remove"
                aria-label="${t('js.config.tile.remove')}" title="${t('js.config.tile.remove')}">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
        </button>
    </div>
    <div class="tile-url-verify-result" hidden></div>
`;
}

function addTileServerRow(value) {
    const rows = document.getElementById('tileServerRows');
    if (!rows) return null;
    const row = document.createElement('div');
    row.className = 'tile-server-row';
    row.innerHTML = tileServerRowHtml();
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
    result.textContent = t('js.config.tile.verifying');
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
            result.textContent = t('js.config.tile.verify_ok', {
                status: data.status_code,
                content_type: data.content_type || t('js.config.tile.unknown_type'),
                elapsed: data.elapsed_ms,
                tile: data.tile
            });
        } else {
            result.classList.add('tile-url-verify-result--fail');
            result.textContent = t('js.config.tile.verify_failed',
                { error: data.error || ('HTTP ' + response.status) });
        }
    } catch (error) {
        result.classList.add('tile-url-verify-result--fail');
        result.textContent = t('js.config.tile.verify_failed', { error: error.message });
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
        gdal_compression: document.getElementById('gdal_compression').value,
        gdal_resampling: document.getElementById('gdal_resampling').value,
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
            showToast(t('js.config.save.ok'), 'success');
        } else {
            const detail = (result.errors && result.errors.length)
                ? result.errors.join(t('js.config.save.error_sep')) : result.error;
            showToast(t('js.config.save.failed', { error: detail }), 'danger');
        }
    } catch (error) {
        showToast(t('js.config.save.failed', { error: error.message }), 'danger');
    }
}

async function resetConfig() {
    if (!await showConfirm(t('js.config.reset.confirm'),
            { title: t('js.config.reset.title'), danger: true })) {
        return;
    }

    try {
        const response = await fetch('/api/config/reset', { method: 'POST' });
        const result = await response.json().catch(() => ({}));
        if (response.ok) {
            showToast(t('js.config.reset.ok'), 'success');
            // 略等一下让用户看到提示，再刷新（服务端会用默认值重渲染表单）。
            // 首页的配置是覆盖面板：挂上 hash，刷新后 panels.js 自动重开面板。
            location.hash = '#config';
            setTimeout(() => location.reload(), 600);
        } else {
            showToast(t('js.config.reset.failed',
                { error: result.error || response.status }), 'danger');
        }
    } catch (error) {
        showToast(t('js.config.reset.failed', { error: error.message }), 'danger');
    }
}
