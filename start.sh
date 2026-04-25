#!/bin/bash
# SLAM Scanner startup script — launches the Flask app + Chromium + Onboard OSK.
#
# Touchscreen note: Onboard (the on-screen keyboard) auto-pops on text-field
# focus, but only when the browser exposes its widgets via AT-SPI. Chromium
# needs --force-renderer-accessibility for that. See the gsettings + flags
# block below.

APP_DIR="/home/talal/Desktop/test_slam"
PORT=5000
URL="http://localhost:$PORT"

cd "$APP_DIR" || exit 1

# ---------------------------------------------------------------------------
# Clean-up: kill any stale Flask, ROS, camera, ffmpeg, bag-record, tf, and
# Onboard instances from a previous run. Each line is independent and
# silently swallowed on "no such process" so the script always proceeds.
# ---------------------------------------------------------------------------
echo "Cleaning up stale processes..."

# 1. Anything bound to our Flask port (covers app.py and any debug spawns).
sudo fuser -k "$PORT/tcp" 2>/dev/null

# 2. The Flask app process itself (in case fuser missed it on a different port).
sudo pkill -f 'python3 .*app\.py' 2>/dev/null

# 3. ROS2 LiDAR driver (Unitree L2).
sudo pkill -f 'unitree_lidar_ros2_node' 2>/dev/null
sudo pkill -f 'unilidar' 2>/dev/null

# 4. Our custom camera node (replaces usb_cam — see commit a22d1bf).
sudo pkill -f 'camera_node\.py' 2>/dev/null

# 5. ROS2 bag recorder.
sudo pkill -f 'ros2 bag record' 2>/dev/null
sudo pkill -f 'ros2_bag_record' 2>/dev/null

# 6. tf_static publisher we spawn during recording.
sudo pkill -f 'static_transform_publisher' 2>/dev/null

# 7. Any ffmpeg we spawned for the Brio (preview, camera_node feeder, or
#    review-time recapture). We match on '/dev/video0' so unrelated ffmpeg
#    work the user may be doing on a different file is left alone.
sudo pkill -f 'ffmpeg.*video0' 2>/dev/null

# 8. Stale Onboard daemon — we'll relaunch it ourselves below so any
#    pre-existing instance gets replaced and the gsettings update applies.
pkill -x onboard 2>/dev/null

# Give the kernel a moment to release file handles + USB devices.
sleep 1

# ---------------------------------------------------------------------------
# Onboard on-screen keyboard
# ---------------------------------------------------------------------------
# Idempotent — safe to run on every launch. Errors swallowed (older Ubuntu /
# KDE setups don't ship the schema).
gsettings set org.onboard auto-show enabled true 2>/dev/null
gsettings set org.onboard layout 'Phone' 2>/dev/null
gsettings set org.onboard auto-show reposition-method 'keep-on-screen' 2>/dev/null

# Background-launch Onboard. It stays hidden until a focus-able text field
# appears, then floats up.
onboard --not-show-in='GNOME;Unity' >/dev/null 2>&1 &
ONBOARD_PID=$!

# ---------------------------------------------------------------------------
# Start the Flask app (root needed for eth0 / LiDAR network setup).
# ---------------------------------------------------------------------------
sudo python3 app.py --port "$PORT" &
APP_PID=$!

# Clean up on terminal close / Ctrl+C.
trap '
    sudo kill $APP_PID 2>/dev/null
    [ -n "$ONBOARD_PID" ] && kill $ONBOARD_PID 2>/dev/null
    sudo pkill -f "ros2 bag record" 2>/dev/null
    sudo pkill -f "unitree_lidar_ros2_node" 2>/dev/null
    sudo pkill -f "camera_node\.py" 2>/dev/null
    sudo pkill -f "ffmpeg.*video0" 2>/dev/null
    wait 2>/dev/null
' EXIT

# Wait for the server to come up.
echo "Starting SLAM Scanner on $URL ..."
for i in $(seq 1 15); do
    if curl -s -o /dev/null "$URL" 2>/dev/null; then
        break
    fi
    sleep 1
done

if ! kill -0 $APP_PID 2>/dev/null; then
    echo "ERROR: SLAM Scanner failed to start."
    read -p "Press Enter to close..."
    exit 1
fi

# ---------------------------------------------------------------------------
# Open Chromium with the flags Onboard needs to detect input-field focus.
#
#   --force-renderer-accessibility  → exposes Chromium widgets to AT-SPI so
#                                     Onboard sees focus events.
#   --touch-events=enabled          → tells Chromium the device is touch-first.
#   --no-first-run / --disable-translate / --disable-features=TranslateUI
#                                   → suppress modals that steal focus on
#                                     first launch.
#
# Hard-reload (cache-bust) the SPA on every launch so JS / CSS edits are
# picked up without the technician needing to Ctrl+Shift+R.
# ---------------------------------------------------------------------------
sleep 1
chromium \
    --force-renderer-accessibility \
    --touch-events=enabled \
    --no-first-run \
    --disable-translate \
    --disable-features=TranslateUI \
    --overscroll-history-navigation=0 \
    --disk-cache-dir=/tmp/chromium-cache-slam \
    --disk-cache-size=1 \
    "$URL" >/dev/null 2>&1 &

echo "SLAM Scanner running. Close this window to stop."
wait $APP_PID
