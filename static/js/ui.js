/**
 * 全局 UI 组件：自定义 Toast 通知 + Confirm 模态框
 *
 * 替代浏览器原生 alert() / confirm()，与 premium 深色设计系统（GIS 蓝强调色）统一。
 * 全局暴露：
 *   window.showToast(message, type, opts)      —— 右上角通知，type: success|danger|warning|info
 *   window.showConfirm(message, opts) -> Promise<boolean>  —— 居中确认框
 *       opts.checkbox = {label, checked} 时改 resolve {confirmed, checked}
 *   window.showNotification(message, type)     —— showToast 的别名（兼容旧调用）
 *   window.parseTaskDate(value) -> Date|null   —— 任务时间字段统一解析（裸格式按 UTC）
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
    function ensureToastContainer() {
        let c = document.getElementById('app-toast-container');
        if (!c) {
            c = document.createElement('div');
            c.id = 'app-toast-container';
            document.body.appendChild(c);
        }
        return c;
    }

    function showToast(message, type, opts) {
        type = VALID_TYPES.indexOf(type) !== -1 ? type : 'info';
        opts = opts || {};
        const duration = opts.duration != null ? opts.duration : 3500;

        const container = ensureToastContainer();

        const toast = document.createElement('div');
        toast.className = 'app-toast app-toast--' + type;
        toast.setAttribute('role', 'alert');

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

            actions.appendChild(cancelBtn);
            actions.appendChild(okBtn);
            dialog.appendChild(titleEl);
            dialog.appendChild(msgEl);
            if (checkWrap) dialog.appendChild(checkWrap);
            dialog.appendChild(actions);
            overlay.appendChild(dialog);
            document.body.appendChild(overlay);

            const prevFocus = document.activeElement;
            let closed = false;
            // M14：确认框挂载时刻。回车连击/自动重复会穿透两级确认 ——
            // cleanup 摘掉 A 的监听后 resolve(true)，续体是微任务，在同一轮
            // 事件循环末尾就同步挂上 B 的监听，必然早于下一个 keydown 宏任务。
            // 受害最重的是 history.js 的删除流程：第二个框问的是【另一个维度】
            // 的问题（默认答案 = 取消 = 保留产物），自动确认会替用户选中破坏性
            // 的那一边，直接发 ?delete_files=true 删掉瓦片/GeoTIFF/DEM。
            const openedAt = (typeof performance !== 'undefined' && performance.now)
                ? performance.now() : Date.now();

            function cleanup(result) {
                if (closed) return;
                closed = true;
                document.removeEventListener('keydown', onKey, true);
                overlay.classList.remove('app-confirm-overlay--in');
                overlay.classList.add('app-confirm-overlay--out');
                overlay.addEventListener('transitionend', function () { overlay.remove(); }, { once: true });
                setTimeout(function () { overlay.remove(); }, 300); // 兜底
                if (prevFocus && typeof prevFocus.focus === 'function') {
                    try { prevFocus.focus(); } catch (e) { /* ignore */ }
                }
                // 取消（ESC / 点遮罩 / 取消键）一律把 checked 压成 false ——
                // 「什么都不做」不该顺带漏出一个用户已经放弃的勾选值。
                resolve(checkboxOpt
                    ? { confirmed: result, checked: result && !!checkEl.checked }
                    : result);
            }

            function onKey(e) {
                if (e.key !== 'Escape' && e.key !== 'Enter') return;
                // 确认框开着时 ESC/Enter 归它独占。为什么必须是
                // stopImmediatePropagation + 捕获阶段：panels.js 的关面板监听
                // 也挂在 document 上，同一节点上的监听按注册顺序跑，
                // stopPropagation 对它无效；而面板先开、监听先注册，所以只有
                // 排在捕获阶段才能抢在它前面。否则一次 ESC 既关确认框又把整个
                // 任务面板收掉（任务没被删，纯打断感）。
                //
                // 挡在 repeat 判断【之前】：面板监听没有 repeat 判断，长按 ESC
                // 时第一发被我们吞掉、后面每一次重复都会漏过去关掉面板。
                e.preventDefault();
                e.stopImmediatePropagation();
                // e.repeat 是承重的那一半：按键自动重复的间隔约 30ms，而下面
                // 那道时间窗最长也就几百毫秒 —— 只加时间窗挡不住连续重复（第一
                // 次重复被挡在 ~280ms，紧接着 ~310ms 那次照样穿透，于是两级
                // 确认被一路自动按穿）。preventDefault 拦不住 repeat，必须显式忽略。
                if (e.repeat) return;
                if (e.key === 'Escape') { cleanup(false); return; }
                // 时间窗挡的是真人快速双击：与淡入动画（200ms）对齐，
                // 挂载后 300ms 内的回车一律忽略，让用户看清这一框问的是什么。
                const now = (typeof performance !== 'undefined' && performance.now)
                    ? performance.now() : Date.now();
                if (now - openedAt < 300) return;
                cleanup(true);
            }

            cancelBtn.addEventListener('click', function () { cleanup(false); });
            okBtn.addEventListener('click', function () { cleanup(true); });
            overlay.addEventListener('click', function (e) { if (e.target === overlay) cleanup(false); });
            document.addEventListener('keydown', onKey, true);

            requestAnimationFrame(function () {
                overlay.classList.add('app-confirm-overlay--in');
                okBtn.focus();
            });
        });
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

    window.showToast = showToast;
    window.showConfirm = showConfirm;
    window.showNotification = showToast; // 兼容旧的 showNotification(message, type) 调用
    window.initConnectionStatus = initConnectionStatus;
    window.parseTaskDate = parseTaskDate;
    window.escapeHtml = escapeHtml;
})();
