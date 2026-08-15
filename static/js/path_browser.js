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
        // ⚠️ 判据是「有没有这个字段」，不是它真不真。后端的 parent 有三种
        // 取值：绝对路径 = 上一级目录；**`''` = 盘符列表**（Windows 的盘符根
        // 之上还有一层）；null = 真的到顶。写成 `if (data.parent)` 会把 `''`
        // 一起吞掉 —— Windows 上退到 C:\ 就没有「上一级」可点，再也回不到盘符
        // 列表，换不了盘。load('') 正好请求那一层（下面 load 里 path 为空就
        // 不带 query），不需要额外分支。
        if (data.parent !== null && data.parent !== undefined) {
            list.appendChild(_item(t('js.path_browser.parent_dir'), function () { load(data.parent); }));
        }
        (data.dirs || []).forEach(function (d) {
            list.appendChild(_item(d.name, function () { load(d.path); }));
        });
        if (!list.children.length) {
            list.appendChild(_item(t('js.path_browser.no_subdirs'), function () {}, true));
        }
    }

    // 请求序号：目录项连点（无防抖、无禁用）时先发的响应可能后返回，
    // 于是列表渲染成一个**较早**点击的目录，而 currentPath 已经是新值 ——
    // 「选择此目录」会写回一个用户没在看的路径。与 history.js loadHistory
    // 的 _historyReqSeq 同一套守卫。
    var _reqSeq = 0;

    async function load(path, keepError) {
        const errBox = _el('pathBrowserError');
        const url = '/api/fs/browse' + (path ? '?path=' + encodeURIComponent(path) : '');
        const seq = ++_reqSeq;
        // 读目录期间列表里必须有话说。慢盘（网络驱动器、休眠的机械盘）上
        // /api/fs/browse 要好几秒，在此之前列表停在**上一个目录**的内容上：
        // 用户看到的是一个「点了没反应」的弹窗，然后再点一个别的目录 ——
        // 于是两发请求竞速，而胜者由网络决定（_reqSeq 只保证不错乱，不保证
        // 是他最后点的那个）。占位项与「(没有子目录)」同一形态，不新增样式。
        const list = _el('pathBrowserDirs');
        if (list) {
            list.innerHTML = '';
            list.appendChild(_item(t('js.path_browser.loading'), function () {}, true));
        }
        try {
            const resp = await fetch(url);
            const data = await resp.json();
            if (seq !== _reqSeq) return;    // 已有更新的请求发出，本次结果作废
            if (!resp.ok || !data.success) {
                throw new Error(data.error || ('HTTP ' + resp.status));
            }
            _render(data);
            // 清错误只能在**渲染成功之后**,而且回退那一跳必须能保留原因。
            // 原来这是入口处一句无条件的 `errBox.hidden = true`:下面
            // 「设好 start_unavailable -> return load('')」的回退分支,刚写好的
            // 错误会被那次递归调用的入口立刻抹掉 —— start_unavailable 因此是
            // 一条永远看不到的死文案,用户填了不存在的路径只会看到一个盘符
            // 列表,完全不知道自己填的值有问题。
            if (!keepError) errBox.hidden = true;
        } catch (e) {
            // 过期请求的失败同样不许写界面：否则一次早点击的超时会把用户
            // 正在看的新目录覆盖成错误态。回退那一跳（load('', true)）自己
            // 会再取一个新序号，不受这条守卫影响。
            if (seq !== _reqSeq) return;
            // 输入框里可能还是相对值/不存在的目录:回退根级(盘符/根目录),把原因亮出来
            if (path) {
                errBox.textContent = t('js.path_browser.start_unavailable', { error: e.message });
                errBox.hidden = false;
                return load('', true);
            }
            errBox.textContent = t('js.path_browser.load_failed', { error: e.message });
            errBox.hidden = false;
            // 根级也读不出来时 _render 一次都不跑：占位项必须自己撤掉，否则
            // 弹窗上会同时挂着一条「加载失败」和一条「正在读取目录…」。
            if (list) list.innerHTML = '';
        }
    }

    // 具名函数表达式：栈帧里显示 openPathBrowser 而不是匿名，源码契约测试也
    // 才能按名字切出函数体。名字只绑定在函数自身作用域内，不污染 IIFE。
    window.openPathBrowser = function openPathBrowser(inputId) {
        targetInput = document.getElementById(inputId);
        if (!targetInput) return;
        // 必须清掉上一次会话留下的 currentPath：请求的目录失败、回退根级又
        // 失败时 _render 一次都不会跑，弹窗于是顶着一条错误横幅显示**上一次**
        // 浏览的目录，「选择此目录」把那个陈旧路径写回输入框 —— 与 _reqSeq
        // 守卫防的是同一件事：写回一个用户没在看的值。
        currentPath = null;
        const cur = _el('pathBrowserCurrent');
        if (cur) cur.textContent = '';
        const modalEl = _el('pathBrowserModal');
        modalInst = bootstrap.Modal.getOrCreateInstance(modalEl);
        modalInst.show();
        load(targetInput.value.trim());
    };

    // 「选择此目录」的落地动作。提出来是因为现在有两个触发点：按钮和回车。
    function _confirmSelection() {
        if (targetInput && currentPath) {
            targetInput.value = currentPath;
            // 触发 input:map.js 的 userEdited 标记靠它,类型切换不再覆盖选择
            targetInput.dispatchEvent(new Event('input', { bubbles: true }));
        }
        if (modalInst) modalInst.hide();
    }

    document.addEventListener('DOMContentLoaded', function () {
        const selectBtn = _el('pathBrowserSelect');
        if (selectBtn) {
            selectBtn.addEventListener('click', _confirmSelection);
        }

        const modalEl = _el('pathBrowserModal');
        if (modalEl) {
            // 焦点落在主按钮上，与 ui.js 的确认框同一条约定（挂载后 focus 到
            // 默认按钮）：不给落点的话 Bootstrap 把焦点留在 .modal 容器上，
            // 键盘用户要先 Tab 过关闭钮、当前目录、整张目录列表才够得到
            // 「选择此目录」，而那正是他打开这个弹窗要按的那一颗。
            modalEl.addEventListener('shown.bs.modal', function () {
                if (selectBtn) selectBtn.focus();
            });
            // 回车 = 选中当前目录。目录项自己是 <button>，焦点在它上面时回车
            // 归它（进那个目录），不抢 —— 抢了的话键盘用户永远下钻不进去。
            modalEl.addEventListener('keydown', function (e) {
                if (e.key !== 'Enter') return;
                const el = e.target;
                if (el && el.classList && el.classList.contains('list-group-item')) return;
                e.preventDefault();
                _confirmSelection();
            });
        }

        // 「浏览」按钮统一在这里接线。模板里原来是
        // onclick="openPathBrowser('outputPath')" 这样的内联属性：它逼着
        // openPathBrowser 必须是全局函数，还和 CSP 的 unsafe-inline 绑死。
        // 目标输入框的 id 现在从 data-path-target 读，不在 JS 里再抄一份。
        // 用委托而不是 querySelectorAll 逐个绑：首页的配置面板是一段服务端
        // 渲染的隐藏子树，将来改成动态插入时这里不用跟着改。模板那边的
        // onclick 已同步删掉 —— 两边都留着会点一次弹两次。
        document.addEventListener('click', function (e) {
            const btn = e.target && e.target.closest && e.target.closest('[data-path-target]');
            if (!btn) return;
            const id = btn.getAttribute('data-path-target');
            if (id) window.openPathBrowser(id);
        });
    });
})();
