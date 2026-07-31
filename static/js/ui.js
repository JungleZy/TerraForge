/**
 * 全局 UI 组件：自定义 Toast 通知 + Confirm 模态框
 *
 * 替代浏览器原生 alert() / confirm()，与 premium 深色设计系统（GIS 蓝强调色）统一。
 * 全局暴露：
 *   window.showToast(message, type, opts)      —— 右上角通知，type: success|danger|warning|info
 *   window.showConfirm(message, opts) -> Promise<boolean>  —— 居中确认框
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
        close.setAttribute('aria-label', '关闭');
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
        function remove() {
            if (removed) return;
            removed = true;
            if (timer) clearTimeout(timer);
            toast.classList.remove('app-toast--in');
            toast.classList.add('app-toast--out');
            toast.addEventListener('transitionend', function () { toast.remove(); }, { once: true });
            setTimeout(function () { toast.remove(); }, 400); // 兜底
        }

        close.addEventListener('click', remove);
        if (duration > 0) timer = setTimeout(remove, duration);

        return { close: remove };
    }

    // -------------------------------------------------------------- Confirm
    function showConfirm(message, opts) {
        opts = opts || {};
        const title = opts.title != null ? opts.title : '确认操作';
        const confirmText = opts.confirmText != null ? opts.confirmText : '确定';
        const cancelText = opts.cancelText != null ? opts.cancelText : '取消';
        const danger = !!opts.danger;

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

            actions.appendChild(cancelBtn);
            actions.appendChild(okBtn);
            dialog.appendChild(titleEl);
            dialog.appendChild(msgEl);
            dialog.appendChild(actions);
            overlay.appendChild(dialog);
            document.body.appendChild(overlay);

            const prevFocus = document.activeElement;
            let closed = false;

            function cleanup(result) {
                if (closed) return;
                closed = true;
                document.removeEventListener('keydown', onKey);
                overlay.classList.remove('app-confirm-overlay--in');
                overlay.classList.add('app-confirm-overlay--out');
                overlay.addEventListener('transitionend', function () { overlay.remove(); }, { once: true });
                setTimeout(function () { overlay.remove(); }, 300); // 兜底
                if (prevFocus && typeof prevFocus.focus === 'function') {
                    try { prevFocus.focus(); } catch (e) { /* ignore */ }
                }
                resolve(result);
            }

            function onKey(e) {
                if (e.key === 'Escape') { e.preventDefault(); cleanup(false); }
                else if (e.key === 'Enter') { e.preventDefault(); cleanup(true); }
            }

            cancelBtn.addEventListener('click', function () { cleanup(false); });
            okBtn.addEventListener('click', function () { cleanup(true); });
            overlay.addEventListener('click', function (e) { if (e.target === overlay) cleanup(false); });
            document.addEventListener('keydown', onKey);

            requestAnimationFrame(function () {
                overlay.classList.add('app-confirm-overlay--in');
                okBtn.focus();
            });
        });
    }

    // ------------------------------------------------------ Connection status
    // 工具栏连接状态点。有 socket 的页面（首页 tasks.js）在 socket 建立后调用；
    // 无 socket 的页面不调用，指示器保持 hidden。
    function initConnectionStatus(socket) {
        const el = document.getElementById('connStatus');
        const text = document.getElementById('connStatusText');
        if (!el || !text || !socket) return;
        el.hidden = false;
        function apply(connected) {
            el.classList.toggle('conn-status--on', connected);
            el.classList.toggle('conn-status--off', !connected);
            text.textContent = connected ? '已连接' : '已断开';
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
