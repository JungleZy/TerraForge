/**
 * 工作台覆盖面板：新建任务 / 任务记录 / 配置 / 插件 以右侧滑出面板的形式浮在
 * 地图上方，不再整页跳转（单窗口 GIS 工作台形态，与 ArcGIS Online / Felt 同模式）。
 *
 * 2026-08-11 起**非模态化**：遮罩层已取消 —— 面板打开时地图保持可见可交互，
 * 面板不再自报 aria-modal，Tab 不再设焦点环；Esc 关闭保留（对 confirm /
 * Bootstrap 弹窗的让位判据不变）。
 *
 * 2026-08-15 起 create 也在这里：四条管线（瓦片/高程/本地地形切片/等高线）的
 * 两个 Bootstrap 弹窗合成一个 #createPanel，与任务/配置同构。它是**非模态**的
 * 关键收益 —— 填表时地图仍可拖，改选区不必关掉任何东西。
 *
 * 顶部工具栏已移除，入口是首页地图左上角的 .map-panel-btn 浮动按钮
 * （index.html）。任何带 data-panel="create|records|history|config|plugins" 的元素
 * 都会被拦截改为**开关**面板（同名再点关闭，见 togglePanel）；独立页
 * （/history、/config）没有面板元素，链接保持正常跳转，行为与之前完全一致。
 *
 * 2026-08-15 起本文件还是**全站层栈的唯一持有者**：`window.TerraLayers =
 * { register, closeTop, topName }`，以及整站**唯一**那个「按 Esc 关最上层」的
 * keydown 监听（见下面的层栈一节）。因此它由 base.html 全站加载，而不再只挂在
 * index.html —— /config 与 /history 上没有面板元素（本文件在那两页对面板部分
 * 完全空载），但 confirm / progress / cmdk / cmdk help 四层照样在，它们得有地方
 * 报到。加载顺序有硬要求：**必须排在 command_palette.js 之前**（它在解析期就
 * register），tests/test_layer_stack.py 钉住了这条。
 *
 * 全局暴露：window.openPanel(name) / window.closePanel()，
 * name ∈ {create, records, history, config, plugins}。openPanel 是**幂等的打开**
 * （已开着就只把焦点收回面板），关闭语义只在 togglePanel 里 —— 程序化入口
 * （map.js 的 openCreatePanel、_afterTaskCreated）调的是 openPanel。
 * 「记录」面板合并了活动任务与历史：records 是新名字，history 作为别名保留
 * （#history hash 与旧入口兼容）。
 * 支持 #create / #records / #history / #config / #plugins hash 直达
 * （resetConfig 刷新后重开配置面板）。
 */
(function () {
    'use strict';

    // records/history 指向同一个面板元素；懒初始化标记按**元素 id** 记，
    // 免得 openPanel('records') 之后 openPanel('history') 又初始化一遍。
    var PANELS = {
        create: 'createPanel',
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
        // 已经开着：**不重开**。重开会走一遍 closePanel + rAF 滑入，视觉上闪一下，
        // 还会再 pushState 一条同 hash 的历史条目。但也不是无声早退 —— 焦点收回
        // 面板：openCreatePanel 这类程序化入口可以在面板已开时再次被调用（选区
        // 浮层、命令面板、任务行深链都指向同一个面板），那时用户的意图是「回到
        // 这张表」。「同名再点关闭」不在这里做，它是 togglePanel 的事 ——
        // openPanel 必须是幂等的「打开」，否则程序化入口会把面板关掉。
        if (current === name) {
            try { el.focus(); } catch (e) { /* 明确忽略：元素可能已被移除 */ }
            return;
        }
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
            try { (closeBtn || el).focus(); } catch (e) { /* 明确忽略：元素可能已被移除 */ }
        });

        // 触发按钮高亮 + aria-expanded：面板打开时点亮对应的入口（含别名，如
        // records/history）。两者必须同步翻 —— .map-panel-btn--active 只是
        // color/border 的差别，读屏用户听不出哪个面板正开着；aria-expanded 是
        // 「这颗按钮控制的东西现在展开了没有」的唯一可听表述，而这三颗按钮
        // 2026-08-15 起是真 toggle，再点一次就关。
        document.querySelectorAll('[data-panel]').forEach(function (b) {
            var on = PANELS[b.getAttribute('data-panel')] === PANELS[name];
            b.classList.toggle('map-panel-btn--active', on);
            b.setAttribute('aria-expanded', on ? 'true' : 'false');
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
        // 焦点归还：面板马上要 hidden，焦点若还落在它子树里，浏览器会把焦点
        // 甩回 <body>，键盘用户得从头 Tab 一遍。判据是「焦点确实在面板里」而
        // 不是无条件还原 —— 面板互切时焦点在刚点的那颗触发钮上（面板之外），
        // 无条件还原会把它抢走再塞进新面板，白跳一次。
        if (el.contains(document.activeElement) && restoreFocus) {
            try { restoreFocus.focus(); } catch (e) { /* 明确忽略：触发钮可能已不在文档里 */ }
        }
        restoreFocus = null;
        document.querySelectorAll('[data-panel]').forEach(function (b) {
            b.classList.remove('map-panel-btn--active');
            b.setAttribute('aria-expanded', 'false');
        });
        current = null;
        if (!silent && window.history && history.replaceState) {
            history.replaceState(null, '', location.pathname);
        }
    }

    // ---- 层栈：全站唯一的「关最上层」------------------------------------
    //
    // 改前「按 Esc 关最上层」被独立实现了三份，事件相位各不相同：命令面板走
    // document capture、ui.js 的两个自绘对话框走 capture + stopImmediatePropagation、
    // 本文件走 bubble 并靠 `body.modal-open` / `.app-confirm-overlay` 两道 DOM
    // 查询给别人让位。三者的先后不是设计出来的，是「谁挂在哪个相位、谁先注册」
    // 碰出来的：加一层就得回头去改另外两份的让位判据，而漏改的表现是「一次 Esc
    // 关掉两层」（实测过：配置面板里开 #pathBrowserModal，一次 Esc 两层全没）。
    // 现在整站只有本文件这一个 keydown 监听，谁在最上层由这张表说了算。
    //
    // 相位仍是 **bubble**（不是 capture）。capture 会抢在元素自己的 keydown
    // 之前，而站内有几处输入框把 Esc 当作「收起我自己那个下拉」（map.js 的地名
    // 搜索按下 Esc 后 stopPropagation，本表就收不到 —— 这正是要的）。改成
    // capture 等于把那些局部 Esc 全部截胡。
    //
    // 栈序 = 注册顺序，topLayer() 从后往前找第一个 isOpen()。静态层在解析期按
    // z 令牌由低到高注册（面板 --z-panel → cmdk --z-cmdk → 拖拽遮罩
    // --z-drop-veil）；confirm / progress 是每次打开现注册、节点移出文档时注销，
    // 于是**后召唤的总在最上面** —— 这正是「最上层」的直觉：刚弹出来挡在眼前的
    // 那一个。
    //
    // **刻意不进层栈**的显隐机制 —— 它们是局部状态，从不参与「谁是最上层」之争，
    // 收进来只会把一次 Esc 的语义搅浑：原生 `<details>`、Vue 的 `v-if`、CSS 的
    // `attr(data-hint)` 气泡、Cesium 自带的 infoBox、以及字段级的裸 `hidden`
    // 翻转。Bootstrap 弹窗（#pathBrowserModal / 历史详情）也不进：它自带 Esc
    // 关闭，本表只需整体让位，判据见 onKey。
    var layers = [];

    // register(name, { isOpen, close, accept?, dismissible?, reason? }) -> unregister
    //   isOpen()     这一层现在在不在屏幕上（栈顶判定的唯一依据）
    //   close()      关掉它。dismissible 为 false 时不需要给 —— 一颗只能骗人的
    //                关闭入口比不给更糟
    //   accept()     可选。最上层时 Enter 的默认动作（只有 confirm 有）
    //   dismissible  默认 true。false = Esc 关不掉
    //   reason       关不掉时说给用户听的那句话
    function register(name, spec) {
        var layer = {
            name: name,
            isOpen: spec.isOpen,
            close: spec.close,
            accept: spec.accept || null,
            dismissible: spec.dismissible !== false,
            reason: spec.reason || ''
        };
        layers.push(layer);
        return function unregister() {
            var i = layers.indexOf(layer);
            if (i !== -1) layers.splice(i, 1);
        };
    }

    function topLayer() {
        for (var i = layers.length - 1; i >= 0; i--) {
            if (layers[i].isOpen()) return layers[i];
        }
        return null;
    }

    function topName() {
        var top = topLayer();
        return top ? top.name : null;
    }

    // 关掉最上层。返回它的名字；栈里没有开着的层返回 null；最上层声明了
    // 不可关闭返回 false（调用方据此给反馈，别静默）。
    function closeTop() {
        var top = topLayer();
        if (!top) return null;
        if (!top.dismissible) return false;
        top.close();
        return top.name;
    }

    var lastRefusalAt = 0;

    function explainRefusal(layer) {
        if (!layer.reason || typeof window.showToast !== 'function') return;
        // 连按 Esc 不该在右上角堆出一列一模一样的提示：节流窗与一条 toast 的
        // 寿命（3.5s）对齐。
        var now = Date.now();
        if (now - lastRefusalAt < 3500) return;
        lastRefusalAt = now;
        window.showToast(layer.reason, 'info');
    }

    function onKey(e) {
        if (e.key !== 'Escape' && e.key !== 'Enter') return;
        // Bootstrap 弹窗不在层栈里：它在目标阶段自己 hide，事件继续冒到
        // document。让位判据必须排在下面的分派【之前】—— 排在后面的话，从配置
        // 面板里点「浏览」开出 #pathBrowserModal 再按一次 Esc，这里会把身后的
        // 面板一起关掉（实测：modalOpen ["pathBrowserModal"]→[]、panelOpen
        // ["configPanel"]→[]，hash 被 replaceState 抹掉、焦点掉回 body）。
        //
        // 判据用 body.modal-open 而不是 .modal.show：Bootstrap 5.3.0 的 hide()
        // 是**同步**摘掉 .show（vendored:`this._element.classList.remove(Li)`）
        // 再排队做过渡收尾的，等事件冒到 document 时 .modal.show 已经不匹配
        // —— 实测拿它当判据面板照样被关掉。modal-open 相反：show() 第一步就
        // `document.body.classList.add(ki)`，直到过渡结束的 _hideModal() 才摘，
        // 正好覆盖「弹窗开着或正在关」整段。
        if (document.body.classList.contains('modal-open')) return;
        var top = topLayer();
        if (!top) return;
        // 回车只归**自报了默认动作**的层（目前只有 confirm）。面板里的表单回车、
        // cmdk 里的选中回车各有各的接线，层栈不许把它们截胡。
        if (e.key === 'Enter' && !top.accept) return;
        // 到这里这一发键归最上层独占：既不许穿到身后的层，也不许触发页面默认行为。
        e.preventDefault();
        e.stopPropagation();
        // 自动重复一律吞掉。按住 Esc 的重复间隔约 30ms —— 不挡的话一次长按就会
        // 一路关穿整个栈；Enter 那侧更凶：两级确认会被自动按穿，而第二级问的是
        // 【另一个维度】的问题（默认答案 = 保留磁盘产物），替用户选中破坏性的
        // 那一边，直接发 ?delete_files=true 删掉瓦片/GeoTIFF/DEM。confirm 自己
        // 那道 300ms 死区挡的是真人快速双击，两道各挡一半，缺一不可。
        if (e.repeat) return;
        if (e.key === 'Enter') { top.accept(); return; }
        if (closeTop() === false) explainRefusal(top);
    }

    document.addEventListener('keydown', onKey);

    // 每个面板名各注册一层。它们共用同一个 `current` 槽（互斥，同时只可能开
    // 一个），分开注册是为了 topName() 报得出**哪一个**面板在最上层，而不是
    // 笼统的 'panel' —— records/history 是同一个元素的两个名字，也就各占一条。
    Object.keys(PANELS).forEach(function (name) {
        register(name, {
            isOpen: function () { return current === name; },
            close: function () { closePanel(); }
        });
    });

    // ---- 面板调宽(2026-08-11 设计 §3.4,借鉴 GeoLibre)--------------------
    // 左缘 8px 热区;拖拽中只写 CSS 变量(rAF 节流),松手写 localStorage。
    // 窄屏(<768px)面板是全屏覆盖,调宽无意义,整个不启用。
    // createPanel 借 --panel-config-w 而不是自己铸一个令牌：applyPanelWidth 把变量
    // **内联写在面板元素上**，所以两个面板各自的宽度互不影响，而缺省值仍是 :root
    // 里那个 480px。localStorage 的 key 各自独立（tf-panel-w-create），拖窄新建
    // 面板不会带走配置面板的宽度。下限比配置面板高一点（380 vs 320）：四枚管线
    // chip 排一行、下面又是 col-6 两列的缩放范围，320px 下会挤成两行半。
    var RESIZE_CONFIGS = [
        { id: 'createPanel', varName: '--panel-config-w', key: 'tf-panel-w-create', min: 380, max: 720 },
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
            try {
                stored = parseFloat(window.localStorage.getItem(cfg.key));
            } catch (e) { /* 明确忽略：读不出宽度就走默认（隐私模式等 localStorage 不可用） */ }
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
                    try {
                        window.localStorage.setItem(cfg.key, String(w));
                    } catch (e2) { /* 明确忽略：存不下只是下次开不到这个宽度，本次拖动已生效 */ }
                }
                handle.addEventListener('pointermove', onMove);
                handle.addEventListener('pointerup', onUp);
                handle.addEventListener('pointercancel', onUp);
            });
        });
    }

    // 工具条按钮是真 toggle（2026-08-15）：同名再点关闭。改前 openPanel 里有一句
    // `if (current === name) return;`，于是点开的按钮再点一次毫无反应 —— 一颗
    // 高亮着、看起来「按下」的按钮，唯一的关闭路径却是 Esc 或面板里的关闭钮。
    // 判据留在这里而不是 openPanel 里：程序化入口（openCreatePanel 等）调
    // openPanel 时要的是幂等的「打开」，把关闭塞进去会让它们把面板关掉。
    function togglePanel(name) {
        if (current === name) {
            closePanel();
            return;
        }
        openPanel(name);
    }

    document.addEventListener('DOMContentLoaded', function () {
        // 任何带 data-panel 的元素（地图浮动按钮、状态栏任务胶囊等）：首页有面板
        // 就拦截改开/关面板；无面板元素（独立页）时不拦截，浏览器正常跳转。
        document.querySelectorAll('[data-panel]').forEach(function (a) {
            a.addEventListener('click', function (e) {
                var name = a.getAttribute('data-panel');
                if (panelEl(name)) {
                    e.preventDefault();
                    togglePanel(name);
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
    // 层栈的公开面：ui.js / command_palette.js / drop_process.js 用 register()
    // 报到，closeTop()/topName() 供程序化关闭与调试（层叠矩阵的验收就靠它读栈顶）。
    window.TerraLayers = { register: register, closeTop: closeTop, topName: topName };
})();
