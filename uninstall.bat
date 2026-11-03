@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

set "UNINSTALL_KEY=HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\JieEnv-win"
set "INSTALL_DIR=%~dp0"
set "SCRIPT_NAME=%~nx0"

if "!INSTALL_DIR:~-1!"=="\" set "INSTALL_DIR=!INSTALL_DIR:~0,-1!"

echo.
echo ==================================================
echo      JieEnv 卸载程序
echo ==================================================
echo.
echo   安装目录: !INSTALL_DIR!
echo.

echo [1/3] 检查 jieEnv 服务状态...
set "SERVICE_RUNNING=0"

for /f "usebackq delims=" %%p in (`powershell -NoProfile -Command "$r='!INSTALL_DIR!'; Get-Process python,pythonw -EA SilentlyContinue ^| Where-Object { $_.Path -like $r+'*' } ^| ForEach-Object { $_.Id }" 2^>nul`) do (
    set "SERVICE_RUNNING=1"
    echo       [发现] pid=%%p
)

if "!SERVICE_RUNNING!"=="1" (
    echo.
    echo [WARN] 检测到 jieEnv 服务正在运行
    echo.
    echo   请先停止服务再卸载，否则可能导致数据损坏。
    echo.
    set /p "STOP_FIRST=是否先退出并停止服务? (Y/N): "
    if /i "!STOP_FIRST!"=="Y" (
        if exist "stop.bat" (
            call stop.bat >nul 2>&1
        ) else (
            powershell -NoProfile -Command "$r='!INSTALL_DIR!'; Get-Process python,pythonw -EA SilentlyContinue ^| Where-Object { $_.Path -like $r+'*' } ^| Stop-Process -Force" >nul 2>&1
        )
        timeout /t 2 /nobreak >nul
        echo       已尝试停止服务。
    ) else (
        echo       继续卸载（不推荐）。
    )
) else (
    echo       服务未运行。
)
echo.

echo [2/3] 确认卸载范围
echo.
echo   将保留以下数据目录（不会被删除）:
echo     - !INSTALL_DIR!\databases\      (业务数据库)
echo     - !INSTALL_DIR!\dev_tools\      (开发工具)
echo     - !INSTALL_DIR!\lang_envs\      (语言运行环境)
echo     - !INSTALL_DIR!\workspace\      (工作区)
echo     - !INSTALL_DIR!\jieenv\data\    (应用数据，保留目录，删除 jieenvmaindb)
echo.
echo   以下内容将被删除:
echo     - runtime\  (Python 运行时)
echo     - jieenv\   (框架核心，保留 data\)
echo     - jieenv\data\jieenvmain.db  (框架主数据库)
echo     - 所有其他目录和 .bat / .py / .exe / .ico 等程序文件
echo     - Windows 卸载面板注册信息 + 桌面快捷方式
echo.
set /p "CONFIRM=确认要卸载吗? (输入 YES 继续，其他取消): "
if not "!CONFIRM!"=="YES" (
    echo.
    echo 已取消卸载。
    echo.
    pause
    exit /b 0
)
echo.

set "TEMP_BACKUP=!INSTALL_DIR!\_uninstall_backup_"
if exist "!TEMP_BACKUP!" rd /s /q "!TEMP_BACKUP!" 2>nul
mkdir "!TEMP_BACKUP!" 2>nul

echo [3/3] 执行卸载...

echo.

if exist "!INSTALL_DIR!\databases" (
    echo   [保留] databases\
    move "!INSTALL_DIR!\databases" "!TEMP_BACKUP!\databases" >nul 2>&1
)

if exist "!INSTALL_DIR!\dev_tools" (
    echo   [保留] dev_tools\
    move "!INSTALL_DIR!\dev_tools" "!TEMP_BACKUP!\dev_tools" >nul 2>&1
)

if exist "!INSTALL_DIR!\lang_envs" (
    echo   [保留] lang_envs\
    move "!INSTALL_DIR!\lang_envs" "!TEMP_BACKUP!\lang_envs" >nul 2>&1
)

if exist "!INSTALL_DIR!\workspace" (
    echo   [保留] workspace\
    move "!INSTALL_DIR!\workspace" "!TEMP_BACKUP!\workspace" >nul 2>&1
)

if exist "!INSTALL_DIR!\jieenv\data" (
    echo   [保留] jieenv\data\
    mkdir "!TEMP_BACKUP!\jieenv" 2>nul
    move "!INSTALL_DIR!\jieenv\data" "!TEMP_BACKUP!\jieenv\data" >nul 2>&1
)

echo.
echo   删除其他文件和目录...

pushd "!INSTALL_DIR!" || (
    echo [ERROR] 无法进入安装目录: !INSTALL_DIR!
    pause
    exit /b 1
)

for /f "delims=" %%D in ('dir /b /ad 2^>nul') do (
    if /i not "%%D"=="_uninstall_backup_" (
        rd /s /q "%%D" 2>nul
        if not errorlevel 1 echo     删除目录: %%D
    )
)

for /f "delims=" %%F in ('dir /b /a-d 2^>nul') do (
    if /i not "%%F"=="!SCRIPT_NAME!" (
        del /f /q "%%F" 2>nul
        if not errorlevel 1 echo     删除文件: %%F
    )
)

popd

echo.
echo   恢复保留的数据目录...
if exist "!TEMP_BACKUP!\databases" move "!TEMP_BACKUP!\databases" "!INSTALL_DIR!\databases" >nul 2>&1
if exist "!TEMP_BACKUP!\dev_tools" move "!TEMP_BACKUP!\dev_tools" "!INSTALL_DIR!\dev_tools" >nul 2>&1
if exist "!TEMP_BACKUP!\lang_envs" move "!TEMP_BACKUP!\lang_envs" "!INSTALL_DIR!\lang_envs" >nul 2>&1
if exist "!TEMP_BACKUP!\workspace" move "!TEMP_BACKUP!\workspace" "!INSTALL_DIR!\workspace" >nul 2>&1
if exist "!TEMP_BACKUP!\jieenv\data" (
    mkdir "!INSTALL_DIR!\jieenv" 2>nul
    move "!TEMP_BACKUP!\jieenv\data" "!INSTALL_DIR!\jieenv\data" >nul 2>&1
)
rd /s /q "!TEMP_BACKUP!" 2>nul

if exist "!INSTALL_DIR!\jieenv\data\jieenvmain.db" (
    echo   删除框架主数据库 jieenv\data\jieenvmain.db...
    del /f /q "!INSTALL_DIR!\jieenv\data\jieenvmain.db" 2>nul
)

echo.
echo   清理 Windows 卸载面板注册信息...
reg delete "%UNINSTALL_KEY%" /f >nul 2>&1
if errorlevel 1 (
    echo     [WARN] 注册表删除失败，可手动在控制面板中卸载
) else (
    echo     已清除注册表项
)

echo.
echo   清理桌面快捷方式...
set "LNK_FOUND=0"

for %%P in (
    "%USERPROFILE%\Desktop"
    "%USERPROFILE%\OneDrive\Desktop"
    "%PUBLIC%\Desktop"
    "%ALLUSERSPROFILE%\Desktop"
) do (
    if exist %%~P\JieEnv*.lnk (
        for %%F in (%%~P\JieEnv*.lnk) do (
            del /f /q "%%F" 2>nul
            if not errorlevel 1 (
                echo     已删除: %%F
                set "LNK_FOUND=1"
            )
        )
    )
)

for /f "tokens=*" %%D in ('powershell -noprofile -command "[Environment]::GetFolderPath('Desktop'); [Environment]::GetFolderPath('CommonDesktopDirectory')" 2^>nul') do (
    if exist "%%D\JieEnv*.lnk" (
        for %%F in ("%%D\JieEnv*.lnk") do (
            del /f /q "%%F" 2>nul
            if not errorlevel 1 (
                echo     已删除: %%F
                set "LNK_FOUND=1"
            )
        )
    )
)

if "!LNK_FOUND!"=="0" echo     (未找到 JieEnv 快捷方式，可能已被手动删除)

echo.
echo ==================================================
echo.
echo   卸载完成！
echo.
echo   已清理：程序文件 + 注册表注册信息 + 桌面快捷方式 + jieenvmaindb
echo   已保留：databases / dev_tools / lang_envs / workspace / jieenv\data\(除主数据库)
echo.
echo   如需彻底清理，可手动删除 !INSTALL_DIR! 整个目录
echo   （或此脚本会在关闭时自删）
echo.
echo ==================================================
echo.
pause

echo.
echo   正在自删...
cd /d "%~dp0"
del /f /q "%~f0" >nul 2>&1
rd /s /q "%~dp0" 2>nul
endlocal
exit /b 0