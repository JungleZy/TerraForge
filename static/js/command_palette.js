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
 * - Esc 走 document **capture**:面板开着时只关面板并 stopPropagation(),
 *   背后的工作台面板 / 弹窗(bubble 监听)收不到 —— 永远「先关最上层」。
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
        { id: 'new_download', titleKey: 'js.cmdk.new_download',
          guard: function () {
              return !!el('boundsDownloadBtn') && typeof window.openDownloadModal === 'function';
          },
          run: function () { window.openDownloadModal(); } },
        { id: 'open_tasks', titleKey: 'js.cmdk.open_tasks',
          guard: function () { return !!el('historyPanel') && typeof window.openPanel === 'function'; },
          run: function () { window.openPanel('records'); } },
        { id: 'open_config', titleKey: 'js.cmdk.open_config',
          guard: function () { return !!el('configPanel') && typeof window.openPanel === 'function'; },
          run: function () { window.openPanel('config'); } },
        { id: 'open_process', titleKey: 'js.cmdk.open_process',
          guard: function () { return !!el('processOpenBtn'); },
          run: function () { el('processOpenBtn').click(); } },
        { id: 'copy_coords', titleKey: 'js.cmdk.copy_coords',
          guard: function () { return !!el('statusCoords'); },
          run: function () { el('statusCoords').click(); } },
        { id: 'goto_history', titleKey: 'js.cmdk.goto_history',
          run: function () { window.location.href = '/history'; } },
        { id: 'goto_config', titleKey: 'js.cmdk.goto_config',
          run: function () { window.location.href = '/config'; } },
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

    // aria-modal 承诺了模态封闭,Tab 焦点不许逃出遮罩 —— 与 panels.js 的
    // focusables 同一范式。palette 里只有 input、help 里只有关闭钮,
    // 环实际就是「钉在唯一控件上」,但结构按通用写,将来加控件不用改。
    var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]),'
        + ' select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

    function trapTab(e, container) {
        var list = [].slice.call(container.querySelectorAll(FOCUSABLE)).filter(function (n) {
            return n.offsetParent !== null;
        });
        if (!list.length) { e.preventDefault(); return; }
        var first = list[0];
        var last = list[list.length - 1];
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

    function openPalette() {
        if (overlayBusy()) return;
        restoreFocus = document.activeElement;
        palette.hidden = false;
        input.value = '';
        render('');
        try { input.focus(); } catch (e) { /* 元素可能已不在文档里 */ }
    }

    function closePalette() {
        if (palette.hidden) return;
        palette.hidden = true;
        if (restoreFocus && typeof restoreFocus.focus === 'function') {
            try { restoreFocus.focus(); } catch (e) { /* 同上 */ }
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
        var btn = help.querySelector('.cmdk__help-close');
        try { (btn || help).focus(); } catch (e) { /* 忽略 */ }
    }

    function closeHelp() {
        if (help.hidden) return;
        help.hidden = true;
        if (restoreHelpFocus && typeof restoreHelpFocus.focus === 'function') {
            try { restoreHelpFocus.focus(); } catch (e) { /* 元素可能已不在文档里 */ }
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
            if (isPaletteOpen()) { trapTab(e, palette.querySelector('.cmdk__dialog')); return; }
            if (isHelpOpen()) { trapTab(e, help.querySelector('.cmdk__dialog')); return; }
        }
        if (isOpen() || overlayBusy() || isEditable(e.target)) return;
        if (e.key === '?') {
            e.preventDefault();
            openHelp();
        }
    });

    // Esc(capture):只关最上层,拦截穿透 —— 工作台面板 / Bootstrap 弹窗
    // 都在 bubble 段监听,stopPropagation 后它们收不到这次 Esc。
    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape') return;
        if (isHelpOpen()) { e.stopPropagation(); closeHelp(); }
        else if (isPaletteOpen()) { e.stopPropagation(); closePalette(); }
    }, true);

    return { open: openPalette, close: closePalette, openHelp: openHelp,
             closeHelp: closeHelp, isOpen: isOpen };
})();
