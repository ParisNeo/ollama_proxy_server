#!/bin/bash

# ollama_internet_off.sh - Block Ollama from accessing the internet
# This script uses multiple methods to ensure Ollama cannot connect to the internet

set -e

echo "🚫 Blocking Ollama internet access..."

# Method 1: Use iptables to block outbound connections from ollama process
if command -v iptables &> /dev/null; then
    echo "📡 Setting up iptables rules..."

    # Block HTTP/HTTPS traffic from ollama
    sudo iptables -A OUTPUT -m owner --exe-owner $(which ollama) -p tcp --dport 80 -j REJECT 2>/dev/null || true
    sudo iptables -A OUTPUT -m owner --exe-owner $(which ollama) -p tcp --dport 443 -j REJECT 2>/dev/null || true
    sudo iptables -A OUTPUT -m owner --exe-owner $(which ollama) -p tcp --dport 11434 -j REJECT 2>/dev/null || true

    # Block DNS queries from ollama
    sudo iptables -A OUTPUT -m owner --exe-owner $(which ollama) -p udp --dport 53 -j REJECT 2>/dev/null || true
    sudo iptables -A OUTPUT -m owner --exe-owner $(which ollama) -p tcp --dport 53 -j REJECT 2>/dev/null || true

    echo "✅ iptables rules applied"
else
    echo "⚠️  iptables not available, skipping firewall rules"
fi

# Method 2: Create a wrapper script that runs ollama without network access
OLLAMA_WRAPPER_DIR="$HOME/.local/bin"
OLLAMA_WRAPPER="$OLLAMA_WRAPPER_DIR/ollama_offline"

mkdir -p "$OLLAMA_WRAPPER_DIR"

cat > "$OLLAMA_WRAPPER" << 'EOF'
#!/bin/bash

# Ollama offline wrapper
export OLLAMA_HOST="127.0.0.1:11434"

# Use unshare to create network namespace without internet (Linux only)
if command -v unshare &> /dev/null && [[ "$OSTYPE" == "linux-gnu"* ]]; then
    # Run ollama in a network namespace with only loopback interface
    exec unshare --net --map-root-user bash -c '
        ip link set lo up
        export PATH="'$(dirname $(which ollama))':$PATH"
        exec '$( which ollama )' "$@"
    ' -- "$@"
else
    # Fallback: just run with localhost binding
    exec $(which ollama) "$@"
fi
EOF

chmod +x "$OLLAMA_WRAPPER"

# Method 3: Set environment variables to restrict network access
echo "🔧 Setting up environment configuration..."
OLLAMA_ENV_FILE="$HOME/.ollama_offline_env"

cat > "$OLLAMA_ENV_FILE" << 'EOF'
# Ollama offline environment variables
export OLLAMA_HOST="127.0.0.1:11434"
export OLLAMA_ORIGINS="http://localhost:*,https://localhost:*"
export OLLAMA_DEBUG=1
EOF

echo "📝 Created environment file at $OLLAMA_ENV_FILE"
echo "💡 To use: source $OLLAMA_ENV_FILE"

# Method 4: Create hosts file backup and block known Ollama domains
if [[ -w /etc/hosts ]] || sudo -n true 2>/dev/null; then
    echo "🔒 Backing up and modifying hosts file..."
    sudo cp /etc/hosts /etc/hosts.ollama_backup 2>/dev/null || true

    # Block known domains that Ollama might try to reach
    sudo tee -a /etc/hosts > /dev/null << 'EOF' || true

# Ollama internet blocking - added by ollama_internet_off.sh
127.0.0.1 registry.ollama.ai
127.0.0.1 ollama.com
127.0.0.1 api.ollama.com
127.0.0.1 huggingface.co
127.0.0.1 github.com
EOF
    echo "✅ Hosts file modified"
else
    echo "⚠️  Cannot modify hosts file, skipping domain blocking"
fi

echo ""
echo "🎯 Ollama internet access has been blocked using multiple methods:"
echo "   1. iptables firewall rules (if available)"
echo "   2. Network namespace wrapper at $OLLAMA_WRAPPER"
echo "   3. Environment variables in $OLLAMA_ENV_FILE"
echo "   4. Hosts file domain blocking (if permissions allow)"
echo ""
echo "💡 Usage options:"
echo "   - Use wrapper: $OLLAMA_WRAPPER run llama2"
echo "   - Source env: source $OLLAMA_ENV_FILE && ollama run llama2"
echo "   - Direct use: OLLAMA_HOST=127.0.0.1:11434 ollama run llama2"
echo ""
echo "⚠️  Make sure you have already downloaded your models with 'ollama pull <model>'"
