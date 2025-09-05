#!/bin/bash
set -euo pipefail   # fail on error, undefined vars, and pipe failures

# ====================================================================
#
#   Ollama Proxy Fortress - Professional Installer & Runner
#   Version: 3.0 (with Linux Service Installer)
#   For: macOS & Linux
#
# ====================================================================

# --- Configuration ---
VENV_DIR="venv"
REQUIREMENTS_FILE="requirements.txt"
GUNICORN_CONF="gunicorn_conf.py"
APP_MODULE="app.main:app"
STATE_FILE=".setup_state"
SERVICE_NAME="ollama_proxy"
PROJECT_DIR=$(pwd) # Get the absolute path to the project directory

# --- Colors and Styling ---
COLOR_RESET='\e[0m'
COLOR_INFO='\e[1;34m'     # Bold Blue
COLOR_SUCCESS='\e[1;32m'  # Bold Green
COLOR_ERROR='\e[1;31m'    # Bold Red
COLOR_WARN='\e[1;33m'     # Bold Yellow
COLOR_HEADER='\e[1;35m'   # Bold Magenta

# --- Helper Functions ---
print_header()  { echo -e "\n${COLOR_HEADER}=====================================================${COLOR_RESET}"; \
                  echo -e "${COLOR_HEADER}$1${COLOR_RESET}"; \
                  echo -e "${COLOR_HEADER}=====================================================${COLOR_RESET}"; }
print_info()    { echo -e "${COLOR_INFO}[INFO]${COLOR_RESET} $*"; }
print_success() { echo -e "${COLOR_SUCCESS}[SUCCESS]${COLOR_RESET} $*"; }
print_error()   { echo -e "${COLOR_ERROR}[ERROR]${COLOR_RESET} $*" >&2; }
print_warn()    { echo -e "${COLOR_WARN}[WARNING]${COLOR_RESET} $*"; }

clear
print_header "    Ollama Proxy Fortress Installer & Runner"

# ====================================================================
# 1. PRE-CHECKS
# ====================================================================
print_info "Performing initial system checks..."
if ! command -v python3 &>/dev/null || ! python3 -m pip --version &>/dev/null || ! python3 -m venv -h &>/dev/null; then
    print_error "Python 3, pip, or venv is missing. Please ensure a complete Python 3 installation."
    exit 1
fi
print_success "Python 3, pip, and venv are available."

CURRENT_STATE=0
if [[ -f "$STATE_FILE" ]]; then CURRENT_STATE=$(cat "$STATE_FILE"); fi

if [[ "$CURRENT_STATE" -ge 4 ]] && [[ ! -f ".env" ]]; then
    print_warn "Setup complete, but '.env' file is missing! The server cannot start."
    read -p "Do you want to run the setup wizard again to create a new .env file? (y/n): " REBUILD_CHOICE
    if [[ "$REBUILD_CHOICE" =~ ^[Yy]$ ]]; then
        print_info "Resetting setup state..."
        rm -f "$STATE_FILE"
        CURRENT_STATE=0
    else
        print_info "Aborting."
        exit 0
    fi
fi

# ====================================================================
# 2. SETUP WIZARD (Resumable)
# ====================================================================
if [[ "$CURRENT_STATE" -lt 4 ]]; then
    print_info "Setup state is ${CURRENT_STATE}/4. Starting or resuming installation..."

    if [[ "$CURRENT_STATE" -lt 1 ]]; then
        print_header "--- [Step 1/4] Creating Python Virtual Environment ---"
        python3 -m venv "$VENV_DIR"
        echo "1" > "$STATE_FILE"
        print_success "Virtual environment created in './${VENV_DIR}'."
    fi
    source "$VENV_DIR/bin/activate"
    if [[ "$CURRENT_STATE" -lt 2 ]]; then
        print_header "--- [Step 2/4] Installing Python Dependencies ---"
        pip install --no-cache-dir -r "$REQUIREMENTS_FILE"
        echo "2" > "$STATE_FILE"
        print_success "All dependencies installed."
    fi
    if [[ "$CURRENT_STATE" -lt 3 ]]; then
        print_header "--- [Step 3/4] Server Configuration ---"
        read -p "   -> Port for the proxy server [8080]: " PROXY_PORT
        read -p "   -> Backend Ollama server(s) [http://127.0.0.1:11434]: " OLLAMA_SERVERS
        read -p "   -> Redis URL [redis://localhost:6379/0]: " REDIS_URL
        read -p "   -> Admin username [admin]: " ADMIN_USER
        ADMIN_PASSWORD=""
        while [[ -z "$ADMIN_PASSWORD" ]]; do
            read -s -p "   -> Admin password (cannot be empty): " ADMIN_PASSWORD; echo
            if [[ -z "$ADMIN_PASSWORD" ]]; then print_error "   Password cannot be empty."; fi
        done
        read -p "   -> Allowed IPs (comma-separated, leave empty for all): " ALLOWED_IPS
        read -p "   -> Denied IPs (comma-separated, leave empty for none): " DENIED_IPS
        print_info "Generating .env configuration file..."
        SECRET_KEY=$(openssl rand -hex 32)
        (
            echo "APP_NAME=\"Ollama Proxy Fortress\""; echo "APP_VERSION=\"8.0.0\""; echo "LOG_LEVEL=\"info\""
            echo "PROXY_PORT=\"${PROXY_PORT:-8080}\""
            echo "OLLAMA_SERVERS=\"${OLLAMA_SERVERS:-http://127.0.0.1:11434}\""
            echo "DATABASE_URL=\"sqlite+aiosqlite:///./ollama_proxy.db\""
            echo "ADMIN_USER=\"${ADMIN_USER:-admin}\""; echo "ADMIN_PASSWORD=\"${ADMIN_PASSWORD}\""
            echo "SECRET_KEY=\"${SECRET_KEY}\""
            echo "REDIS_URL=\"${REDIS_URL:-redis://localhost:6379/0}\""
            echo "RATE_LIMIT_REQUESTS=\"100\""; echo "RATE_LIMIT_WINDOW_MINUTES=\"1\""
            echo "ALLOWED_IPS=\"${ALLOWED_IPS}\""; echo "DENIED_IPS=\"${DENIED_IPS}\""
        ) > .env
        echo "3" > "$STATE_FILE"
        print_success ".env file created."
    fi
    if [[ "$CURRENT_STATE" -lt 4 ]]; then
        print_header "--- [Step 4/4] Initializing Database ---"
        alembic upgrade head
        echo "4" > "$STATE_FILE"
        print_success "Database migrated to the latest version."
    fi

    print_header "--- Setup Complete! ---"
    ADMIN_USER_FINAL=$(grep -E '^ADMIN_USER=' .env | cut -d '=' -f2 | tr -d '"')
    PORT_FINAL=$(grep -E '^PROXY_PORT=' .env | cut -d '=' -f2 | tr -d '"')
    print_success "Your Ollama Proxy Fortress is ready."
    print_info "Admin Dashboard: http://127.0.0.1:${PORT_FINAL}/admin"
    print_info "Admin Username:  ${ADMIN_USER_FINAL}"
fi

# ====================================================================
# 3. OPTIONAL: CREATE LINUX SYSTEMD SERVICE
# ====================================================================
SERVICE_CREATED=false
if [[ "$(uname)" == "Linux" ]] && command -v systemctl &>/dev/null; then
    print_header "--- Optional: Create a Systemd Service ---"
    print_info "A service will automatically start the proxy on boot and restart it if it fails."
    read -p "Do you want to create and enable a systemd service for this application? (y/n): " CREATE_SERVICE
    if [[ "$CREATE_SERVICE" =~ ^[Yy]$ ]]; then
        SERVICE_FILE_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
        print_info "Creating systemd service file..."

        # Using a 'here document' to create the service file content
        SERVICE_FILE_CONTENT=$(cat << EOF
[Unit]
Description=Ollama Proxy Fortress Service
After=network.target

[Service]
User=${USER}
Group=$(id -gn ${USER})
WorkingDirectory=${PROJECT_DIR}
Environment="PYTHONPATH=${PROJECT_DIR}"
ExecStart=${PROJECT_DIR}/${VENV_DIR}/bin/gunicorn -c ${PROJECT_DIR}/${GUNICORN_CONF} ${APP_MODULE} --bind 0.0.0.0:$(grep -E '^PROXY_PORT=' .env | cut -d '=' -f2 | tr -d '"')

Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
)
        print_warn "Root privileges are required to install the service."
        echo "$SERVICE_FILE_CONTENT" | sudo tee "$SERVICE_FILE_PATH" > /dev/null
        
        print_info "Reloading systemd daemon..."
        sudo systemctl daemon-reload
        
        print_info "Enabling the service to start on boot..."
        sudo systemctl enable "${SERVICE_NAME}.service"
        
        print_info "Starting the service now..."
        sudo systemctl start "${SERVICE_NAME}.service"
        
        print_header "--- Service Management ---"
        print_success "Service '${SERVICE_NAME}' is now running."
        print_info "Check status: sudo systemctl status ${SERVICE_NAME}"
        print_info "View logs:    sudo journalctl -u ${SERVICE_NAME} -f"
        print_info "Stop service: sudo systemctl stop ${SERVICE_NAME}"
        SERVICE_CREATED=true
    fi
fi

# ====================================================================
# 4. START THE SERVER (if service was not created)
# ====================================================================
if [ "$SERVICE_CREATED" = false ]; then
    print_header "--- Starting Ollama Proxy Fortress (Foreground Mode) ---"
    print_info "Activating virtual environment..."
    source "$VENV_DIR/bin/activate"

    print_info "Setting PYTHONPATH to project root..."
    export PYTHONPATH=.
    PORT_TO_USE=$(grep -E '^PROXY_PORT=' .env | cut -d '=' -f2 | tr -d '"' | tr -d "'" || echo "8080")

    print_info "Starting Gunicorn server on http://0.0.0.0:${PORT_TO_USE}"
    print_info "Press Ctrl+C to stop the server."
    echo
    exec gunicorn -c "$GUNICORN_CONF" "$APP_MODULE" --bind "0.0.0.0:${PORT_TO_USE}"
fi