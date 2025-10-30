#!/bin/bash
set -euo pipefail

# ====================================================================
#
#   Ollama Proxy Fortress - Admin Password Reset Runner
#
# ====================================================================

VENV_DIR="venv"

COLOR_RESET='\e[0m'; COLOR_INFO='\e[1;34m'; COLOR_ERROR='\e[1;31m'

print_info()    { echo -e "${COLOR_INFO}[INFO]${COLOR_RESET} $*"; }
print_error()   { echo -e "${COLOR_ERROR}[ERROR]${COLOR_RESET} $*" >&2; }

# --- Check if virtual environment exists ---
if [ ! -d "$VENV_DIR" ]; then
    print_error "Python virtual environment not found at './${VENV_DIR}'."
    print_error "Please run './run.sh' first to complete the setup."
    exit 1
fi

# --- Activate virtual environment and run the script ---
print_info "Activating Python virtual environment..."
source "${VENV_DIR}/bin/activate"

print_info "Running the admin password reset script..."
echo

python reset_admin_password.py

echo
print_info "Script finished."