/**
 * 全局 UI 组件：自定义 Toast 通知 + Confirm 模态框
 *
 * 替代浏览器原生 alert() / confirm()，与 premium 深色设计系统（GIS 蓝强调色）统一。
 * 全局暴露：
 *   window.showToast(message, type, opts)      —— 右上角通知，type: success|danger|warning|info
 *   window.showConfirm(message, opts) -> Promise<boolean>  —— 居中确认框
 *       opts.checkbox = {label, checked} 时改 resolve {confirmed, checked}
 *       opts.select = {label, options:[{value,label}], value} 时改 resolve {confirmed, selected}
 *   window.showProgressDialog(opts) -> {update, close}  —— 不可取消的模态进度框
 *   window.guard(triggerEl, asyncFn) -> Promise  —— 动作在飞时锁住触发钮（in-flight 守卫）
 *   window.trapTab(e, container)               —— Tab 焦点环：自报 aria-modal 的浮层共用
 *   window.showNotification(message, type)     —— showToast 的别名（兼容旧调用）
 *   window.parseTaskDate(value) -> Date|null   —— 任务时间字段统一解析（裸格式按 UTC）
 *   window.formatBytes(bytes, opts) -> string  —— 字节 → 人类可读（1024 进制 KiB/MiB，全站唯一一份）
 *       opts.suffix 追加单位后缀（速度用 '/s'），opts.roundAtHundred 让 ≥100 取整
 *   window.formatCoord(deg) -> string          —— 经纬度读数档（5 位，≈1.1 m）
 *   window.formatCoordExact(deg) -> string     —— 经纬度详情档（6 位，≈0.11 m）
 *   window.initTileOrigin(tilePort) -> Promise<boolean>  —— 页面级瓦片端口探测
 *   window.tileUrl(path) -> string            —— 已探测成功时改写内部绝对路径
 */
(function () {
    'use strict';

    // 内联 SVG 图标（stroke 风格，跟 navbar 图标一致，颜色继承自父元素 currentColor）
    const ICONS = {
        success: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>',
        danger: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
        warning: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
        info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>'
    };

    const VALID_TYPES = ['success', 'danger', 'warning', 'info'];

    // toast 总量上限：失败 toast 是常驻的（duration: 0 不自动消失），批量失败
    // 时右上角会无限堆高。超过上限时最旧的自动收起——走该 toast 自己的
    // remove，进出场动画与计时语义不变，常驻设计本身不变。
    const MAX_TOASTS = 10;
    const openToasts = [];

    // ---------------------------------------------------------------- Toast
    // 容器是一个**常驻的 live region**：aria-live 挂在容器上、toast 作为它的子
    // 节点插进来，读屏才会稳定播报。改前容器上什么都没有，靠每个 toast 节点
    // 自带 role="alert" —— 那条路径要求读屏把「刚插入的这个节点」当成警报来
    // 处理，各家实现并不一致（尤其节点是先 setAttribute 再 appendChild 的），
    // 而容器本身根本不是 live 区，漏播时前端这边完全无从察觉。
    // aria-atomic="false"：只播新插进来的那一条，不把右上角堆着的十条重念一遍。
    //
    // 「常驻」是播报的前提而不是修辞：live 区要先在无障碍树里落定，之后插进去
    // 的子节点才算「变化」被念出来。这一版之前容器是在**第一次** showToast 里
    // createElement + appendChild 的，和 toast 内容同一个同步块出现——读屏普遍
    // 不播，于是每次页面加载后的第一条提示（包括失败提示）对读屏用户是静默的：
    // 注释写着「常驻」，代码不是。现在创建/挂载移到模块初始化。
    const TOAST_CONTAINER_ID = 'app-toast-container';

    function createToastContainer() {
        const c = document.createElement('div');
        c.id = TOAST_CONTAINER_ID;
        c.setAttribute('aria-live', 'polite');
        c.setAttribute('aria-atomic', 'false');
        document.body.appendChild(c);
        return c;
    }

    // 取现成节点；建一次是**兜底不是常态**——正常路径下容器在下面的初始化里
    // 就挂好了。兜底分支保留，因为 showToast 可能被更早的内联脚本调用（那时
    // 本模块的初始化还没跑到），或者容器被别的代码摘掉。这两种情况下退回
    // 「容器与内容同 tick、可能不播」也强过让提示整个消失。
    function ensureToastContainer() {
        return document.getElementById(TOAST_CONTAINER_ID) || createToastContainer();
    }

    // 立即挂载，不等 DOMContentLoaded：base.html 把 ui.js 的 <script> 放在
    // <body> 内（#taskDetailModal、命令面板之后、panels.js 之前），执行到这里
    // document.body 必然已存在；改等 DOMContentLoaded 只会让这之前的 showToast
    // 全部落回「同 tick」老路。
    //
    // 三个分支而不是两个，且**一律不许抛**：本模块的初始化一旦抛，整个 IIFE
    // 中断，window.showToast / guard / formatBytes 全都不存在 —— 比漏播严重
    // 得多。第二支防脚本被挪进 <head>（body 还没有）；第三支防 document 根本
    // 不是真 DOM：tests/test_tile_origin_runtime.py 把 ui.js 整份内联进 node、
    // 只给 `global.document = {}`（它测的是 initTileOrigin/tileUrl，与 DOM 无关），
    // 那里既没有 body 也没有 addEventListener。裸调会 TypeError，11 个用例连
    // 断言都跑不到就退出 1 —— 这条路实际发生过。落到第三支时容器由
    // ensureToastContainer() 懒建（常驻性丢掉，但那已经不是浏览器环境）。
    if (document.body) {
        createToastContainer();
    } else if (typeof document.addEventListener === 'function') {
        document.addEventListener('DOMContentLoaded', createToastContainer, { once: true });
    }

    function showToast(message, type, opts) {
        // 降级本身保留（一个拼错的 type 不该让提示整个消失），但必须出声：
        // 静默降级把 map.js 传的 'error' 显示成蓝色 ⓘ，复制失败读起来像成功，
        // 而控制台里没有任何痕迹可查。type 省略（undefined）是合法用法，不警告。
        if (type != null && VALID_TYPES.indexOf(type) === -1) {
            console.warn('[showToast] 未知 type ' + JSON.stringify(type) +
                '，已降级为 info；有效值：' + VALID_TYPES.join(' / '));
        }
        type = VALID_TYPES.indexOf(type) !== -1 ? type : 'info';
        opts = opts || {};
        const duration = opts.duration != null ? opts.duration : 3500;

        const container = ensureToastContainer();

        const toast = document.createElement('div');
        toast.className = 'app-toast app-toast--' + type;
        // 不再逐条设 role="alert"：容器已是 aria-live 区，两者叠在一起会让
        // 同一条提示被念两遍（alert 是 assertive，还会打断用户正在听的内容）。

        const icon = document.createElement('span');
        icon.className = 'app-toast__icon';
        icon.innerHTML = ICONS[type] || ICONS.info;

        const msg = document.createElement('span');
        msg.className = 'app-toast__msg';
        msg.textContent = message == null ? '' : String(message); // textContent 防 XSS

        const close = document.createElement('button');
        close.className = 'app-toast__close';
        close.setAttribute('aria-label', t('js.ui.toast.close'));
        close.innerHTML = '&times;';

        toast.appendChild(icon);
        toast.appendChild(msg);
        toast.appendChild(close);
        container.appendChild(toast);

        // 下一帧加 --in 触发进入过渡
        requestAnimationFrame(function () {
            toast.classList.add('app-toast--in');
        });

        let removed = false;
        let timer = null;
        // 先占位再回填 close：remove 闭包里要按引用把自己从 openToasts 摘掉
        const handle = { close: null };
        function remove() {
            if (removed) return;
            removed = true;
            const i = openToasts.indexOf(handle);
            if (i !== -1) openToasts.splice(i, 1);
            if (timer) clearTimeout(timer);
            toast.classList.remove('app-toast--in');
            toast.classList.add('app-toast--out');
            toast.addEventListener('transitionend', function () { toast.remove(); }, { once: true });
            setTimeout(function () { toast.remove(); }, 400); // 兜底
        }
        handle.close = remove;

        close.addEventListener('click', remove);
        if (duration > 0) timer = setTimeout(remove, duration);

        openToasts.push(handle);
        // 超上限：最旧的先收（remove 会把它自己从数组里摘掉，循环因此推进）
        while (openToasts.length > MAX_TOASTS) {
            openToasts[0].close();
        }

        return handle;
    }

    // ------------------------------------------------------------ Tab 焦点环
    // 自报 `aria-modal="true"` 就是向读屏承诺「遮罩之外的一切已经冻结」。承诺了
    // 却不拦 Tab，键盘/读屏用户会一路 Tab 到身后那半个看不见的界面上去 —— 那比
    // 不声明 aria-modal 更糟：读屏按承诺把外面的内容从虚拟缓冲里摘掉了，用户
    // 却把焦点送了进去，落点在读屏眼里根本不存在。
    //
    // 这份实现原本只长在 command_palette.js 里（cmdk 与速查表两个 dialog 共用），
    // 而 ui.js 这两个自绘对话框是「声明了 aria-modal、零拦截」。提到这里共用，
    // 四个浮层同一套语义。
    const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]),'
        + ' select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

    function trapTab(e, container) {
        const list = [].slice.call(container.querySelectorAll(FOCUSABLE)).filter(function (n) {
            return n.offsetParent !== null;
        });
        // 一个可聚焦控件都没有（进度框就是这样）：Tab 原地不动。承诺了封闭，
        // 就不许把焦点交到外面去。
        if (!list.length) { e.preventDefault(); return; }
        const first = list[0];
        const last = list[list.length - 1];
        if (!container.contains(document.activeElement)) {
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

    // -------------------------------------------------------------- Confirm
    function showConfirm(message, opts) {
        opts = opts || {};
        // t() 必须在调用期求值（语种取自 window.__I18N__），所以留在函数体里
        const title = opts.title != null ? opts.title : t('js.ui.confirm.title');
        const confirmText = opts.confirmText != null ? opts.confirmText : t('js.ui.confirm.ok');
        const cancelText = opts.cancelText != null ? opts.cancelText : t('js.ui.confirm.cancel');
        const danger = !!opts.danger;
        // opts.checkbox = { label, checked } —— 给「一个框同时问两件事」用：主问题走
        // 确定/取消，附带的布尔选项走勾选框。为什么不再串第二个框：第二个框的取消位
        // （ESC / 点遮罩 / 左边那颗按钮）落到的是**另一个维度**的默认答案，用户以为
        // 自己取消了整件事，实际上主动作照做 —— history.js 的删除流程就栽在这。
        //
        // 带 checkbox 时 resolve 的是 {confirmed, checked}，不带时仍是 boolean，
        // 既有调用点（config.js×3 / map.js×1）一个字都不用改。
        const checkboxOpt = opts.checkbox || null;
        // opts.select = { label, options: [{value, label}], value } —— 「确定之前先
        // 选一个」的那类问题（导出格式选择器是第一个调用点：一个任务能导出的格式
        // 不止一种时，写死一种等于让另外几种永远点不到）。不串第二个框的理由同
        // checkbox；resolve 的是 {confirmed, selected}，两者都带时是
        // {confirmed, checked, selected}。
        const selectOpt = opts.select || null;

        return new Promise(function (resolve) {
            const overlay = document.createElement('div');
            overlay.className = 'app-confirm-overlay';

            const dialog = document.createElement('div');
            dialog.className = 'app-confirm';
            dialog.setAttribute('role', 'dialog');
            dialog.setAttribute('aria-modal', 'true');

            const titleEl = document.createElement('div');
            titleEl.className = 'app-confirm__title';
            titleEl.textContent = title;

            const msgEl = document.createElement('div');
            msgEl.className = 'app-confirm__msg';
            msgEl.textContent = message == null ? '' : String(message);

            const actions = document.createElement('div');
            actions.className = 'app-confirm__actions';

            const cancelBtn = document.createElement('button');
            cancelBtn.type = 'button';
            cancelBtn.className = 'app-confirm__btn app-confirm__btn--cancel';
            cancelBtn.textContent = cancelText;

            const okBtn = document.createElement('button');
            okBtn.type = 'button';
            okBtn.className = 'app-confirm__btn app-confirm__btn--ok' + (danger ? ' is-danger' : '');
            okBtn.textContent = confirmText;

            // 用 <label> 而不是 <div>：点文字也能勾是 label 的本分；顺带避开
            // test_css_contract.py 那张逐条建模 <div> 背景层叠的运行时注入表。
            let checkEl = null;
            let checkWrap = null;
            if (checkboxOpt) {
                checkWrap = document.createElement('label');
                checkWrap.className = 'app-confirm__check';
                checkEl = document.createElement('input');
                checkEl.type = 'checkbox';
                checkEl.className = 'app-confirm__check-box';
                checkEl.checked = !!checkboxOpt.checked;
                const checkText = document.createElement('span');
                checkText.textContent = checkboxOpt.label == null ? '' : String(checkboxOpt.label);
                checkWrap.appendChild(checkEl);
                checkWrap.appendChild(checkText);
            }

            // 同上一段用 <label> 的两条理由（点文字也落到控件上、不给
            // test_css_contract.py 那张运行时注入 <div> 表添行）。
            //
            // 复用 .app-confirm__check 的类名不是偷懒：那条规则就是「附带输入行
            // 贴回问题的下边距里」这个布局槽本身（负上边距 + 24px 下边距 + 小字
            // 弱色），与下拉要的完全一样。新起一个 .app-confirm__select 就是往
            // style.css 里塞第二份同样的间距值，而那张表正在做间距刻度归一。
            let selectEl = null;
            let selectWrap = null;
            if (selectOpt) {
                selectWrap = document.createElement('label');
                selectWrap.className = 'app-confirm__check';
                const selectText = document.createElement('span');
                // .text-nowrap（2026-08-20 Task 9b 起为 style.css 自有工具类）：
                // .app-confirm__check 是 flex row，标签文字不设 nowrap 时会被
                // 下拉挤成两行（实测「导出格式」在 400px 的框里断成
                // 「导出格」/「式」）。
                selectText.className = 'text-nowrap';
                selectText.textContent = selectOpt.label == null
                    ? '' : String(selectOpt.label);
                selectEl = document.createElement('select');
                // .form-select 走全站那份控件样式（含给下拉箭头让位的 36px 右
                // 内边距，见 style.css 里那条规则的说明）。不加 .form-select-sm：
                // 它的 padding 会被后加载的站内规则压掉，只剩一个不一致的字号。
                selectEl.className = 'form-select';
                (selectOpt.options || []).forEach(function (o) {
                    const optEl = document.createElement('option');
                    optEl.value = String(o.value);
                    // textContent 而不是 innerHTML：选项文案可能来自插件注册的
                    // 格式 id（第三方字符串）。
                    optEl.textContent = o.label == null ? String(o.value) : String(o.label);
                    selectEl.appendChild(optEl);
                });
                if (selectOpt.value != null) selectEl.value = String(selectOpt.value);
                selectWrap.appendChild(selectText);
                selectWrap.appendChild(selectEl);
            }

            actions.appendChild(cancelBtn);
            actions.appendChild(okBtn);
            dialog.appendChild(titleEl);
            dialog.appendChild(msgEl);
            if (selectWrap) dialog.appendChild(selectWrap);
            if (checkWrap) dialog.appendChild(checkWrap);
            dialog.appendChild(actions);
            overlay.appendChild(dialog);
            document.body.appendChild(overlay);

            const prevFocus = document.activeElement;
            let closed = false;
            // M14：确认框挂载时刻。两级确认之间的回车穿透就靠它与层栈的 repeat
            // 守卫拦住，完整推理写在下面的 accept()。
            const openedAt = (typeof performance !== 'undefined' && performance.now)
                ? performance.now() : Date.now();

            function cleanup(result) {
                if (closed) return;
                closed = true;
                overlay.classList.remove('app-confirm-overlay--in');
                overlay.classList.add('app-confirm-overlay--out');
                // 出场动画跑完（或 300ms 兜底）才真正下线：**注销必须与节点一起
                // 消失，不能提前到这里**。淡出的那 300ms 里框还看得见，层栈里也
                // 还留着它 —— 于是紧跟的第二发 Esc 落在这一层上、被 close() 的
                // `closed` 守卫吃掉，而不是穿到身后的面板去。这正是改前
                // 「capture + stopImmediatePropagation」那套 hack 在防的事，现在
                // 由「层什么时候算关掉」这条规则本身承担。
                overlay.addEventListener('transitionend', finish, { once: true });
                setTimeout(finish, 300);            // transitionend 不触发的兜底
                if (prevFocus && typeof prevFocus.focus === 'function') {
                    try { prevFocus.focus(); } catch (e) { /* 明确忽略：打开前的焦点元素可能已不在文档里 */ }
                }
                // 取消（ESC / 点遮罩 / 取消键）一律把附带输入压成「没给」——
                // 「什么都不做」不该顺带漏出一个用户已经放弃的勾选值或选项值。
                if (!checkboxOpt && !selectOpt) {
                    resolve(result);
                    return;
                }
                // 写成对象字面量而不是逐个赋属性：tests/test_tasks_js_contract.py
                // 的两条断言按 `checked: result &&` / `selected: result ?` 逐字
                // 匹配这里 —— 它们钉的就是「取消不许漏出附带输入」这条。
                resolve(Object.assign(
                    { confirmed: result },
                    checkboxOpt ? { checked: result && !!checkEl.checked } : null,
                    selectOpt ? { selected: result ? selectEl.value : null } : null));
            }

            function finish() {
                overlay.remove();
                unregister();
            }

            // 层栈按下的 Enter：确认。
            function accept() {
                // 时间窗挡的是**真人快速双击**：与淡入动画（--dur-base 200ms）
                // 对齐，挂载后 300ms 内的回车一律忽略，让用户看清这一框问的是
                // 什么。它与层栈里那道 `e.repeat` 守卫各挡一半，缺一不可 ——
                // 自动重复的间隔约 30ms，时间窗根本挡不住（第一次重复被挡在
                // ~280ms，紧接着 ~310ms 那次照样穿透）；而 repeat 守卫认不出
                // 「手指还没离开鼠标就又敲了回车」这种真人连击。
                //
                // 防的是同一件事：回车穿透两级确认。cleanup 里 resolve(true) 的
                // 续体是微任务，在同一轮事件循环末尾就同步挂上第二个框，必然
                // 早于下一个 keydown 宏任务。第二个框问的是【另一个维度】的问题
                // （默认答案 = 取消 = 保留产物），自动确认会替用户选中破坏性的
                // 那一边，直接发 ?delete_files=true 删掉瓦片/GeoTIFF/DEM。
                const now = (typeof performance !== 'undefined' && performance.now)
                    ? performance.now() : Date.now();
                if (now - openedAt < 300) return;
                cleanup(true);
            }

            // Esc / Enter 不再自己监听：全站唯一那个 keydown 在 panels.js 的
            // 层栈里，这里只声明「我是一层、我怎么关、怎么算确认」。
            const unregister = window.TerraLayers.register('confirm', {
                isOpen: function () { return !!overlay.parentNode; },
                close: function () { cleanup(false); },
                accept: accept,
            });

            cancelBtn.addEventListener('click', function () { cleanup(false); });
            okBtn.addEventListener('click', function () { cleanup(true); });
            overlay.addEventListener('click', function (e) { if (e.target === overlay) cleanup(false); });
            // Tab 焦点环挂在遮罩上而不是 document 上：焦点开局就在框里（下面那句
            // focus()），trapTab 保证它出不去，所以事件必然冒得到这里。
            overlay.addEventListener('keydown', function (e) {
                if (e.key === 'Tab') trapTab(e, dialog);
            });

            requestAnimationFrame(function () {
                overlay.classList.add('app-confirm-overlay--in');
                // 落点三档，按「按错了代价多大」排：
                //   带选择器 —— 给下拉，用户要做的第一件事是选，不是确认；
                //   danger    —— 给取消键。破坏性操作的静息焦点不许停在确认键上，
                //                 否则一发回车就把东西删了（审查记为暗模式）。
                //                 Enter 仍然是确认（层栈的 accept），只是要用户
                //                 主动按，而不是「焦点已经在那儿了」顺手按到；
                //   其余      —— 给确认键。
                (selectEl || (danger ? cancelBtn : okBtn)).focus();
            });
        });
    }

    // ------------------------------------------------------- Progress dialog
    // 一个不可取消的模态进度框。唯一调用方是 history.js 的 deleteTask ——
    // 勾了「同时删除磁盘产物」的删除是**同步**请求：后端在请求线程里 rmtree
    // 整个瓦片金字塔（几万到上百万个文件），几十秒到几分钟内 fetch 不返回。
    // 在此之前界面上什么都不发生：确认框一关就是一片死寂，用户会以为没点上。
    //
    // 为什么没有取消按钮、也不响应 ESC/点遮罩：删除到一半没有回滚 —— 目录已经
    // 空了一半，任务行也早就没了。给一颗只能骗人的取消按钮比不给更糟。但改前它
    // 是在捕获阶段把 ESC **吞掉**、界面上什么都不发生 —— 与「这个键坏了」完全
    // 无法区分。现在改成在层栈里显式声明 `dismissible: false` 并给出理由，
    // 由层栈说明为什么关不掉。
    //
    // 复用 .app-confirm-overlay / .app-confirm 两个类不是偷懒：tests/
    // test_css_contract.py 的 _RUNTIME_INJECTED_DIVS 逐条登记了运行时注入的
    // div 背景层叠链，这两个已在册；换一对新类名就得同步改那张表和它的计数断言。
    // 进度条同理复用 .progress / .progress-bar / .progress__label（history.js
    // 的任务详情就是这套 markup），不新增任何带过渡的选择器分支 —— 那会动到
    // test_motion_rule_index_is_complete 的锚点。
    //
    // 返回 { update({text, percent}), close() }：percent 为 null 表示还没有分母
    // （后端的 scan 阶段），此时条留在 0 只显示文案。
    function showProgressDialog(opts) {
        opts = opts || {};
        const overlay = document.createElement('div');
        overlay.className = 'app-confirm-overlay';

        const dialog = document.createElement('div');
        dialog.className = 'app-confirm';
        dialog.setAttribute('role', 'dialog');
        dialog.setAttribute('aria-modal', 'true');
        // 进度只在数字里变化。改前框上只有 role=progressbar + aria-valuenow，
        // 而**属性变化不进 live 区** —— 对读屏用户，这个框从头到尾一声不响。
        // live 区放在 dialog 上而不是某一行上：阶段文案（msgEl）与百分比
        // （label）是两处，一处一个 live 区会各念各的。
        // 这里**不需要**像 toast 容器那样把 live 区提前挂进文档：要播的是
        // update() 里后续的阶段文案与百分比，它们发生在后面的 tick（socket
        // 推送），那时 dialog 早已在无障碍树里。首屏的标题/文案不靠 live 区
        // 播，靠下面 dialog.focus() 把焦点送进 role=dialog。
        dialog.setAttribute('aria-live', 'polite');
        // 供程序化聚焦：这个框里一个可聚焦控件都没有（它没有取消键，见上），
        // 而 aria-modal 承诺了模态封闭 —— 焦点必须先落进来，Tab 才有东西可拦，
        // 否则读屏用户的焦点还留在身后那半个「已被宣告冻结」的界面上。
        dialog.tabIndex = -1;

        const titleEl = document.createElement('div');
        titleEl.className = 'app-confirm__title';
        titleEl.textContent = opts.title == null ? '' : String(opts.title);

        const msgEl = document.createElement('div');
        msgEl.className = 'app-confirm__msg';
        msgEl.textContent = opts.message == null ? '' : String(opts.message);

        const track = document.createElement('div');
        track.className = 'progress';
        const fill = document.createElement('div');
        // bg-danger：删除是破坏性动作，与确认框的 is-danger 主按钮同一语义色。
        fill.className = 'progress-bar bg-danger';
        fill.setAttribute('role', 'progressbar');
        fill.setAttribute('aria-valuemin', '0');
        fill.setAttribute('aria-valuemax', '100');
        fill.style.width = '0%';
        const label = document.createElement('span');
        label.className = 'progress__label';
        // 不再 aria-hidden：它是这个 live 区里唯一会变的数字。视觉上它与
        // progressbar 的 aria-valuenow 重复，但那条是属性、播不出来。
        track.appendChild(fill);
        track.appendChild(label);

        dialog.appendChild(titleEl);
        dialog.appendChild(msgEl);
        dialog.appendChild(track);
        overlay.appendChild(dialog);
        document.body.appendChild(overlay);

        const prevFocus = document.activeElement;
        // Esc / Enter 不再自己监听：全站唯一那个 keydown 在 panels.js 的层栈里。
        const unregister = window.TerraLayers.register('progress', {
            isOpen: function () { return !!overlay.parentNode; },
            // 不给 close：这一层压根关不掉，给一个只能骗人的 close 比不给更糟。
            dismissible: false,
            reason: t('js.ui.progress.locked'),
        });
        // 框里没有可聚焦控件，trapTab 的候选表是空的 —— 它会 preventDefault，
        // Tab 原地不动。这正是要的：aria-modal 说了外面冻结，就不许 Tab 出去。
        overlay.addEventListener('keydown', function (e) {
            if (e.key === 'Tab') trapTab(e, dialog);
        });

        requestAnimationFrame(function () {
            overlay.classList.add('app-confirm-overlay--in');
            try { dialog.focus(); } catch (e) { /* 明确忽略：框可能已被 close() 摘掉 */ }
        });

        let closed = false;

        function finish() {
            overlay.remove();
            unregister();
        }

        return {
            update: function (state) {
                if (closed || !state) return;
                if (state.text != null) msgEl.textContent = String(state.text);
                if (state.percent == null) return;
                // 钳到 0..100：后端的分母是扫描阶段数出来的，删除阶段若因为
                // 目录被并发写入而多出条目，百分比会冲过 100，条会顶出圆角轨道。
                const pct = Math.max(0, Math.min(100, Math.round(state.percent)));
                fill.style.width = pct + '%';
                fill.setAttribute('aria-valuenow', String(pct));
                // 只在整数档真的变了才写：label 现在在 live 区里，每次
                // update() 都赋一遍同样的字符串等于让读屏把「37%」念上十遍
                // （后端的进度事件比 1% 密得多）。
                const text = pct + '%';
                if (label.textContent !== text) label.textContent = text;
            },
            close: function () {
                if (closed) return;
                closed = true;
                overlay.classList.remove('app-confirm-overlay--in');
                overlay.classList.add('app-confirm-overlay--out');
                overlay.addEventListener('transitionend', finish, { once: true });
                setTimeout(finish, 300);            // transitionend 不触发的兜底
                // 焦点归还：框马上要从文档里消失，焦点还在它身上的话浏览器会把
                // 焦点甩回 <body>，键盘用户得从头 Tab 一遍。
                if (dialog.contains(document.activeElement)
                    && prevFocus && typeof prevFocus.focus === 'function') {
                    try { prevFocus.focus(); } catch (e) { /* 明确忽略：打开前的焦点元素可能已不在文档里 */ }
                }
            },
        };
    }

    // -------------------------------------------------------------- 提交守卫
    // 一个动作在飞的时候，触发它的那颗按钮必须点不动。
    //
    //   guard(triggerEl, asyncFn) -> Promise（透传 asyncFn 的返回值）
    //
    // 形态照抄 map.js 下载提交处那一份（存原文案 → disabled → 换 spinner →
    // finally 复原），另外 11 处 POST/DELETE 零守卫 ——「开始」连点三次就是三发
    // start，删除连点三次就是三发 DELETE，后两发撞 404 再弹两条红字。提成公共
    // 函数而不是各处再抄一遍那 20 行。
    //
    // ⚠️ 2026-08-15 订正两句话。改前这里写「被抄的那一份是全仓唯一写对的一处」，
    // 以及「map.js 那份写的是 `animation: spin`，而全仓**没有** @keyframes spin
    // —— 它其实一动不动」。两句都是错的：
    //   · 那份的锁点排在 `await currentTileEstimate()` **之后**，多边形选区的
    //     张数往返飞着的时候按钮完全可点，连点两次建两个任务 —— 它恰恰是唯一
    //     有真实竞态的一处。现在 #taskForm 的整条 submit 改走本函数，锁点在任何
    //     await 之前，那三份手写锁连同它们的「创建中/上传中」文案一起删了。
    //   · @keyframes spin 确实存在，由 map.js 在解析期注入 document.head（它同时
    //     注入一条零消费者的 fadeOut）。那个 spinner 一直在转。注入的关键帧还绕过
    //     tests/test_css_contract.py 的动画计数（那边只读 style.css），所以两条
    //     关键帧随手写锁一并删除。
    //
    // 与被抄的那一份的两点差异：
    //   1. spinner 用 .hint-spin（style.css 里已登记的那条无限旋转，config.js
    //      的代理检测在用）—— 全站一份实现，不再有第二条 spin。
    //   2. 文案不参数化：按钮自己的可见文字原样留在 spinner 后面（图标钮
    //      textContent 为空，就只剩 spinner）。每处各配一条「正在…」等于新增
    //      一批要维护的文案，而按钮已经写着它在做什么。
    //
    // triggerEl 允许为空（键盘触发、事件代理拿不到按钮）：那时退化成直接
    // await，动作语义一个字不变，只是没有视觉锁。
    const GUARD_SPINNER = '<svg class="icon-inline icon-inline--md hint-spin"'
        + ' width="14" height="14" viewBox="0 0 24 24" fill="none"'
        + ' stroke="currentColor" stroke-width="2" aria-hidden="true">'
        + '<path d="M21 12a9 9 0 1 1-6.22-8.56"/></svg>';

    async function guard(triggerEl, asyncFn) {
        if (!triggerEl) return asyncFn();
        // disabled 只挡鼠标。回车重复触发、事件代理里同一颗按钮被两条路径
        // 分派、程序化调用都进得来，所以另立一个在飞标志。
        if (triggerEl.dataset.guardBusy === '1') return undefined;
        triggerEl.dataset.guardBusy = '1';
        const originalHtml = triggerEl.innerHTML;
        const originalDisabled = !!triggerEl.disabled;
        const label = (triggerEl.textContent || '').trim();
        triggerEl.disabled = true;
        triggerEl.setAttribute('aria-busy', 'true');
        // label 来自按钮自己的 textContent，先转义再拼：任务名不会出现在按钮
        // 文案里，但「按钮文字永远是安全的」不是一条能长期成立的假设。
        triggerEl.innerHTML = GUARD_SPINNER + (label ? ' ' + escapeHtml(label) : '');
        try {
            return await asyncFn();
        } finally {
            delete triggerEl.dataset.guardBusy;
            triggerEl.disabled = originalDisabled;
            triggerEl.removeAttribute('aria-busy');
            triggerEl.innerHTML = originalHtml;
            // 焦点收尾。改前这里只解禁不还焦点，键盘用户删一条任务、确认完之后
            // Tab 序要从整页开头重来 —— 上面那句 `disabled = true` 会触发 UA 的
            // unfocusing steps：被聚焦元素变 disabled 的当场，焦点就掉回 <body>。
            //
            // 兜不住的是 showConfirm 自己那份 prevFocus：它在弹框挂载时才抓
            // document.activeElement，而 guard 内套 showConfirm 的四处动作
            // （history.deleteTask、config.clearCacheCategory、config.resetConfig、
            // task_center.acceptTaskGaps）此刻抓到的已经是 body，归还给 body 等于
            // 没归还。所以由制造这次焦点丢失的 guard 自己收尾。
            //
            // 顺序不可换：focus() 对 disabled 元素是 no-op，必须先恢复 disabled
            // 再抢焦点。只在焦点确实掉到 body（或落焦元素已离开文档）时才抢 ——
            // 请求飞行期间用户可能主动点进别处，那种焦点不许动。
            const active = document.activeElement;
            if ((!active || active === document.body || !active.isConnected)
                && triggerEl.isConnected && typeof triggerEl.focus === 'function') {
                try { triggerEl.focus(); } catch (e) { /* 明确忽略：按钮可能刚被移出文档 */ }
            }
        }
    }

    // ------------------------------------------------------ Connection status
    // 底部状态栏的连接状态点。**唯一调用方是 socket.js 的 get()**（socket 实例刚
    // 建好时调一次），不是 tasks.js —— 连接的创建权在 socket.js 提走之后就易主了。
    // 而 socket 现在是全站单例、每页都会连，所以「无 socket 的页面」已经不存在；
    // hidden 只是首帧到建连之间的那一小段。
    function initConnectionStatus(socket) {
        const el = document.getElementById('connStatus');
        const text = document.getElementById('connStatusText');
        if (!el || !text || !socket) return;
        el.hidden = false;
        function apply(connected) {
            el.classList.toggle('conn-status--on', connected);
            el.classList.toggle('conn-status--off', !connected);
            text.textContent = connected ? t('js.ui.conn.connected') : t('js.ui.conn.disconnected');
        }
        apply(socket.connected);
        socket.on('connect', function () { apply(true); });
        socket.on('disconnect', function () { apply(false); });
    }

    // -------------------------------------------------------------- escaping
    // 服务端/用户可控字符串（任务名、output_dir、style 原文等）拼进 innerHTML
    // 模板前必须过这一道（C6：存储型 XSS）。error_message 走 textContent 的
    // 既有约定不变，不经过这里。& 必须最先替换，否则后面的实体会被二次转义。
    function escapeHtml(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // ---------------------------------------------------------------- bytes
    // 字节 → 人类可读。**全站唯一一份 1024 进位换算**：配置页的缓存卡、任务
    // 详情的产物清单、地图的磁盘预算判决与 TIF 信息卡、任务行的速度读数，
    // 全部落到这一个函数上。
    //
    // 住在 ui.js 而不是 task_center.js，是因为 /config 独立页把 base.html 的
    // vendor_task_list_js 块覆盖成空（省掉 Vue + 三个任务脚本约 160 KB），
    // 那一页没有 task_center.js —— 放那边就是缓存卡一片 ReferenceError。
    // 本文件是无条件全局加载的那一档。
    //
    // 单位是 KiB/MiB/GiB —— 1024 进制的前缀就是这几个。写 KB/MB 是 1000 进制
    // 的前缀，标在除以 1024 得来的数字上每级偏 2.4%，到 TiB 偏 10%；而产品里
    // 唯一会拒绝用户文件的字节阈值（api.region.too_large）标的就是 MiB，两处
    // 对不上时用户没法拿界面读数验算「我这个文件到底超没超」。
    // 单位词不翻译（B/KiB/MiB 中英通用），只有句子模板走 i18n。
    //
    // 舍入规则是**参数**，不是第二份实现。曾经有三份：这里、task_center.js 的
    // formatSpeed（≥100 取整）、map.js 的 _fmtBytes（`v < 10 ? 1 : 0` 位），
    // 于是同一个 102400 B 分别读作 100.0 KB / 100 KB / 100 KB，而没有任何机制
    // 会报错。差别本身是真的 —— 速度是每秒重画的活读数，≥100 取整是为了不让
    // 字宽在 99.9 ↔ 100.0 之间来回跳；文件大小是静态读数，没有这个问题，
    // 一律留一位小数反而更准（「1.4 GiB」比「1 GiB」有用）—— 但那是一行 if，
    // 不值三份进位循环。
    const BYTE_UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];

    function formatBytes(bytes, opts) {
        const o = opts || {};
        let value = Number(bytes) || 0;
        let i = 0;
        while (value >= 1024 && i < BYTE_UNITS.length - 1) {
            value /= 1024;
            i += 1;
        }
        // 整字节不留小数（「1023 B」而不是「1023.0 B」）；半个字节没有意义，
        // 所以 B 档取整而不是原样打印。
        const shown = (i === 0 || (o.roundAtHundred && value >= 100))
            ? Math.round(value)
            : value.toFixed(1);
        return `${shown} ${BYTE_UNITS[i]}${o.suffix || ''}`;
    }

    // ------------------------------------------------------------ coordinates
    // 经纬度 → 字符串。精度只有**两档**，而且位数只由这两个函数持有：
    //   formatCoord      读数档 5 位（≈1.1 m）：状态栏、选区四至、任务行区域
    //   formatCoordExact 详情档 6 位（≈0.11 m）：复制到剪贴板、任务详情四至
    //
    // 改造前是四档，位数散在十几个调用点上，没有一处写着「为什么是这个位数」：
    // task_list.js 的 toFixed(2)（≈1.1 km —— 同一个选区在任务行和状态栏读出
    // 两个不同的框）、map.js 状态栏与四至浮层的 toFixed(4)、五处 toFixed(5)、
    // 复制路径的 toFixed(6)。
    //
    // 分两档的理由：屏幕读数要窄，5 位已经比一个屏幕像素更细，再多只是噪声；
    // 而**离开界面**的值（剪贴板、任务详情记录）要能原样贴回去复现选区，
    // 多一位不占地方。所以「复制」走详情档，哪怕它旁边显示的是读数档。
    //
    // 角度**跨度**（`east - west` 那种差值）不走这里：它回答「这个框多大」而
    // 不是「它在哪」，是另一个概念，3 位（≈100 m）就够 —— 见 map.js 的选区
    // 尺寸读数与下载面板四至摘要。
    function formatCoord(deg) {
        return Number(deg).toFixed(5);
    }

    function formatCoordExact(deg) {
        return Number(deg).toFixed(6);
    }

    // ------------------------------------------------------ task timestamps
    // 后端任务时间字段（created_at/started_at/completed_at）有两种格式：
    // 新的 UTC ISO 8601（带 +00:00 时区标记）和历史遗留的 SQLite 裸格式
    // 'YYYY-MM-DD HH:MM:SS[.ffffff]'（UTC，无时区标记）。裸格式直接
    // new Date() 会被当成本地时间（Safari 对空格分隔格式甚至返回
    // Invalid Date），所以一律走这里：无时区后缀的一律按 UTC 解析。
    function parseTaskDate(value) {
        if (!value) return null;
        let s = String(value).trim();
        if (!/([zZ]|[+-]\d{2}:?\d{2})$/.test(s)) {
            s = s.replace(' ', 'T') + 'Z';
        }
        const d = new Date(s);
        return isNaN(d.getTime()) ? null : d;
    }

    // ------------------------------------------------------------ tile origin
    // 每个页面只探测一次瓦片专用端口；失败、超时或 HTTPS 页面都保留同源路径。
    // 仅内部绝对路径会改写，外部 URL、协议相对 URL 和相对路径保持原值。
    //
    // 名单必须与后端 src/core/tile_paths.py 的 TILE_PATH_PREFIXES 逐字一致
    // （tests/test_tile_server.py 有相等性断言防漂移，**顺序也算**）：瓦片端口
    // 只放行这五条前缀，把名单外的路径改写过去换来的是瓦片端口自己应答的
    // **硬 404** —— 主端口上那份能用的资源根本没被请求，而探测结果整页缓存，
    // 本次会话内不会回退。所以名单外一律 fail-open 返回原路径，走主端口，
    // 最坏只是慢。
    //
    // '/mbtiles/' 是 §5.3 的单一读路由（/mbtiles/<pipeline>/<task_id>/z/x/y.ext，
    // 影像 / 等高线 / 将来的 MVT 全走它一条，§5.3 明确禁止按数据类型各开一条）。
    // 它与其它四条一样是逐瓦片的高频路径，必须能落到瓦片专用端口 —— 漏了它
    // 只会「慢」，但那个慢是每一块瓦片都多绕一次主端口。
    const TILE_PATH_PREFIXES = ['/basemap/', '/tiles/', '/terrain/', '/contour/', '/mbtiles/'];
    const TILE_HEALTH_TIMEOUT_MS = 1000;
    let tileOrigin = '';
    let tileOriginReady = null;

    function initTileOrigin(tilePort) {
        if (tileOriginReady) return tileOriginReady;
        tileOriginReady = (async function () {
            if (!tilePort || location.protocol !== 'http:') return false;
            let timer = null;
            try {
                const health = new URL(location.href);
                health.port = String(tilePort);
                // 与后端 tile_server.TILE_HEALTH_PATH 逐字一致（精确匹配，
                // tests/test_tile_server.py 有相等性断言）：差一个字符就是 404，
                // 表现为每页白等 1 秒然后整页退回同源，两侧都不报错。
                health.pathname = '/tile-health';
                health.search = '';
                health.hash = '';
                const controller = new AbortController();
                timer = setTimeout(function () {
                    controller.abort();
                }, TILE_HEALTH_TIMEOUT_MS);
                const response = await fetch(health.href, {
                    signal: controller.signal,
                    cache: 'no-store',
                });
                if (!response.ok) return false;
                tileOrigin = health.origin;
                return true;
            } catch (error) {
                // 明确忽略：探不通就是没有可用的瓦片端口，全站回落同源路径
                // （tileUrl 在 tileOrigin 为空时原样返回，是正常降级）。
                return false;
            } finally {
                if (timer !== null) clearTimeout(timer);
            }
        })();
        return tileOriginReady;
    }

    function tileUrl(path) {
        if (typeof path !== 'string' || !/^\/(?!\/)/.test(path)) return path;
        if (!tileOrigin) return path;
        const isTilePath = TILE_PATH_PREFIXES.some(function (prefix) {
            return path.startsWith(prefix);
        });
        return isTilePath ? tileOrigin + path : path;
    }

    window.showToast = showToast;
    window.showConfirm = showConfirm;
    window.showProgressDialog = showProgressDialog;
    window.guard = guard;
    window.trapTab = trapTab;
    window.showNotification = showToast; // 兼容旧的 showNotification(message, type) 调用
    window.initConnectionStatus = initConnectionStatus;
    window.parseTaskDate = parseTaskDate;
    window.escapeHtml = escapeHtml;
    window.formatBytes = formatBytes;
    window.formatCoord = formatCoord;
    window.formatCoordExact = formatCoordExact;
    window.initTileOrigin = initTileOrigin;
    window.tileUrl = tileUrl;
})();
