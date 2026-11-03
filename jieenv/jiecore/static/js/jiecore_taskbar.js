/**
 * JieCore 全局任务指示器（导航栏图标 + 下拉面板）
 * 依赖: jQuery + layui + JieTaskLog（layout_base.html 已全局引入）
 *
 * 功能：
 *   - 在导航栏右侧显示任务图标（fa-tasks）+ 活跃任务数 badge
 *   - 定时轮询 /jiecore/api/task-list/ 获取活跃 + 近期任务
 *   - 点击图标展开下拉面板，列出所有任务（活跃在前、已完成在后）
 *   - 点击任一任务项调用 JieTaskLog.open({taskId}) 重新打开日志弹窗
 *
 * 初始化：页面加载后调用 JieTaskBar.init()（layout_base.html 已自动调用）
 */
(function () {
    'use strict';

    var API_URL = '/jiecore/api/task-list/';
    var POLL_INTERVAL = 3000;   // 轮询间隔（毫秒）
    var _timer = null;          // 轮询定时器
    var _panelIdx = -1;         // 当前下拉面板的 layer 索引，-1 表示未打开
    var _layer = null;          // layui.layer 缓存
    var _prevTasksKey = '';     // 上次渲染的任务列表快照，用于变化检测避免面板闪动

    // ── 状态 → 图标 / 颜色 ──
    var STATUS_MAP = {
        pending:  {icon: 'fa-clock',          color: '#e6a23c', label: '等待中'},
        running:  {icon: 'fa-circle-notch fa-spin', color: '#409eff', label: '执行中'},
        success:  {icon: 'fa-check-circle',   color: '#67c23a', label: '已完成'},
        error:    {icon: 'fa-times-circle',   color: '#f56c6a', label: '失败'}
    };

    /**
     * 获取任务展示标题：优先用后端 title，为空时展示任务类型
     * （jiecore 是框架，不维护业务类型映射，由各应用 create_task 时传入 title）
     */
    function getDisplayTitle(task) {
        if (task.title) return task.title;
        return task.type || '未知任务';
    }

    /**
     * 获取状态展示信息
     */
    function getStatusInfo(status) {
        return STATUS_MAP[status] || {icon: 'fa-question', color: '#909399', label: status};
    }

    /**
     * 构建单条任务 HTML
     */
    function buildItemHtml(task) {
        var si = getStatusInfo(task.status);
        var title = getDisplayTitle(task);
        var isActive = (task.status === 'pending' || task.status === 'running');
        var borderClr = isActive ? si.color : 'transparent';
        var progressHtml = '';

        if (isActive && task.progress > 0) {
            progressHtml = '<div style="margin-top:4px">'
                + '<div style="background:#e9ecef;border-radius:2px;height:4px;overflow:hidden">'
                + '<div style="width:' + task.progress + '%;height:100%;background:' + si.color
                + ';transition:width .3s"></div></div></div>';
        }

        var stageHtml = '';
        if (isActive && task.stage) {
            stageHtml = '<div style="font-size:12px;color:#999;margin-top:2px">'
                + task.stage + '</div>';
        }

        var resultHtml = '';
        if (!isActive && task.result && task.result.message) {
            var rColor = task.result.success ? '#67c23a' : '#f56c6a';
            resultHtml = '<div style="font-size:12px;color:' + rColor
                + ';margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'
                + task.result.message + '</div>';
        }

        return '<div class="jie-taskbar-item" data-task-id="' + task.task_id + '"'
            + ' style="padding:10px 14px;cursor:pointer;border-bottom:1px solid #f0f0f0;'
            + 'border-left:3px solid ' + borderClr + ';transition:background .15s"'
            + ' onmouseover="this.style.background=\'#f5f7fa\'"'
            + ' onmouseout="this.style.background=\'\'">'
            + '<div style="display:flex;align-items:center">'
            + '<i class="fas ' + si.icon + '" style="color:' + si.color
            + ';margin-right:8px;font-size:15px;flex-shrink:0"></i>'
            + '<div style="flex:1;min-width:0">'
            + '<div style="font-size:14px;color:#333;white-space:nowrap;'
            + 'overflow:hidden;text-overflow:ellipsis">' + title + '</div>'
            + '<div style="font-size:12px;color:#aaa;margin-top:1px">'
            + si.label + (task.created_at ? ' · ' + task.created_at : '') + '</div>'
            + progressHtml + stageHtml + resultHtml
            + '</div></div></div>';
    }

    /**
     * 构建下拉面板内容
     */
    function buildPanelHtml(tasks) {
        var active = [], finished = [];
        for (var i = 0; i < tasks.length; i++) {
            var s = tasks[i].status;
            if (s === 'pending' || s === 'running') active.push(tasks[i]);
            else finished.push(tasks[i]);
        }

        var html = '<div style="max-height:600px;overflow-y:auto">';

        // 任务列表
        if (tasks.length === 0) {
            html += '<div style="padding:24px;text-align:center;color:#bbb;font-size:14px">'
                + '暂无任务记录</div>';
        } else {
            for (var j = 0; j < active.length; j++) {
                html += buildItemHtml(active[j]);
            }
            if (finished.length > 0 && active.length > 0) {
                html += '<div style="padding:4px 14px;font-size:12px;color:#bbb;'
                    + 'background:#fafafa;border-bottom:1px solid #f0f0f0">近期完成</div>';
            }
            for (var k = 0; k < finished.length; k++) {
                html += buildItemHtml(finished[k]);
            }
        }

        html += '</div>';
        return html;
    }

    /**
     * 更新导航栏图标上的 badge 数字
     */
    function updateBadge(count) {
        var $badge = $('#jie-taskbar-badge');
        if (count > 0) {
            $badge.text(count > 99 ? '99+' : count).show();
        } else {
            $badge.hide();
        }
    }

    /**
     * 关闭下拉面板
     */
    function closePanel() {
        if (_panelIdx >= 0 && _layer) {
            _layer.close(_panelIdx);
            _panelIdx = -1;
        }
    }

    /**
     * 打开下拉面板
     */
    function openPanel(tasks) {
        closePanel();
        _prevTasksKey = JSON.stringify(tasks);
        // 标题栏携带活跃任务数
        var activeCount = 0;
        for (var i = 0; i < tasks.length; i++) {
            if (tasks[i].status === 'pending' || tasks[i].status === 'running') activeCount++;
        }
        var panelTitle = activeCount > 0
            ? '任务 <span style="background:#409eff;color:#fff;font-size:11px;'
              + 'padding:1px 8px;border-radius:10px;margin-left:6px">'
              + activeCount + ' 个进行中</span>'
            : '任务';
        _panelIdx = _layer.open({
            type: 1,
            title: panelTitle,
            closeBtn: 1,
            shade: 0.3,
            shadeClose: false,
            area: '500px',
            fixed: true,
            content: buildPanelHtml(tasks),
            success: function (layero) {
                // 点击任务项 → 打开日志弹窗
                layero.find('.jie-taskbar-item').on('click', function () {
                    var taskId = $(this).data('task-id');
                    if (taskId) {
                        JieTaskLog.open({taskId: taskId});
                    }
                });
            }
        });
    }

    /**
     * 轮询任务列表
     */
    function poll() {
        $.getJSON(API_URL).done(function (res) {
            if (!res || !res.success) return;
            var tasks = res.tasks || [];
            var activeCount = 0;
            for (var i = 0; i < tasks.length; i++) {
                if (tasks[i].status === 'pending' || tasks[i].status === 'running') {
                    activeCount++;
                }
            }
            updateBadge(activeCount);

            // 面板打开时仅在任务数据变化时刷新，避免每次轮询都关闭+重开导致闪动
            if (_panelIdx >= 0) {
                var tasksKey = JSON.stringify(tasks);
                if (tasksKey !== _prevTasksKey) {
                    _prevTasksKey = tasksKey;
                    closePanel();
                    if (tasks.length > 0) {
                        openPanel(tasks);
                    }
                }
            }
        }).fail(function () {
            // 网络瞬时错误，静默等待下次轮询
        });
    }

    // ── 公开接口 ──

    function init() {
        layui.use('layer', function () {
            _layer = layui.layer;

            // 点击导航栏图标：切换面板
            $(document).on('click', '#jie-taskbar-btn', function (e) {
                e.preventDefault();
                e.stopPropagation();
                if (_panelIdx >= 0) {
                    closePanel();
                } else {
                    // 立即拉一次最新数据再展示面板
                    $.getJSON(API_URL).done(function (res) {
                        if (res && res.success) {
                            openPanel(res.tasks || []);
                        }
                    });
                }
            });

            // 启动轮询
            poll();
            _timer = setInterval(poll, POLL_INTERVAL);
        });
    }

    function destroy() {
        if (_timer) {
            clearInterval(_timer);
            _timer = null;
        }
        closePanel();
    }

    window.JieTaskBar = {init: init, destroy: destroy};
})();
