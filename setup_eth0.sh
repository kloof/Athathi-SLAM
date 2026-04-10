#!/usr/bin/env bash
set -euo pipefail

# Configure eth0 for Unitree L2 LiDAR connection (idempotent)
# Usage: sudo bash setup_eth0.sh

YELLOW='\033[93m'
GREEN='\033[92m'
RED='\033[91m'
RESET='\033[0m'

if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}Error: Must run as root (sudo bash setup_eth0.sh)${RESET}"
    exit 1
fi

IFACE="eth0"
HOST_IP="192.168.1.2"
LIDAR_IP="192.168.1.62"

CURRENT_IP=$(ip -4 addr show "$IFACE" 2>/dev/null | grep -oP 'inet \K[\d.]+' || true)
if [[ "$CURRENT_IP" != "$HOST_IP" ]]; then
    echo -e "${YELLOW}Configuring $IFACE with $HOST_IP/24...${RESET}"
    ip addr flush dev "$IFACE" 2>/dev/null || true
    ip addr add "${HOST_IP}/24" dev "$IFACE"
    ip link set "$IFACE" up
    echo -e "${GREEN}Network configured ($HOST_IP on $IFACE).${RESET}"
else
    echo -e "${GREEN}Network already configured ($HOST_IP on $IFACE).${RESET}"
fi

# Ping check
if ping -c 1 -W 2 "$LIDAR_IP" &>/dev/null; then
    echo -e "${GREEN}LiDAR reachable at $LIDAR_IP.${RESET}"
else
    echo -e "${YELLOW}Warning: LiDAR not responding at $LIDAR_IP (check power/cable).${RESET}"
fi
