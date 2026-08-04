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
        el.textContent = '选区跨反经线，后端会拒绝该四至，无法预估瓦片数';
        el.classList.remove('tile-estimate--over');
        el.hidden = false;
        return null;
    }
    const count = estimateTileCount({ ...currentBounds, west, east }, zMin, zMax);
    const formatted = count.toLocaleString('zh-CN');
    const over = count > TASK_TILE_LIMIT;
    if (over) {
        const hours = (count / 10 / 3600).toFixed(1);
        el.textContent = `预计 ${formatted} 块瓦片 · 按 10 张/秒约 ${hours} 小时（大任务，创建时将要求确认）`;
        el.classList.add('tile-estimate--over');
    } else {
        el.textContent = `预计 ${formatted} 块瓦片`;
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

    // 底图 = tile_servers 列表的第一个条目（与下载共用一份配置，配置页可改）：
    // 别名/主机按 Google vt 格式拼 lyrs=m；完整 XYZ 模板原样用（{style} 替换为 m）。
    // 列表为空时回退内置 OSM。条目语义与 services/tile_url_probe.py 保持一致。
    function _baseMapUrl(serversRaw) {
        const first = (serversRaw || '').split(',').map(s => s.trim()).filter(Boolean)[0];
        if (!first) return 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
        if (first.startsWith('http://') || first.startsWith('https://')) {
            return first.replace('{style}', 'm');
        }
        const host = first.includes('.') ? first : first + '.googleapis.com';
        // 协议相对：页面走 https 时硬编码 http:// 会被混合内容拦截
        return `//${host}/vt?lyrs=m&x={x}&y={y}&z={z}`;
    }
    const tileUrl = _baseMapUrl(config.tile_servers);
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
        if (swbWrap) swbWrap.style.display = 'none';
    }

    function apply() {
        const t = _radioValue('downloadType', 'map');
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
// 两张表单各自一颗按钮：数据下载（瓦片/高程）必须先框选（瓦片数只是
// 提示项，0.1.4 起不禁用按钮）；处理类（本地高程切片 / 等高线）都是
// 上传驱动，没有 bbox，无条件启用（不检查文件）—— 文件是否已选在提交时
// 由 submitLocalTerrain() / submitContour() 各自校验。
function refreshSubmitButtonState() {
    const dlBtn = document.getElementById('createTaskBtn');
    if (dlBtn) dlBtn.disabled = !currentBounds;
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
                <button type="button" class="btn btn-danger btn-sm" id="boundsClearBtn" title="清除选区">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display: inline-block; vertical-align: middle; margin-right: 4px;">
                        <polyline points="3 6 5 6 21 6"></polyline>
                        <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                    </svg>
                    删除
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
    // 矩形/手柄是 CallbackProperty，改 _rectDegrees 不会自动重绘，显式请求一帧
    viewer.scene.requestRender();
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

document.getElementById('downloadForm').addEventListener('submit', async function(e) {
    e.preventDefault();

    const downloadType = _radioValue('downloadType', 'map');

    if (!currentBounds) {
        showNotification('请先在地图上框选下载区域', 'warning');
        return;
    }

    // 大任务二次确认：瓦片数超软阈值（100k，小时级作业）时把预计耗时
    // 摆给用户，确认后才提交。0.1.4 起服务端不再硬性拒绝。
    const est = updateTileEstimate();
    if (est && est.over) {
        const hours = (est.count / 10 / 3600).toFixed(1);
        const ok = await showConfirm(
            `预计 ${est.count.toLocaleString('zh-CN')} 块瓦片，按 10 张/秒估算耗时约 ${hours} 小时。确定创建吗？`,
            { title: '大任务确认', danger: true });
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
            showNotification('请至少勾选一种输出格式（瓦片 / GeoTIFF）', 'warning');
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
        resetForm({ clearBounds: false, formId: 'processForm' });
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
            _previewState = { kind: 'imagery', taskId: task.id, taskType: t, name: task.name, layer };
            if (t === 'contour') contourPreviewActiveId = task.id;
        } else if (t === 'local_terrain' || t === 'dem') {
            const base = t === 'local_terrain'
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
                const provider = await Cesium.CesiumTerrainProvider.fromUrl(base);
                if (seq !== _previewSeq) return;
                viewer.terrainProvider = provider;
                _previewState = { kind: 'terrain', taskId: task.id, taskType: t, name: task.name, prevTerrainProvider: prev };
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
                    _previewState = { kind: 'imagery', taskId: task.id, taskType: t, name: task.name, layer };
                    if (task.north == null) {
                        viewer.camera.flyTo({
                            destination: Cesium.Rectangle.fromDegrees(hs.bounds[0], hs.bounds[1], hs.bounds[2], hs.bounds[3]),
                            duration: 1.2,
                        });
                    }
                    showNotification('该任务还没有地形切片，显示源 DEM 的晕渲预览', 'info');
                } else {
                    showNotification(t === 'dem'
                        ? '该任务没有地形切片、也没有可渲染的 DEM 源文件，仅定位到区域'
                        : '切片与源文件都不存在，仅定位到区域', 'info');
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
            showNotification('预览失败: ' + (err && err.message ? err.message : err), 'danger');
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
    previewTask({
        id: taskId, task_type: 'contour', zoom_max: zoomMax,
        name: info.name || ('等高线 #' + taskId),
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
            contourPreviewTasks.set(taskId, {
                name: task.name, zoom_max: task.zoom_max,
                north: task.north, south: task.south, east: task.east, west: task.west,
            });
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
    // 首屏不再自己 fetch /api/contour/tasks：loadActiveTasks（tasks.js）首屏
    // 必拉同一份响应，且 contour 路刻意不带 ?status=active（预览要从里面筛
    // completed）——等它 resolve 后共享，首屏同接口只拉一遍。
    // 接口失败时 loadActiveTasks 自己已经 toast 过，这里静默不重复打扰。
    const shared = (typeof firstActiveTasksLoad !== 'undefined' && firstActiveTasksLoad)
        ? firstActiveTasksLoad
        : Promise.resolve();
    shared.then(function() {
        const tasks = (typeof latestContourTasks !== 'undefined') ? latestContourTasks : [];
        tasks.forEach(function(t) {
            if (t.status === 'completed') {
                contourPreviewTasks.set(t.id, {
                    name: t.name, zoom_max: t.zoom_max,
                    north: t.north, south: t.south, east: t.east, west: t.west,
                });
            }
        });
        updateContourPreviewButtons();
    }).catch(function() {});
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
            else showToast('复制失败，请手动选择复制', 'error');
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

    // 缩放级别：相机高度换算的近似 zoom，300ms 轮询（camera.changed 太高频）
    function updateZoom() {
        if (zoomEl) zoomEl.textContent = 'z' + _heightToZoom(viewer.camera.positionCartographic.height);
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
        coordsEl.textContent =
            '经度 ' + lastCoords.lng.toFixed(4) + '°  纬度 ' + lastCoords.lat.toFixed(4) + '°';
    }
    if (coordsEl) {
        const moveHandler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
        moveHandler.setInputAction(function (event) {
            pending = event.endPosition;
        }, Cesium.ScreenSpaceEventType.MOUSE_MOVE);
        viewer.scene.canvas.addEventListener('mouseout', function () {
            pending = null;
            lastCoords = null;
            coordsEl.textContent = '经度 — 纬度 —';
        });
        // 点击状态栏坐标 -> 复制「经度,纬度」（GIS 惯用顺序，可直接贴进大多数工具）
        coordsEl.addEventListener('click', function () {
            if (!lastCoords) return;
            _copyText(
                lastCoords.lng.toFixed(6) + ', ' + lastCoords.lat.toFixed(6),
                '坐标已复制'
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
                '选区四至已复制（W,S,E,N）'
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
    // 「下载」按钮 -> 下载弹窗；「删除」按钮 -> 清空选区；.bounds-v 数值 -> 点击编辑。
    const boundsInfo = document.getElementById('boundsInfo');
    if (boundsInfo) {
        boundsInfo.addEventListener('click', function (e) {
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
    }

    // 首屏填充「请在地图上框选下载区域」提示与状态栏选区摘要
    updateBoundsInfo();
}
