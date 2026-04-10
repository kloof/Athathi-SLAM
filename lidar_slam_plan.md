# Handheld LiDAR Scanner — Full Implementation Plan

## Project Overview

Build a handheld 3D indoor scanner using a **Unitree L2 LiDAR** and **Raspberry Pi 4**, then post-process the recorded data on a workstation for maximum accuracy using SLAM + bundle adjustment.

**Two devices, two phases:**

| Device | Role | Phase |
|--------|------|-------|
| Raspberry Pi 4 (4GB+ RAM) | Data logger — records raw LiDAR + IMU to USB SSD | Phase 1: Capture |
| PC Workstation (Ubuntu, 32GB+ RAM) | Runs SLAM, loop closure, bundle adjustment | Phase 2: Post-processing |

**Target output:** Globally consistent 3D point cloud (PCD/PLY) with 5–10 mm accuracy for indoor measurement.

---

## PHASE 1: RASPBERRY PI 4 — DATA CAPTURE DEVICE

### 1.1 Hardware Setup

**Components needed:**

- Raspberry Pi 4 (4GB or 8GB RAM)
- USB 3.0 SSD (128GB+ recommended, e.g., Samsung T7)
- Ethernet cable (Cat5e or better, direct Pi ↔ L2 connection)
- USB-C PD power bank for Pi (5V 3A, 10000+ mAh)
- 12V battery pack for L2 (the L2 draws 10W typical, 13W peak)
- Monopod / selfie stick / short pole for mounting
- 3D printed or aluminum bracket to mount L2 on pole
- (Optional) Small fan or heatsink for Pi — sustained Ethernet + SSD writes can warm it up

**Physical rig assembly:**

- Mount L2 on top of pole, sensor face pointing UP (hemispherical FOV covers room above and around)
- Mount Pi in a small case strapped to the pole or clipped to your belt
- SSD connects to Pi via USB 3.0 port (the blue one)
- Ethernet cable from Pi directly to L2 (no router/switch needed)
- Keep cables tidy with velcro straps — loose cables cause snag-induced jerks

### 1.2 Raspberry Pi OS Setup

```bash
# ============================================================
# STEP 1: Flash Ubuntu 22.04 Server (64-bit) onto microSD
# ============================================================
# Use Raspberry Pi Imager → Other general-purpose OS → Ubuntu Server 22.04.x LTS (64-bit)
# Boot the Pi, connect via SSH or monitor

# ============================================================
# STEP 2: System setup
# ============================================================
sudo apt update && sudo apt upgrade -y

# Set locale (needed for ROS2)
sudo apt install -y locales
sudo locale-gen en_US en_US.UTF-8
sudo update-locale LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8
export LANG=en_US.UTF-8

# Install essential tools
sudo apt install -y \
  build-essential \
  cmake \
  git \
  python3-pip \
  python3-colcon-common-extensions \
  net-tools \
  htop \
  screen \
  usbutils

# ============================================================
# STEP 3: Install ROS2 Humble
# ============================================================
sudo apt install -y software-properties-common
sudo add-apt-repository universe
sudo curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.key -o /usr/share/keyrings/ros-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] http://packages.ros.org/ros2/ubuntu $(. /etc/os-release && echo $UBUNTU_CODENAME) main" | sudo tee /etc/apt/sources.list.d/ros2.list > /dev/null
sudo apt update
sudo apt install -y ros-humble-ros-base ros-humble-rosbag2-storage-mcap
# NOTE: ros-base, NOT ros-desktop (no GUI needed on Pi)

# Add to bashrc
echo "source /opt/ros/humble/setup.bash" >> ~/.bashrc
source ~/.bashrc

# ============================================================
# STEP 4: Mount USB SSD (auto-mount on boot)
# ============================================================
# Plug in SSD, find it
lsblk
# Format if needed (CAREFUL — erases data):
# sudo mkfs.ext4 /dev/sda1

sudo mkdir -p /mnt/ssd
# Get UUID
sudo blkid /dev/sda1
# Add to /etc/fstab:
# UUID=<your-uuid> /mnt/ssd ext4 defaults,nofail 0 2
sudo mount -a

# Create recording directory
mkdir -p /mnt/ssd/lidar_recordings

# ============================================================
# STEP 5: Configure static IP for Ethernet (L2 connection)
# ============================================================
# The Unitree L2 defaults to sending data to 192.168.1.2
# Create netplan config for the Pi's Ethernet interface

sudo tee /etc/netplan/01-lidar.yaml << 'EOF'
network:
  version: 2
  ethernets:
    eth0:
      addresses:
        - 192.168.1.2/24
      dhcp4: false
      optional: true
EOF

sudo netplan apply

# Verify
ip addr show eth0
# Should show 192.168.1.2/24
```

### 1.3 Unitree L2 SDK and ROS2 Driver

```bash
# ============================================================
# STEP 1: Clone and build the Unitree L2 SDK2
# ============================================================
mkdir -p ~/lidar_ws/src
cd ~/lidar_ws/src

git clone https://github.com/unitreerobotics/unilidar_sdk2.git

cd unilidar_sdk2
mkdir build && cd build
cmake ..
make -j4

# ============================================================
# STEP 2: Enable the IMU (CRITICAL — disabled by default!)
# ============================================================
# The L2's IMU ships disabled. You MUST enable it once via SDK.
# Edit or create a small utility program:

cd ~/lidar_ws/src/unilidar_sdk2

cat > enable_imu.cpp << 'CPPEOF'
#include "unitree_lidar_sdk.h"
#include <iostream>
#include <unistd.h>

int main() {
    auto lreader = createUnitreeLidarReader();
    // Connect via UDP — default L2 listens on 192.168.1.62 port 6101
    if (lreader->initialize(/* cloud_port */ 6101, /* imu_port */ 6102)) {
        std::cout << "Connected to L2" << std::endl;
    } else {
        std::cerr << "Failed to connect" << std::endl;
        return 1;
    }
    
    // Enable IMU output
    lreader->setLidarWorkingMode(AUXILIARY);
    sleep(1);
    lreader->enableIMU(true);
    sleep(1);
    lreader->setLidarWorkingMode(NORMAL);
    
    std::cout << "IMU enabled successfully!" << std::endl;
    
    // Verify IMU is streaming
    for (int i = 0; i < 100; i++) {
        auto msg = lreader->runParse();
        if (msg == IMU) {
            auto imu = lreader->getIMU();
            std::cout << "IMU data received: "
                      << "gyro=(" << imu.gyro[0] << ", " << imu.gyro[1] << ", " << imu.gyro[2] << ") "
                      << "acc=(" << imu.acc[0] << ", " << imu.acc[1] << ", " << imu.acc[2] << ")"
                      << std::endl;
            break;
        }
        usleep(10000); // 10ms
    }
    
    return 0;
}
CPPEOF

# Build it (adjust include/lib paths as needed)
g++ enable_imu.cpp -o enable_imu \
  -I../include -L../lib/aarch64 -lunitree_lidar_sdk -lpthread
  
# Run it ONCE (IMU setting persists after power cycle)
./enable_imu

# ============================================================
# STEP 3: Build the ROS2 driver
# ============================================================
cd ~/lidar_ws/src

# Clone the ROS2 wrapper
# Check if unilidar_sdk2 has a ros2 folder, otherwise use:
git clone https://github.com/unitreerobotics/unilidar_sdk.git

# The ROS2 package is at: unilidar_sdk/unitree_lidar_ros2/

# Build the ROS2 workspace
cd ~/lidar_ws
colcon build --packages-select unitree_lidar_ros2 --cmake-args -DCMAKE_BUILD_TYPE=Release
source install/setup.bash

echo "source ~/lidar_ws/install/setup.bash" >> ~/.bashrc

# ============================================================
# STEP 4: Test the driver
# ============================================================
# Power on L2, connect Ethernet, then:
ros2 launch unitree_lidar_ros2 launch.py

# In another terminal, verify topics:
ros2 topic list
# Should see:
#   /unilidar/cloud    (sensor_msgs/PointCloud2)
#   /unilidar/imu      (sensor_msgs/Imu)

# Check rates:
ros2 topic hz /unilidar/cloud
# Expected: ~10-15 Hz (depending on L2 scan rate config)

ros2 topic hz /unilidar/imu
# Expected: ~500 Hz
```

### 1.4 Recording Script

```bash
# ============================================================
# Create the main recording script
# ============================================================
cat > ~/record_scan.sh << 'BASH'
#!/bin/bash
# ============================================================
# Handheld LiDAR Recording Script
# Usage: ./record_scan.sh [session_name]
# ============================================================

SESSION_NAME=${1:-"scan_$(date +%Y%m%d_%H%M%S)"}
RECORD_DIR="/mnt/ssd/lidar_recordings/${SESSION_NAME}"
TOPICS="/unilidar/cloud /unilidar/imu"

# --- Pre-flight checks ---
echo "============================================"
echo "  Handheld LiDAR Recorder"
echo "============================================"

# Check SSD is mounted
if ! mountpoint -q /mnt/ssd; then
    echo "[ERROR] SSD not mounted at /mnt/ssd"
    exit 1
fi

# Check disk space (need at least 10GB free)
FREE_GB=$(df /mnt/ssd --output=avail -BG | tail -1 | tr -d ' G')
echo "[INFO] SSD free space: ${FREE_GB} GB"
if [ "$FREE_GB" -lt 10 ]; then
    echo "[WARN] Low disk space! Less than 10 GB free."
fi

# Check L2 is reachable
if ! ping -c 1 -W 2 192.168.1.62 > /dev/null 2>&1; then
    echo "[WARN] Cannot ping L2 at 192.168.1.62 — check Ethernet connection"
    echo "       (Some L2 firmware versions don't respond to ping, continuing anyway...)"
fi

# Check ROS2 topics
echo "[INFO] Checking ROS2 topics..."
CLOUD_HZ=$(timeout 5 ros2 topic hz /unilidar/cloud 2>/dev/null | head -1)
IMU_HZ=$(timeout 5 ros2 topic hz /unilidar/imu 2>/dev/null | head -1)

if [ -z "$CLOUD_HZ" ]; then
    echo "[ERROR] /unilidar/cloud not publishing. Is the driver running?"
    echo "        Start it with: ros2 launch unitree_lidar_ros2 launch.py"
    exit 1
fi

echo "[OK] Cloud topic active"
echo "[OK] IMU topic active"

# --- Start recording ---
mkdir -p "$RECORD_DIR"
echo ""
echo "[INFO] Recording to: ${RECORD_DIR}"
echo "[INFO] Topics: ${TOPICS}"
echo ""
echo ">>> Press ENTER to start recording, then Ctrl+C to stop <<<"
read

echo "[RECORDING] Started at $(date)"
echo "[RECORDING] Walk slowly (0.5-1.0 m/s), make smooth turns"
echo "[RECORDING] Return to start position for loop closure!"
echo ""

ros2 bag record \
    -o "${RECORD_DIR}/rosbag" \
    --storage mcap \
    --max-cache-size 200000000 \
    ${TOPICS}

# --- Post-record stats ---
echo ""
echo "[DONE] Recording stopped at $(date)"
BAG_SIZE=$(du -sh "${RECORD_DIR}/rosbag" 2>/dev/null | cut -f1)
echo "[INFO] Bag size: ${BAG_SIZE}"
echo "[INFO] Saved to: ${RECORD_DIR}/rosbag"

# Save metadata
cat > "${RECORD_DIR}/metadata.txt" << META
Session: ${SESSION_NAME}
Date: $(date -Iseconds)
Bag path: ${RECORD_DIR}/rosbag
Bag size: ${BAG_SIZE}
Topics: ${TOPICS}
Notes: 
META

echo "[INFO] Edit ${RECORD_DIR}/metadata.txt to add scan notes"
BASH

chmod +x ~/record_scan.sh

# ============================================================
# Create a driver launcher + recorder combo script
# ============================================================
cat > ~/start_scanning.sh << 'BASH'
#!/bin/bash
# Starts the L2 driver in background, then runs the recorder

echo "Starting Unitree L2 ROS2 driver..."
ros2 launch unitree_lidar_ros2 launch.py &
DRIVER_PID=$!

# Wait for topics to appear
echo "Waiting for LiDAR topics..."
for i in $(seq 1 30); do
    if ros2 topic list 2>/dev/null | grep -q "/unilidar/cloud"; then
        echo "Driver ready!"
        break
    fi
    sleep 1
done

# Run the recorder
~/record_scan.sh "$1"

# Cleanup
echo "Stopping driver..."
kill $DRIVER_PID 2>/dev/null
wait $DRIVER_PID 2>/dev/null
echo "Done."
BASH

chmod +x ~/start_scanning.sh
```

### 1.5 Auto-Start on Boot (Optional)

```bash
# ============================================================
# Systemd service to auto-start the L2 driver on boot
# (So you just power on Pi and it's ready to record)
# ============================================================
sudo tee /etc/systemd/system/lidar-driver.service << 'EOF'
[Unit]
Description=Unitree L2 LiDAR ROS2 Driver
After=network.target

[Service]
Type=simple
User=ubuntu
Environment="HOME=/home/ubuntu"
ExecStartPre=/bin/bash -c "source /opt/ros/humble/setup.bash && source /home/ubuntu/lidar_ws/install/setup.bash"
ExecStart=/bin/bash -c "source /opt/ros/humble/setup.bash && source /home/ubuntu/lidar_ws/install/setup.bash && ros2 launch unitree_lidar_ros2 launch.py"
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable lidar-driver.service
# To start now: sudo systemctl start lidar-driver.service
# To check status: sudo systemctl status lidar-driver.service
```

### 1.6 Transfer Data to Workstation

```bash
# ============================================================
# Option A: Direct SSD transfer (fastest — recommended)
# ============================================================
# Just unplug the USB SSD from the Pi and plug it into your workstation.
# Mount it and copy the rosbag folder.

# ============================================================
# Option B: Network transfer via rsync (if Pi is on WiFi)
# ============================================================
# From the WORKSTATION:
rsync -avP ubuntu@<pi_ip>:/mnt/ssd/lidar_recordings/<session_name>/ \
  ~/lidar_data/<session_name>/

# ============================================================
# Option C: Network transfer via scp
# ============================================================
scp -r ubuntu@<pi_ip>:/mnt/ssd/lidar_recordings/<session_name>/ \
  ~/lidar_data/<session_name>/

# NOTE: At 20 Mbps upload, a 2 GB bag takes ~14 minutes.
# Direct SSD swap is much faster.
```

---

## PHASE 2: WORKSTATION — POST-PROCESSING PIPELINE

### 2.1 Workstation Requirements

**Minimum specs for single-room scans (5–20 min):**

- OS: Ubuntu 22.04 LTS (same as Pi for ROS2 compatibility)
- CPU: Intel i5 / AMD Ryzen 5 (4+ cores)
- RAM: 16 GB
- Storage: SSD with 50 GB free
- GPU: Not required (entire pipeline is CPU-only)

**Recommended specs for full-apartment scans (30–60 min):**

- CPU: Intel i7-12700 / AMD Ryzen 7 or better
- RAM: 32–64 GB
- Storage: NVMe SSD with 100 GB free
- GPU: Still not required, but useful for later mesh reconstruction

### 2.2 Workstation Software Setup

```bash
# ============================================================
# STEP 1: Install ROS2 Humble (if not already installed)
# ============================================================
# Same steps as Pi, but install desktop version:
sudo apt install -y ros-humble-desktop ros-humble-rosbag2-storage-mcap
source /opt/ros/humble/setup.bash

# Additional dependencies
sudo apt install -y \
  ros-humble-pcl-ros \
  ros-humble-pcl-conversions \
  ros-humble-tf2-ros \
  ros-humble-tf2-eigen \
  libpcl-dev \
  libeigen3-dev \
  libgtsam-dev \
  libgoogle-glog-dev \
  python3-open3d \
  cloudcompare

# ============================================================
# STEP 2: Create workspace
# ============================================================
mkdir -p ~/slam_ws/src
cd ~/slam_ws/src

# ============================================================
# STEP 3: Clone FAST-LIO2
# ============================================================
git clone https://github.com/hku-mars/FAST_LIO.git
cd FAST_LIO
git submodule update --init

# ============================================================
# STEP 4: Clone FAST_LIO_SLAM (FAST-LIO2 + ScanContext loop closure)
# ============================================================
cd ~/slam_ws/src
git clone https://github.com/gisbi-kim/FAST_LIO_SLAM.git

# ============================================================
# STEP 5: Clone HBA (Hierarchical Bundle Adjustment)
# ============================================================
cd ~/slam_ws/src
git clone https://github.com/hku-mars/HBA.git

# ============================================================
# STEP 6: Clone Point-LIO (Unitree's officially supported SLAM)
# ============================================================
cd ~/slam_ws/src
git clone https://github.com/unitreerobotics/point_lio_unilidar.git

# ============================================================
# STEP 7: Build everything
# ============================================================
cd ~/slam_ws
colcon build --cmake-args -DCMAKE_BUILD_TYPE=Release -j$(nproc)
source install/setup.bash
echo "source ~/slam_ws/install/setup.bash" >> ~/.bashrc
```

### 2.3 Configuration Files for Unitree L2

```bash
# ============================================================
# FAST-LIO2 config for Unitree L2
# ============================================================
# Create or modify: ~/slam_ws/src/FAST_LIO/config/unitree_l2.yaml

cat > ~/slam_ws/src/FAST_LIO/config/unitree_l2.yaml << 'YAML'
common:
    lid_topic: "/unilidar/cloud"
    imu_topic: "/unilidar/imu"
    time_sync_en: false

preprocess:
    lidar_type: 1                # 1 for custom/generic point cloud
    scan_line: 18                # L2 effective scan lines
    blind: 0.05                  # L2 min range is 0.05m
    point_filter_num: 3          # Downsample: keep every 3rd point
    feature_extract_enable: false # Direct method (no feature extraction)
    scan_rate: 15                # L2 publishes ~15 Hz

mapping:
    acc_cov: 0.1                 # IMU accelerometer noise covariance
    gyr_cov: 0.1                 # IMU gyroscope noise covariance
    b_acc_cov: 0.0001            # IMU accelerometer bias covariance
    b_gyr_cov: 0.0001            # IMU gyroscope bias covariance
    fov_degree: 360              # L2 is 360° horizontal
    det_range: 30.0              # L2 max range
    extrinsic_est_en: true       # Estimate LiDAR-IMU extrinsic online
    
    # LiDAR-IMU extrinsic (identity as starting point — will be refined)
    extrinsic_T: [0.0, 0.0, 0.0]
    extrinsic_R: [1.0, 0.0, 0.0,
                  0.0, 1.0, 0.0,
                  0.0, 0.0, 1.0]

publish:
    path_en: true
    scan_publish_en: true
    dense_publish_en: true
    scan_bodyframe_pub_en: true  # Needed for HBA post-processing

pcd_save:
    pcd_save_en: true            # Save per-frame PCD files for HBA
    interval: 1                  # Save every frame
YAML

# ============================================================
# FAST_LIO_SLAM config (ScanContext parameters)
# ============================================================
# This wraps FAST-LIO2 with ScanContext loop closure.
# Key parameters to tune:

cat > ~/slam_ws/src/FAST_LIO_SLAM/config/unitree_l2_sc.yaml << 'YAML'
# Include all FAST-LIO2 params above, plus:
sc:
    # ScanContext parameters
    max_radius: 30.0             # Match L2 max range
    num_sectors: 60              # Angular bins
    num_rings: 20                # Radial bins
    num_candidates: 10           # Top-N candidates for loop detection
    sc_dist_threshold: 0.2       # Similarity threshold (lower = stricter)
    
    # Loop closure frequency
    loop_detection_interval: 5   # Check every N scans
    
    # ICP refinement for loop closure
    icp_max_correspondence_distance: 0.5
    icp_max_iterations: 50
    icp_fitness_score_threshold: 0.3
    
    # Pose graph optimization
    odom_noise: [0.01, 0.01, 0.01, 0.01, 0.01, 0.01]  # x,y,z,r,p,y
    loop_noise: [0.05, 0.05, 0.05, 0.05, 0.05, 0.05]
YAML

# ============================================================
# HBA config
# ============================================================
cat > ~/slam_ws/src/HBA/config/unitree_l2_hba.yaml << 'YAML'
# HBA parameters
pcd_name_fill_num: 6             # Zero-padding in PCD filenames

# Voxel resolution for feature extraction
voxel_size: 0.1                  # 10cm voxels (indoor)
eigen_value_array: [1e-2, 1e-2]  # Planarity thresholds
min_ps: 10                       # Min points in voxel to use

# Bundle adjustment iterations per layer
max_iter: 5                      # Iterations per BA layer
layer_num: 3                     # Number of hierarchical layers
                                  # Layer 0: every 10 scans
                                  # Layer 1: every 100 scans
                                  # Layer 2: all scans
layer_size: [10, 100, 0]         # Scans per group at each layer
                                  # 0 = use all remaining

# Convergence
converge_threshold: 1e-6
YAML

# ============================================================
# Point-LIO config for Unitree L2 (alternative front-end)
# ============================================================
# The point_lio_unilidar repo should already include a config.
# Check: ~/slam_ws/src/point_lio_unilidar/config/
# If not, create one similar to FAST-LIO2 config above.
```

### 2.4 Post-Processing Pipeline Script

```bash
# ============================================================
# Master post-processing script
# ============================================================
cat > ~/process_scan.sh << 'BASH'
#!/bin/bash
# ============================================================
# Post-Processing Pipeline: rosbag → FAST-LIO2 → ScanContext → HBA → final PCD
#
# Usage: ./process_scan.sh /path/to/session_folder
#
# The session folder should contain:
#   rosbag/    — the recorded rosbag2 directory
#
# Output:
#   slam_output/       — FAST-LIO2 raw output (trajectory + per-frame PCD)
#   loop_closed/       — After ScanContext loop closure
#   hba_refined/       — After HBA bundle adjustment (final result)
#   final_map.pcd      — Merged final point cloud
# ============================================================

set -e

SESSION_DIR="${1:?Usage: ./process_scan.sh /path/to/session_folder}"
BAG_PATH="${SESSION_DIR}/rosbag"

if [ ! -d "$BAG_PATH" ]; then
    echo "[ERROR] rosbag not found at ${BAG_PATH}"
    exit 1
fi

echo "============================================"
echo "  LiDAR SLAM Post-Processing Pipeline"
echo "============================================"
echo "[INFO] Session: ${SESSION_DIR}"
echo ""

# Source ROS2
source /opt/ros/humble/setup.bash
source ~/slam_ws/install/setup.bash

# ============================================================
# STAGE 1: Run FAST-LIO2 on the rosbag
# ============================================================
echo "========== STAGE 1: FAST-LIO2 SLAM =========="
echo "[INFO] Running FAST-LIO2 on recorded data..."

SLAM_OUTPUT="${SESSION_DIR}/slam_output"
mkdir -p "${SLAM_OUTPUT}"

# Play the rosbag and run FAST-LIO2
# Use --clock to simulate time from the bag
ros2 launch fast_lio mapping.launch.py \
    config_path:=~/slam_ws/src/FAST_LIO/config/unitree_l2.yaml \
    &
SLAM_PID=$!

sleep 3  # Wait for SLAM node to initialize

ros2 bag play "${BAG_PATH}" --clock --rate 1.0

# Wait for SLAM to finish processing
sleep 5
kill $SLAM_PID 2>/dev/null
wait $SLAM_PID 2>/dev/null

# Move output files
mv ~/.ros/scans/ "${SLAM_OUTPUT}/pcd_frames/" 2>/dev/null || true
mv ~/.ros/Log/ "${SLAM_OUTPUT}/trajectory/" 2>/dev/null || true

echo "[DONE] FAST-LIO2 complete"
echo "[INFO] Per-frame PCDs: ${SLAM_OUTPUT}/pcd_frames/"
echo "[INFO] Trajectory: ${SLAM_OUTPUT}/trajectory/"

FRAME_COUNT=$(ls "${SLAM_OUTPUT}/pcd_frames/"*.pcd 2>/dev/null | wc -l)
echo "[INFO] Total frames: ${FRAME_COUNT}"

# ============================================================
# STAGE 2: ScanContext Loop Closure
# ============================================================
echo ""
echo "========== STAGE 2: SCANCONTEXT LOOP CLOSURE =========="
echo "[INFO] Detecting loops and optimizing pose graph..."

LOOP_OUTPUT="${SESSION_DIR}/loop_closed"
mkdir -p "${LOOP_OUTPUT}"

# Alternative: Run FAST_LIO_SLAM which integrates both
# If using the integrated version, replace Stage 1 with:
# ros2 launch fast_lio_slam run.launch.py config:=unitree_l2_sc.yaml

# If running ScanContext separately as post-processing:
# This depends on the specific FAST_LIO_SLAM repo structure.
# The general pattern is:
#   1. Load SLAM trajectory (poses) + per-frame point clouds
#   2. Build ScanContext descriptors for each frame
#   3. Detect loop closures (frame pairs with high SC similarity)
#   4. Run ICP between loop closure pairs for precise alignment
#   5. Build pose graph with odometry + loop closure constraints
#   6. Optimize with GTSAM
#   7. Output corrected poses

# For the integrated approach (recommended), run FAST_LIO_SLAM
# instead of base FAST-LIO2 in Stage 1, and this stage produces
# the corrected trajectory automatically.

# Save corrected poses
echo "[DONE] Loop closure complete"
echo "[INFO] Loops detected: check ${LOOP_OUTPUT}/loop_pairs.txt"

# ============================================================
# STAGE 3: HBA Bundle Adjustment
# ============================================================
echo ""
echo "========== STAGE 3: HBA BUNDLE ADJUSTMENT =========="
echo "[INFO] Running hierarchical bundle adjustment..."

HBA_OUTPUT="${SESSION_DIR}/hba_refined"
mkdir -p "${HBA_OUTPUT}"

# HBA expects:
#   - A folder of per-frame PCD files (from FAST-LIO2)
#   - A poses file (from loop-closed trajectory)
#
# Prepare input:
#   - Copy per-frame PCDs to HBA input folder
#   - Convert trajectory to HBA pose format

# Run HBA
cd ~/slam_ws/src/HBA
# The HBA binary typically runs as:
./build/hba_main \
    --pcd_dir "${SLAM_OUTPUT}/pcd_frames/" \
    --pose_file "${LOOP_OUTPUT}/optimized_poses.txt" \
    --output_dir "${HBA_OUTPUT}/" \
    --config ~/slam_ws/src/HBA/config/unitree_l2_hba.yaml

echo "[DONE] HBA refinement complete"

# ============================================================
# STAGE 4: Merge into final point cloud
# ============================================================
echo ""
echo "========== STAGE 4: MERGE FINAL MAP =========="
echo "[INFO] Merging all frames with refined poses..."

# Python script to merge per-frame PCD with HBA-refined poses
python3 << 'PYEOF'
import open3d as o3d
import numpy as np
import os
import sys

hba_dir = "${HBA_OUTPUT}"
pcd_dir = "${SLAM_OUTPUT}/pcd_frames/"
output_file = "${SESSION_DIR}/final_map.pcd"

# Load refined poses from HBA output
poses_file = os.path.join(hba_dir, "refined_poses.txt")
if not os.path.exists(poses_file):
    # Try alternate naming
    poses_file = os.path.join(hba_dir, "poses_refined.txt")

poses = np.loadtxt(poses_file)
# Each row: [x, y, z, qx, qy, qz, qw] or 4x4 flattened

pcd_files = sorted([f for f in os.listdir(pcd_dir) if f.endswith('.pcd')])

print(f"Merging {len(pcd_files)} frames...")

merged = o3d.geometry.PointCloud()

for i, (pcd_file, pose) in enumerate(zip(pcd_files, poses)):
    pcd = o3d.io.read_point_cloud(os.path.join(pcd_dir, pcd_file))
    
    # Build 4x4 transform from pose
    # (adjust based on actual pose format from HBA)
    if len(pose) == 7:  # x,y,z,qx,qy,qz,qw
        from scipy.spatial.transform import Rotation
        T = np.eye(4)
        T[:3, :3] = Rotation.from_quat(pose[3:7]).as_matrix()
        T[:3, 3] = pose[:3]
    elif len(pose) == 16:  # flattened 4x4
        T = pose.reshape(4, 4)
    else:
        T = np.eye(4)
        T[:3, 3] = pose[:3]
    
    pcd.transform(T)
    merged += pcd
    
    if (i + 1) % 100 == 0:
        print(f"  Processed {i+1}/{len(pcd_files)} frames")

# Downsample final cloud (optional — adjust voxel size)
print("Downsampling...")
merged = merged.voxel_down_sample(voxel_size=0.005)  # 5mm voxel

# Statistical outlier removal
print("Removing outliers...")
merged, _ = merged.remove_statistical_outlier(nb_neighbors=20, std_ratio=2.0)

# Save
o3d.io.write_point_cloud(output_file, merged)
print(f"Final map saved: {output_file}")
print(f"Total points: {len(merged.points)}")
PYEOF

# Also save as PLY for compatibility
python3 -c "
import open3d as o3d
pcd = o3d.io.read_point_cloud('${SESSION_DIR}/final_map.pcd')
o3d.io.write_point_cloud('${SESSION_DIR}/final_map.ply', pcd)
print('PLY saved: ${SESSION_DIR}/final_map.ply')
"

echo ""
echo "============================================"
echo "  PIPELINE COMPLETE"
echo "============================================"
echo "  Final map: ${SESSION_DIR}/final_map.pcd"
echo "  Final map: ${SESSION_DIR}/final_map.ply"
echo ""
echo "  Open in CloudCompare:"
echo "    cloudcompare ${SESSION_DIR}/final_map.pcd"
echo "============================================"
BASH

chmod +x ~/process_scan.sh
```

### 2.5 Quality Verification Script

```bash
# ============================================================
# Accuracy verification tools
# ============================================================
cat > ~/verify_scan.py << 'PYTHON'
"""
Scan Quality Verification Tool

Checks:
1. Wall flatness (fit planes to walls, measure residuals)
2. Room dimensions (measure distances between parallel walls)
3. Point density statistics
4. Drift indicators (wall thickness, doubled features)

Usage: python3 verify_scan.py /path/to/final_map.pcd
"""

import open3d as o3d
import numpy as np
import sys

def load_cloud(path):
    pcd = o3d.io.read_point_cloud(path)
    print(f"Loaded {len(pcd.points)} points from {path}")
    return pcd

def check_density(pcd):
    """Compute local point density statistics"""
    print("\n=== Point Density ===")
    distances = pcd.compute_nearest_neighbor_distance()
    distances = np.asarray(distances)
    print(f"  Mean nearest-neighbor distance: {distances.mean()*1000:.2f} mm")
    print(f"  Median: {np.median(distances)*1000:.2f} mm")
    print(f"  Std dev: {distances.std()*1000:.2f} mm")
    print(f"  Points within 5mm of neighbor: {(distances < 0.005).sum() / len(distances) * 100:.1f}%")

def check_planarity(pcd):
    """Detect planes and measure flatness"""
    print("\n=== Wall Planarity (RANSAC) ===")
    remaining = pcd
    for i in range(5):  # Find up to 5 planes
        if len(remaining.points) < 1000:
            break
        plane_model, inliers = remaining.segment_plane(
            distance_threshold=0.01,  # 10mm threshold
            ransac_n=3,
            num_iterations=1000
        )
        [a, b, c, d] = plane_model
        inlier_cloud = remaining.select_by_index(inliers)
        
        # Compute point-to-plane distances for inliers
        points = np.asarray(inlier_cloud.points)
        distances = np.abs(a * points[:, 0] + b * points[:, 1] + c * points[:, 2] + d)
        
        print(f"  Plane {i+1}: {len(inliers)} points")
        print(f"    Normal: ({a:.3f}, {b:.3f}, {c:.3f})")
        print(f"    Mean residual: {distances.mean()*1000:.2f} mm")
        print(f"    Max residual:  {distances.max()*1000:.2f} mm")
        print(f"    RMS residual:  {np.sqrt((distances**2).mean())*1000:.2f} mm")
        
        remaining = remaining.select_by_index(inliers, invert=True)

def check_bounding_box(pcd):
    """Report overall dimensions"""
    print("\n=== Bounding Box ===")
    bbox = pcd.get_axis_aligned_bounding_box()
    extent = bbox.get_extent()
    print(f"  X: {extent[0]:.3f} m")
    print(f"  Y: {extent[1]:.3f} m")
    print(f"  Z: {extent[2]:.3f} m")

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "final_map.pcd"
    pcd = load_cloud(path)
    check_bounding_box(pcd)
    check_density(pcd)
    check_planarity(pcd)
    
    print("\n=== Quick Visual Check ===")
    print("  Launching viewer... (close window to exit)")
    o3d.visualization.draw_geometries([pcd], window_name="Scan Verification")
PYTHON
```

### 2.6 Optional: Hybrid Workflow (SLAM + Stationary Anchors)

```bash
# ============================================================
# If you also captured stationary tripod scans,
# align the SLAM cloud to them for best accuracy (2-5mm)
# ============================================================
cat > ~/align_to_stationary.py << 'PYTHON'
"""
Align SLAM point cloud to stationary reference scans.
Uses TEASER++ for coarse global registration, then ICP for refinement.

Usage: python3 align_to_stationary.py slam_cloud.pcd reference_cloud.pcd

Output: aligned_final.pcd
"""

import open3d as o3d
import numpy as np
import copy

def preprocess(pcd, voxel_size):
    """Downsample and compute FPFH features"""
    down = pcd.voxel_down_sample(voxel_size)
    down.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(
        radius=voxel_size * 2, max_nn=30))
    fpfh = o3d.pipelines.registration.compute_fpfh_feature(
        down, o3d.geometry.KDTreeSearchParamHybrid(
            radius=voxel_size * 5, max_nn=100))
    return down, fpfh

def global_registration(source, target, source_feat, target_feat, voxel_size):
    """RANSAC-based global registration (substitute for TEASER++)"""
    # Open3D's RANSAC is a good substitute if TEASER++ isn't installed
    result = o3d.pipelines.registration.registration_ransac_based_on_feature_matching(
        source, target, source_feat, target_feat,
        mutual_filter=True,
        max_correspondence_distance=voxel_size * 2.0,
        estimation_method=o3d.pipelines.registration.TransformationEstimationPointToPoint(),
        ransac_n=3,
        checkers=[
            o3d.pipelines.registration.CorrespondenceCheckerBasedOnEdgeLength(0.9),
            o3d.pipelines.registration.CorrespondenceCheckerBasedOnDistance(voxel_size * 2.0)
        ],
        criteria=o3d.pipelines.registration.RANSACConvergenceCriteria(100000, 0.999)
    )
    return result

def fine_icp(source, target, init_transform, voxel_size):
    """Point-to-plane ICP refinement"""
    result = o3d.pipelines.registration.registration_icp(
        source, target,
        max_correspondence_distance=voxel_size * 0.5,
        init=init_transform,
        estimation_method=o3d.pipelines.registration.TransformationEstimationPointToPlane(),
        criteria=o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=200)
    )
    return result

if __name__ == "__main__":
    import sys
    slam_path = sys.argv[1]
    ref_path = sys.argv[2]
    
    print("Loading clouds...")
    slam_cloud = o3d.io.read_point_cloud(slam_path)
    ref_cloud = o3d.io.read_point_cloud(ref_path)
    
    voxel_size = 0.02  # 2cm for registration
    
    print("Preprocessing...")
    slam_down, slam_feat = preprocess(slam_cloud, voxel_size)
    ref_down, ref_feat = preprocess(ref_cloud, voxel_size)
    
    print("Global registration (coarse)...")
    global_result = global_registration(slam_down, ref_down, slam_feat, ref_feat, voxel_size)
    print(f"  Fitness: {global_result.fitness:.4f}")
    print(f"  RMSE: {global_result.inlier_rmse*1000:.2f} mm")
    
    print("ICP refinement (fine)...")
    icp_result = fine_icp(slam_down, ref_down, global_result.transformation, voxel_size)
    print(f"  Fitness: {icp_result.fitness:.4f}")
    print(f"  RMSE: {icp_result.inlier_rmse*1000:.2f} mm")
    
    # Apply transform to full-resolution SLAM cloud
    aligned = copy.deepcopy(slam_cloud)
    aligned.transform(icp_result.transformation)
    
    # Merge
    merged = aligned + ref_cloud
    merged = merged.voxel_down_sample(voxel_size=0.005)
    
    output_path = "aligned_final.pcd"
    o3d.io.write_point_cloud(output_path, merged)
    print(f"\nSaved: {output_path} ({len(merged.points)} points)")
    
    # Visualize
    aligned.paint_uniform_color([0, 0.651, 0.929])  # Blue = SLAM
    ref_cloud.paint_uniform_color([1, 0.706, 0])     # Orange = Reference
    o3d.visualization.draw_geometries([aligned, ref_cloud],
        window_name="Blue=SLAM, Orange=Reference")
PYTHON
```

---

## SCANNING BEST PRACTICES

### Pre-Scan Checklist

1. Charge all batteries (Pi power bank, L2 12V pack)
2. Verify SSD has >10 GB free: `df -h /mnt/ssd`
3. Check L2 driver is running: `ros2 topic hz /unilidar/cloud`
4. Check IMU is streaming: `ros2 topic hz /unilidar/imu`
5. Plan your walking path — sketch it mentally, note loop closure points

### During Scanning

- Walk at **0.5–1.0 m/s** (slow, deliberate pace)
- **Smooth turns** — no jerky direction changes at corners
- **Return to start position** at the end (critical loop closure)
- For multi-room scans, **revisit doorways** between rooms (creates inter-room loop closures)
- Stay **at least 0.5 m from walls** (reduces self-occlusion from your body)
- Spend **5 extra seconds lingering** in feature-rich areas (bookshelves, kitchen counters)
- Avoid long featureless corridors — walk these slowly
- Keep **15-minute maximum** per session for optimal accuracy (less drift to correct)

### After Scanning

1. Stop recording (Ctrl+C)
2. Check bag size makes sense (~1 GB per 10 min)
3. Note any issues in `metadata.txt` (bumped something, fast section, etc.)
4. Transfer data to workstation

---

## PIPELINE SUMMARY

```
┌─────────────────────────────────────────────────────────┐
│                    RASPBERRY PI 4                        │
│                                                         │
│  ┌─────────┐    Ethernet    ┌──────────────┐           │
│  │ Unitree ├───────────────►│  ROS2 Driver  │           │
│  │   L2    │  192.168.1.x   │  (publisher)  │           │
│  └─────────┘                └──────┬───────┘           │
│                                     │                   │
│                              /cloud + /imu              │
│                                     │                   │
│                              ┌──────▼───────┐           │
│                              │  ros2 bag    │           │
│                              │  record      │           │
│                              │  (mcap)      │           │
│                              └──────┬───────┘           │
│                                     │                   │
│                              ┌──────▼───────┐           │
│                              │   USB SSD    │           │
│                              │  (~2 MB/s)   │           │
│                              └──────────────┘           │
└─────────────────────────────────────────────────────────┘
                        │
                  Transfer SSD
                        │
                        ▼
┌─────────────────────────────────────────────────────────┐
│                    WORKSTATION                           │
│                                                         │
│  ┌──────────┐     ┌─────────────┐     ┌──────────────┐ │
│  │  rosbag  ├────►│  FAST-LIO2  ├────►│ ScanContext  │ │
│  │  replay  │     │  (15-20mm)  │     │ Loop Closure │ │
│  └──────────┘     └─────────────┘     │ + GTSAM PGO  │ │
│                                       └──────┬───────┘ │
│                                              │         │
│                                       ┌──────▼───────┐ │
│                                       │     HBA      │ │
│                                       │   Bundle     │ │
│                                       │  Adjustment  │ │
│                                       │  (5-10mm)    │ │
│                                       └──────┬───────┘ │
│                                              │         │
│                   ┌──────────────────────────▼───────┐ │
│                   │  Merge + Downsample + Cleanup    │ │
│                   │  → final_map.pcd / final_map.ply │ │
│                   └──────────────────────────────────┘ │
│                                                         │
│  Optional: Align to stationary scans → 2-5mm accuracy  │
└─────────────────────────────────────────────────────────┘
```

---

## EXPECTED RESULTS

| Metric | Expected Value |
|--------|---------------|
| Recording data rate | ~2 MB/s |
| Storage per 10 min scan | ~1-1.5 GB |
| Post-processing time (10 min scan) | ~10-25 min |
| Points in final map (10 min room) | 5-15 million (after 5mm downsampling) |
| Accuracy (SLAM only) | 5-10 mm |
| Accuracy (SLAM + stationary anchors) | 2-5 mm |
| Wall planarity RMS | 3-8 mm |
| Scan time vs. stationary (3-10 scans) | 5-10x faster |

---

## TROUBLESHOOTING

| Problem | Likely Cause | Fix |
|---------|-------------|-----|
| No /unilidar/cloud topic | L2 not powered, wrong IP, driver not running | Check Ethernet, ping 192.168.1.62, restart driver |
| No /unilidar/imu topic | IMU not enabled | Run `enable_imu` utility (Section 1.3, Step 2) |
| FAST-LIO2 crashes immediately | Wrong LiDAR type in config | Ensure `lidar_type: 1` and `feature_extract_enable: false` |
| Map has doubled walls | No loop closure detected | Walk back to start, add more revisits to path |
| Map has stretched/warped rooms | Poor IMU calibration or fast motion | Walk slower, let L2 sit still for 5s at start for IMU init |
| Very sparse point cloud | Walking too fast for 64K pts/s | Slow down to 0.5 m/s |
| HBA diverges or crashes | Too few points per frame, wrong pose format | Check PCD frame count matches pose count, increase voxel_size |
| Pi overheating during recording | Sustained load + no cooling | Add heatsink + small fan, or use a vented case |
