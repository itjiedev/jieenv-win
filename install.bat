@echo off
cd /d "%~dp0"

echo.
echo ==================================================
echo      JieEnv 首次安装向导
echo ==================================================
echo.

:: -- 检查 Python 运行时 --
if not exist "runtime\python.exe" (
    echo [错误] 未找到 Python 运行时（runtime\python.exe）！
    echo.
    echo   便携包应自带 runtime/ 目录，请确认解压完整。
    echo   源码部署请先到以下地址下载 Python 嵌入式版并放入 runtime/ 目录：
    echo   https://www.python.org/ftp/python/3.13.15/python-3.13.15-amd64.zip
    echo.
    pause
    exit /b 1
)

:: -- 步骤 1/4：初始化项目结构 --
echo [1/4] 初始化项目结构（创建目录、迁移数据库）...
echo.
runtime\python.exe jieenv\manage.py jieinit --skip-config
if errorlevel 1 (
    echo.
    echo [错误] 项目初始化失败，请检查上方错误信息。
    goto :fail
)
echo.
echo       项目结构初始化完成。
echo.

echo [2/4] 注册必须应用...
echo.
runtime\python.exe jieenv\manage.py appinstall --path apps.mainapp
runtime\python.exe jieenv\manage.py appinstall --path apps.appmanage
runtime\python.exe jieenv\manage.py appinstall --path apps.project_mgmt
runtime\python.exe jieenv\manage.py appinstall --path apps.python_main
runtime\python.exe jieenv\manage.py appinstall --path apps.python_archive
runtime\python.exe jieenv\manage.py appinstall --path apps.python_installer
runtime\python.exe jieenv\manage.py appinstall --path apps.dbmysql
runtime\python.exe jieenv\manage.py appinstall --path apps.conda
runtime\python.exe jieenv\manage.py refreshcache
if errorlevel 1 (
    echo.
    echo [错误] 注册应用失败~
    goto :fail
)
echo.
echo       必须应用注册完成。
echo.


:: -- 步骤 2/4：刷新端口 --
echo [3/4] 分配服务端口...
call refreshport.bat
echo.

:: -- 步骤 3/4：创建桌面快捷方式 --
echo [4/4] 创建桌面快捷方式...
runtime\python.exe create_shortcut.py "%~dp0"
if errorlevel 1 (
    echo       桌面快捷方式创建失败（可手动创建 run_desktop.bat 的快捷方式）。
) else (
    echo       桌面快捷方式已创建。
)
echo.

:: -- 步骤 4/4：注册到 Windows 卸载面板 --
echo [5/5] 注册到系统卸载面板...
set "UNINSTALL_KEY=HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\JieEnv-win"
set "INSTALL_DIR=%~dp0"
rem 去掉结尾反斜杠：注册表 InstallLocation 不应该带尾巴
if "%INSTALL_DIR:~-1%"=="\" set "INSTALL_DIR=%INSTALL_DIR:~0,-1%"

set "PLATFORM_VERSION="
if exist "_build_helper.py" (
    for /f "delims=" %%v in ('runtime\python.exe _build_helper.py version 2^>nul') do set "PLATFORM_VERSION=%%v"
)
if "%PLATFORM_VERSION%"=="" set "PLATFORM_VERSION=未知"

set "_REG_OK=1"
reg add "%UNINSTALL_KEY%" /v "DisplayName" /t REG_SZ /d "JieEnv(win)环境管理" /f >nul 2>&1 || set "_REG_OK=0"
reg add "%UNINSTALL_KEY%" /v "DisplayVersion" /t REG_SZ /d "%PLATFORM_VERSION%" /f >nul 2>&1 || set "_REG_OK=0"
reg add "%UNINSTALL_KEY%" /v "InstallLocation" /t REG_SZ /d "%INSTALL_DIR%" /f >nul 2>&1 || set "_REG_OK=0"
reg add "%UNINSTALL_KEY%" /v "Publisher" /t REG_SZ /d "JieSoft" /f >nul 2>&1 || set "_REG_OK=0"
rem 注意：UninstallString / DisplayIcon 不需要把引号写进值里，reg add /d 会自己处理路径
rem 如果值本身包含引号，控制面板读取时会原样带出，导致卸载图标/功能异常
reg add "%UNINSTALL_KEY%" /v "UninstallString" /t REG_SZ /d "%INSTALL_DIR%\uninstall.bat" /f >nul 2>&1 || set "_REG_OK=0"
if exist "favicon.ico" (
    reg add "%UNINSTALL_KEY%" /v "DisplayIcon" /t REG_SZ /d "%INSTALL_DIR%\favicon.ico" /f >nul 2>&1 || set "_REG_OK=0"
)
reg add "%UNINSTALL_KEY%" /v "NoModify" /t REG_DWORD /d 1 /f >nul 2>&1 || set "_REG_OK=0"
reg add "%UNINSTALL_KEY%" /v "NoRepair" /t REG_DWORD /d 1 /f >nul 2>&1 || set "_REG_OK=0"

if "%_REG_OK%"=="0" (
    echo       [WARN] 部分注册表写入失败，稍后可手动卸载
) else (
    echo       已注册到 Windows 卸载面板 ^(可在设置/控制面板卸载^)
)
echo.

:: -- 完成 --
echo ==================================================
echo.
echo   安装完成！
echo.
echo   启动方式：
echo     - 双击桌图 JieEnv(win)环境管理 图标  启动桌面模式（推荐）
echo     - 双击 run_desktop.bat
echo.
echo   安装目录: %~dp0
echo.
echo ==================================================
echo.
pause
exit /b 0

:fail
echo.
echo 安装失败，请检查上述错误信息。
echo.
pause
exit /b 1