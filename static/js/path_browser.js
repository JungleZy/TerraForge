/**
 * 保存路径「浏览」按钮的目录选择弹窗。
 *
 * 数据源 GET /api/fs/browse(0.2.4 起全盘可浏览;Windows 根级返回盘符列表)。
 * 用法:按钮 onclick="openPathBrowser('<inputId>')";选中结果写回该 input
 * 并派发 input 事件(map.js 靠它把字段标记为 userEdited,不被类型切换
 * 的默认值覆盖)。base.html 全站加载,首页任务表单与配置页共用。
 */
(function () {
    'use strict';

    let currentPath = null;    // 弹窗里正在浏览的目录(绝对路径)
    let targetInput = null;    // 选中后要写回的输入框
    let modalInst = null;

    function _el(id) { return document.getElementById(id); }

    function _item(label, onClick, muted) {
        const a = document.createElement('button');
        a.type = 'button';
        a.className = 'list-group-item list-group-item-action' + (muted ? ' text-muted' : '');
        a.textContent = label;
        a.addEventListener('click', onClick);
        return a;
    }

    function _render(data) {
        currentPath = data.path;
        // Windows 盘符列表视图 path 为 ''(此时「选择此目录」因 currentPath
        // 为空不会写回,只能继续点盘符下钻)
        _el('pathBrowserCurrent').textContent = data.path || t('js.path_browser.pick_drive');
        const list = _el('pathBrowserDirs');
        list.innerHTML = '';
        if (data.parent) {
            list.appendChild(_item(t('js.path_browser.parent_dir'), function () { load(data.parent); }));
        }
        (data.dirs || []).forEach(function (d) {
            list.appendChild(_item(d.name, function () { load(d.path); }));
        });
        if (!list.children.length) {
            list.appendChild(_item(t('js.path_browser.no_subdirs'), function () {}, true));
        }
    }

    async function load(path) {
        const errBox = _el('pathBrowserError');
        errBox.hidden = true;
        const url = '/api/fs/browse' + (path ? '?path=' + encodeURIComponent(path) : '');
        try {
            const resp = await fetch(url);
            const data = await resp.json();
            if (!resp.ok || !data.success) {
                throw new Error(data.error || ('HTTP ' + resp.status));
            }
            _render(data);
        } catch (e) {
            // 输入框里可能还是相对值/不存在的目录:回退根级(盘符/根目录),把原因亮出来
            if (path) {
                errBox.textContent = t('js.path_browser.start_unavailable', { error: e.message });
                errBox.hidden = false;
                return load('');
            }
            errBox.textContent = t('js.path_browser.load_failed', { error: e.message });
            errBox.hidden = false;
        }
    }

    window.openPathBrowser = function (inputId) {
        targetInput = document.getElementById(inputId);
        if (!targetInput) return;
        const modalEl = _el('pathBrowserModal');
        modalInst = bootstrap.Modal.getOrCreateInstance(modalEl);
        modalInst.show();
        load(targetInput.value.trim());
    };

    document.addEventListener('DOMContentLoaded', function () {
        const selectBtn = _el('pathBrowserSelect');
        if (selectBtn) {
            selectBtn.addEventListener('click', function () {
                if (targetInput && currentPath) {
                    targetInput.value = currentPath;
                    // 触发 input:map.js 的 userEdited 标记靠它,类型切换不再覆盖选择
                    targetInput.dispatchEvent(new Event('input', { bubbles: true }));
                }
                if (modalInst) modalInst.hide();
            });
        }
    });
})();
