@echo off
chcp 65001 >nul 2>&1
title GDUST Timetable Tool
cd /d "%~dp0"

echo ========================================
echo   GDUST Timetable Tool
echo ========================================
echo.

:: Check Python
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Please install Python 3.9+ first.
    echo         https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"') do set PY_VER=%%i
echo [OK] Python %PY_VER%

:: pip mirror source
set PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
set PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

:: Install dependencies
echo.
echo [INFO] Checking dependencies...
python -c "import flask" >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Installing flask...
    pip install flask -i %PIP_INDEX% --trusted-host %PIP_TRUSTED_HOST% -q
    if %errorlevel% neq 0 (
        echo [WARN] flask install failed, trying default source...
        pip install flask -q
    )
)
python -c "import requests" >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Installing requests...
    pip install requests -i %PIP_INDEX% --trusted-host %PIP_TRUSTED_HOST% -q
)
python -c "import ddddocr" >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] ddddocr installed (auto captcha available)
) else (
    echo [SKIP] ddddocr not installed (optional, manual captcha only)
)

:: Copy config
if not exist config.json (
    if exist config.example.json (
        echo.
        echo [INFO] First run, copying config template...
        copy config.example.json config.json >nul
        echo [INFO] Please fill in your info via the web UI
    )
)

:: Launch
echo.
echo ========================================
echo   Starting Web GUI...
echo   Open browser: http://localhost:5000
echo   Press Ctrl+C to stop
echo ========================================
echo.

python app.py

echo.
echo [INFO] Server stopped.
pause
