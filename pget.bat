@echo off
rem ============================================================
rem  pget - py++ 包管理器 (Windows 一键启动)
rem  用法示例:
rem      pget install requests
rem      pget list
rem      pget --help
rem ============================================================
setlocal enabledelayedexpansion
set PYPP_PGET_TOOL=auto
cd /d "%~dp0"

rem 1) 优先用 py++ 自带的解释器（如果有）
if exist "%~dp0tools\python\python.exe" (
    "%~dp0tools\python\python.exe" "%~dp0pget.py" %*
    exit /b %errorlevel%
)

rem 2) 官方安装器常见位置
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%~dp0pget.py" %*
    exit /b %errorlevel%
)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%~dp0pget.py" %*
    exit /b %errorlevel%
)

rem 3) PATH 上的任意 Python
for %%I in (python python3 py) do (
    where %%I >nul 2>&1
    if !errorlevel! equ 0 (
        %%I "%~dp0pget.py" %*
        exit /b !errorlevel!
    )
)

echo.
echo ============================================================
echo  未找到可用的 Python 解释器，pget 无法启动。
echo  pget 依赖 Python 来解析与安装 wheel 包。
echo.
echo  请先安装 Python 3，或手动指定：
echo      "C:\path\to\python.exe" pget.py install requests
echo ============================================================
echo.
pause
exit /b 1
