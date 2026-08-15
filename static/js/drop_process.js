/**
 * 全窗口拖拽打开「本地处理」或导入下载区域
 * （2026-08-11 设计 §3.6 借鉴 GeoLibre 的窗口级 drag-drop；区域导入是 §5.1）。
 *
 * - 只在首页生效:没有 #createPanel 的页面(/config、/history)整个空载。
 * - 遮罩纯展示(pointer-events:none),拖放事件始终落 window;
 *   卡死自救:窗口失焦(blur)或 Esc(向 panels.js 的层栈注册 'dropVeil')强制复位。
 * - **两类文件，一次判定**：
 *     .tif/.tiff        -> 经 DataTransfer 喂给 #sourceFiles，并打开新建任务面板
 *                          预选「本地地形切片」；dispatch change 让 map.js 的
 *                          updateSourceTifInfo 等既有接线照常跑；
 *     区域矢量文件       -> POST /api/region/import，落地成当前下载区域（map.js
 *                          的 importRegionFile）。
 *   为什么放在同一个投放处理器里：用户拖进来的时候脑子里想的是「用这个文件」，
 *   不是「这是第几类文件」。两个投放区（一个收 DEM、一个收边界）在一张铺满屏幕
 *   的地图上根本划不出来，而划不出来的投放区只会让人两边都试一遍。
 *   混着拖时 DEM 优先：它是既有行为，而且一次能收多个文件；区域一次只取一个
 *   （选区是单值，拖 5 个边界文件里没有一个「合并」语义是显然的）。
 */
(function () {
    'use strict';

    // 首页守卫：判据从退场的处理弹窗换成 #createPanel（2026-08-15 两个弹窗合一）。
    // 投放这条快捷路径**保留** —— 它不是唯一路径（rail 的「新建」是），但它是
    // 最短的一条：把 .tif 拖进窗口就等于「用这个文件切地形」。
    if (!document.getElementById('createPanel')) return;

    var depth = 0;    // dragenter/dragleave 深度计数(进出子元素会成对触发)
    var veil = null;

    // 区域矢量的后缀。与服务端 region_import.SUPPORTED_EXTENSIONS 同一组值，
    // 也与 templates/index.html 里那个 file input 的 accept 一致。
    //
    // 前端为什么仍要判一次（服务端已经会拒）：不判就得把一个 500MB 的随手拖入
    // 文件整个 POST 上去才换回一句「不支持」。而**拒绝的理由**不在这里编 ——
    // 服务端 400 的 body 带 supported_extensions，那份清单才是真相
    // （见 map.js 的 importRegionFile）。
    var REGION_EXT_RE = /\.(geojson|json|kml|kmz|zip|shp)$/i;
    var TIF_EXT_RE = /\.tiff?$/i;

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

    // .tif 投放的既有路径：过滤 -> 预选管线 -> 喂 input -> 开面板。
    // 返回 false 表示喂文件失败（DataTransfer 不可用），已经 toast 过。
    //
    // **顺序不能反**：必须先把管线切到 local_terrain 再喂文件。#sourceFiles 装在
    // #sourceUploadRow 里，而那一行的可见性由 map.js 的显隐表按「管线 × 来源」
    // 决定 —— 默认管线是瓦片，那时整个 #sourceField 是 hidden 的，先喂文件就是
    // 把它塞进一个用户看不见的控件里；updateSourceTifInfo 读的 mode 也会按瓦片
    // 管线算成 'terrain' 之外的东西。openCreatePanel('local_terrain') 一次把
    // 管线、面板、显隐三件事办齐。
    function openLocalProcess(files) {
        var input = document.getElementById('sourceFiles');
        if (!input || typeof window.openCreatePanel !== 'function') return false;
        window.openCreatePanel('local_terrain');
        // 来源必须是「上传文件」：用户上一次可能停在「已完成的高程任务」上，
        // 那时上传行是收起的。摆正后补发 change 让显隐表重算。
        var srcSel = document.getElementById('processSource');
        if (srcSel) {
            srcSel.value = 'upload';
            srcSel.dispatchEvent(new Event('change', { bubbles: true }));
        }
        try {
            var dt = new DataTransfer();
            files.forEach(function (f) { dt.items.add(f); });
            input.files = dt.files;
        } catch (err) {
            window.showToast(t('js.drop.failed'), 'danger');
            return false;
        }
        input.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
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
        var dropped = [].slice.call(e.dataTransfer.files);
        var tifs = dropped.filter(function (f) { return TIF_EXT_RE.test(f.name); });
        if (tifs.length) {
            openLocalProcess(tifs);
            return;
        }
        var regions = dropped.filter(function (f) { return REGION_EXT_RE.test(f.name); });
        if (regions.length) {
            // 选区是单值：只取第一个。多拖时说一句 —— 静默丢掉后面几个会让人
            // 以为它们被合并了，而下载范围是这个应用里最不能含糊的一个数。
            if (regions.length > 1) {
                window.showToast(t('js.region.drop.only_first', { name: regions[0].name }), 'warning');
            }
            // importRegionFile 来自 map.js（首页专属，与本文件同页加载）。
            // typeof 守卫兜住加载顺序被改动的情形 —— 裸调会 ReferenceError，
            // 而那会让投放彻底静默。
            if (typeof importRegionFile === 'function') {
                importRegionFile(regions[0]);
            }
            return;
        }
        window.showToast(t('js.drop.unsupported'), 'warning');
    });

    // 卡死自救:拖出窗口松手时 dragleave/drop 可能整个丢,blur 兜底。
    window.addEventListener('blur', reset);
    // Esc 也是自救路径，但不再自己监听：整站唯一那个「关最上层」的 keydown 在
    // panels.js 的层栈里，这里只报到。改前是 document capture + stopPropagation
    // ——「谁的相位早谁先关」那套的第三份。
    window.TerraLayers.register('dropVeil', {
        isOpen: function () { return !!veil && veil.classList.contains('drop-veil--in'); },
        close: reset,
    });
})();
