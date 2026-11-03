/**
 * JieCore 通用任务日志弹窗（黑底白字滚动日志）
 * 依赖: jQuery + layui（layout_base.html 已全局引入）
 *
 * 适用场景：后台任务执行过程中需要向用户实时展示逐步日志。
 * 调用方只需创建一个后台任务（POST 返回 task_id），然后将 task_id
 * 与状态轮询地址交给 JieTaskLog，组件自动完成弹窗、轮询、日志追加、
 * 状态图标切换、完成/失败回调等全部 UI 逻辑。
 *
 * 用法:
 *     // 1. 先发起 POST 创建任务，拿到 task_id
 *     $.post('/myapp/api/start-task/', {id: 123}).done(function (res) {
 *         if (!res || !res.success) { alert(res.message); return; }
 *
 *         // 2. 打开日志弹窗，开始轮询
 *         //    任务由 jiecore.tasks 登记时，只传 taskId 即可（走 jiecore 通用状态接口）
 *         JieTaskLog.open({
 *             title: '执行任务',                          // 弹窗标题
 *             taskId: res.task_id,                        // 任务 ID
 *             onSuccess: function () {                    // 可选，任务成功回调
 *                 setTimeout(function () { window.location.reload(); }, 300);
 *             },
 *             onFail: function (result) {                 // 可选，任务失败回调
 *                 console.log('失败原因', result && result.message);
 *             }
 *         });
 *     });
 *
 *     // 应用自己提供了状态接口时，改用 taskUrl 指定完整轮询地址（二者取其一，
 *     // taskUrl 优先）。无论走哪个地址，返回体都须符合下方约定
 *     JieTaskLog.open({title: '执行任务', taskUrl: '/myapp/api/task-status/' + res.task_id + '/'});
 *
 * 后端任务状态接口约定（GET，返回 JSON）:
 *     {
 *         success: true,
 *         status: 'running' | 'pending' | 'success' | 'error',
 *         stage: '当前阶段描述',
 *         logs: ['新增日志行1', '新增日志行2'],
 *         log_index: 15,           // 累计日志行数（下次请求传 after 参数实现增量）
 *         result: { success: true/false, message: '...' }   // 任务结束时存在
 *     }
 */
(function () {
    'use strict';

    var API_TASK_STATUS = '/jiecore/api/task-status/';

    /**
     * 打开任务日志弹窗
     * @param {Object} options
     * @param {string} options.title       弹窗标题（默认"任务日志"）
     * @param {string} [options.taskId]    任务 ID，走 jiecore 通用状态接口
     *                                     /jiecore/api/task-status/<task_id>/
     * @param {string} [options.taskUrl]   任务状态轮询地址（含 task_id），应用自建
     *                                     状态接口时使用；与 taskId 同时给出时优先用它
     * @param {string} [options.width]     弹窗宽度（默认 560px）
     * @param {string} [options.height]    弹窗高度（默认 420px）
     * @param {number} [options.interval]  轮询间隔毫秒（默认 1000）
     * @param {number} [options.logHeight] 日志区域高度（默认 230px）
     * @param {Function} [options.onSuccess] 任务成功后的回调
     * @param {Function} [options.onFail]    任务失败后的回调，参数为 result 对象
     */
    function open(options) {
        options = options || {};
        var title = options.title || '任务日志';
        var taskUrl = options.taskUrl || '';
        var taskId = options.taskId || '';
        // 未指定轮询地址时用 jiecore 的通用任务状态接口（后端
        // jiecore.views.api_task_status），应用只需回传 task_id 即可，无需自建视图。
        // 末尾补斜杠：Django 路由带斜杠，少了会多一次 APPEND_SLASH 重定向往返
        if (!taskUrl && taskId) {
            taskUrl = API_TASK_STATUS + encodeURIComponent(taskId) + '/';
        }
        var width = options.width || '560px';
        var height = options.height || '420px';
        var interval = options.interval || 1000;
        var logHeight = options.logHeight || 250;
        var onSuccess = typeof options.onSuccess === 'function' ? options.onSuccess : null;
        var onFail = typeof options.onFail === 'function' ? options.onFail : null;

        if (!taskUrl) {
            console.error('JieTaskLog.open: taskId 与 taskUrl 不能同时为空');
            return;
        }

        layui.use('layer', function () {
            var layer = layui.layer;

            var content = '<div style="padding:10px 12px">'
                + '<div style="margin-bottom:8px;color:#6c757d">'
                + '<i class="fas fa-circle-notch fa-spin jie-task-icon" style="font-size:16px"></i> '
                + '<span class="jie-task-text">正在加载...</span></div>'
                + '<pre class="jie-task-logs" style="height:' + logHeight + 'px;overflow-y:auto;'
                + 'background:#111;color:#eee;padding:10px;font-size:12px;margin:0;'
                + 'white-space:pre-wrap;font-family:Consolas,monospace"></pre>'
                + '</div>';

            var stopped = false;   // 弹窗关闭后置真，用于终止轮询链

            var boxIdx = layer.open({
                type: 1, title: title, area: [width, height],
                // 允许用户随时用右上角 X 关闭：任务在后台线程继续跑，关窗不影响执行，
                // 但原先 closeBtn:0 + shadeClose:false + btn:false 让弹窗只能靠“确定”
                // 按钮关闭，一旦任务卡在失败分支就再也关不掉
                shadeClose: false, closeBtn: 1, btn: false, content: content,
                end: function () { stopped = true; }
            });

            var $box = $('#layui-layer' + boxIdx);
            var $logs = $box.find('.jie-task-logs');
            var $icon = $box.find('.jie-task-icon');
            var $stateText = $box.find('.jie-task-text');
            var after = 0;

            function appendLines(lines) {
                if (!lines || !lines.length) return;
                $.each(lines, function (i, line) {
                    $logs.append(document.createTextNode(line + '\n'));
                });
                $logs.scrollTop($logs[0].scrollHeight);
            }

            function finish(ok, doneText) {
                $icon.removeClass('fa-circle-notch fa-spin')
                     .addClass(ok ? 'fa-check' : 'fa-times')
                     .css('color', ok ? '#67c23a' : '#f56c6a');
                $stateText.text(doneText).css('color', ok ? '#67c23a' : '#f56c6a');
            }

            function showButton(text, btnClass, callback) {
                var $btn = $('<div class="text-center"><button class="btn ' + btnClass + '">' + text + '</button></div>');
                $btn.on('click', function () {
                    layer.close(boxIdx);
                    if (callback) callback();
                });
                $logs.parent().append($btn);
            }

            function buildUrl(base, afterVal) {
                // 兼容 URL 末尾有无斜杠、有无已有查询参数
                var sep = base.indexOf('?') >= 0 ? '&' : '?';
                return base + sep + 'after=' + afterVal;
            }

            function poll() {
                if (stopped) return;
                $.get(buildUrl(taskUrl, after)).done(function (s) {
                    // 响应回来时弹窗可能已被用户关闭，此时 DOM 已销毁，不可再操作
                    if (stopped) return;
                    if (!s || !s.success) {
                        appendLines(['任务状态查询失败']);
                        finish(false, '执行失败');
                        if (onFail) onFail(null);
                        return;
                    }
                    after = s.log_index;
                    appendLines(s.logs);

                    if (s.status === 'running' || s.status === 'pending') {
                        $stateText.text(s.stage || '执行中，请稍候...');
                        setTimeout(poll, interval);
                    } else if (s.status === 'success') {
                        finish(true, '执行成功');
                        if (onSuccess) {
                            // 有回调时显示“确定”按钮，点击后执行回调
                            showButton('确定', 'btn-success mt-2', onSuccess);
                        }
                        // 无回调时不自动关闭，用户通过右上角 X 按钮手动关闭
                    } else {
                        // error / failed 等
                        finish(false, '执行失败');
                        if (onFail) {
                            // AdminLTE 3 / Bootstrap 4 已无 btn-default（改名 btn-secondary），
                            // 沿用旧类名会让按钮只剩 .btn 基础样式、视觉上近乎纯文本
                            showButton('确定', 'btn-secondary mt-2', function () { onFail(s.result); });
                        } else {
                            showButton('确定', 'btn-secondary mt-2', null);
                        }
                    }
                }).fail(function () {
                    if (stopped) return;
                    // 网络瞬时错误，不立即放弃，2 秒后重试
                    setTimeout(poll, 2000);
                });
            }

            // 开始轮询
            poll();
        });
    }

    window.JieTaskLog = {open: open};
})();
