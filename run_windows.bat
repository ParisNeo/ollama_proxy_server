@echo off
setlocal enabledelayedexpansion

:: This script sets up and runs the Ollama Proxy Server on Windows.
:: It has been rewritten to be more robust.
:: On first run, it will guide you through setup.
:: Afterwards, it will just start the server.

set VENV_DIR=venv

:: --- Check for Python ---
echo [INFO] Checking for Python installation...
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python not found in your PATH.
    echo Please install Python 3.11+ from https://python.org or the Microsoft Store.
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)
echo [SUCCESS] Python found.

:: --- Check if setup is needed ---
if exist "%VENV_DIR%\Scripts\activate.bat" goto :start_server


:: ##################################################################
:: #                    FIRST-TIME SETUP SCRIPT                     #
:: ##################################################################
echo.
echo [INFO] First-time setup detected. Configuring the server...

:: 1. Create Virtual Environment
echo [INFO] Creating Python virtual environment...
python -m venv %VENV_DIR%
if %errorlevel% neq 0 (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
)

:: 2. Install Dependencies
echo [INFO] Activating environment and installing dependencies (this may take a moment)...
call %VENV_DIR%\Scripts\activate.bat
pip install --no-cache-dir -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install Python packages.
    pause
    exit /b 1
)

:: 3. Gather Configuration
echo [INFO] Please provide the following configuration:
set /p PROXY_PORT="Enter the port for the proxy server to listen on [8080]: "
if not defined PROXY_PORT set PROXY_PORT=8080

set /p OLLAMA_SERVERS="Enter the backend Ollama server URL(s), comma-separated [http://127.0.0.1:11434]: "
if not defined OLLAMA_SERVERS set OLLAMA_SERVERS=http://127.0.0.1:11434

set /p REDIS_URL="Enter the Redis URL for rate limiting [redis://localhost:6379/0]: "
if not defined REDIS_URL set REDIS_URL=redis://localhost:6379/0

set /p ADMIN_USER="Enter a username for the admin dashboard [admin]: "
if not defined ADMIN_USER set ADMIN_USER=admin

set /p ADMIN_PASSWORD="Enter a password for the admin user: "
if not defined ADMIN_PASSWORD (
    echo [ERROR] Admin password cannot be empty.
    pause
    exit /b 1
)

set /p ALLOWED_IPS_INPUT="Enter allowed IPs, comma-separated (e.g., 127.0.0.1,localhost) [*]: "
if not defined ALLOWED_IPS_INPUT set ALLOWED_IPS_INPUT=*

set /p DENIED_IPS_INPUT="Enter denied IPs, comma-separated (leave empty for none): "


:: 4. Generate .env file
echo [INFO] Generating .env configuration file...
(
    echo # Application Settings
    REM --- FIX START ---
    REM Removed quotes from simple key-value pairs
    echo APP_NAME=Ollama Proxy Server
    echo APP_VERSION=8.0.0
    echo LOG_LEVEL=info
    echo PROXY_PORT=!PROXY_PORT!
    echo OLLAMA_SERVERS=!OLLAMA_SERVERS!
    echo DATABASE_URL=sqlite+aiosqlite:///./ollama_proxy.db
    echo ADMIN_USER=!ADMIN_USER!
    echo ADMIN_PASSWORD=!ADMIN_PASSWORD!
    echo SECRET_KEY=!RANDOM!!RANDOM!!RANDOM!!RANDOM!
    echo.
    echo # --- Advanced Security ---
    echo REDIS_URL=!REDIS_URL!
    echo RATE_LIMIT_REQUESTS=100
    echo RATE_LIMIT_WINDOW_MINUTES=1
    REM --- FIX END ---
    
    REM Correctly formats the ALLOWED_IPS value. These need special formatting.
    if "!ALLOWED_IPS_INPUT!"=="*" (
        echo ALLOWED_IPS='["*"]'
    ) else (
        set formatted_allowed_ips=!ALLOWED_IPS_INPUT:,=","!
        echo ALLOWED_IPS='["!formatted_allowed_ips!"]'
    )
    
    REM Correctly formats the DENIED_IPS value. These need special formatting.
    if not defined DENIED_IPS_INPUT (
        echo DENIED_IPS='[]'
    ) else (
        set formatted_denied_ips=!DENIED_IPS_INPUT:,=","!
        echo DENIED_IPS='["!formatted_denied_ips!"]'
    )

) > .env

:: 5. Initialize Database
echo [INFO] Initializing the database...
alembic upgrade head
if %errorlevel% neq 0 (
    echo [ERROR] Failed to initialize the database.
    pause
    exit /b 1
)

echo [SUCCESS] First-time setup complete!
echo.
goto :start_server

:: ##################################################################
:: #                       SERVER START SCRIPT                      #
:: ##################################################################
:start_server
echo [INFO] Activating virtual environment...
call %VENV_DIR%\Scripts\activate.bat

echo [INFO] Setting Python Path...
set PYTHONPATH=.

:: Read the port from the .env file for the status message
set PORT_TO_USE=8080
for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
    if "%%a"=="PROXY_PORT" set PORT_TO_USE=%%b
)

echo [INFO] Starting Ollama Proxy Server on port !PORT_TO_USE!...
echo To stop the server, simply close this window or press Ctrl+C.

:: Uvicorn is used directly as Gunicorn is not available on Windows.
uvicorn app.main:app --host 0.0.0.0 --port !PORT_TO_USE!

echo [INFO] Server has been stopped.
pause