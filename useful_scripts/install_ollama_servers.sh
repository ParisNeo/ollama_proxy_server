#!/bin/bash

# ---------------------------
# Ollama Multi-Instance Installer / Updater
# ---------------------------

# Colors
GREEN="\e[32m"
YELLOW="\e[33m"
CYAN="\e[36m"
MAGENTA="\e[35m"
RESET="\e[0m"

# Root check
if [[ $EUID -ne 0 ]]; then
   echo -e "${YELLOW}This script must be run as root or with sudo. Exiting.${RESET}" 
   exit 1
fi

echo -e "${CYAN}Welcome to the Ollama multi-instance installer/updater!${RESET}"

OLLAMA_BIN=${OLLAMA_BIN:-/usr/local/bin/ollama}
SERVICE_DIR=${SERVICE_DIR:-/etc/systemd/system}
DEFAULT_PORT=5000

# Ask user where to store models and libraries
read -p "Where do you want to store Ollama models? [/data/ollama/models]: " MODEL_DIR
MODEL_DIR=${MODEL_DIR:-/data/ollama/models}

read -p "Where do you want to install Ollama libraries? [/data/ollama/lib]: " LIB_DIR
LIB_DIR=${LIB_DIR:-/data/ollama/lib}

# Detect existing instances
EXISTING_SERVICES=($(ls $SERVICE_DIR/ollama@*.service 2>/dev/null | xargs -n1 basename 2>/dev/null))
reuse="n"
if [ ${#EXISTING_SERVICES[@]} -gt 0 ]; then
  echo -e "${YELLOW}Detected existing Ollama instances:${RESET}"
  for s in "${EXISTING_SERVICES[@]}"; do
    echo -e " - $s"
  done
  read -p "Do you want to reuse the existing configuration? (y/n): " reuse
fi

# Arrays for instance configuration
GPU_INSTANCES=()
HOSTS=()
CONTEXTS=()
FLASH_FLAGS=()

if [[ "$reuse" =~ ^[Yy] ]]; then
  # Load existing hosts and GPU from systemd environment properly
  for f in "${EXISTING_SERVICES[@]}"; do
    IDX=$(echo $f | grep -oP '(?<=@)\d+(?=\.service)')
    ENV_VARS=$(systemctl show "$f" -p Environment | cut -d'=' -f2-)
    GPU=$(echo "$ENV_VARS" | grep -oP 'CUDA_VISIBLE_DEVICES=\K[^ ]+')
    HOST=$(echo "$ENV_VARS" | grep -oP 'OLLAMA_HOST=\K[^ ]+')
    CONTEXT=$(echo "$ENV_VARS" | grep -oP 'OLLAMA_CONTEXT_LENGTH=\K[^ ]+')
    FLASH=$(echo "$ENV_VARS" | grep -oP 'OLLAMA_FLASH_ATTENTION=\K[^ ]+')

    GPU_INSTANCES[IDX]=$GPU
    HOSTS[IDX]=$HOST
    CONTEXTS[IDX]=$CONTEXT
    FLASH_FLAGS[IDX]=$FLASH
  done
else
  # Ask if they want to remove old instances
  if [ ${#EXISTING_SERVICES[@]} -gt 0 ]; then
      read -p "Do you want to remove the old Ollama instances? (stop, disable, delete service files) (y/n): " remove_old
      if [[ "$remove_old" =~ ^[Yy] ]]; then
          for f in "${EXISTING_SERVICES[@]}"; do
              echo -e "${YELLOW}Stopping and disabling $f...${RESET}"
              systemctl stop "$f" 2>/dev/null
              systemctl disable "$f" 2>/dev/null
              echo -e "${YELLOW}Deleting service file $SERVICE_DIR/$f...${RESET}"
              rm -f "$SERVICE_DIR/$f"
          done
          echo -e "${GREEN}Old Ollama instances removed.${RESET}"
      else
          echo -e "${CYAN}Old instances left in place. Make sure ports and GPUs do not conflict.${RESET}"
      fi
  fi

  # New configuration
  echo -e "${MAGENTA}Current GPUs available:${RESET}"
  nvidia-smi --list-gpus
  read -p "How many Ollama instances do you want to run? " NUM_INSTANCES

  for ((i=0;i<NUM_INSTANCES;i++)); do
    read -p "Enter comma-separated GPU IDs for instance $i (e.g., 0,1): " gpus
    GPU_INSTANCES+=("$gpus")

    read -p "Enter OLLAMA_HOST for instance $i (leave empty to auto-increment port): " host
    if [ -z "$host" ]; then
      host="localhost:$((DEFAULT_PORT + i))"
    fi
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
fi

# Create directories if they don't exist
mkdir -p "$LIB_DIR" "$MODEL_DIR"
chown -R ollama:ollama "$LIB_DIR" "$MODEL_DIR"
chmod -R 770 "$LIB_DIR" "$MODEL_DIR"

# Backup existing Ollama service files
DATE=$(date +%Y-%m-%d_%H-%M-%S)
for f in "$SERVICE_DIR"/ollama@*.service; do
  [ -f "$f" ] && cp "$f" "$f.$DATE.bak"
done

# Stop & disable any old Ollama services before install
for f in "$SERVICE_DIR"/ollama@*.service; do
  systemctl stop "$(basename $f .service)" 2>/dev/null
  systemctl disable "$(basename $f .service)" 2>/dev/null
done

# Move Ollama libraries if needed
if [ -d "/usr/local/lib/ollama" ]; then
  if [ -d "$LIB_DIR/ollama" ]; then
    echo -e "${YELLOW}Libraries already exist in $LIB_DIR${RESET}"
  else
    mv /usr/local/lib/ollama "$LIB_DIR" || echo "Could not move libraries"
  fi
fi

# Move Ollama models if needed
if [ -d "/usr/local/models" ]; then
  if [ -d "$MODEL_DIR" ]; then
    echo -e "${YELLOW}Models already exist in $MODEL_DIR${RESET}"
  else
    mv /usr/local/models "$MODEL_DIR" || echo "Could not move models"
  fi
fi

# Install latest Ollama
echo -e "${CYAN}Installing latest Ollama...${RESET}"
curl -fsSL https://ollama.com/install.sh | sh

# Ensure correct ownership
chown -R ollama:ollama "$LIB_DIR" "$MODEL_DIR"
chmod -R 770 "$LIB_DIR" "$MODEL_DIR"

# Create systemd services for each instance
for i in "${!GPU_INSTANCES[@]}"; do
  INSTANCE_GPU=${GPU_INSTANCES[i]}
  INSTANCE_HOST=${HOSTS[i]}
  FLASH=${FLASH_FLAGS[i]}
  CONTEXT=${CONTEXTS[i]}
  SERVICE_FILE="$SERVICE_DIR/ollama@$i.service"

  ENV_CTX=""
  [ -n "$CONTEXT" ] && ENV_CTX="Environment=\"OLLAMA_CONTEXT_LENGTH=$CONTEXT\""

  tee "$SERVICE_FILE" > /dev/null <<EOL
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
done

# Reload systemd, enable & start all instances
echo -e "\n${CYAN}Starting all Ollama instances...${RESET}"
for i in "${!GPU_INSTANCES[@]}"; do
  systemctl daemon-reload
  systemctl enable "ollama@$i"
  systemctl restart "ollama@$i"
  echo -e "${GREEN}✅ Instance $i started and enabled at boot.${RESET}"
done

# Summary
echo -e "\n${GREEN}🎉 Ollama update & multi-instance setup completed!${RESET}"
echo -e "${CYAN}Summary of your instances:${RESET}"
for i in "${!GPU_INSTANCES[@]}"; do
  echo -e "${MAGENTA}Instance $i${RESET} -> GPUs: ${YELLOW}${GPU_INSTANCES[i]}${RESET}, Host: ${CYAN}${HOSTS[i]}${RESET}, Flash Attention: ${GREEN}${FLASH_FLAGS[i]}${RESET}, Context: ${CONTEXTS[i]:-auto}"
done

echo -e "\n${CYAN}💡 Next Steps:${RESET}"
echo -e "1. Open your LoLLMs Hub UI (e.g., http://localhost:8080)"
echo -e "2. Add each instance to the proxy using the ${YELLOW}OLLAMA_HOST${RESET} values listed above"
echo -e "3. Test the connections and start multi-user requests"
echo -e "4. Use colored labels in the proxy UI to track GPU allocation and instance numbers"

echo -e "\n${GREEN}✅ All done! Your Ollama instances are ready to serve multiple users.${RESET}"
