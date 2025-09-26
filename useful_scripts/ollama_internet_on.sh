#!/bin/bash

# ollama_internet_on.sh - Restore Ollama internet access
# This script removes the restrictions applied by ollama_internet_off.sh

set -e

echo "🌐 Restoring Ollama internet access..."

# Method 1: Remove iptables rules
if command -v iptables &> /dev/null; then
    echo "📡 Removing iptables rules..."

    # Remove the specific rules we added (note: this removes ALL matching rules)
    sudo iptables -D OUTPUT -m owner --exe-owner $(which ollama) -p tcp --dport 80 -j REJECT 2>/dev/null || true
    sudo iptables -D OUTPUT -m owner --exe-owner $(which ollama) -p tcp --dport 443 -j REJECT 2>/dev/null || true
    sudo iptables -D OUTPUT -m owner --exe-owner $(which ollama) -p tcp --dport 11434 -j REJECT 2>/dev/null || true
    sudo iptables -D OUTPUT -m owner --exe-owner $(which ollama) -p udp --dport 53 -j REJECT 2>/dev/null || true
    sudo iptables -D OUTPUT -m owner --exe-owner $(which ollama) -p tcp --dport 53 -j REJECT 2>/dev/null || true

    echo "✅ iptables rules removed"
else
    echo "⚠️  iptables not available"
fi

# Method 2: Remove wrapper script
OLLAMA_WRAPPER="$HOME/.local/bin/ollama_offline"
if [[ -f "$OLLAMA_WRAPPER" ]]; then
    rm "$OLLAMA_WRAPPER"
    echo "✅ Removed offline wrapper script"
fi

# Method 3: Remove environment file
OLLAMA_ENV_FILE="$HOME/.ollama_offline_env"
if [[ -f "$OLLAMA_ENV_FILE" ]]; then
    rm "$OLLAMA_ENV_FILE"
    echo "✅ Removed offline environment file"
fi

# Method 4: Restore hosts file
if [[ -f /etc/hosts.ollama_backup ]]; then
    echo "🔒 Restoring hosts file..."
    sudo cp /etc/hosts.ollama_backup /etc/hosts 2>/dev/null || true
    sudo rm /etc/hosts.ollama_backup 2>/dev/null || true
    echo "✅ Hosts file restored"
else
    # Manually remove the lines if backup doesn't exist
    if [[ -w /etc/hosts ]] || sudo -n true 2>/dev/null; then
        sudo sed -i '/# Ollama internet blocking/,$d' /etc/hosts 2>/dev/null || true
        echo "✅ Removed Ollama blocking entries from hosts file"
    fi
fi

# Method 5: Reset environment variables to defaults
echo "🔧 Resetting environment variables..."
unset OLLAMA_HOST
unset OLLAMA_ORIGINS
unset OLLAMA_DEBUG

echo ""
echo "🎯 Ollama internet access has been restored!"
echo "💡 You can now use Ollama normally:"
echo "   ollama run llama2"
echo "   ollama pull mistral"
echo ""
echo "🔄 You may want to restart any running Ollama processes:"
echo "   pkill ollama"
echo "   ollama serve"
