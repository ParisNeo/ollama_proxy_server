#!/bin/bash
set -euo pipefail

# ====================================================================
#
#   Ollama Proxy Fortress - Alembic-Free Installer & Runner
#
# ====================================================================

VENV_DIR="venv"
REQUIREMENTS_FILE="requirements.txt"
GUNICORN_CONF="gunicorn_conf.py"
APP_MODULE="app.main:app"
STATE_FILE=".setup_state"

# Allow override with $PYTHON_BIN env var
PYTHON_BIN="${PYTHON_BIN:-python3}"

COLOR_RESET='\e[0m'; COLOR_INFO='\e[1;34m'; COLOR_SUCCESS='\e[1;32m'
COLOR_ERROR='\e[1;31m'; COLOR_WARN='\e[1;33m'; COLOR_HEADER='\e[1;35m'

print_header()  { echo -e "\n${COLOR_HEADER}=====================================================${COLOR_RESET}"; \
                  echo -e "${COLOR_HEADER}$1${COLOR_RESET}"; \
                  echo -e "${COLOR_HEADER}=====================================================${COLOR_RESET}"; }
print_info()    { echo -e "${COLOR_INFO}[INFO]${COLOR_RESET} $*"; }
print_success() { echo -e "${COLOR_SUCCESS}[SUCCESS]${COLOR_RESET} $*"; }
print_error()   { echo -e "${COLOR_ERROR}[ERROR]${COLOR_RESET} $*" >&2; }
print_warn()    { echo -e "${COLOR_WARN}[WARNING]${COLOR_RESET} $*"; }

clear
print_header "    Ollama Proxy Fortress Installer & Runner"

print_info "Performing initial system checks..."
if ! command -v "$PYTHON_BIN" &>/dev/null || ! "$PYTHON_BIN" -m pip --version &>/dev/null || ! "$PYTHON_BIN" -m venv -h &>/dev/null; then
    print_error "Python ($PYTHON_BIN), pip, or venv is missing."
    exit 1
fi
print_success "Python ($PYTHON_BIN), pip, and venv are available."

CURRENT_STATE=0
if [[ -f "$STATE_FILE" ]]; then CURRENT_STATE=$(cat "$STATE_FILE"); fi

if [[ "$CURRENT_STATE" -ge 3 ]] && [[ ! -f ".env" ]]; then
    print_warn "Setup complete, but '.env' file is missing!"
    read -p "Run setup wizard again? (y/n): " REBUILD_CHOICE
    if [[ "$REBUILD_CHOICE" =~ ^[Yy]$ ]]; then
        print_info "Resetting setup state..."
        rm -f "$STATE_FILE"
        CURRENT_STATE=0
    else
        print_info "Aborting."
        exit 0
    fi
fi

if [[ "$CURRENT_STATE" -lt 3 ]]; then
    print_info "Setup state is ${CURRENT_STATE}/3. Starting or resuming installation..."

    if [[ "$CURRENT_STATE" -lt 1 ]]; then
        print_header "--- [Step 1/3] Creating Python Virtual Environment ---"
        "$PYTHON_BIN" -m venv "$VENV_DIR"
        echo "1" > "$STATE_FILE"
        print_success "Virtual environment created."
    fi
    source "$VENV_DIR/bin/activate"
    if [[ "$CURRENT_STATE" -lt 2 ]]; then
        print_header "--- [Step 2/3] Installing Python Dependencies ---"
        pip install --no-cache-dir -r "$REQUIREMENTS_FILE"
        echo "2" > "$STATE_FILE"
        print_success "All dependencies installed."
    fi
    if [[ "$CURRENT_STATE" -lt 3 ]]; then
        print_header "--- [Step 3/3] Server Configuration ---"
        "$PYTHON_BIN" setup_wizard.py
        if [ $? -ne 0 ]; then
            print_error "Setup wizard failed. Aborting."
            exit 1
        fi
        echo "3" > "$STATE_FILE"
        print_success ".env file created."
    fi

    print_header "--- Setup Complete! ---"
    print_success "The database will be created automatically on first run."
fi

SERVICE_CREATED=false
if [[ "$(uname)" == "Linux" ]] && command -v systemctl &>/dev/null && [[ ! -f "/etc/systemd/system/ollama_proxy.service" ]]; then
    print_header "--- Optional: Create a Systemd Service ---"
    read -p "Create and enable a systemd service to run on boot? (y/n): " CREATE_SERVICE
    if [[ "$CREATE_SERVICE" =~ ^[Yy]$ ]]; then
        SERVICE_FILE_PATH="/etc/systemd/system/ollama_proxy.service"
        print_info "Creating systemd service file..."
        PROJECT_DIR=$(pwd)
        PORT_TO_USE=$(grep -E '^PROXY_PORT=' .env | cut -d '=' -f2 | tr -d '"' || echo "8080")
        SERVICE_FILE_CONTENT=$(cat << EOF
[Unit]
Description=Ollama Proxy Fortress Service
After=network.target
[Service]
User=${USER}
Group=$(id -gn ${USER})
WorkingDirectory=${PROJECT_DIR}
Environment="PYTHONPATH=${PROJECT_DIR}"
ExecStart=${PROJECT_DIR}/${VENV_DIR}/bin/gunicorn -c ${PROJECT_DIR}/${GUNICORN_CONF} ${APP_MODULE} --bind 0.0.0.0:${PORT_TO_USE}
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
)
        print_warn "Root privileges are required to install the service."
        echo "$SERVICE_FILE_CONTENT" | sudo tee "$SERVICE_FILE_PATH" > /dev/null
        sudo systemctl daemon-reload
        sudo systemctl enable "ollama_proxy.service"
        sudo systemctl start "ollama_proxy.service"
        print_header "--- Service Management ---"
        print_success "Service 'ollama_proxy' is now running."
        print_info "Check status: sudo systemctl status ollama_proxy"
        SERVICE_CREATED=true
    fi
fi

if [ "$SERVICE_CREATED" = false ]; then
    print_header "--- Starting Ollama Proxy Fortress (Foreground Mode) ---"
    source "$VENV_DIR/bin/activate"
    export PYTHONPATH=.
    PORT_TO_USE=$(grep -E '^PROXY_PORT=' .env | cut -d '=' -f2 | tr -d '"' | tr -d "'" || echo "8080")
    print_info "Starting Gunicorn server on http://0.0.0.0:${PORT_TO_USE}"
    print_info "Press Ctrl+C to stop the server."
    echo
    exec gunicorn -c "$GUNICORN_CONF" "$APP_MODULE" --bind "0.0.0.0:${PORT_TO_USE}"
fi
