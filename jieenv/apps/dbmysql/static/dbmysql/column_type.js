/**
 * dbmysql 字段类型联动表单组件
 *
 * 根据后端 FIELD_TYPES 注册表生成类型下拉列表（按分组），
 * 选中类型后动态展示对应的配置项（长度 / 精度+小数位 / 枚举取值 / UNSIGNED）。
 *
 * 用法:
 *   var ct = JieColumnType.init({
 *     types:  window.JIE_FIELD_TYPES,   // 类型注册表（后端 mc.FIELD_TYPES）
 *     select: '#ct-type',               // 类型下拉框（选项由本组件生成）
 *     argBox: '#ct-arg-box',            // 参数区渲染容器
 *     tipBox: '#ct-type-tip'            // 类型说明显示元素（可选）
 *   });
 *   ct.fill({name: 'VARCHAR', args: ['50'], unsigned: false});  // 编辑回填
 *   var r = ct.getValue();               // {ok, message, sql}
 */
(function () {
    'use strict';

    // 已有字段可能出现的别名类型 → 注册表标准类型
    var ALIASES = {INTEGER: 'INT', NUMERIC: 'DECIMAL', BOOL: 'TINYINT', BOOLEAN: 'TINYINT'};

    function init(opts) {
        var $select = $(opts.select);
        var $argBox = $(opts.argBox);
        var $tip = opts.tipBox ? $(opts.tipBox) : null;
        var byName = {};

        // 生成下拉选项（按分组 optgroup，保持注册表顺序）
        $select.empty().append($('<option>').val('').text('-- 请选择字段类型 --'));
        var groupMap = {};
        $.each(opts.types, function (_, t) {
            byName[t.name] = t;
            if (!groupMap[t.group]) {
                groupMap[t.group] = $('<optgroup>').attr('label', t.group);
                $select.append(groupMap[t.group]);
            }
            groupMap[t.group].append($('<option>').val(t.name).text(t.name));
        });

        // 参数区控件（按类型显示/隐藏）
        $argBox.html(
            '<div class="form-group" id="ct-length-wrap" style="display:none">' +
            '  <label>长度 <span class="text-danger" id="ct-length-req"></span></label>' +
            '  <input type="number" min="1" class="form-control" id="ct-length">' +
            '</div>' +
            '<div class="form-group" id="ct-precision-wrap" style="display:none">' +
            '  <div class="row">' +
            '    <div class="col-6">' +
            '      <label>精度（总位数）</label>' +
            '      <input type="number" min="1" class="form-control" id="ct-precision">' +
            '    </div>' +
            '    <div class="col-6">' +
            '      <label>小数位数</label>' +
            '      <input type="number" min="0" class="form-control" id="ct-scale">' +
            '    </div>' +
            '  </div>' +
            '</div>' +
            '<div class="form-group" id="ct-values-wrap" style="display:none">' +
            '  <label>取值列表（逗号分隔）</label>' +
            '  <input type="text" class="form-control" id="ct-values" placeholder="如: 男,女,未知">' +
            '</div>' +
            '<div class="form-group" id="ct-unsigned-wrap" style="display:none">' +
            '  <div class="custom-control custom-checkbox">' +
            '    <input type="checkbox" class="custom-control-input" id="ct-unsigned">' +
            '    <label class="custom-control-label" for="ct-unsigned">UNSIGNED（无符号，不允许负数）</label>' +
            '  </div>' +
            '</div>'
        );

        function currentMeta() {
            return byName[$select.val()] || null;
        }

        function refresh() {
            var meta = currentMeta();
            $('#ct-length-wrap,#ct-precision-wrap,#ct-values-wrap,#ct-unsigned-wrap').hide();
            if ($tip) { $tip.text(meta ? (meta.tip || '') : ''); }
            if (!meta) { return; }
            if (meta.arg === 'length') {
                $('#ct-length-wrap').show();
                $('#ct-length-req').text(meta.arg_required ? '*' : '');
                $('#ct-length').attr('placeholder',
                    meta.arg_required ? '必填' : '可选，留空使用该类型默认值');
            } else if (meta.arg === 'precision') {
                $('#ct-precision-wrap').show();
            } else if (meta.arg === 'values') {
                $('#ct-values-wrap').show();
            }
            if (meta.unsigned) { $('#ct-unsigned-wrap').show(); }
        }

        $select.on('change', refresh);
        refresh();

        return {
            /** 编辑页回填: parsed = {name, args:[...], unsigned, zerofill} */
            fill: function (parsed) {
                if (!parsed || !parsed.name) { return; }
                var name = String(parsed.name).toUpperCase();
                if (!byName[name] && ALIASES[name]) { name = ALIASES[name]; }
                if (byName[name]) {
                    $select.val(name);
                    refresh();
                    var args = parsed.args || [];
                    var meta = currentMeta();
                    if (meta) {
                        if (meta.arg === 'length') {
                            $('#ct-length').val(args[0] || '');
                        } else if (meta.arg === 'precision') {
                            $('#ct-precision').val(args[0] || '');
                            $('#ct-scale').val(args[1] || '');
                        } else if (meta.arg === 'values') {
                            $('#ct-values').val(args.join(','));
                        }
                    }
                    $('#ct-unsigned').prop('checked', !!parsed.unsigned);
                }
            },

            /** 收集当前选择，生成类型 SQL 片段。返回 {ok, message, sql} */
            getValue: function () {
                var meta = currentMeta();
                if (!meta) { return {ok: false, message: '请选择字段类型', sql: ''}; }
                var sql = meta.name;

                if (meta.arg === 'length') {
                    var len = $.trim($('#ct-length').val() || '');
                    if (len) {
                        if (!/^\d+$/.test(len) || len === '0') {
                            return {ok: false, message: meta.name + ' 的长度必须为正整数', sql: ''};
                        }
                        sql += '(' + len + ')';
                    } else if (meta.arg_required) {
                        return {ok: false, message: meta.name + ' 必须填写长度', sql: ''};
                    }
                } else if (meta.arg === 'precision') {
                    var p = $.trim($('#ct-precision').val() || '');
                    var s = $.trim($('#ct-scale').val() || '');
                    if (p || s) {
                        if (!/^\d+$/.test(p) || p === '0') {
                            return {ok: false, message: meta.name + ' 的精度（总位数）必须为正整数', sql: ''};
                        }
                        if (s && (!/^\d+$/.test(s) || Number(s) > Number(p))) {
                            return {ok: false, message: '小数位数必须为不大于精度的非负整数', sql: ''};
                        }
                        sql += '(' + p + (s ? ',' + s : '') + ')';
                    }
                } else if (meta.arg === 'values') {
                    var raw = $.trim($('#ct-values').val() || '');
                    if (!raw) { return {ok: false, message: meta.name + ' 必须填写取值列表', sql: ''}; }
                    var quoted = [];
                    $.each(raw.split(/[,，]/), function (_, v) {
                        v = $.trim(v).replace(/^'+|'+$/g, '');
                        if (v !== '') { quoted.push("'" + v.replace(/'/g, "''") + "'"); }
                    });
                    if (!quoted.length) {
                        return {ok: false, message: meta.name + ' 的取值列表不能为空', sql: ''};
                    }
                    sql += '(' + quoted.join(',') + ')';
                }

                if (meta.unsigned && $('#ct-unsigned').is(':checked')) {
                    sql += ' UNSIGNED';
                }
                return {ok: true, message: '', sql: sql};
            }
        };
    }

    window.JieColumnType = {init: init};
})();
