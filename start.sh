#!/bin/bash
# SLAM Scanner startup script

APP_DIR="/home/talal/Desktop/test_slam"
PORT=5000
URL="http://localhost:$PORT"

cd "$APP_DIR" || exit 1

# Add gsutil to PATH
export PATH="$PATH:/home/talal/exec -l /bin/bash/google-cloud-sdk/bin"

# Kill any previous instance
sudo fuser -k "$PORT/tcp" 2>/dev/null
sleep 0.5

# Start the app
sudo python3 app.py --port "$PORT" &
APP_PID=$!

trap 'sudo kill $APP_PID 2>/dev/null; wait $APP_PID 2>/dev/null' EXIT

# Wait for server
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

# Open browser
sleep 1
chromium "$URL" 2>/dev/null &

echo "SLAM Scanner running. Close this window to stop."
wait $APP_PID
