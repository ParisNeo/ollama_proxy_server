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
CONFIG_FILE="/etc/ops/config.ini"
AUTHORIZED_USERS_FILE="/etc/ops/authorized_users.txt"

# Default port and log path; these can be customized by the user
DEFAULT_PORT=11534
DEFAULT_LOG_PATH="$LOG_DIR/server.log"

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

read -p "Enter the port number (default: $DEFAULT_PORT): " PORT
PORT=${PORT:-$DEFAULT_PORT}

read -p "Enter the log path (default: $DEFAULT_LOG_PATH): " LOG_PATH
LOG_PATH=${LOG_PATH:-$DEFAULT_LOG_PATH}

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
ExecStart=/bin/bash $WORKING_DIR/run.sh --log_path $LOG_PATH --port $PORT --config $CONFIG_FILE --users_list $AUTHORIZED_USERS_FILE
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

# Create and populate config.ini and authorized_users.txt files
echo "Creating configuration files..."
sudo mkdir -p /etc/ops
sudo tee $CONFIG_FILE > /dev/null << EOF
[DefaultServer]
url = http://localhost:11434
EOF
sudo chown $USER:$USER $CONFIG_FILE

echo "Adding authorized users to the list. Type 'done' when finished."
while true; do
    read -p "Enter user:password or type 'done': " input
    if [ "$input" == "done" ]; then
        break
    fi
    echo "$input" | sudo tee -a $AUTHORIZED_USERS_FILE > /dev/null
    sudo chown $USER:$USER $AUTHORIZED_USERS_FILE
done

echo "You can add more users to the authorized_users.txt file if needed."

# Create ops command script
echo "Creating 'ops' command..."
sudo tee /usr/local/bin/ops > /dev/null << 'EOF'
#!/bin/bash

# Define usage function to display help message
usage() {
    echo "Usage: $0 add_user username:password"
    exit 1
}

# Check if exactly one argument is provided and it's 'add_user'
if [ "$#" -ne 2 ] || [ "$1" != "add_user" ]; then
    usage
fi

USER_PAIR="$2"

# Extract the user and password from the input
IFS=':' read -r USER PASSWORD <<< "$USER_PAIR"
if [ -z "$USER" ] || [ -z "$PASSWORD" ]; then
    echo "Invalid username:password format."
    usage
fi

AUTHORIZED_USERS_FILE="/etc/ops/authorized_users.txt"

# Check if the authorized_users file exists, create it otherwise
sudo mkdir -p /etc/ops
if [ ! -f "$AUTHORIZED_USERS_FILE" ]; then
    sudo touch $AUTHORIZED_USERS_FILE
fi

# Append the new user:password pair to the file
echo "$USER:$PASSWORD" | sudo tee -a $AUTHORIZED_USERS_FILE > /dev/null

# Ensure correct permissions for the file
sudo chown ops:ops $AUTHORIZED_USERS_FILE

echo "User '$USER' added successfully."
EOF

# Make ops command executable
sudo chmod +x /usr/local/bin/ops

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

echo ""
echo "How to use the new 'ops' command:"
echo "  To add a user, run: ops add_user username:password"
