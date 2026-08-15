/**
 * 全站主题机制：暗黑 / 明亮 / 跟随系统。
 *
 * 契约（团队统一约定，改之前先读）：
 * - `<html>` 的 data-bs-theme 属性是唯一开关，取值 dark / light。Bootstrap 5.3
 *   原生响应它；自定义组件全部走 style.css :root 里的 --color-* 令牌，
 *   亮色令牌由 style.css 的 `[data-bs-theme="light"]` 块覆盖同名变量。
 * - 偏好存 localStorage key `tf-theme`，值为 dark | light | system，缺省 dark。
 *   system = matchMedia('(prefers-color-scheme: light)')，并监听其 change
 *   事件实时跟随。
 * - base.html <head> 里另有一段**内联引导脚本**在首帧绘制前同步设置该属性
 *   （避免闪烁）；本文件是它之后的运行期实现：切换 UI（配置面板「外观」组，
 *   config.js 接线）调 TerraTheme.set，全站即时生效。
 * - <html> 标签上的 data-bs-theme="dark" 字面量是 SSR/无 JS 回退默认值，
 *   由 tests/test_css_contract.py 钉住，不要动。
 *
 * init() 幂等：在 DOMContentLoaded 之前或之后、重复调用都安全。
 */
window.TerraTheme = (function () {
    'use strict';

    var STORAGE_KEY = 'tf-theme';
    var MODES = ['dark', 'light', 'system'];
    var LIGHT_QUERY = '(prefers-color-scheme: light)';

    var ACCENT_KEY = 'tf-accent';
    var ACCENTS = ['sky', 'teal', 'violet', 'rose', 'orange'];

    var media = window.matchMedia(LIGHT_QUERY);
    var systemListener = null;

    // 当前偏好（dark | light | system）。非法值/读不到一律回退 dark。
    function get() {
        var mode = 'dark';
        try {
            mode = window.localStorage.getItem(STORAGE_KEY) || 'dark';
        } catch (e) { /* 明确忽略：隐私模式等 localStorage 不可用时按缺省 dark */ }
        return MODES.indexOf(mode) !== -1 ? mode : 'dark';
    }

    // 求值后的实际主题（dark | light）：system 按系统配色求值。
    function resolved() {
        var mode = get();
        if (mode === 'system') return media.matches ? 'light' : 'dark';
        return mode;
    }

    function applyToDom() {
        var before = document.documentElement.dataset.bsTheme;
        var now = resolved();
        document.documentElement.dataset.bsTheme = now;
        if (before === now) return;
        // U5：广播主题变更。有模块把 CSS 自定义属性的求值结果缓存了下来
        // （task_status.js 的 _statusStrokeCache：小地图矩形描边色，Cesium 要真实
        // 色值字符串、不认 var()），其前提是「调色板运行期不变」—— 主题切换
        // 落地后这个前提不再成立，没有失效钩子的话矩形会一直留着旧主题的颜色。
        try {
            document.dispatchEvent(new CustomEvent('terraforge:themechange', {
                detail: { theme: now }
            }));
        } catch (e) { /* 明确忽略：老浏览器没有 CustomEvent 构造器 */ }
    }

    function onSystemChange() {
        applyToDom();
    }

    function syncSystemListener() {
        if (get() === 'system' && !systemListener) {
            systemListener = onSystemChange;
            if (media.addEventListener) media.addEventListener('change', systemListener);
            else media.addListener(systemListener);   // 旧 WebKit 回退
        } else if (get() !== 'system' && systemListener) {
            if (media.removeEventListener) media.removeEventListener('change', systemListener);
            else media.removeListener(systemListener);
            systemListener = null;
        }
    }

    function apply() {
        applyToDom();
        syncSystemListener();
    }

    // 写偏好并立即应用。非法 mode 直接忽略。
    function set(mode) {
        if (MODES.indexOf(mode) === -1) return;
        try {
            window.localStorage.setItem(STORAGE_KEY, mode);
        } catch (e) { /* 明确忽略：写不进也先把本次会话的主题应用上 */ }
        apply();
    }

    // ---- 强调色预设(2026-08-11 设计 §3.5)---------------------------------
    // 与主题同一套机制:localStorage `tf-accent` + <html> data-accent。
    // sky 是缺省 —— 删属性而不是写 "sky",保持「缺省 = 零覆盖」。

    // 当前强调色偏好。非法值/读不到一律回退 sky。
    function getAccent() {
        var accent = 'sky';
        try {
            accent = window.localStorage.getItem(ACCENT_KEY) || 'sky';
        } catch (e) { /* 明确忽略：隐私模式等 localStorage 不可用时按缺省 sky */ }
        return ACCENTS.indexOf(accent) !== -1 ? accent : 'sky';
    }

    function applyAccentToDom() {
        var accent = getAccent();
        var el = document.documentElement;
        var before = el.getAttribute('data-accent') || 'sky';
        if (before === accent) return;
        if (accent === 'sky') el.removeAttribute('data-accent');
        else el.setAttribute('data-accent', accent);
        // 与主题切换广播同一个事件:task_status.js 的 _statusStrokeCache 这类
        // 「缓存了 CSS 变量求值结果」的模块无需区分是哪种换肤。
        try {
            document.dispatchEvent(new CustomEvent('terraforge:themechange', {
                detail: { theme: resolved(), accent: accent }
            }));
        } catch (e) { /* 明确忽略：老浏览器没有 CustomEvent 构造器 */ }
    }

    // 写强调色偏好并立即应用。非法值直接忽略。
    function setAccent(accent) {
        if (ACCENTS.indexOf(accent) === -1) return;
        try {
            window.localStorage.setItem(ACCENT_KEY, accent);
        } catch (e) { /* 明确忽略：写不进也先把本次会话的强调色应用上 */ }
        applyAccentToDom();
    }

    // 幂等：重复调用只是重新应用一次，监听器有 systemListener 守卫不会叠挂。
    function init() {
        apply();
        applyAccentToDom();
    }

    return { get: get, set: set, resolved: resolved, init: init,
             getAccent: getAccent, setAccent: setAccent, ACCENTS: ACCENTS };
})();
