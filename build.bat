@echo off
setlocal
title GFA Tokenizer - Build EXE

cd /d "%~dp0"

echo.
echo  ============================================================
echo   GFA Tokenizer - Build EXE
echo  ============================================================
echo.

:: -- Make sure the virtual environment exists --------------------
if not exist ".venv\Scripts\python.exe" (
    echo  [INFO] Virtual environment not found. Running installer...
    echo.
    set GFA_AUTO_INSTALL=1
    call "%~dp0install.bat"
    set GFA_AUTO_INSTALL=
    if errorlevel 1 (
        echo  [ERROR] Installation failed. Fix the errors above and try again.
        pause
        exit /b 1
    )
)

:: -- Regenerate the icon if it's missing --------------------------
if not exist "icon.ico" (
    echo  [INFO] icon.ico not found, generating it...
    ".venv\Scripts\python.exe" -m pip install --quiet Pillow
    ".venv\Scripts\python.exe" make_icon.py
    if errorlevel 1 (
        echo  [ERROR] Icon generation failed.
        pause
        exit /b 1
    )
    echo.
)

:: -- Install PyInstaller (build-time only, not in requirements.txt) --
echo  [INFO] Installing PyInstaller...
".venv\Scripts\python.exe" -m pip install --quiet pyinstaller
if errorlevel 1 (
    echo  [ERROR] Failed to install PyInstaller.
    pause
    exit /b 1
)

:: -- Build ---------------------------------------------------------
echo  [INFO] Building GFA Tokenizer.exe...
echo.
".venv\Scripts\python.exe" -m PyInstaller "GFA Tokenizer.spec" --noconfirm
if errorlevel 1 (
    echo.
    echo  [ERROR] Build failed. See errors above.
    pause
    exit /b 1
)

echo.
echo  [OK] Build complete: dist\GFA Tokenizer.exe
echo.
pause
