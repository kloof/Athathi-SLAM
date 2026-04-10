#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Handheld LiDAR + Camera Recording Script (CLI-only)
# Records /unilidar/cloud + /unilidar/imu + camera topics to rosbag (mcap)
# Camera is optional — falls back to LiDAR-only if not connected.
#
# Usage: sudo bash record_scan.sh [session_name]
# Stop:  Ctrl+C
# ============================================================

SESSION_NAME=${1:-"scan_$(date +%Y%m%d_%H%M%S)"}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECORD_DIR="${SCRIPT_DIR}/recordings/${SESSION_NAME}"
CALIB_DIR="${SCRIPT_DIR}/calibration"
TOPICS="/unilidar/cloud /unilidar/imu"

YELLOW='\033[93m'
GREEN='\033[92m'
RED='\033[91m'
RESET='\033[0m'

DRIVER_PID=""
CAMERA_PID=""
TF_PID=""
CAMERA_OK=false

# --- Cleanup trap (set early to catch Ctrl+C at any point) ---
cleanup() {
    set +e  # Don't abort cleanup on errors
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
Camera: ${CAMERA_OK}
Notes:
META
        echo -e "[INFO] Edit ${RECORD_DIR}/metadata.txt to add scan notes"
    fi

    # Stop processes first (before extracting MP4 — MCAP must be finalized)
    [ -n "$TF_PID" ] && kill $TF_PID 2>/dev/null && wait $TF_PID 2>/dev/null || true
    [ -n "$CAMERA_PID" ] && kill $CAMERA_PID 2>/dev/null && wait $CAMERA_PID 2>/dev/null || true
    if [ -n "$DRIVER_PID" ]; then
        echo -e "${YELLOW}Stopping driver...${RESET}"
        kill $DRIVER_PID 2>/dev/null
        wait $DRIVER_PID 2>/dev/null || true
    fi

    # Copy calibration files into session
    if [ -d "${CALIB_DIR}" ]; then
        mkdir -p "${RECORD_DIR}/calibration"
        cp -f "${CALIB_DIR}"/*.yaml "${RECORD_DIR}/calibration/" 2>/dev/null || true
    fi

    # Extract MP4 from MCAP if camera was active
    if [ "$CAMERA_OK" = true ]; then
        echo -e "${YELLOW}Extracting video from MCAP...${RESET}"
        python3 -c "
import os, glob, cv2, numpy as np
from mcap.reader import make_reader
from mcap_ros2.decoder import DecoderFactory
bag_dir = '${RECORD_DIR}/rosbag'
mcap_files = glob.glob(os.path.join(bag_dir, '*.mcap'))
if not mcap_files: exit()
writer = None
with open(mcap_files[0], 'rb') as fh:
    reader = make_reader(fh, decoder_factories=[DecoderFactory()])
    for _, _, _, decoded in reader.iter_decoded_messages(topics=['/camera/image_raw/compressed']):
        frame = cv2.imdecode(np.frombuffer(bytes(decoded.data), np.uint8), cv2.IMREAD_COLOR)
        if frame is None: continue
        if writer is None:
            h, w = frame.shape[:2]
            writer = cv2.VideoWriter('${RECORD_DIR}/video.mp4', cv2.VideoWriter_fourcc(*'mp4v'), 10, (w, h))
        writer.write(frame)
if writer: writer.release(); print('Video extracted')
" 2>&1 || echo -e "${YELLOW}[WARN] MP4 extraction failed${RESET}"
    fi
    echo -e "${GREEN}Done.${RESET}"
    exit 0
}
trap cleanup EXIT

if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}Error: Must run as root (sudo bash record_scan.sh)${RESET}"
    exit 1
fi

echo "============================================"
echo "  Handheld LiDAR + Camera Recorder"
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

# --- Camera check (find Brio dynamically, index 0 = RGB) ---
CAMERA_DEVICE=""
for dev in /sys/class/video4linux/video*; do
    devname=$(basename "$dev")
    cam_name=$(cat "$dev/name" 2>/dev/null || true)
    cam_idx=$(cat "$dev/index" 2>/dev/null || true)
    if [[ "$cam_name" == *"Logitech BRIO"* ]] && [[ "$cam_idx" == "0" ]]; then
        CAMERA_DEVICE="/dev/$devname"
        break
    fi
done

if [ -n "$CAMERA_DEVICE" ]; then
    CAM_NAME=$(cat "/sys/class/video4linux/$(basename $CAMERA_DEVICE)/name" 2>/dev/null || echo "unknown")
    echo -e "${GREEN}Camera detected: $CAM_NAME ($CAMERA_DEVICE)${RESET}"
    CAMERA_OK=true
else
    echo -e "${YELLOW}No camera detected — recording LiDAR only${RESET}"
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

# --- Launch camera (if available) ---
if [ "$CAMERA_OK" = true ]; then
    echo -e "${YELLOW}Launching camera driver...${RESET}"
    CAMERA_INFO_URL=""
    if [ -f "${CALIB_DIR}/intrinsics.yaml" ]; then
        CAMERA_INFO_URL="file://${CALIB_DIR}/intrinsics.yaml"
    fi

    ros2 run v4l2_camera v4l2_camera_node \
        --ros-args \
        -p video_device:=$CAMERA_DEVICE \
        -p "image_size:=[1280,720]" \
        -p pixel_format:=YUYV \
        -p "camera_info_url:=${CAMERA_INFO_URL}" \
        -p auto_exposure:=3 \
        -r __ns:=/camera &
    CAMERA_PID=$!

    # Launch TF static publisher for extrinsics (graceful on parse failure)
    if [ -f "${CALIB_DIR}/extrinsics.yaml" ]; then
        TF_VALS=$(python3 -c "
import yaml
d = yaml.safe_load(open('${CALIB_DIR}/extrinsics.yaml'))
t, r = d['translation'], d['rotation']
print(t['x'], t['y'], t['z'], r['x'], r['y'], r['z'], r['w'])
" 2>/dev/null) && read -r TX TY TZ QX QY QZ QW <<< "$TF_VALS" && \
        ros2 run tf2_ros static_transform_publisher \
            --x "$TX" --y "$TY" --z "$TZ" \
            --qx "$QX" --qy "$QY" --qz "$QZ" --qw "$QW" \
            --frame-id unilidar_lidar --child-frame-id camera_optical_frame &
        TF_PID=$!
        if [ -z "$TF_PID" ]; then
            echo -e "${YELLOW}[WARN] Could not parse extrinsics.yaml — skipping TF${RESET}"
        fi
    fi

    TOPICS="/unilidar/cloud /unilidar/imu /camera/image_raw/compressed /camera/camera_info /tf_static"
fi

# Wait for LiDAR topics
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
    [ -n "$CAMERA_PID" ] && kill $CAMERA_PID 2>/dev/null
    exit 1
fi

# --- Verify topics ---
echo -e "${GREEN}[OK] Cloud topic active${RESET}"
IMU_OK=true
if ros2 topic list 2>/dev/null | grep -q "/unilidar/imu"; then
    echo -e "${GREEN}[OK] IMU topic active${RESET}"
else
    echo -e "${YELLOW}[WARN] IMU topic not found — recording cloud only${RESET}"
    IMU_OK=false
fi

if [ "$CAMERA_OK" = true ]; then
    # Wait up to 10s for camera topic
    CAM_READY=false
    for i in $(seq 1 10); do
        if ros2 topic list 2>/dev/null | grep -q "/camera/image_raw"; then
            CAM_READY=true
            break
        fi
        sleep 1
    done
    if [ "$CAM_READY" = true ]; then
        echo -e "${GREEN}[OK] Camera topic active${RESET}"
    else
        echo -e "${YELLOW}[WARN] Camera topic not found — recording without camera${RESET}"
        CAMERA_OK=false
    fi
fi

# Rebuild topic list based on verified topics
TOPICS="/unilidar/cloud"
if [ "$IMU_OK" = true ]; then
    TOPICS="$TOPICS /unilidar/imu"
fi
if [ "$CAMERA_OK" = true ]; then
    TOPICS="$TOPICS /camera/image_raw/compressed /camera/camera_info /tf_static"
fi

# --- Start recording ---
mkdir -p "$RECORD_DIR"
echo ""
echo -e "[INFO] Session:  ${SESSION_NAME}"
echo -e "[INFO] Output:   ${RECORD_DIR}"
echo -e "[INFO] Topics:   ${TOPICS}"
echo -e "[INFO] Camera:   ${CAMERA_OK}"
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

# If ros2 bag exits normally, the EXIT trap will handle cleanup
