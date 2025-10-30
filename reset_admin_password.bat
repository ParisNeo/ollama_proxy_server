@echo off
setlocal

:: ==================================================================
:: Ollama Proxy Fortress - Admin Password Reset Runner for Windows
:: ==================================================================

set VENV_DIR=venv

:: --- Check if virtual environment exists ---
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo [ERROR] Python virtual environment not found at '.\%VENV_DIR%'.
    echo [ERROR] Please run 'run_windows.bat' first to complete the setup.
    pause
    exit /b 1
)

:: --- Activate virtual environment and run the script ---
echo [INFO] Activating Python virtual environment...
call .\%VENV_DIR%\Scripts\activate.bat

echo [INFO] Running the admin password reset script...
echo.

python reset_admin_password.py

echo.
echo [INFO] Script finished.
pause