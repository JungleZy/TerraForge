/**
 * 地形光照开关（Cesium `scene.globe.enableLighting`）。
 *
 * 契约（与 theme.js 同款，改之前先读）：
 * - 偏好只存 localStorage key `tf-terrain-lighting`（'1' 开 / 其它一律关），
 *   **不进 config 表** —— 它是纯客户端渲染偏好，切换不需要重切片，也不该
 *   影响别的客户端。
 * - 默认**关**。理由不是审美偏好，是实测的作用域：`enableLighting` 是
 *   **全球唯一开关**，不区分「有没有加载地形」。开着它而当前地形没有
 *   逐顶点法线时，vendored Cesium 1.143.0 会退到 `ENABLE_DAYNIGHT_SHADING`
 *   分支（Cesium.js 里 `enableLighting ? (hasVertexNormals ? push
 *   ENABLE_VERTEX_LIGHTING : push ENABLE_DAYNIGHT_SHADING)`），按太阳方位
 *   给整颗地球（含纯影像预览）刷一层明暗渐变 —— 也就是说它会改变**所有**
 *   预览的外观，不只是地形预览。Cesium 自己的默认值同样是 false
 *   （Globe 构造函数里 `this.enableLighting = false`）。
 * - 开关只改渲染参数，不碰 terrainProvider，所以随时可切、即时生效，
 *   跟当前预览的是哪个任务无关。
 *
 * 两个容易踩空的实现点（都已核实，不是照抄）：
 * 1. `viewer.scene.requestRenderMode = true`（map.js 里设的，同处还把
 *    `maximumRenderTimeChange` 设成 Infinity）=> 场景只在被显式请求时重绘。
 *    `Globe.enableLighting` 在 Cesium 里是**普通数据字段**（构造函数直接
 *    赋值，没有 setter 去 requestRender），改了它不会自己触发重绘 ——
 *    必须显式 `scene.requestRender()`，否则要等用户拖动地图才看得到变化。
 * 2. 按下态的类名是 `map-panel-btn--active`（style.css 里唯一给这族按钮
 *    上色的规则）。写成 Bootstrap 那个通用的 `.active` 不会报错、也不会
 *    有任何视觉效果 —— 按钮会变成「点了没反应」的哑开关。
 *
 * init() 幂等：重复调用只重新应用一次状态，click 监听有 _wired 守卫不叠挂。
 */
window.TerrainLighting = (function () {
    'use strict';

    var STORAGE_KEY = 'tf-terrain-lighting';
    var BUTTON_ID = 'mapTerrainLighting';
    var ACTIVE_CLASS = 'map-panel-btn--active';

    var _viewer = null;
    var _wired = false;

    function _button() {
        return document.getElementById(BUTTON_ID);
    }

    // 当前偏好。读不到（隐私模式等 localStorage 不可用）一律按默认关。
    function get() {
        try {
            return window.localStorage.getItem(STORAGE_KEY) === '1';
        } catch (e) {
            return false;
        }
    }

    // 把状态落到 Cesium 与按钮上。viewer 还没建好时只更新按钮，
    // 等 init(viewer) 再补上渲染侧。
    function apply(on) {
        if (_viewer && _viewer.scene && _viewer.scene.globe) {
            _viewer.scene.globe.enableLighting = on;
            _viewer.scene.requestRender();   // requestRenderMode=true，见文件头注释 1
        }
        var btn = _button();
        if (btn) {
            btn.setAttribute('aria-pressed', on ? 'true' : 'false');
            btn.classList.toggle(ACTIVE_CLASS, on);
        }
    }

    // 写偏好并立即应用。写不进也先把本次会话的状态应用上。
    function set(on) {
        on = !!on;
        try {
            window.localStorage.setItem(STORAGE_KEY, on ? '1' : '0');
        } catch (e) { /* 忽略 */ }
        apply(on);
    }

    function init(viewer) {
        _viewer = viewer || _viewer;
        apply(get());
        var btn = _button();
        if (btn && !_wired) {
            _wired = true;
            btn.addEventListener('click', function () {
                set(!get());
            });
        }
    }

    return { get: get, set: set, init: init };
})();
