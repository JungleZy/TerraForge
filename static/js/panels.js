/**
 * 工作台覆盖面板：历史记录 / 配置 以右侧滑出面板的形式浮在地图上方，
 * 不再整页跳转（单窗口 GIS 工作台形态，与 ArcGIS Online / Felt 同模式）。
 *
 * 2026-08-11 起**非模态化**：遮罩层已取消 —— 面板打开时地图保持可见可交互，
 * 面板不再自报 aria-modal，Tab 不再设焦点环；Esc 关闭保留（对 confirm /
 * Bootstrap 弹窗的让位判据不变）。
 *
 * 顶部工具栏已移除，入口是首页地图左上角的 .map-panel-btn 浮动按钮
 * （index.html）。任何带 data-panel="records|history|config|plugins" 的元素都会被
 * 拦截改为打开面板；独立页（/history、/config）没有面板元素，链接保持正常
 * 跳转，行为与之前完全一致。
 *
 * 全局暴露：window.openPanel(name) / window.closePanel()，
 * name ∈ {records, history, config, plugins}。「记录」面板合并了活动任务与历史：
 * records 是新名字，history 作为别名保留（#history hash 与旧入口兼容）。
 * 支持 #records / #history / #config / #plugins hash 直达（resetConfig 刷新后重开配置面板）。
 */
(function () {
    'use strict';

    // records/history 指向同一个面板元素；懒初始化标记按**元素 id** 记，
    // 免得 openPanel('records') 之后 openPanel('history') 又初始化一遍。
    var PANELS = {
        history: 'historyPanel', records: 'historyPanel',
        config: 'configPanel', plugins: 'pluginsPanel'
    };
    var inited = {};
    var current = null;
    // 面板打开前那个焦点元素（通常是触发它的 .map-panel-btn），关闭时焦点还给它。
    var restoreFocus = null;

    function panelEl(name) { return document.getElementById(PANELS[name]); }

    function openPanel(name, syncUrl) {
        var el = panelEl(name);
        if (!el) return;                    // 非首页：无面板，链接正常跳转
        if (current === name) return;
        // 必须赶在 closePanel 之前取：面板互切时 closePanel 可能把焦点交还出去，
        // 之后再读 activeElement 拿到的就是上一个面板的触发钮了。
        var opener = document.activeElement;
        closePanel(true);
        restoreFocus = (opener && opener !== document.body && typeof opener.focus === 'function')
            ? opener : null;
        current = name;
        el.hidden = false;
        requestAnimationFrame(function () {
            el.classList.add('workbench-panel--in');
            // 焦点收进面板：落点选关闭钮而不是第一个表单控件 —— 用户至少能
            // 立刻退出去（ui.js 的自定义 confirm 是同一套：挂载后 focus 到
            // 默认按钮）。非模态化后这层依然成立:打开即给键盘用户一个明确落点。
            var closeBtn = el.querySelector('[data-panel-close]');
            try { (closeBtn || el).focus(); } catch (e) { /* 元素可能已被移除 */ }
        });
        document.addEventListener('keydown', onKey);

        // 触发按钮高亮：面板打开时点亮对应的入口（含别名，如 records/history）
        document.querySelectorAll('[data-panel]').forEach(function (b) {
            b.classList.toggle('map-panel-btn--active',
                PANELS[b.getAttribute('data-panel')] === PANELS[name]);
        });

        // 懒初始化：面板可见后才建 Cesium 地图（hidden 容器初始化会得到 0 尺寸）
        var key = PANELS[name];
        if (!inited[key]) {
            inited[key] = true;
            if (key === 'historyPanel' && typeof initHistory === 'function') initHistory();
            if (key === 'configPanel' && typeof initConfig === 'function') initConfig();
            if (key === 'pluginsPanel' && typeof initPlugins === 'function') initPlugins();
        } else if (key === 'historyPanel' && typeof historyViewer !== 'undefined' && historyViewer) {
            setTimeout(function () {
                historyViewer.resize();
                // 小地图开了 requestRenderMode，resize 后显式请求一帧保证重开后画面刷新
                historyViewer.scene.requestRender();
            }, 250);
            // 重开必须重拉时间流 + 统计:懒初始化只在首次 open 跑一遍,之后
            // 时间流停在旧内容上 —— 新建的 pending 任务没有 socket 进度事件
            // 可触发 prependStreamRow,不重拉就要等整页刷新才看得见。
            // loadHistory/loadStats/currentPage 都是 history.js 的全局。
            if (typeof loadHistory === 'function') {
                loadHistory(typeof currentPage !== 'undefined' ? currentPage : 1);
            }
            if (typeof loadStats === 'function') loadStats();
        }

        // hash 已经是本面板时不再 pushState。前进/后退回到 `#name` 会触发
        // hashchange -> openPanel，再 push 一条同 hash 的历史条目就会：①堆出
        // 一串重复条目（用户要连按好几次后退才出得去）②销毁前进栈。
        if (syncUrl !== false && window.history && history.pushState
            && location.hash !== '#' + name) {
            history.pushState(null, '', '#' + name);
        }
    }

    function closePanel(silent) {
        if (!current) return;
        var el = panelEl(current);
        el.classList.remove('workbench-panel--in');
        // done 是延迟回调（transitionend 或 350ms 兜底），触发时面板可能已经被
        // 重新打开或切换成另一个：没有守卫的话这里会把刚重开面板的本体撤掉。
        // 守卫按**元素引用**比较而不是名字：records/history 是同一个元素
        // （PANELS 别名），按名字比较会在 openPanel('records') → openPanel('history')
        // 切换时把刚重开的共享面板又藏起来。
        var done = function () {
            if (panelEl(current) !== el) el.hidden = true;
        };
        el.addEventListener('transitionend', done, { once: true });
        setTimeout(done, 350);              // transitionend 不触发的兜底
        document.removeEventListener('keydown', onKey);
        // 焦点归还：面板马上要 hidden，焦点若还落在它子树里，浏览器会把焦点
        // 甩回 <body>，键盘用户得从头 Tab 一遍。判据是「焦点确实在面板里」而
        // 不是无条件还原 —— 面板互切时焦点在刚点的那颗触发钮上（面板之外），
        // 无条件还原会把它抢走再塞进新面板，白跳一次。
        if (el.contains(document.activeElement) && restoreFocus) {
            try { restoreFocus.focus(); } catch (e) { /* 触发钮可能已不在文档里 */ }
        }
        restoreFocus = null;
        document.querySelectorAll('[data-panel]').forEach(function (b) {
            b.classList.remove('map-panel-btn--active');
        });
        current = null;
        if (!silent && window.history && history.replaceState) {
            history.replaceState(null, '', location.pathname);
        }
    }

    // 面板之上还盖着一层浮层（自定义确认框 / Bootstrap 弹窗）时，Esc 必须让位。
    //
    // 让位判据必须排在 Escape 分支【上面】：从面板里开出来的弹窗按一次 Esc，
    // Bootstrap 在目标阶段先 hide 弹窗、事件继续冒泡到 document，判据若在
    // 下面，这里会把身后的面板一起关掉（实测：开配置面板 → 点「浏览」开
    // #pathBrowserModal → 一次 Esc，modal 与 configPanel 同时消失、hash 被
    // replaceState 抹掉、焦点掉回 body）。自定义 confirm 之所以一直没出事，
    // 靠的是它自己那侧 —— ui.js 用 capture 阶段注册并 stopImmediatePropagation，
    // 这里根本收不到那个 Esc；不做这个动作的浮层（Bootstrap 就不做）全部暴露。
    //
    // 判据用 body.modal-open 而不是 .modal.show：Bootstrap 的 hide() 是**同步**
    // 摘掉 .show 再排队做过渡收尾的（vendored 5.3.0：`this._element.classList
    // .remove(Li)` 紧接 `_queueCallback(()=>this._hideModal())`），等事件冒泡到
    // document 时 .modal.show 已经不匹配了 —— 实测用它当判据这里照样会把面板
    // 关掉。modal-open 相反：show() 第一步就 `document.body.classList.add(ki)`，
    // 直到过渡结束的 _hideModal() 才摘，正好覆盖「弹窗开着或正在关」整段。
    //
    // 2026-08-11 非模态化：Tab 焦点环随遮罩层一起移除 —— 面板打开时地图保持
    // 可见可交互，再把 Tab 关进环里等于把键盘用户困在一个视觉上没有被隔离的
    // 区域。
    function onKey(e) {
        if (document.querySelector('.app-confirm-overlay')) return;
        if (document.body.classList.contains('modal-open')) return;
        if (e.key === 'Escape') { closePanel(); }
    }

    // ---- 面板调宽(2026-08-11 设计 §3.4,借鉴 GeoLibre)--------------------
    // 左缘 8px 热区;拖拽中只写 CSS 变量(rAF 节流),松手写 localStorage。
    // 窄屏(<768px)面板是全屏覆盖,调宽无意义,整个不启用。
    var RESIZE_CONFIGS = [
        { id: 'historyPanel', varName: '--panel-tasks-w', key: 'tf-panel-w-tasks', min: 560, max: 1100 },
        { id: 'configPanel', varName: '--panel-config-w', key: 'tf-panel-w-config', min: 320, max: 640 }
    ];

    function clampWidth(v, min, max) { return Math.min(max, Math.max(min, v)); }

    function applyPanelWidth(cfg, px) {
        var el = document.getElementById(cfg.id);
        if (el) el.style.setProperty(cfg.varName, px + 'px');
    }

    function initResizers() {
        if (!window.matchMedia('(min-width: 768px)').matches) return;
        RESIZE_CONFIGS.forEach(function (cfg) {
            var el = document.getElementById(cfg.id);
            var handle = el && el.querySelector('[data-panel-resizer]');
            if (!handle) return;
            var stored = NaN;
            try { stored = parseFloat(window.localStorage.getItem(cfg.key)); } catch (e) {}
            if (!isNaN(stored)) applyPanelWidth(cfg, clampWidth(stored, cfg.min, cfg.max));

            handle.addEventListener('pointerdown', function (e) {
                e.preventDefault();
                handle.setPointerCapture(e.pointerId);
                handle.classList.add('workbench-panel__resizer--active');
                var startX = e.clientX;
                var startW = el.getBoundingClientRect().width;
                var raf = 0;
                function widthAt(clientX) {
                    // 面板钉在视口右缘:指针往左 = 变宽。
                    return clampWidth(startW + (startX - clientX), cfg.min, cfg.max);
                }
                function onMove(ev) {
                    if (raf) return;
                    raf = requestAnimationFrame(function () {
                        raf = 0;
                        applyPanelWidth(cfg, widthAt(ev.clientX));
                    });
                }
                function onUp(ev) {
                    handle.removeEventListener('pointermove', onMove);
                    handle.removeEventListener('pointerup', onUp);
                    handle.removeEventListener('pointercancel', onUp);
                    if (raf) { cancelAnimationFrame(raf); raf = 0; }
                    handle.classList.remove('workbench-panel__resizer--active');
                    var w = widthAt(ev.clientX);
                    applyPanelWidth(cfg, w);
                    try { window.localStorage.setItem(cfg.key, String(w)); } catch (e2) {}
                }
                handle.addEventListener('pointermove', onMove);
                handle.addEventListener('pointerup', onUp);
                handle.addEventListener('pointercancel', onUp);
            });
        });
    }

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
        document.querySelectorAll('[data-panel-close]').forEach(function (b) {
            b.addEventListener('click', function () { closePanel(); });
        });
        // hash 直达：URL 已经是目标 hash，不要再 push 一条重复条目
        var h = location.hash.replace('#', '');
        if (PANELS[h]) openPanel(h, false);
        initResizers();
    });

    // 同文档 hash 变化（前进/后退、地址栏改 hash）：openPanel/closePanel 内部
    // 用的是 pushState/replaceState，不会触发本事件，不会成环。
    //
    // 但**必须**传 syncUrl=false：后退/前进到 `#name` 时 URL 已经是目标值，
    // 再 push 一条就是「堆重复条目 + 销毁前进栈」。closePanel(true) 同理 ——
    // 空 hash 是浏览器导航的结果，不该再 replaceState 一次。
    window.addEventListener('hashchange', function () {
        var h = location.hash.replace('#', '');
        if (PANELS[h]) openPanel(h, false);
        else closePanel(true);
    });

    window.openPanel = openPanel;
    window.closePanel = closePanel;
})();
