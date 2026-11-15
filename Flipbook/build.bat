@echo off
setlocal
cd /d "%~dp0"
rem Since v1.8.7 the build uses Python 3.13 in the project's .venv (see dev\requirements-dev.txt).
set "PY=python"
if exist "%~dp0..\.venv\Scripts\python.exe" set "PY=%~dp0..\.venv\Scripts\python.exe"

if not exist "flipbook.ico" (
    echo Generating icon...
    "%PY%" make_icon.py
    if errorlevel 1 goto :fail
)

"%PY%" -m PyInstaller --noconfirm --distpath "%~dp0." --workpath "%~dp0_build" Flipbook.spec
if errorlevel 1 goto :fail

echo.
echo SUCCESS: "%~dp0Flipbook.exe" built.
endlocal
exit /b 0

:fail
echo.
echo BUILD FAILED.
pause
endlocal
exit /b 1
