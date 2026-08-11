/**
 * 全窗口拖拽 .tif/.tiff 打开「本地处理」(2026-08-11 设计 §3.6,
 * 借鉴 GeoLibre 的窗口级 drag-drop)。
 *
 * - 只在首页生效:没有 #processModal 的页面(/config、/history)整个空载。
 * - 遮罩纯展示(pointer-events:none),拖放事件始终落 window;
 *   卡死自救:窗口失焦(blur)或 Esc(capture)强制复位。
 * - 文件经 DataTransfer 过滤(只留 .tif/.tiff)后喂给 #localTerrainFiles,
 *   并 dispatch change 让 map.js 的 updateTifInfo 等既有接线照常跑。
 */
(function () {
    'use strict';

    var modalEl = document.getElementById('processModal');
    if (!modalEl) return;

    var depth = 0;    // dragenter/dragleave 深度计数(进出子元素会成对触发)
    var veil = null;

    function buildVeil() {
        if (veil) return veil;
        veil = document.createElement('div');
        veil.className = 'drop-veil';
        var tip = document.createElement('span');
        tip.className = 'drop-veil__tip';
        tip.textContent = t('js.drop.hint');
        veil.appendChild(tip);
        document.body.appendChild(veil);
        return veil;
    }

    function show() { buildVeil().classList.add('drop-veil--in'); }
    function hide() { if (veil) veil.classList.remove('drop-veil--in'); }
    function reset() { depth = 0; hide(); }

    function hasFiles(e) {
        var types = e.dataTransfer && e.dataTransfer.types;
        return !!(types && [].indexOf.call(types, 'Files') !== -1);
    }

    window.addEventListener('dragenter', function (e) {
        if (!hasFiles(e)) return;
        e.preventDefault();
        depth += 1;
        show();
    });
    // dragover 必须 preventDefault,drop 才会触发(浏览器默认不许投放)。
    window.addEventListener('dragover', function (e) {
        if (depth > 0) e.preventDefault();
    });
    window.addEventListener('dragleave', function () {
        depth = Math.max(0, depth - 1);
        if (depth === 0) hide();
    });
    window.addEventListener('drop', function (e) {
        if (depth === 0 && !hasFiles(e)) return;
        e.preventDefault();
        reset();
        var files = [].filter.call(e.dataTransfer.files, function (f) {
            return /\.tiff?$/i.test(f.name);
        });
        if (!files.length) {
            window.showToast(t('js.drop.no_tif'), 'warning');
            return;
        }
        var input = document.getElementById('localTerrainFiles');
        try {
            var dt = new DataTransfer();
            files.forEach(function (f) { dt.items.add(f); });
            input.files = dt.files;
        } catch (err) {
            window.showToast(t('js.drop.failed'), 'danger');
            return;
        }
        // 先摆正两个下拉(触发既有 change 接线刷新行显隐),再喂文件、开弹窗。
        var typeSel = document.getElementById('processType');
        var srcSel = document.getElementById('processSource');
        typeSel.value = 'local_terrain';
        typeSel.dispatchEvent(new Event('change', { bubbles: true }));
        srcSel.value = 'upload';
        srcSel.dispatchEvent(new Event('change', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
        bootstrap.Modal.getOrCreateInstance(modalEl).show();
    });

    // 卡死自救:拖出窗口松手时 dragleave/drop 可能整个丢,blur 兜底。
    window.addEventListener('blur', reset);
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && veil && veil.classList.contains('drop-veil--in')) {
            e.stopPropagation();
            reset();
        }
    }, true);
})();
