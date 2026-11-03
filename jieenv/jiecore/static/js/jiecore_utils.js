function getAndReload(url, data, reloadOnSuccess) {
    layui.use('layer', function () {
        var layer = layui.layer;
        var loadIdx = layer.msg('正在处理，请稍候...', {
            icon: 16,
            shade: [0.3, '#000'],
            time: 0,
            offset: 'auto'
        });
        $.get(url, data).done(function (res) {
            layer.close(loadIdx);
            if (res && res.success) {
                layer.msg(res.message || '操作完成', {icon: 1, time: 2000}, function () {
                    if (reloadOnSuccess !== false) { location.reload(); }
                });
            } else {
                layer.msg((res && res.message) || '操作失败', {icon: 2, time: 3500});
            }
        }).fail(function () {
            layer.close(loadIdx);
            layer.msg('请求失败，请检查网络', {icon: 2, time: 3000});
        });
    });
}