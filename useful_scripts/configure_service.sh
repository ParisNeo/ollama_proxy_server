#!/bin/bash
set -euo pipefail

# --------------------------------------------------------------------
# Ollama Proxy Server - Standalone Systemd Service Creator
# --------------------------------------------------------------------

COLOR_RESET='\e[0m'; COLOR_INFO='\e[1;34m'; COLOR_SUCCESS='\e[1;32m'
COLOR_ERROR='\e[1;31m'; COLOR_WARN='\e[1;33m'; COLOR_HEADER='\e[1;35m'

print_header()  { echo -e "\n${COLOR_HEADER}$1${COLOR_RESET}"; }
print_info()    { echo -e "${COLOR_INFO}[INFO]${COLOR_RESET} $*"; }
print_success() { echo -e "${COLOR_SUCCESS}[SUCCESS]${COLOR_RESET} $*"; }
print_warn()    { echo -e "${COLOR_WARN}[WARNING]${COLOR_RESET} $*"; }
print_error()   { echo -e "${COLOR_ERROR}[ERROR]${COLOR_RESET} $*" >&2; }

SERVICE_FILE_PATH="/etc/systemd/system/ollama_proxy.service"

# Check for root
if [[ $EUID -ne 0 ]]; then
    print_error "This script must be run with sudo/root privileges."
    exit 1
fi

# Detect project directory
PROJECT_DIR=$(pwd)
VENV_DIR="venv"
GUNICORN_CONF="gunicorn_conf.py"
APP_MODULE="app.main:app"

# Detect port from .env, fallback to 8080
if [[ -f ".env" ]]; then
    PORT_TO_USE=$(grep -E '^PROXY_PORT=' .env | cut -d '=' -f2 | tr -d '"' | tr -d "'" || echo "8080")
else
    PORT_TO_USE=8080
    print_warn ".env file not found, using default port 8080"
fi

print_header "--- Creating Ollama Proxy Server Systemd Service ---"

SERVICE_CONTENT=$(cat << EOF
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

print_info "Writing service file to $SERVICE_FILE_PATH"
echo "$SERVICE_CONTENT" | tee "$SERVICE_FILE_PATH" > /dev/null

print_info "Reloading systemd daemon..."
systemctl daemon-reload

print_info "Enabling service to start at boot..."
systemctl enable ollama_proxy.service

print_info "Starting Ollama Proxy Server service..."
systemctl start ollama_proxy.service

print_success "✅ Ollama Proxy Server systemd service created and running!"
print_info "Check status with: sudo systemctl status ollama_proxy"
print_info "Access the UI on http://<server-ip>:${PORT_TO_USE}"
