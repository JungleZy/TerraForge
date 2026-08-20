/**
 * 插件管理面板：列表 / 启停 / 加载失败原因 / 声明式新建任务表单。
 *
 * 列表**不走 Jinja**：插件集是运行期数据（注册表随 plugins/ 目录与启停状态变），
 * 服务端渲染一次就过期。模板只给骨架（_plugins_content.html），这里拉
 * `GET /api/plugins` 渲染到 #pluginsList。
 *
 * 文案两条口径，别混：
 *   - **宿主自己的**界面文字全部走 `t('js.plugins.*')`。必须是 `js.` 前缀 ——
 *     只有那部分文案会内联到浏览器（src/i18n/__init__.py 的 client_catalog），
 *     这里调 `t('tpl.…')` 会把键名原样显示给用户。
 *   - **插件自带的**字符串（名字/描述来自 plugin.toml，参数标签来自
 *     ParamSpec.label）不进 catalog：它们是运行期数据，翻译归插件作者。宿主
 *     只负责在进 DOM 之前过 esc() —— 第三方字符串直接拼进 innerHTML 就是 XSS。
 *
 * 新建任务的区域参数是四个数字输入（北/南/东/西），v1 **刻意不接地图框选**：
 * 那要把插件面板与地图的框选状态绑起来，属于范围切割掉的那一块。
 */
(function () {
    'use strict';

    /** 第三方字符串进 innerHTML 的唯一入口。属性值也走它（引号一并转义）。 */
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
            return {
                '&': '&amp;', '<': '&lt;', '>': '&gt;',
                '"': '&quot;', "'": '&#39;'
            }[c];
        });
    }

    /** type 取 ui.js 的 VALID_TYPES（success/danger/warning/info），'error' 会被降级。 */
    function toast(message, type) {
        if (window.showToast) window.showToast(message, type);
    }

    /** 四至 + 名称是**宿主**解释的参数，插件 schema 里没有它们。 */
    var HOST_FIELDS = ['name', 'north', 'south', 'east', 'west'];

    // ------------------------------------------------------------ 列表

    function cardHtml(p) {
        var origin = p.origin === 'builtin'
            ? t('js.plugins.origin_builtin')
            : t('js.plugins.origin_external');
        var caps = (p.capabilities || []).map(esc).join(' / ');
        var meta = [esc(p.id), esc(p.version), esc(origin)];
        if (caps) meta.push(caps);

        // 加载失败的插件也在列表里（坏插件不许打穿宿主，但必须看得见），
        // 而且启用钮要禁掉 —— 启用一个加载失败的插件没有任何效果。
        var err = p.load_error
            ? '<div class="alert alert-danger py-1 px-2 mb-2 small plugin-load-error">'
              + '<strong>' + esc(t('js.plugins.load_error')) + '</strong> '
              + esc(p.load_error) + '</div>'
            : '';
        var toggle = p.enabled
            ? '<button type="button" class="btn btn-sm btn-outline-secondary"'
              + ' data-action="disable" data-id="' + esc(p.id) + '">'
              + esc(t('js.plugins.disable')) + '</button>'
            : '<button type="button" class="btn btn-sm btn-outline-primary"'
              + ' data-action="enable" data-id="' + esc(p.id) + '"'
              + (p.load_error ? ' disabled' : '') + '>'
              + esc(t('js.plugins.enable')) + '</button>';
        // 新建任务只对**已启用且有管线能力**的插件开：禁用时
        // `GET /api/plugins/<pid>/schema` 返回空参数表，渲染出来的表单提交必然
        // 被 404 挡回（registry.get_pipeline 为 None）。
        var newTask = (p.enabled && (p.capabilities || []).indexOf('pipeline') >= 0)
            ? '<button type="button" class="btn btn-sm btn-outline-primary"'
              + ' data-newtask="' + esc(p.id) + '">'
              + esc(t('js.plugins.new_task')) + '</button>'
            : '';
        // 配置区对**加载成功**的插件一律开（不看 enabled）：先填 token 再开启
        // 是最自然的顺序，而且天地图这类纯数据源插件没有「新建任务」入口，
        // 配置是它在界面上唯一的可操作项。加载失败的插件没有 definition，
        // 配置 schema 取不到，开了也只是个填不进去的空框。
        var config = p.load_error
            ? ''
            : '<button type="button" class="btn btn-sm btn-outline-secondary"'
              + ' data-config="' + esc(p.id) + '">'
              + esc(t('js.plugins.config')) + '</button>';

        return '<div class="card mb-2 plugin-card" data-plugin="' + esc(p.id) + '">'
            + '<div class="card-body p-3">'
            + '<div class="mb-1"><strong>' + esc(p.name) + '</strong> '
            + '<span class="small text-secondary">' + meta.join(' · ') + '</span></div>'
            + (p.description
                ? '<p class="small mb-2">' + esc(p.description) + '</p>' : '')
            + err
            + '<div class="d-flex gap-2 flex-wrap">'
            + toggle + config + newTask + '</div>'
            + '<div class="plugin-config-slot"></div>'
            + '<div class="plugin-task-form-slot"></div>'
            + '</div></div>';
    }

    function render(plugins) {
        var root = document.getElementById('pluginsList');
        if (!root) return;
        root.innerHTML = plugins.length
            ? plugins.map(cardHtml).join('')
            : '<p class="text-secondary mb-0">' + esc(t('js.plugins.empty')) + '</p>';
    }

    function load() {
        return fetch('/api/plugins')
            .then(function (r) { return r.json(); })
            .then(function (data) { render(data.plugins || []); })
            .catch(function (e) {
                console.error('[plugins] 列表加载失败:', e);
                toast(t('js.plugins.load_failed'), 'danger');
            });
    }

    // ------------------------------------------------------------ 插件配置

    /**
     * 配置区：`GET /api/plugins/<pid>/config` 给的 `schema` + `config` 现渲染。
     *
     * 凭据键（`type === 'credential'`）用 `type=password`，且**永远不回显真值**
     * —— 服务端下发的是 `__TF_UNCHANGED__` 哨兵（与 GET /api/config 对
     * earthdata_password 的做法一致），原样提交就是「不改」，清空提交就是清除。
     *
     * 没声明 schema 的插件（第三方插件可以不声明）回落到一个 JSON textarea：
     * `PUT /config` 收的本来就是一个 JSON 对象，没有 schema 时后端也不校验，
     * 界面上就该如实呈现这一点，而不是显示一个填不进去的空框。
     */
    function configFieldHtml(spec, value) {
        var name = esc(spec.key);
        var secret = spec.type === 'credential';
        var numeric = spec.type === 'int' || spec.type === 'float';
        var input;
        if (spec.type === 'bool') {
            return '<div class="form-check mb-2">'
                + '<input type="checkbox" class="form-check-input"'
                + ' id="pluginCfg-' + name + '" name="' + name + '"'
                + (value ? ' checked' : '') + '>'
                + '<label class="form-check-label small"'
                + ' for="pluginCfg-' + name + '">'
                + esc(spec.label || spec.key) + '</label></div>';
        }
        if (spec.type === 'enum') {
            input = '<select class="form-select" name="' + name + '">'
                + (spec.choices || []).map(function (c) {
                    return '<option value="' + esc(c) + '"'
                        + (c === value ? ' selected' : '') + '>'
                        + esc(c) + '</option>';
                }).join('')
                + '</select>';
        } else {
            input = '<input class="form-control" name="' + name + '"'
                + ' type="' + (secret ? 'password' : numeric ? 'number' : 'text') + '"'
                + (spec.type === 'float' ? ' step="any"' : '')
                + (spec.min != null ? ' min="' + esc(spec.min) + '"' : '')
                + (spec.max != null ? ' max="' + esc(spec.max) + '"' : '')
                + (secret ? ' autocomplete="new-password"' : '')
                + (value != null ? ' value="' + esc(value) + '"' : '')
                + '>';
        }
        return '<div class="mb-2" data-cfg-field="' + name + '">'
            + '<label class="form-label small mb-1">'
            + esc(spec.label || spec.key) + '</label>' + input
            + (secret
                ? '<div class="form-text small">'
                  + esc(t('js.plugins.config_secret_hint')) + '</div>'
                : '')
            + '<div class="invalid-feedback d-block small plugin-cfg-error"></div>'
            + '</div>';
    }

    function configFormHtml(pid, specs, config) {
        var body = specs.length
            ? specs.map(function (s) {
                return configFieldHtml(s, config[s.key]);
            }).join('')
            : '<div class="mb-2"><label class="form-label small mb-1">'
              + esc(t('js.plugins.config_json_label')) + '</label>'
              + '<textarea class="form-control font-monospace" rows="4"'
              + ' data-cfg-json>' + esc(JSON.stringify(config, null, 2))
              + '</textarea>'
              + '<div class="invalid-feedback d-block small plugin-cfg-error">'
              + '</div></div>';
        return '<form class="plugin-config-form mt-3" data-plugin="' + esc(pid) + '">'
            + body
            + '<button type="submit" class="btn btn-sm btn-primary">'
            + esc(t('js.plugins.config_save')) + '</button>'
            + '</form>';
    }

    /** 配置表单 -> PUT body。空串照发：清空一个凭据就是要把它删掉。 */
    function configPayloadOf(form) {
        var raw = form.querySelector('[data-cfg-json]');
        if (raw) {
            // 无 schema 的回落分支：解析失败让调用方按「本地校验失败」处理。
            return JSON.parse(raw.value || '{}');
        }
        var payload = {};
        form.querySelectorAll('[name]').forEach(function (el) {
            payload[el.getAttribute('name')] = el.type === 'checkbox'
                ? el.checked
                : el.type === 'number' ? Number(el.value) : el.value;
        });
        return payload;
    }

    /** 400 回来的 `errors` 是逐键的，按键回显到对应字段下面。 */
    function showConfigErrors(form, errors) {
        form.querySelectorAll('.plugin-cfg-error').forEach(function (el) {
            el.textContent = '';
        });
        Object.keys(errors || {}).forEach(function (key) {
            var field = form.querySelector('[data-cfg-field="'
                + (window.CSS && CSS.escape ? CSS.escape(key) : key) + '"]');
            var slot = (field || form).querySelector('.plugin-cfg-error');
            if (slot) slot.textContent = key + '：' + errors[key];
        });
    }

    function openConfig(pid, slot) {
        return fetch('/api/plugins/' + encodeURIComponent(pid) + '/config')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                slot.innerHTML = configFormHtml(pid, data.schema || [],
                                                data.config || {});
            })
            .catch(function (err) {
                console.error('[plugins] 配置加载失败:', err);
                toast(t('js.plugins.config_load_failed'), 'danger');
            });
    }

    // ------------------------------------------------------------ 声明式表单

    /**
     * 宿主字段：名称 + 四至。四至必填 —— 任务行的 bbox 没有缺省值。
     *
     * 四个键写成完整字面量而不是「前缀 + 方位」拼出来的：tests/test_i18n.py
     * 按**键形字面量**双向闭合，拼出来的键两头都会红（引用了一个不存在的前缀
     * 键，同时四个真键成了没人引用的孤儿）。拼接点确实需要时要登记进那边的
     * _DYNAMIC_KEY_SITES —— 这里四个字面量更直白，不值得动那张表。
     */
    function hostFieldsHtml() {
        var bbox = [
            ['north', t('js.plugins.form_north')],
            ['south', t('js.plugins.form_south')],
            ['east', t('js.plugins.form_east')],
            ['west', t('js.plugins.form_west')]
        ].map(function (f) {
            return '<div>'
                + '<label class="form-label small mb-1">' + esc(f[1]) + '</label>'
                + '<input type="number" step="any" required'
                + ' class="form-control" name="' + f[0] + '">'
                + '</div>';
        }).join('');
        return '<div class="mb-2">'
            + '<label class="form-label small mb-1">'
            + esc(t('js.plugins.form_name')) + '</label>'
            + '<input type="text" class="form-control" name="name">'
            + '</div>'
            // 自有栅格：两栏、≥992px 四栏（原 Bootstrap 的 row g-2 + col-6 col-lg-3）
            + '<div class="tf-cols tf-cols--2 tf-cols--lg-4 tf-cols--compact mb-2">' + bbox + '</div>';
    }

    /** 插件自己声明的参数（GET /api/plugins/<pid>/schema 的 params）。 */
    function pluginFieldHtml(s) {
        var name = esc(s.key);
        var input;
        if (s.type === 'bool') {
            return '<div class="form-check mb-2">'
                + '<input type="checkbox" class="form-check-input"'
                + ' id="pluginParam-' + name + '" name="' + name + '"'
                + (s.default ? ' checked' : '') + '>'
                + '<label class="form-check-label small"'
                + ' for="pluginParam-' + name + '">'
                + esc(s.label || s.key) + '</label></div>';
        }
        if (s.type === 'enum') {
            input = '<select class="form-select" name="' + name + '">'
                + (s.choices || []).map(function (c) {
                    return '<option value="' + esc(c) + '"'
                        + (c === s.default ? ' selected' : '') + '>'
                        + esc(c) + '</option>';
                }).join('')
                + '</select>';
        } else {
            var numeric = s.type === 'int' || s.type === 'float';
            input = '<input class="form-control" name="' + name + '"'
                + ' type="' + (numeric ? 'number' : 'text') + '"'
                + (s.type === 'float' ? ' step="any"' : '')
                + (s.min != null ? ' min="' + esc(s.min) + '"' : '')
                + (s.max != null ? ' max="' + esc(s.max) + '"' : '')
                + (s.default != null ? ' value="' + esc(s.default) + '"' : '')
                + (s.required ? ' required' : '') + '>';
        }
        return '<div class="mb-2"><label class="form-label small mb-1">'
            + esc(s.label || s.key) + '</label>' + input + '</div>';
    }

    function formHtml(pid, specs) {
        return '<form class="plugin-task-form mt-3" data-plugin="' + esc(pid) + '">'
            + hostFieldsHtml()
            + specs.map(pluginFieldHtml).join('')
            + '<button type="submit" class="btn btn-sm btn-primary">'
            + esc(t('js.plugins.form_submit')) + '</button>'
            + '</form>';
    }

    /** 表单 -> POST body。`auto_start` 是请求上的动作开关，不是任务参数。 */
    function payloadOf(form) {
        var fd = new FormData(form);
        var payload = {
            bbox: [Number(fd.get('north')), Number(fd.get('south')),
                   Number(fd.get('east')), Number(fd.get('west'))],
            auto_start: true
        };
        // 名称留空就不发：让后端按 `<插件 id> 任务` 起名，别把空串写进任务行。
        if (fd.get('name')) payload.name = fd.get('name');
        form.querySelectorAll('[name]').forEach(function (el) {
            var k = el.getAttribute('name');
            if (HOST_FIELDS.indexOf(k) >= 0) return;
            payload[k] = el.type === 'checkbox' ? el.checked
                : el.type === 'number' ? Number(el.value) : el.value;
        });
        return payload;
    }

    // ------------------------------------------------------------ 交互

    document.addEventListener('click', function (e) {
        var target = e.target;
        if (!target || typeof target.closest !== 'function') return;

        var toggleBtn = target.closest('#pluginsList [data-action]');
        if (toggleBtn) {
            fetch('/api/plugins/' + encodeURIComponent(toggleBtn.getAttribute('data-id'))
                  + '/' + toggleBtn.getAttribute('data-action'), { method: 'POST' })
                .then(function (r) {
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    return load();
                })
                .catch(function (err) {
                    console.error('[plugins] 启停失败:', err);
                    toast(t('js.plugins.toggle_failed'), 'danger');
                });
            return;
        }

        var cfgBtn = target.closest('#pluginsList [data-config]');
        if (cfgBtn) {
            var cfgPid = cfgBtn.getAttribute('data-config');
            var cfgSlot = cfgBtn.closest('.plugin-card')
                .querySelector('.plugin-config-slot');
            // 再点一次收起 —— 与新建任务表单同一个开合口径。
            if (cfgSlot.innerHTML) { cfgSlot.innerHTML = ''; return; }
            openConfig(cfgPid, cfgSlot);
            return;
        }

        var newBtn = target.closest('#pluginsList [data-newtask]');
        if (!newBtn) return;
        var pid = newBtn.getAttribute('data-newtask');
        var slot = newBtn.closest('.plugin-card').querySelector('.plugin-task-form-slot');
        if (slot.innerHTML) { slot.innerHTML = ''; return; }   // 再点一次收起
        fetch('/api/plugins/' + encodeURIComponent(pid) + '/schema')
            .then(function (r) { return r.json(); })
            .then(function (data) { slot.innerHTML = formHtml(pid, data.params || []); })
            .catch(function (err) {
                console.error('[plugins] schema 加载失败:', err);
                toast(t('js.plugins.schema_failed'), 'danger');
            });
    });

    document.addEventListener('submit', function (e) {
        var form = e.target && typeof e.target.closest === 'function'
            ? e.target.closest('.plugin-config-form') : null;
        if (!form) return;
        e.preventDefault();
        var pid = form.getAttribute('data-plugin');
        var body;
        try {
            body = configPayloadOf(form);
        } catch (err) {
            // 无 schema 的 JSON 回落分支里用户打错了括号。本地就能判，不必
            // 让服务端回一句更含糊的 400。
            showConfigErrors(form, { _: String(err) });
            return;
        }
        fetch('/api/plugins/' + encodeURIComponent(pid) + '/config', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        })
            .then(function (r) {
                return r.json().then(function (data) {
                    return { ok: r.ok, data: data };
                });
            })
            .then(function (res) {
                if (!res.ok || !res.data.success) {
                    // 400 的 `errors` 是逐键的，回显到对应字段；其余错误
                    // （404、500）只有一句 `error`，走 toast。
                    if (res.data.errors) {
                        showConfigErrors(form, res.data.errors);
                    } else {
                        toast(t('js.plugins.config_failed',
                                { reason: res.data.error || '' }), 'danger');
                    }
                    return;
                }
                showConfigErrors(form, {});
                toast(t('js.plugins.config_saved'), 'success');
            })
            .catch(function (err) {
                console.error('[plugins] 配置保存失败:', err);
                toast(t('js.plugins.config_failed',
                        { reason: String(err) }), 'danger');
            });
    });

    document.addEventListener('submit', function (e) {
        var form = e.target && typeof e.target.closest === 'function'
            ? e.target.closest('.plugin-task-form') : null;
        if (!form) return;
        e.preventDefault();
        var pid = form.getAttribute('data-plugin');
        var slot = form.closest('.plugin-task-form-slot');
        fetch('/api/plugins/' + encodeURIComponent(pid) + '/tasks', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payloadOf(form))
        })
            .then(function (r) {
                return r.json().then(function (data) {
                    return { ok: r.ok, data: data };
                });
            })
            .then(function (res) {
                if (!res.ok || !res.data.success) {
                    toast(t('js.plugins.create_failed',
                            { reason: res.data.error || '' }), 'danger');
                    return;
                }
                // 建成了但没起来是**两件事**：任务行已经落库（响应里有
                // task_id），提示不能说成创建失败。
                if (res.data.started === false) {
                    toast(t('js.plugins.created_not_started',
                            { reason: res.data.start_error || '' }), 'warning');
                } else {
                    toast(t('js.plugins.created_started'), 'success');
                }
                if (slot) slot.innerHTML = '';
            })
            .catch(function (err) {
                console.error('[plugins] 任务创建失败:', err);
                toast(t('js.plugins.create_failed', { reason: String(err) }), 'danger');
            });
    });

    // 懒初始化入口（panels.js 在面板首次打开时调）。
    window.initPlugins = function () { load(); };
})();
