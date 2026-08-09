let viewer = null;
let currentBounds = null;

// 选区经度归一化:在环绕显示（拖过 ±180）的视图上画框时，东/西边界可能超出
// ±180。后端会拒绝超出 ±180 的四至，这里先 wrap 回标准范围:
// 西边界 wrap 到 [-180,180)，东边界 wrap 到 (-180,180](让 180 保持 180;
// 东边界输入 -180 与 180 是同一条经线，同样归一到 180)。
// wrap 只在提交给后端的 payload 里做;
// wrap 后若 east < west(选区跨反经线),由后端报错提示,不在前端静默交换。
function _wrapLngWest(lng) {
    return ((lng + 180) % 360 + 360) % 360 - 180;
}
function _wrapLngEast(lng) {
    // 先按西边界口径 wrap 到 [-180,180)，再把 -180 翻到 180，
    // 保证东经 180 不会被折成 -180(east < west 会被后端 400 拒掉)。
    const w = _wrapLngWest(lng);
    return w === -180 ? 180 : w;
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

// --- 瓦片数量预估（下载弹窗实时提示） ------------------------------------------
// 与 services/download_engine.py 的 calculate_tiles 同一公式（Web Mercator
// deg2num，逐 zoom 求 (x 跨度 + 1) × (y 跨度 + 1) 再累加）。前端预估与
// 后端硬上限（TASK_TILE_LIMIT）同口径，超限在提交前就拦下，而不是等 400。
const TASK_TILE_LIMIT = 100000;

function _latLonToTile(lat, lon, zoom) {
    lat = Math.max(-85.0511, Math.min(85.0511, lat));
    const n = Math.pow(2, zoom);
    let x = Math.floor((lon + 180) / 360 * n);
    const latRad = lat * Math.PI / 180;
    let y = Math.floor((1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2 * n);
    x = Math.max(0, Math.min(n - 1, x));
    y = Math.max(0, Math.min(n - 1, y));
    return [x, y];
}

function estimateTileCount(bounds, zoomMin, zoomMax) {
    let total = 0;
    for (let z = zoomMin; z <= zoomMax; z++) {
        let [xMin, yMax] = _latLonToTile(bounds.south, bounds.west, z);
        let [xMax, yMin] = _latLonToTile(bounds.north, bounds.east, z);
        if (xMin > xMax) [xMin, xMax] = [xMax, xMin];
        if (yMin > yMax) [yMin, yMax] = [yMax, yMin];
        total += (xMax - xMin + 1) * (yMax - yMin + 1);
    }
    return total;
}

// 单选组取值：下载类型从下拉改成 radio 后，统一从 :checked 取。
function _radioValue(name, fallback) {
    return document.querySelector(`input[name="${name}"]:checked`)?.value || fallback;
}

// 输出格式多选（瓦片 / GeoTIFF 两个 checkbox）映射回后端 OutputFormat 枚举：
// 都勾 → both；只勾瓦片 → tiles_only；只勾 GeoTIFF → image_only；
// 都不勾 → null（提交时拦下，不可能有没有产出的下载）。
function _outputFormatValue() {
    const tiles = document.getElementById('outputFormatTiles')?.checked;
    const geotiff = document.getElementById('outputFormatGeoTiff')?.checked;
    if (tiles && geotiff) return 'both';
    if (tiles) return 'tiles_only';
    if (geotiff) return 'image_only';
    return null;
}

// 刷新 #tileEstimate 读数，返回 {count, over}（无选区/DEM 模式返回 null）。
// 0.1.4 起瓦片数是软阈值：不再禁用提交，只提示并在提交时要求二次确认。
// DEM 下载按颗粒计、不用瓦片数，高程模式下隐藏读数。
function updateTileEstimate() {
    const el = document.getElementById('tileEstimate');
    if (!el) return null;
    const type = _radioValue('downloadType', 'map');
    if (type === 'dem' || !currentBounds) {
        el.hidden = true;
        return null;
    }
    const zMin = parseInt(document.getElementById('zoomMin')?.value, 10);
    const zMax = parseInt(document.getElementById('zoomMax')?.value, 10);
    if (isNaN(zMin) || isNaN(zMax) || zMin > zMax) {
        el.hidden = true;
        return null;
    }
    // 与提交时的 wrap 口径一致（_wrapLngWest/_wrapLngEast）：wrap 后 east < west
    // 说明选区跨反经线，后端会 400 拒绝——预估同样给不出有意义的数，不算数，
    // 而不是静默 swap 东西边界算一个必然提交失败的瓦片数。
    const west = _wrapLngWest(currentBounds.west);
    const east = _wrapLngEast(currentBounds.east);
    if (east < west) {
        el.textContent = t('js.map.tile_estimate.antimeridian');
        el.classList.remove('tile-estimate--over');
        el.hidden = false;
        return null;
    }
    const count = estimateTileCount({ ...currentBounds, west, east }, zMin, zMax);
    const formatted = count.toLocaleString('zh-CN');
    const over = count > TASK_TILE_LIMIT;
    if (over) {
        const hours = (count / 10 / 3600).toFixed(1);
        el.textContent = t('js.map.tile_estimate.over', { count: formatted, hours: hours });
        el.classList.add('tile-estimate--over');
    } else {
        el.textContent = t('js.map.tile_estimate.count', { count: formatted });
        el.classList.remove('tile-estimate--over');
    }
    el.hidden = false;
    return { count, over };
}

// --- 首屏加载动画（Splash） ----------------------------------------------------
// 进度 = 模拟缓动（封顶 90%）+ 真实就绪事件补完：Cesium Viewer 创建成功且
// 首帧渲染后由 splashReady() 推满并淡出。JS 异常时 stage 原地显示错误，
// 不让用户对着永远转圈的动画猜。
let _splashTimer = null;
let _splashDone = false;

// 具名而不是写成 initSplash 里的内联匿名函数：匿名版本没有引用可以传给
// removeEventListener，于是这个 window 级监听器会活到页面关闭 —— 而
// splashReady 里 splash.remove() 之后，它闭包持有的整棵 splash 子树已经脱离
// 文档却仍被钉在内存里。这里改成现查 DOM + 由 splashReady 显式摘除。
function _onSplashError(e) {
    if (_splashDone) return;
    const stage = document.getElementById('splashStage');
    if (!stage) return;
    stage.textContent = t('js.map.splash.error', {
        message: e.message || t('js.map.splash.unknown_error'),
    });
    stage.classList.add('splash-stage--error');
}

function initSplash() {
    const splash = document.getElementById('splashScreen');
    if (!splash || _splashTimer) return;
    const bar = document.getElementById('splashBar');
    const stage = document.getElementById('splashStage');
    const stages = [
        t('js.map.splash.stage_engine'),
        t('js.map.splash.stage_imagery'),
        t('js.map.splash.stage_workbench'),
    ];
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
    window.addEventListener('error', _onSplashError);
    // 兜底：正常路径几百毫秒就就绪；万一渲染管线异常，20s 后也不把用户
    // 关在 splash 里（地图已在后面可用）。
    setTimeout(splashReady, 20000);
}

function splashReady() {
    if (_splashDone) return;
    _splashDone = true;
    // 摘监听必须排在下面 `if (!splash) return` 之前：否则 splash 已被移除的
    // 那条路径会带着监听器直接返回，泄漏原样留着。
    window.removeEventListener('error', _onSplashError);
    if (_splashTimer) {
        clearInterval(_splashTimer);
        _splashTimer = null;
    }
    const splash = document.getElementById('splashScreen');
    if (!splash) return;
    const bar = document.getElementById('splashBar');
    const stage = document.getElementById('splashStage');
    if (bar) bar.style.width = '100%';
    if (stage) stage.textContent = t('js.map.splash.ready');
    splash.classList.add('splash-screen--done');
    setTimeout(function () { splash.remove(); }, 550);
}

function initMap(config, basemap) {
    initSplash();

    const centerLat = parseFloat(config.map_center_lat || 29.56);
    const centerLng = parseFloat(config.map_center_lng || 106.55);
    const initialZoom = parseInt(config.map_initial_zoom || 3);

    // 底图由**服务端**解析（src/services/basemap_source.py -> routes/main.py），
    // 这里不再自己展开别名。改造前这里有一份 _baseMapUrl 平行实现：它写死
    // lyrs=m（路网图，不是卫星图）、写死署名 © OpenStreetMap（而实际加载的
    // 是 Google 瓦片），并且与 services/tile_url_probe 的条目语义各写各的。
    // 底图与下载源现在是两个配置：用途不同（底图给页面看、tile_servers 是下载
    // 源），出网路径其实相同 —— 底图瓦片由服务端转发，一样吃 proxy_url。
    const bm = basemap || {};
    viewer = new Cesium.Viewer('map', {
        baseLayer: new Cesium.ImageryLayer(new Cesium.UrlTemplateImageryProvider({
            url: bm.url,
            tilingScheme: new Cesium.WebMercatorTilingScheme(),
            // 超出这一层瓦片服务器返回 404，Cesium 画成空白 —— 不设上限的话
            // 放大过头是一片黑，看不出是缩放过头还是底图挂了。
            maximumLevel: bm.max_level == null ? undefined : bm.max_level,
            credit: bm.credit || '',
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
        // 星空盒关掉：864 KB 的六面贴图，加上它触发的 1.8 MB IAU2006_XYS
        // 岁差章动数据（星空是惯性系固定的，渲染它要做 ICRF→Fixed 变换）。
        // 本工具是俯视地图作业，正常视角根本看不到星空。
        // ⚠️ skyAtmosphere **不关**：地球边缘那圈蓝色大气辉光是 shader 算的、
        // 不吃贴图，关掉纯亏视觉。
        skyBox: false,
    });
    // 月亮同理（moonSmall.jpg）。sun 留着 —— 它是 shader 画的不吃贴图，
    // 而且地形光照（enableLighting）用的是 scene.light 的太阳方向，
    // 与这个可见圆盘是两回事，关它并不省什么。
    viewer.scene.moon = undefined;
    // 按需渲染：默认模式每帧重绘（60fps 空转耗电），改为仅在场景变化时渲染。
    // 注意：CallbackProperty（选区矩形/角点手柄）的值变化不会自动触发重绘，
    // 拖拽等直接改 _rectDegrees 的路径必须显式调 scene.requestRender()。
    viewer.scene.requestRenderMode = true;
    viewer.scene.maximumRenderTimeChange = Infinity;

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
    // 地形光照开关（工具条「光照」按钮）：模块在 base.html 里定义，
    // 这里把 viewer 交给它并按 localStorage 里的偏好落一次状态。
    if (window.TerrainLighting) window.TerrainLighting.init(viewer);
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

// 拖拽期 bounds 浮层刷新合并：拖角点每个 MOUSE_MOVE 都会走到
// _syncBoundsFromRect，整层 innerHTML 重写（含 SVG 模板）太贵，用 rAF
// 合并成一帧一次。updateBoundsInfo 本体保持同步（模板被契约测试钉住），
// 低频调用点（LEFT_UP / clearSelection / 编辑校验失败回退）仍直接调。
let _boundsInfoRaf = 0;
function _scheduleBoundsInfoUpdate() {
    if (_boundsInfoRaf) return;
    _boundsInfoRaf = requestAnimationFrame(function () {
        _boundsInfoRaf = 0;
        updateBoundsInfo();
    });
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
    _scheduleBoundsInfoUpdate();
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
            // requestRenderMode 下相机不动不会自动重绘，矩形是 CallbackProperty，
            // 改了 _rectDegrees 必须显式请求一帧
            viewer.scene.requestRender();
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
            // 同上：拖手柄只改 _rectDegrees，需显式请求重绘
            viewer.scene.requestRender();
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
            // 落定点可能与最后一次 MOUSE_MOVE 不同，矩形仍是 CallbackProperty，
            // 显式请求一帧保证落定形状立即显示
            viewer.scene.requestRender();
            currentBounds = {
                north: _rectDegrees.north,
                south: _rectDegrees.south,
                east: _rectDegrees.east,
                west: _rectDegrees.west,
            };
            _exitDrawMode();
            _ensureHandles();
            updateBoundsInfo();
            announceBounds();
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
    // 数据下载表单（#downloadForm）：地图瓦片 / DEM（radio 组，按 name 取）。
    const typeRadios = document.querySelectorAll('input[name="downloadType"]');
    if (!typeRadios.length) return;

    const zoomRow = document.getElementById('zoomMin')?.closest('.row');
    const outputFormatField = document.querySelector('input[name="outputFormat"]')?.closest('.mb-3');

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
        if (swbWrap) swbWrap.hidden = true;
    }

    function apply() {
        const t = _radioValue('downloadType', 'map');
        const isMap = t === 'map';
        const isDem = t === 'dem';
        // Zoom range is map-only; DEM zoom is fixed by the dataset.
        if (zoomRow) zoomRow.hidden = isDem;
        // Output format (tiles/stitch) is map-only.
        if (outputFormatField) outputFormatField.hidden = !(isMap);
        if (mapStyleField) mapStyleField.hidden = !(isMap);
        if (demOptions) demOptions.hidden = !(isDem);

        const outputPath = document.getElementById('outputPath');
        if (outputPath && !outputPath.dataset.userEdited) {
            // 默认保存路径一律绝对:default_save_path 已在 init_database 归一成
            // 绝对值(0.2.3 起建任务拒相对路径);拿不到配置仅是模板未注入的兜底。
            const base = ((typeof config !== 'undefined' && config && config.default_save_path) || './downloads')
                .trim().replace(/[\\/]+$/, '');
            outputPath.value = base + (isDem ? '/dem' : '/map');
        }

        // 切到 DEM 时隐藏读数、切回瓦片时按当前选区/缩放重算——不刷的话
        // 高程模式下会残留上一次地图模式的旧读数。
        updateTileEstimate();
        refreshSubmitButtonState();
    }

    typeRadios.forEach(function (r) { r.addEventListener('change', apply); });

    // 缩放级别变化实时刷新瓦片预估（顺带经 refreshSubmitButtonState 更新按钮态）
    ['zoomMin', 'zoomMax'].forEach(function (id) {
        const el = document.getElementById(id);
        if (el) el.addEventListener('input', function () { updateTileEstimate(); refreshSubmitButtonState(); });
    });

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
    // 字段可见性是「处理类型 × 数据来源」二维的：来源为已下载的 DEM 任务时
    // 用不上上传控件，改成从 #processDemTask 里挑任务。
    const typeEl = document.getElementById('processType');
    if (!typeEl) return;

    const sourceEl = document.getElementById('processSource');
    const localOptions = document.getElementById('localTerrainOptions');
    const contourOptions = document.getElementById('contourOptions');
    const zoomSection = document.getElementById('processZoomSection');
    const localUploadRow = document.getElementById('localTerrainUploadRow');
    const contourUploadRow = document.getElementById('contourUploadRow');
    const demTaskRow = document.getElementById('processDemTaskRow');
    const nameRow = document.getElementById('processNameRow');
    const nameInput = document.getElementById('processTaskName');

    function apply() {
        const type = typeEl.value;
        const source = sourceEl?.value || 'upload';
        const isLocal = type === 'local_terrain';
        const isContour = type === 'contour';
        const fromDemTask = source === 'dem_task';
        if (localOptions) localOptions.hidden = !(isLocal);
        if (contourOptions) contourOptions.hidden = !(isContour);
        // 缩放范围只有等高线用；本地高程的层级在它自己的字段里
        if (zoomSection) zoomSection.hidden = !(isContour);

        if (localUploadRow) localUploadRow.hidden = !(isLocal && !fromDemTask);
        if (contourUploadRow) contourUploadRow.hidden = !(isContour && !fromDemTask);
        if (demTaskRow) demTaskRow.hidden = !(fromDemTask);

        // 「本地高程切片 + DEM 任务」这一格复用 DEM 任务自己的地形切片作业，
        // 不新建任务、没有独立任务名，留着输入框是误导。required 必须跟着摘掉：
        // 隐藏的 required 字段会让浏览器原生校验拦下 submit 事件，按钮点了没反应。
        const nameless = isLocal && fromDemTask;
        if (nameRow) nameRow.hidden = nameless;
        if (nameInput) nameInput.required = !nameless;

        refreshSubmitButtonState();
    }

    typeEl.addEventListener('change', apply);
    if (sourceEl) {
        sourceEl.addEventListener('change', () => {
            apply();
            // 每次切到 DEM 来源都重拉：任务列表随下载进度变化，陈旧列表会让
            // 用户选到一个当时还没完成的任务。
            if (sourceEl.value === 'dem_task') loadProcessDemTasks();
        });
    }
    apply();
    initContourTintUI();
}

// 处理表单当前选中的 DEM 任务 id；下拉处在空态（disabled 占位）时返回 null。
function _selectedProcessDemTaskId() {
    const sel = document.getElementById('processDemTask');
    if (!sel || sel.disabled) return null;
    return sel.value || null;
}

// 用「已完成」的 DEM 下载任务填 #processDemTask。
// 刻意不带 status 查询参数：后端只对 status=active 做特殊处理，completed 不过滤，
// 所以拉全量在前端筛。
async function loadProcessDemTasks() {
    const sel = document.getElementById('processDemTask');
    if (!sel) return;

    function setEmpty() {
        sel.innerHTML = '';
        const opt = document.createElement('option');
        opt.value = '';
        opt.disabled = true;
        opt.textContent = t('js.map.process.no_completed_dem_task');
        sel.appendChild(opt);
        sel.disabled = true;
    }

    try {
        const resp = await fetch('/api/dem/tasks');
        if (!resp.ok) {
            showNotification(t('js.map.process.dem_task_load_failed', {
                error: resp.status,
            }), 'danger');
            setEmpty();
            return;
        }
        const data = await resp.json();
        const tasks = (data.tasks || [])
            .filter((task) => task.status === 'completed')
            .sort((a, b) => b.id - a.id);
        if (tasks.length === 0) {
            setEmpty();
            return;
        }
        sel.innerHTML = '';
        sel.disabled = false;
        for (const task of tasks) {
            const opt = document.createElement('option');
            opt.value = String(task.id);
            opt.textContent = `#${task.id} ${task.name || ''}`.trim();
            sel.appendChild(opt);
        }
    } catch (err) {
        showNotification(t('js.map.process.dem_task_load_failed', {
            error: err.message,
        }), 'danger');
        setEmpty();
    }
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
    if (breaks.length === 0) return t('js.map.tint.band_all');
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
// 两张表单各自一颗按钮，**都不按业务前置条件禁用**，只在提交进行中由各自的
// 提交处理器临时 disabled 上锁（finally 里回到这里解锁）。
//
// 下载按钮为什么不再按 currentBounds 禁用（B4）：disabled 的元素不可聚焦、
// 不在 tab 序里，键盘用户 Tab 过整张表单根本碰不到它，也就无从知道「为什么
// 不能提交」——把解释挂成 aria-describedby 也没用，读屏不会去念一个焦点永远
// 落不上去的元素。改成常态可用后，缺选区由 #downloadForm 的 submit 处理器
// toast js.map.download.need_selection 当场说明原因，与 openDownloadModal()
// 同一条文案、同一个口径。
// 处理类（本地高程切片 / 等高线）都是上传驱动，没有 bbox，同样无条件启用
//（不检查文件）—— 文件是否已选在提交时由 submitLocalTerrain() /
// submitContour() 各自校验。
function refreshSubmitButtonState() {
    const dlBtn = document.getElementById('createTaskBtn');
    if (dlBtn) dlBtn.disabled = false;
    const prBtn = document.getElementById('createProcessBtn');
    if (prBtn) prBtn.disabled = false;
}

// 任务创建成功后复位表单。formId 指明复位哪一张（下载/处理各自独立）。
// clearBounds=false 用于两条上传驱动的处理类分支（本地高程切片/等高线）：
// 它们本来就没有 bbox，清空选区会把用户为下一个任务画好的框也一起删掉。
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

    // 让 apply() 重新按当前类型摆好字段可见性和默认路径。
    // 下载类型是 radio 组（按 name 取选中项），处理类型仍是 <select>（按 id 取）。
    const fieldName = formId === 'downloadForm' ? 'downloadType' : 'processType';
    const typeEl = document.getElementById(fieldName)
        || document.querySelector(`input[name="${fieldName}"]:checked`);
    if (typeEl) typeEl.dispatchEvent(new Event('change'));
    // form.reset() 会把 #processSource 拨回默认的「上传文件」，来源相关字段
    // （上传控件 / DEM 任务下拉）必须跟着复位，否则下次打开弹窗看到的是上一次
    // 来源的字段组合。
    if (formId === 'processForm') {
        const sourceEl = document.getElementById('processSource');
        if (sourceEl) sourceEl.dispatchEvent(new Event('change'));
    }

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
 *   2. .bounds-actions —— 「下载」按钮（打开下载弹窗）+ 「删除」按钮
 *      （清空选区）+ 调整提示。
 *
 * 小数 5 位（≈1.1m），框选下载范围够用，两个数字并排也放得下。
 */
function updateBoundsInfo() {
    const boundsInfo = document.getElementById('boundsInfo');
    const statusSel = document.getElementById('statusSelection');
    // M15：编辑态不重写整层。浮层内容每次都是 innerHTML 全量重建，而委托点击
    // 监听挂在 #boundsInfo 上 —— 编辑坐标时用鼠标点浮层里的任何东西（另一个
    // .bounds-v、下载按钮、删除按钮），mousedown 先触发 blur → 提交 → 整层
    // 重建，等到 click 派发时旧节点的传播路径已不含 #boundsInfo，处理器根本
    // 不会被调用（浏览器通常也干脆不派发这次 click）：**这一次点击完全没反应**，
    // 必须再点一次。反过来若重建发生在 click 之后（校验通过且点击极快），
    // 刚建好的第二个输入框会被立刻抹掉、焦点丢失、之后敲的字全丢。
    if (boundsInfo && boundsInfo.querySelector('.bounds-edit-input')) {
        return;
    }
    if (currentBounds) {
        const f = (v) => v.toFixed(5);
        boundsInfo.innerHTML = `
            <div class="bounds-grid">
                <span class="bounds-k" aria-hidden="true">N</span><span class="bounds-v" data-field="north" title="${t('js.map.bounds.edit_title')}"><span class="bounds-sr">${t('js.map.bounds.sr_north')} </span>${f(currentBounds.north)}</span>
                <span class="bounds-k" aria-hidden="true">S</span><span class="bounds-v" data-field="south" title="${t('js.map.bounds.edit_title')}"><span class="bounds-sr">${t('js.map.bounds.sr_south')} </span>${f(currentBounds.south)}</span>
                <span class="bounds-k" aria-hidden="true">E</span><span class="bounds-v" data-field="east" title="${t('js.map.bounds.edit_title')}"><span class="bounds-sr">${t('js.map.bounds.sr_east')} </span>${f(currentBounds.east)}</span>
                <span class="bounds-k" aria-hidden="true">W</span><span class="bounds-v" data-field="west" title="${t('js.map.bounds.edit_title')}"><span class="bounds-sr">${t('js.map.bounds.sr_west')} </span>${f(currentBounds.west)}</span>
            </div>
            <div class="bounds-actions">
                <button type="button" class="btn btn-primary btn-sm" id="boundsDownloadBtn">
                    <svg class="icon-inline icon-inline--sm" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                        <polyline points="7 10 12 15 17 10"></polyline>
                        <line x1="12" y1="15" x2="12" y2="3"></line>
                    </svg>
                    ${t('js.map.bounds.download')}
                </button>
                <button type="button" class="btn btn-danger btn-sm" id="boundsClearBtn" title="${t('js.map.bounds.clear_title')}">
                    <svg class="icon-inline icon-inline--sm" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <polyline points="3 6 5 6 21 6"></polyline>
                        <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                    </svg>
                    ${t('js.map.bounds.delete')}
                </button>
                <span class="bounds-hint">${t('js.map.bounds.hint')}</span>
            </div>
        `;
        if (statusSel) {
            const w = (currentBounds.east - currentBounds.west).toFixed(3);
            const h = (currentBounds.north - currentBounds.south).toFixed(3);
            statusSel.textContent = t('js.map.status.selection', { w: w, h: h });
        }
    } else if (_manualBoundsOpen) {
        // 手动输入面板展开时不重建浮层，否则用户正在敲的四至会被抹掉。
        // 面板里的 input 带 .bounds-edit-input，上面那道编辑态守卫通常已经
        // 拦下来了；这里兜住「面板该在却不在」的路径（例如别处刚清过 innerHTML）。
        if (!boundsInfo.querySelector('.bounds-manual')) _renderManualBounds(boundsInfo);
        if (statusSel) statusSel.textContent = t('js.map.status.no_selection');
    } else {
        // 不用 <small>：style.css 的全局 `small` 是 --font-size-sm(14px)，比浮层
        // 自己的字号还大，套上去等于把提示放大一号。字号由 .bounds-overlay 统一给。
        //
        // 「手动输入范围」是键盘用户**唯一**的选区入口（B4）：矩形只能由 canvas 的
        // LEFT_DOWN→MOUSE_MOVE→LEFT_UP 鼠标手势产生，没有它 currentBounds 恒为
        // null，整条下载链路对键盘 100% 不可达。放在空态里而不是工具条上，是因为
        // 这层浮层本来就是「当前选区」的归属地，用户找选区自然会看这里。
        boundsInfo.innerHTML = `
            <svg class="icon-inline icon-inline--sm" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="12" y1="16" x2="12" y2="12"></line>
                <line x1="12" y1="8" x2="12.01" y2="8"></line>
            </svg>
            ${t('js.map.bounds.empty')}
            <div class="bounds-actions">
                <button type="button" class="btn btn-secondary btn-sm" id="boundsManualBtn">${t('js.map.bounds.manual')}</button>
            </div>
        `;
        if (statusSel) statusSel.textContent = t('js.map.status.no_selection');
    }
}

/**
 * 把当前四至播报给读屏软件（#boundsAnnounce，视觉隐藏的 role="status"）。
 *
 * 为什么另起一个元素而不复用 #boundsInfo：那层浮层由 updateBoundsInfo() 整层
 * innerHTML 重建，而重建走 MOUSE_MOVE → _scheduleBoundsInfoUpdate → rAF，
 * 拖角点时最高 60 次/秒且每帧坐标都变。它若是 live region，polite 队列会一路
 * 积压到用户松手之后还在念（拖 3 秒 ≈ 180 条公告）；浮层里还有按钮，交互控件
 * 塞进 live region 本身也不对。
 *
 * 所以播报只在**落定时刻**写,一共三处,拖拽过程中一条都不发:
 *   1. LEFT_UP —— 鼠标框选结束;
 *   2. _applyBoundsEdit 校验通过 —— 点四至读数改数生效;
 *   3. _applyManualBounds —— 手动输入面板确定(键盘用户的选区入口,见 B4)。
 *
 * 小数 5 位与浮层读数一致（≈1.1m）。
 */
function announceBounds() {
    const el = document.getElementById('boundsAnnounce');
    if (!el || !currentBounds) return;
    const f = (v) => v.toFixed(5);
    el.textContent = t('js.map.bounds.announce', {
        north: f(currentBounds.north),
        south: f(currentBounds.south),
        east: f(currentBounds.east),
        west: f(currentBounds.west),
    });
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
    input.setAttribute('aria-label', t('js.map.bounds.edit_aria', { field: field }));
    vEl.innerHTML = '';
    vEl.appendChild(input);
    input.focus();
    input.select();
    let done = false;
    function commit(apply) {
        if (done) return;
        done = true;
        // 先把输入框从 DOM 摘掉,再走后续的重渲染。
        //
        // updateBoundsInfo() 开头那道 M15 守卫(见 :924)靠「浮层里还有
        // .bounds-edit-input」判断「正处于编辑态,不要重写整层」。而本函数建的
        // input 用的正是这个类 —— 留着它的话,下面无论走哪条路,那次重渲染都会
        // 被守卫拦掉:
        //   - Escape 取消:回不到原读数,格子里永远停着一个 input;
        //   - 校验失败(_applyBoundsEdit 里 4 条 return 前的 updateBoundsInfo):
        //     同样回不去,提示弹了但输入框还在;
        //   - 校验通过:_syncBoundsFromRect 排的那次重渲染也被拦。
        // 更远的连带:input 一直留着,之后点「删除」时 clearSelection 触发的
        // 重渲染继续被拦,currentBounds 已是 null 而浮层还显示着旧四至。
        //
        // done 已在上面置位,所以 remove() 万一在某些浏览器上触发 blur,
        // 那次重入会被第一行的守卫挡掉,不会重复提交。
        const value = input.value;
        input.remove();
        if (apply) _applyBoundsEdit(field, value);
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
        showNotification(t('js.map.edit.invalid_number', { value: raw }), 'warning');
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
        showNotification(t('js.map.edit.north_gt_south'), 'warning');
        updateBoundsInfo();
        return;
    }
    if (b.north > 90 || b.south < -90) {
        showNotification(t('js.map.edit.lat_range'), 'warning');
        updateBoundsInfo();
        return;
    }
    if (Math.abs(b.east - b.west) < 1e-9) {
        showNotification(t('js.map.edit.zero_width'), 'warning');
        updateBoundsInfo();
        return;
    }
    _rectDegrees = { west: b.west, south: b.south, east: b.east, north: b.north };
    _ensureSelectionEntity();
    _ensureHandles();
    _syncBoundsFromRect();
    // 矩形/手柄是 CallbackProperty，改 _rectDegrees 不会自动重绘，显式请求一帧
    viewer.scene.requestRender();
    announceBounds();
    refreshSubmitButtonState();
}

// --- 手动输入范围（键盘可达的选区入口，B4）--------------------------------------
//
// 矩形选区原本只能由 Cesium canvas 的 LEFT_DOWN → MOUSE_MOVE → LEFT_UP 产生，
// 键盘用户拿不到 currentBounds，整条下载链路对他们不可用。这里在范围浮层的
// **空态**里提供一颗普通 <button>（天然可 Tab 可 Enter），展开后填 N/S/E/W 四个
// 数字即可落定选区。
//
// 刻意**不做**「键盘画矩形」：那要为方向键设计移动步长、锚点、缩放语义，
// 是另一套交互，而这里真正缺的只是「把四至喂进去」。
//
// 落定走的是与鼠标框选同一套状态写入（_rectDegrees → 实体 + 手柄 →
// currentBounds），所以手动输入出来的选区和框出来的没有区别：角点能拖、
// 四至读数能点开编辑、下载/删除按钮照常。

// 面板是否展开。updateBoundsInfo() 的空态分支据此决定渲染提示还是面板。
let _manualBoundsOpen = false;

// 输入框 id 由方位名直接派生，不列查找表：方位与输入框接反是数据正确性缺陷
//（和四至读数标反同一类），由构造保证配对比列表逐条核对更硬。
function _manualBoundsInputId(field) {
    return 'manualBounds_' + field;
}

// 步长 1e-5 度 ≈ 1.1m，与浮层读数的 5 位小数同精度；再细的位数读数也显示不出来。
const _MANUAL_BOUNDS_STEP = '0.00001';

function _renderManualBounds(boundsInfo) {
    // 复用 .bounds-edit-input（点击编辑用的那套外观），行内 width 覆盖它的
    // 10ch 定宽：这里的输入框是 .bounds-grid 的 1fr 轨道成员，定宽会在窄视口
    // 撑破浮层。min-width:0 是网格项能收缩的前提。
    const box = (field, letter) => `
                <label class="bounds-k" for="${_manualBoundsInputId(field)}">${letter}</label><input
                    type="number" step="${_MANUAL_BOUNDS_STEP}" class="bounds-edit-input"
                    id="${_manualBoundsInputId(field)}" data-field="${field}"
                    style="width: 100%; min-width: 0;"
                    aria-label="${t('js.map.bounds.sr_' + field)}">`;
    boundsInfo.innerHTML = `
        <div class="bounds-manual">
            <div class="bounds-grid">${box('north', 'N')}${box('south', 'S')}${box('east', 'E')}${box('west', 'W')}
            </div>
            <div class="bounds-actions">
                <button type="button" class="btn btn-primary btn-sm" id="manualBoundsApply">${t('js.map.bounds.manual_apply')}</button>
                <button type="button" class="btn btn-secondary btn-sm" id="manualBoundsCancel">${t('js.map.bounds.manual_cancel')}</button>
            </div>
        </div>
    `;
}

function _openManualBounds() {
    _manualBoundsOpen = true;
    updateBoundsInfo();
    const first = document.getElementById(_manualBoundsInputId('north'));
    if (first) first.focus();
}

// 关闭面板。必须先把面板从 DOM 里拿掉再让调用方重渲染：面板里的 input 带
// .bounds-edit-input，而 updateBoundsInfo() 开头正是靠这个类判断「编辑态，
// 不要重写整层」—— 留着它，之后那次重渲染会被自己拦掉，浮层永远停在面板上。
function _closeManualBounds() {
    _manualBoundsOpen = false;
    const boundsInfo = document.getElementById('boundsInfo');
    if (boundsInfo) boundsInfo.innerHTML = '';
}

// 校验口径与 _applyBoundsEdit 逐条对齐（顺序、判据、文案 key 全同），
// 不另立一套：同一个选区被两个入口用不同标准放行，是更难查的缺陷。
//   1. 非数字        -> js.map.edit.invalid_number
//   2. north <= south -> js.map.edit.north_gt_south
//   3. 纬度越界 ±90   -> js.map.edit.lat_range
//   4. |east-west| < 1e-9 -> js.map.edit.zero_width
// 注意第 4 条只禁「零宽」不要求 east > west —— 与 _applyBoundsEdit 一致，
// west=170/east=-170 这类跨 180° 经线的矩形照样放行。
// 返回 {north, south, east, west}（度），任一条不过则 toast 并返回 null。
function _readManualBounds() {
    const num = {};
    for (const field of ['north', 'south', 'east', 'west']) {
        const el = document.getElementById(_manualBoundsInputId(field));
        const raw = el ? el.value : '';
        const v = parseFloat(String(raw).trim());
        if (isNaN(v)) {
            showNotification(t('js.map.edit.invalid_number', { value: raw }), 'warning');
            if (el) el.focus();
            return null;
        }
        num[field] = v;
    }
    if (num.north <= num.south) {
        showNotification(t('js.map.edit.north_gt_south'), 'warning');
        return null;
    }
    if (num.north > 90 || num.south < -90) {
        showNotification(t('js.map.edit.lat_range'), 'warning');
        return null;
    }
    if (Math.abs(num.east - num.west) < 1e-9) {
        showNotification(t('js.map.edit.zero_width'), 'warning');
        return null;
    }
    return num;
}

function _applyManualBounds() {
    const b = _readManualBounds();
    if (!b) return;                 // 校验失败：面板留在原地，用户就地改
    _closeManualBounds();
    // 与 LEFT_UP 落定同一套写入：_rectDegrees 是几何真值，矩形实体与四个角点
    // 手柄都从它读，所以这之后拖角点、点读数编辑的行为与框选出来的完全一致。
    _rectDegrees = { west: b.west, south: b.south, east: b.east, north: b.north };
    _ensureSelectionEntity();
    _ensureHandles();
    _syncBoundsFromRect();
    // 矩形/手柄是 CallbackProperty，改 _rectDegrees 不会自动重绘，显式请求一帧
    if (viewer) viewer.scene.requestRender();
    updateBoundsInfo();
    announceBounds();
    refreshSubmitButtonState();
    // 焦点不能留在刚被删掉的「确定」上（会掉回 <body>，键盘用户丢失位置）。
    // 交给新出现的「下载」按钮 —— 正好是这条流程的下一步。
    const dlBtn = document.getElementById('boundsDownloadBtn');
    if (dlBtn) dlBtn.focus();
}

function _cancelManualBounds() {
    _closeManualBounds();
    updateBoundsInfo();
    const btn = document.getElementById('boundsManualBtn');
    if (btn) btn.focus();           // 焦点回到打开面板的那颗按钮
}

// #boundsInfo 上的委托点击里调用（浮层每次都是 innerHTML 全量重建，
// 不能给面板里的按钮直接挂监听）。命中返回 true，让调用方短路。
function _handleManualBoundsClick(e) {
    if (e.target.closest('#boundsManualBtn')) {
        _openManualBounds();
        return true;
    }
    if (e.target.closest('#manualBoundsApply')) {
        _applyManualBounds();
        return true;
    }
    if (e.target.closest('#manualBoundsCancel')) {
        _cancelManualBounds();
        return true;
    }
    return false;
}

// 面板内 Enter 直接落定、Esc 取消 —— 不用让键盘用户从第四个输入框再 Tab 两下
// 才够得着「确定」。限定在 .bounds-manual 内，避免和点击编辑那套输入框
//（它们自己处理 Enter/Esc，同挂在 #boundsInfo 上）互相抢事件。
function _handleManualBoundsKeydown(e) {
    if (!e.target.closest('.bounds-manual')) return;
    if (e.key === 'Enter') {
        e.preventDefault();
        _applyManualBounds();
    } else if (e.key === 'Escape') {
        e.preventDefault();
        _cancelManualBounds();
    }
}

// --- 下载 / 处理弹窗 -----------------------------------------------------------

// 打开下载弹窗前刷新顶部的选区四至摘要——弹窗可能关过又开，
// 期间用户拖过角点或改过数值，摘要必须反映当前选区。
function openDownloadModal() {
    if (!currentBounds) {
        showNotification(t('js.map.download.need_selection'), 'warning');
        return;
    }
    const summary = document.getElementById('downloadModalBounds');
    if (summary) {
        const f = (v) => v.toFixed(5);
        const w = (currentBounds.east - currentBounds.west).toFixed(3);
        const h = (currentBounds.north - currentBounds.south).toFixed(3);
        summary.textContent = t('js.map.download.bounds_summary', {
            north: f(currentBounds.north),
            south: f(currentBounds.south),
            east: f(currentBounds.east),
            west: f(currentBounds.west),
            width: w,
            height: h,
        });
    }
    const modalEl = document.getElementById('downloadModal');
    if (!modalEl || typeof bootstrap === 'undefined') return;
    updateTileEstimate();
    refreshSubmitButtonState();
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

// ?. 而不是包一层 if：处理器有近百行，缩进进去只会让 diff 噪声盖住逻辑。
// 缺元素时整个文件后面的部分（等高线预览、预览管理器初始化）会全部不执行
// —— 本文件其它入口（initDownloadTypeToggle 等）都判了空，这里是例外。
document.getElementById('downloadForm')?.addEventListener('submit', async function(e) {
    e.preventDefault();

    const downloadType = _radioValue('downloadType', 'map');

    if (!currentBounds) {
        showNotification(t('js.map.download.need_selection'), 'warning');
        return;
    }

    // 大任务二次确认：瓦片数超软阈值（100k，小时级作业）时把预计耗时
    // 摆给用户，确认后才提交。0.1.4 起服务端不再硬性拒绝。
    const est = updateTileEstimate();
    if (est && est.over) {
        const hours = (est.count / 10 / 3600).toFixed(1);
        const ok = await showConfirm(
            t('js.map.download.confirm_large', {
                count: est.count.toLocaleString('zh-CN'),
                hours: hours,
            }),
            { title: t('js.map.download.confirm_large_title'), danger: true });
        if (!ok) return;
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
        const outputFormat = _outputFormatValue();
        if (!outputFormat) {
            showNotification(t('js.map.download.need_output_format'), 'warning');
            return;
        }
        taskData = {
            name: document.getElementById('taskName').value,
            north: currentBounds.north,
            south: currentBounds.south,
            east: _wrapLngEast(currentBounds.east),
            west: _wrapLngWest(currentBounds.west),
            zoom_min: parseInt(document.getElementById('zoomMin').value),
            zoom_max: parseInt(document.getElementById('zoomMax').value),
            style: document.getElementById('mapStyle').value,
            output_format: outputFormat,
            output_path: document.getElementById('outputPath').value
        };
        apiUrl = '/api/tasks';
    }

    const btn = document.getElementById('createTaskBtn');
    const originalText = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `
        <svg class="icon-inline icon-inline--md" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="animation: spin 1s linear infinite;">
            <circle cx="12" cy="12" r="10"></circle>
        </svg>
        ${t('js.map.download.creating')}
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
            showNotification(t('js.map.download.created', { id: result.task_id }), 'success');
            resetForm();
            loadActiveTasks();
            _afterTaskCreated('downloadModal');
        } else {
            showNotification(t('js.map.download.create_failed', { error: result.error }), 'danger');
        }
    } catch (error) {
        showNotification(t('js.map.download.create_failed', { error: error.message }), 'danger');
    } finally {
        btn.innerHTML = originalText;
        refreshSubmitButtonState();
    }
});

document.getElementById('processForm')?.addEventListener('submit', async function(e) {
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
    // 等高线有两种数据来源：上传 GeoTIFF，或直接用某个已完成 DEM 任务已经下载好
    // 的 .tif（零拷贝，后端按 dem_task_id 指向那个任务目录）。数据处理本身不下载，
    // 远程高程下载在「数据下载」的 DEM 任务里做。没有 bbox：瓦片范围由后端按 DEM
    // 实际覆盖决定；来源是否已选齐在这里校验。
    const fromDemTask = (document.getElementById('processSource')?.value || 'upload') === 'dem_task';
    const demTaskId = _selectedProcessDemTaskId();
    const fileInput = document.getElementById('contourFiles');
    const files = fileInput?.files;
    if (fromDemTask) {
        if (!demTaskId) {
            showNotification(t('js.map.process.need_dem_task'), 'warning');
            return;
        }
    } else if (!files || files.length === 0) {
        showNotification(t('js.map.process.need_files'), 'warning');
        return;
    }

    const interval = parseFloat(document.getElementById('contourInterval').value) || 50;
    const bgTransparent = document.getElementById('contourBackgroundTransparent').checked;
    const background = bgTransparent ? 'transparent' : (document.getElementById('contourBackground').value || '#faf6ec');

    const fd = new FormData();
    fd.append('name', document.getElementById('processTaskName').value
        || t('js.map.process.contour_default_name'));
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
    if (fromDemTask) {
        fd.append('dem_task_id', demTaskId);
    } else {
        for (const f of files) {
            fd.append('files', f);
        }
    }

    const btn = document.getElementById('createProcessBtn');
    const original = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = t(fromDemTask ? 'js.map.process.submitting' : 'js.map.process.uploading');
    try {
        const createResp = await fetch('/api/contour/tasks', { method: 'POST', body: fd });
        if (!createResp.ok) {
            // .json() 必须在 resp.ok **之后**:上传超 2 GiB 时 Flask 的 413
            // 返回的是 HTML 错误页,先 .json() 会抛 SyntaxError 被外层 catch
            // 接住 —— 用户看到的是「Unexpected token '<'」而不是「文件过大」。
            // 正确写法 9 行之后就有一份(startResp 那条 .catch(() => ({})))。
            const failed = await createResp.json().catch(() => ({}));
            showNotification(t('js.map.process.create_failed', {
                error: failed.error || createResp.status,
            }), 'danger');
            return;
        }
        const created = await createResp.json();
        const startResp = await fetch(`/api/contour/tasks/${created.task_id}/start`, { method: 'POST' });
        if (!startResp.ok) {
            const started = await startResp.json().catch(() => ({}));
            showNotification(t('js.map.process.start_failed', {
                error: started.error || startResp.status,
            }), 'danger');
            return;
        }
        showNotification(fromDemTask
            ? t('js.map.process.contour_started_dem_task', { id: demTaskId })
            : t('js.map.process.contour_started'), 'success');
        resetForm({ clearBounds: false, formId: 'processForm' });
        resetContourTintUI();
        loadActiveTasks();
        _afterTaskCreated('processModal');
    } catch (err) {
        showNotification(t('js.map.process.create_failed', { error: err.message }), 'danger');
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
//   本地高程切片 -> Cesium 地形（/terrain/local/<id>/layer.json，valid_bounds 定位）
//   DEM 任务     -> 地形切片存在时按 Cesium 地形预览（/terrain/dem/<id>/layer.json）
//   dem/local_terrain 无切片 -> 源 DEM 晕渲图单图叠加（<base>/hillshade 按需渲染）
// 同一时刻只有一个预览；「关闭预览」撤掉影像层并还原默认椭球地形。
let _previewState = null;   // { kind: 'imagery'|'terrain', taskId, taskType, name, layer?, prevTerrainProvider? }
// 预览调用序号：地形分支有 await(layer.json + fromUrl / hillshade)，期间用户可能
// 已切到另一个预览或关闭预览；await 返回后比对序号，过期结果直接丢弃不落地。
let _previewSeq = 0;

function stopTaskPreview() {
    _previewSeq += 1;   // 作废任何在途的 previewTask
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

// 删除任务时的联动（history.js 删除成功后调用）：预览中的正是被删任务，
// 留着只会让瓦片逐个 404、chip 挂着一个不存在的任务 —— 直接关掉。
function stopTaskPreviewForTask(taskType, taskId) {
    if (_previewState && _previewState.taskType === taskType && _previewState.taskId === taskId) {
        stopTaskPreview();
    }
}

async function previewTask(task) {
    if (!viewer) return;
    stopTaskPreview();
    const seq = _previewSeq;
    try {
        const taskType = task.task_type;
        if (taskType === 'map' || taskType === 'contour') {
            const base = taskType === 'map' ? `/tiles/${task.id}` : `/contour/${task.id}`;
            const layer = viewer.imageryLayers.addImageryProvider(
                new Cesium.UrlTemplateImageryProvider({
                    url: `${base}/{z}/{x}/{y}.png`,
                    tilingScheme: new Cesium.WebMercatorTilingScheme(),
                    maximumLevel: task.zoom_max || undefined,
                })
            );
            layer.alpha = 0.9;
            _previewState = { kind: 'imagery', taskId: task.id, taskType: taskType, name: task.name, layer };
            if (taskType === 'contour') contourPreviewActiveId = task.id;
        } else if (taskType === 'local_terrain' || taskType === 'dem') {
            const base = taskType === 'local_terrain'
                ? `/terrain/local/${task.id}`
                : `/terrain/dem/${task.id}`;
            // GET 而非 HEAD：layer.json 里的 valid_bounds 是数据真实范围，
            // 任务行没有 bbox（local_terrain）时靠它定位。
            const layerMeta = await fetch(`${base}/layer.json`)
                .then((r) => (r.ok ? r.json() : null))
                .catch(() => null);
            if (seq !== _previewSeq) return;   // await 期间预览已被切换/关闭
            if (layerMeta) {
                const prev = viewer.terrainProvider;
                // 先 await 到局部变量，序号仍有效才落地，避免过期结果覆盖当前预览
                // 传目录，不能传 `${base}/layer.json` —— fromUrl 内部会 appendForwardSlash()
                // 后再拼 layer.json，传后者会请求 .../layer.json/layer.json 得 404。
                // 更坑的是它不 reject：拿不到 layer.json 时静默按默认假设建 provider
                // （实测 hasWaterMask 变成 true），随后瓦片请求全 404，前端毫无提示。
                //
                // requestVertexNormals: true 不能省。build_terrain 默认仍出 oct
                // 法线段，但应用侧 TileParams.normals 默认【关】（三档预设），
                // 所以任务瓦片可能有也可能没有 —— 随包底图与显式开了法线的任务
                // 都还要它。少了这个选项**不是少下载几个字节**，而是
                // vendored Cesium 1.143.0 里三处连锁失效：解码 worker 的扩展段
                // 循环有 `extensionId === OCT_VERTEX_NORMALS && _requestVertexNormals`
                // 双条件，法线段被跳过；`provider.hasVertexNormals` getter 是
                // `_hasVertexNormals && _requestVertexNormals`，恒 false；
                // 全球着色器于是走 ENABLE_DAYNIGHT_SHADING 而不是
                // ENABLE_VERTEX_LIGHTING。表现就是「光照开关点了只有一层随太阳
                // 方位的明暗渐变、地形起伏一点都看不出来」，且全程不报错。
                // tests/test_terrain_lighting_frontend.py 钉住这一行。
                const provider = await Cesium.CesiumTerrainProvider.fromUrl(base, {
                    requestVertexNormals: true,
                });
                if (seq !== _previewSeq) return;
                viewer.terrainProvider = provider;
                _previewState = { kind: 'terrain', taskId: task.id, taskType: taskType, name: task.name, prevTerrainProvider: prev };
                if (task.north == null && Array.isArray(layerMeta.valid_bounds) && layerMeta.valid_bounds.length === 4) {
                    const b = layerMeta.valid_bounds;   // [west, south, east, north]
                    viewer.camera.flyTo({
                        destination: Cesium.Rectangle.fromDegrees(b[0], b[1], b[2], b[3]),
                        duration: 1.2,
                    });
                }
            } else {
                // 无地形切片：退到源 DEM 的晕渲预览（后端按需渲染 *_dem.tif），
                // 也没有源文件时才只定位。
                const hs = await fetch(`${base}/hillshade`)
                    .then((r) => (r.ok ? r.json() : null))
                    .catch(() => null);
                if (seq !== _previewSeq) return;
                if (hs && hs.url && Array.isArray(hs.bounds) && hs.bounds.length === 4) {
                    const layer = viewer.imageryLayers.addImageryProvider(
                        new Cesium.SingleTileImageryProvider({
                            url: hs.url,
                            rectangle: Cesium.Rectangle.fromDegrees(hs.bounds[0], hs.bounds[1], hs.bounds[2], hs.bounds[3]),
                        })
                    );
                    layer.alpha = 0.85;
                    _previewState = { kind: 'imagery', taskId: task.id, taskType: taskType, name: task.name, layer };
                    if (task.north == null) {
                        viewer.camera.flyTo({
                            destination: Cesium.Rectangle.fromDegrees(hs.bounds[0], hs.bounds[1], hs.bounds[2], hs.bounds[3]),
                            duration: 1.2,
                        });
                    }
                    showNotification(t('js.map.preview.hillshade_fallback'), 'info');
                } else {
                    showNotification(taskType === 'dem'
                        ? t('js.map.preview.dem_no_tiles')
                        : t('js.map.preview.no_tiles_no_source'), 'info');
                }
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
    } catch (err) {
        // fromUrl reject / 网络异常等：只有仍是当前预览时才打扰用户;
        // 过期调用的报错随结果一起丢弃。
        if (seq === _previewSeq) {
            showNotification(t('js.map.preview.failed', {
                error: err && err.message ? err.message : err,
            }), 'danger');
        }
    }
}

// 预览中的浮动提示条（地图右下、状态栏上方）
function _renderPreviewChip() {
    let chip = document.getElementById('taskPreviewChip');
    if (!_previewState) {
        if (chip) chip.remove();
        return;
    }
    if (!chip) {
        // 与右上的 .bounds-overlay 共用 .map-overlay-chip 底座（定位/底色/描边/
        // 圆角/阴影/文字色），本类只留右下锚点与字号差异。
        const host = document.querySelector('.index-map');
        // 只有首页有这个容器。改前是直接 .appendChild 解引用查询结果 ——
        // 同文件的 contourPreviewPanel 对 #map 判了空，这里是唯一的例外。
        if (!host) return;
        chip = document.createElement('div');
        chip.id = 'taskPreviewChip';
        chip.className = 'map-overlay-chip task-preview-chip';
        host.appendChild(chip);
    }
    chip.innerHTML = `
        <span>${t('js.map.preview.chip', {
            name: '<strong>' + escapeHtml(_previewState.name) + '</strong>',
            id: _previewState.taskId,
        })}</span>
        <button type="button" class="btn btn-sm btn-secondary" onclick="stopTaskPreview()">${t('js.map.preview.stop')}</button>
    `;
}

function toggleContourPreview(taskId, zoomMax) {
    // 当前预览就是这个任务 -> 再点一次关掉；否则切过去（预览管理器统一管）。
    //
    // taskType 必须一起比:四张任务表(tasks / dem_tasks / contour_tasks /
    // local_terrain_tasks)的 id 各自自增,`contour:5` 与 `map:5` 会同时存在;
    // 而 kind === 'imagery' 是 map、contour、DEM 晕渲回退三者共用的。只比
    // taskId 的话,正在预览 map 任务 5 时点 contour 任务 5 的预览按钮会走进
    // 这个分支、把 map 的预览关掉而不是切过去 —— 用户看到的是「第一次点
    // 没反应,再点一次才出来」。
    if (_previewState && _previewState.kind === 'imagery'
        && _previewState.taskType === 'contour' && _previewState.taskId === taskId) {
        stopTaskPreview();
        return;
    }
    const info = contourPreviewTasks.get(taskId) || {};
    previewTask({
        id: taskId, task_type: 'contour', zoom_max: zoomMax,
        name: info.name || t('js.map.contour.default_name', { id: taskId }),
        // contour_tasks 行有 bbox（列表接口 SELECT * 带出来的）：带上才能
        // flyTo 到任务区域，与历史面板的预览行为对齐。没拿到时不传键，
        // previewTask 的 `task.north != null` 检查会跳过 flyTo。
        ...(info.north != null
            ? { north: info.north, south: info.south, east: info.east, west: info.west }
            : {}),
    });
}

// Completed contour tasks available for preview: id -> {name, zoom_max, north?, south?, east?, west?}.
// bbox 用于预览时 flyTo（列表接口的行带不出 bbox 的兜底场景除外）。
const contourPreviewTasks = new Map();

function contourPreviewPanel() {
    let panel = document.getElementById('contourPreviewPanel');
    if (!panel) {
        const mapEl = document.getElementById('map');
        if (!mapEl) return null;
        panel = document.createElement('div');
        panel.id = 'contourPreviewPanel';
        // U6：必须绝对定位。父节点是 .index-map（position:relative +
        // overflow:hidden），而 #map 高 100% 已经吃满容器 —— 作为普通流内块
        // append 进去的话，起始位置就在容器高度之下，被完整裁掉：面板永远
        // 不可见，却照样会去拉 /api/contour/tasks。定位到左下角（避开右上角
        // 的 .bounds-overlay 与左侧工具条）。
        panel.className = 'contour-preview-panel';
        // Place the panel over the map, anchored to its bottom-left corner.
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
        const label = escapeHtml(info.name || t('js.map.contour.default_name', { id: id }));
        rows.push(`
            <button type="button"
                    class="btn btn-sm ${active ? 'btn-primary' : 'btn-outline-primary'}"
                    style="margin: 0 6px 6px 0;"
                    onclick="toggleContourPreview(${id}, ${info.zoom_max || 'null'})">
                ${active
                    ? t('js.map.contour.hide_preview', { name: label })
                    : t('js.map.contour.show_preview', { name: label })}
            </button>
        `);
    });
    panel.innerHTML = `
        <div class="alert alert-info" style="margin-bottom:0;">
            <small><strong>${t('js.map.contour.panel_title')}</strong></small><br>
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
            contourPreviewTasks.set(taskId, {
                name: task.name, zoom_max: task.zoom_max,
                north: task.north, south: task.south, east: task.east, west: task.west,
            });
        } else {
            contourPreviewTasks.set(taskId, {
                name: t('js.map.contour.default_name', { id: taskId }), zoom_max: null,
            });
        }
    } catch (e) {
        contourPreviewTasks.set(taskId, {
            name: t('js.map.contour.default_name', { id: taskId }), zoom_max: null,
        });
    }
    updateContourPreviewButtons();
}

// Hook the shared socket. 实例由 socket.js 的 window.TerraSocket.get() 创建，
// tasks.js 的 initTasks 只是把它取回来赋给全局 `socket`。本函数从页面 init 块里
// 排在 initTasks() 之后调用，所以那时 `socket` 已经赋好值。
function initContourPreview() {
    if (typeof socket === 'undefined' || !socket) return;
    socket.on('task_completed', function(data) {
        if (data && data.task_type === 'contour') {
            registerCompletedContourTask(data.task_id);
        }
    });
    // 首屏 + 每次重新拉取活动列表时，把已完成的等高线任务补进预览注册表。
    // 首屏不再自己 fetch /api/contour/tasks：loadActiveTasks（tasks.js）首屏
    // 必拉同一份响应，且 contour 路刻意不带 ?status=active（预览要从里面筛
    // completed）——等它 resolve 后共享，首屏同接口只拉一遍。
    // 接口失败时 loadActiveTasks 自己已经 toast 过，这里静默不重复打扰。
    const shared = (typeof firstActiveTasksLoad !== 'undefined' && firstActiveTasksLoad)
        ? firstActiveTasksLoad
        : Promise.resolve();
    shared.then(syncContourPreviewFromLatest).catch(function() {});
}

// 把 latestContourTasks（tasks.js 每次 loadActiveTasks 都会刷新）里已完成的
// 任务并进预览注册表。**幂等**，可以反复调。
//
// 除首屏外，断线重连也必须走这里：socket.io 不重放错过的事件，断线窗口内
// 完成的等高线任务收不到 task_completed，光靠事件注册的话那些任务要到整页
// 刷新才会出现预览按钮。loadActiveTasks 本来就是重连补拉的一环，挂在它的
// 尾巴上比只补一个 connect 分支更严：任何一次重新拉取都顺带对齐注册表。
function syncContourPreviewFromLatest() {
    const tasks = (typeof latestContourTasks !== 'undefined') ? latestContourTasks : [];
    let added = false;
    tasks.forEach(function(task) {
        if (task.status !== 'completed' || contourPreviewTasks.has(task.id)) return;
        contourPreviewTasks.set(task.id, {
            name: task.name, zoom_max: task.zoom_max,
            north: task.north, south: task.south, east: task.east, west: task.west,
        });
        added = true;
    });
    if (added) updateContourPreviewButtons();
}

async function submitLocalTerrain() {
    // 来源是已下载的 DEM 任务时不走上传管线，见 startDemTaskTerrainTiling()。
    if ((document.getElementById('processSource')?.value || 'upload') === 'dem_task') {
        await startDemTaskTerrainTiling();
        return;
    }

    const fileInput = document.getElementById('localTerrainFiles');
    const files = fileInput?.files;
    if (!files || files.length === 0) {
        showNotification(t('js.map.process.need_files'), 'warning');
        return;
    }

    const fd = new FormData();
    fd.append('name', document.getElementById('processTaskName').value
        || t('js.map.process.local_terrain_default_name'));
    // 三个字段的兜底一律是空串，不是前端自己抄一份默认值：空串 = 未传 = 走配置
    // 默认（后端 local_terrain_api.py:39-47 把空串当未传）。写死 '14' / 'balanced'
    // 会在控件缺席或被清空时用前端的默认盖掉运维配的 terrain_local_maxzoom /
    // terrain_quality_preset —— 而 DEM 分支（startDemTaskTerrainTiling）本来就送空串，
    // 两边不一致就是同一份 DEM 从两个入口切出不同产物。
    fd.append('maxzoom', document.getElementById('localTerrainMaxzoom')?.value || '');
    fd.append('quality', document.getElementById('localTerrainQuality')?.value || '');
    // ⚠️ 法线必须送 checked 状态。checkbox 的 .value 恒为 'on'（与勾没勾无关），
    // 把它或 checkbox 本身丢进 FormData 送出去的都是 'on'，而后端
    // coerce_vertex_normals 是严格白名单，'on' 一律 400。控件不在时送空串走
    // 配置默认，不要送 'undefined'。
    const normalsEl = document.getElementById('localTerrainNormals');
    fd.append('vertex_normals', normalsEl ? String(normalsEl.checked) : '');
    for (const f of files) {
        fd.append('files', f);
    }

    const btn = document.getElementById('createProcessBtn');
    btn.disabled = true;
    const original = btn.innerHTML;
    btn.innerHTML = t('js.map.process.uploading');
    try {
        const resp = await fetch('/api/terrain/local/tasks', { method: 'POST', body: fd });
        if (!resp.ok) {
            // 同 submitContour:.json() 必须在 resp.ok 之后。本地高程整包上传
            // 比等高线更容易撞 413,先解析就会把「文件过大」变成
            // 「Unexpected token '<'」。
            const failed = await resp.json().catch(() => ({}));
            showNotification(t('js.map.process.upload_failed', {
                error: failed.error || resp.status,
            }), 'danger');
            return;
        }
        const result = await resp.json();
        showNotification(t('js.map.process.upload_started', { id: result.task_id }), 'success');
        resetForm({ clearBounds: false, formId: 'processForm' });
        loadActiveTasks();
        _afterTaskCreated('processModal');
    } catch (err) {
        showNotification(t('js.map.process.upload_failed', { error: err.message }), 'danger');
    } finally {
        btn.innerHTML = original;
        refreshSubmitButtonState();
    }
}

// 「本地高程切片 + 已下载的 DEM 任务」刻意不新建 local_terrain 任务，而是复用 DEM
// 任务自己的地形切片管线（POST /api/terrain/dem/<id>/start）：它的产物落在
// /terrain/dem/<id>/，主视图预览、任务详情面板、terrain_job_progress 进度推送全都
// 已经接好。另建一条 local_terrain 任务只会把同一份 DEM 切出第二个副本，白占磁盘，
// 还让历史记录里出现两条指向同一数据的任务。
async function startDemTaskTerrainTiling() {
    const demTaskId = _selectedProcessDemTaskId();
    if (!demTaskId) {
        showNotification(t('js.map.process.need_dem_task'), 'warning');
        return;
    }

    // 这条分支不上传任何文件，所以不套「上传中...」的按钮文案，只在请求期间禁用。
    const btn = document.getElementById('createProcessBtn');
    btn.disabled = true;
    try {
        // 三个字段的空串一律表示「未传，走配置默认」（terrain_api.py:38-51）。
        // 法线在这条分支送的是真布尔：JSON body 不做字符串化，后端
        // coerce_vertex_normals 同时收真布尔与 'true'/'false' 两种形态。
        // 同样不能读 checkbox 的 .value —— 它恒为 'on'，后端白名单不认，400。
        const normalsEl = document.getElementById('localTerrainNormals');
        const resp = await fetch(`/api/terrain/dem/${demTaskId}/start`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                maxzoom: document.getElementById('localTerrainMaxzoom')?.value || '',
                quality: document.getElementById('localTerrainQuality')?.value || '',
                vertex_normals: normalsEl ? normalsEl.checked : '',
            }),
        });
        if (resp.ok) {
            showNotification(t('js.map.process.dem_tiling_started', { id: demTaskId }), 'success');
            resetForm({ clearBounds: false, formId: 'processForm' });
            loadActiveTasks();
            _afterTaskCreated('processModal');
        } else {
            const result = await resp.json().catch(() => ({}));
            showNotification(t('js.map.process.start_failed', {
                error: result.error || resp.status,
            }), 'danger');
        }
    } catch (err) {
        showNotification(t('js.map.process.start_failed', { error: err.message }), 'danger');
    } finally {
        refreshSubmitButtonState();
    }
}

/**
 * 工作台行为：状态栏读数（鼠标经纬度 / 缩放级别 / 选区摘要 / 日期时钟；
 * 坐标与选区四至支持点击复制）、bounds 浮层交互（下载按钮、数值点击
 * 编辑）。在 initMap 之后由页面 init 块调用（index.html）。
 */
function initMapWorkbench() {
    if (!viewer) return;

    const coordsEl = document.getElementById('statusCoords');
    const zoomEl = document.getElementById('statusZoom');

    // 复制到剪贴板：navigator.clipboard 只在安全上下文可用（127.0.0.1 可以，
    // 局域网 http://IP 不行），不可用时退到 execCommand 老路。
    function _copyText(text, toastMsg) {
        function fallback() {
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            let ok = false;
            try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
            ta.remove();
            if (ok) showToast(toastMsg, 'success');
            // 'danger' 而不是 'error'：ui.js 的 VALID_TYPES 里没有 'error'，
            // 会被静默降级成蓝色 ⓘ —— 而这条分支服务的正是局域网 http://IP
            // 这种非安全上下文，复制失败读起来像复制成功。
            else showToast(t('js.map.copy.failed'), 'danger');
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(
                function () { showToast(toastMsg, 'success'); },
                fallback
            );
        } else {
            fallback();
        }
    }

    // 缩放级别 + 镜头高度：高度换算的近似 zoom，300ms 轮询（camera.changed 太高频）
    function _fmtHeight(h) {
        if (h >= 100000) return Math.round(h / 1000) + ' km';
        if (h >= 1000) return (h / 1000).toFixed(1) + ' km';
        return Math.round(h) + ' m';
    }
    function updateZoom() {
        if (!zoomEl) return;
        const h = viewer.camera.positionCartographic.height;
        // 级别与高度分两个 span：窄屏 CSS 只藏高度、保住 z 读数
        document.getElementById('statusZoomZ').textContent =
            t('js.map.status.zoom', { z: _heightToZoom(h) });
        document.getElementById('statusZoomAlt').textContent =
            t('js.map.status.alt', { h: _fmtHeight(h) });
    }

    // 鼠标经纬度：50ms 节流，避免 mousemove 高频刷新
    let pending = null;
    let lastCoords = null;  // {lng, lat}，供状态栏点击复制
    function pickCoords() {
        if (!pending) return;
        const carto = _pickCartographic(pending);
        pending = null;
        if (!carto) return;
        lastCoords = {
            lng: Cesium.Math.toDegrees(carto.longitude),
            lat: Cesium.Math.toDegrees(carto.latitude),
        };
        coordsEl.textContent = t('js.map.status.coords', {
            lng: lastCoords.lng.toFixed(4),
            lat: lastCoords.lat.toFixed(4),
        });
    }
    if (coordsEl) {
        const moveHandler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
        moveHandler.setInputAction(function (event) {
            pending = event.endPosition;
        }, Cesium.ScreenSpaceEventType.MOUSE_MOVE);
        viewer.scene.canvas.addEventListener('mouseout', function () {
            // 只停止拾取，不清读数：移开后保留最后一个坐标（仍可点击复制），
            // 比闪回「经度 — 纬度 —」占位符更符合读数栏的定位。
            pending = null;
        });
        // 点击状态栏坐标 -> 复制「经度,纬度」（GIS 惯用顺序，可直接贴进大多数工具）
        coordsEl.addEventListener('click', function () {
            if (!lastCoords) return;
            _copyText(
                lastCoords.lng.toFixed(6) + ', ' + lastCoords.lat.toFixed(6),
                t('js.map.copy.coords_done')
            );
        });
    }

    // 点击状态栏选区摘要 -> 复制四至（W,S,E,N 顺序，GDAL/PostGIS 的 bbox 惯例）
    const statusSelEl = document.getElementById('statusSelection');
    if (statusSelEl) {
        statusSelEl.addEventListener('click', function () {
            if (!currentBounds) return;
            const f = (v) => v.toFixed(5);
            _copyText(
                f(currentBounds.west) + ',' + f(currentBounds.south) + ',' +
                f(currentBounds.east) + ',' + f(currentBounds.north),
                t('js.map.copy.bounds_done')
            );
        });
    }

    // 状态栏时钟：本地日期+时间 MM-DD HH:MM:SS，1s 刷新
    const clockEl = document.getElementById('statusClock');
    const tickClock = function () {
        if (!clockEl) return;
        const d = new Date();
        const p = (n) => String(n).padStart(2, '0');
        clockEl.textContent = p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
            p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
    };

    // 三档刷新（拾取 50ms / 缩放 300ms / 时钟 1s）合并成单个 50ms 基准
    // tick，按 elapsed 时间分派 —— 少两个常驻 interval。仍用 setInterval
    // 而不是 rAF：后台标签页节流下时钟照样走，不冻结。
    let lastZoom = 0;
    let lastClock = 0;
    updateZoom();
    tickClock();
    setInterval(function () {
        const now = Date.now();
        pickCoords();
        if (now - lastZoom >= 300) {
            lastZoom = now;
            updateZoom();
        }
        if (now - lastClock >= 1000) {
            lastClock = now;
            tickClock();
        }
    }, 50);

    // bounds 浮层交互（事件代理，浮层内容每次 updateBoundsInfo 都重渲染）：
    // 「下载」按钮 -> 下载弹窗；「删除」按钮 -> 清空选区；.bounds-v 数值 -> 点击编辑；
    // 空态的「手动输入范围」及其面板 -> _handleManualBoundsClick。
    const boundsInfo = document.getElementById('boundsInfo');
    if (boundsInfo) {
        boundsInfo.addEventListener('click', function (e) {
            if (_handleManualBoundsClick(e)) return;
            const dl = e.target.closest('#boundsDownloadBtn');
            if (dl) {
                openDownloadModal();
                return;
            }
            const clr = e.target.closest('#boundsClearBtn');
            if (clr) {
                clearSelection();
                return;
            }
            const v = e.target.closest('.bounds-v');
            if (v && currentBounds) _beginBoundsEdit(v);
        });
        // 手动输入面板里 Enter 落定 / Esc 取消（同样代理，面板是重建出来的）
        boundsInfo.addEventListener('keydown', _handleManualBoundsKeydown);
    }

    // 首屏填充「请在地图上框选下载区域」提示与状态栏选区摘要
    updateBoundsInfo();
}
