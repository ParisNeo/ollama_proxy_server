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
max_parallel_connections = 4
queue_size = 100

# Additional servers can be added here with similar format
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
    echo "Usage: $0 [help | add_user username[:password] | add_server server_name url [max_parallel_connections queue_size] | edit_server server_name parameter value | list_servers]"
    exit 1
}

# Check if exactly one argument is provided and it's 'add_user'
if [ "$#" -lt 1 ]; then
    usage
fi

COMMAND="$1"
shift

case $COMMAND in
    help)
        echo "ops command usage:"
        echo ""
        echo "  ops help                Display this help message"
        echo "  ops add_user username[:password]  Add a user with optional password generation if only username is provided"
        echo "  ops add_server server_name url [max_parallel_connections queue_size]  Add a new server configuration"
        echo "  ops edit_server server_name parameter value  Edit an existing server's parameter"
        echo "  ops list_servers         List all configured servers and their parameters"
        ;;
    add_user)
        USER_PAIR="$1"

        # Extract the user and password from the input
        IFS=':' read -r USER PASSWORD <<< "$USER_PAIR"

        if [ -z "$PASSWORD" ]; then
            # Generate a random password if only username is provided
            PASSWORD=$(tr -dc A-Za-z0-9 </dev/urandom | head -c 12)
            echo "Generated password: $PASSWORD"
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

        echo "User '$USER' added successfully with password '$PASSWORD'."
        ;;
    add_server)
        if [ "$#" -lt 2 ]; then
            usage
        fi

        SERVER_NAME="$1"
        URL="$2"
        MAX_PARALLEL_CONNECTIONS="${3:-4}"
        QUEUE_SIZE="${4:-100}"

        CONFIG_FILE="/etc/ops/config.ini"

        # Add the server to the config file
        sudo bash -c "echo \"[$SERVER_NAME]\" >> $CONFIG_FILE"
        sudo bash -c "echo \"url = $URL\" >> $CONFIG_FILE"
        sudo bash -c "echo \"max_parallel_connections = $MAX_PARALLEL_CONNECTIONS\" >> $CONFIG_FILE"
        sudo bash -c "echo \"queue_size = $QUEUE_SIZE\" >> $CONFIG_FILE"

        echo "Server '$SERVER_NAME' added successfully."
        ;;
    edit_server)
        if [ "$#" -ne 3 ]; then
            usage
        fi

        SERVER_NAME="$1"
        PARAMETER="$2"
        VALUE="$3"
        CONFIG_FILE="/etc/ops/config.ini"

        # Edit the server parameter in the config file
        sudo sed -i "/\[$SERVER_NAME\]/,/^\[.*\]/ s/^\($PARAMETER\s*=\s*\).*\$/\\1$VALUE/" $CONFIG_FILE

        echo "Parameter '$PARAMETER' for server '$SERVER_NAME' updated to '$VALUE'."
        ;;
    list_servers)
        CONFIG_FILE="/etc/ops/config.ini"

        # List all servers and their parameters
        echo "Listing servers and their configuration:"
        sudo grep -E '^\[.*\]' $CONFIG_FILE | while read -r SERVER_SECTION; do
            SERVER_NAME="${SERVER_SECTION#*[}"
            SERVER_NAME="${SERVER_NAME%]"
            echo "$SERVER_NAME:"
            sudo sed -i "/^\[$SERVER_NAME\]/,/^\[.*\]/ s/^\([^[]\+\)/  \\1/" $CONFIG_FILE | grep -A 50 "^\s*$SERVER_NAME" | head -n 3
        done
        ;;
    *)
        usage
        ;;
esac
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
echo "  To display help, run: ops help"
echo "  To add a user, run: ops add_user username[:password]"
echo "  To add a server, run: ops add_server server_name url [max_parallel_connections queue_size]"
echo "  To edit a server setting, run: ops edit_server server_name parameter value"
echo "  To list all servers and their parameters, run: ops list_servers"
