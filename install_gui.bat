@echo off
rem ============================================================
rem  py++ 图形安装器 - Windows 一键启动
rem  会依次尝试带 tkinter 的 Python 解释器，找到就启动 GUI。
rem ============================================================
setlocal
cd /d "%~dp0"

set "CANDIDATES="
for %%P in (python.exe py.exe) do (
    for /f "delims=" %%I in ('where %%P 2^>nul') do (
        set "CANDIDATES=!CANDIDATES! %%I"
    )
)

rem 常见官方安装路径
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" call :try "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" call :try "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" call :try "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"

for %%I in (python python3 py) do (
    call :try "%%I"
)

rem ---- 全部失败 ----
echo.
echo ============================================================
echo  未能找到带 tkinter 的 Python，无法启动图形安装器。
echo.
echo  你可以改用命令行安装器：
echo      python install.py
echo  或指定解释器：
echo      "C:\path\to\python.exe" install_gui.py
echo ============================================================
echo.
pause
exit /b 1

:try
if "%~1"=="" exit /b 1
"%~1" -c "import tkinter" >nul 2>&1
if errorlevel 1 exit /b 1
echo 使用解释器: %~1
"%~1" "%~dp0install_gui.py" %*
exit /b %errorlevel%
