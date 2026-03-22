#!/bin/bash
set -euo pipefail

COLOR_RESET='\e[0m'
COLOR_INFO='\e[1;34m'
COLOR_SUCCESS='\e[1;32m'
COLOR_ERROR='\e[1;31m'
COLOR_WARN='\e[1;33m'
COLOR_HEADER='\e[1;35m'

print_header(){ echo -e "\n${COLOR_HEADER}==============================${COLOR_RESET}";
echo -e "${COLOR_HEADER}$1${COLOR_RESET}";
echo -e "${COLOR_HEADER}==============================${COLOR_RESET}"; }

print_info(){ echo -e "${COLOR_INFO}[INFO]${COLOR_RESET} $*"; }
print_success(){ echo -e "${COLOR_SUCCESS}[OK]${COLOR_RESET} $*"; }
print_warn(){ echo -e "${COLOR_WARN}[WARN]${COLOR_RESET} $*"; }
print_error(){ echo -e "${COLOR_ERROR}[ERROR]${COLOR_RESET} $*"; }

if [[ $EUID -ne 0 ]]; then
    print_error "Run this script with sudo."
    exit 1
fi

OLLAMA_INSTANCES=$(systemctl list-unit-files | grep "ollama@.*service" | awk '{print $1}')
PROXY_SERVICE="lollms_hub.service"

start_servers() {

    print_header "Starting Ollama Instances"

    for svc in $OLLAMA_INSTANCES; do
        print_info "Starting $svc"
        systemctl start "$svc"
    done

    if systemctl list-unit-files | grep -q "$PROXY_SERVICE"; then
        print_info "Starting LoLLMs Hub"
        systemctl start "$PROXY_SERVICE"
    fi

    print_success "Servers started."
}

stop_servers() {

    print_header "Stopping Ollama Instances"

    if systemctl list-unit-files | grep -q "$PROXY_SERVICE"; then
        print_info "Stopping LoLLMs Hub"
        systemctl stop "$PROXY_SERVICE"
    fi

    for svc in $OLLAMA_INSTANCES; do
        print_info "Stopping $svc"
        systemctl stop "$svc"
    done

    print_success "Servers stopped."
}

restart_servers() {

    print_header "Restarting Ollama Servers"

    if systemctl list-unit-files | grep -q "$PROXY_SERVICE"; then
        print_info "Restarting LoLLMs Hub"
        systemctl restart "$PROXY_SERVICE"
    fi

    for svc in $OLLAMA_INSTANCES; do
        print_info "Restarting $svc"
        systemctl restart "$svc"
    done

    print_success "Servers restarted."
}

status_servers() {

    print_header "Server Status"

    for svc in $OLLAMA_INSTANCES; do
        systemctl status "$svc" --no-pager
        echo
    done

    if systemctl list-unit-files | grep -q "$PROXY_SERVICE"; then
        systemctl status "$PROXY_SERVICE" --no-pager
    fi
}

menu() {

    clear
    print_header "Ollama Server Manager"

    echo "1) Start servers"
    echo "2) Stop servers"
    echo "3) Restart servers"
    echo "4) Status"
    echo "5) Exit"
    echo

    read -p "Select an option: " choice

    case $choice in
        1) start_servers ;;
        2) stop_servers ;;
        3) restart_servers ;;
        4) status_servers ;;
        5) exit 0 ;;
        *) print_warn "Invalid option" ;;
    esac
}

while true; do
    menu
    read -p "Press Enter to continue..."
done
