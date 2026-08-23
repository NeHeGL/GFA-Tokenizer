@echo off
setlocal
title GFA Detokenizer - Install

cd /d "%~dp0"

echo.
echo  ============================================================
echo   GFA Detokenizer - Setup
echo  ============================================================
echo.

echo  [INFO] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found. Please install Python from https://www.python.org/downloads/
    echo          Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

:: -- Create .venv if it doesn't exist --------------------------
if not exist ".venv\Scripts\python.exe" (
    echo  [INFO] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo  [ERROR] Failed to create .venv.
        pause
        exit /b 1
    )
    echo  [OK] Virtual environment created.
    echo.
) else (
    echo  [OK] Virtual environment already exists.
    echo.
)

:: -- Install / upgrade requirements ------------------------------
echo  [INFO] Installing requirements...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo  [ERROR] Installation failed. See errors above.
    pause
    exit /b 1
)

echo.
echo  [OK] All packages installed successfully.
echo.
echo  You can now run the app with start_app.bat
echo.
if not defined GFA_AUTO_INSTALL pause
