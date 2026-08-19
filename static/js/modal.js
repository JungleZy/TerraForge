/* 极简 modal:焦点陷阱 / ESC / 遮罩点击。接口与 bootstrap.Modal 同形。 */
/*
 * 在简报骨架上补的三处,都是「调用处零改动语义」的硬依赖,不是增强:
 *
 * 1. _isOpen 守卫。Bootstrap 的 show()/hide() 对已开/已关的实例是 no-op;
 *    没有这道守卫,viewTaskDetails 连发两次 show() 就会叠两层遮罩 —— 正是
 *    getOrCreateInstance 当初要消灭的事故。
 * 2. shown.bs.modal / hidden.bs.modal 事件(冒泡)。两个既有监听靠它们活:
 *    path_browser.js 挂 shown.bs.modal 把焦点落到「选择此目录」,
 *    history.js 挂 hidden.bs.modal 停任务日志轮询。不发就是静默回归。
 * 3. [data-bs-dismiss="modal"] 点击。两个模板的关闭/取消按钮全靠 Bootstrap
 *    的 data-API 委托;换成 TfModal 后那条委托拿到的是 _isShown=false 的
 *    Bootstrap 实例,hide() 直接 no-op,按钮全灭。所以这里自己接。
 */
(function () {
    'use strict';
    const instances = new WeakMap();

    class TfModalInstance {
        constructor(el) {
            this.el = el;
            this._isOpen = false;
            this._onKeydown = (e) => {
                if (e.key === 'Escape') this.hide();
                if (e.key === 'Tab') this._trapFocus(e);
            };
            this._onBackdrop = (e) => { if (e.target === this.el) this.hide(); };
            this._onDismiss = (e) => {
                if (e.target.closest('[data-bs-dismiss="modal"]')) this.hide();
            };
        }
        show() {
            if (this._isOpen) return;
            this._isOpen = true;
            this._prevFocus = document.activeElement;
            this.el.classList.add('show');
            this.el.style.display = 'block';
            this.el.removeAttribute('aria-hidden');
            document.body.classList.add('modal-open');
            const bd = document.createElement('div');
            bd.className = 'modal-backdrop fade show';
            document.body.appendChild(bd);
            this._backdrop = bd;
            document.addEventListener('keydown', this._onKeydown);
            this.el.addEventListener('mousedown', this._onBackdrop);
            this.el.addEventListener('click', this._onDismiss);
            const first = this.el.querySelector('[autofocus], button, input, select, textarea, [tabindex]');
            if (first) first.focus();
            this.el.dispatchEvent(new CustomEvent('shown.bs.modal', { bubbles: true }));
        }
        hide() {
            if (!this._isOpen) return;
            this._isOpen = false;
            this.el.classList.remove('show');
            this.el.style.display = 'none';
            this.el.setAttribute('aria-hidden', 'true');
            document.body.classList.remove('modal-open');
            if (this._backdrop) { this._backdrop.remove(); this._backdrop = null; }
            document.removeEventListener('keydown', this._onKeydown);
            this.el.removeEventListener('mousedown', this._onBackdrop);
            this.el.removeEventListener('click', this._onDismiss);
            if (this._prevFocus) this._prevFocus.focus();
            this.el.dispatchEvent(new CustomEvent('hidden.bs.modal', { bubbles: true }));
        }
        _trapFocus(e) {
            const items = this.el.querySelectorAll('button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])');
            if (!items.length) return;
            const first = items[0], last = items[items.length - 1];
            if (e.shiftKey && document.activeElement === first) { last.focus(); e.preventDefault(); }
            else if (!e.shiftKey && document.activeElement === last) { first.focus(); e.preventDefault(); }
        }
    }

    window.TfModal = {
        getOrCreateInstance(el) {
            if (!instances.has(el)) instances.set(el, new TfModalInstance(el));
            return instances.get(el);
        }
    };
})();
