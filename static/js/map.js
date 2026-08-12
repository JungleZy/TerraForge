let viewer = null;
let currentBounds = null;

// 选区四至的唯一校验口径 —— 与后端 geo_validation.validate_bbox 的四条规则
// 逐条同构：north > south、|lat| <= 90、|lon| <= 180、east > west。
//
// 为什么必须只有一份：改前三个选区入口各判各的 —— 框选落定层零面积照收
//（单击就能造出 0°×0° 的「选区」，预估框还报「约 6 张瓦片」），
// _applyBoundsEdit 与 _readManualBounds 只校纬度、不校经度量级（east=400 照收，
// 状态栏读出「300.000° × 20.000°」），三处又都刻意放行 west=170/east=-170
// 这类跨反经线矩形。裸四至那条路后端一律 400，于是用户拿到的是一条**负宽度**
// 读数、一颗点下去必然失败的按钮，和一句直接漏进中文界面的英文报错。
//
// 跨反经线**不是**做不了，只是这四个数表达不了它：2026-08 起 POST /api/tasks
// 与 /api/dem/tasks 在载荷带 region 时接受它（east 规范化成 >180，见
// src/contracts/region.py 与 DemTaskManager.create_task；库里真的躺着 east=181
// 的行）。裸四至那条路仍然 400，而那是**有意**的 —— east > west 是它的定义域，
// 少了这条就没法把「跨界」和「用户把东西写反了」区分开。所以这道闸对**画出来
// 的矩形**照旧正确：要在地图上跨界，得先给它一条 region 的表达（导入多边形那
// 条路），而不是在这里放开 east > west。
//
// 每条判据都写成双边、且顺序照抄后端 —— 不是冗余，是**报错理由**的对拍口径。
// 只写单边会把越界漏给下一条：east=-400 时缺了「east >= -180」，
// 落到 east > west 上报「东边界必须大于西边界」，而后端说的是「经度越界」。
// 放行集合一样、理由不一样，用户照着改还是过不了。
//
// 返回 null 表示通过；否则返回该说给用户听的文案 key。
function validateBoundsRules(b) {
    if (!(b.north > b.south)) return 'js.map.edit.north_gt_south';
    if (!(b.south >= -90 && b.south <= 90 && b.north >= -90 && b.north <= 90)) {
        return 'js.map.edit.lat_range';
    }
    if (!(b.west >= -180 && b.west <= 180 && b.east >= -180 && b.east <= 180)) {
        return 'js.map.edit.lon_range';
    }
    if (!(b.east > b.west)) return 'js.map.edit.east_gt_west';
    return null;
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
        // 下标不再 swap：currentBounds 的每一个写入点都过了 validateBoundsRules
        //（east > west、north > south），xMin > xMax 与 yMin > yMax 已不可达，
        // swap 只会掩盖真出现时的口径错误。
        const [xMin, yMax] = _latLonToTile(bounds.south, bounds.west, z);
        const [xMax, yMin] = _latLonToTile(bounds.north, bounds.east, z);
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
    // 导入的区域走服务端（POST /api/region/estimate）。
    //
    // 这条分支不是优化，是这个功能**存在的理由**：多边形的意义就是它比自己的
    // 外接矩形小。一个 L 形省份、一条流域、一个带洞的行政区，bbox 里可能有
    // 一半以上的瓦片压根不需要下载。继续拿下面那个 bbox 估算器报数，读数就会
    // 与真正会下载的量差出一倍，用户看到的是「导入了多边形但张数没变」——
    // 那等于宣布这个功能没生效。
    //
    // 多边形栅格化不能搬到前端：那需要与后端 iter_region_tile_spans 逐像素
    // 一致的实现（含孔洞的奇偶规则与跨反经线拆分），两份实现必然漂移，而漂移
    // 的表现是「预估说 8 万、实际下了 12 万」。
    //
    // 矩形路仍走客户端 estimateTileCount：读数要跟着拖角点即时变（MOUSE_MOVE
    // 级别），一次往返在那条路上不可接受。
    if (_regionSpec) {
        return _renderRegionTileEstimate(el, zMin, zMax);
    }
    const count = estimateTileCount(currentBounds, zMin, zMax);
    _paintTileEstimate(el, count, null);
    return { count, over: count > TASK_TILE_LIMIT };
}

// 把张数（可选：磁盘预算判决）写进 #tileEstimate。
// 走 textContent 而不是 innerHTML：这里唯一的变量是数字，但 verdict 的数字
// 来自服务端响应，没有理由让它经过 HTML 解析。
function _paintTileEstimate(el, count, verdict) {
    const formatted = count.toLocaleString('zh-CN');
    const over = count > TASK_TILE_LIMIT;
    const parts = [];
    if (over) {
        const hours = (count / 10 / 3600).toFixed(1);
        parts.push(t('js.map.tile_estimate.over', { count: formatted, hours: hours }));
    } else {
        parts.push(t('js.map.tile_estimate.count', { count: formatted }));
    }
    // 磁盘预算：**永远带数字**（后端 BudgetVerdict 的同一条约定）——「空间不足」
    // 这四个字对用户没有任何操作性，他要知道的是还差多少。
    // 刻意不显示 verdict.reason：那是一句英文散文（disk_budget.py 就是这么拼的），
    // 原样贴进中文界面就是 A7 修过的中英混杂问题。数字自己说话。
    if (verdict) {
        parts.push(verdict.ok
            ? t('js.region.budget.ok', {
                required: _fmtBytes(verdict.required_bytes),
                free: _fmtBytes(verdict.free_bytes),
            })
            : t('js.region.budget.short', {
                required: _fmtBytes(verdict.required_bytes),
                free: _fmtBytes(verdict.free_bytes),
                shortfall: _fmtBytes(verdict.shortfall_bytes),
            }));
    }
    el.textContent = parts.join(' · ');
    el.classList.toggle('tile-estimate--over', over || !!(verdict && !verdict.ok));
    el.hidden = false;
}

// --- 首屏加载动画（Splash） ----------------------------------------------------
// 进度 = 模拟缓动（封顶 90%）+ 真实就绪事件补完：当前视野的地形/影像瓦片
// 全部落地（globe.tilesLoaded）即推满淡出；慢网络不等到底，initMap 里的
// 3s 上限先到先放。JS 异常时 stage 原地显示错误，不让用户对着永远转圈的
// 动画猜。
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

// 底图源名 -> 界面文案。前缀写成完整字面量（不是模板串）：tests/test_i18n.py 的
// _DYNAMIC_KEY_SITES 靠在源码里找到这个字面量来确认拼接点还活着。
function _basemapSourceLabel(source) {
    const key = 'js.map.basemap.src_' + source;
    const label = t(key);
    return label === key ? source : label;     // 自定义模板等未登记的源报原值
}

// 配置的底图源取不到瓦片时后端会自动换一张（见 services/basemap_source.py 的
// AUTO_FALLBACK_ORDER）。换了必须说 —— 用户点开配置页看到的还是 Esri，屏幕上
// 却是 OSM，不解释一句就是本项目最不能接受的那种静默。
//
// 为什么首屏之后还得接着查：首屏渲染时后端一块瓦片都还没取过，回退状态是未知的，
// 内联下发的描述符只能如实报配置值。等第一批瓦片落定（几秒）状态才成立 —— 而且
// 它之后还会变，上游可能在会话中途挂掉、也可能又活过来。

// 已经通告过、且当前正画在屏幕上的那张底图。
// _announcedBasemapSource 用来判「变了没」：轮询每 30 秒一轮，不记住上一次就会
// 把同一条 toast 每轮弹一遍。_renderedBasemap 用来判「要不要重建图层」：首屏那次
// 通告拿到的就是构造 Viewer 用的那份描述符，重建等于把整屏瓦片白重新拉一遍。
let _announcedBasemapSource = null;
let _renderedBasemap = null;

function _watchBasemapFallback(descriptor) {
    if (descriptor && descriptor.fallback) {
        _announceBasemapFallback(descriptor);
    } else if (descriptor && descriptor.source) {
        _announcedBasemapSource = descriptor.source;
    }
    // 首屏 5 秒查一次，之后每 30 秒一轮。回退是会中途才发生的（用户平移了一小时，
    // 上游这时候挂了），一次性的 setTimeout 意味着这种回退只有刷新才看得见 ——
    // 与本函数上面那句「换了必须说」自相矛盾。
    setTimeout(function () {
        _checkBasemapFallback();
        setInterval(_checkBasemapFallback, 30000);
    }, 5000);
}

async function _checkBasemapFallback() {
    try {
        const resp = await fetch('/api/basemap');
        const data = await resp.json();
        const bm = data && data.basemap;
        if (!bm || !bm.source || bm.source === _announcedBasemapSource) return;
        if (bm.fallback) {
            _announceBasemapFallback(bm);
        } else {
            // 回到配置的那张同样要说：用户看着「已切换到 OSM」的提示，屏幕上却
            // 早就换回 Esri 了，一样是对不上。
            _announceBasemapRestored(bm);
        }
    } catch (err) {
        console.warn('[basemap] fallback check failed:', err);
    }
}

// maximumLevel 和 credit 在 UrlTemplateImageryProvider 构造完之后是只读的，
// 换源只能整层替掉。不替的后果是两条：配置 Google（21 层）回退到 Esri（19 层）
// 后 Cesium 照旧请求 z20+，后端 _MAX_ZOOM 是 24 放行、上游 404，用户看到的是
// 一片黑；署名也还挂着配置那张源的名字，而 Esri / OSM 的署名是许可要求。
function _rebuildBaseImagery(bm) {
    if (!viewer || !bm || !bm.url) return;
    if (_renderedBasemap
        && _renderedBasemap.url === bm.url
        && _renderedBasemap.max_level === bm.max_level
        && _renderedBasemap.credit === bm.credit) {
        return;                                 // 没换源，别把整屏瓦片重拉一遍
    }
    const layer = new Cesium.ImageryLayer(new Cesium.UrlTemplateImageryProvider({
        // 瓦片走页面级瓦片 origin（ui.js tileUrl / src/core/tile_server.py），
        // 躲开浏览器对单源的 6 连接上限。用哪个 origin 是页面启动时
        // initTileOrigin() 探测一次定下的会话状态，这里不再看描述符里的端口 ——
        // 探测失败或非 http 页面时它保留同源路径，行为不变。
        url: tileUrl(bm.url),
        tilingScheme: new Cesium.WebMercatorTilingScheme(),
        maximumLevel: bm.max_level == null ? undefined : bm.max_level,
        credit: bm.credit || '',
    }));
    const previous = viewer.imageryLayers.get(0);
    viewer.imageryLayers.add(layer, 0);         // 先加后删：中间不留没有底图的那一帧
    if (previous) viewer.imageryLayers.remove(previous, true);
    _renderedBasemap = bm;
}

function _announceBasemapFallback(bm) {
    showNotification(t('js.map.basemap.fallback', {
        source: _basemapSourceLabel(bm.source),
        configured: _basemapSourceLabel(bm.configured_source),
    }), 'warning');
    _announcedBasemapSource = bm.source;
    _rebuildBaseImagery(bm);
}

function _announceBasemapRestored(bm) {
    showNotification(t('js.map.basemap.restored', {
        source: _basemapSourceLabel(bm.source),
    }), 'info');
    _announcedBasemapSource = bm.source;
    _rebuildBaseImagery(bm);
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
            // 与 _rebuildBaseImagery 同一口径：origin 由 initTileOrigin() 在
            // 创建 Viewer 之前探测一次定下（templates/index.html 的 tileOriginReady
            // 门控），这里只把内部绝对路径交给 tileUrl。
            url: tileUrl(bm.url),
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
    // _watchBasemapFallback 必须排在 Viewer 之后：首屏描述符里 fallback 已经为
    // true 时它会立刻通告并重建底图图层，而重建要拿 viewer.imageryLayers ——
    // 排在前面的话那条分支跑在 viewer 还是 null 的时候。
    _renderedBasemap = bm;
    _watchBasemapFallback(bm);

    viewer.camera.setView({
        destination: Cesium.Cartesian3.fromDegrees(centerLng, centerLat, _zoomToHeight(initialZoom)),
    });

    // 首帧渲染 ≠ 地图就绪：第一帧上屏时底图瓦片还在网络上（服务端代理转发，
    // 秒级），画出的是没有影像的黑色球体 —— skyBox 关着、globe 底色纯黑。
    // 在首帧淡出 splash 等于把这段黑屏原样露给用户。真正的就绪信号是
    // globe.tilesLoaded（当前视野的地形/影像瓦片全部落地）；瓦片每落地一批
    // 都会触发 requestRender，postRender 持续有帧，轮询不会卡死。放人的
    // 上限在下面的 3s 计时器，initSplash 里另有 20s 兜底。
    // 具名函数声明而不是 const 箭头/匿名：removeEventListener 要引用，
    // tests/test_splash_ready_signal.py 也按函数名切这段体。
    function onFirstFrame() {
        if (!viewer.scene.globe.tilesLoaded) return;
        viewer.scene.postRender.removeEventListener(onFirstFrame);
        splashReady();
    }
    viewer.scene.postRender.addEventListener(onFirstFrame);
    // 上限放人：tilesLoaded 要等视野瓦片全部经代理落地（逐级细化、串行
    // 往返），慢网络下会把用户关在开屏里好几秒 —— 比一闪而过的黑屏更难忍。
    // 3 秒一到无论瓦片是否齐都淡出；splashReady 幂等，先到先放。
    setTimeout(splashReady, 3000);

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

// 选区（矩形与导入的多边形共用）在地图上的强调色。字面量原本在本文件里出现
// 三次（矩形填充、矩形描边、角点手柄），导入区域又要用同一个色 —— 第四份
// 复制就该收口了。取值与 --color-accent 一致；Cesium 不认 var()，主题切换
// 也不会重画已有实体，所以这里保持字面量而不是去求 CSS 自定义属性。
const SELECTION_ACCENT_CSS = '#38bdf8';

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
            material: Cesium.Color.fromCssColorString(SELECTION_ACCENT_CSS).withAlpha(0.15),
            outline: true,
            outlineColor: Cesium.Color.fromCssColorString(SELECTION_ACCENT_CSS),
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
                color: Cesium.Color.fromCssColorString(SELECTION_ACCENT_CSS),
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

// 清空选区。矩形与导入区域**一起**清：用户点的是范围浮层上那颗「删除」，
// 他的意思是「当前选区没了」，而不是「只删掉其中的矩形部分」。
function clearSelection() {
    if (_selectionEntity && viewer) {
        viewer.entities.remove(_selectionEntity);
        _selectionEntity = null;
    }
    _removeHandles();
    _rectDegrees = null;
    currentBounds = null;
    _clearRegionState();
    _exitDrawMode();
    updateBoundsInfo();
    refreshSubmitButtonState();
}

// --- 导入区域（§5.1）----------------------------------------------------------
//
// 选区的第二种来源：用户拖进来或选进来一个
// GeoJSON / KML / KMZ / Shapefile(zip) 文件，服务端 POST /api/region/import
// 解析成 RegionSpec 回给前端（geometry 是 MultiPolygon，每个多边形的第一个环
// 是外环、其余是**孔洞**）。
//
// 与矩形是**互斥**的两种选区：导入即替换。刻意不做「矩形 ∩ 多边形」那种组合 ——
// 没人能预期结果，而下载范围是这个应用里最不能含糊的一个数。
//
// 为什么必须留住整份几何而不是只留 bbox：多边形的意义就是它比自己的外接矩形小
// （见 updateTileEstimate 里那段）。只留 bbox 等于把这个功能的收益全扔掉。

// 服务端 RegionSpec.to_dict() 原样：{type, coordinates, bbox, crs, source, display_name}。
// null = 当前没有导入区域（选区是矩形或空）。
let _regionSpec = null;
// 画在地图上的实体（每个多边形一个填充 + 每个环一条贴地描边线）。
let _regionEntities = [];
// 服务端估算的缓存 + 在飞请求。key 把「会改变张数的输入」编码进去，
// 输入没变就不重复往返（弹窗每次打开、每次改层级都会调 updateTileEstimate）。
let _regionEstimate = null;
let _regionEstimatePending = null;
// 请求序号：用户可以连着改层级，晚发的请求可能先回来。
let _regionEstimateSeq = 0;

/** 撤掉区域实体并清空状态。**不**碰矩形选区那一套。 */
function _clearRegionState() {
    if (viewer) {
        _regionEntities.forEach(function (entity) {
            try { viewer.entities.remove(entity); } catch (e) { /* 实体已被移除 */ }
        });
    }
    _regionEntities = [];
    _regionSpec = null;
    _regionEstimate = null;
    _regionEstimatePending = null;
}

/** `[[lon, lat], ...]` -> Cesium 位置数组。 */
function _regionRingPositions(ring) {
    const flat = [];
    ring.forEach(function (point) { flat.push(point[0], point[1]); });
    return Cesium.Cartesian3.fromDegreesArray(flat);
}

/**
 * 把 RegionSpec 的几何画到地图上，**含孔洞**。
 *
 * 填充用 PolygonHierarchy（第二个参数就是洞的层级），描边**另开贴地折线**而不是
 * `polygon.outline: true`：贴地多边形（不带 perPositionHeight）的 outline 在
 * Cesium 里不可靠 —— 它是 geometry 级的线框，在地形上会被埋进地表看不见，
 * 而洞的边界根本不会被描出来。逐环画 clampToGround 的 polyline 是唯一能让
 * 「洞在哪」看得见的办法，而洞看不见就等于用户无法确认导入是否正确。
 */
function _drawRegionEntities(region) {
    const fill = Cesium.Color.fromCssColorString(SELECTION_ACCENT_CSS).withAlpha(0.15);
    const stroke = Cesium.Color.fromCssColorString(SELECTION_ACCENT_CSS);
    (region.coordinates || []).forEach(function (poly) {
        const rings = (poly || []).filter(function (ring) { return ring && ring.length >= 3; });
        if (!rings.length) return;
        const holes = rings.slice(1).map(function (ring) {
            return new Cesium.PolygonHierarchy(_regionRingPositions(ring));
        });
        _regionEntities.push(viewer.entities.add({
            polygon: {
                hierarchy: new Cesium.PolygonHierarchy(_regionRingPositions(rings[0]), holes),
                material: fill,
                // 贴地：导入的区域可能横跨几十度，不贴地会在地形上穿进穿出。
                classificationType: Cesium.ClassificationType.TERRAIN,
                arcType: Cesium.ArcType.GEODESIC,
            },
        }));
        rings.forEach(function (ring) {
            // 闭合：GeoJSON 的环首尾点相同，但 KML/Shapefile 经服务端归一后
            // 也保证闭合，所以直接照原样画，不再补点。
            _regionEntities.push(viewer.entities.add({
                polyline: {
                    positions: _regionRingPositions(ring),
                    width: 2,
                    material: stroke,
                    clampToGround: true,
                    arcType: Cesium.ArcType.GEODESIC,
                },
            }));
        });
    });
}

/**
 * 几何事实一行文本：多边形数、孔洞数、跨反经线。
 *
 * 这三件事**必须**出现在范围读数里：它们是「这不是一个矩形」的全部内容，
 * 而它们各自都会让下载量与用户的直觉差出一大截（孔洞少下、跨反经线要拆两段）。
 * 数字在前端现算而不是让服务端给：RegionSpec.to_dict() 只给几何本身，
 * 而这三个数就是几何的直接读数 —— 让服务端多回三个派生字段才是重复。
 */
function _regionFactsText(region) {
    const polys = region.coordinates || [];
    const holes = polys.reduce(function (n, poly) {
        return n + Math.max(0, (poly || []).length - 1);
    }, 0);
    const facts = [];
    if (polys.length > 1) facts.push(t('js.region.facts.polygons', { n: polys.length }));
    if (holes > 0) facts.push(t('js.region.facts.holes', { n: holes }));
    // bbox 是 [west, south, east, north]；east > 180 是服务端表达「跨反经线」
    // 的方式（RegionSpec.crosses_antimeridian 同一判据），不是脏数据。
    const bbox = region.bbox || [];
    if (bbox.length === 4 && bbox[2] > 180) facts.push(t('js.region.facts.antimeridian'));
    if (!facts.length) facts.push(t('js.region.facts.polygon'));
    return facts.join(' · ');
}

/**
 * 落地一个导入的区域：替换选区、画几何、飞过去、刷新读数。
 *
 * payload 是 POST /api/region/import 的响应：{region, summary, warnings}。
 * warnings 是**机器码**（服务端约定，目前只有 'crosses_antimeridian'），
 * 按码查文案；认不出的码原样显示，绝不静默丢掉 —— 一条没显示出来的警告
 * 与没有警告是两件完全不同的事。
 */
function applyImportedRegion(payload) {
    const region = payload && payload.region;
    if (!region || !region.bbox || region.bbox.length !== 4) {
        showToast(t('js.region.import.bad_payload'), 'danger');
        return;
    }
    // 先整体清（含上一个导入区域与矩形），再落新的 —— clearSelection 内部会
    // 调 _clearRegionState，顺序反了会把刚设好的区域清掉。
    clearSelection();
    _regionSpec = region;
    const [west, south, east, north] = region.bbox;
    currentBounds = { north: north, south: south, east: east, west: west };
    if (viewer) {
        _drawRegionEntities(region);
        // 不夹 east：Cesium 收 east > 180。实测（vendor 的 1.143.0 发行版，
        // 本页控制台）Rectangle.fromDegrees(179,39,181,40) 回的就是
        // west=179 / east=181 / width=2.0000°，center 在 180°；
        // getRectangleCameraCoordinates 把相机放在 lon 180（区域正中）。
        // 夹成 180 反而把相机挪到 lon 179.5 —— 一个跨界区域只框住西边那一半。
        // try/catch 留着，但理由不是「fromDegrees 会拒绝」（它不会，flyTo 也
        // 不抛）：相机是这个函数里唯一可能炸的一步，让它吃掉后面的四至刷新与
        // 警告 toast，用户就会拿到一个没有任何提示的半截状态。
        try {
            viewer.camera.flyTo({
                destination: Cesium.Rectangle.fromDegrees(west, south, east, north),
                duration: 1.0,
            });
        } catch (e) {
            console.error('Failed to fly to imported region:', e);
        }
    }
    updateBoundsInfo();
    announceBounds();
    updateTileEstimate();
    refreshSubmitButtonState();

    (payload.warnings || []).forEach(function (code) {
        showToast(regionWarningText(code), 'warning');
    });
    showToast(t('js.region.import.applied', {
        name: region.display_name || t('js.region.unnamed'),
        facts: _regionFactsText(region),
    }), 'success');
}

// 服务端只回机器码，不回散文（它没有语种上下文）。逐码写成完整键字面量：
// tests/test_i18n.py 的双向闭合按字面量扫源码，把前缀和 code 拼起来取键那种
// 写法会让文案被判成无人引用而删掉（本注释里也刻意不写出那个拼接式，
// 引号里的键形状字面量同样会被扫到）。同一形态的先例是 history.js 的
// TERRAIN_QUALITY_KEYS。
const REGION_WARNING_TEXTS = {
    'crosses_antimeridian': t('js.region.warning.crosses_antimeridian'),
    // 坐标系不明的两条（TF-SEC-011）。它们以前只写服务端日志，用户完全看不到
    // —— 而后果是区域静默落在错误的位置，一整轮下载全白跑。
    'missing_crs': t('js.region.warning.missing_crs'),
    'unreadable_crs': t('js.region.warning.unreadable_crs'),
    'skipped_non_polygon_features': t('js.region.warning.skipped_non_polygon_features'),
    'encoding_fallback_gb18030': t('js.region.warning.encoding_fallback_gb18030'),
    'extension_content_mismatch': t('js.region.warning.extension_content_mismatch'),
};

function regionWarningText(code) {
    // hasOwnProperty 同 getStatusColor 的理由：裸下标下 code === 'constructor'
    // 会取到构造函数，`||` 兜不到，一坨函数源码进 toast。
    return (Object.prototype.hasOwnProperty.call(REGION_WARNING_TEXTS, code)
        && REGION_WARNING_TEXTS[code]) || code;
}

/**
 * 上传一个区域文件。成功即应用；失败把服务端的理由说全。
 *
 * 后缀被拒时 400 的 body 带 `supported_extensions` 数组（服务端刻意不把这份
 * 清单塞进文案里，避免文案与实际支持的格式漂移）—— 在这里拼进提示，
 * 用户才知道该拿什么格式再来一次。
 */
async function importRegionFile(file) {
    if (!file) return;
    const form = new FormData();
    form.append('file', file);
    try {
        const response = await fetch('/api/region/import', { method: 'POST', body: form });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) {
            const exts = (result.supported_extensions || []).join(' ');
            throw new Error(exts
                ? `${result.error || 'HTTP ' + response.status}（${exts}）`
                : (result.error || 'HTTP ' + response.status));
        }
        applyImportedRegion(result);
    } catch (error) {
        showToast(t('js.region.import.failed', { error: error.message }), 'danger');
    }
}

// 会改变张数**或磁盘占用**的输入。style / output_format 也算：服务端的估算里
// 含单张均值与磁盘预算，两者都与样式和产物形态有关。
// export_mbtiles 同理且更狠 —— 容器差不多是松散镜像的**又一整份**（实测
// 116.0-116.2/39.0-39.2 z10-13：output_bytes 5,711,460 → 6,795,261）。
// 不把它编进 key 的后果不是「少算一点」，是勾选框翻转后界面继续画上一次的
// 判决：缓存命中，请求根本不发。
function _regionEstimateKey(zMin, zMax) {
    return [
        zMin, zMax,
        document.getElementById('mapStyle')?.value || '',
        _outputFormatValue() || '',
        document.getElementById('outputPath')?.value || '',
        document.getElementById('exportMbtiles')?.checked ? 1 : 0,
    ].join('|');
}

/** 缓存命中就直接画；否则先写「计算中」再发请求，回来后重画。 */
function _renderRegionTileEstimate(el, zMin, zMax) {
    const key = _regionEstimateKey(zMin, zMax);
    if (_regionEstimate && _regionEstimate.key === key) {
        _paintTileEstimate(el, _regionEstimate.tile_count, _regionEstimate.verdict);
        return {
            count: _regionEstimate.tile_count,
            over: _regionEstimate.tile_count > TASK_TILE_LIMIT,
        };
    }
    el.textContent = t('js.region.estimate.pending');
    el.classList.remove('tile-estimate--over');
    el.hidden = false;
    _requestRegionEstimate(key, zMin, zMax);
    return null;
}

function _requestRegionEstimate(key, zMin, zMax) {
    const seq = ++_regionEstimateSeq;
    const spec = _regionSpec;
    _regionEstimatePending = (async function () {
        try {
            const response = await fetch('/api/region/estimate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    region: spec,
                    zoom_min: zMin,
                    zoom_max: zMax,
                    style: document.getElementById('mapStyle')?.value || '',
                    output_format: _outputFormatValue() || '',
                    output_path: document.getElementById('outputPath')?.value || '',
                    // 与创建任务的提交体同名同义（见 submit 那处的 export_mbtiles）。
                    // 漏掉它，勾了「同时导出 MBTiles」的用户拿到的判决短一整个容器
                    // 的量 —— 而磁盘预算判决存在的意义就是「下之前先知道够不够」。
                    export_mbtiles: document.getElementById('exportMbtiles')?.checked ? 1 : 0,
                }),
            });
            const result = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(result.error || ('HTTP ' + response.status));
            // 过期响应直接丢：用户已经改过层级、或者已经换/清了区域。
            if (seq !== _regionEstimateSeq || spec !== _regionSpec) return;
            _regionEstimate = {
                key: key,
                tile_count: Number(result.tile_count) || 0,
                estimate: result.estimate || null,
                verdict: result.verdict || null,
            };
            updateTileEstimate();
        } catch (error) {
            if (seq !== _regionEstimateSeq || spec !== _regionSpec) return;
            const el = document.getElementById('tileEstimate');
            if (el) {
                el.textContent = t('js.region.estimate.failed', { error: error.message });
                el.classList.add('tile-estimate--over');
                el.hidden = false;
            }
        }
    })();
}

/**
 * 提交前拿到一个**确定**的张数。
 *
 * 矩形路是同步的，直接返回。多边形路第一次调用时估算可能还在飞 —— 那时
 * updateTileEstimate 返回 null，超限二次确认就会被静默跳过，用户在毫无预告的
 * 情况下建出一个几小时的任务。所以这里等那一次往返。
 */
async function currentTileEstimate() {
    const sync = updateTileEstimate();
    if (sync || !_regionSpec) return sync;
    if (_regionEstimatePending) {
        try { await _regionEstimatePending; } catch (e) { /* 失败已在界面上说明 */ }
    }
    return updateTileEstimate();
}

// --- 地点搜索（§5.1）----------------------------------------------------------
//
// GET /api/places/search?q=&limit= -> {enabled, results: [{name, bbox, kind, region}]}。
// bbox 是 [west, south, east, north]，落地成一个**矩形**选区（它本来就是矩形），
// 所以之后拖角点、点读数编辑的行为与手画的框完全一致。
//
// 地理编码器未配置时服务端回 200 + enabled:false。那时控件**渲染成禁用**并给出
// 指向 geocoder_url 设置的提示 —— 不静默隐藏（用户会以为这个功能不存在，而它
// 只是没配地址），也不内置任何默认服务商（那等于替用户决定把他的地名查询发给
// 一个第三方，而这是个可离线部署的工具）。

let _placeSearchTimer = null;
// 请求序号：连续敲字时晚发的请求可能先回来，把旧结果盖在新关键词上。
let _placeSearchSeq = 0;
// null = 还没探测过；true/false = 服务端说的。探测只在面板首次展开时做一次。
let _geocoderEnabled = null;

function _closePlaceSearch() {
    const panel = document.getElementById('placeSearchPanel');
    const toggle = document.getElementById('mapPlaceSearch');
    if (panel) panel.hidden = true;
    if (toggle) {
        toggle.setAttribute('aria-expanded', 'false');
        toggle.classList.remove('map-panel-btn--active');
        toggle.focus();          // 焦点不能掉回 <body>：键盘用户会丢失位置
    }
}

/** 把结果区换成一句提示（无结果 / 未配置 / 出错都走这里）。 */
function _renderPlaceSearchHint(text) {
    const results = document.getElementById('placeSearchResults');
    if (!results) return;
    // textContent：提示里可能含服务端的错误原文。
    results.innerHTML = '';
    const hint = document.createElement('div');
    hint.className = 'place-search__hint';
    hint.textContent = text;
    results.appendChild(hint);
}

/** enabled:false -> 输入框禁用 + 指向 geocoder_url 的提示。 */
function _applyGeocoderDisabled() {
    const input = document.getElementById('placeSearchInput');
    if (input) {
        input.disabled = true;
        input.value = '';
    }
    _renderPlaceSearchHint(t('js.search.disabled_hint'));
}

/**
 * 探测地理编码器是否配置好。空 q 的请求不查上游，只回 enabled。
 *
 * 为什么在面板展开时探测而不是等用户敲第一个字：验收标准是「未配置时控件
 * **看得出**是禁用的」。让用户先敲完一个地名再告诉他这个功能没开，是把
 * 「配置缺失」伪装成「搜不到」。
 * 探测本身失败（网络/500）时**不**禁用控件：那是未知，不是「没配」——
 * 禁用一个其实可用的控件比多一次无结果更糟。
 */
async function _probeGeocoder() {
    if (_geocoderEnabled !== null) return _geocoderEnabled;
    try {
        const response = await fetch('/api/places/search?q=&limit=1');
        if (!response.ok) return null;
        const data = await response.json();
        _geocoderEnabled = !!data.enabled;
        if (!_geocoderEnabled) _applyGeocoderDisabled();
        return _geocoderEnabled;
    } catch (error) {
        console.error('Failed to probe geocoder:', error);
        return null;
    }
}

async function _runPlaceSearch(query) {
    const term = (query || '').trim();
    const results = document.getElementById('placeSearchResults');
    if (!results) return;
    if (!term) {
        results.innerHTML = '';
        return;
    }
    const seq = ++_placeSearchSeq;
    try {
        const response = await fetch(
            `/api/places/search?q=${encodeURIComponent(term)}&limit=8`);
        const data = await response.json().catch(() => ({}));
        if (seq !== _placeSearchSeq) return;      // 过期响应
        if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
        // 未配置：这条路径也要兜住 —— 用户可能在面板开着的时候才把 geocoder_url
        // 配上/去掉，探测那一次的结论会过期。
        _geocoderEnabled = !!data.enabled;
        if (!_geocoderEnabled) {
            _applyGeocoderDisabled();
            return;
        }
        _renderPlaceResults(data.results || []);
    } catch (error) {
        if (seq !== _placeSearchSeq) return;
        _renderPlaceSearchHint(t('js.search.failed', { error: error.message }));
    }
}

/**
 * 渲染结果列表。
 *
 * 逐节点建 DOM 而不是拼 innerHTML：name / kind / region 全部来自第三方地理
 * 编码服务的响应，是本应用里最典型的「外部可控字符串」。bbox 走 data 属性存
 * JSON，点击时解析 —— 比在闭包里挂一堆监听更好清理（列表每次搜索整体重建）。
 */
function _renderPlaceResults(list) {
    const results = document.getElementById('placeSearchResults');
    if (!results) return;
    results.innerHTML = '';
    if (!list.length) {
        _renderPlaceSearchHint(t('js.search.no_results'));
        return;
    }
    list.forEach(function (place) {
        if (!place || !place.bbox || place.bbox.length !== 4) return;
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'place-search__item';
        item.dataset.bbox = JSON.stringify(place.bbox);
        item.textContent = place.name || '';
        const meta = document.createElement('span');
        meta.className = 'place-search__meta';
        meta.textContent = [place.kind, place.region].filter(Boolean).join(' · ');
        item.appendChild(meta);
        results.appendChild(item);
    });
}

/**
 * 选中一个地点：落成矩形选区并飞过去。
 *
 * 走的是与手动输入范围（_applyManualBounds）**同一套**写入，包括同一道
 * validateBoundsRules 闸门 —— 地理编码服务给的 bbox 未必满足本应用的四条规则
 * （跨反经线的国家、退化成一个点的地名都真实存在）。同一个选区被两个入口用
 * 两种标准放行，正是这套代码此前修掉的缺陷。
 */
function applyPlaceResult(bbox, name) {
    const west = Number(bbox[0]);
    const south = Number(bbox[1]);
    const east = Number(bbox[2]);
    const north = Number(bbox[3]);
    const reason = validateBoundsRules({ north: north, south: south, east: east, west: west });
    if (reason) {
        showNotification(t('js.search.unusable_bbox', { name: name, reason: t(reason) }), 'warning');
        return;
    }
    clearSelection();
    _rectDegrees = { west: west, south: south, east: east, north: north };
    _ensureSelectionEntity();
    _ensureHandles();
    _syncBoundsFromRect();
    if (viewer) {
        viewer.scene.requestRender();
        viewer.camera.flyTo({
            destination: Cesium.Rectangle.fromDegrees(west, south, east, north),
            duration: 1.0,
        });
    }
    updateBoundsInfo();
    announceBounds();
    updateTileEstimate();
    refreshSubmitButtonState();
    _closePlaceSearch();
}

/**
 * 区域导入按钮 + 地点搜索面板的接线。由 index.html 的 boot 块在 initMap 之后
 * 调用（applyPlaceResult / applyImportedRegion 都要 viewer）。
 */
function initRegionTools() {
    const importBtn = document.getElementById('mapRegionImport');
    const fileInput = document.getElementById('regionImportFile');
    if (importBtn && fileInput) {
        importBtn.addEventListener('click', function () { fileInput.click(); });
        fileInput.addEventListener('change', function () {
            const file = fileInput.files && fileInput.files[0];
            // 先清 value 再处理：不清的话再选**同一个文件**不会触发 change，
            // 用户点了没反应（清过一次选区之后重新导入同一份文件就是这条路）。
            fileInput.value = '';
            importRegionFile(file);
        });
    }

    const toggle = document.getElementById('mapPlaceSearch');
    const panel = document.getElementById('placeSearchPanel');
    const input = document.getElementById('placeSearchInput');
    const results = document.getElementById('placeSearchResults');
    if (!toggle || !panel || !input || !results) return;

    toggle.addEventListener('click', function () {
        if (!panel.hidden) {
            _closePlaceSearch();
            return;
        }
        panel.hidden = false;
        toggle.setAttribute('aria-expanded', 'true');
        toggle.classList.add('map-panel-btn--active');
        input.focus();
        _probeGeocoder();
    });

    // 300ms 去抖：每个按键都打一次上游地理编码是在替用户浪费他的配额。
    input.addEventListener('input', function () {
        clearTimeout(_placeSearchTimer);
        _placeSearchTimer = setTimeout(function () { _runPlaceSearch(input.value); }, 300);
    });
    input.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') {
            e.stopPropagation();
            _closePlaceSearch();
        } else if (e.key === 'Enter') {
            // 回车立刻搜，不等去抖 —— 用户已经明确表示「就是它」。
            e.preventDefault();
            clearTimeout(_placeSearchTimer);
            _runPlaceSearch(input.value);
        }
    });
    // 委托：结果列表每次搜索整体重建，逐项挂监听会漏清理。
    results.addEventListener('click', function (e) {
        const item = e.target.closest('.place-search__item');
        if (!item) return;
        let bbox;
        try {
            bbox = JSON.parse(item.dataset.bbox);
        } catch (err) {
            return;
        }
        applyPlaceResult(bbox, item.textContent);
    });
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
            // 与 validateBoundsRules 的量级规则同向兜住手柄拖拽这条不走闸门的
            // 路径：对侧钳位用的 ±1e-6 会把紧贴 180°/90° 的边推出合法域，
            // 提交时后端 400 报一个用户根本没输入过的数。
            d.north = Math.min(90, d.north);
            d.south = Math.max(-90, d.south);
            d.east = Math.min(180, d.east);
            d.west = Math.max(-180, d.west);
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
            // 单击（按下与抬起落在同一像素）会落成 west === east、
            // south === north 的零面积矩形。改前它照样入库：浮层读出
            // 0.000° × 0.000°，预估框还报「约 6 张瓦片」，而后端必然 400。
            // 这里与另外两个入口过同一道闸门；鼠标手势唯一能造出的违规就是
            // 零宽/零高（经纬度量级由拾取结果保证），所以不过就按
            // 「没画出选区」丢弃，而不是把闸门的具体理由念给用户听。
            if (validateBoundsRules(_rectDegrees)) {
                clearSelection();
                showNotification(t('js.map.bounds.no_area'), 'warning');
                return;
            }
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

    // 「同时导出 MBTiles」勾选态变化要重算判决：容器差不多是松散镜像的又一整份，
    // 磁盘够不够的结论会因为这一个勾选框翻面。updateTileEstimate 走的是
    // _regionEstimateKey 的缓存，勾选态已经编进 key，所以这一发必然打穿缓存。
    const exportMbtilesToggle = document.getElementById('exportMbtiles');
    if (exportMbtilesToggle) {
        exportMbtilesToggle.addEventListener('change', function () {
            updateTileEstimate();
            refreshSubmitButtonState();
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

        refreshSubmitButtonState();
    }

    typeEl.addEventListener('change', apply);
    if (sourceEl) {
        sourceEl.addEventListener('change', () => {
            apply();
            // 预告行不在上传行里（模板里它挨着档位下拉），apply() 藏不掉它 ——
            // 来源一变就得重画，否则切到 DEM 任务后它还挂着上一批上传文件的
            // 层级/张数/体积。收起的判据写在 renderTerrainTileEstimate 自己那儿。
            renderTerrainTileEstimate();
            // 每次切到 DEM 来源都重拉：任务列表随下载进度变化，陈旧列表会让
            // 用户选到一个当时还没完成的任务。
            if (sourceEl.value === 'dem_task') loadProcessDemTasks();
        });
    }
    const localFilesEl = document.getElementById('localTerrainFiles');
    if (localFilesEl) localFilesEl.addEventListener('change', updateLocalTerrainTifInfo);
    const contourFilesEl = document.getElementById('contourFiles');
    if (contourFilesEl) contourFilesEl.addEventListener('change', updateContourTifInfo);
    // 「自动」勾选态与层级数字框的禁用态联动。初值由服务端渲染（模板那侧的
    // {% if terrain_local_maxzoom_auto %}disabled），这里只管运行时的切换。
    const maxzoomAutoToggle = document.getElementById('localTerrainMaxzoomAuto');
    if (maxzoomAutoToggle) {
        maxzoomAutoToggle.addEventListener('change', () => {
            syncLocalTerrainMaxzoomDisabled();
            // 自动/手动会换掉基准层级的来源（后端估的建议值 vs 数字框里的数），
            // 预告必须跟着重算。
            renderTerrainTileEstimate();
        });
    }
    // 预告的另外三个输入。这三个都不需要再打一次 /api/raster/inspect：
    // 层级、bounds、逐层张数都在上一次的汇总里。
    ['localTerrainMaxzoom', 'localTerrainQuality', 'localTerrainNormals'].forEach((id) => {
        document.getElementById(id)?.addEventListener('change', renderTerrainTileEstimate);
    });
    apply();
    initContourTintUI();
}

// 「自动层级」勾选态 -> 层级数字框的禁用态。change 监听与 resetForm 共用一份：
// form.reset() 只把复选框拨回服务端渲染的那个默认，**不触发 change**，两边当场
// 脱节 —— 界面上是「自动挡勾着、数字框却能填」，提交送的却是 auto，用户填的数
// 静默不算数（resetForm 里两张 tif 信息卡跟着收起是同一个理由）。
// 只碰 disabled、不碰 value：那个数是用户取消勾选后的起点。清掉它不会让表单
// :invalid（min/max 对空值不适用，空值要 required 才拦），失败是静默的 ——
// 取消勾选后是个空数字框，提交那侧送的就是空串（`maxzoomEl?.value || ''`），
// 后端把空串当「未表态」回落到配置默认的自动挡，用户取消勾选等于没生效。
function syncLocalTerrainMaxzoomDisabled() {
    const autoEl = document.getElementById('localTerrainMaxzoomAuto');
    const numEl = document.getElementById('localTerrainMaxzoom');
    if (autoEl && numEl) numEl.disabled = autoEl.checked;
}

// --- 选完 tif 立刻显示的有效信息（本地高程切片 / 等高线共用）--------------------
//
// 不为了看一眼元信息先整包上传：DEM 动辄几百 MB 到 2 GB，而
// static/js/geotiff_meta.js 用 File.slice 只读几 KB 的 TIFF 目录就能拿到全部
// 标签。地理解释（EPSG -> 坐标系名称、投影坐标 -> 经纬度、像素 -> 建议层级）
// 交给带 GDAL 的后端 /api/raster/inspect —— 那需要一份完整 CRS 库，
// 前端手写换算在国内常见的 CGCS2000 分带上迟早出错。
//
// mode 一定要传对：高程切片按 Cesium 经纬度分块估层级、等高线按 Web Mercator
// 瓦片估，同一份 DEM 两者给出的数不一样（见 raster_probe._estimate_maxzoom）。
// 传错的话卡片上写的层级与不填层级时真正切出来的对不上，比不显示更糟。
//
// _tifInfoSeq 按卡片记：用户可以连着改选好几次，每次都有「读头部 + 一次 fetch」
// 两段异步。读一个 2 GB 文件的头部可能比一次 localhost fetch 慢，晚发的请求会
// 先回来；不带序号就会把旧文件的信息盖在新选择上。两张卡各记各的。
const _tifInfoSeq = new Map();

// 同一张卡上一次还没答完的请求。seq 闸门已经保证晚到的响应盖不掉新的渲染，
// 这里管的是另一件事：那个注定被丢弃的请求不该继续占着连接、让后端把一份
// 几百 MB DEM 的头部白解释一遍。按卡片记，两张卡互不打断。
const _tifInfoAbort = new Map();

// 这些警告意味着「这个文件切不了片」，用红色；其余只是提醒。
const _TIF_FATAL_WARNINGS = new Set([
    'header_unreadable', 'no_georeference', 'unknown_crs', 'some_unusable',
]);

function _fmtBytes(bytes) {
    const n = Number(bytes) || 0;
    if (n < 1024) return `${n} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let v = n / 1024;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}

// 有效数字截断 + 去掉尾随零：0.000277777778 -> 0.000277778，30.0 -> 30
function _sig(value, digits) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '—';
    return String(parseFloat(n.toPrecision(digits || 6)));
}

function _fmtLonLat(bounds) {
    const lon = (v) => `${Math.abs(v).toFixed(4)}°${v < 0 ? 'W' : 'E'}`;
    const lat = (v) => `${Math.abs(v).toFixed(4)}°${v < 0 ? 'S' : 'N'}`;
    return `${lon(bounds[0])} ${lat(bounds[1])} → ${lon(bounds[2])} ${lat(bounds[3])}`;
}

function _fmtResolution(file) {
    const scale = file.pixel_size;
    if (!scale || !scale[0]) return null;
    const [sx, sy] = scale;
    const square = !sy || Math.abs(sx - sy) / sx < 1e-6;
    if (file.crs_unit === 'degree') {
        const deg = square ? `${_sig(sx)}°` : `${_sig(sx)}° × ${_sig(sy)}°`;
        return file.pixel_meters ? `${deg} ≈ ${_sig(file.pixel_meters, 3)} m` : deg;
    }
    const unit = file.crs_unit || 'm';
    return square ? `${_sig(sx)} ${unit}` : `${_sig(sx)} × ${_sig(sy)} ${unit}`;
}

// 一行键值。wide=true 占满两列（范围/坐标系这种长串挤在半列里会折成三行）。
function _tifInfoRow(grid, key, value, wide) {
    if (value === null || value === undefined || value === '') return;
    const item = document.createElement('div');
    item.className = wide ? 'detail-item tif-info__item--wide' : 'detail-item';
    const k = document.createElement('span');
    k.className = 'detail-k';
    k.textContent = key;
    const v = document.createElement('span');
    v.className = 'detail-v detail-v--num';
    v.textContent = value;
    item.append(k, v);
    grid.appendChild(item);
}

function _tifInfoWarnings(box, warnings) {
    (warnings || []).forEach(function (code) {
        const line = document.createElement('div');
        line.className = _TIF_FATAL_WARNINGS.has(code)
            ? 'tif-info__warn tif-info__warn--fatal'
            : 'tif-info__warn';
        // 前缀写成完整字面量（不是模板串）：tests/test_i18n.py 的
        // _DYNAMIC_KEY_SITES 靠在源码里找到这个字面量来确认拼接点还活着。
        line.textContent = t('js.map.tifinfo.warn_' + code);
        box.appendChild(line);
    });
}

// index>0 时加 --sep 画分隔线。用修饰类而不是 CSS 的 `+` 兄弟组合符：
// tests/test_css_contract.py 的层叠模型不支持组合符（见 _btn_branch_applies）。
function _tifInfoFileBlock(file, index) {
    const box = document.createElement('div');
    box.className = index > 0 ? 'tif-info__file tif-info__file--sep' : 'tif-info__file';

    const head = document.createElement('div');
    head.className = 'tif-info__name';
    // 文件名必须自成一个元素：裸文本节点在 flex 容器里是匿名 flex item，
    // text-overflow 管不到它（那条属性只作用于块容器自己的行内内容），
    // 而且 min-width:auto 让它拒绝收缩，长名字会把 .tif-info__size 顶出卡片。
    const name = document.createElement('span');
    name.className = 'tif-info__filename';
    name.textContent = file.name;
    const size = document.createElement('span');
    size.className = 'tif-info__size';
    size.textContent = _fmtBytes(file.size);
    head.append(name, size);
    box.appendChild(head);

    const grid = document.createElement('div');
    grid.className = 'detail-grid';
    if (file.width && file.height) {
        _tifInfoRow(grid, t('js.map.tifinfo.dimensions'),
            `${file.width} × ${file.height} px`);
    }
    _tifInfoRow(grid, t('js.map.tifinfo.resolution'), _fmtResolution(file));
    if (file.dtype) {
        const parts = [file.dtype, t('js.map.tifinfo.bands', { n: file.bands || 1 })];
        if (file.nodata !== null && file.nodata !== undefined) {
            parts.push(`NoData ${_sig(file.nodata)}`);
        }
        _tifInfoRow(grid, t('js.map.tifinfo.data'), parts.join(' · '));
    }
    if (file.elevation) {
        _tifInfoRow(grid, t('js.map.tifinfo.elevation'),
            `${_sig(file.elevation.min, 6)} ~ ${_sig(file.elevation.max, 6)} m`);
    }
    if (file.recommended_maxzoom !== null && file.recommended_maxzoom !== undefined) {
        _tifInfoRow(grid, t('js.map.tifinfo.recommended_maxzoom'),
            String(file.recommended_maxzoom));
    }
    if (file.epsg) {
        _tifInfoRow(grid, t('js.map.tifinfo.crs'),
            file.crs_name ? `EPSG:${file.epsg} · ${file.crs_name}` : `EPSG:${file.epsg}`,
            true);
    }
    if (file.bounds_wgs84) {
        _tifInfoRow(grid, t('js.map.tifinfo.bounds'), _fmtLonLat(file.bounds_wgs84), true);
    } else if (file.bounds_native) {
        // 换算不出经纬度时报原生坐标，并说清楚它不是经纬度
        _tifInfoRow(grid, t('js.map.tifinfo.bounds_native'),
            file.bounds_native.map((v) => _sig(v, 9)).join(', '), true);
    }
    box.appendChild(grid);

    _tifInfoWarnings(box, file.warnings);
    return box;
}

// --- 起切前的规模预告 -----------------------------------------------------------
//
// 「自动」挡下用户不再控制层级：一份 5 m DEM 会自动算到 z16、精细档 z17，
// 1°×1° 就是约 71 万张、约 6 GB。这一行只预告、不拦。
//
// 三个数都是估算：
//   体积 —— 单张均值 8.4 KB，取自 docs/reference/terrain/tiling-presets-measured.md
//           9.3 节的三档实测值（速度 10.4 / 均衡 8.8 / 精细 8.4 KB）里**最小**的
//           那个。说清楚方向：对一条规模警告而言，最小值是**最不保守**的选择 ——
//           它偏低报（速度档按 10.4 算才对得上）。之所以仍然站得住：那三个数是
//           三档各自整座金字塔的均值，档越深均值越小，而任一档的累加张数里约
//           3/4 都落在最深一层（每加一级 x/y 各翻倍），最深层的单张又比该档均值
//           更小，量级上兜得住；
//   法线 —— 开启后 +35%~+100% 字节（同文档第五节），取下沿 1.4；
//   起点 —— 8，随包底图可用时 dem_task_tiler 恒传的 min_level。
// ⚠️ 预告只算这个任务**自己**要切的瓦片，**不含**起切时 graft 进来的随包底图
//    （z0-z7）：同盘是硬链接、几乎不占字节，跨盘则是 224 MB / 43,690 个文件的
//    真实拷贝 —— 那一份磁盘占用在这行数字里一个字节都没有。
// ⚠️ 档位偏移**不在这里抄第二份**：它由服务端渲染进 <option data-offset>，
//    取值表只有 geo_validation.TILING_QUALITY_OFFSETS 一份。
const TERRAIN_TILE_BYTES = 8.4 * 1024;
const TERRAIN_NORMALS_FACTOR = 1.4;
const TERRAIN_MIN_LEVEL = 8;

// 最近一次 /api/raster/inspect 的汇总，只在高程管线下缓存（等高线走 Web
// Mercator，后端压根不给它 tile_counts）。档位/层级/法线变了要重算预告，而那
// 几个事件不该再打一次服务端 —— 层级与 bounds 都已经在手上。
let _terrainInspectSummary = null;

// 缓存 + 立刻重画。文件被清空、探测失败时传 null：预告要跟着收起，否则上一份
// DEM 的张数会挂在一个空的选择框旁边（同 resetForm 收起两张信息卡的理由）。
function cacheTerrainInspectSummary(summary) {
    _terrainInspectSummary = summary;
    renderTerrainTileEstimate();
}

function renderTerrainTileEstimate() {
    const box = document.getElementById('localTerrainEstimate');
    if (!box) return;

    // 预告只对「上传 DEM」这条线成立：它算的全部是缓存里那批**上传文件**的层级
    // 表。数据来源切到已下载的 DEM 任务时，initProcessTypeToggle 的 apply() 只藏
    // 得掉上传行，而这一行在上传行之外（模板里它挨着档位下拉），留在原地就会拿
    // 上一批上传文件的层级/张数/体积去讲一个毫不相干的任务 —— 在这里给一个自信
    // 的错数比没有预告更糟。缓存不清：切回上传时那份汇总仍然对得上，不必再打一
    // 次 /api/raster/inspect。
    // ⚠️ 因此 dem_task 这条线**没有**预告，这不是漏了：/api/raster/inspect 的原料
    //    是前端读本地文件头得来的，服务端够不着任务目录里的那些 DEM，无从起算。
    if (document.getElementById('processSource')?.value !== 'upload') {
        box.hidden = true;
        box.textContent = '';
        return;
    }

    const summary = _terrainInspectSummary;
    const autoEl = document.getElementById('localTerrainMaxzoomAuto');
    const numEl = document.getElementById('localTerrainMaxzoom');
    const qualityEl = document.getElementById('localTerrainQuality');

    // 基准层级的来源随「自动」开关换：勾着就用后端按源像素估的那个，没勾就用
    // 数字框里的数（此时它不是 disabled）。
    let base;
    if (autoEl?.checked) {
        base = summary?.recommended_maxzoom;
    } else if (numEl && numEl.value !== '') {
        // 空的数字框不是 z0 —— Number('') === 0，不挡的话会预告成「基准 z0」。
        // 截断到整数：数字框收得下 14.5，而后端起切前自己就是 int(max_level)
        // （cesium_terrain.py）。不截的话标题写「实际 z13.5」，下面那个张数却
        // 是 counts[13.5] 落空按 0 算之后 z8..z13 的和 —— 两个数出自不同的层级。
        // Number('') 那道门仍在上面：空串照旧不显示，Math.trunc(NaN) 也还是 NaN。
        base = Math.trunc(Number(numEl.value));
    }
    const counts = summary?.tile_counts;
    // 跨 180° 的 DEM **现在也预告**。
    //
    // 这里曾经整段隐藏，理由是 raster_probe._tile_counts_per_level 那张表在跨界
    // 数据上少算约六成（intersecting_tile_range 把超出 180 的整段钳掉）。那个
    // 理由已经不成立：该函数改为按 RegionSpec.antimeridian_parts 分东西两段各数
    // 一遍再求和，跨界数据的张数是对的（见它 docstring 的第 1 条）。
    //
    // 残留的偏差换了来源（同 docstring 第 2 条）：切片器喂给几何的是
    // DemSampler.bounds，没有做 +360 展开，所以「预告」与「实切」在跨界数据上
    // 仍可能不一致。方向是**我们偏高报**（我们数了完整的两段，它可能只切被钳过
    // 的那一侧）——而这一行的用途是规模警告，高报是保守方向。继续整段隐藏的代价
    // 是跨界 DEM 在起切前拿不到任何规模提示，而那种作业动辄几十万张、几个小时。
    if (!Number.isFinite(base) || !Array.isArray(counts)) {
        box.hidden = true;
        box.textContent = '';
        return;
    }

    // 偏移只从服务端渲染的 data-offset 读。取不到（没有下拉/属性缺失）按 0 算，
    // 也就是基准档 —— 宁可少报一级，不要在这里塞一份猜的取值表。
    const opt = qualityEl?.selectedOptions?.[0];
    const offset = Number(opt?.dataset.offset) || 0;
    // 「自动」挡下的实际层级**必须**直接取服务端给的
    // recommended_maxzoom_by_quality[档位]，不能自己 base + offset：
    // 那个 base 已经被后端钳到 MAX_ZOOM 了（亚分米源的原始估算 22 会被钳成 21），
    // 再叠一次「精细 +1 / 速度 -1」就会在钳位边界上错一级 —— 预告写 z20 而切片器
    // 真的切 z21，张数差约 4 倍。服务端那张表里的每个值都是 build_terrain
    // **实际会切到**的层级，钳位已经算在里面。
    //
    // 手填挡仍走 base + offset：那个 base 是用户自己填在数字框里的数，我们从不
    // 钳它，所以在这一侧做加法是对的。钳位上界取 counts.length - 1 而不是另写
    // 一个 21：那张表的长度就是 MAX_ZOOM + 1。
    let level;
    if (autoEl?.checked) {
        const byQuality = summary?.recommended_maxzoom_by_quality;
        const quality = qualityEl?.value;
        // hasOwnProperty 同 getStatusColor 的理由：裸下标下 quality ===
        // 'constructor' 会取到构造函数，Number.isFinite 才兜得住。
        // 表缺失时（后端估不出层级就不给这个字段）**不回退**到 base + offset ——
        // 那正是上面说的错一级。宁可不预告，也不给一个自信的错数。
        level = (byQuality && quality
            && Object.prototype.hasOwnProperty.call(byQuality, quality))
            ? byQuality[quality]
            : undefined;
    } else {
        level = Math.max(0, Math.min(counts.length - 1, base + offset));
    }
    if (!Number.isFinite(level)) {
        box.hidden = true;
        box.textContent = '';
        return;
    }

    // 起点也要跟着钳下来，与 build_terrain 的 min_level = min(min_level, max_level)
    // 同一条：基准 8 配「比基准少一级」的档实际切到 z7，起点死守 8 的话循环一轮
    // 都不进，预告会写成「约 0 张」。counts 是逐层数、不累加，区间在这里自己累。
    let tiles = 0;
    for (let z = Math.min(TERRAIN_MIN_LEVEL, level); z <= level; z++) tiles += counts[z] || 0;

    const normalsEl = document.getElementById('localTerrainNormals');
    const bytes = tiles * TERRAIN_TILE_BYTES * (normalsEl?.checked ? TERRAIN_NORMALS_FACTOR : 1);

    box.hidden = false;
    box.textContent = t('js.map.terrain.estimate', {
        base: String(base),
        level: String(level),
        tiles: tiles.toLocaleString('zh-CN'),
        size: _fmtBytes(bytes),
    });
    box.title = t('js.map.terrain.estimate_hint');
}

function _tifInfoSummaryBlock(summary) {
    const box = document.createElement('div');
    box.className = 'tif-info__summary';

    const head = document.createElement('div');
    head.className = 'tif-info__name';
    head.textContent = t('js.map.tifinfo.summary', { n: summary.count });
    const size = document.createElement('span');
    size.className = 'tif-info__size';
    size.textContent = _fmtBytes(summary.total_size);
    head.appendChild(size);
    box.appendChild(head);

    const grid = document.createElement('div');
    grid.className = 'detail-grid';
    if (summary.pixel_meters) {
        _tifInfoRow(grid, t('js.map.tifinfo.finest_resolution'),
            `${_sig(summary.pixel_meters, 3)} m`);
    }
    if (summary.recommended_maxzoom !== null && summary.recommended_maxzoom !== undefined) {
        _tifInfoRow(grid, t('js.map.tifinfo.recommended_maxzoom'),
            String(summary.recommended_maxzoom));
    }
    if (summary.bounds_wgs84) {
        _tifInfoRow(grid, t('js.map.tifinfo.merged_bounds'),
            _fmtLonLat(summary.bounds_wgs84), true);
    }
    box.appendChild(grid);

    _tifInfoWarnings(box, summary.warnings);
    return box;
}

function _tifInfoMessage(el, text, fatal) {
    el.textContent = '';
    el.classList.remove('tif-info--scroll');
    const line = document.createElement('div');
    line.className = fatal ? 'tif-info__warn tif-info__warn--fatal' : 'tif-info__msg';
    line.textContent = text;
    el.appendChild(line);
    el.hidden = false;
}

// 读头部 -> 后端解释 -> 填卡片。inputId 是 file input，cardId 是信息卡，
// mode 见本节头注释（'terrain' | 'contour'）。
async function updateTifInfo(inputId, cardId, mode) {
    const el = document.getElementById(cardId);
    if (!el) return;
    const files = document.getElementById(inputId)?.files;
    const seq = (_tifInfoSeq.get(cardId) || 0) + 1;
    _tifInfoSeq.set(cardId, seq);
    const stale = () => _tifInfoSeq.get(cardId) !== seq;
    _tifInfoAbort.get(cardId)?.abort();          // 上一次的请求已经没人要了

    if (!files || files.length === 0) {
        el.hidden = true;
        el.textContent = '';
        el.classList.remove('tif-info--scroll');
        // 文件被清空（含 resetForm 的重跑）时预告也要收起：留着的话，上一个任务
        // 那份 DEM 的张数与体积会挂在一个空的选择框旁边。
        if (mode === 'terrain') cacheTerrainInspectSummary(null);
        return;
    }

    _tifInfoMessage(el, t('js.map.tifinfo.reading'), false);

    const entries = [];
    for (const file of files) {
        try {
            entries.push(await window.GeoTiffMeta.read(file));
        } catch (err) {
            // 读不出头部不是致命错：其余文件照常解释，这一个带
            // header_unreadable 出现在结果里（后端按缺字段判定）。
            console.warn('[tif-info] header read failed:', file.name, err);
            entries.push({ name: file.name, size: file.size });
        }
    }
    if (stale()) return;

    let data;
    const ctrl = new AbortController();
    _tifInfoAbort.set(cardId, ctrl);
    try {
        const resp = await fetch('/api/raster/inspect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ files: entries, mode: mode }),
            signal: ctrl.signal,
        });
        const payload = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(payload.error || resp.status);
        data = payload;
    } catch (err) {
        // 自己取消的不是失败：只有新的一次选择会取消它，那次会自己重画卡片。
        // 报出来的话用户每改选一次文件都要先看见一条红字。
        if (err && err.name === 'AbortError') return;
        if (stale()) return;
        _tifInfoMessage(el, t('js.map.tifinfo.failed', { error: err.message }), true);
        // 这次探测没拿到任何东西，上一份汇总也不再对应现在选中的文件。
        if (mode === 'terrain') cacheTerrainInspectSummary(null);
        return;
    }
    if (stale()) return;

    el.textContent = '';
    // 多文件才封高（见 style.css 的 .tif-info--scroll）
    el.classList.toggle('tif-info--scroll', (data.files || []).length > 1);
    (data.files || []).forEach(function (file, index) {
        el.appendChild(_tifInfoFileBlock(file, index));
    });
    if (data.summary && data.summary.count > 1) {
        el.appendChild(_tifInfoSummaryBlock(data.summary));
    }
    el.hidden = false;

    // 汇总里有起切前预告要的全部原料（tile_counts / recommended_maxzoom /
    // bounds_wgs84）。只在高程管线下缓存：等高线那次探测的汇总没有 tile_counts，
    // 存进来只会把高程这份冲掉，预告静默消失。
    if (mode === 'terrain') cacheTerrainInspectSummary(data.summary || null);
}

function updateLocalTerrainTifInfo() {
    return updateTifInfo('localTerrainFiles', 'localTerrainTifInfo', 'terrain');
}

function updateContourTifInfo() {
    return updateTifInfo('contourFiles', 'contourTifInfo', 'contour');
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

// 任务行「处理」按钮的入口（task_list.js 的 processDem）：把已完成的高程
// 下载任务转成地形切片任务。不另起提交链路 —— 打开处理弹窗并预选
// 「本地高程切片 + 该任务」，档位/法线/层级都可调，「创建」走
// submitLocalTerrain 生成新的 local_terrain 任务进时间流。
async function openProcessForDemTask(demTaskId) {
    const typeEl = document.getElementById('processType');
    const sourceEl = document.getElementById('processSource');
    const modalEl = document.getElementById('processModal');
    if (!typeEl || !sourceEl || !modalEl || typeof bootstrap === 'undefined') return;
    typeEl.value = 'local_terrain';
    sourceEl.value = 'dem_task';
    // 字段可见性由 initProcessTypeToggle 的 change 监听驱动，直接改 .value
    // 不触发，必须补发事件。
    typeEl.dispatchEvent(new Event('change'));
    sourceEl.dispatchEvent(new Event('change'));
    // source 的 change 监听里那次 loadProcessDemTasks() 不带 await；这里自己
    // 再等一次（幂等重填），确保下拉开出选项后才能选中目标任务。
    await loadProcessDemTasks();
    const sel = document.getElementById('processDemTask');
    if (sel && !sel.disabled) sel.value = String(demTaskId);
    refreshSubmitButtonState();
    bootstrap.Modal.getOrCreateInstance(modalEl).show();
    setTimeout(function () {
        const nameEl = document.getElementById('processTaskName');
        if (nameEl) nameEl.focus();
    }, 350);
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
        // form.reset() 已经清空两个文件选择框，两张信息卡必须跟着收起，
        // 否则下一次打开弹窗还挂着上一个任务那份 tif 的范围和层级。
        updateLocalTerrainTifInfo();
        updateContourTifInfo();
        // 复选框被 form.reset() 拨回了默认值，但 reset 不触发 change：
        // 层级数字框的禁用态得在这里跟上，否则自动挡勾着、数字框却能填。
        syncLocalTerrainMaxzoomDisabled();
    }

    refreshSubmitButtonState();
}

/**
 * 渲染框选后的四至（#boundsInfo，地图右上角的 .bounds-overlay 浮层），
 * 并同步状态栏的选区摘要（#statusSelection 胶囊里的 #statusSelectionText）。
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
    // 同 #statusCoordsText：写文字 span，别写胶囊本身（会抹掉图标 SVG）。
    const statusSel = document.getElementById('statusSelectionText');
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
        // 导入区域先说「这不是一个矩形」：多边形数 / 孔洞数 / 跨反经线。
        // 放在四至之前而不是之后 —— 下面那四个数是**外接矩形**的四至，用户在
        // 不知道有洞、有多段的前提下读它们，会以为选中的就是那个矩形。
        // display_name 来自文件内容（用户可控），必须过 escapeHtml：整层是
        // innerHTML 拼的（模板被契约测试钉住，不能改成逐节点 textContent）。
        const regionRow = _regionSpec ? `
            <div class="bounds-region">
                <span class="bounds-region__name">${escapeHtml(_regionSpec.display_name || t('js.region.unnamed'))}</span>
                <span class="bounds-region__facts">${escapeHtml(_regionFactsText(_regionSpec))}</span>
            </div>` : '';
        boundsInfo.innerHTML = `
            ${regionRow}
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
            // 宽高恒正的依据只有一条：currentBounds 的每个写入点都过了
            // validateBoundsRules。改前跨反经线选区在这里读出「-340.000°」
            // 并当成正常读数显示 —— 状态栏不做二次防御，闸门破了这里就错。
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
// 提交经 _applyBoundsEdit → validateBoundsRules（与后端同一套四条规则），
// 非法输入回退原值并 toast。

function _beginBoundsEdit(vEl) {
    if (!currentBounds || vEl.querySelector('input')) return;
    // 导入区域的四至是几何的**派生读数**（外接矩形），不是可编辑的真相。
    // 让它可编辑就会造出一个 bbox 与多边形不一致的选区：画在地图上的还是原来
    // 那个多边形，服务端按几何算张数，而用户以为自己刚刚缩小了下载范围。
    // 说一句而不是静默无反应 —— 读数上挂着「点击可编辑」的 title，
    // 点了什么都不发生只会让人以为界面坏了。
    if (_regionSpec) {
        showToast(t('js.region.bbox_readonly'), 'info');
        return;
    }
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
        // updateBoundsInfo() 开头那道 M15 守卫靠「浮层里还有
        // .bounds-edit-input」判断「正处于编辑态,不要重写整层」。而本函数建的
        // input 用的正是这个类 —— 留着它的话,下面无论走哪条路,那次重渲染都会
        // 被守卫拦掉:
        //   - Escape 取消:回不到原读数,格子里永远停着一个 input;
        //   - 校验失败(_applyBoundsEdit 里两条 return 前的 updateBoundsInfo):
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
    const reason = validateBoundsRules(b);
    if (reason) {
        showNotification(t(reason), 'warning');
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

// 非数字先各自 toast 并把焦点送回那一格（面板留在原地就地改），四至齐了再过
// validateBoundsRules —— 与框选落定、点读数编辑同一道闸门，不另立一套：
// 同一个选区被三个入口用三种标准放行，正是这次要拆掉的缺陷。
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
    const reason = validateBoundsRules(num);
    if (reason) {
        showNotification(t(reason), 'warning');
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
    // await 而不是直接调 updateTileEstimate()：多边形区域的张数来自服务端，
    // 第一次点提交时那次往返可能还在飞，同步读会拿到 null 而把整条确认静默
    // 跳过 —— 用户会在毫无预告的情况下建出一个几小时的任务。
    const est = await currentTileEstimate();
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
            east: currentBounds.east,
            west: currentBounds.west,
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
            east: currentBounds.east,
            west: currentBounds.west,
            zoom_min: parseInt(document.getElementById('zoomMin').value),
            zoom_max: parseInt(document.getElementById('zoomMax').value),
            style: document.getElementById('mapStyle').value,
            output_format: outputFormat,
            output_path: document.getElementById('outputPath').value,
            // MBTiles 是**正交**的一项，不是第四个 output_format 值（§5.3：它是
            // 通用产物容器）。勾上时后端在任务成功收尾后额外打一个 .mbtiles，
            // 松散瓦片目录照常保留 —— 预览与之后的手动导出都从它出。
            export_mbtiles: document.getElementById('exportMbtiles')?.checked ? 1 : 0
        };
        apiUrl = '/api/tasks';
    }

    // 导入的多边形区域：bbox 四至照常送（后端老路径与历史列表都要它），
    // 再附上完整几何。服务端按几何裁瓦片 / 裁 DEM 颗粒 —— 只送 bbox 就会把
    // L 形省份外面那一半也下下来，而那正是导入多边形要避免的事。
    //
    // **两条分支都要挂**。曾经这一行写在地图分支里面，于是导入同一条 L 形
    // 省界建 DEM 任务时后端只看得见四至，颗粒数与它的外接矩形一个不差 ——
    // DemTaskManager.create_task 里那段 `if not region.is_rectangle` 的按真实
    // 几何过滤（src/services/dem_task_manager.py）从界面上根本走不到。
    // 矩形选区不带这个字段，两条路都走原路径，行为一个字节都不变。
    if (_regionSpec) taskData.region = _regionSpec;

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

// --- 地形预览的进场薄雾 -------------------------------------------------------
// 换 terrainProvider 是一次**没有中间态**的整球几何重建：赋值那一帧地面就从
// 椭球（或上一份地形）跳成新地形，随后还要当着用户的面逐级细化。影像层有
// alpha 可以淡入，地形没有——全球只有一个 provider，也没法让两份共存。
// 所以拿一层雾把这一段盖过去：换之前起雾，视野瓦片落地之后散雾。
// 等待信号与首屏 splash 同款（postRender 轮询 tilesLoaded + 上限兜底）。
const _VEIL_MAX_WAIT_MS = 3000;
// 淡出与元素移除之间的等待，必须 ≥ CSS 里 .map-transition-veil 的过渡时长
// （0.35s），否则元素在淡出走完之前就被摘掉，看到的还是硬切。
const _VEIL_FADE_OUT_MS = 400;
let _veilRemoveTimer = null;

function _showMapVeil() {
    const host = document.querySelector('.index-map');
    if (!host) return null;                 // 只有首页有地图容器
    let veil = document.getElementById('mapTransitionVeil');
    if (!veil) {
        veil = document.createElement('div');
        veil.id = 'mapTransitionVeil';
        veil.className = 'map-transition-veil';
        host.appendChild(veil);
    }
    if (_veilRemoveTimer) {                 // 上一次的移除计时器要作废
        clearTimeout(_veilRemoveTimer);
        _veilRemoveTimer = null;
    }
    // 下一帧才加类：同一帧里「插入 DOM + 加类」浏览器不会跑过渡，雾会瞬间出现。
    requestAnimationFrame(function () { veil.classList.add('map-transition-veil--in'); });
    return veil;
}

function _hideMapVeil() {
    const veil = document.getElementById('mapTransitionVeil');
    if (!veil) return;
    veil.classList.remove('map-transition-veil--in');
    if (_veilRemoveTimer) clearTimeout(_veilRemoveTimer);
    _veilRemoveTimer = setTimeout(function () {
        _veilRemoveTimer = null;
        veil.remove();
    }, _VEIL_FADE_OUT_MS);
}

// 视野瓦片落地即散雾；上限一到无论齐不齐都散。
// 上限不是保险丝而是常态路径：flyTo 飞行期间相机一直在动，tilesLoaded 长时间
// 为 false，只等它就等于把用户关在雾里。
function _hideMapVeilWhenTilesSettle(maxWaitMs) {
    if (!viewer) { _hideMapVeil(); return; }
    let settled = false;
    function finish() {
        if (settled) return;
        settled = true;
        // 监听器必须显式摘：不摘的话每预览一次就多挂一个，永久累积在
        // postRender 上（每帧都跑）。
        viewer.scene.postRender.removeEventListener(onFrame);
        _hideMapVeil();
    }
    function onFrame() {
        if (!viewer.scene.globe.tilesLoaded) return;
        finish();
    }
    viewer.scene.postRender.addEventListener(onFrame);
    setTimeout(finish, maxWaitMs || _VEIL_MAX_WAIT_MS);
}

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
    // 只有换了地形 provider 的那条路会起雾（影像叠加没有几何跳变，罩一层雾
    // 只是白闪一下）。散雾的责任跟着这个标记走。
    let veiled = false;
    try {
        const taskType = task.task_type;
        if (taskType === 'map' || taskType === 'contour') {
            // 预览瓦片与底图共用页面级瓦片 origin（initTileOrigin() 已定）。
            const base = taskType === 'map'
                ? tileUrl(`/tiles/${task.id}`)
                : tileUrl(`/contour/${task.id}`);
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
            // base 已经是解析过的绝对地址：下面的 layer.json / hillshade 元数据
            // 请求和 CesiumTerrainProvider.fromUrl 都直接拿它拼，不再二次解析。
            const base = taskType === 'local_terrain'
                ? tileUrl(`/terrain/local/${task.id}`)
                : tileUrl(`/terrain/dem/${task.id}`);
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
                // 起雾必须在赋值之前：赋值那一帧几何就跳了，雾晚一步等于给
                // 一次硬切加了个尾巴。散雾交给 tilesLoaded（下面 flyTo 之后）。
                _showMapVeil();
                veiled = true;
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
                    // hs.url 是后端给的内部绝对路径（/terrain/{dem,local}/<id>/hillshade.png，
                    // 见 routes/terrain_static.py），落在瓦片网关的 /terrain/ 前缀里。
                    // 直接塞原值的话，前面的元数据请求都走了瓦片 origin、唯独这张
                    // 用户真正看得见的图绕回主端口 —— 整条隔离链路只差最后一跳失效。
                    const layer = viewer.imageryLayers.addImageryProvider(
                        new Cesium.SingleTileImageryProvider({
                            url: tileUrl(hs.url),
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
        // 排在 flyTo 之后：雾要盖到「相机停稳 + 那一片瓦片落地」为止。
        if (veiled) _hideMapVeilWhenTilesSettle();
        _renderPreviewChip();
        updateContourPreviewButtons();
    } catch (err) {
        // 雾必须撤：起雾之后抛出的话（换完 provider 再出错），地图会一直蒙着，
        // 而散雾的那条路已经不会走到了。这里立刻撤，不等瓦片。
        if (veiled) _hideMapVeil();
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
    // 缺口徽章：带洞的成品在**每一处**露面都要带上这个数字（行、详情、这里）。
    // 预览是最需要它的一处 —— 用户正盯着屏幕上的图判断「这份数据能不能用」，
    // 而屏幕上看不出缺的是哪几块（缺块渲染出来就是空白，与海面、与未加载
    // 的瓦片长得一样）。
    // 数字从 store 现取而不是在 _previewState 里存一份：socket 推送只写 store，
    // 存一份快照会在补漏跑完后继续显示旧的缺口数。
    const previewKey = `${_previewState.taskType}:${_previewState.taskId}`;
    const previewTaskRow = window.TaskStore
        ? (window.TaskStore.get(previewKey) || window.TaskStore.getActive(previewKey))
        : null;
    const previewGaps = (previewTaskRow && previewTaskRow.gap_tiles) || 0;
    const gapBadge = previewGaps > 0
        ? `<span class="task-gap-chip" title="${escapeHtml(t('js.gaps.chip_title', { n: previewGaps }))}">${escapeHtml(t('js.gaps.chip', { n: previewGaps }))}</span>`
        : '';
    chip.innerHTML = `
        <span>${t('js.map.preview.chip', {
            name: '<strong>' + escapeHtml(_previewState.name) + '</strong>',
            id: _previewState.taskId,
        })}</span>
        ${gapBadge}
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
    // 两种来源：上传 GeoTIFF，或零拷贝复用某个已完成 DEM 任务已下载的 .tif
    // （任务行「处理」按钮最终也落到这张表单）。两条路殊途同归 —— 都在
    // /api/terrain/local/tasks 建一个新的 local_terrain 任务进时间流。
    // 早前 dem_task 分支复用 DEM 任务自己的切片作业（不新建任务、进度只能在
    // 详情弹窗里看），与「转出一个新任务」的预期相反，已改。
    const fromDemTask = (document.getElementById('processSource')?.value || 'upload') === 'dem_task';
    const demTaskId = _selectedProcessDemTaskId();
    const fileInput = document.getElementById('localTerrainFiles');
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

    const fd = new FormData();
    fd.append('name', document.getElementById('processTaskName').value
        || t('js.map.process.local_terrain_default_name'));
    // 三个字段的兜底一律是空串，不是前端自己抄一份默认值：空串 = 未传 = 走配置
    // 默认（后端 local_terrain_api 的 create_local_terrain_task 把空串当未传）。
    // 写死 '14' / 'balanced'
    // 会在控件缺席或被清空时用前端的默认盖掉运维配的 terrain_local_maxzoom /
    // terrain_quality_preset。
    // 勾了「自动」就送字面量 'auto'，不是数字框里那个陈旧的数 —— 数字框在自动挡
    // 下是 disabled 的，它的 value 只是用户取消勾选后的起点。
    const maxzoomAutoEl = document.getElementById('localTerrainMaxzoomAuto');
    const maxzoomEl = document.getElementById('localTerrainMaxzoom');
    fd.append('maxzoom', maxzoomAutoEl?.checked ? 'auto' : (maxzoomEl?.value || ''));
    fd.append('quality', document.getElementById('localTerrainQuality')?.value || '');
    // ⚠️ 法线必须送 checked 状态。checkbox 的 .value 恒为 'on'（与勾没勾无关），
    // 把它或 checkbox 本身丢进 FormData 送出去的都是 'on'，而后端
    // coerce_vertex_normals 是严格白名单，'on' 一律 400。控件不在时送空串走
    // 配置默认，不要送 'undefined'。
    const normalsEl = document.getElementById('localTerrainNormals');
    fd.append('vertex_normals', normalsEl ? String(normalsEl.checked) : '');
    if (fromDemTask) {
        fd.append('dem_task_id', demTaskId);
    } else {
        for (const f of files) {
            fd.append('files', f);
        }
    }

    const btn = document.getElementById('createProcessBtn');
    btn.disabled = true;
    const original = btn.innerHTML;
    // dem_task 分支一个字节都不上传，按钮文案不能写「上传中...」。
    btn.innerHTML = t(fromDemTask ? 'js.map.process.submitting' : 'js.map.process.uploading');
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
        showNotification(fromDemTask
            ? t('js.map.process.terrain_started_dem_task', { id: demTaskId })
            : t('js.map.process.upload_started', { id: result.task_id }), 'success');
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

/**
 * 工作台行为：状态栏读数（鼠标经纬度 / 缩放级别 / 选区摘要 / 日期时钟；
 * 坐标与选区四至支持点击复制）、bounds 浮层交互（下载按钮、数值点击
 * 编辑）。在 initMap 之后由页面 init 块调用（index.html）。
 */
function initMapWorkbench() {
    if (!viewer) return;

    // 读数写在胶囊里的文字 span 上，不是胶囊本身 —— 胶囊第一个子节点是图标
    // SVG，往胶囊写 textContent 会把图标一起抹掉（首帧还在，一刷新就没了）。
    const coordsEl = document.getElementById('statusCoords');
    const coordsTextEl = document.getElementById('statusCoordsText');
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
        coordsTextEl.textContent = t('js.map.status.coords', {
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
    const clockEl = document.getElementById('statusClockText');
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
