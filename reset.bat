@echo off
setlocal enabledelayedexpansion

:: ==================================================================
::
::   Ollama Proxy Fortress - FULL RESET SCRIPT
::   For: Windows
::
:: ==================================================================

cls

:: --- Display the Irreversible Action Warning ---
echo #####################################################################
echo #                                                                   #
echo #                      W  A  R  N  I  N  G                          #
echo #                                                                   #
echo #####################################################################
echo.
echo This script will completely reset the application.
echo The following actions will be performed:
echo.
echo   - The Python virtual environment ('venv') will be deleted.
echo   - The configuration file ('.env') will be deleted.
echo   - The setup state file ('.setup_state') will be deleted.
echo   - The database ('ollama_proxy.db') will be deleted.
echo   - All generated Python cache files ('__pycache__') will be removed.
echo.
echo THIS OPERATION IS IRREVERSIBLE.
echo All your users, API keys, and settings will be permanently lost.
echo.

:: --- Require User Confirmation ---
set "CONFIRMATION="
set /p CONFIRMATION="To confirm you want to proceed, please type 'reset now': "

if /i not "!CONFIRMATION!"=="reset now" (
    echo.
    echo Confirmation not received. Reset has been cancelled.
    pause
    exit /b 1
)

echo.
echo Confirmation received. Proceeding with the reset...
echo.

:: --- Perform Deletion Actions ---

echo 1. Deleting Python virtual environment ('venv')...
if exist "venv" (
    rmdir /s /q "venv"
    echo    - Done.
) else (
    echo    - Not found.
)

echo 2. Deleting configuration file ('.env')...
if exist ".env" (
    del ".env"
    echo    - Done.
) else (
    echo    - Not found.
)

echo 3. Deleting setup state file ('.setup_state')...
if exist ".setup_state" (
    del ".setup_state"
    echo    - Done.
) else (
    echo    - Not found.
)

echo 4. Deleting database file ('ollama_proxy.db')...
if exist "ollama_proxy.db" (
    del "ollama_proxy.db"
    del "ollama_proxy.db-journal" 2>nul
    echo    - Done.
) else (
    echo    - Not found.
)

echo 5. Deleting Alembic versions (optional, for clean migration history)...
if exist "alembic\versions" (
    for /d %%i in ("alembic\versions\*") do (
        if /i not "%%~nxi"==".gitkeep" (
            del "%%i" 2>nul
        )
    )
    echo    - Done.
) else (
    echo    - Not found.
)

echo 6. Cleaning up Python cache files...
for /d /r . %%d in (__pycache__) do (
    if exist "%%d" (
        rmdir /s /q "%%d"
    )
)
echo    - Done.

echo.
echo #####################################################################
echo #                                                                   #
echo #                         RESET COMPLETE                            #
echo #                                                                   #
echo #####################################################################
echo.
echo You can now run 'run_windows.bat' to start a fresh installation.
echo.
pause