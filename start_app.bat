@echo off
setlocal
title GFA Tokenizer

cd /d "%~dp0"

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

echo.
echo Starting GFA Tokenizer...
".venv\Scripts\python.exe" gfa_tokenizer.py %*
if errorlevel 1 (
    echo.
    echo ERROR: GFA Tokenizer exited with an error. See above for details.
    pause
)
