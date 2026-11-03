"""
后台任务注册表（内存级）—— 各应用共用的公共能力。

长耗时操作（下载安装、服务启停、卸载等）放到后台线程执行，前端轮询取状态与
增量日志，配合 static/js/jiecore_tasklog.js 的 JieTaskLog 弹窗即可实时展示
逐步进度。

原先各应用各自维护一份结构相同的 tasks.py（dbmysql、lang_python 等逐字重复），
轮询视图也在应用内重复实现（dbmysql 的 api_task_status 与 api_install_status
返回体完全一致），故提升为 jiecore 公共模块：
- 后端：本模块提供任务注册表与统一的状态载荷构造函数 task_payload()，
  jiecore.views.api_task_status 提供开箱即用的轮询接口，路由为
  /jiecore/api/task-status/<task_id>/；
- 前端：JieTaskLog.open({title: '...', taskId: res.task_id}) 即可免配置轮询，
  应用自己的状态接口也可继续用 taskUrl 指定。

应用侧典型用法：
    from jiecore import tasks

    @require_POST
    def api_do_something(request):
        task = tasks.create_task('my_type', meta={'id': 1})
        tasks.run_in_thread(task, _worker, arg1, arg2)
        return JsonResponse({'success': True, 'task_id': task['task_id']})

    def _worker(task, arg1, arg2):
        # 首参固定为 task；抛异常即视为失败，无需自己兜状态
        tasks.append_log(task, '开始处理...')
        tasks.set_progress(task, progress=50, stage='处理中')
        ...
        tasks.set_result(task, {'success': True, 'message': '处理完成'})

任务只存在内存中，服务重启即全部丢失（长耗时操作本身也不适合跨重启续跑）。
已结束的任务超过 TASK_TTL_SECONDS 后由 create_task 惰性回收，避免长时间运行的
服务里注册表无限膨胀；仍在执行的任务永不被回收。

本模块不依赖 Django（不读 settings、不碰模型），可在 django.setup() 之前导入。
"""
import uuid
import time
import threading
from datetime import datetime

# 每个任务最多保留的日志行数，防止内存膨胀
MAX_LOG_LINES = 2000
# 已结束任务的保留秒数，超期由 create_task 惰性回收
TASK_TTL_SECONDS = 3600
# 两次惰性回收的最小间隔，避免高频建任务时反复扫描注册表
_CLEAN_INTERVAL = 60.0

# 任务状态取值，与前端 JieTaskLog 的判定分支严格对应
STATUS_PENDING = 'pending'
STATUS_RUNNING = 'running'
STATUS_SUCCESS = 'success'
STATUS_ERROR = 'error'
_FINISHED_STATUSES = (STATUS_SUCCESS, STATUS_ERROR)

_TASKS = {}
_LOCK = threading.Lock()
_last_clean = 0.0


def _cleanup_locked(now: float):
    """回收已结束且超期的任务（调用方须已持有 _LOCK）。

    只按 finished_at 判定，状态仍为 pending/running 的任务无论多久都不回收：
    下载类任务跑上几十分钟是正常的，误回收会让前端轮询直接收到“任务不存在”。
    """
    global _last_clean
    if now - _last_clean < _CLEAN_INTERVAL:
        return
    _last_clean = now
    expired = [tid for tid, t in _TASKS.items()
               if t.get('status') in _FINISHED_STATUSES
               and now - (t.get('finished_at') or now) > TASK_TTL_SECONDS]
    for tid in expired:
        del _TASKS[tid]


def create_task(task_type: str, meta: dict = None, title: str = '') -> dict:
    """创建任务记录，返回任务 dict（含 task_id）。

    title 是可选的人类可读标题（如"安装 Python 3.13"），供全局任务指示器
    （JieTaskBar）展示；未传时前端按 type + meta 自行推断。
    """
    now = time.time()
    task = {
        'task_id': uuid.uuid4().hex[:12],
        'type': task_type,
        'title': title,
        'status': STATUS_PENDING,   # pending / running / success / error
        'progress': 0,              # 0-100
        'stage': '',                # 当前阶段描述
        'logs': [],
        'meta': meta or {},
        'result': {},
        'created_at': datetime.now().strftime('%H:%M:%S'),
        # log_seq 为累计追加行数（只增不退），log_dropped 为因超出 MAX_LOG_LINES
        # 被丢弃的行数。增量拉取靠 log_seq 而非 len(logs)：日志被从头截断后
        # len(logs) 会恒定停在上限，用它做游标会让客户端此后再也收不到新行
        'log_seq': 0,
        'log_dropped': 0,
        'finished_at': 0.0,         # 结束时刻（epoch 秒），供 TTL 回收判定
    }
    with _LOCK:
        _cleanup_locked(now)
        _TASKS[task['task_id']] = task
    return task


def get_task(task_id: str) -> dict:
    """按 task_id 取任务，不存在返回 None"""
    with _LOCK:
        return _TASKS.get(task_id)


def list_tasks(recent_finished_minutes: int = 30) -> list:
    """返回当前所有活跃任务 + 近期已完成任务的摘要列表。

    供全局任务指示器（JieTaskBar）轮询展示，返回字段：
    [{task_id, type, title, status, progress, stage, meta, result, created_at}]

    活跃任务（pending/running）始终返回；已完成任务（success/error）只返回
    finished_at 在 recent_finished_minutes 以内的，避免列表无限增长。
    """
    now = time.time()
    cutoff = now - recent_finished_minutes * 60
    result = []
    with _LOCK:
        for t in _TASKS.values():
            status = t.get('status', '')
            if status in _FINISHED_STATUSES:
                if (t.get('finished_at') or 0) < cutoff:
                    continue
            result.append({
                'task_id': t['task_id'],
                'type': t.get('type', ''),
                'title': t.get('title', ''),
                'status': status,
                'progress': t.get('progress', 0),
                'stage': t.get('stage', ''),
                'meta': t.get('meta', {}),
                'result': t.get('result', {}),
                'created_at': t.get('created_at', ''),
            })
    # 活跃的排前面，同组内按创建时间倒序（最近创建排在最前）
    result.sort(key=lambda x: (0 if x['status'] in ('pending', 'running') else 1,
                               -int(x.get('created_at', '00:00:00')
                                    .replace(':', ''))))
    return result


def append_log(task: dict, message: str):
    """追加一行日志（线程安全），自动加时间戳并按 MAX_LOG_LINES 从头截断"""
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
    with _LOCK:
        task['log_seq'] = task.get('log_seq', 0) + 1
        task['logs'].append(line)
        overflow = len(task['logs']) - MAX_LOG_LINES
        if overflow > 0:
            del task['logs'][:overflow]
            task['log_dropped'] = task.get('log_dropped', 0) + overflow


def set_progress(task: dict, progress: int = None, stage: str = None, status: str = None):
    """更新进度 / 阶段 / 状态（线程安全），三者均可选，只改传入的那些"""
    with _LOCK:
        if progress is not None:
            task['progress'] = max(0, min(100, int(progress)))
        if stage is not None:
            task['stage'] = stage
        if status is not None:
            task['status'] = status
            if status in _FINISHED_STATUSES and not task.get('finished_at'):
                task['finished_at'] = time.time()


def set_result(task: dict, result: dict):
    """写入任务结果（线程安全）。

    直接赋值 task['result'] = ... 同样可用（既有应用代码就是这么写的），但那样
    绕过锁且不会记 finished_at，新代码推荐统一走本函数。
    只写结果不改状态：成败由 run_in_thread 按是否抛异常决定，或调用方自行
    set_progress(status=...)。
    """
    with _LOCK:
        task['result'] = result or {}
        if not task.get('finished_at'):
            task['finished_at'] = time.time()


def run_in_thread(task: dict, func, *args, **kwargs):
    """在后台线程执行 func(task, *args, **kwargs)，自动维护任务状态。

    func 首参固定为 task；正常返回且状态仍为 running 时置 success 并把进度补满，
    抛异常则记一行失败日志并置 error。线程为 daemon，不阻塞进程退出。
    返回 Thread 对象，需要 join 的调用方自行处理。
    """
    def _wrapper():
        set_progress(task, status=STATUS_RUNNING)
        try:
            func(task, *args, **kwargs)
            if task['status'] == STATUS_RUNNING:
                set_progress(task, progress=100, status=STATUS_SUCCESS)
        except Exception as e:
            append_log(task, f'任务失败: {e}')
            set_progress(task, status=STATUS_ERROR)

    t = threading.Thread(target=_wrapper, daemon=True)
    t.start()
    return t


def task_payload(task: dict, after: int = 0) -> dict:
    """构造轮询接口的返回体（含增量日志），字段与 jiecore_tasklog.js 约定一致：
    {success, status, progress, stage, logs, log_index, result}

    after 是客户端上次收到的 log_index；log_index 为累计追加行数（只增不退），
    客户端下次原样带回即实现增量拉取。after 非法（非数字/负数）按 0 处理。

    日志超出 MAX_LOG_LINES 会从头丢弃，若客户端落后到已被丢弃的区间，则返回
    现存的全部行——宁可重复一段也不漏行，否则前端会看到日志凭空断档。
    """
    with _LOCK:
        logs = list(task['logs'])
        seq = task.get('log_seq', len(logs))
        payload = {
            'success': True,
            'status': task['status'],
            'progress': task['progress'],
            'stage': task['stage'],
            'log_index': seq,
            'result': task.get('result', {}),
        }
    try:
        cursor = int(after)
    except (TypeError, ValueError):
        cursor = 0
    # 现存日志中第一行的序号（seq 与 logs 在同一把锁内读出，差值即为保留行数）
    first_seq = seq - len(logs) + 1
    if cursor < first_seq:
        start = 0
    else:
        start = cursor - first_seq + 1
    payload['logs'] = logs[start:]
    return payload
