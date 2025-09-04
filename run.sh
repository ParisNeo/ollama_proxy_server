#!/bin/bash
set -e

# This script sets up and runs the Ollama Proxy Server on macOS and Linux.
# On first run, it will guide you through setup.
# Afterwards, it will just start the server.

VENV_DIR="venv"

# --- Helper Functions ---
function print_info() {
    echo -e "\e[34m[INFO]\e[0m $1"
}

function print_success() {
    echo -e "\e[32m[SUCCESS]\e[0m $1"
}

function print_error() {
    echo -e "\e[31m[ERROR]\e[0m $1" >&2
}

# --- Check for Python ---
print_info "Checking for Python 3 installation..."
if ! command -v python3 &> /dev/null; then
    print_error "Python 3 not found. Please install it to continue."
    exit 1
fi
print_success "Python 3 found."

# --- First-Time Setup ---
if [ ! -d "$VENV_DIR" ]; then
    print_info "First-time setup detected. Configuring the server..."

    # 1. Create Virtual Environment
    print_info "Creating Python virtual environment..."
    python3 -m venv "$VENV_DIR"

    # 2. Activate and Install Dependencies
    print_info "Activating environment and installing dependencies (this may take a moment)..."
    source "$VENV_DIR/bin/activate"
    pip install --no-cache-dir -r requirements.txt

    # 3. Gather Configuration
    print_info "Please provide the following configuration:"
    read -p "Enter the port for the proxy server to listen on [8080]: " PROXY_PORT
    PROXY_PORT=${PROXY_PORT:-8080}

    read -p "Enter the backend Ollama server URL(s), comma-separated [http://127.0.0.1:11434]: " OLLAMA_SERVERS
    OLLAMA_SERVERS=${OLLAMA_SERVERS:-"http://127.0.0.1:11434"}

    read -p "Enter the Redis URL for rate limiting [redis://localhost:6379/0]: " REDIS_URL
    REDIS_URL=${REDIS_URL:-"redis://localhost:6379/0"}

    read -p "Enter a username for the admin dashboard [admin]: " ADMIN_USER
    ADMIN_USER=${ADMIN_USER:-"admin"}

    read -s -p "Enter a password for the admin user (will be hidden): " ADMIN_PASSWORD
    echo
    if [ -z "$ADMIN_PASSWORD" ]; then
        print_error "Admin password cannot be empty."
        exit 1
    fi

    read -p "Enter allowed IPs, comma-separated (leave empty for all): " ALLOWED_IPS
    ALLOWED_IPS=${ALLOWED_IPS:-""}

    read -p "Enter denied IPs, comma-separated (leave empty for none): " DENIED_IPS
    DENIED_IPS=${DENIED_IPS:-""}


    # 4. Generate .env file
    print_info "Generating .env configuration file..."
    SECRET_KEY=$(openssl rand -hex 32)
    cat > .env << EOF
# Application Settings
APP_NAME="Ollama Proxy Server"
APP_VERSION="8.0.0"
LOG_LEVEL="info"
PROXY_PORT=${PROXY_PORT}
OLLAMA_SERVERS="${OLLAMA_SERVERS}"
DATABASE_URL="sqlite+aiosqlite:///./ollama_proxy.db"
ADMIN_USER=${ADMIN_USER}
ADMIN_PASSWORD="${ADMIN_PASSWORD}"
SECRET_KEY=${SECRET_KEY}

# --- Advanced Security ---
REDIS_URL="${REDIS_URL}"
RATE_LIMIT_REQUESTS=100
RATE_LIMIT_WINDOW_MINUTES=1
ALLOWED_IPS="${ALLOWED_IPS}"
DENIED_IPS="${DENIED_IPS}"
EOF

    # 5. Initialize Database
    print_info "Initializing the database..."
    alembic upgrade head
    
    print_success "First-time setup complete!"
    echo
fi

# --- Start the Server ---
print_info "Activating virtual environment..."
source "$VENV_DIR/bin/activate"

print_info "Setting Python Path..."
export PYTHONPATH=.

# Read port from .env, with a fallback
PORT_TO_USE=8080
if [ -f .env ] && grep -q "PROXY_PORT" .env; then
    # Correctly parse the value, removing potential quotes
    PORT_TO_USE=$(grep "PROXY_PORT" .env | cut -d '=' -f2 | tr -d '"' | tr -d "'")
fi

print_info "Starting Ollama Proxy Server on port ${PORT_TO_USE}..."
print_info "Press Ctrl+C to stop the server."

# Use Gunicorn to run the server, ensuring the port is passed correctly
gunicorn -c ./gunicorn_conf.py app.main:app --bind "0.0.0.0:${PORT_TO_USE}"