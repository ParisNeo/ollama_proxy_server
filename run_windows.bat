@echo off
setlocal enabledelayedexpansion

:: ==================================================================
:: Ollama Proxy Server - Alembic-Free Installer for Windows
:: ==================================================================
:: This script now relies on the application to create the database.

set VENV_DIR=venv
set REQUIREMENTS_FILE=requirements.txt
set STATE_FILE=.setup_state
set SETUP_WIZARD_SCRIPT=setup_wizard.py

:: ------------------------------------------------------------------
:: 1. PRE-CHECKS
:: ------------------------------------------------------------------
echo [INFO] Checking for Python installation...
where python >nul 2>nul
if %errorlevel% neq 0 ( echo [ERROR] Python not found. & pause & exit /b 1 )
echo [SUCCESS] Python found.

set "CURRENT_STATE=0"
if exist "%STATE_FILE%" ( set /p CURRENT_STATE=<%STATE_FILE% )

if %CURRENT_STATE% GEQ 3 (
    if not exist ".env" (
        echo.
        echo *****************************************************************
        echo * [WARNING] Setup complete, but '.env' file is missing!
        echo *****************************************************************
        echo.
        set /p REBUILD_CHOICE="Run setup wizard again? (y/n): "
        if /i "!REBUILD_CHOICE!"=="y" (
            echo [INFO] Resetting setup state...
            del /f "%STATE_FILE%" >nul 2>nul
            set "CURRENT_STATE=0"
            echo.
        ) else (
            echo [INFO] Aborting. & pause & exit /b 0
        )
    )
)

if %CURRENT_STATE% GEQ 3 goto start_server

:: ==================================================================
::                       SETUP WIZARD (RESUMABLE)
:: ==================================================================
echo [INFO] Setup state is !CURRENT_STATE!/3. Starting or resuming installation...

if %CURRENT_STATE% GEQ 1 goto setup_step_2
echo [INFO] [1/3] Creating Python virtual environment...
python -m venv %VENV_DIR%
if %errorlevel% neq 0 ( echo [ERROR] Failed to create venv. & pause & exit /b 1 )
(echo 1) > %STATE_FILE%
echo [SUCCESS] Virtual environment created.

:setup_step_2
call .\%VENV_DIR%\Scripts\activate.bat

if %CURRENT_STATE% GEQ 2 goto setup_step_3
echo [INFO] [2/3] Installing dependencies...
pip install --no-cache-dir -r %REQUIREMENTS_FILE%
if %errorlevel% neq 0 ( echo [ERROR] Failed to install packages. & pause & exit /b 1 )
(echo 2) > %STATE_FILE%
echo [SUCCESS] Dependencies installed.

:setup_step_3
echo [INFO] [3/3] Launching Python setup wizard for configuration...
python %SETUP_WIZARD_SCRIPT%
if %errorlevel% neq 0 ( echo [ERROR] Setup wizard failed. & pause & exit /b 1 )
(echo 3) > %STATE_FILE%
echo [SUCCESS] .env file created.

echo.
echo [SUCCESS] Setup complete! The database will be created on first run.
echo.

:: ==================================================================
::                            START THE SERVER
:: ==================================================================
:start_server
echo [INFO] Activating virtual environment...
call .\%VENV_DIR%\Scripts\activate.bat

echo [INFO] Setting Python Path...
set PYTHONPATH=.

echo.
echo [INFO] Starting Ollama Proxy Server...
echo [INFO] Note: Uvicorn uses a logger named 'uvicorn.error' for general server messages.
echo [INFO] This does NOT necessarily indicate an error.
echo [INFO] To stop the server, simply close this window or press Ctrl+C.
echo.

python app/main.py

echo [INFO] Server has been stopped.
pause
exit /b
