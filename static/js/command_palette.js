/**
 * 命令面板(Ctrl/Cmd+K)+ 快捷键速查(`?`)。借鉴 GeoLibre:
 * 单一命令注册表同时驱动面板列表、全局快捷键与速查表。
 *
 * 契约(改之前先读):
 * - 零依赖:IIFE 挂 window.TerraCommands;文案一律 t('js.cmdk.*'),
 *   键以**完整字面量**写在注册表的 titleKey 里 —— tests/test_i18n.py 的
 *   双向闭合按字面量扫描,运行时拼 key 会养出假孤儿。
 * - 全局键走 window bubble:尊重 e.defaultPrevented;input/textarea/select/
 *   contenteditable 豁免;confirm(.app-confirm-overlay)或 Bootstrap 弹窗
 *   (body.modal-open)在场时不抢。
 * - Esc 不自己监听：向 panels.js 的层栈 register('cmdk'/'cmdkHelp')，全站唯一
 *   那个「关最上层」的 keydown 在那里。所以 base.html 里 panels.js 必须排在
 *   本文件之前（解析期就 register）。
 * - 命令的 guard() 决定它在当前页面是否出现(独立页没有地图/面板元素)。
 * 加载顺序(base.html 依赖图):i18n.js、ui.js、theme.js 之后。
 */
window.TerraCommands = (function () {
    'use strict';

    function el(id) { return document.getElementById(id); }

    /* 命令注册表。listed:false 只进速查表;info:true 是纯说明行(无动作);
       带 keys 的进速查表。 */
    var REGISTRY = [
        { id: 'open_palette', titleKey: 'js.cmdk.open_palette', keys: 'Ctrl/⌘+K', listed: false,
          run: function () { toggle(); } },
        { id: 'show_help', titleKey: 'js.cmdk.show_help', keys: '?',
          run: function () { closePalette(); openHelp(); } },
        { id: 'esc_close', titleKey: 'js.cmdk.esc_close', keys: 'Esc', listed: false, info: true },
        { id: 'start_bounds', titleKey: 'js.cmdk.start_bounds',
          guard: function () { return !!el('mapDrawRect'); },
          run: function () { el('mapDrawRect').click(); } },
        { id: 'clear_bounds', titleKey: 'js.cmdk.clear_bounds',
          guard: function () { return !!el('boundsClearBtn'); },
          run: function () { el('boundsClearBtn').click(); } },
        // 「新建任务」：打开 #createPanel 并预选瓦片管线。改前它 guard 在
        // #boundsDownloadBtn 上（选区浮层里那颗按钮），于是**没框选就没有这条
        // 命令** —— 而新建面板本来就该在没有选区时也能开（缺选区拦在提交那一刻）。
        // 判据换成面板本体在不在这一页。
        { id: 'new_download', titleKey: 'js.cmdk.new_download',
          guard: function () {
              return !!el('createPanel') && typeof window.openCreatePanel === 'function';
          },
          run: function () { window.openCreatePanel('map'); } },
        { id: 'open_tasks', titleKey: 'js.cmdk.open_tasks',
          guard: function () { return !!el('historyPanel') && typeof window.openPanel === 'function'; },
          run: function () { window.openPanel('records'); } },
        { id: 'open_config', titleKey: 'js.cmdk.open_config',
          guard: function () { return !!el('configPanel') && typeof window.openPanel === 'function'; },
          run: function () { window.openPanel('config'); } },
        // 「新建地形切片任务」：同一个面板，预选本地地形切片。改前它 guard 在任务
        // 面板筛选行右端那颗「处理」按钮上并 .click() 转点 —— 那颗按钮 2026-08-15
        // 随入口收敛删掉了，转点式的 run 会连命令一起变成死代码（guard 返回
        // false，命令从列表里静默消失）。现在直接调函数。
        { id: 'open_process', titleKey: 'js.cmdk.open_process',
          guard: function () {
              return !!el('createPanel') && typeof window.openCreatePanel === 'function';
          },
          run: function () { window.openCreatePanel('local_terrain'); } },
        { id: 'copy_coords', titleKey: 'js.cmdk.copy_coords',
          guard: function () { return !!el('statusCoords'); },
          run: function () { el('statusCoords').click(); } },
        // goto_history / goto_config 两条已删（2026-08-15 入口收敛）：命令面板同时
        // 列「打开任务面板」+「前往历史记录页」是同一件事的两种形态，而 /history、
        // /config 两条路由本身**保留**（深链与打包可达性需要），只是不再从命令
        // 面板露出第二条路。
        { id: 'theme_dark', titleKey: 'js.cmdk.theme_dark',
          guard: function () { return !!(window.TerraTheme && window.TerraTheme.set); },
          run: function () { window.TerraTheme.set('dark'); } },
        { id: 'theme_light', titleKey: 'js.cmdk.theme_light',
          guard: function () { return !!(window.TerraTheme && window.TerraTheme.set); },
          run: function () { window.TerraTheme.set('light'); } },
        { id: 'lang_switch', titleKey: 'js.cmdk.lang_switch',
          run: function () {
              var next = (window.__LANG__ === 'zh') ? 'en' : 'zh';
              document.cookie = 'tf-lang=' + next + ';path=/;max-age=31536000';
              window.location.reload();
          } },
    ];

    var palette = el('cmdk');
    var input = el('cmdkInput');
    var list = el('cmdkList');
    var help = el('cmdkHelp');
    var helpList = el('cmdkHelpList');
    if (!palette || !input || !list || !help || !helpList) {
        // 没有外壳的页面(理论上不会 —— base.html 全站 include):整个空载。
        return { open: function () {}, close: function () {}, openHelp: function () {},
                 closeHelp: function () {}, isOpen: function () { return false; } };
    }

    var items = [];       // 当前过滤结果(REGISTRY 条目)
    var active = 0;       // 高亮下标
    var restoreFocus = null;
    var restoreHelpFocus = null;

    function title(cmd) { return t(cmd.titleKey); }
    function isPaletteOpen() { return !palette.hidden; }
    function isHelpOpen() { return !help.hidden; }
    function isOpen() { return isPaletteOpen() || isHelpOpen(); }

    function isEditable(target) {
        return target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA'
            || target.tagName === 'SELECT' || target.isContentEditable);
    }

    // Tab 焦点环由 ui.js 的 window.trapTab 提供（cmdk / 速查 / confirm /
    // progress 四个自报 aria-modal 的浮层共用一份，2026-08-15 Task 6 从这里
    // 提出去的）。palette 里只有 input、help 里只有关闭钮，环实际就是「钉在
    // 唯一控件上」，但通用实现将来加控件不用改。

    function overlayBusy() {
        return !!document.querySelector('.app-confirm-overlay')
            || document.body.classList.contains('modal-open');
    }

    function visibleCommands() {
        return REGISTRY.filter(function (c) {
            if (c.listed === false || c.info) return false;
            return !c.guard || c.guard();
        });
    }

    function setActive(i) {
        if (!items.length) return;
        active = (i + items.length) % items.length;
        [].forEach.call(list.children, function (li, j) {
            var on = j === active;
            li.classList.toggle('cmdk__item--active', on);
            li.setAttribute('aria-selected', on ? 'true' : 'false');
        });
        input.setAttribute('aria-activedescendant', 'cmdk-item-' + items[active].id);
    }

    function run(cmd) {
        closePalette();
        if (cmd.run) cmd.run();
    }

    function render(query) {
        var q = query.trim().toLowerCase();
        items = visibleCommands().filter(function (c) {
            return !q || title(c).toLowerCase().indexOf(q) !== -1;
        });
        active = 0;
        list.textContent = '';
        if (!items.length) {
            // activedescendant 指向的 id 已随列表清空而不在 DOM,残留是悬空引用。
            input.removeAttribute('aria-activedescendant');
            var empty = document.createElement('li');
            empty.className = 'cmdk__empty';
            empty.setAttribute('role', 'presentation');
            empty.textContent = t('js.cmdk.empty');
            list.appendChild(empty);
            return;
        }
        items.forEach(function (c, i) {
            var li = document.createElement('li');
            li.className = 'cmdk__item' + (i === active ? ' cmdk__item--active' : '');
            li.id = 'cmdk-item-' + c.id;
            li.setAttribute('role', 'option');
            li.setAttribute('aria-selected', i === active ? 'true' : 'false');
            var label = document.createElement('span');
            label.textContent = title(c);
            li.appendChild(label);
            if (c.keys) {
                var kbd = document.createElement('kbd');
                kbd.textContent = c.keys;
                li.appendChild(kbd);
            }
            li.addEventListener('click', function () { run(c); });
            li.addEventListener('mousemove', function () { setActive(i); });
            list.appendChild(li);
        });
        input.setAttribute('aria-activedescendant', 'cmdk-item-' + items[active].id);
    }

    // `hidden` 翻掉之后隔一帧再加 --in：`.cmdk[hidden]` 是 display:none，
    // display 从 none 变过来的那一帧不会跑 transition，不隔帧等于没有动画。
    // 与 .workbench-panel / .app-confirm-overlay 同一套两步（2026-08-15 Task 6
    // 把浮层入场统一成 opacity + transform / --dur-base / --ease 一套）。
    // 关闭仍是**立刻**：命令面板是打了就走的东西，退场再等 200ms 只会挡住
    // 它刚触发的那条命令。
    function openPalette() {
        if (overlayBusy()) return;
        restoreFocus = document.activeElement;
        palette.hidden = false;
        input.value = '';
        render('');
        requestAnimationFrame(function () { palette.classList.add('cmdk--in'); });
        try { input.focus(); } catch (e) { /* 明确忽略：元素可能已不在文档里 */ }
    }

    function closePalette() {
        if (palette.hidden) return;
        palette.hidden = true;
        palette.classList.remove('cmdk--in');
        if (restoreFocus && typeof restoreFocus.focus === 'function') {
            try { restoreFocus.focus(); } catch (e) { /* 明确忽略：同上，来源元素可能已被重建 */ }
        }
        restoreFocus = null;
    }

    function toggle() {
        if (isHelpOpen()) closeHelp();
        else if (isPaletteOpen()) closePalette();
        else openPalette();
    }

    // ---- 速查表:注册表里带 keys 的条目 ----------------

    function renderHelp() {
        helpList.textContent = '';
        REGISTRY.filter(function (c) { return c.keys; }).forEach(function (c) {
            var li = document.createElement('li');
            li.className = 'cmdk__help-row';
            var label = document.createElement('span');
            label.textContent = title(c);
            var kbd = document.createElement('kbd');
            kbd.textContent = c.keys;
            li.appendChild(label);
            li.appendChild(kbd);
            helpList.appendChild(li);
        });
    }

    function openHelp() {
        if (overlayBusy()) return;
        // show_help 是 closePalette() 再 openHelp():closePalette 先把焦点还给
        // 面板触发钮,这里再捕获它 —— 接力链正好是对的,顺序不能换。
        restoreHelpFocus = document.activeElement;
        renderHelp();
        help.hidden = false;
        requestAnimationFrame(function () { help.classList.add('cmdk--in'); });
        var btn = help.querySelector('.cmdk__help-close');
        try { (btn || help).focus(); } catch (e) { /* 明确忽略：帮助层刚重建，元素可能已不在文档里 */ }
    }

    function closeHelp() {
        if (help.hidden) return;
        help.hidden = true;
        help.classList.remove('cmdk--in');
        if (restoreHelpFocus && typeof restoreHelpFocus.focus === 'function') {
            try { restoreHelpFocus.focus(); } catch (e) { /* 明确忽略：元素可能已不在文档里 */ }
        }
        restoreHelpFocus = null;
    }

    // ---- 事件接线 ----------------

    input.addEventListener('input', function () { render(input.value); });
    input.addEventListener('keydown', function (e) {
        if (e.key === 'ArrowDown') { e.preventDefault(); setActive(active + 1); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(active - 1); }
        else if (e.key === 'Enter') {
            e.preventDefault();
            if (items[active]) run(items[active]);
        }
        // Esc 不在这里处理 —— 统一走下面的 document capture(面板/速查同一层)。
    });

    palette.querySelector('[data-cmdk-close]').addEventListener('click', closePalette);
    [].forEach.call(help.querySelectorAll('[data-cmdk-help-close]'), function (n) {
        n.addEventListener('click', closeHelp);
    });

    // 全局键(bubble):Ctrl/Cmd+K 开关面板,`?` 开速查。
    window.addEventListener('keydown', function (e) {
        if (e.defaultPrevented) return;
        if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 'k' || e.key === 'K')) {
            // 面板开着时焦点在自家输入框里,也要能 toggle 关闭 —— 所以可编辑
            // 豁免只挡「面板没开」的情况(别把正文输入框里的 Ctrl+K 抢过来)。
            if (!isOpen() && isEditable(e.target)) return;
            e.preventDefault();
            toggle();
            return;
        }
        if (e.key === 'Tab') {
            if (isPaletteOpen()) { window.trapTab(e, palette.querySelector('.cmdk__dialog')); return; }
            if (isHelpOpen()) { window.trapTab(e, help.querySelector('.cmdk__dialog')); return; }
        }
        if (isOpen() || overlayBusy() || isEditable(e.target)) return;
        if (e.key === '?') {
            e.preventDefault();
            openHelp();
        }
    });

    // Esc 不在这里处理：整站唯一那个「关最上层」的 keydown 在 panels.js 的层栈
    // 里，本文件只报到。改前这里是 document capture + stopPropagation，靠「相位
    // 比别人早」抢在工作台面板前面 —— 那是三份 Esc 实现互相让位的一环，加一层
    // 就要回头改另外两份。
    //
    // 两条各注册一层而不是合成一条 'cmdk'：速查表是从命令面板里开出去的
    // （show_help 先 closePalette 再 openHelp），两者各有各的 restoreFocus
    // 接力，topName() 也应当报得出到底哪一层在上面。速查后注册 = 在上面，与
    // toggle() 里「先关速查再关面板」的既有顺序一致。
    window.TerraLayers.register('cmdk', {
        isOpen: isPaletteOpen,
        close: closePalette,
    });
    window.TerraLayers.register('cmdkHelp', {
        isOpen: isHelpOpen,
        close: closeHelp,
    });

    return { open: openPalette, close: closePalette, openHelp: openHelp,
             closeHelp: closeHelp, isOpen: isOpen };
})();
