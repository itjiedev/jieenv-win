"""
系统级公共操作模块（Windows 服务、目录与环境变量操作）

提供跨应用复用的通用能力，任何插件应用均可直接导入调用：

    from jiecore.system_ops import (query_service_state, start_service,
                                    stop_service, restart_service,
                                    query_service_startup_type,
                                    set_service_startup_type,
                                    enable_service_autostart,
                                    disable_service_autostart,
                                    open_directory, open_env_variables,
                                    broadcast_env_change, get_path_entries,
                                    set_system_path, remove_from_path,
                                    split_path)

Windows 服务：
- query_service_state: 查询 Windows 服务状态（sc query）
- query_service_startup_type: 查询服务启动类型（sc qc，含 delayed-auto 识别）
- set_service_startup_type: 设置服务启动类型
  （sc config start=auto|delayed-auto|demand|disabled|boot|system）；
  权限不足时自动弹 UAC 提权，用户拒绝返回失败提示；
  可传 on_step 回调获取逐步进度
- enable_service_autostart / disable_service_autostart: 上述两场景的快捷封装
- start_service / stop_service / restart_service: 启停 Windows 服务
  （net start / net stop / 先 stop 再 start）；
  当前权限不足时自动弹 UAC 对话框申请管理员权限，由用户决定是否许可，
  用户拒绝则返回失败提示；可传 on_step 回调获取逐步进度（如等待 UAC 授权）
- run_elevated: 通用 UAC 提权执行（ShellExecuteEx → PowerShell → schtasks 三级后备），
  下级应用可直接调用来包装需要管理员权限的操作

目录操作：
- open_directory: 在资源管理器中打开指定目录

环境变量：
- broadcast_env_change: 广播 WM_SETTINGCHANGE，使资源管理器与新进程刷新环境变量缓存
- split_path: PATH 原始值拆条目，去空
- get_path_entries: 从注册表读取有效 PATH 条目（系统在前、用户在后），展开环境变量
- set_system_path: UAC 提权写系统 PATH（HKLM）
- remove_from_path: 从 PATH 原始字符串中删除指定目录条目（规范化比较，保留原始写法）
- open_env_variables: 打开 Windows 系统环境变量编辑对话框

所有函数均返回 dict（{success, message} 或附加字段），不抛业务异常；
HTTP 层封装见 jiecore/views.py 中 api_service_state / api_service_action /
api_service_startup / api_fs_open_dir。

注意：UAC 提权对话框仅在交互式桌面会话中可见；若本系统部署在后台会话
（如注册为 Windows 服务，session 0）则无法弹窗，需直接以管理员身份运行。
"""
import os
import re
import uuid
import base64
import ctypes
import logging
import subprocess
from pathlib import Path
from datetime import datetime, timedelta

from jiecore.utils import get_cmd_encoding

logger = logging.getLogger(__name__)

# 状态查询类子进程统一超时（秒）
_CMD_TIMEOUT = 15
# 服务启停超时（秒）：部分服务启动耗时较长
_NET_TIMEOUT = 120

# net start/stop 权限不足的输出特征（中英文系统，系统错误 5 = 拒绝访问）
_ACCESS_DENIED_RE = re.compile(r'系统错误\s*5|拒绝访问|Access is denied', re.IGNORECASE)

# 子进程隐藏窗口标志：防止执行 sc/net 等命令时闪现黑色控制台窗口
_CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0



# ── 服务启动类型（sc config）──────────────────────────────────
# sc config start= 合法取值 → 中文展示名
#   auto          开机自动启动
#   delayed-auto  延迟自动启动（Win7+，开机后延迟启动；等价于 AUTO_START + DELAYED）
#   demand        手动启动（开机不启动）
#   disabled      禁用（开机不启动）
#   boot / system 驱动类服务专用，普通 Win32 服务不支持
_START_TYPE_LABELS = {
    'boot':          '引导启动（驱动）',
    'system':        '系统启动（驱动）',
    'auto':          '自动（开机启动）',
    'delayed-auto':  '延迟自动（开机后延迟启动）',
    'demand':        '手动（开机不启动）',
    'disabled':      '禁用（开机不启动）',
}
_VALID_START_TYPES = tuple(_START_TYPE_LABELS)

# sc qc 输出 START_TYPE 行的关键字 → 内部取值
_SC_START_TYPE_TOKENS = (
    ('BOOT_START',    'boot'),
    ('SYSTEM_START',  'system'),
    ('AUTO_START',    'auto'),
    ('DEMAND_START',  'demand'),
    ('DISABLED',      'disabled'),
)



def is_admin() -> bool:
    """当前进程是否具有管理员权限"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def query_service_state(service_name: str) -> dict:
    """
    查询指定 Windows 服务状态（sc query）。
    返回 dict: {'exists': bool, 'running': bool}
    服务名为空或查询失败时返回 {'exists': False, 'running': False}。
    """
    if not service_name:
        return {'exists': False, 'running': False}
    try:
        proc = subprocess.run(['sc', 'query', service_name],
                              capture_output=True, timeout=_CMD_TIMEOUT,
                              creationflags=_CREATE_NO_WINDOW)
        out = ((proc.stdout or b'') + b'\n' + (proc.stderr or b'')).decode(get_cmd_encoding(), errors='replace')
    except Exception as e:
        logger.debug('查询服务状态失败 %s: %s', service_name, e)
        return {'exists': False, 'running': False}
    exists = 'SERVICE_NAME:' in out
    return {'exists': exists, 'running': exists and 'RUNNING' in out}


def _run_net(action: str, service_name: str) -> tuple:
    """以当前权限直接执行 net start/stop，返回 (result, 原始输出)"""
    verb = '启动' if action == 'start' else '停止'
    try:
        proc = subprocess.run(['net', action, service_name],
                              capture_output=True, timeout=_NET_TIMEOUT,
                              creationflags=_CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {'success': False, 'message': f'{verb}服务超时: {service_name}'}, ''
    except Exception as e:
        logger.exception('%s服务失败 %s', verb, service_name)
        return {'success': False, 'message': f'{verb}服务失败: {e}'}, ''
    out = ((proc.stdout or b'') + b'\n' + (proc.stderr or b'')).decode(get_cmd_encoding(), errors='replace')
    if proc.returncode == 0:
        return {'success': True, 'message': f'服务已{verb}: {out.strip()[:200]}'}, out
    return {'success': False, 'message': f'{verb}服务失败: {out.strip()[:200]}'}, out


# ── UAC 提权执行 ──────────────────────────────────────────

class _SHELLEXECUTEINFOW(ctypes.Structure):
    """ShellExecuteExW 参数结构（仅声明所需字段）"""
    _fields_ = [
        ('cbSize', ctypes.c_ulong),
        ('fMask', ctypes.c_ulong),
        ('hwnd', ctypes.c_void_p),
        ('lpVerb', ctypes.c_wchar_p),
        ('lpFile', ctypes.c_wchar_p),
        ('lpParameters', ctypes.c_wchar_p),
        ('lpDirectory', ctypes.c_wchar_p),
        ('nShow', ctypes.c_int),
        ('hInstApp', ctypes.c_void_p),
        ('lpIDList', ctypes.c_void_p),
        ('lpClass', ctypes.c_wchar_p),
        ('hkeyClass', ctypes.c_void_p),
        ('dwHotKey', ctypes.c_ulong),
        # shellapi.h 在 dwHotKey 之后是 union { HANDLE hIcon; HANDLE hMonitor; }，
        # 漏掉它会让结构体在 x64 下只有 104 字节（真实为 112，hProcess 位于偏移
        # 104），cbSize 校验不通过，ShellExecuteExW 恒返回 FALSE + 错误码 5（拒绝
        # 访问），一级提权链路彻底失效并级联降级到 PowerShell / schtasks
        ('hIcon', ctypes.c_void_p),
        ('hProcess', ctypes.c_void_p),  # 配合 SEE_MASK_NOCLOSEPROCESS 取进程句柄
    ]


# 显式声明原型：restype 缺省为 c_int，use_last_error 保证取到的是本次调用的
# 错误码（GetLastError 会被两次调用之间的其它 ctypes 调用覆盖）
if os.name == 'nt':
    _shell32 = ctypes.WinDLL('shell32', use_last_error=True)
    _shell32.ShellExecuteExW.restype = ctypes.c_bool
    _shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
else:
    _shell32 = None


def _current_session_id() -> int:
    """当前进程所在会话 ID，获取失败返回 -1"""
    sid = ctypes.c_ulong()
    try:
        if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)):
            return sid.value
    except Exception:
        pass
    return -1


def _uac_enabled() -> bool:
    """UAC 是否开启（注册表 EnableLUA，读取失败时按开启处理）"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r'SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System') as key:
            value, _ = winreg.QueryValueEx(key, 'EnableLUA')
        return bool(value)
    except OSError:
        return True


def _elevation_failure_message(err: int) -> str:
    """ShellExecuteEx(runas) 无法拉起 UAC 对话框时，诊断常见原因并给出可行建议"""
    if _current_session_id() == 0:
        return ('无法弹出 UAC 提权对话框：本系统运行于后台会话（session 0）。'
                '请直接以管理员身份重启本系统后再重试服务启停')
    if not _uac_enabled():
        return ('无法弹出 UAC 提权对话框：系统策略已禁用 UAC（EnableLUA=0），无法申请提权。'
                '请直接以管理员身份重启本系统后再重试服务启停')
    appinfo = query_service_state('AppInfo')
    if appinfo['exists'] and not appinfo['running']:
        return ('无法弹出 UAC 提权对话框：UAC 依赖的 Application Information（AppInfo）服务未运行。'
                '请先启动该服务，或直接以管理员身份运行本系统')
    try:
        active = ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
        current = _current_session_id()
        if current >= 0 and active not in (0xFFFFFFFF, current):
            return (f'无法弹出 UAC 提权对话框：本进程所在会话（{current}）'
                    f'不是当前活动桌面会话（{active}）。请直接以管理员身份重启本系统后再重试')
    except Exception:
        pass
    return (f'无法弹出 UAC 提权对话框（错误码 {err}）：可能受组策略限制。'
            '请直接以管理员身份重启本系统后再重试服务启停')


def run_elevated(file_path: str, parameters: str, timeout: int,
                 show: int = 0) -> int:
    """
    提权启动程序并阻塞等待结束，返回退出码；用户拒绝提权时抛 PermissionError。
    show: 窗口显示方式，0=SW_HIDE（默认隐藏），1=SW_SHOWNORMAL（可见窗口）。
    三级后备链路：
      1. ShellExecuteExW(runas) 直接提权；
      2. PowerShell Start-Process -Verb RunAs 中转；
      3. schtasks /ru SYSTEM 计划任务（绕过 ShellExecuteEx，适用于 ShellExecuteEx
         被系统拒绝（错误码 5）的场景，需当前进程已是管理员）。
    仅在交互式桌面会话可用（后台服务会话无法弹窗）。
    """
    try:
        return _run_elevated_native(file_path, parameters, timeout, show=show)
    except (PermissionError, TimeoutError):
        # 二者都是 OSError 子类，必须在通用分支之前拦截：用户已明确拒绝 UAC，
        # 或命令已成功提权拉起只是等待超时，再降级重试等于二次弹窗/重复执行
        raise
    except OSError as e:
        logger.warning('ShellExecuteEx 直接提权失败（%s），改用 PowerShell 提权', e)
    try:
        return _run_elevated_powershell(file_path, parameters, timeout)
    except (PermissionError, TimeoutError):
        raise
    except OSError as e2:
        logger.warning('PowerShell 提权也失败（%s），改用计划任务（SYSTEM）提权', e2)
    return _run_elevated_schtasks(file_path, parameters, timeout)


def _run_elevated_native(file_path: str, parameters: str, timeout: int,
                         show: int = 0) -> int:
    """ShellExecuteExW(runas) 直接提权；拉起失败抛 OSError，用户拒绝抛 PermissionError
    show: 0=SW_HIDE 隐藏窗口，1=SW_SHOWNORMAL 可见窗口
    """
    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040   # SEE_MASK_NOCLOSEPROCESS：执行后返回进程句柄
    info.lpVerb = 'runas'     # 请求提权（触发 UAC）
    info.lpFile = file_path
    info.lpParameters = parameters
    info.lpDirectory = os.environ.get('SystemRoot', r'C:\Windows')
    info.nShow = show         # 窗口显示方式（由调用方控制）
    # 关联前台窗口：确保 UAC 对话框能正确显示在桌面壳（pywebview）等场景
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if hwnd:
            info.hwnd = hwnd
    except Exception:
        pass
    logger.info('ShellExecuteExW(runas): file=%s, params=%s, show=%d, hwnd=%s',
                file_path, parameters, show, info.hwnd)
    if not _shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == 1223:       # ERROR_CANCELLED：用户拒绝提权
            raise PermissionError('用户取消了管理员权限申请（UAC 对话框）')
        raise OSError(_elevation_failure_message(err))

    handle = info.hProcess
    if not handle:
        # 极少数场景（verb 被 shell 扩展接管）不回填进程句柄，无法等待与取退出码，
        # 视为命令已下发成功，避免退化成 WaitForSingleObject(NULL) 的假超时
        logger.warning('ShellExecuteExW 成功但未返回进程句柄，跳过等待')
        return 0
    try:
        wait = ctypes.windll.kernel32.WaitForSingleObject(handle, timeout * 1000)
        if wait != 0:         # WAIT_OBJECT_0
            raise TimeoutError(f'提权执行超时（{timeout} 秒）')
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _run_elevated_powershell(file_path: str, parameters: str, timeout: int) -> int:
    """
    后备提权链路：经 PowerShell Start-Process -Verb RunAs 提权（同样弹 UAC 对话框）。
    适用于限制 python.exe 直接 ShellExecuteEx(runas) 的环境。
    以退出码标记文件为唯一成功凭证：提权被拒绝或执行异常时不会写入标记。
    """
    marker = Path(os.environ.get('TEMP', '.')) / f'jie_elev_{os.getpid()}_{uuid.uuid4().hex[:8]}.txt'
    # PowerShell 单引号字符串内的单引号需双写转义
    ps_file = file_path.replace("'", "''")
    ps_args = parameters.replace("'", "''")
    # 注意：不能直接在管道中读 ExitCode（进程未完全退出时属性获取会抛异常），
    # 需先赋值给变量等待进程彻底结束后再写标记文件
    ps_cmd = (f"$p = Start-Process -FilePath '{ps_file}' "
              f"-ArgumentList '{ps_args}' -Verb RunAs -WindowStyle Hidden -Wait -PassThru; "
              f"$code = $p.ExitCode; "
              f"Set-Content -Path '{marker}' -Value $code -Encoding ascii")
    encoded = base64.b64encode(ps_cmd.encode('utf-16-le')).decode('ascii')
    try:
        proc = subprocess.run(['powershell', '-NoProfile', '-EncodedCommand', encoded],
                              capture_output=True, timeout=timeout + 30,
                              creationflags=_CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise TimeoutError(f'提权执行超时（{timeout} 秒）')
    err_out = ((proc.stdout or b'') + b'\n' + (proc.stderr or b'')).decode(get_cmd_encoding(), errors='replace')

    exit_code = None
    try:
        if marker.exists():
            exit_code = int(marker.read_text(errors='replace').strip())
    except (OSError, ValueError):
        exit_code = None
    finally:
        try:
            os.remove(marker)
        except OSError:
            pass

    if exit_code is not None:
        return exit_code
    # 无退出码标记：提权被拒绝或执行失败（powershell.exe 自身退出码恒为 0，
    # 只能靠 stderr 区分：用户取消 UAC 时报 1223/操作已取消）
    if '1223' in err_out or '取消' in err_out or 'cancel' in err_out.lower():
        raise PermissionError('用户取消了管理员权限申请（UAC 对话框）')
    raise OSError(f'PowerShell 提权执行失败: {err_out.strip()[:200] or "无错误输出"}')


def _run_elevated_schtasks(file_path: str, parameters: str, timeout: int) -> int:
    """
    后备提权链路：通过 Windows 计划任务以 SYSTEM 身份执行命令。
    适用于 ShellExecuteEx(runas) 被系统直接拒绝（错误码 5）的场景。
    SYSTEM 账户拥有最高权限，无需 UAC 对话框；但创建 /ru SYSTEM 的计划任务本身
    需要管理员权限，非管理员进程走到这一级必然“拒绝访问”，故先做前置校验，
    给出可操作的提示而不是把 schtasks 的原始报错抛给前端。
    """
    if not is_admin():
        raise PermissionError(
            '无法通过计划任务（SYSTEM）提权：创建 SYSTEM 计划任务需要管理员权限，'
            '而当前进程不是管理员，前两级 UAC 提权也均未成功。'
            '请直接以管理员身份重启本系统后再重试')

    task_name = f'JienvElev_{os.getpid()}_{uuid.uuid4().hex[:8]}'
    sys_temp = os.environ.get('TEMP', os.environ.get('TMP', '.'))
    bat_file = Path(sys_temp) / f'jie_schtasks_{os.getpid()}.bat'
    out_file = Path(sys_temp) / f'jie_schtasks_out_{os.getpid()}.log'

    # 将命令写入 bat 文件，避免 schtasks /tr 参数的引号嵌套问题
    try:
        bat_file.write_text(
            f'@echo off\n"{file_path}" {parameters} > "{out_file}" 2>&1\n',
            encoding='gbk')
    except OSError as e:
        raise OSError(f'无法写入临时批处理: {e}')

    # /st 必须是当前时间之后的时刻，否则 schtasks 会打印
    # “警告: 因为 /ST 早于当前的时间，任务可能无法运行”污染错误输出；
    # 任务紧接着由 /run 立即触发，/st 取值本身并不影响执行时机
    start_at = (datetime.now() + timedelta(minutes=1)).strftime('%H:%M')
    create_cmd = [
        'schtasks', '/create', '/tn', task_name,
        '/tr', str(bat_file),
        '/sc', 'once', '/st', start_at,
        '/ru', 'SYSTEM', '/f',
    ]

    try:
        # 创建计划任务
        proc = subprocess.run(create_cmd, capture_output=True,
                              timeout=30, creationflags=_CREATE_NO_WINDOW)
        if proc.returncode != 0:
            err = ((proc.stdout or b'') + b'\n' + (proc.stderr or b'')).decode(
                'gbk', errors='replace')
            raise OSError(f'创建计划任务失败: {err.strip()[:200]}')

        # 立即触发运行
        subprocess.run(['schtasks', '/run', '/tn', task_name],
                       capture_output=True, timeout=30,
                       creationflags=_CREATE_NO_WINDOW)

        # 轮询等待任务完成
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1)
            q = subprocess.run(['schtasks', '/query', '/tn', task_name],
                               capture_output=True, timeout=15,
                               creationflags=_CREATE_NO_WINDOW)
            q_out = (q.stdout or b'').decode(get_cmd_encoding(), errors='replace')
            if 'Running' not in q_out:
                break
        else:
            # 超时：尝试停止任务
            subprocess.run(['schtasks', '/end', '/tn', task_name],
                           capture_output=True, timeout=15,
                           creationflags=_CREATE_NO_WINDOW)
            raise TimeoutError(f'计划任务执行超时（{timeout} 秒）')

        # 获取退出码
        exit_code = 1
        try:
            q = subprocess.run(['schtasks', '/query', '/tn', task_name,
                                '/fo', 'list', '/v'],
                               capture_output=True, timeout=15,
                               creationflags=_CREATE_NO_WINDOW)
            q_out = (q.stdout or b'').decode(get_cmd_encoding(), errors='replace')
            for line in q_out.splitlines():
                if 'Last Result' in line:
                    exit_code = int(line.split(':')[-1].strip())
                    break
        except (OSError, ValueError):
            pass

        return exit_code
    finally:
        # 清理：删除任务 + 临时文件
        subprocess.run(['schtasks', '/delete', '/tn', task_name, '/f'],
                       capture_output=True, timeout=15,
                       creationflags=_CREATE_NO_WINDOW)
        for f in (bat_file, out_file):
            try:
                os.remove(f)
            except OSError:
                pass


def _net_service_elevated(action: str, service_name: str, on_step=None) -> dict:
    """UAC 提权执行 net start/stop：输出重定向到临时文件以便读取结果"""
    verb = '启动' if action == 'start' else '停止'
    from django.conf import settings
    out_file = settings.TMP_DIR / f'net_{action}_{os.getpid()}.log'
    params = f'/c net {action} "{service_name}" > "{out_file}" 2>&1'
    if on_step:
        on_step('已发起管理员权限申请，请在 UAC 对话框中点击“是”授权...')
    try:
        code = run_elevated('cmd.exe', params, _NET_TIMEOUT)
    except PermissionError as e:
        return {'success': False, 'message': str(e)}
    except Exception as e:
        logger.exception('提权%s服务失败 %s', verb, service_name)
        return {'success': False, 'message': f'以管理员权限执行失败: {e}'}
    out = ''
    try:
        out = out_file.read_text(encoding=get_cmd_encoding(), errors='replace').strip()
    except OSError:
        pass
    finally:
        try:
            os.remove(out_file)
        except OSError:
            pass
    if code == 0:
        return {'success': True, 'message': f'服务已{verb}（管理员权限）: {out[:200]}'}
    return {'success': False, 'message': f'{verb}服务失败: {out[:200] or "无错误输出"}'}


def _net_service(action: str, service_name: str, on_step=None) -> dict:
    """
    net start / net stop 统一执行逻辑：权限不足时自动申请 UAC 提权重试。
    on_step: 可选回调 on_step(message)，在每个关键阶段回报一次进度。
    """
    if not service_name:
        return {'success': False, 'message': '未指定服务名'}
    verb = '启动' if action == 'start' else '停止'
    if on_step:
        on_step(f'正在以当前权限{verb}服务 {service_name} ...')
    result, raw = _run_net(action, service_name)
    if result['success']:
        # 验证服务是否真正启动/停止（防止 pywebview 桌面壳 filtered token 场景
        # 下 net start 返回 0 但服务未实际启动的假成功）
        import time
        expected_running = (action == 'start')
        for attempt in range(5):
            if attempt > 0:
                time.sleep(1)
            state = query_service_state(service_name)
            if state['exists'] and state['running'] == expected_running:
                if on_step:
                    on_step(f'服务{verb}成功')
                return result
            # stop 操作：服务不存在也视为成功
            if action == 'stop' and not state['exists']:
                if on_step:
                    on_step(f'服务{verb}成功')
                return result
        # net 返回成功但服务状态未变，走 UAC 提权
        logger.warning('%s服务 %s: net 返回成功但服务状态未变为%s，尝试 UAC 提权',
                       verb, service_name, '运行' if expected_running else '停止')
        if on_step:
            on_step(f'net {action} 返回成功但服务状态未变，申请 UAC 提权...')
    # 权限问题或假成功：弹 UAC 对话框由用户决定是否提权重试
    # 不再检查 is_admin()：在桌面壳（pywebview）等场景 IsUserAnAdmin 可能返回 True
    # 但进程令牌已被过滤，实际不具备管理员权限，导致 net start/stop 失败却不尝试提权
    if result['success'] or _ACCESS_DENIED_RE.search(raw):
        if not result['success']:
            logger.info('%s服务 %s 权限不足，申请管理员提权', verb, service_name)
            if on_step:
                on_step(f'当前权限不足，需要管理员权限，正在申请 UAC 提权...')
        return _net_service_elevated(action, service_name, on_step)
    return result


def start_service(service_name: str, on_step=None) -> dict:
    """启动 Windows 服务（net start），返回 dict: {success, message}"""
    return _net_service('start', service_name, on_step)


def stop_service(service_name: str, on_step=None) -> dict:
    """停止 Windows 服务（net stop），返回 dict: {success, message}"""
    return _net_service('stop', service_name, on_step)


def restart_service(service_name: str, on_step=None) -> dict:
    """
    重启 Windows 服务（先 stop 再 start），返回 dict: {success, message}。
    停止阶段失败时仍会继续尝试启动（服务可能本就未运行）。
    """
    if not service_name:
        return {'success': False, 'message': '未指定服务名'}
    if on_step:
        on_step(f'正在重启服务 {service_name} ...')
    # 1. 停止
    stop_result = stop_service(service_name, on_step=on_step)
    if not stop_result['success']:
        # 停止失败但服务可能本就未运行，记录日志后继续尝试启动
        logger.warning('重启服务 %s：停止阶段失败（%s），继续尝试启动',
                       service_name, stop_result['message'])
        if on_step:
            on_step(f'停止阶段返回: {stop_result["message"][:120]}，继续尝试启动...')
    # 2. 启动
    start_result = start_service(service_name, on_step=on_step)
    if start_result['success']:
        if on_step:
            on_step(f'服务重启成功: {service_name}')
        return start_result
    return {'success': False,
            'message': f'重启失败（停止: {stop_result["message"][:120]}；'
                       f'启动: {start_result["message"][:120]}）'}


def query_service_startup_type(service_name: str) -> dict:
    """
    查询指定 Windows 服务的启动类型（sc qc）。
    返回 dict: {'exists': bool, 'start_type': str}
    start_type 取值: 'boot'（设备启动） / 'system'（系统启动） / 'auto'(启动) / 'delayed-auto'(延迟自动启动) /
                     'demand'（手动） / 'disabled'（禁止启动） / ''（解析失败）
    服务不存在或查询失败时 exists=False。
    """
    if not service_name:
        return {'exists': False, 'start_type': ''}
    try:
        proc = subprocess.run(['sc', 'qc', service_name],
                              capture_output=True, timeout=_CMD_TIMEOUT,
                              creationflags=_CREATE_NO_WINDOW)
        out = ((proc.stdout or b'') + b'\n' + (proc.stderr or b'')).decode(
            get_cmd_encoding(), errors='replace')
    except Exception as e:
        logger.debug('查询服务启动类型失败 %s: %s', service_name, e)
        return {'exists': False, 'start_type': ''}

    if 'SERVICE_NAME:' not in out:
        return {'exists': False, 'start_type': ''}

    start_type, delayed = '', False
    for line in out.splitlines():
        upper = line.strip().upper()
        if upper.startswith('START_TYPE'):
            # 形如: START_TYPE         : 2   AUTO_START  (DELAYED)
            delayed = 'DELAYED' in upper
            for token, mapped in _SC_START_TYPE_TOKENS:
                if token in upper:
                    start_type = mapped
                    break
            break

    # AUTO_START + (DELAYED) 统一归并为 delayed-auto
    if start_type == 'auto' and delayed:
        start_type = 'delayed-auto'
    return {'exists': True, 'start_type': start_type}


def _sc_config(service_name: str, startup_type: str) -> tuple:
    """以当前权限直接执行 sc config 设置启动类型，返回 (result, 原始输出)"""
    label = _START_TYPE_LABELS.get(startup_type, startup_type)
    try:
        proc = subprocess.run(['sc', 'config', service_name, f'start={startup_type}'],
                              capture_output=True, timeout=_CMD_TIMEOUT,
                              creationflags=_CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {'success': False,
                'message': f'设置服务启动类型超时: {service_name}'}, ''
    except Exception as e:
        logger.exception('设置服务启动类型失败 %s', service_name)
        return {'success': False,
                'message': f'设置服务启动类型失败: {e}'}, ''
    out = ((proc.stdout or b'') + b'\n' + (proc.stderr or b'')).decode(
        'gbk', errors='replace')
    if proc.returncode == 0:
        return {'success': True,
                'message': f'服务启动类型已设为「{label}」: {service_name}'}, out
    return {'success': False,
            'message': f'设置服务启动类型失败: {out.strip()[:200]}'}, out


def _sc_config_elevated(service_name: str, startup_type: str,
                        on_step=None) -> dict:
    """UAC 提权执行 sc config 设置服务启动类型：输出重定向到临时文件以便读取结果"""
    label = _START_TYPE_LABELS.get(startup_type, startup_type)
    from django.conf import settings
    out_file = settings.TMP_DIR / f'sc_config_{os.getpid()}_{uuid.uuid4().hex[:8]}.log'
    # 注意 sc 语法：start= 与取值之间允许一个空格；整体用 cmd /c 包一层以便重定向
    params = (f'/c sc config "{service_name}" start={startup_type} '
              f'> "{out_file}" 2>&1')
    if on_step:
        on_step('已发起管理员权限申请，请在 UAC 对话框中点击“是”授权...')
    try:
        code = run_elevated('cmd.exe', params, _CMD_TIMEOUT)
    except PermissionError as e:
        return {'success': False, 'message': str(e)}
    except Exception as e:
        logger.exception('提权设置服务启动类型失败 %s', service_name)
        return {'success': False, 'message': f'以管理员权限执行失败: {e}'}

    out = ''
    try:
        out = out_file.read_text(encoding=get_cmd_encoding(), errors='replace').strip()
    except OSError:
        pass
    finally:
        try:
            os.remove(out_file)
        except OSError:
            pass

    if code == 0:
        return {'success': True,
                'message': f'服务启动类型已设为「{label}」（管理员权限）: {service_name}'}
    return {'success': False,
            'message': f'设置服务启动类型失败: {out[:200] or "无错误输出"}'}


def set_service_startup_type(service_name: str, startup_type: str,
                             on_step=None) -> dict:
    """
    设置 Windows 服务的启动类型（sc config），权限不足时自动申请 UAC 提权重试。

    startup_type 取值:
      'auto'          开机自动启动
      'delayed-auto'  延迟自动启动（开机后延迟启动）
      'demand'        手动启动（开机不启动）
      'disabled'      禁用（开机不启动）
      'boot' / 'system' 驱动类服务专用，普通 Win32 服务不支持

    on_step: 可选回调 on_step(message)，在每个关键阶段回报一次进度。
    返回 dict: {success, message}
    """
    if not service_name:
        return {'success': False, 'message': '未指定服务名'}

    startup_type = (startup_type or '').strip().lower()
    if startup_type not in _VALID_START_TYPES:
        return {'success': False,
                'message': f'不支持的启动类型: {startup_type!r}，'
                           f'可选值: {" / ".join(_VALID_START_TYPES)}'}

    # 前置校验：服务不存在时直接返回，避免 sc config 报错信息难以理解
    state = query_service_state(service_name)
    if not state['exists']:
        return {'success': False, 'message': f'服务不存在: {service_name}'}

    label = _START_TYPE_LABELS[startup_type]
    if on_step:
        on_step(f'正在将服务 {service_name} 的启动类型设为「{label}」...')

    result, raw = _sc_config(service_name, startup_type)

    need_elevate = False
    if result['success']:
        # 验证是否真正生效（pywebview 桌面壳 filtered token 场景下
        # sc config 可能返回 0 但启动类型未变）
        import time
        for attempt in range(3):
            if attempt > 0:
                time.sleep(1)
            cur = query_service_startup_type(service_name)
            if cur['exists'] and cur['start_type'] == startup_type:
                if on_step:
                    on_step(f'服务启动类型已设为「{label}」')
                return result
        logger.warning('设置服务 %s 启动类型: sc 返回成功但未生效，尝试 UAC 提权',
                       service_name)
        if on_step:
            on_step('sc config 返回成功但启动类型未变，申请 UAC 提权...')
        need_elevate = True
    elif _ACCESS_DENIED_RE.search(raw):
        logger.info('设置服务 %s 启动类型权限不足，申请管理员提权', service_name)
        if on_step:
            on_step('当前权限不足，需要管理员权限，正在申请 UAC 提权...')
        need_elevate = True

    if need_elevate:
        return _sc_config_elevated(service_name, startup_type, on_step)
    return result

def enable_service_autostart(service_name: str, on_step=None) -> dict:
    """开启服务开机自动启动（start=auto），返回 {success, message}"""
    return set_service_startup_type(service_name, 'auto', on_step=on_step)


def disable_service_autostart(service_name: str, on_step=None) -> dict:
    """禁用服务开机自动启动（start=disabled），返回 {success, message}"""
    return set_service_startup_type(service_name, 'disabled', on_step=on_step)


def open_directory(path: str) -> dict:
    """
    在资源管理器中打开目录（Windows os.startfile）。
    路径为空或不存在时返回失败；返回 dict: {success, message}
    """
    path = (path or '').strip().strip('"')
    if not path:
        return {'success': False, 'message': '未指定目录路径'}
    if not Path(path).exists():
        return {'success': False, 'message': f'目录不存在: {path}'}
    try:
        os.startfile(path)  # Windows: 打开资源管理器
    except OSError as e:
        return {'success': False, 'message': f'打开目录失败: {e}'}
    return {'success': True, 'message': '已打开目录'}


def open_env_variables() -> dict:
    """
    打开 Windows 环境变量编辑对话框。
    先尝试 UAC 提权打开系统级（用户+系统变量均可编辑）；
    用户拒绝 UAC 则降级打开用户级（仅用户变量可编辑）。
    返回 dict: {success, message}
    """
    cmd = 'rundll32.exe'
    args = 'sysdm.cpl,EditEnvironmentVariables'
    workdir = r'C:\Windows'
    # 尝试提权打开系统级
    try:
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, 'runas', cmd, args, workdir, 1
        )
        if ret > 32:
            return {'success': True, 'message': '已打开环境变量对话框（系统级）'}
        err = ctypes.windll.kernel32.GetLastError()
        if err != 1223:  # 非用户拒绝，是真正的错误
            return {'success': False, 'message': f'打开环境变量对话框失败（错误码 {ret}）'}
    except Exception as e:
        logger.warning('提权打开环境变量失败: %s', e)
    # 用户拒绝 UAC 或其他提权异常：降级打开用户级
    try:
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, 'open', cmd, args, workdir, 1
        )
        if ret > 32:
            return {'success': True, 'message': '已打开环境变量对话框（用户级）'}
        return {'success': False, 'message': f'打开环境变量对话框失败（错误码 {ret}）'}
    except Exception as e:
        logger.exception('打开用户级环境变量失败')
        return {'success': False, 'message': f'打开环境变量对话框失败: {e}'}


# ── 环境变量公共操作 ──────────────────────────────────────

def broadcast_env_change() -> None:
    """广播 WM_SETTINGCHANGE，使资源管理器与新进程刷新环境变量缓存。"""
    try:
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        result = ctypes.c_long()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, 'Environment',
            SMTO_ABORTIFHUNG, 5000, ctypes.byref(result))
    except Exception as e:
        logger.warning('广播环境变量变更通知失败: %s', e)


def split_path(value: str) -> list:
    """PATH 原始值拆条目，去空。"""
    return [p for p in (value or '').split(';') if p.strip()]


def get_path_entries() -> list:
    """从注册表读取有效 PATH 条目（系统在前、用户在后），展开环境变量并去掉空条目。

    直接读注册表而非进程环境，保证修改后立即重新读取能反映最新值。
    非 Windows 系统回退到 os.environ['PATH']。
    """
    if os.name != 'nt':
        return [p for p in os.environ.get('PATH', '').split(os.pathsep) if p.strip()]
    from .regutil import read_str
    import winreg
    sys_path = read_str(winreg.HKEY_LOCAL_MACHINE,
                        r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment',
                        'Path')
    usr_path = read_str(winreg.HKEY_CURRENT_USER, r'Environment', 'Path')
    entries = []
    for part in ';'.join(p for p in (sys_path, usr_path) if p).split(';'):
        part = os.path.expandvars(part).strip()
        if part:
            entries.append(part)
    return entries


def set_system_path(value: str) -> dict:
    """UAC 提权写系统 PATH（HKLM），返回 {success, message}。"""
    params = (f'/c reg add "HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment" '
              f'/v Path /t REG_EXPAND_SZ /d "{value}" /f >nul 2>&1')
    try:
        code = run_elevated('cmd.exe', params, 60)
    except PermissionError as e:
        return {'success': False, 'message': str(e)}
    except Exception as e:
        logger.exception('提权写系统 PATH 失败')
        return {'success': False, 'message': f'提权写系统 PATH 失败: {e}'}
    if code != 0:
        return {'success': False, 'message': f'提权写系统 PATH 失败（退出码 {code}）'}
    return {'success': True, 'message': ''}


def remove_from_path(raw: str, remove_dirs: list) -> tuple:
    """从 PATH 原始字符串中删除与 remove_dirs 指向同一位置的条目。

    比较时内联规范化：展开环境变量 → 去空白 → 要求带盘符或 UNC →
    转绝对路径 → 统一大小写 → 去尾部分隔符。因此 C:\\Python311、
    C:\\PYTHON311\\、%PYTHON_HOME% 会被视为同一条。

    保留的条目写回时仍用原始写法（%VAR% 不展开），保证 PATH 可移植。
    空条目直接丢弃；remove_dirs 为空或全部规范化失败时不做删除。
    返回 (新的 raw 字符串, 被移除的原始条目列表)。
    """
    # ---- 规范化目标集合 ----
    targets = set()
    for t in remove_dirs:
        p = os.path.expandvars(t or '').strip()
        if not p:
            continue
        if not os.path.splitdrive(p)[0]:          # 无盘符 / 非 UNC → 跳过
            continue
        try:
            p = os.path.abspath(p)
        except OSError:
            continue
        targets.add(os.path.normcase(p.rstrip('\\/')))

    # ---- 逐条过滤 raw ----
    kept, removed = [], []
    for part in (raw or '').split(';'):
        part = part.strip()
        if not part:
            continue

        p = os.path.expandvars(part).strip()
        if not p:
            kept.append(part); continue
        if not os.path.splitdrive(p)[0]:
            kept.append(part); continue
        try:
            p = os.path.abspath(p)
        except OSError:
            kept.append(part); continue
        key = os.path.normcase(p.rstrip('\\/'))

        if targets and key in targets:
            removed.append(part)
        else:
            kept.append(part)

    return ';'.join(kept), removed


# ── 终端启动 ──────────────────────────────────────────

_CREATE_NEW_CONSOLE = 0x00000010 if os.name == 'nt' else 0


def open_terminal(work_dir: str, path_prepend: list = None,
                  activate_cmd: str = None, env_extra: dict = None,
                  subprocess_env: dict = None) -> dict:
    """
    打开 cmd 终端窗口，可选注入环境变量。

    支持两种用法：
    1. 逐项指定（path_prepend + activate_cmd + env_extra）→ 自动合并 os.environ
    2. 直接传完整 env dict（subprocess_env）→ 跳过自动合并，最高优先级

    Args:
        work_dir:        终端工作目录
        path_prepend:    要前置到 PATH 的目录列表（list[str]）
        activate_cmd:    启动时用 cmd /k 执行的激活命令（如 activate.bat 路径）
        env_extra:       要额外注入的环境变量 dict（会覆盖同名已有变量）
        subprocess_env:  直接传入完整的环境变量 dict（传入时跳过 PATH/env_extra 处理）

    Returns:
        dict: {'success': bool, 'message': str}
    """
    work_dir = (work_dir or '').strip().strip('"')
    if not work_dir:
        return {'success': False, 'message': '未指定工作目录'}
    if not Path(work_dir).is_dir():
        return {'success': False, 'message': f'目录不存在: {work_dir}'}

    if subprocess_env is not None:
        merged_env = subprocess_env
    else:
        merged_env = os.environ.copy()
        if env_extra:
            for k, v in env_extra.items():
                if v is not None:
                    merged_env[k] = str(v)
        if path_prepend:
            existing = merged_env.get('PATH', '')
            prepend_str = os.pathsep.join(p for p in path_prepend if p)
            if prepend_str:
                merged_env['PATH'] = (prepend_str + os.pathsep + existing
                                       if existing else prepend_str)

    try:
        if activate_cmd:
            subprocess.Popen(
                ['cmd', '/k', activate_cmd],
                cwd=work_dir, env=merged_env,
                creationflags=_CREATE_NEW_CONSOLE,
            )
        else:
            subprocess.Popen(
                ['cmd'],
                cwd=work_dir, env=merged_env,
                creationflags=_CREATE_NEW_CONSOLE,
            )
    except OSError as e:
        return {'success': False, 'message': f'打开终端失败: {e}'}
    return {'success': True, 'message': '已打开终端'}