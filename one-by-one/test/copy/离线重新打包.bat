@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul

for %%I in ("%~dp0.") do set "APP_DIR=%%~fI"
set "OFFLINE_DIR=%APP_DIR%\offline_build"
set "PYTHON_DIR=%OFFLINE_DIR%\python"
set "PYTHON_EXE=%PYTHON_DIR%\python.exe"
set "PACKAGES_DIR=%OFFLINE_DIR%\packages"
set "SOURCE_FILE=%APP_DIR%\copy_gui.py"
set "OUTPUT_EXE=%APP_DIR%\TIF批量复制工具.exe"

echo ============================================================
echo              TIF 批量复制工具 - 离线重新打包
echo ============================================================
echo.

if not exist "%PYTHON_EXE%" (
    echo 错误：找不到离线 Python：
    echo %PYTHON_EXE%
    goto failed
)
if not exist "%PACKAGES_DIR%\PyInstaller" (
    echo 错误：找不到离线 PyInstaller：
    echo %PACKAGES_DIR%
    goto failed
)
if not exist "%SOURCE_FILE%" (
    echo 错误：找不到源码：
    echo %SOURCE_FILE%
    goto failed
)

set "PYTHONHOME=%PYTHON_DIR%"
set "PYTHONPATH=%PACKAGES_DIR%"
set "PYTHONNOUSERSITE=1"
set "PYTHONDONTWRITEBYTECODE=1"

echo Python：
"%PYTHON_EXE%" --version
if errorlevel 1 goto failed

echo.
echo 正在检查源码语法...
"%PYTHON_EXE%" -m py_compile "%SOURCE_FILE%"
if errorlevel 1 (
    echo.
    echo 源码存在语法错误，请根据上方提示修改。
    goto failed
)

echo.
echo 正在生成 EXE，请稍候...
echo 输出：%OUTPUT_EXE%
echo.

"%PYTHON_EXE%" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --windowed ^
    --name "TIF批量复制工具" ^
    --distpath "%APP_DIR%" ^
    --workpath "%OFFLINE_DIR%\work" ^
    --specpath "%OFFLINE_DIR%" ^
    "%SOURCE_FILE%"

if errorlevel 1 goto failed
if not exist "%OUTPUT_EXE%" goto failed

echo.
echo ============================================================
echo 打包成功：
echo %OUTPUT_EXE%
echo ============================================================
echo.
echo 按任意键关闭窗口...
pause >nul
exit /b 0

:failed
echo.
echo ============================================================
echo 打包失败，请查看上方错误信息。
echo ============================================================
echo.
echo 按任意键关闭窗口...
pause >nul
exit /b 1
