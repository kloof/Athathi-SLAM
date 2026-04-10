#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Handheld LiDAR Recording Script (CLI-only, not used by Flask app)
# Records /unilidar/cloud + /unilidar/imu to rosbag (mcap)
#
# Usage: sudo bash record_scan.sh [session_name]
# Stop:  Ctrl+C
# ============================================================

SESSION_NAME=${1:-"scan_$(date +%Y%m%d_%H%M%S)"}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECORD_DIR="${SCRIPT_DIR}/recordings/${SESSION_NAME}"
TOPICS="/unilidar/cloud /unilidar/imu"

YELLOW='\033[93m'
GREEN='\033[92m'
RED='\033[91m'
RESET='\033[0m'

DRIVER_PID=""

# --- Cleanup trap (set early to catch Ctrl+C at any point) ---
cleanup() {
    echo ""
    echo -e "${YELLOW}Stopping...${RESET}"

    if [ -d "${RECORD_DIR}/rosbag" ]; then
        BAG_SIZE=$(du -sh "${RECORD_DIR}/rosbag" 2>/dev/null | cut -f1 || echo "unknown")
        echo -e "[INFO] Bag size: ${BAG_SIZE}"
        echo -e "[INFO] Saved to: ${RECORD_DIR}/rosbag"

        # Save metadata
        cat > "${RECORD_DIR}/metadata.txt" << META
Session: ${SESSION_NAME}
Date: $(date -Iseconds)
Bag path: ${RECORD_DIR}/rosbag
Bag size: ${BAG_SIZE}
Topics: ${TOPICS}
Notes:
META
        echo -e "[INFO] Edit ${RECORD_DIR}/metadata.txt to add scan notes"
    fi

    # Stop driver
    if [ -n "$DRIVER_PID" ]; then
        echo -e "${YELLOW}Stopping driver...${RESET}"
        kill $DRIVER_PID 2>/dev/null
        wait $DRIVER_PID 2>/dev/null || true
    fi
    echo -e "${GREEN}Done.${RESET}"
    exit 0
}
trap cleanup SIGINT SIGTERM

if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}Error: Must run as root (sudo bash record_scan.sh)${RESET}"
    exit 1
fi

echo "============================================"
echo "  Handheld LiDAR Recorder"
echo "============================================"

# --- Network setup (idempotent) ---
IFACE="eth0"
HOST_IP="192.168.1.2"
LIDAR_IP="192.168.1.62"

CURRENT_IP=$(ip -4 addr show "$IFACE" 2>/dev/null | grep -oP 'inet \K[\d.]+' || true)
if [[ "$CURRENT_IP" != "$HOST_IP" ]]; then
    echo -e "${YELLOW}Configuring $IFACE with $HOST_IP/24...${RESET}"
    ip addr flush dev "$IFACE" 2>/dev/null || true
    ip addr add "${HOST_IP}/24" dev "$IFACE"
    ip link set "$IFACE" up
    echo -e "${GREEN}Network configured.${RESET}"
else
    echo -e "${GREEN}Network already configured ($HOST_IP on $IFACE).${RESET}"
fi

# --- Ping check ---
if ping -c 1 -W 2 "$LIDAR_IP" &>/dev/null; then
    echo -e "${GREEN}LiDAR reachable at $LIDAR_IP.${RESET}"
else
    echo -e "${YELLOW}Warning: LiDAR not responding at $LIDAR_IP (continuing anyway).${RESET}"
fi

# --- Check disk space (need at least 2GB free) ---
FREE_MB=$(df "${SCRIPT_DIR}" --output=avail -BM | tail -1 | tr -d ' M')
echo -e "[INFO] Free space: ${FREE_MB} MB"
if [ "$FREE_MB" -lt 2000 ]; then
    echo -e "${RED}[WARN] Low disk space! Less than 2 GB free.${RESET}"
fi

# --- Source ROS2 ---
set +u
source /opt/ros/humble/setup.bash
source /home/talal/unilidar_sdk2/unitree_lidar_ros2/install/setup.bash
set -u

export ROS_LOG_DIR="/tmp/ros2_logs"
mkdir -p "$ROS_LOG_DIR"

# --- Launch driver node directly (no rviz) ---
echo -e "${YELLOW}Launching Unitree LiDAR driver...${RESET}"
ros2 run unitree_lidar_ros2 unitree_lidar_ros2_node \
    --ros-args \
    -p initialize_type:=2 \
    -p work_mode:=0 \
    -p use_system_timestamp:=true \
    -p range_min:=0.0 \
    -p range_max:=100.0 \
    -p cloud_scan_num:=18 \
    -p lidar_port:=6101 \
    -p lidar_ip:=192.168.1.62 \
    -p local_port:=6201 \
    -p local_ip:=192.168.1.2 \
    -p cloud_topic:=unilidar/cloud \
    -p imu_topic:=unilidar/imu &
DRIVER_PID=$!

# Wait for topics
echo -e "${YELLOW}Waiting for LiDAR topics...${RESET}"
READY=false
for i in $(seq 1 30); do
    if ros2 topic list 2>/dev/null | grep -q "/unilidar/cloud"; then
        READY=true
        break
    fi
    sleep 1
done

if [ "$READY" = false ]; then
    echo -e "${RED}[ERROR] /unilidar/cloud not found after 30s. Check LiDAR connection.${RESET}"
    kill $DRIVER_PID 2>/dev/null
    exit 1
fi

# --- Verify topics ---
echo -e "${GREEN}[OK] Cloud topic active${RESET}"
if ros2 topic list 2>/dev/null | grep -q "/unilidar/imu"; then
    echo -e "${GREEN}[OK] IMU topic active${RESET}"
else
    echo -e "${YELLOW}[WARN] IMU topic not found — recording cloud only${RESET}"
    TOPICS="/unilidar/cloud"
fi

# --- Start recording ---
mkdir -p "$RECORD_DIR"
echo ""
echo -e "[INFO] Session:  ${SESSION_NAME}"
echo -e "[INFO] Output:   ${RECORD_DIR}"
echo -e "[INFO] Topics:   ${TOPICS}"
echo ""
echo -e ">>> Press ENTER to start recording, then Ctrl+C to stop <<<"
read

echo -e "${GREEN}[RECORDING] Started at $(date)${RESET}"
echo -e "[RECORDING] Walk slowly (0.5-1.0 m/s), make smooth turns"
echo -e "[RECORDING] Return to start position for loop closure!"
echo ""

ros2 bag record \
    -o "${RECORD_DIR}/rosbag" \
    --storage mcap \
    --max-cache-size 200000000 \
    ${TOPICS}

# If ros2 bag exits normally (shouldn't happen unless error)
cleanup
