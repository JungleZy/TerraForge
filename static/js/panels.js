/**
 * 工作台覆盖面板：历史记录 / 配置 以右侧滑出面板的形式浮在地图上方，
 * 不再整页跳转（单窗口 GIS 工作台形态，与 ArcGIS Online / Felt 同模式）。
 *
 * 顶部工具栏已移除，入口是首页地图左上角的 .map-panel-btn 浮动按钮
 * （index.html）。任何带 data-panel="records|history|config" 的元素都会被拦截
 * 改为打开面板；独立页（/history、/config）没有面板元素，链接保持正常
 * 跳转，行为与之前完全一致。
 *
 * 全局暴露：window.openPanel(name) / window.closePanel()，
 * name ∈ {records, history, config}。「记录」面板合并了活动任务与历史：
 * records 是新名字，history 作为别名保留（#history hash 与旧入口兼容）。
 * 支持 #records / #history / #config hash 直达（resetConfig 刷新后重开配置面板）。
 */
(function () {
    'use strict';

    // records/history 指向同一个面板元素；懒初始化标记按**元素 id** 记，
    // 免得 openPanel('records') 之后 openPanel('history') 又初始化一遍。
    var PANELS = { history: 'historyPanel', records: 'historyPanel', config: 'configPanel' };
    var inited = {};
    var current = null;
    // 面板打开前那个焦点元素（通常是触发它的 .map-panel-btn），关闭时焦点还给它。
    var restoreFocus = null;

    // 焦点环用的候选集。`[tabindex="-1"]` 排除在外：面板自身就带 -1，
    // 它只供程序化聚焦，不该出现在 Tab 序里。
    var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]),'
        + ' select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

    function panelEl(name) { return document.getElementById(PANELS[name]); }
    function backdrop() { return document.getElementById('panelBackdrop'); }

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
        backdrop().hidden = false;
        el.hidden = false;
        requestAnimationFrame(function () {
            backdrop().classList.add('panel-backdrop--in');
            el.classList.add('workbench-panel--in');
            // 焦点收进面板：面板行为上是模态（遮罩 + Esc 关闭），焦点留在外面
            // 等于读屏与键盘用户还站在被遮罩盖住的那半个界面上。落点选关闭钮
            // 而不是第一个表单控件 —— 用户至少能立刻退出去（ui.js 的自定义
            // confirm 是同一套：挂载后 focus 到默认按钮）。
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
        backdrop().classList.remove('panel-backdrop--in');
        el.classList.remove('workbench-panel--in');
        // done 是延迟回调（transitionend 或 350ms 兜底），触发时面板可能已经被
        // 重新打开或切换成另一个：closePanel(true) 之后 openPanel 会把共享
        // backdrop 和（同一个）面板重新显示，没有守卫的话这里会把新面板的
        // 遮罩甚至面板本体撤掉 —— 表现为「遮罩闪一下就消失」。
        // 所以：backdrop 只在确实没有任何面板打开时才藏；面板元素只在它
        // 没有重新成为当前面板时才藏。守卫按**元素引用**比较而不是名字：
        // records/history 是同一个元素（PANELS 别名），按名字比较会在
        // openPanel('records') → openPanel('history') 切换时把刚重开的
        // 共享面板又藏起来。
        var done = function () {
            if (panelEl(current) !== el) el.hidden = true;
            if (!current) backdrop().hidden = true;
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

    // 可聚焦且**当前可见**的后代。offsetParent 为空即被 hidden/display:none
    // 收起（配置面板里有一大票这种控件），把它们算进环里会出现「Tab 一下焦点
    // 消失」。
    function focusables(el) {
        return [].slice.call(el.querySelectorAll(FOCUSABLE)).filter(function (n) {
            return n.offsetParent !== null;
        });
    }

    function onKey(e) {
        if (e.key === 'Escape') { closePanel(); return; }
        if (e.key !== 'Tab' || !current) return;
        // 自定义确认框开着时让位：它的浮层是运行时 append 到 document.body 的，
        // 不在面板子树里，焦点环会把焦点从「确定/取消」上抢回面板。
        // 这也是这里做焦点环、而不是给面板之外的兄弟节点批量上 inert 的原因 ——
        // 那会让从面板里弹出的 confirm / toast 一起变成不可交互。
        if (document.querySelector('.app-confirm-overlay')) return;
        var el = panelEl(current);
        if (!el) return;
        var list = focusables(el);
        if (!list.length) { e.preventDefault(); try { el.focus(); } catch (err) {} return; }
        var first = list[0];
        var last = list[list.length - 1];
        if (!el.contains(document.activeElement)) {
            // 焦点已经溜到面板外（点了遮罩、或上一次 Tab 漏出去）：拉回来
            e.preventDefault();
            (e.shiftKey ? last : first).focus();
        } else if (!e.shiftKey && document.activeElement === last) {
            e.preventDefault();
            first.focus();
        } else if (e.shiftKey && document.activeElement === first) {
            e.preventDefault();
            last.focus();
        }
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
        var bd = backdrop();
        if (bd) bd.addEventListener('click', function () { closePanel(); });
        document.querySelectorAll('[data-panel-close]').forEach(function (b) {
            b.addEventListener('click', function () { closePanel(); });
        });
        // hash 直达：URL 已经是目标 hash，不要再 push 一条重复条目
        var h = location.hash.replace('#', '');
        if (PANELS[h]) openPanel(h, false);
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
