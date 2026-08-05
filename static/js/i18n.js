/**
 * 浏览器侧文案查表。
 *
 * 服务端在 base.html 里内联了 `window.__I18N__`（只含 `js.` 前缀那部分，模板文案
 * 在服务端就渲染完了）和 `window.__LANG__`。本文件把它包成全局 `t()`，其余脚本
 * 直接调用。
 *
 * 必须排在所有业务脚本之前加载：ui.js / map.js 这些在解析期就可能调 t()。
 *
 * 查不到 key 时原样返回 key —— 界面上直接看到 `js.foo.bar`，比静默回退成中文
 * 更容易发现漏翻（与服务端 src/i18n/__init__.py 的口径一致）。
 */
(function (global) {
    'use strict';

    var catalog = global.__I18N__ || {};
    var lang = global.__LANG__ || 'zh';

    /**
     * @param {string} key   文案 key（js.<模块>.<短名>）
     * @param {Object} [params] 占位符取值，模板里写成 {name}
     * @returns {string}
     */
    function t(key, params) {
        var text = Object.prototype.hasOwnProperty.call(catalog, key) ? catalog[key] : key;
        if (!params) return text;
        return text.replace(/\{(\w+)\}/g, function (whole, name) {
            return Object.prototype.hasOwnProperty.call(params, name) ? params[name] : whole;
        });
    }

    global.TerraI18n = {
        lang: lang,
        t: t,
        /** 语言开关用：写 cookie 后由调用方 reload —— 语种要送到服务端才能渲染模板。 */
        set: function (next) {
            if (next !== 'zh' && next !== 'en') return;
            document.cookie = 'tf-lang=' + next + ';path=/;max-age=' + (365 * 24 * 3600) + ';SameSite=Lax';
        }
    };
    global.t = t;
})(window);
