#!/bin/bash

# ---------------------------
# Ollama Generic Updater & Multi-Instance Installer
# ---------------------------

OLLAMA_DIR="/data/ollama"
LIB_DIR="$OLLAMA_DIR/lib"
MODEL_DIR="$OLLAMA_DIR/models"
SERVICE_DIR="/etc/systemd/system"
OLLAMA_BIN="/usr/local/bin/ollama"

echo "Welcome to the Ollama multi-instance installer!"
echo "Current GPUs available:"
nvidia-smi --list-gpus

# Ask user how many instances
read -p "How many Ollama instances do you want to run? " NUM_INSTANCES

# Arrays to hold user input
GPU_INSTANCES=()
HOSTS=()
CONTEXTS=()
FLASH_FLAGS=()

for ((i=0;i<NUM_INSTANCES;i++)); do
  read -p "Enter comma-separated GPU IDs for instance $i (e.g., 0,1): " gpus
  GPU_INSTANCES+=("$gpus")
  read -p "Enter OLLAMA_HOST for instance $i (e.g., localhost:5000): " host
  HOSTS+=("$host")

  read -p "Do you want to specify context length for instance $i? Leave empty to let Ollama manage: " ctx
  CONTEXTS+=("$ctx")

  read -p "Enable Flash Attention for instance $i? Recommended for large models (y/n): " flash
  if [[ "$flash" =~ ^[Yy] ]]; then
    FLASH_FLAGS+=("1")
  else
    FLASH_FLAGS+=("0")
  fi
done

# Create directories if they don't exist
sudo mkdir -p "$LIB_DIR" "$MODEL_DIR"
sudo chown -R ollama:ollama "$OLLAMA_DIR"
sudo chmod -R 770 "$OLLAMA_DIR"

# Backup existing Ollama service files
DATE=$(date +%Y-%m-%d_%H-%M-%S)
for f in "$SERVICE_DIR"/ollama@*.service; do
  [ -f "$f" ] && sudo cp "$f" "$f.$DATE.bak"
done

# Stop existing Ollama services
sudo systemctl stop ollama@*
sudo systemctl disable ollama@*

# Move Ollama libraries/models if needed
if [ -d "/usr/local/lib/ollama" ]; then
  sudo mv /usr/local/lib/ollama "$LIB_DIR" || echo "Libraries already in $LIB_DIR"
fi
if [ -d "/usr/local/models" ]; then
  sudo mv /usr/local/models "$MODEL_DIR" || echo "Models already in $MODEL_DIR"
fi

# Install latest Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Ensure correct ownership
sudo chown -R ollama:ollama "$LIB_DIR" "$MODEL_DIR"
sudo chmod -R 770 "$LIB_DIR" "$MODEL_DIR"

# Create systemd services for each instance
for i in "${!GPU_INSTANCES[@]}"; do
  INSTANCE_GPU=${GPU_INSTANCES[i]}
  INSTANCE_HOST=${HOSTS[i]}
  FLASH=${FLASH_FLAGS[i]}
  CONTEXT=${CONTEXTS[i]}
  SERVICE_FILE="$SERVICE_DIR/ollama@$i.service"

  ENV_CTX=""
  [ -n "$CONTEXT" ] && ENV_CTX="Environment=\"OLLAMA_CONTEXT_LENGTH=$CONTEXT\""

  sudo tee "$SERVICE_FILE" > /dev/null <<EOL
[Unit]
Description=Ollama LLM Service Instance $i
After=network.target

[Service]
User=ollama
Group=ollama
Environment="OLLAMA_MODELS=$MODEL_DIR"
Environment="OLLAMA_PARALLEL=1"
Environment="OLLAMA_FLASH_ATTENTION=$FLASH"
$ENV_CTX
Environment="CUDA_VISIBLE_DEVICES=$INSTANCE_GPU"
Environment="OLLAMA_HOST=$INSTANCE_HOST"
ExecStart=$OLLAMA_BIN serve
Restart=always

[Install]
WantedBy=multi-user.target
EOL

  # Enable and start the service
  sudo systemctl daemon-reload
  sudo systemctl enable ollama@$i
  sudo systemctl start ollama@$i
done

echo "Ollama update & multi-instance setup completed."
for i in "${!GPU_INSTANCES[@]}"; do
  echo "- Instance $i on GPUs ${GPU_INSTANCES[i]} listening at ${HOSTS[i]}, Flash Attention=${FLASH_FLAGS[i]}, Context=${CONTEXTS[i]:-auto}"
done
echo "Ready to be used with a proxy server for multi-user access."
