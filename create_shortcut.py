"""Create desktop shortcut for JieEnv. Called by install.bat.

Exit code 0 = success, 1 = failed.
"""
import os
import sys
import subprocess

base_dir = sys.argv[1].strip('"') if len(sys.argv) > 1 else os.getcwd()
if not base_dir.endswith(os.sep):
    base_dir += os.sep

tmp_dir = os.path.join(base_dir, 'tmp')
os.makedirs(tmp_dir, exist_ok=True)

# VBS script: get desktop path via SpecialFolders, create shortcut, echo result
vbs_path = os.path.join(tmp_dir, '_create_shortcut.vbs')
vbs_lines = [
    'Set ws = CreateObject("WScript.Shell")',
    'desktopPath = ws.SpecialFolders("Desktop")',
    'Set sc = ws.CreateShortcut(desktopPath & "\\JieEnv(win)环境管理.lnk")',
    f'sc.TargetPath = "{base_dir}run_desktop.bat"',
    f'sc.WorkingDirectory = "{base_dir}"',
    f'sc.IconLocation = "{base_dir}favicon.ico,0"',
    'sc.Description = "JieEnv(win)环境管理"',
    'sc.Save',
    'WScript.Echo desktopPath',
]

with open(vbs_path, 'w', encoding='gbk') as f:
    f.write('\n'.join(vbs_lines) + '\n')

result = subprocess.run(['cscript', '//nologo', vbs_path], capture_output=True, text=True)
try:
    os.remove(vbs_path)
except OSError:
    pass

if result.returncode != 0:
    sys.exit(1)

# VBS outputs the desktop path; verify the .lnk file exists there
desktop_path = result.stdout.strip()
if desktop_path:
    lnk_path = os.path.join(desktop_path, 'JieEnv(win)环境管理.lnk')
    if os.path.isfile(lnk_path):
        sys.exit(0)

sys.exit(1)
