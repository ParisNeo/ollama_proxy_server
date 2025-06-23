#!/bin/bash

# Configuration with parameters
SERVICE_NAME="ollama-proxy-server"
USER="ops"

if [ "$#" -ne 1 ]; then
    echo "Usage: $0 <working_directory>"
    exit 1
fi

WORKING_DIR=$1
LOG_DIR="$WORKING_DIR/logs"
SCRIPT_PATH="$WORKING_DIR/ollama-proxy-server/main.py"

echo "Setting up Ollama Proxy Server..."

# Create dedicated user if it doesn't exist already
if ! id "$USER" &>/dev/null; then
    echo "Creating user $USER..."
    sudo useradd -r -s /bin/false "$USER"
fi

# Ensure the working directory is writable by the dedicated user
sudo mkdir -p "$WORKING_DIR"
sudo cp -r * "$WORKING_DIR/"
sudo chown -R "$USER:$USER" "$WORKING_DIR"

# Set permissions for logs and reports directories
echo "Setting up directories and files..."
sudo mkdir -p "$LOG_DIR"
sudo mkdir -p "$WORKING_DIR/reports"
sudo chown -R "$USER:$USER" "$LOG_DIR"

# Create systemd service file
echo "Creating systemd service..."
sudo tee /etc/systemd/system/$SERVICE_NAME.service > /dev/null << EOF
[Unit]
Description=Ollama Proxy Server
After=network.target
Wants=network.target

[Service]
Type=simple
User=$USER
Group=$USER
WorkingDirectory=$WORKING_DIR
ExecStart=/bin/bash $WORKING_DIR/run.sh
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# Environment
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=PYTHONUNBUFFERED=1

# Security settings
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=$WORKING_DIR $LOG_DIR

[Install]
WantedBy=multi-user.target
EOF

# Install Python dependencies with proper permissions and environment variables preserved
echo "Installing Python dependencies..."
sudo -u "$USER" python3 -m venv $WORKING_DIR/venv
sudo chown -R "$USER:$USER" $WORKING_DIR/venv

# Activate the virtual environment and install dependencies as user without --user flag
echo "Activating virtualenv and installing Python packages..."
sudo -H -u "$USER" bash << EOF
source $WORKING_DIR/venv/bin/activate && pip install --no-cache-dir $WORKING_DIR
EOF

# Create logrotate config
echo "Setting up log rotation..."
sudo tee /etc/logrotate.d/$SERVICE_NAME > /dev/null << EOF
$LOG_DIR/*.log {
    daily
    rotate 15
    compress
    delaycompress
    missingok
    notifempty
    create 644 $USER $USER
    postrotate
        systemctl reload-or-restart $SERVICE_NAME
    endscript
}
EOF

# Reload systemd and enable service
echo "Enabling service..."
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

echo "Service setup complete!"
echo ""
echo "Commands:"
echo "  Start:   sudo systemctl start $SERVICE_NAME"
echo "  Stop:    sudo systemctl stop $SERVICE_NAME"
echo "  Status:  sudo journalctl -u $SERVICE_NAME -f"
echo "  Logs:    sudo journalctl -u $SERVICE_NAME -f"
echo "  Reports: ls $WORKING_DIR/reports/"
