@echo off
setlocal enabledelayedexpansion

:: ==================================================================
:: Ollama Proxy Server - Dependency Installer for Windows
:: ==================================================================
:: This script installs/upgrades all dependencies from requirements.txt

set VENV_DIR=venv
set REQUIREMENTS_FILE=requirements.txt

:: ------------------------------------------------------------------
:: 1. PRE-CHECKS
:: ------------------------------------------------------------------
echo [INFO] Checking for Python installation...
where python >nul 2>nul
if %errorlevel% neq 0 ( 
    echo [ERROR] Python not found. Please install Python 3.11+ first.
    pause 
    exit /b 1 
)
echo [SUCCESS] Python found.

:: Check if virtual environment exists
if not exist "%VENV_DIR%" (
    echo [INFO] Virtual environment not found. Creating it...
    python -m venv %VENV_DIR%
    if %errorlevel% neq 0 ( 
        echo [ERROR] Failed to create virtual environment. 
        pause 
        exit /b 1 
    )
    echo [SUCCESS] Virtual environment created.
)

:: ------------------------------------------------------------------
:: 2. ACTIVATE VENV AND INSTALL DEPENDENCIES
:: ------------------------------------------------------------------
echo [INFO] Activating virtual environment...
call .\%VENV_DIR%\Scripts\activate.bat
if %errorlevel% neq 0 ( 
    echo [ERROR] Failed to activate virtual environment. 
    pause 
    exit /b 1 
)

echo [INFO] Upgrading pip to latest version...
python -m pip install --upgrade pip
if %errorlevel% neq 0 ( 
    echo [WARNING] Failed to upgrade pip, continuing anyway... 
)

echo.
echo [INFO] Installing/upgrading dependencies from requirements.txt...
echo [INFO] This may take several minutes, especially for transformer libraries (torch, transformers, etc.)...
echo.

pip install --upgrade -r %REQUIREMENTS_FILE%
if %errorlevel% neq 0 ( 
    echo [ERROR] Failed to install some packages. Check the error messages above.
    pause 
    exit /b 1 
)

echo.
echo [INFO] Verifying critical dependencies...
python -c "import sentence_transformers; print('[OK] sentence-transformers')" 2>nul || echo [WARNING] sentence-transformers not available
python -c "import transformers; print('[OK] transformers')" 2>nul || echo [WARNING] transformers not available
python -c "import torch; print('[OK] torch')" 2>nul || echo [WARNING] torch not available
python -c "import chromadb; print('[OK] chromadb')" 2>nul || echo [WARNING] chromadb not available
python -c "import langchain; print('[OK] langchain')" 2>nul || echo [WARNING] langchain not available
python -c "import safetensors; print('[OK] safetensors')" 2>nul || echo [WARNING] safetensors not available

echo.
echo [SUCCESS] Installation complete!
echo [INFO] You can now run the server using: run_windows.bat
echo.
pause
exit /b 0

