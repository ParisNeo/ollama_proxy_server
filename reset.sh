#!/bin/bash

# ====================================================================
#
#   Ollama Proxy Fortress - FULL RESET SCRIPT
#   For: macOS & Linux
#
# ====================================================================

# --- Colors for the warning message ---
COLOR_RESET='\e[0m'
COLOR_ERROR='\e[1;31m'    # Bold Red
COLOR_WARN='\e[1;33m'     # Bold Yellow

clear

# --- Display the Irreversible Action Warning ---
echo -e "${COLOR_ERROR}!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
echo -e "${COLOR_ERROR}!!                    W A R N I N G                   !!"
echo -e "${COLOR_ERROR}!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!${COLOR_RESET}"
echo
echo -e "${COLOR_WARN}This script will completely reset the application.${COLOR_RESET}"
echo "The following actions will be performed:"
echo
echo "  - The Python virtual environment ('venv') will be deleted."
echo "  - The configuration file ('.env') will be deleted."
echo "  - The setup state file ('.setup_state') will be deleted."
echo "  - The database ('ollama_proxy.db') will be deleted."
echo "  - All generated Python cache files ('__pycache__') will be removed."
echo
echo -e "${COLOR_ERROR}This operation is IRREVERSIBLE and all your users, API keys,"
echo -e "and settings will be permanently lost.${COLOR_RESET}"
echo

# --- Require User Confirmation ---
read -p "To confirm you want to proceed, please type 'reset now': " CONFIRMATION

if [[ "$CONFIRMATION" != "reset now" ]]; then
    echo
    echo "Confirmation not received. Reset has been cancelled."
    exit 1
fi

echo
echo "Confirmation received. Proceeding with the reset..."
echo

# --- Perform Deletion Actions ---

echo "1. Deleting Python virtual environment ('venv')..."
rm -rf venv
echo "   - Done."

echo "2. Deleting configuration file ('.env')..."
rm -f .env
echo "   - Done."

echo "3. Deleting setup state file ('.setup_state')..."
rm -f .setup_state
echo "   - Done."

echo "4. Deleting database file ('ollama_proxy.db')..."
rm -f ollama_proxy.db ollama_proxy.db-journal
echo "   - Done."

echo "5. Deleting Alembic versions (optional, for clean migration history)..."
# In case of corrupted migrations, this is a good idea.
# We recreate the directory so alembic doesn't fail.
if [ -d "alembic/versions" ]; then
    rm -rf alembic/versions/*
    touch alembic/versions/.gitkeep
fi
echo "   - Done."


echo "6. Cleaning up Python cache files..."
find . -type d -name "__pycache__" -exec rm -r {} +
echo "   - Done."

echo
echo -e "${COLOR_SUCCESS}Reset complete.${COLOR_RESET}"
echo "You can now run 'run.sh' to start a fresh installation."
echo