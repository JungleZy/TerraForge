/**
 * 保存路径「浏览」按钮的目录选择弹窗。
 *
 * 数据源 GET /api/fs/browse(只列 DOWNLOADS_DIR 内的子目录,越界 400)。
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
        _el('pathBrowserCurrent').textContent = data.path;
        const list = _el('pathBrowserDirs');
        list.innerHTML = '';
        if (data.parent) {
            list.appendChild(_item('.. (上一级)', function () { load(data.parent); }));
        }
        (data.dirs || []).forEach(function (d) {
            list.appendChild(_item(d.name, function () { load(d.path); }));
        });
        if (!list.children.length) {
            list.appendChild(_item('(没有子目录)', function () {}, true));
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
            // 输入框里可能还是相对值/不存在的目录:回退根目录,把原因亮出来
            if (path) {
                errBox.textContent = '起点不可用(' + e.message + '),已回到下载根目录';
                errBox.hidden = false;
                return load('');
            }
            errBox.textContent = '目录列表加载失败:' + e.message;
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
