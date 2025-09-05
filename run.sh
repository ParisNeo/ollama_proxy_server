#!/bin/bash
set -euo pipefail   # fail on error, undefined vars, and pipe failures

# ------------------------------------------------------------
# Ollama Proxy Server installer & runner (macOS / Linux)
# ------------------------------------------------------------

VENV_DIR="venv"
REQUIREMENTS_FILE="requirements.txt"
GUNICORN_CONF="gunicorn_conf.py"
APP_MODULE="app.main:app"

# ------------------------------------------------------------
# Helper Functions
# ------------------------------------------------------------
print_info()    { echo -e "\e[34m[INFO]\e[0m $*"; }
print_success() { echo -e "\e[32m[SUCCESS]\e[0m $*"; }
print_error()   { echo -e "\e[31m[ERROR]\e[0m $*" >&2; }

# ------------------------------------------------------------
# 1️⃣ Check for Python 3
# ------------------------------------------------------------
print_info "Checking for Python 3 installation..."
if ! command -v python3 &>/dev/null; then
    print_error "Python 3 not found. Please install it to continue."
    exit 1
fi
print_success "Python 3 is available."

# ------------------------------------------------------------
# 2️⃣ First‑time setup (create venv, install deps, generate .env)
# ------------------------------------------------------------
if [[ ! -d "$VENV_DIR" ]]; then
    print_info "First‑time setup detected – configuring the server..."

    # ---- 2.1 Create virtual environment
    print_info "Creating Python virtual environment in ./$VENV_DIR ..."
    python3 -m venv "$VENV_DIR"

    # ---- 2.2 Activate and install dependencies
    print_info "Activating environment and installing dependencies..."
    source "$VENV_DIR/bin/activate"
    if [[ -f "$REQUIREMENTS_FILE" ]]; then
        pip install --no-cache-dir -r "$REQUIREMENTS_FILE"
    else
        print_error "Missing $REQUIREMENTS_FILE – aborting."
        exit 1
    fi

    # ---- 2.3 Gather configuration from the user
    print_info "Please provide the following configuration (press Enter for defaults):"

    read -p "Port for the proxy server to listen on [8080]: " PROXY_PORT
    PROXY_PORT=${PROXY_PORT:-8080}

    read -p "Backend Ollama server URL(s), comma‑separated [http://127.0.0.1:11434]: " OLLAMA_SERVERS
    OLLAMA_SERVERS=${OLLAMA_SERVERS:-http://127.0.0.1:11434}

    read -p "Redis URL for rate limiting [redis://localhost:6379/0]: " REDIS_URL
    REDIS_URL=${REDIS_URL:-redis://localhost:6379/0}

    read -p "Username for the admin dashboard [admin]: " ADMIN_USER
    ADMIN_USER=${ADMIN_USER:-admin}

    # hide password input
    read -s -p "Password for the admin user (will be hidden): " ADMIN_PASSWORD
    echo
    if [[ -z "$ADMIN_PASSWORD" ]]; then
        print_error "Admin password cannot be empty."
        exit 1
    fi

    read -p "Allowed IPs, comma‑separated (leave empty for all): " ALLOWED_IPS
    read -p "Denied IPs, comma‑separated (leave empty for none): " DENIED_IPS

    # ---- 2.4 Generate .env file
    print_info "Generating .env configuration file..."

    # Create a random secret key (32‑byte hex)
    SECRET_KEY=$(openssl rand -hex 32)

    # Escape any double quotes that might be present in the password
    ESCAPED_ADMIN_PASSWORD=${ADMIN_PASSWORD//\"/\\\"}

    {
        echo "# --------------------------------------------------"
        echo "# Application Settings"
        echo "# --------------------------------------------------"
        echo "APP_NAME=\"Ollama Proxy Server\""
        echo "APP_VERSION=\"8.0.0\""
        echo "LOG_LEVEL=\"info\""
        echo "PROXY_PORT=${PROXY_PORT}"
        echo "OLLAMA_SERVERS=\"${OLLAMA_SERVERS}\""
        echo "DATABASE_URL=\"sqlite+aiosqlite:///./ollama_proxy.db\""
        echo "ADMIN_USER=${ADMIN_USER}"
        echo "ADMIN_PASSWORD=\"${ESCAPED_ADMIN_PASSWORD}\""
        echo "SECRET_KEY=${SECRET_KEY}"
        echo ""
        echo "# --------------------------------------------------"
        echo "# Advanced Security"
        echo "# --------------------------------------------------"
        echo "REDIS_URL=\"${REDIS_URL}\""
        echo "RATE_LIMIT_REQUESTS=100"
        echo "RATE_LIMIT_WINDOW_MINUTES=1"
        # Only write the list‑type variables when they are non‑empty.
        if [[ -n "$ALLOWED_IPS" ]]; then
            echo "ALLOWED_IPS=${ALLOWED_IPS}"
        fi
        if [[ -n "$DENIED_IPS" ]]; then
            echo "DENIED_IPS=${DENIED_IPS}"
        fi
    } > .env

    print_success ".env file created."

    # ---- 2.5 Initialise the database with Alembic
    print_info "Running database migrations (Alembic)..."
    alembic upgrade head
    print_success "Database is up‑to‑date."

    print_success "First‑time setup complete!"
    echo
fi

# ------------------------------------------------------------
# 3️⃣ Start the server
# ------------------------------------------------------------
print_info "Activating virtual environment..."
source "$VENV_DIR/bin/activate"

print_info "Setting PYTHONPATH to project root..."
export PYTHONPATH=.

# Determine the port from .env (fallback to 8080)
DEFAULT_PORT=8080
if [[ -f .env && $(grep -E '^PROXY_PORT=' .env) ]]; then
    # Strip quotes if present
    PORT_TO_USE=$(grep -E '^PROXY_PORT=' .env | cut -d '=' -f2 | tr -d '"' | tr -d "'")
else
    PORT_TO_USE=$DEFAULT_PORT
fi

print_info "Starting Ollama Proxy Server on port ${PORT_TO_USE}..."
print_info "Press Ctrl+C to stop the server."

# Run via Gunicorn – the config file is expected to exist in the repo.
exec gunicorn -c "$GUNICORN_CONF" "$APP_MODULE" --bind "0.0.0.0:${PORT_TO_USE}"
