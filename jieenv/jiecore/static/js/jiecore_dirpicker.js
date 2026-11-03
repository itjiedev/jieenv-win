/**
 * JieCore 通用目录/文件选择器（资源管理器风格）
 * 依赖: jQuery + layui（layout_base.html 已全局引入）
 *
 * 用法:
 *     JieDirPicker.open({
 *         title: '选择目录',              // 可选，弹窗标题
 *         initialPath: 'D:\\Programs',   // 可选，初始目录（文件模式下可为文件全路径），为空从盘符开始
 *         mode: 'dir',                    // 可选，'dir' 选目录（默认）/ 'file' 选文件
 *         fileTypes: ['.zip', '.ini'],   // 可选，文件模式下的扩展名过滤，缺省不限类型
 *         onSelect: function (path) {}   // 用户确认选择后的回调
 *     });
 *
 * 后端接口: GET /jiecore/api/fs/dirs/?path=xxx[&files=1&exts=.zip,.ini]（jiecore.api_fs_dirs）
 */
(function () {
    'use strict';

    var API_FS_DIRS = '/jiecore/api/fs/dirs/';

    function esc(text) {
        return $('<span>').text(text == null ? '' : String(text)).html();
    }

    function joinPath(base, name) {
        if (!base) {
            return name;                       // 盘符条目本身即完整路径（如 D:\）
        }
        if (/[\\/]$/.test(base)) {
            return base + name;
        }
        return base + '\\' + name;
    }

    // 按扩展名给文件配图标
    function fileIcon(name) {
        var ext = (name.split('.').pop() || '').toLowerCase();
        if (ext === 'zip' || ext === 'rar' || ext === '7z' || ext === 'tar' || ext === 'gz') {
            return 'file-zip-o';
        }
        if (ext === 'txt' || ext === 'log' || ext === 'ini' || ext === 'md' || ext === 'json') {
            return 'file-text-o';
        }
        return 'file-o';
    }

    function open(options) {
        options = options || {};
        var fileMode = options.mode === 'file';
        var exts = (options.fileTypes || []).filter(Boolean).join(',');

        layui.use('layer', function () {
            var layer = layui.layer;

            var state = {
                current: '',     // 当前浏览目录（空 = 盘符列表）
                parent: '',
                selected: ''     // 当前选中路径（文件模式下只能是文件）
            };

            var html =
                '<div style="padding:10px 15px;">' +
                '  <div class="input-group input-group-sm mb-2">' +
                '    <div class="input-group-prepend">' +
                '      <button class="btn btn-outline-secondary" type="button" id="jdp-up" title="上级目录">' +
                '        <i class="fa fa-arrow-up"></i></button>' +
                '    </div>' +
                '    <input type="text" id="jdp-path-input" class="form-control" placeholder="可直接输入路径后回车跳转">' +
                '    <div class="input-group-append">' +
                '      <button class="btn btn-outline-secondary" type="button" id="jdp-goto">跳转</button>' +
                '    </div>' +
                '  </div>' +
                '  <div id="jdp-list" style="height:320px;overflow-y:auto;border:1px solid #dee2e6;' +
                '       border-radius:3px;background:#fff;"></div>' +
                '  <div id="jdp-selected" class="text-muted mt-2" style="font-size:12px;word-break:break-all;"></div>' +
                '</div>';

            var layerIdx = layer.open({
                type: 1,
                title: options.title || (fileMode ? '选择文件' : '选择目录'),
                area: ['600px'],
                content: html,
                btn: ['确定选择', '取消'],
                success: function () {
                    var initial = options.initialPath || '';
                    load(initial);
                },
                yes: function () {
                    if (fileMode) {
                        confirmPick(state.selected);
                        return;
                    }
                    // 目录模式：优先用户显式选中/浏览到的目录；若都没有（例如目录
                    // 不存在回退到盘符列表、state.current 为空），则采用输入框中用户
                    // 键入的路径，尊重“除非点确定，否则不修改用户已输入内容”的约定
                    confirmPick(state.selected || state.current ||
                        $('#jdp-path-input').val().trim());
                }
            });

            function confirmPick(picked) {
                if (!picked) {
                    layer.msg(fileMode ? '请先选择一个文件' : '请先选择一个目录', {icon: 2});
                    return;
                }
                layer.close(layerIdx);
                if (typeof options.onSelect === 'function') {
                    options.onSelect(picked);
                }
            }

            function load(path) {
                var $list = $('#jdp-list');
                $list.html('<div class="text-center text-muted py-4">加载中...</div>');
                var params = {path: path};
                if (fileMode) {
                    params.files = 1;
                    if (exts) {
                        params.exts = exts;
                    }
                }
                $.get(API_FS_DIRS, params).done(function (res) {
                    if (!res || !res.success) {
                        $list.html('<div class="text-center text-danger py-4">' +
                            esc((res && res.message) || '加载失败') + '</div>');
                        return;
                    }
                    state.current = res.path;
                    state.parent = res.parent;
                    if (res.selected) {
                        // 跳转时直接给了文件全路径：后端已定位到父目录，回填选中态
                        state.selected = joinPath(res.path, res.selected);
                    } else if (state.selected && state.selected !== res.path &&
                        state.selected.indexOf(res.path) !== 0) {
                        state.selected = '';
                    }
                    // 目录不存在回退盘符列表时（res.fallback），保留用户键入/请求的路径，
                    // 不用盘符根（空）覆盖输入框；其余情况正常回填当前目录
                    $('#jdp-path-input').val(res.fallback ? (res.requested || '') : res.path);
                    $('#jdp-up').prop('disabled', !res.parent && !res.path);
                    render(res.dirs, res.files || []);
                }).fail(function () {
                    $list.html('<div class="text-center text-danger py-4">请求失败</div>');
                });
            }

            function itemHtml(fullPath, name, icon, kind, color) {
                return '<div class="jdp-item d-flex align-items-center px-2 py-1" ' +
                    'style="cursor:pointer;border-bottom:1px solid #f1f1f1;" ' +
                    'data-kind="' + kind + '" data-path="' + esc(fullPath) + '">' +
                    '<i class="fa fa-' + icon + ' ' + color + ' mr-2"></i>' +
                    '<span style="word-break:break-all;">' + esc(name) + '</span></div>';
            }

            function render(dirs, files) {
                var $list = $('#jdp-list');
                if (!dirs.length && !files.length) {
                    $list.html('<div class="text-center text-muted py-4">' +
                        (fileMode ? '（无子目录和匹配文件）' : '（无子目录）') + '</div>');
                    updateSelectedTip();
                    return;
                }
                var rows = dirs.map(function (name) {
                    var isDrive = /^[A-Za-z]:\\$/.test(name);
                    return itemHtml(joinPath(state.current, name), name,
                        isDrive ? 'hdd' : 'folder', 'dir', 'text-warning');
                });
                if (fileMode) {
                    rows = rows.concat(files.map(function (name) {
                        return itemHtml(joinPath(state.current, name), name,
                            fileIcon(name), 'file', 'text-muted');
                    }));
                }
                $list.html(rows.join(''));
                highlight();
                updateSelectedTip();
            }

            function highlight() {
                $('#jdp-list .jdp-item').each(function () {
                    var $it = $(this);
                    if ($it.data('path') === state.selected) {
                        $it.css('background', '#e7f1ff');
                    } else {
                        $it.css('background', '');
                    }
                });
            }

            function updateSelectedTip() {
                var emptyTip = fileMode
                    ? '未选择文件（双击进入目录，单击选中文件' +
                      (exts ? '，仅显示 ' + esc(exts) + ' 类型' : '') + '）'
                    : '未选择目录（双击进入，单击选中；不选中则提交当前目录）';
                $('#jdp-selected').html(state.selected
                    ? '已选择: <b>' + esc(state.selected) + '</b>'
                    : emptyTip);
            }

            // 单击选中（文件模式下目录不可选，仅用于进入），双击进入目录/确认文件
            $('#jdp-list').on('click', '.jdp-item', function () {
                var $it = $(this);
                if (fileMode && $it.data('kind') === 'dir') {
                    return;
                }
                state.selected = $it.data('path');
                highlight();
                updateSelectedTip();
            }).on('dblclick', '.jdp-item', function () {
                var $it = $(this);
                if ($it.data('kind') === 'dir') {
                    load($it.data('path'));
                } else if (fileMode) {
                    confirmPick($it.data('path'));
                }
            });

            $('#jdp-up').on('click', function () {
                if (state.parent || state.current) {
                    load(state.parent || '');
                }
            });

            $('#jdp-goto').on('click', function () {
                load($('#jdp-path-input').val().trim());
            });
            $('#jdp-path-input').on('keydown', function (e) {
                if (e.key === 'Enter') {
                    load($(this).val().trim());
                }
            });
        });
    }

    window.JieDirPicker = {open: open};
})();
