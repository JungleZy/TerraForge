function initConfig() {
    document.getElementById('configForm').addEventListener('submit', saveConfig);
    // 「恢复默认」原来是模板里的 onclick="resetConfig()"：内联处理器强制
    // resetConfig 必须是全局函数，且与 CSP 的 unsafe-inline 绑死。接线挪到这里，
    // 模板那边的 onclick 已同步删掉 —— 两边都留着会点一次跑两次。
    const resetBtn = document.getElementById('configResetBtn');
    if (resetBtn) resetBtn.addEventListener('click', resetConfig);
    initTileServerEditor();
    // 瓦片源向导（§6.2）。必须排在 initTileServerEditor 之后：没有空行时它要调
    // addTileServerRow 新增一行，而那之前编辑器得先把「至少留一行」补出来。
    initTileUrlWizard();
    initBasemapSource();
    initThemeSwitcher();
    initAccentSwitcher();
    initLangSwitcher();
    initConcurrencyRecommend();
    initProxyAutodetect();
    initCacheManager();
}

// --- 底图源 -------------------------------------------------------------------
// 底图与下载源（tile_servers）是两个独立配置，理由见
// src/services/basemap_source.py：两者是不同用途的地址（底图给页面看、
// tile_servers 是下载源），不是不同的出网路径 —— 底图瓦片由服务端转发
// （/basemap/{z}/{x}/{y}，见 routes/basemap_static.py），一样吃 proxy_url。
// 这条路径是应用内的，但**未必同源**：0.3 起默认由瓦片专用端口出图
// （src/core/tile_server.py），只有降级时才回到主端口。吃不吃 proxy_url 与
// 走哪个端口无关 —— 两个端口是同一个 Flask app。
//
// 存库的值只有一个字符串：预设名（esri / google_satellite / google_roadmap /
// download_source）或一条完整 XYZ 模板。UI 上拆成「下拉 + 自定义输入框」两个
// 控件，collectBasemapSource 负责合回一个值。

function syncBasemapCustom() {
    const sel = document.getElementById('basemap_source_preset');
    const custom = document.getElementById('basemap_source_custom');
    if (!sel || !custom) return;
    custom.hidden = sel.value !== 'custom';
}

function collectBasemapSource() {
    const sel = document.getElementById('basemap_source_preset');
    if (!sel) return '';
    if (sel.value !== 'custom') return sel.value;
    return document.getElementById('basemap_source_custom').value.trim();
}

function initBasemapSource() {
    const sel = document.getElementById('basemap_source_preset');
    if (!sel) return;
    sel.addEventListener('change', syncBasemapCustom);
    syncBasemapCustom();
}

// --- 缓存管理 -----------------------------------------------------------------
// 缓存不做任何自动清理：这里分类展示占用，手动清理走两次 showConfirm
// （第一次说明将删什么，第二次 danger 样式确认不可恢复）。

// 字节 → 人类可读走 static/js/ui.js 的 formatBytes（window.formatBytes，
// base.html 无条件全局加载，全站唯一一份 1024 进位换算）。
//
// 这段注释原来说它在 task_center.js —— **错文件**，而且错得刚好会害人：
// /config 正是把 base.html 的 vendor_task_list_js 块覆盖成空的那一页，
// task_center.js 在这里根本不加载。谁照注释把函数搬过去，缓存卡就是一片
// ReferenceError。
//
// 这里曾有一份逐字相同的 formatCacheBytes —— 两份四舍五入规则一旦漂移，
// 缓存卡和任务详情的产物清单会对同一个数字给出不同读数，而没有任何机制
// 会报错。调用点已全部改直接调 formatBytes。

function initCacheManager() {
    const body = document.getElementById('cacheStatsBody');
    if (!body) return;

    // 事件代理：行是 loadCacheStats 动态渲染的
    body.addEventListener('click', function (e) {
        const btn = e.target.closest('.cache-clear-btn');
        if (btn) clearCacheCategory(btn.dataset.key, btn.dataset.label, btn.dataset.size, btn);
    });

    const refreshBtn = document.getElementById('cacheStatsRefresh');
    if (refreshBtn) refreshBtn.addEventListener('click', loadCacheStats);

    const clearAllBtn = document.getElementById('cacheClearAll');
    if (clearAllBtn) {
        clearAllBtn.addEventListener('click', function () {
            const total = document.getElementById('cacheStatsTotal').textContent || '—';
            clearCacheCategory('__all__', t('js.config.cache.all_label'), total, clearAllBtn);
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
        // 三段结构（分类名 / 尺寸读数 / 清理按钮）由 style.css 的 .cache-row 排版，
        // 不再用 d-flex + justify-content-between 这几个工具类拼：读数必须整段
        // 不换行（工具类没地方挂 white-space），而分类名是允许折行的那一段 ——
        // `瓦片缓存（roadmap · 23e09a69）` 折两行时读数曾跟着断成
        // 「581.6 KB · 39」+「files」（中英文都会，实测）。
        body.innerHTML = categories.map(function (c) {
            return '<div class="cache-row">' +
                '<span class="cache-row__label">' + window.escapeHtml(c.label) + '</span>' +
                '<span class="cache-row__meta">' +
                '<span class="cache-row__size text-muted">' +
                t('js.config.cache.size_files',
                    { size: formatBytes(c.size_bytes), count: c.file_count }) +
                '</span>' +
                '<button type="button" class="btn btn-outline-danger btn-compact cache-clear-btn" ' +
                'data-key="' + window.escapeHtml(c.key) + '" data-label="' + window.escapeHtml(c.label) + '" ' +
                'data-size="' + formatBytes(c.size_bytes) + '">' +
                t('js.config.cache.clear') + '</button>' +
                '</span></div>';
        }).join('');
    }
    document.getElementById('cacheStatsTotal').textContent = formatBytes(data.total_bytes || 0);
    document.getElementById('cacheStatsTotalFiles').textContent =
        categories.reduce(function (sum, c) { return sum + (c.file_count || 0); }, 0);
}

// trigger：行上那颗「清理」钮，或顶上的「全部清理」。两道确认框也在守卫里
// 面 —— 清理是不可撤销的，连点叠出两个确认框、两次回车就是两发 POST，第二
// 发在第一发还在 rmtree 时进来。
async function clearCacheCategory(category, label, sizeText, trigger = null) {
    return guard(trigger, async function () {
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
                    size: formatBytes(result.data.total_removed_bytes)
                }), 'success');
            } else {
                showToast(t('js.config.cache.clear_failed',
                    { error: result.data.error || ('HTTP ' + result.status) }), 'danger');
            }
        } catch (error) {
            showToast(t('js.config.cache.clear_failed', { error: error.message }), 'danger');
        }
        loadCacheStats();
    });
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
        // 明确忽略：非 JSON 响应按空对象处理，调用方只看 ok / status。
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

// --- 代理自动检测 -------------------------------------------------------------
// proxy_url 留空时后端会自己找代理（环境变量/系统代理、Windows PAC、本机与 WSL
// 宿主的常见代理端口），每个候选都用真实瓦片实测。这里只做两件事：进页面拉一次
// 当前状态、点按钮强制重探。开关本身随「保存配置」落库，与其它字段口径一致。
//
// 状态呈现是**一个图标**：颜色表结论（绿=找到 / 灰=直连 / 黄=用的手动值 /
// 蓝转=检测中 / 红=请求失败），完整说明写进 data-hint，hover 才展开。
// 说明文字有五六十个字，常驻在版面上会把这一块压成一堵墙。

const PROXY_ICONS = {
    ok: '<circle cx="12" cy="12" r="9"></circle><polyline points="8.5 12.5 11 15 15.5 9.5"></polyline>',
    none: '<circle cx="12" cy="12" r="9"></circle><line x1="8" y1="12" x2="16" y2="12"></line>',
    manual: '<circle cx="12" cy="12" r="9"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line>',
    busy: '<path d="M12 3a9 9 0 1 0 9 9"></path>',
    error: '<circle cx="12" cy="12" r="9"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line>',
};

function proxySourceLabel(source) {
    const key = { env: 'js.config.proxy.source_env',
                  pac: 'js.config.proxy.source_pac',
                  scan: 'js.config.proxy.source_scan' }[source];
    return key ? t(key) : (source || '');
}

// shape: PROXY_ICONS 的键，同时决定 .hint 的状态色类。
function setProxyIcon(icon, shape, text) {
    // 必须写进 <svg> 而不是外层 <button>：innerHTML 走 HTML 解析器，裸
    // <circle>/<line> 落在 HTML 命名空间里既不是 SVG 元素也不渲染，
    // 图标会变成一块什么都没有的空白（改前实测就是全黑的 18×18）。
    const svg = icon.querySelector('svg');
    if (svg) {
        svg.innerHTML = PROXY_ICONS[shape] || PROXY_ICONS.none;
        // 转圈挂在 svg 上（转 button 会把 hover 气泡一起转），且必须是单类
        // 选择器：写成 `.hint.is-busy > svg` 会让契约测试构造动画上下文的
        // `_motion_contexts_from_stylesheet`（tests/test_css_contract.py，用
        // `branch.split()` + `_parse_compound(...) is not None`）解不出 `>`，
        // test_reduced_motion_actually_stops_every_animated_element 失败。
        // 层叠判定本身（`_text_branch_applies`）自 2026-08-14 起已支持子组合符。
        svg.classList.toggle('hint-spin', shape === 'busy');
    }
    icon.classList.remove('is-ok', 'is-warn', 'is-danger', 'is-busy');
    const tone = { ok: 'is-ok', manual: 'is-warn', busy: 'is-busy',
                   error: 'is-danger' }[shape];
    if (tone) icon.classList.add(tone);
    // data-hint 为空时 CSS 把图标整个隐藏，所以任何分支都必须给出文本
    icon.dataset.hint = text;
    // 无障碍名必须跟着一起更新:气泡是 ::after + content: attr(data-hint),而
    // aria-label 按 accname 规范优先于 CSS 生成内容 —— 只留模板里那个固定的
    // 「代理检测状态」,读屏用户永远拿不到检测结果本身。
    // 前缀取模板渲染的初始 aria-label,记进 dataset:setProxyIcon 每次检测都
    // 会被调用多次(busy -> ok),不缓存前缀就会一层层累积拼接。
    if (icon.dataset.ariaPrefix === undefined) {
        icon.dataset.ariaPrefix = icon.getAttribute('aria-label') || '';
    }
    icon.setAttribute('aria-label', `${icon.dataset.ariaPrefix}: ${text}`);
}

function renderProxyStatus(icon, data) {
    if (data.manual) {
        setProxyIcon(icon, 'manual', t('js.config.proxy.manual', { url: data.manual }));
    } else if (!data.auto_enabled) {
        setProxyIcon(icon, 'none', t('js.config.proxy.disabled'));
    } else if (data.url) {
        setProxyIcon(icon, 'ok', t('js.config.proxy.found', {
            url: data.url, source: proxySourceLabel(data.source),
        }));
    } else if (data.status === 'done') {
        setProxyIcon(icon, 'none', t('js.config.proxy.none',
            { tried: (data.candidates || []).length }));
    } else {
        // idle（从没探过）或 detecting（启动那轮还在跑）
        setProxyIcon(icon, 'busy', t('js.config.proxy.pending'));
    }
}

async function fetchProxyStatus(icon, { force = false } = {}) {
    try {
        const response = await fetch('/api/config/proxy_status',
            { method: force ? 'POST' : 'GET' });
        const data = await response.json();
        if (!response.ok) {
            setProxyIcon(icon, 'error', t('js.config.proxy.failed',
                { error: data.error || ('HTTP ' + response.status) }));
            return;
        }
        renderProxyStatus(icon, data);
    } catch (error) {
        setProxyIcon(icon, 'error',
            t('js.config.proxy.failed', { error: error.message }));
    }
}

function initProxyAutodetect() {
    const icon = document.getElementById('proxyStatusIcon');
    const btn = document.getElementById('proxyDetectNow');
    if (!icon || !btn) return;

    btn.addEventListener('click', async function () {
        const originalText = btn.textContent;
        btn.disabled = true;
        btn.textContent = t('js.config.proxy.detecting');
        setProxyIcon(icon, 'busy', t('js.config.proxy.detecting_hint'));
        try {
            await fetchProxyStatus(icon, { force: true });
        } finally {
            btn.disabled = false;
            btn.textContent = originalText;
        }
    });

    fetchProxyStatus(icon);
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
        chips.forEach(chip => {
            const on = chip.dataset.themeMode === mode;
            chip.classList.toggle('active', on);
            // aria-pressed 与 .active 必须同步翻：只有 CSS class 时读屏用户
            // 听不出当前生效的是哪一档主题（map.js 的 .map-panel-btn 同写法）。
            chip.setAttribute('aria-pressed', on ? 'true' : 'false');
        });
    }

    group.addEventListener('click', function (e) {
        const chip = e.target.closest('[data-theme-mode]');
        if (!chip) return;
        TerraTheme.set(chip.dataset.themeMode);
        refresh();
    });

    refresh();
}

// --- 外观:强调色分段开关 -----------------------------------------------------
// 五枚 chip(sky / teal / violet / rose / orange),当前值高亮。只调
// TerraTheme.setAccent —— 偏好存 localStorage `tf-accent`、立即全站生效,
// 不随表单提交。机制与上面的主题开关完全同构。

function initAccentSwitcher() {
    const group = document.getElementById('accentModeGroup');
    if (!group || !window.TerraTheme || !TerraTheme.getAccent) return;
    const chips = [...group.querySelectorAll('[data-accent]')];

    function refresh() {
        const accent = TerraTheme.getAccent();
        chips.forEach(chip => {
            const on = chip.dataset.accent === accent;
            chip.classList.toggle('active', on);
            // aria-pressed 与 .active 必须同步翻(与主题组同一写法)。
            chip.setAttribute('aria-pressed', on ? 'true' : 'false');
        });
    }

    group.addEventListener('click', function (e) {
        const chip = e.target.closest('[data-accent]');
        if (!chip) return;
        TerraTheme.setAccent(chip.dataset.accent);
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

// --- 瓦片源向导（§6.2）--------------------------------------------------------
//
// 用户手上有的是**一条真实瓦片 URL**（从别的软件的网络面板里抄来的、从文档里
// 复制的），而这里要的是一个带 {z}/{x}/{y} 的模板。手工替换那三个数字是这条
// 配置最常见的出错点：抄成 z/y/x（ArcGIS REST 就是这个顺序）不会报错，
// 一样过校验、一样返回 200 的真瓦片，只是内容与位置对不上 —— 要等整张图拼完
// 才看得出来。
//
// 所以把这一步交给服务端 POST /api/config/analyze_tile_url：它穷举整数槽位、
// 用 Web 墨卡托格网约束筛选、按 XYZ 事实标准排序，并把**它猜了什么**说清楚。
//
// ⚠️ 警告逐字显示，且**不静默采纳**被标记的模板。
// 服务端的警告里有三类会让用户下出一整套废图或泄露凭据：
//   · 查询参数像凭据 —— 它会原样进 tasks 表、进缓存命名空间、进诊断包；
//   · x/y 顺序是猜的 —— 数值上分不出来，判错不会报错；
//   · 疑似 TMS 而按 xyz 给出 —— 判错只会让成品南北颠倒。
// 有警告时模板**不自动**填进条目框，改为多一颗「仍然使用此模板」——
// 让用户为这个决定按一下，是这三条警告存在的全部意义。

function tileUrlWizardHtml() {
    return `
    <div class="tile-url-wizard__row">
        <input type="url" class="form-control flex-grow-1" id="tileUrlWizardInput"
               placeholder="${t('js.wizard.placeholder')}" aria-label="${t('js.wizard.label')}">
        <button type="button" class="btn btn-outline-primary" id="tileUrlWizardRun">${t('js.wizard.analyze')}</button>
    </div>
    <div class="tile-url-wizard__out" id="tileUrlWizardOut" hidden></div>
`;
}

// 最近一次分析的结果。「仍然使用此模板」要用它，而它不能从 DOM 里读回来
// （模板里可能有 & 与引号，往返一趟 innerHTML 就不再逐字节相同了 —— 而
// 「除三个槽位外逐字节一致」正是服务端给这个模板的全部保证）。
let _wizardResult = null;

function initTileUrlWizard() {
    const host = document.getElementById('tileUrlWizard');
    if (!host) return;
    host.innerHTML = tileUrlWizardHtml();
    const input = document.getElementById('tileUrlWizardInput');
    const runBtn = document.getElementById('tileUrlWizardRun');
    runBtn.addEventListener('click', function () { runTileUrlWizard(); });
    // 回车即分析：这是一个「粘贴 → 回车」的动作，不该要求先去点按钮。
    input.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter') return;
        e.preventDefault();
        runTileUrlWizard();
    });
    // 「仍然使用此模板」是分析之后才出现的，走委托。
    host.addEventListener('click', function (e) {
        if (!e.target.closest('#tileUrlWizardApply')) return;
        applyWizardTemplate();
    });
}

async function runTileUrlWizard() {
    const input = document.getElementById('tileUrlWizardInput');
    const out = document.getElementById('tileUrlWizardOut');
    const runBtn = document.getElementById('tileUrlWizardRun');
    if (!input || !out) return;
    const url = input.value.trim();
    if (!url) {
        _renderWizardMessage(out, t('js.wizard.need_url'));
        return;
    }
    _wizardResult = null;
    _renderWizardMessage(out, t('js.wizard.analyzing'));
    runBtn.disabled = true;
    try {
        const response = await fetch('/api/config/analyze_tile_url', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
        _wizardResult = data;
        _renderWizardResult(out, data);
        // 干净的检测结果（零警告）直接落进条目框：那时没有任何需要用户判断的
        // 东西，多按一下只是仪式。有警告则等「仍然使用此模板」。
        if (!(data.warnings || []).length) applyWizardTemplate();
    } catch (error) {
        _renderWizardMessage(out, t('js.wizard.failed', { error: error.message }));
    } finally {
        runBtn.disabled = false;
    }
}

// 逐节点建 DOM 而不是拼 innerHTML：模板与警告都来自服务端对**用户粘贴的 URL**
// 的加工结果，是最典型的外部可控字符串。
function _renderWizardMessage(out, text) {
    out.innerHTML = '';
    const line = document.createElement('div');
    line.className = 'tile-url-wizard__template';
    line.textContent = text;
    out.appendChild(line);
    out.hidden = false;
}

function _renderWizardResult(out, data) {
    out.innerHTML = '';

    const template = document.createElement('div');
    template.className = 'tile-url-wizard__template';
    const detected = data.detected || {};
    template.textContent = t('js.wizard.detected', {
        template: data.template || '',
        scheme: data.scheme || '',
        z: detected.z,
        x: detected.x,
        y: detected.y,
    });
    out.appendChild(template);

    const warnings = data.warnings || [];
    if (warnings.length) {
        const box = document.createElement('div');
        box.className = 'tile-url-wizard__warnings';
        warnings.forEach(function (text) {
            const line = document.createElement('span');
            line.className = 'tile-url-wizard__warning';
            // 逐字：服务端的警告原文里带具体主机名、具体参数名、以及**转置后的
            // 正确模板**。摘要化会把用户唯一能照着做的那一句删掉。
            line.textContent = text;
            box.appendChild(line);
        });
        out.appendChild(box);

        const row = document.createElement('div');
        row.className = 'tile-url-wizard__row';
        const apply = document.createElement('button');
        apply.type = 'button';
        apply.id = 'tileUrlWizardApply';
        apply.className = 'btn btn-outline-secondary btn-compact';
        apply.textContent = t('js.wizard.apply_anyway');
        row.appendChild(apply);
        out.appendChild(row);
    }
    out.hidden = false;
}

/**
 * 把模板填进条目列表。
 *
 * 填进**第一个空行**，没有空行就新增一行 —— 覆盖一个已填好的条目会让用户
 * 悄悄丢掉一条已配好的下载源。填完聚焦到它，用户看得见东西落在哪儿了。
 */
function applyWizardTemplate() {
    if (!_wizardResult || !_wizardResult.template) return;
    const rows = document.getElementById('tileServerRows');
    if (!rows) return;
    let target = null;
    rows.querySelectorAll('.tile-server-input').forEach(function (el) {
        if (!target && !el.value.trim()) target = el;
    });
    if (!target) {
        const row = addTileServerRow('');
        target = row && row.querySelector('.tile-server-input');
    }
    if (!target) return;
    target.value = _wizardResult.template;
    target.focus();
    showToast(t('js.wizard.applied', { template: _wizardResult.template }), 'success');
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
        proxy_auto_detect: document.getElementById('proxy_auto_detect').checked ? 'true' : 'false',
        tile_servers: collectTileServers(),
        basemap_source: collectBasemapSource(),
        cache_enabled: document.getElementById('cache_enabled').checked ? 'true' : 'false',
        gdal_compression: document.getElementById('gdal_compression').value,
        gdal_resampling: document.getElementById('gdal_resampling').value,
        map_center_lat: document.getElementById('map_center_lat').value,
        map_center_lng: document.getElementById('map_center_lng').value,
        map_initial_zoom: document.getElementById('map_initial_zoom').value,
        earthdata_username: document.getElementById('earthdata_username').value,
        earthdata_password: document.getElementById('earthdata_password').value,
        // 地名搜索的服务地址。留空 = 关闭（出厂状态）。这一栏 0.3.3 就有配置键、
        // 校验和 API，唯独没进过这份 payload 和模板 —— 搜索面板的提示叫用户
        // 「去配置页填 geocoder_url」，而配置页里既没有输入框、这里也不会提交。
        geocoder_url: document.getElementById('geocoder_url').value
    };

    // e.submitter 是这次提交的那颗按钮（保存钮在 <form> 之外，靠
    // form="configForm" 关联，所以拿不到「表单里的第一个 submit」）。
    // 老引擎没有 submitter 时按关联关系找回来 —— 锁不住按钮时回车照样能
    // 二次提交，那正是本守卫要防的。
    const trigger = e.submitter
        || document.querySelector('button[type="submit"][form="configForm"]');
    return guard(trigger, async function () {
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
    });
}

async function resetConfig(e) {
    const trigger = (e && e.currentTarget) || document.getElementById('configResetBtn');
    return guard(trigger, async function () {
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
    });
}
