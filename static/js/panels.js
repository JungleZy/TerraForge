/**
 * 工作台覆盖面板：历史记录 / 配置 以右侧滑出面板的形式浮在地图上方，
 * 不再整页跳转（单窗口 GIS 工作台形态，与 ArcGIS Online / Felt 同模式）。
 *
 * 顶部工具栏已移除，入口是首页地图左上角的 .map-panel-btn 浮动按钮
 * （index.html）。任何带 data-panel="history|config" 的元素都会被拦截
 * 改为打开面板；独立页（/history、/config）没有面板元素，链接保持正常
 * 跳转，行为与之前完全一致。
 *
 * 全局暴露：window.openPanel(name) / window.closePanel()，name ∈ {history, config}
 * 支持 #history / #config hash 直达（resetConfig 刷新后重开配置面板）。
 */
(function () {
    'use strict';

    var PANELS = { history: 'historyPanel', config: 'configPanel' };
    var inited = { history: false, config: false };
    var current = null;

    function panelEl(name) { return document.getElementById(PANELS[name]); }
    function backdrop() { return document.getElementById('panelBackdrop'); }

    function openPanel(name) {
        var el = panelEl(name);
        if (!el) return;                    // 非首页：无面板，链接正常跳转
        if (current === name) return;
        closePanel(true);
        current = name;
        backdrop().hidden = false;
        el.hidden = false;
        requestAnimationFrame(function () {
            backdrop().classList.add('panel-backdrop--in');
            el.classList.add('workbench-panel--in');
        });
        document.addEventListener('keydown', onKey);

        // 触发按钮高亮：面板打开时点亮对应的 .map-panel-btn
        document.querySelectorAll('[data-panel]').forEach(function (b) {
            b.classList.toggle('map-panel-btn--active', b.getAttribute('data-panel') === name);
        });

        // 懒初始化：面板可见后才建 Leaflet 地图（hidden 容器初始化会得到 0 尺寸）
        if (!inited[name]) {
            inited[name] = true;
            if (name === 'history' && typeof initHistory === 'function') initHistory();
            if (name === 'config' && typeof initConfig === 'function') initConfig();
        } else if (name === 'history' && typeof historyViewer !== 'undefined' && historyViewer) {
            setTimeout(function () { historyViewer.resize(); }, 250);
        }

        if (window.history && history.pushState) history.pushState(null, '', '#' + name);
    }

    function closePanel(silent) {
        if (!current) return;
        var el = panelEl(current);
        var closing = current;
        backdrop().classList.remove('panel-backdrop--in');
        el.classList.remove('workbench-panel--in');
        // done 是延迟回调（transitionend 或 350ms 兜底），触发时面板可能已经被
        // 重新打开或切换成另一个：closePanel(true) 之后 openPanel 会把共享
        // backdrop 和（同一个）面板重新显示，没有守卫的话这里会把新面板的
        // 遮罩甚至面板本体撤掉 —— 表现为「遮罩闪一下就消失」。
        // 所以：backdrop 只在确实没有任何面板打开时才藏；面板元素只在它
        // 没有重新成为当前面板时才藏。
        var done = function () {
            if (current !== closing) el.hidden = true;
            if (!current) backdrop().hidden = true;
        };
        el.addEventListener('transitionend', done, { once: true });
        setTimeout(done, 350);              // transitionend 不触发的兜底
        document.removeEventListener('keydown', onKey);
        document.querySelectorAll('[data-panel]').forEach(function (b) {
            b.classList.remove('map-panel-btn--active');
        });
        current = null;
        if (!silent && window.history && history.replaceState) {
            history.replaceState(null, '', location.pathname);
        }
    }

    function onKey(e) { if (e.key === 'Escape') closePanel(); }

    document.addEventListener('DOMContentLoaded', function () {
        // 任何带 data-panel 的元素（地图浮动按钮等）：首页有面板就拦截开面板；
        // 无面板元素（独立页）时不拦截，浏览器正常跳转。
        document.querySelectorAll('[data-panel]').forEach(function (a) {
            a.addEventListener('click', function (e) {
                var name = a.getAttribute('data-panel');
                if (panelEl(name)) {
                    e.preventDefault();
                    openPanel(name);
                }
            });
        });
        var bd = backdrop();
        if (bd) bd.addEventListener('click', function () { closePanel(); });
        document.querySelectorAll('[data-panel-close]').forEach(function (b) {
            b.addEventListener('click', function () { closePanel(); });
        });
        // hash 直达
        var h = location.hash.replace('#', '');
        if (PANELS[h]) openPanel(h);
    });

    // 同文档 hash 变化（前进/后退、地址栏改 hash）：openPanel/closePanel 内部
    // 用的是 pushState/replaceState，不会触发本事件，不会成环。
    window.addEventListener('hashchange', function () {
        var h = location.hash.replace('#', '');
        if (PANELS[h]) openPanel(h);
        else closePanel();
    });

    window.openPanel = openPanel;
    window.closePanel = closePanel;
})();
