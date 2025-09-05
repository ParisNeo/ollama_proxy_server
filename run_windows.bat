@echo off
setlocal enabledelayedexpansion

:: ==================================================================
:: Ollama Proxy Server - Python-Powered Installer for Windows
:: ==================================================================
:: This script now delegates the fragile .env creation to a Python
:: script (`setup_wizard.py`) to guarantee a correct setup.

set VENV_DIR=venv
set REQUIREMENTS_FILE=requirements.txt
set STATE_FILE=.setup_state
set SETUP_WIZARD_SCRIPT=setup_wizard.py

:: ------------------------------------------------------------------
:: 1. PRE-CHECKS
:: ------------------------------------------------------------------
echo [INFO] Checking for Python installation...
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python not found in your PATH. Please install Python 3.11+.
    pause
    exit /b 1
)
echo [SUCCESS] Python found.

set "CURRENT_STATE=0"
if exist "%STATE_FILE%" (
    set /p CURRENT_STATE=<%STATE_FILE%
)

if %CURRENT_STATE% GEQ 4 (
    if not exist ".env" (
        echo.
        echo *****************************************************************
        echo * [WARNING] The setup is complete, but the '.env' file is missing!
        echo *****************************************************************
        echo.
        set /p REBUILD_CHOICE="Do you want to run the setup wizard again? (y/n): "
        if /i "!REBUILD_CHOICE!"=="y" (
            echo [INFO] Resetting setup state...
            del /f "%STATE_FILE%" >nul 2>nul
            set "CURRENT_STATE=0"
            echo.
        ) else (
            echo [INFO] Aborting.
            pause
            exit /b 0
        )
    )
)

if %CURRENT_STATE% GEQ 4 goto start_server

:: ==================================================================
::                       SETUP WIZARD (RESUMABLE)
:: ==================================================================
echo [INFO] Setup state is !CURRENT_STATE!/4. Starting or resuming installation...

if %CURRENT_STATE% GEQ 1 goto setup_step_2
echo [INFO] [1/4] Creating Python virtual environment...
python -m venv %VENV_DIR%
if %errorlevel% neq 0 ( echo [ERROR] Failed to create virtual environment. & pause & exit /b 1 )
(echo 1) > %STATE_FILE%
echo [SUCCESS] Virtual environment created.

:setup_step_2
call .\%VENV_DIR%\Scripts\activate.bat

if %CURRENT_STATE% GEQ 2 goto setup_step_3
echo [INFO] [2/4] Installing dependencies...
pip install --no-cache-dir -r %REQUIREMENTS_FILE%
if %errorlevel% neq 0 ( echo [ERROR] Failed to install Python packages. & pause & exit /b 1 )
(echo 2) > %STATE_FILE%
echo [SUCCESS] Dependencies installed.

:setup_step_3
if %CURRENT_STATE% GEQ 4 goto setup_step_4
echo [INFO] [3/4] Launching Python setup wizard for configuration...
python %SETUP_WIZARD_SCRIPT%
if %errorlevel% neq 0 (
    echo [ERROR] The Python setup wizard failed to create the .env file.
    pause
    exit /b 1
)
(echo 3) > %STATE_FILE%
echo [SUCCESS] .env file created successfully by Python wizard.

:setup_step_4
if %CURRENT_STATE% GEQ 4 goto all_setup_done
echo [INFO] [4/4] Initializing the database with Alembic...
alembic upgrade head
if %errorlevel% neq 0 (
    echo [ERROR] Failed to initialize the database. The app configuration might be invalid.
    pause
    exit /b 1
)
(echo 4) > %STATE_FILE%
echo [SUCCESS] Database is up-to-date.

:all_setup_done
echo.
echo [SUCCESS] Setup complete!
echo.

:: ==================================================================
::                            START THE SERVER
:: ==================================================================
:start_server
echo [INFO] Activating virtual environment...
call .\%VENV_DIR%\Scripts\activate.bat

echo [INFO] Setting Python Path...
set PYTHONPATH=.

set PORT_TO_USE=8080
for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
    if /i "%%a"=="PROXY_PORT" set "PORT_TO_USE=%%~b"
)

echo [INFO] Starting Ollama Proxy Server on port !PORT_TO_USE!...
echo To stop the server, simply close this window or press Ctrl+C.
echo.

uvicorn app.main:app --host 0.0.0.0 --port !PORT_TO_USE!

echo [INFO] Server has been stopped.
pause
exit /b