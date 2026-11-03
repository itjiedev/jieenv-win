/**
 * JieCore 应用操作前端支撑脚本
 * 依赖: jQuery + layui（layout_base.html 已全局引入）
 *
 * 功能：
 * 1. 为带 data-ajax-submit 的表单绑定 AJAX 提交，提交期间显示 layui 加载遮罩
 * 2. 操作返回 restart_required 时自动调用重启接口，并轮询 ping 探测
 *    服务重启完成后自动跳转/刷新页面
 */
(function () {
    'use strict';

    function getCookie(name) {
        var match = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
        return match ? decodeURIComponent(match[2]) : '';
    }

    $.ajaxSetup({
        beforeSend: function (xhr, settings) {
            if (!/^(GET|HEAD|OPTIONS)$/i.test(settings.type)) {
                xhr.setRequestHeader('X-CSRFToken', getCookie('csrftoken'));
            }
        }
    });

    // API 地址从页面 meta/data 无法获知时，使用相对路径约定
    var API_PING = '/jiecore/api/ping/';
    var API_RESTART = '/jiecore/api/restart/';
    var API_SAVE_RESTART_URL = '/jiecore/api/save-restart-url/';

    var JieOps = {
        // 页面加载时记录当前服务的启动时间戳
        serviceStartTime: null,

        initStartTime: function () {
            $.get(API_PING).done(function (res) {
                if (res && res.start_time) {
                    JieOps.serviceStartTime = res.start_time;
                }
            });
        },

        /**
         * 触发后台重启并轮询探测，重启完成后跳转 redirect 页面
         */
        restartAndWait: function (redirect) {
            layui.use('layer', function () {
                var layer = layui.layer;
                var waitIdx = layer.msg('操作需要重启服务，正在重启，请稍候...', {
                    icon: 16, shade: [0.3, '#000'], time: 0
                });

                // 保存当前页面 URL，供桌面壳重启后导航回原页面（而非首页）
                var currentUrl = redirect || window.location.href;
                $.post(API_SAVE_RESTART_URL, {url: currentUrl});

                // 启动轮询函数（仅当服务确实在重启时调用）
                // manualMode=true 时，ping 失败会更新提示让用户手动重启
                function startPolling(manualMode) {
                    var tries = 0;
                    var maxTries = 60; // 最长约 2 分钟
                    var manualMsgShown = false;
                    var timer = setInterval(function () {
                        tries += 1;
                        if (tries >= maxTries) {
                            clearInterval(timer);
                            layer.close(waitIdx);
                            layer.msg('重启超时，请手动检查服务状态', {icon: 2, time: 5000});
                            return;
                        }
                        $.get(API_PING).done(function (res) {
                            // start_time 变化说明新进程已启动完成
                            if (res && res.status === 'ok' && res.start_time &&
                                res.start_time !== JieOps.serviceStartTime) {
                                clearInterval(timer);
                                layer.close(waitIdx);
                                layer.msg('服务已重启完成，正在跳转...', {icon: 1, time: 1200});
                                setTimeout(function () {
                                    window.location.href = redirect || window.location.href;
                                }, 800);
                            }
                        }).fail(function () {
                            // runserver 模式下服务已终止，仅首次更新提示
                            if (manualMode && !manualMsgShown) {
                                manualMsgShown = true;
                                layer.close(waitIdx);
                                waitIdx = layer.msg('服务已停止，请手动重新启动服务', {
                                    icon: 16, shade: [0.3, '#000'], time: 0
                                });
                            }
                        });
                    }, 2000);
                }

                $.post(API_RESTART).done(function (res) {
                    // runserver 模式：服务将自动终止，保持遮罩轮询等待手动重启后恢复
                    if (res && res.manual_restart_required) {
                        startPolling(true);
                        return;
                    }
                    // 服务正在重启，启动轮询等待
                    startPolling(false);
                }).fail(function () {
                    // 请求失败时仍启动轮询（服务可能正在重启中导致连接中断）
                    startPolling(false);
                });
            });
        },

        /**
         * 处理操作接口的 JSON 响应
         */
        handleResult: function (res, redirect) {
            layui.use('layer', function () {
                var layer = layui.layer;
                if (res && res.success) {
                    if (res.restart_required) {
                        layer.msg(res.message || '操作成功', {icon: 1, time: 1500});
                        setTimeout(function () {
                            JieOps.restartAndWait(redirect);
                        }, 600);
                    } else {
                        layer.msg(res.message || '操作成功', {icon: 1, time: 1200});
                        setTimeout(function () {
                            window.location.href = redirect || window.location.href;
                        }, 800);
                    }
                } else {
                    layer.msg((res && res.message) || '操作失败', {icon: 2, time: 4000});
                }
            });
        }
    };

    // 绑定所有 data-ajax-submit 表单
    $(document).on('submit', 'form[data-ajax-submit]', function (e) {
        e.preventDefault();
        var $form = $(this);
        if ($form.data('submitting')) {
            return;
        }
        var url = $form.data('url') || $form.attr('action') || window.location.href;
        var redirect = $form.data('redirect') || '';
        var formData = new FormData(this);

        layui.use('layer', function () {
            var layer = layui.layer;
            var loadIdx = layer.load(2, {shade: [0.3, '#000']});
            $form.data('submitting', true);

            $.ajax({
                url: url,
                type: 'POST',
                data: formData,
                dataType: 'json',
                processData: false,
                contentType: false
            }).done(function (res) {
                layer.close(loadIdx);
                $form.data('submitting', false);
                JieOps.handleResult(res, redirect);
            }).fail(function () {
                layer.close(loadIdx);
                $form.data('submitting', false);
                layer.msg('请求失败，请检查网络后重试', {icon: 2, time: 4000});
            });
        });
    });

    // 绑定顶部导航栏“重启服务”按钮（layout_main.html 渲染）
    $(document).on('click', '#btn-restart-server', function () {
        layui.use('layer', function () {
            var layer = layui.layer;
            layer.confirm('确定要重启服务吗？', {icon: 3, title: '重启服务'}, function (index) {
                layer.close(index);
                JieOps.restartAndWait(window.location.href);
            });
        });
    });
    //刷新页面
    $(document).on('click', '#btn-refresh-page', function () {
        window.location.href = window.location.href;
    });

    window.JieOps = JieOps;
    $(function () {
        JieOps.initStartTime();
    });
})();
