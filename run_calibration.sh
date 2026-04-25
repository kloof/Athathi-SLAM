#!/bin/bash
# Camera-LiDAR Extrinsic Calibration Tool launcher
# Double-click or run: ./run_calibration.sh

cd "$(dirname "$0")"

source /opt/ros/humble/setup.bash
source /home/talal/unilidar_sdk2/unitree_lidar_ros2/install/setup.bash

# Start LiDAR driver if not already running
if ! pgrep -f unitree_lidar_ros2_node > /dev/null; then
    echo "Starting LiDAR driver..."
    ros2 run unitree_lidar_ros2 unitree_lidar_ros2_node --ros-args \
        -p initialize_type:=2 -p work_mode:=0 -p use_system_timestamp:=true \
        -p range_min:=0.0 -p range_max:=100.0 -p cloud_scan_num:=18 \
        -p lidar_port:=6101 -p lidar_ip:=192.168.1.62 \
        -p local_port:=6201 -p local_ip:=192.168.1.2 \
        -p cloud_topic:=unilidar/cloud -p imu_topic:=unilidar/imu &
    sleep 3
fi

# Use current DISPLAY (works for both VNC :0 and RDP :10)
export DISPLAY="${DISPLAY:-:0}"
exec python3 calibration_tool.py
