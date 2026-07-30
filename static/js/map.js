let viewer = null;
let currentBounds = null;

// 选区经度归一化:在环绕显示（拖过 ±180）的视图上画框时，东/西边界可能超出
// ±180。后端会拒绝超出 ±180 的四至，这里先 wrap 回标准范围:
// 西边界 wrap 到 [-180,180)，东边界 wrap 到 (-180,180](让 180 保持 180)。
// wrap 只在提交给后端的 payload 里做;
// wrap 后若 east < west(选区跨反经线),由后端报错提示,不在前端静默交换。
function _wrapLngWest(lng) {
    return ((lng + 180) % 360 + 360) % 360 - 180;
}
function _wrapLngEast(lng) {
    return ((lng - 180) % 360 + 360) % 360 - 180;
}

// --- Cesium 基础 --------------------------------------------------------------
// leaflet zoom 与 Cesium 相机高度的粗略换算（只用于初始视图与状态栏读数，
// 不是精确投影换算）。
function _zoomToHeight(z) {
    return 3.0 * 40075017 / Math.pow(2, z);
}
function _heightToZoom(h) {
    const z = Math.log2(3.0 * 40075017 / Math.max(h, 1));
    return Math.max(0, Math.min(21, Math.round(z)));
}

// --- 首屏加载动画（Splash） ----------------------------------------------------
// 进度 = 模拟缓动（封顶 90%）+ 真实就绪事件补完：Cesium Viewer 创建成功且
// 首帧渲染后由 splashReady() 推满并淡出。JS 异常时 stage 原地显示错误，
// 不让用户对着永远转圈的动画猜。
let _splashTimer = null;
let _splashDone = false;

function initSplash() {
    const splash = document.getElementById('splashScreen');
    if (!splash || _splashTimer) return;
    const bar = document.getElementById('splashBar');
    const stage = document.getElementById('splashStage');
    const stages = ['正在初始化地图引擎…', '加载影像服务…', '准备工作台…'];
    let progress = 0;
    let stageIdx = 0;
    _splashTimer = setInterval(function () {
        // 缓动逼近 90%，剩下的 10% 留给真实就绪事件
        progress += (90 - progress) * 0.06 + 0.15;
        if (progress > 90) progress = 90;
        if (bar) bar.style.width = progress.toFixed(1) + '%';
        const target = Math.min(stages.length - 1, Math.floor(progress / 35));
        if (target !== stageIdx) {
            stageIdx = target;
            if (stage) stage.textContent = stages[stageIdx];
        }
    }, 120);
    window.addEventListener('error', function (e) {
        if (_splashDone || !stage) return;
        stage.textContent = '加载出错：' + (e.message || '未知错误');
        stage.classList.add('splash-stage--error');
    });
    // 兜底：正常路径几百毫秒就就绪；万一渲染管线异常，20s 后也不把用户
    // 关在 splash 里（地图已在后面可用）。
    setTimeout(splashReady, 20000);
}

function splashReady() {
    if (_splashDone) return;
    _splashDone = true;
    if (_splashTimer) {
        clearInterval(_splashTimer);
        _splashTimer = null;
    }
    const splash = document.getElementById('splashScreen');
    if (!splash) return;
    const bar = document.getElementById('splashBar');
    const stage = document.getElementById('splashStage');
    if (bar) bar.style.width = '100%';
    if (stage) stage.textContent = '就绪';
    splash.classList.add('splash-screen--done');
    setTimeout(function () { splash.remove(); }, 550);
}

function initMap(config) {
    initSplash();

    const centerLat = parseFloat(config.map_center_lat || 29.56);
    const centerLng = parseFloat(config.map_center_lng || 106.55);
    const initialZoom = parseInt(config.map_initial_zoom || 3);

    // 底图 XYZ 源：配置页可换（内网/自建瓦片服务），留空回退内置 OSM。
    // 不用 Cesium Ion（离线打包工具，不能依赖 Ion token）
    const tileUrl = (config.map_tile_url || '').trim()
        || 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
    viewer = new Cesium.Viewer('map', {
        baseLayer: new Cesium.ImageryLayer(new Cesium.UrlTemplateImageryProvider({
            url: tileUrl,
            tilingScheme: new Cesium.WebMercatorTilingScheme(),
            credit: '© OpenStreetMap contributors',
        })),
        baseLayerPicker: false,
        geocoder: false,
        homeButton: false,
        sceneModePicker: false,
        navigationHelpButton: false,
        animation: false,
        timeline: false,
        fullscreenButton: false,
        infoBox: false,
        selectionIndicator: false,
    });
    viewer.camera.setView({
        destination: Cesium.Cartesian3.fromDegrees(centerLng, centerLat, _zoomToHeight(initialZoom)),
    });

    // 首帧渲染完成 = 地图真正可用，splash 推满淡出
    const onFirstFrame = function () {
        viewer.scene.postRender.removeEventListener(onFirstFrame);
        splashReady();
    };
    viewer.scene.postRender.addEventListener(onFirstFrame);

    // 默认缩放级别与「配置」页同步（默认最小/最大缩放）
    const zoomMinEl = document.getElementById('zoomMin');
    const zoomMaxEl = document.getElementById('zoomMax');
    if (zoomMinEl && String(config.default_zoom_min ?? '') !== '') zoomMinEl.value = config.default_zoom_min;
    if (zoomMaxEl && String(config.default_zoom_max ?? '') !== '') zoomMaxEl.value = config.default_zoom_max;

    _initMapTools();
}

// --- 矩形框选（下载区域）-------------------------------------------------------
// Cesium 没有 Leaflet.draw 那样的现成绘制控件，自己实现：「框选」按钮进入
// 绘制态（相机操作暂停、光标十字），LEFT_DOWN 记起点、MOUSE_MOVE 实时更新
// 矩形 entity、LEFT_UP 落定。落定后选区**可调整**：四个角点手柄可拖拽，
// bounds 浮层里的数值可点击编辑；「删除」清空，「框选」重画。
let _selectionEntity = null;
let _drawing = false;
let _drawStart = null;              // Cartographic
let _rectDegrees = null;            // {west, south, east, north}（度）

// 角点调整手柄：选区落定后出现，拖到哪儿矩形跟到哪儿
const _HANDLE_CORNERS = ['nw', 'ne', 'sw', 'se'];
let _handleEntities = {};           // corner -> entity
let _draggingHandle = null;         // 正在拖拽的角点（'nw' 等）或 null

function _pickCartographic(position) {
    // 优先纯数学的椭球拾取（不依赖 GPU/地形渲染状态），失败再退回 globe.pick
    let cartesian = viewer.camera.pickEllipsoid(position, Cesium.Ellipsoid.WGS84);
    if (!cartesian) {
        const ray = viewer.camera.getPickRay(position);
        if (ray) cartesian = viewer.scene.globe.pick(ray, viewer.scene);
    }
    return cartesian ? Cesium.Cartographic.fromCartesian(cartesian) : null;
}

function _ensureSelectionEntity() {
    if (_selectionEntity) return _selectionEntity;
    _selectionEntity = viewer.entities.add({
        rectangle: {
            coordinates: new Cesium.CallbackProperty(function () {
                return Cesium.Rectangle.fromDegrees(
                    _rectDegrees.west, _rectDegrees.south, _rectDegrees.east, _rectDegrees.north);
            }, false),
            material: Cesium.Color.fromCssColorString('#38bdf8').withAlpha(0.15),
            outline: true,
            outlineColor: Cesium.Color.fromCssColorString('#38bdf8'),
            outlineWidth: 3,
        },
    });
    return _selectionEntity;
}

// 手柄位置直接读 _rectDegrees（CallbackProperty），拖手柄改 _rectDegrees
// 时手柄与矩形一起动，不需要手动同步。
function _cornerPosition(corner) {
    if (!_rectDegrees) return null;
    const lng = corner.indexOf('w') !== -1 ? _rectDegrees.west : _rectDegrees.east;
    const lat = corner.indexOf('n') !== -1 ? _rectDegrees.north : _rectDegrees.south;
    return Cesium.Cartesian3.fromDegrees(lng, lat);
}

function _ensureHandles() {
    if (!_rectDegrees || !viewer) return;
    _HANDLE_CORNERS.forEach(function (corner) {
        if (_handleEntities[corner]) return;
        const entity = viewer.entities.add({
            position: new Cesium.CallbackProperty(function () {
                return _cornerPosition(corner);
            }, false),
            point: {
                pixelSize: 11,
                color: Cesium.Color.fromCssColorString('#38bdf8'),
                outlineColor: Cesium.Color.WHITE,
                outlineWidth: 2,
                // 手柄必须始终压在矩形与地形之上，否则 3D 视角下会被盖住拖不到
                disableDepthTestDistance: Number.POSITIVE_INFINITY,
            },
        });
        entity._selectionHandle = corner;   // scene.pick 命中后据此识别
        _handleEntities[corner] = entity;
    });
}

function _removeHandles() {
    _HANDLE_CORNERS.forEach(function (corner) {
        if (_handleEntities[corner] && viewer) {
            viewer.entities.remove(_handleEntities[corner]);
        }
        delete _handleEntities[corner];
    });
    _draggingHandle = null;
}

// _rectDegrees 是选区的唯一真相；currentBounds 是它的提交态快照。
// 拖手柄 / 数值编辑改了 _rectDegrees 之后走这里同步并刷新浮层。
function _syncBoundsFromRect() {
    if (!_rectDegrees) return;
    currentBounds = {
        north: _rectDegrees.north,
        south: _rectDegrees.south,
        east: _rectDegrees.east,
        west: _rectDegrees.west,
    };
    updateBoundsInfo();
}

function _setRectFromCartographics(a, b) {
    // 键与值必须按方位配对（契约测试逐键扫描）：先落成以方位命名的中间变量。
    const west = Math.min(a.longitude, b.longitude);
    const east = Math.max(a.longitude, b.longitude);
    const south = Math.min(a.latitude, b.latitude);
    const north = Math.max(a.latitude, b.latitude);
    _rectDegrees = {
        west: Cesium.Math.toDegrees(west),
        east: Cesium.Math.toDegrees(east),
        south: Cesium.Math.toDegrees(south),
        north: Cesium.Math.toDegrees(north),
    };
}

function _enterDrawMode() {
    _drawing = true;
    viewer.scene.canvas.style.cursor = 'crosshair';
    const btn = document.getElementById('mapDrawRect');
    if (btn) {
        btn.classList.add('map-panel-btn--active');
        btn.setAttribute('aria-pressed', 'true');
    }
}

function _exitDrawMode() {
    _drawing = false;
    if (viewer) viewer.scene.canvas.style.cursor = '';
    const btn = document.getElementById('mapDrawRect');
    if (btn) {
        btn.classList.remove('map-panel-btn--active');
        btn.setAttribute('aria-pressed', 'false');
    }
}

function clearSelection() {
    if (_selectionEntity && viewer) {
        viewer.entities.remove(_selectionEntity);
        _selectionEntity = null;
    }
    _removeHandles();
    _rectDegrees = null;
    currentBounds = null;
    _exitDrawMode();
    updateBoundsInfo();
    refreshSubmitButtonState();
}

function _initMapTools() {
    const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
    const scc = viewer.scene.screenSpaceCameraController;

    handler.setInputAction(function (event) {
        if (_drawing) {
            const carto = _pickCartographic(event.position);
            if (!carto) return;
            _drawStart = carto;
            scc.enableRotate = false;
            scc.enableTilt = false;
            scc.enableTranslate = false;
            return;
        }
        // 非绘制态：命中角点手柄则开始拖拽调整（同样暂停相机操作）
        if (_rectDegrees) {
            const picked = viewer.scene.pick(event.position, 16, 16);
            const corner = picked && picked.id && picked.id._selectionHandle;
            if (corner) {
                _draggingHandle = corner;
                scc.enableRotate = false;
                scc.enableTilt = false;
                scc.enableTranslate = false;
            }
        }
    }, Cesium.ScreenSpaceEventType.LEFT_DOWN);

    handler.setInputAction(function (event) {
        if (_drawing && _drawStart) {
            const carto = _pickCartographic(event.endPosition);
            if (!carto) return;
            _setRectFromCartographics(_drawStart, carto);
            _ensureSelectionEntity();
            return;
        }
        if (_draggingHandle && _rectDegrees) {
            const carto = _pickCartographic(event.endPosition);
            if (!carto) return;
            const lng = Cesium.Math.toDegrees(carto.longitude);
            const lat = Cesium.Math.toDegrees(carto.latitude);
            const d = _rectDegrees;
            // 钳位在对侧边内侧 1e-6°，拖过对边不会翻转成负宽/负高的矩形
            if (_draggingHandle.indexOf('w') !== -1) d.west = Math.min(lng, d.east - 1e-6);
            else d.east = Math.max(lng, d.west + 1e-6);
            if (_draggingHandle.indexOf('n') !== -1) d.north = Math.max(lat, d.south + 1e-6);
            else d.south = Math.min(lat, d.north - 1e-6);
            d.north = Math.min(90, d.north);
            d.south = Math.max(-90, d.south);
            _syncBoundsFromRect();
        }
    }, Cesium.ScreenSpaceEventType.MOUSE_MOVE);

    handler.setInputAction(function (event) {
        if (_drawing && _drawStart) {
            const carto = _pickCartographic(event.position) || _drawStart;
            _setRectFromCartographics(_drawStart, carto);
            _drawStart = null;
            scc.enableRotate = true;
            scc.enableTilt = true;
            scc.enableTranslate = true;
            _ensureSelectionEntity();
            currentBounds = {
                north: _rectDegrees.north,
                south: _rectDegrees.south,
                east: _rectDegrees.east,
                west: _rectDegrees.west,
            };
            _exitDrawMode();
            _ensureHandles();
            updateBoundsInfo();
            refreshSubmitButtonState();

            // 选区落定后 pulse 浮层上的「下载」按钮，引导下一步
            const dlBtn = document.getElementById('boundsDownloadBtn');
            if (dlBtn) {
                dlBtn.style.animation = 'pulse 0.5s ease-in-out';
                setTimeout(() => { dlBtn.style.animation = ''; }, 500);
            }
            return;
        }
        if (_draggingHandle) {
            _draggingHandle = null;
            scc.enableRotate = true;
            scc.enableTilt = true;
            scc.enableTranslate = true;
            refreshSubmitButtonState();
        }
    }, Cesium.ScreenSpaceEventType.LEFT_UP);

    // 左列工具条按钮
    const drawBtn = document.getElementById('mapDrawRect');
    if (drawBtn) drawBtn.addEventListener('click', function () {
        if (_drawing) _exitDrawMode(); else _enterDrawMode();
    });
    const clearBtn = document.getElementById('mapClearSelection');
    if (clearBtn) clearBtn.addEventListener('click', function () { clearSelection(); });
    const zoomInBtn = document.getElementById('mapZoomIn');
    if (zoomInBtn) zoomInBtn.addEventListener('click', function () {
        viewer.camera.zoomIn(viewer.camera.positionCartographic.height * 0.5);
    });
    const zoomOutBtn = document.getElementById('mapZoomOut');
    if (zoomOutBtn) zoomOutBtn.addEventListener('click', function () {
        viewer.camera.zoomOut(viewer.camera.positionCartographic.height);
    });
}

// 地图样式预览：缩略图是仓库内置的样例瓦片（static/img/map-styles/，
// 重庆 z10 的真实 Google 瓦片快照），完全离线可看。想换样例位置时重新抓
// 五张覆盖即可（lyrs 码 m/s/y/h/t 与下载引擎一致）。
function initMapStylePreview() {
    const sel = document.getElementById('mapStyle');
    const img = document.getElementById('mapStylePreview');
    if (!sel || !img) return;

    function refresh() {
        img.src = `/static/img/map-styles/${sel.value}.png`;
    }
    sel.addEventListener('change', refresh);
    refresh();
}

function initDownloadTypeToggle() {
    // 数据下载表单（#downloadForm）：地图瓦片 / DEM。
    const typeEl = document.getElementById('downloadType');
    if (!typeEl) return;

    const zoomRow = document.getElementById('zoomMin')?.closest('.row');
    const outputFormatField = document.getElementById('outputFormat')?.closest('.mb-3');

    const mapStyleField = document.getElementById('mapStyleField');

    const demOptions = document.getElementById('demOptions');
    // I11：ASTGTM.003 只发布 _dem/_num 颗粒，_swb 水体掩膜不存在（真正的
    // 水体在 ASTWBD.001），勾选必然全部 404；后端同样拒绝该组合。
    // 前端直接禁用并隐藏这个选项，提交时永远带 'false'。
    const swbCheckbox = document.getElementById('demDownloadSwb');
    if (swbCheckbox) {
        swbCheckbox.checked = false;
        swbCheckbox.disabled = true;
        const swbWrap = swbCheckbox.closest('.form-check');
        if (swbWrap) swbWrap.style.display = 'none';
    }

    function apply() {
        const t = typeEl.value;
        const isMap = t === 'map';
        const isDem = t === 'dem';
        // Zoom range is map-only; DEM zoom is fixed by the dataset.
        if (zoomRow) zoomRow.style.display = isDem ? 'none' : '';
        // Output format (tiles/stitch) is map-only.
        if (outputFormatField) outputFormatField.style.display = isMap ? '' : 'none';
        if (mapStyleField) mapStyleField.style.display = isMap ? '' : 'none';
        if (demOptions) demOptions.style.display = isDem ? '' : 'none';

        const outputPath = document.getElementById('outputPath');
        if (outputPath && !outputPath.dataset.userEdited) {
            outputPath.value = isDem ? './downloads/dem' : './downloads/map';
        }

        refreshSubmitButtonState();
    }

    typeEl.addEventListener('change', apply);

    const outputPath = document.getElementById('outputPath');
    if (outputPath) {
        outputPath.addEventListener('input', () => {
            outputPath.dataset.userEdited = '1';
        });
    }

    apply();
}

function initProcessTypeToggle() {
    // 数据处理表单（#processForm）：本地高程切片 / 等高线瓦片。
    const typeEl = document.getElementById('processType');
    if (!typeEl) return;

    const localOptions = document.getElementById('localTerrainOptions');
    const contourOptions = document.getElementById('contourOptions');
    const zoomSection = document.getElementById('processZoomSection');

    function apply() {
        const t = typeEl.value;
        const isLocal = t === 'local_terrain';
        const isContour = t === 'contour';
        if (localOptions) localOptions.style.display = isLocal ? '' : 'none';
        if (contourOptions) contourOptions.style.display = isContour ? '' : 'none';
        // 缩放范围只有等高线用；本地高程的层级在它自己的字段里
        if (zoomSection) zoomSection.style.display = isContour ? '' : 'none';

        refreshSubmitButtonState();
    }

    typeEl.addEventListener('change', apply);
    apply();
    initContourTintUI();
}

// --- 等高线配色自定义 UI ------------------------------------------------------
// 默认值与 ContourStyle 的现行方案一致（后端同样以这些为默认回退）。
const CONTOUR_DEFAULT_LINE_MID = '#9c6b3f';
const CONTOUR_DEFAULT_LINE_IDX = '#7a4f2a';
const CONTOUR_DEFAULT_TINT_BREAKS = [0, 200, 500, 1000, 2000, 3000, 4000, 5000];
const CONTOUR_DEFAULT_TINT_COLORS = [
    '#5e8c61', '#8fbf6f', '#b6cf7e', '#dcd98e', '#d9b97e',
    '#c49a6c', '#ac7f58', '#8e6246', '#f0eae2',
];

function _parseTintBreaks(raw) {
    const out = [];
    String(raw).split(',').forEach((p) => {
        const v = parseFloat(p.trim());
        if (!isNaN(v)) out.push(v);
    });
    return out;
}

function _tintBandLabel(breaks, i) {
    if (breaks.length === 0) return '全部';
    if (i === 0) return '<' + breaks[0];
    if (i === breaks.length) return '≥' + breaks[breaks.length - 1];
    return breaks[i - 1] + '~' + breaks[i];
}

function buildTintStops() {
    const box = document.getElementById('tintColorStops');
    if (!box) return;
    let breaks = _parseTintBreaks(document.getElementById('tintBreaks').value);
    if (breaks.length === 0) breaks = CONTOUR_DEFAULT_TINT_BREAKS.slice();
    const bandCount = breaks.length + 1;
    // 尽量保留用户已选的颜色（断点改了只增删尾部色块）
    const prev = [...box.querySelectorAll('input[type=color]')].map((el) => el.value);
    box.innerHTML = '';
    for (let i = 0; i < bandCount; i++) {
        const wrap = document.createElement('div');
        wrap.className = 'tint-stop';
        const input = document.createElement('input');
        input.type = 'color';
        input.className = 'form-control form-control-color';
        input.value = prev[i] || CONTOUR_DEFAULT_TINT_COLORS[i] || '#cccccc';
        input.title = _tintBandLabel(breaks, i) + ' m';
        const label = document.createElement('span');
        label.className = 'tint-stop-label';
        label.textContent = _tintBandLabel(breaks, i);
        wrap.appendChild(input);
        wrap.appendChild(label);
        box.appendChild(wrap);
    }
}

function resetContourTintUI() {
    const mid = document.getElementById('lineColorIntermediate');
    const idx = document.getElementById('lineColorIndex');
    const brk = document.getElementById('tintBreaks');
    if (mid) mid.value = CONTOUR_DEFAULT_LINE_MID;
    if (idx) idx.value = CONTOUR_DEFAULT_LINE_IDX;
    if (brk) brk.value = CONTOUR_DEFAULT_TINT_BREAKS.join(',');
    buildTintStops();
}

function refreshContourStylePreview() {
    // 样式预览：当前 UI 的配色值全部编进 query，后端用同一套 style_for_task 出图
    const img = document.getElementById('contourStylePreview');
    if (!img) return;
    const q = new URLSearchParams({
        interval: document.getElementById('contourInterval').value || '50',
        background: document.getElementById('contourBackgroundTransparent').checked
            ? 'transparent' : document.getElementById('contourBackground').value,
        terrain_shade: document.getElementById('contourTerrainShade').checked ? '1' : '0',
        line_color_intermediate: document.getElementById('lineColorIntermediate').value,
        line_color_index: document.getElementById('lineColorIndex').value,
        tint_breaks: document.getElementById('tintBreaks').value,
        tint_colors: [...document.querySelectorAll('#tintColorStops input[type=color]')]
            .map((el) => el.value).join(','),
    });
    img.src = '/api/contour/style_preview?' + q.toString();
}

let _stylePreviewTimer = null;
function scheduleContourStylePreview() {
    clearTimeout(_stylePreviewTimer);
    _stylePreviewTimer = setTimeout(refreshContourStylePreview, 300);
}

function initContourTintUI() {
    if (!document.getElementById('tintColorStops')) return;
    buildTintStops();
    const brk = document.getElementById('tintBreaks');
    if (brk) brk.addEventListener('change', function () {
        buildTintStops();
        scheduleContourStylePreview();
    });
    const resetBtn = document.getElementById('tintResetBtn');
    if (resetBtn) resetBtn.addEventListener('click', function () {
        resetContourTintUI();
        scheduleContourStylePreview();
    });
    // 取色器/等高距/背景/着色开关任何变化都刷新预览；色块是动态生成的，
    // 用容器代理。details 首次展开时拉一张。
    ['lineColorIntermediate', 'lineColorIndex', 'contourInterval',
     'contourBackground', 'contourBackgroundTransparent', 'contourTerrainShade']
        .forEach(function (id) {
            const el = document.getElementById(id);
            if (el) {
                el.addEventListener('input', scheduleContourStylePreview);
                el.addEventListener('change', scheduleContourStylePreview);
            }
        });
    document.getElementById('tintColorStops')
        .addEventListener('input', scheduleContourStylePreview);
    const details = document.getElementById('contourStyleCustom');
    if (details) details.addEventListener('toggle', function () {
        if (details.open) refreshContourStylePreview();
    });
    if (details && details.open) refreshContourStylePreview();
}

// 提交按钮的启用条件集中在这里，避免各处只加不减导致状态残留。
// 两张表单各自一颗按钮：数据下载（瓦片/高程）必须先框选；
// 处理类（本地高程切片 / 等高线）都是上传驱动，没有 bbox，无条件启用
// （不检查文件）—— 文件是否已选在提交时由 submitLocalTerrain() /
// submitContour() 各自校验。
function refreshSubmitButtonState() {
    const dlBtn = document.getElementById('createTaskBtn');
    if (dlBtn) dlBtn.disabled = !currentBounds;
    const prBtn = document.getElementById('createProcessBtn');
    if (prBtn) prBtn.disabled = false;
}

// 任务创建成功后复位表单。formId 指明复位哪一张（下载/处理各自独立）。
// clearBounds=false 用于本地高程切片：该模式本来就没有 bbox，
// 清空选区会把用户为下一个任务画好的框也一起删掉。
function resetForm({ clearBounds = true, formId = 'downloadForm' } = {}) {
    const form = document.getElementById(formId);
    if (form) form.reset();

    if (formId === 'downloadForm') {
        const outputPath = document.getElementById('outputPath');
        if (outputPath) delete outputPath.dataset.userEdited;
    }

    if (clearBounds) {
        if (_selectionEntity && viewer) {
            viewer.entities.remove(_selectionEntity);
            _selectionEntity = null;
        }
        _removeHandles();
        _rectDegrees = null;
        currentBounds = null;
        updateBoundsInfo();
    }

    // 让 apply() 重新按当前类型摆好字段可见性和默认路径
    const typeEl = document.getElementById(formId === 'downloadForm' ? 'downloadType' : 'processType');
    if (typeEl) typeEl.dispatchEvent(new Event('change'));

    refreshSubmitButtonState();
}

/**
 * 渲染框选后的四至（#boundsInfo，地图右上角的 .bounds-overlay 浮层），
 * 并同步状态栏的选区摘要（#statusSelection）。
 *
 * 浮层分两段：
 *   1. .bounds-grid —— 4 列网格装 8 个格子（4 键 + 4 值），恰好 2 行。
 *      每个值带 data-field，**点击可编辑**（_beginBoundsEdit 换成输入框，
 *      Enter/失焦提交，Esc 取消）。`N/S/E/W` 键与 currentBounds 字段的
 *      配对关系是数据正确性，由
 *      test_bounds_labels_bind_to_the_right_coordinate 逐对钉住；
 *      `.bounds-sr` 读屏方位词由 test_bounds_readout_is_announced_to_screen_readers
 *      钉住。这两段 markup 不要动结构。
 *   2. .bounds-actions —— 「下载」按钮（打开下载弹窗）+ 调整提示。
 *
 * 小数 5 位（≈1.1m），框选下载范围够用，两个数字并排也放得下。
 */
function updateBoundsInfo() {
    const boundsInfo = document.getElementById('boundsInfo');
    const statusSel = document.getElementById('statusSelection');
    if (currentBounds) {
        const f = (v) => v.toFixed(5);
        boundsInfo.innerHTML = `
            <div class="bounds-grid">
                <span class="bounds-k" aria-hidden="true">N</span><span class="bounds-v" data-field="north" title="点击编辑"><span class="bounds-sr">北纬 </span>${f(currentBounds.north)}</span>
                <span class="bounds-k" aria-hidden="true">S</span><span class="bounds-v" data-field="south" title="点击编辑"><span class="bounds-sr">南纬 </span>${f(currentBounds.south)}</span>
                <span class="bounds-k" aria-hidden="true">E</span><span class="bounds-v" data-field="east" title="点击编辑"><span class="bounds-sr">东经 </span>${f(currentBounds.east)}</span>
                <span class="bounds-k" aria-hidden="true">W</span><span class="bounds-v" data-field="west" title="点击编辑"><span class="bounds-sr">西经 </span>${f(currentBounds.west)}</span>
            </div>
            <div class="bounds-actions">
                <button type="button" class="btn btn-primary btn-sm" id="boundsDownloadBtn">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display: inline-block; vertical-align: middle; margin-right: 4px;">
                        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                        <polyline points="7 10 12 15 17 10"></polyline>
                        <line x1="12" y1="15" x2="12" y2="3"></line>
                    </svg>
                    下载
                </button>
                <span class="bounds-hint">拖拽角点调整 · 点击数值编辑</span>
            </div>
        `;
        if (statusSel) {
            const w = (currentBounds.east - currentBounds.west).toFixed(3);
            const h = (currentBounds.north - currentBounds.south).toFixed(3);
            statusSel.textContent = `已选区域 ${w}° × ${h}°`;
        }
    } else {
        boundsInfo.innerHTML = `
            <small>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display: inline-block; vertical-align: middle; margin-right: 4px;">
                    <circle cx="12" cy="12" r="10"></circle>
                    <line x1="12" y1="16" x2="12" y2="12"></line>
                    <line x1="12" y1="8" x2="12.01" y2="8"></line>
                </svg>
                请在地图上框选下载区域
            </small>
        `;
        if (statusSel) statusSel.textContent = '未选择区域';
    }
}

// --- 选区数值点击编辑 ----------------------------------------------------------
// 点击 .bounds-v 把读数换成输入框；Enter / 失焦提交，Esc 取消。
// 提交经 _applyBoundsEdit 校验（北纬>南纬、纬度 ±90、经度非零宽），
// 非法输入回退原值并 toast。

function _beginBoundsEdit(vEl) {
    if (!currentBounds || vEl.querySelector('input')) return;
    const field = vEl.dataset.field;
    if (!field) return;
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'bounds-edit-input';
    input.value = currentBounds[field].toFixed(5);
    input.setAttribute('aria-label', '编辑' + field);
    vEl.innerHTML = '';
    vEl.appendChild(input);
    input.focus();
    input.select();
    let done = false;
    function commit(apply) {
        if (done) return;
        done = true;
        if (apply) _applyBoundsEdit(field, input.value);
        else updateBoundsInfo();    // 取消：重渲染回原读数
    }
    input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            commit(true);
        } else if (e.key === 'Escape') {
            commit(false);
        }
    });
    input.addEventListener('blur', function () { commit(true); });
}

function _applyBoundsEdit(field, raw) {
    const v = parseFloat(String(raw).trim());
    if (isNaN(v)) {
        showNotification('坐标格式无效：' + raw, 'warning');
        updateBoundsInfo();
        return;
    }
    const b = {
        north: currentBounds.north,
        south: currentBounds.south,
        east: currentBounds.east,
        west: currentBounds.west,
    };
    b[field] = v;
    if (b.north <= b.south) {
        showNotification('北纬必须大于南纬', 'warning');
        updateBoundsInfo();
        return;
    }
    if (b.north > 90 || b.south < -90) {
        showNotification('纬度必须在 ±90° 之间', 'warning');
        updateBoundsInfo();
        return;
    }
    if (Math.abs(b.east - b.west) < 1e-9) {
        showNotification('东西经不能相同（选区宽度为 0）', 'warning');
        updateBoundsInfo();
        return;
    }
    _rectDegrees = { west: b.west, south: b.south, east: b.east, north: b.north };
    _ensureSelectionEntity();
    _ensureHandles();
    _syncBoundsFromRect();
    refreshSubmitButtonState();
}

// --- 下载 / 处理弹窗 -----------------------------------------------------------

// 打开下载弹窗前刷新顶部的选区四至摘要——弹窗可能关过又开，
// 期间用户拖过角点或改过数值，摘要必须反映当前选区。
function openDownloadModal() {
    if (!currentBounds) {
        showNotification('请先在地图上框选下载区域', 'warning');
        return;
    }
    const summary = document.getElementById('downloadModalBounds');
    if (summary) {
        const f = (v) => v.toFixed(5);
        const w = (currentBounds.east - currentBounds.west).toFixed(3);
        const h = (currentBounds.north - currentBounds.south).toFixed(3);
        summary.textContent =
            `选区 N ${f(currentBounds.north)} · S ${f(currentBounds.south)} · ` +
            `E ${f(currentBounds.east)} · W ${f(currentBounds.west)}（${w}° × ${h}°）`;
    }
    const modalEl = document.getElementById('downloadModal');
    if (!modalEl || typeof bootstrap === 'undefined') return;
    bootstrap.Modal.getOrCreateInstance(modalEl).show();
    setTimeout(function () {
        const nameEl = document.getElementById('taskName');
        if (nameEl) nameEl.focus();
    }, 350);
}

// 任务创建成功后：关掉对应弹窗，滑出记录面板让用户看到新任务。
function _afterTaskCreated(modalId) {
    const modalEl = document.getElementById(modalId);
    if (modalEl && typeof bootstrap !== 'undefined') {
        bootstrap.Modal.getOrCreateInstance(modalEl).hide();
    }
    if (window.openPanel) window.openPanel('records');
}

document.getElementById('downloadForm').addEventListener('submit', async function(e) {
    e.preventDefault();

    const downloadType = document.getElementById('downloadType')?.value || 'map';

    if (!currentBounds) {
        showNotification('请先在地图上框选下载区域', 'warning');
        return;
    }

    let taskData;
    let apiUrl;
    if (downloadType === 'dem') {
        taskData = {
            name: document.getElementById('taskName').value,
            north: currentBounds.north,
            south: currentBounds.south,
            east: _wrapLngEast(currentBounds.east),
            west: _wrapLngWest(currentBounds.west),
            dataset: document.getElementById('demDataset')?.value || 'COP-DEM-GLO-30',
            output_path: document.getElementById('outputPath').value,
            download_num: document.getElementById('demDownloadNum')?.checked ? 'true' : 'false',
            download_swb: document.getElementById('demDownloadSwb')?.checked ? 'true' : 'false'
        };
        apiUrl = '/api/dem/tasks';
    } else {
        taskData = {
            name: document.getElementById('taskName').value,
            north: currentBounds.north,
            south: currentBounds.south,
            east: _wrapLngEast(currentBounds.east),
            west: _wrapLngWest(currentBounds.west),
            zoom_min: parseInt(document.getElementById('zoomMin').value),
            zoom_max: parseInt(document.getElementById('zoomMax').value),
            style: document.getElementById('mapStyle').value,
            output_format: document.getElementById('outputFormat').value,
            output_path: document.getElementById('outputPath').value
        };
        apiUrl = '/api/tasks';
    }

    const btn = document.getElementById('createTaskBtn');
    const originalText = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display: inline-block; vertical-align: middle; margin-right: 6px; animation: spin 1s linear infinite;">
            <circle cx="12" cy="12" r="10"></circle>
        </svg>
        创建中...
    `;

    try {
        const response = await fetch(apiUrl, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(taskData)
        });

        const result = await response.json();

        if (response.ok) {
            showNotification('任务创建成功！ID: ' + result.task_id, 'success');
            resetForm();
            loadActiveTasks();
            _afterTaskCreated('downloadModal');
        } else {
            showNotification('创建任务失败: ' + result.error, 'danger');
        }
    } catch (error) {
        showNotification('创建任务失败: ' + error.message, 'danger');
    } finally {
        btn.innerHTML = originalText;
        refreshSubmitButtonState();
    }
});

document.getElementById('processForm').addEventListener('submit', async function(e) {
    e.preventDefault();

    const processType = document.getElementById('processType')?.value || 'local_terrain';

    // Local terrain uploads have no bbox; handle separately and return early.
    if (processType === 'local_terrain') {
        await submitLocalTerrain();
        return;
    }

    // 处理类都是上传驱动，不需要框选；等高线是一站式：创建后立即开始。
    if (processType === 'contour') {
        await submitContour();
    }
});

function showNotification(message, type = 'info') {
    // 委托给全局 showToast（ui.js）。保留函数名，map.js 内 13 处调用无需改动。
    if (window.showToast) {
        return window.showToast(message, type);
    }
    alert(message); // 兜底：ui.js 未加载时退回原生
}

const style = document.createElement('style');
style.textContent = `
    @keyframes spin {
        from { transform: rotate(0deg); }
        to { transform: rotate(360deg); }
    }
    @keyframes fadeOut {
        from { opacity: 1; transform: translateY(0); }
        to { opacity: 0; transform: translateY(-20px); }
    }
`;
document.head.appendChild(style);

// ---------------------------------------------------------------------------
// Contour: one-stop create -> start, plus a map preview overlay for finished
// contour tasks. The shared active-task list (tasks.js) removes cards on
// completion and has no contour branch, so the preview lives here in its own
// panel and listens to the shared socket independently.
// ---------------------------------------------------------------------------

async function submitContour() {
    // 等高线从上传的 GeoTIFF 渲染（数据处理不下载，远程高程下载在「数据下载」
    // 的 DEM 任务里做）。没有 bbox：瓦片范围由后端按 DEM 实际覆盖决定，
    // 文件是否已选在这里校验。
    const fileInput = document.getElementById('contourFiles');
    const files = fileInput?.files;
    if (!files || files.length === 0) {
        showNotification('请先选择至少一个 .tif/.tiff 文件', 'warning');
        return;
    }

    const interval = parseFloat(document.getElementById('contourInterval').value) || 50;
    const bgTransparent = document.getElementById('contourBackgroundTransparent').checked;
    const background = bgTransparent ? 'transparent' : (document.getElementById('contourBackground').value || '#faf6ec');

    const fd = new FormData();
    fd.append('name', document.getElementById('processTaskName').value || '等高线瓦片');
    fd.append('contour_interval', String(interval));
    fd.append('zoom_min', document.getElementById('processZoomMin').value);
    fd.append('zoom_max', document.getElementById('processZoomMax').value);
    fd.append('background', background);
    fd.append('terrain_shade', document.getElementById('contourTerrainShade')?.checked ? '1' : '0');
    // 配色自定义：总是发送当前 UI 值 —— 用户看到什么就渲染什么；
    // 恢复默认后这些值等于后端的默认方案。
    fd.append('line_color_intermediate', document.getElementById('lineColorIntermediate').value);
    fd.append('line_color_index', document.getElementById('lineColorIndex').value);
    fd.append('tint_breaks', document.getElementById('tintBreaks').value);
    fd.append('tint_colors', [...document.querySelectorAll('#tintColorStops input[type=color]')].map((el) => el.value).join(','));
    for (const f of files) {
        fd.append('files', f);
    }

    const btn = document.getElementById('createProcessBtn');
    const original = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '上传中...';
    try {
        const createResp = await fetch('/api/contour/tasks', { method: 'POST', body: fd });
        const created = await createResp.json();
        if (!createResp.ok) {
            showNotification('创建失败: ' + (created.error || createResp.status), 'danger');
            return;
        }
        const startResp = await fetch(`/api/contour/tasks/${created.task_id}/start`, { method: 'POST' });
        if (!startResp.ok) {
            const started = await startResp.json().catch(() => ({}));
            showNotification('任务已创建但启动失败: ' + (started.error || startResp.status), 'danger');
            return;
        }
        showNotification('等高线任务已开始（上传 DEM → 渲染瓦片）', 'success');
        resetForm({ formId: 'processForm' });
        resetContourTintUI();
        loadActiveTasks();
        _afterTaskCreated('processModal');
    } catch (err) {
        showNotification('创建失败: ' + err.message, 'danger');
    } finally {
        btn.innerHTML = original;
        refreshSubmitButtonState();
    }
}

// --- Contour preview overlay ------------------------------------------------

let contourPreviewActiveId = null;

// --- 历史任务预览（主视图叠加） --------------------------------------------------
// 各类已完成任务的可视化输出在 Cesium 主视图上预览：
//   地图瓦片任务 -> XYZ 瓦片叠加（/tiles/<id>/，按任务 output_path 服务）
//   等高线任务   -> XYZ 瓦片叠加（/contour/<id>/）
//   本地高程切片 -> Cesium 地形（/terrain/local/<id>/layer.json）
//   DEM 任务     -> 地形切片存在时按 Cesium 地形预览（/terrain/dem/<id>/layer.json）
// 同一时刻只有一个预览；「关闭预览」撤掉影像层并还原默认椭球地形。
let _previewState = null;   // { kind: 'imagery'|'terrain', taskId, name, layer?, prevTerrainProvider? }

function stopTaskPreview() {
    if (viewer && _previewState) {
        if (_previewState.layer) {
            viewer.imageryLayers.remove(_previewState.layer, true);
        }
        if (_previewState.prevTerrainProvider) {
            viewer.terrainProvider = _previewState.prevTerrainProvider;
        }
    }
    _previewState = null;
    contourPreviewActiveId = null;
    updateContourPreviewButtons();
    _renderPreviewChip();
}

async function previewTask(task) {
    if (!viewer) return;
    stopTaskPreview();
    const t = task.task_type;
    if (t === 'map' || t === 'contour') {
        const base = t === 'map' ? `/tiles/${task.id}` : `/contour/${task.id}`;
        const layer = viewer.imageryLayers.addImageryProvider(
            new Cesium.UrlTemplateImageryProvider({
                url: `${base}/{z}/{x}/{y}.png`,
                tilingScheme: new Cesium.WebMercatorTilingScheme(),
                maximumLevel: task.zoom_max || undefined,
            })
        );
        layer.alpha = 0.9;
        _previewState = { kind: 'imagery', taskId: task.id, name: task.name, layer };
        if (t === 'contour') contourPreviewActiveId = task.id;
    } else if (t === 'local_terrain' || t === 'dem') {
        const url = t === 'local_terrain'
            ? `/terrain/local/${task.id}/layer.json`
            : `/terrain/dem/${task.id}/layer.json`;
        // 地形切片不存在时 layer.json 404：先探一下，没有就只飞到区域
        const ok = await fetch(url, { method: 'HEAD' }).then((r) => r.ok).catch(() => false);
        if (ok) {
            const prev = viewer.terrainProvider;
            viewer.terrainProvider = await Cesium.CesiumTerrainProvider.fromUrl(url);
            _previewState = { kind: 'terrain', taskId: task.id, name: task.name, prevTerrainProvider: prev };
        } else {
            showNotification(t === 'dem'
                ? '该任务还没有地形切片（可在详情里启动），仅定位到区域'
                : '切片文件不存在，仅定位到区域', 'info');
        }
    }
    if (task.north != null && task.south != null && task.east != null && task.west != null) {
        viewer.camera.flyTo({
            destination: Cesium.Rectangle.fromDegrees(task.west, task.south, task.east, task.north),
            duration: 1.2,
        });
    }
    _renderPreviewChip();
    updateContourPreviewButtons();
}

// 预览中的浮动提示条（地图右下、状态栏上方）
function _renderPreviewChip() {
    let chip = document.getElementById('taskPreviewChip');
    if (!_previewState) {
        if (chip) chip.remove();
        return;
    }
    if (!chip) {
        chip = document.createElement('div');
        chip.id = 'taskPreviewChip';
        chip.className = 'task-preview-chip';
        document.querySelector('.index-map').appendChild(chip);
    }
    chip.innerHTML = `
        <span>预览中：<strong>${escapeHtml(_previewState.name)}</strong>（#${_previewState.taskId}）</span>
        <button type="button" class="btn btn-sm btn-secondary" onclick="stopTaskPreview()">关闭预览</button>
    `;
}

function toggleContourPreview(taskId, zoomMax) {
    // 当前预览就是这个任务 -> 再点一次关掉；否则切过去（预览管理器统一管）。
    if (_previewState && _previewState.kind === 'imagery' && _previewState.taskId === taskId) {
        stopTaskPreview();
        return;
    }
    const info = contourPreviewTasks.get(taskId) || {};
    // 不传 bbox 键：previewTask 的 `task.north != null` 检查会跳过 flyTo，
    // 也避免造一个无意义的 null-bbox 字面量（契约测试会逐键扫方位配对）。
    previewTask({
        id: taskId, task_type: 'contour', zoom_max: zoomMax,
        name: info.name || ('等高线 #' + taskId),
    });
}

// Completed contour tasks available for preview: id -> {name, zoom_max}.
const contourPreviewTasks = new Map();

function contourPreviewPanel() {
    let panel = document.getElementById('contourPreviewPanel');
    if (!panel) {
        const mapEl = document.getElementById('map');
        if (!mapEl) return null;
        panel = document.createElement('div');
        panel.id = 'contourPreviewPanel';
        panel.style.marginTop = '0.75rem';
        // Place the panel right under the map card body.
        mapEl.parentNode.appendChild(panel);
    }
    return panel;
}

function updateContourPreviewButtons() {
    const panel = contourPreviewPanel();
    if (!panel) return;
    if (contourPreviewTasks.size === 0) {
        panel.innerHTML = '';
        return;
    }
    const rows = [];
    contourPreviewTasks.forEach((info, id) => {
        const active = contourPreviewActiveId === id;
        rows.push(`
            <button type="button"
                    class="btn btn-sm ${active ? 'btn-primary' : 'btn-outline-primary'}"
                    style="margin: 0 6px 6px 0;"
                    onclick="toggleContourPreview(${id}, ${info.zoom_max || 'null'})">
                ${active ? '隐藏预览' : '在地图上预览'}：${escapeHtml(info.name || ('等高线 #' + id))}
            </button>
        `);
    });
    panel.innerHTML = `
        <div class="alert alert-info" style="margin-bottom:0;">
            <small><strong>已完成的等高线瓦片</strong></small><br>
            ${rows.join('')}
        </div>
    `;
}

async function registerCompletedContourTask(taskId) {
    // Pull zoom_max + name for the just-finished task from the list endpoint.
    try {
        const resp = await fetch('/api/contour/tasks');
        const data = await resp.json();
        const task = (data.tasks || []).find(t => t.id === taskId);
        if (task) {
            contourPreviewTasks.set(taskId, { name: task.name, zoom_max: task.zoom_max });
        } else {
            contourPreviewTasks.set(taskId, { name: '等高线 #' + taskId, zoom_max: null });
        }
    } catch (e) {
        contourPreviewTasks.set(taskId, { name: '等高线 #' + taskId, zoom_max: null });
    }
    updateContourPreviewButtons();
}

// Hook the shared socket (created in tasks.js' initTasks). Called from the page
// init block after initTasks(), so `socket` is already assigned.
function initContourPreview() {
    if (typeof socket === 'undefined' || !socket) return;
    socket.on('task_completed', function(data) {
        if (data && data.task_type === 'contour') {
            registerCompletedContourTask(data.task_id);
        }
    });
    // On page load, surface any already-completed contour tasks for preview.
    fetch('/api/contour/tasks').then(r => r.json()).then(data => {
        (data.tasks || []).forEach(t => {
            if (t.status === 'completed') {
                contourPreviewTasks.set(t.id, { name: t.name, zoom_max: t.zoom_max });
            }
        });
        updateContourPreviewButtons();
    }).catch(() => {});
}

async function submitLocalTerrain() {
    const fileInput = document.getElementById('localTerrainFiles');
    const files = fileInput?.files;
    if (!files || files.length === 0) {
        showNotification('请先选择至少一个 .tif/.tiff 文件', 'warning');
        return;
    }

    const fd = new FormData();
    fd.append('name', document.getElementById('processTaskName').value || '本地高程切片');
    fd.append('maxzoom', document.getElementById('localTerrainMaxzoom')?.value || '14');
    for (const f of files) {
        fd.append('files', f);
    }

    const btn = document.getElementById('createProcessBtn');
    btn.disabled = true;
    const original = btn.innerHTML;
    btn.innerHTML = '上传中...';
    try {
        const resp = await fetch('/api/terrain/local/tasks', { method: 'POST', body: fd });
        const result = await resp.json();
        if (resp.ok) {
            showNotification('上传成功，已开始切片！ID: ' + result.task_id, 'success');
            resetForm({ clearBounds: false, formId: 'processForm' });
            loadActiveTasks();
            _afterTaskCreated('processModal');
        } else {
            showNotification('上传失败: ' + (result.error || resp.status), 'danger');
        }
    } catch (err) {
        showNotification('上传失败: ' + err.message, 'danger');
    } finally {
        btn.innerHTML = original;
        refreshSubmitButtonState();
    }
}

/**
 * 工作台行为：状态栏读数（鼠标经纬度 / 缩放级别 / 选区摘要 / 时钟）、
 * bounds 浮层交互（下载按钮、数值点击编辑）。在 initMap 之后由页面
 * init 块调用（index.html）。
 */
function initMapWorkbench() {
    if (!viewer) return;

    const coordsEl = document.getElementById('statusCoords');
    const zoomEl = document.getElementById('statusZoom');

    // 缩放级别：相机高度换算的近似 zoom，300ms 轮询（camera.changed 太高频）
    function updateZoom() {
        if (zoomEl) zoomEl.textContent = 'z' + _heightToZoom(viewer.camera.positionCartographic.height);
    }
    setInterval(updateZoom, 300);
    updateZoom();

    // 鼠标经纬度：50ms 节流，避免 mousemove 高频刷新
    if (coordsEl) {
        let pending = null;
        const moveHandler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
        moveHandler.setInputAction(function (event) {
            pending = event.endPosition;
        }, Cesium.ScreenSpaceEventType.MOUSE_MOVE);
        setInterval(function () {
            if (!pending) return;
            const carto = _pickCartographic(pending);
            pending = null;
            if (!carto) return;
            coordsEl.textContent =
                '经度 ' + Cesium.Math.toDegrees(carto.longitude).toFixed(4) + '°  纬度 ' +
                Cesium.Math.toDegrees(carto.latitude).toFixed(4) + '°';
        }, 50);
        viewer.scene.canvas.addEventListener('mouseout', function () {
            pending = null;
            coordsEl.textContent = '经度 — 纬度 —';
        });
    }

    // 状态栏时钟：本地时间 HH:MM:SS，1s 刷新
    const clockEl = document.getElementById('statusClock');
    if (clockEl) {
        const tick = function () {
            const d = new Date();
            const p = (n) => String(n).padStart(2, '0');
            clockEl.textContent = p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
        };
        tick();
        setInterval(tick, 1000);
    }

    // bounds 浮层交互（事件代理，浮层内容每次 updateBoundsInfo 都重渲染）：
    // 「下载」按钮 -> 下载弹窗；.bounds-v 数值 -> 点击编辑。
    const boundsInfo = document.getElementById('boundsInfo');
    if (boundsInfo) {
        boundsInfo.addEventListener('click', function (e) {
            const dl = e.target.closest('#boundsDownloadBtn');
            if (dl) {
                openDownloadModal();
                return;
            }
            const v = e.target.closest('.bounds-v');
            if (v && currentBounds) _beginBoundsEdit(v);
        });
    }

    // 首屏填充「请在地图上框选下载区域」提示与状态栏选区摘要
    updateBoundsInfo();
}
