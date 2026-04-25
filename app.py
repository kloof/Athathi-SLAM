#!/usr/bin/env python3
"""Flask web app for ROS2 bag recording with Unitree L2 LiDAR + Logitech Brio camera.

Provides a web interface to start/stop rosbag recordings and list sessions.
Camera is optional — falls back to LiDAR-only if not connected.
Runs on Raspberry Pi 4, accessible over local network.

Usage:
    sudo python3 app.py
    sudo python3 app.py --port 8080
"""

import argparse
import atexit
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone


import cv2
import yaml
from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

import auth
import athathi_proxy

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_USB_RECORDINGS = '/mnt/slam_data/recordings'
RECORDINGS_DIR = _USB_RECORDINGS if os.path.isdir('/mnt/slam_data') else os.path.join(SCRIPT_DIR, 'recordings')
SESSIONS_FILE = os.path.join(SCRIPT_DIR, 'sessions.json')

# Modal cloud-slam-icp endpoint. API key is loaded from .env (see _load_env_file).
MODAL_API_URL = 'https://tiktokredditkw--cloud-slam-icp-web.modal.run'
# Where Modal-derived results are stored (separate from raw .mcap recordings).
_USB_PROCESSED = '/mnt/slam_data/processed'
PROCESSED_DIR = _USB_PROCESSED if os.path.isdir('/mnt/slam_data') else os.path.join(SCRIPT_DIR, 'processed')
# Default poll cadence when the Modal `Retry-After` header is absent.
POLL_INTERVAL_S = 3

IFACE = 'eth0'
HOST_IP = '192.168.1.2'
LIDAR_IP = '192.168.1.62'

ROS_SETUP = '/opt/ros/humble/setup.bash'
DRIVER_SETUP = '/home/talal/unilidar_sdk2/unitree_lidar_ros2/install/setup.bash'

# Camera settings (Logitech Brio) — shared with calibration tools
from camera_config import (
    CAMERA_DEVICE, CAMERA_WIDTH, CAMERA_HEIGHT, CAMERA_FPS_CAPTURE,
    CAMERA_EXPOSURE_100US, CAMERA_GAIN, CAMERA_WB_KELVIN, CAMERA_FOCUS_ABS,
    CAMERA_BRIGHTNESS, CAMERA_SHARPNESS, CAMERA_POWERLINE_HZ,
    lock_camera_controls,
)
CALIBRATION_DIR = os.path.join(SCRIPT_DIR, 'calibration')
INTRINSICS_FILE = os.path.join(CALIBRATION_DIR, 'intrinsics.yaml')
EXTRINSICS_FILE = os.path.join(CALIBRATION_DIR, 'extrinsics.yaml')

app = Flask(__name__)

_LOG_FILE = '/tmp/slam_app_debug.log'
def _log(msg):
    with open(_LOG_FILE, 'a') as f:
        f.write(f'{datetime.now().isoformat()} {msg}\n')


# ---------------------------------------------------------------------------
# .env loader (no external dep)
# ---------------------------------------------------------------------------

def _load_env_file(path):
    """Parse a tiny KEY=value `.env` file and return a dict.

    Grammar:
      - Lines starting with `#` are ignored.
      - Blank lines are ignored.
      - `KEY=value`, `KEY="value"`, `KEY='value'` all supported (surrounding
        quotes stripped, but only matched pairs).
      - CRLF tolerated (line is rstripped).
      - No shell expansion; no nested quotes.

    Missing file → empty dict (no exception).
    """
    out = {}
    if not os.path.isfile(path):
        return out
    try:
        with open(path, 'r') as f:
            for raw in f:
                line = raw.rstrip('\r\n').strip()
                if not line or line.startswith('#'):
                    continue
                if '=' not in line:
                    continue
                key, _, val = line.partition('=')
                key = key.strip()
                val = val.strip()
                if not key:
                    continue
                # Strip matching surrounding quotes only.
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                out[key] = val
    except OSError:
        return out
    return out


_ENV = _load_env_file(os.path.join(SCRIPT_DIR, '.env'))
MODAL_API_KEY = _ENV.get('MODAL_API', '')
if not MODAL_API_KEY:
    print('WARNING: MODAL_API not set in .env — /api/session/<id>/process '
          'will return 503. Recording and calibration still work.',
          file=sys.stderr)

# zstd availability is checked at boot. If missing, /process returns 503.
_ZSTD_BIN = shutil.which('zstd')
if not _ZSTD_BIN:
    print('WARNING: `zstd` binary not found on PATH — /api/session/<id>/process '
          'will return 503. Install with `apt install zstd`.',
          file=sys.stderr)

# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------

_sessions_lock = threading.Lock()


def _load_sessions():
    if not os.path.isfile(SESSIONS_FILE):
        return {}
    try:
        with open(SESSIONS_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def _save_sessions(sessions):
    tmp = SESSIONS_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(sessions, f, indent=2)
    os.replace(tmp, SESSIONS_FILE)


def _get_sessions():
    with _sessions_lock:
        return _load_sessions()


def _get_session(session_id):
    return _get_sessions().get(session_id)


def _put_session(session_id, session):
    with _sessions_lock:
        sessions = _load_sessions()
        sessions[session_id] = session
        _save_sessions(sessions)


def _delete_session(session_id):
    with _sessions_lock:
        sessions = _load_sessions()
        sessions.pop(session_id, None)
        _save_sessions(sessions)


# ---------------------------------------------------------------------------
# Recording state
# ---------------------------------------------------------------------------

_record_lock = threading.RLock()
# Tracks the ffmpeg subprocess serving /api/camera/preview so recording can
# reclaim /dev/video0 before launching v4l2_camera_node.
_active_preview = {'proc': None}
_active_recording = {
    'session_id': None,
    'driver_proc': None,
    'camera_proc': None,
    'tf_proc': None,
    'bag_proc': None,
    'camera_monitor_proc': None,
    'start_time': None,
    'starting': False,
    'camera_ok': False,
    'camera_frames': 0,
    'camera_streaming': False,
    '_camera_frames_prev': 0,
}

# ---------------------------------------------------------------------------
# SLAM processing state
# ---------------------------------------------------------------------------

_processing_lock = threading.Lock()
# Maps session_id -> { 'status': str, 'start_time': float, 'progress': str }
_active_processing = {}


def _is_recording():
    proc = _active_recording['bag_proc']
    if proc is None:
        return False
    if proc.poll() is not None:
        # bag process died — clean up ALL associated processes under lock
        with _record_lock:
            if _active_recording['bag_proc'] is proc:
                _kill_process_group(_active_recording.get('driver_proc'))
                _kill_process_group(_active_recording.get('camera_proc'), timeout=10)
                _kill_process_group(_active_recording.get('tf_proc'))
                _kill_process_group(_active_recording.get('camera_monitor_proc'))
                # Update session status so it doesn't stay stuck as 'recording'
                sid = _active_recording.get('session_id')
                if sid:
                    session = _get_session(sid)
                    if session and session.get('status') == 'recording':
                        session['status'] = 'error'
                        session['error'] = 'Recording process died unexpectedly'
                        _put_session(sid, session)
                _active_recording['bag_proc'] = None
                _active_recording['driver_proc'] = None
                _active_recording['camera_proc'] = None
                _active_recording['tf_proc'] = None
                _active_recording['camera_monitor_proc'] = None
                _active_recording['start_time'] = None
                _active_recording['session_id'] = None
                _active_recording['camera_ok'] = False
                _active_recording['camera_frames'] = 0
                _active_recording['camera_streaming'] = False
        return False
    return True


def _is_busy():
    """Check if recording or in the process of starting."""
    return _is_recording() or _active_recording['starting']


# ---------------------------------------------------------------------------
# Network setup
# ---------------------------------------------------------------------------

def _check_network():
    """Read-only check of eth0 configuration."""
    try:
        result = subprocess.run(
            ['ip', '-4', 'addr', 'show', IFACE],
            capture_output=True, text=True
        )
        if HOST_IP in result.stdout:
            return True, f'{HOST_IP} on {IFACE}'
        return False, f'{IFACE} not configured'
    except Exception as e:
        return False, str(e)


def _setup_network():
    """Configure eth0 for LiDAR connection (idempotent)."""
    ok, msg = _check_network()
    if ok:
        return True, msg
    try:
        subprocess.run(['ip', 'addr', 'flush', 'dev', IFACE],
                       capture_output=True)
        subprocess.run(['ip', 'addr', 'add', f'{HOST_IP}/24', 'dev', IFACE],
                       capture_output=True, check=True)
        subprocess.run(['ip', 'link', 'set', IFACE, 'up'],
                       capture_output=True, check=True)
        return True, f'Configured {HOST_IP}/24 on {IFACE}'
    except subprocess.CalledProcessError as e:
        return False, f'Network setup failed: {e}'


def _ping_lidar():
    """Check if LiDAR is reachable."""
    result = subprocess.run(
        ['ping', '-c', '1', '-W', '2', LIDAR_IP],
        capture_output=True
    )
    return result.returncode == 0


def _find_camera_device():
    """Find the Brio camera device node dynamically (index 0 = RGB capture)."""
    sysfs = '/sys/class/video4linux'
    if not os.path.isdir(sysfs):
        return None
    for dev_name in sorted(os.listdir(sysfs)):
        name_path = os.path.join(sysfs, dev_name, 'name')
        index_path = os.path.join(sysfs, dev_name, 'index')
        try:
            with open(name_path) as f:
                name = f.read().strip()
            with open(index_path) as f:
                index = int(f.read().strip())
            if 'Logitech BRIO' in name and index == 0:
                return f'/dev/{dev_name}'
        except (OSError, ValueError):
            continue
    return None


def _check_camera():
    """Check if Brio camera is connected. Updates CAMERA_DEVICE dynamically."""
    global CAMERA_DEVICE
    try:
        dev = _find_camera_device()
        if dev is None:
            return False, 'No camera device'
        CAMERA_DEVICE = dev
        dev_base = os.path.basename(dev)
        with open(f'/sys/class/video4linux/{dev_base}/name') as f:
            name = f.read().strip()
        return True, f'{name} ({dev})'
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Driver + recording management
# ---------------------------------------------------------------------------

def _launch_driver():
    """Launch the Unitree LiDAR ROS2 driver node directly (no rviz)."""
    cmd = (
        f'source {ROS_SETUP} && '
        f'source {DRIVER_SETUP} && '
        f'ros2 run unitree_lidar_ros2 unitree_lidar_ros2_node '
        f'--ros-args '
        f'-p initialize_type:=2 '
        f'-p work_mode:=0 '
        f'-p use_system_timestamp:=true '
        f'-p range_min:=0.0 '
        f'-p range_max:=100.0 '
        f'-p cloud_scan_num:=18 '
        f'-p lidar_port:=6101 '
        f'-p lidar_ip:={LIDAR_IP} '
        f'-p local_port:=6201 '
        f'-p local_ip:={HOST_IP} '
        f'-p cloud_topic:=unilidar/cloud '
        f'-p imu_topic:=unilidar/imu '
    )
    return subprocess.Popen(
        cmd, shell=True, executable='/bin/bash',
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        preexec_fn=os.setsid
    )


def _set_brio_fov(fov=90):
    """Set Brio FOV using cameractrls (must be called before v4l2_camera takes the device)."""
    try:
        subprocess.run(
            ['python3', '/tmp/cameractrls/cameractrls.py',
             '-d', CAMERA_DEVICE, '-c', f'logitech_brio_fov={fov}'],
            capture_output=True, timeout=5
        )
    except Exception:
        pass


def _lock_camera_controls():
    """Thin wrapper around camera_config.lock_camera_controls that logs readback."""
    readback = lock_camera_controls(CAMERA_DEVICE)
    if readback:
        _log(f'camera lock: {readback.replace(chr(10), " | ")}')


_CAMERA_STDERR_LOG = '/tmp/slam_camera_stderr.log'


def _release_camera_device(timeout=3.0):
    """Make CAMERA_DEVICE openable before a new scan claims it.

    Steps in order: (1) terminate the tracked preview ffmpeg, (2) poll up to
    `timeout` seconds for the device to go free, (3) if still busy, `fuser
    -k -9` any holders (orphan ffmpeg from a prior scan whose cleanup was
    interrupted) and re-poll once. Must run before _launch_camera().
    """
    preview = _active_preview.get('proc')
    if preview is not None:
        try:
            preview.terminate()
            try:
                preview.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                preview.kill()
                preview.wait(timeout=1.5)
        except Exception:
            pass
        _active_preview['proc'] = None

    # Poll until we can open the device exclusively (or timeout).
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            fd = os.open(CAMERA_DEVICE, os.O_RDWR | os.O_NONBLOCK)
            os.close(fd)
            return True
        except OSError:
            time.sleep(0.1)

    # Still busy — log who's holding it, then fuser -k -9. On this Pi only
    # our own camera pipeline uses the Brio, so this only hits orphans.
    _log(f'_release_camera_device: {CAMERA_DEVICE} still busy after {timeout}s')
    try:
        # fuser -v writes a human-readable "process holding file" list to stderr.
        result = subprocess.run(
            ['fuser', '-v', CAMERA_DEVICE],
            capture_output=True, timeout=2, text=True,
        )
        holders = (result.stderr or result.stdout or '').strip()
        if holders:
            _log(f'_release_camera_device: holders of {CAMERA_DEVICE}:\n{holders}')
    except Exception as e:
        _log(f'_release_camera_device: fuser -v failed: {e}')
    _log(f'_release_camera_device: invoking fuser -k -9 {CAMERA_DEVICE}')
    try:
        subprocess.run(
            ['fuser', '-k', '-9', CAMERA_DEVICE],
            capture_output=True, timeout=3,
        )
    except Exception as e:
        _log(f'_release_camera_device: fuser -k failed: {e}')
    # Give the kernel a beat to release the fd after SIGKILL reaps the holder.
    time.sleep(0.5)
    try:
        fd = os.open(CAMERA_DEVICE, os.O_RDWR | os.O_NONBLOCK)
        os.close(fd)
        return True
    except OSError:
        return False


def _launch_camera():
    """Launch the custom Brio ROS2 publisher (camera_node.py).

    Replaces usb_cam, which on Humble/ARM64 with this Brio:
      - raw_mjpeg: strips chroma → grayscale JPEGs
      - mjpeg2rgb: crashes with 'Unable to exchange buffer with the driver'
      - hardcodes brightness=50 on startup, overriding v4l2 locks
    Our camera_node.py pipes ffmpeg MJPG out of /dev/video0 (which works
    correctly) and republishes each JPEG as-is on /camera/image_raw/compressed.
    """
    _set_brio_fov(90)

    # Reclaim /dev/video0 from the preview stream before the camera node opens it.
    if not _release_camera_device(timeout=3.0):
        _log(f'WARNING: {CAMERA_DEVICE} still busy after 3s; camera launch will likely fail')

    _lock_camera_controls()

    camera_node_script = os.path.join(SCRIPT_DIR, 'camera_node.py')
    cmd = (
        f'source {ROS_SETUP} && '
        f'python3 {camera_node_script}'
    )
    # Redirect stderr to a log file so failures are visible post-mortem rather
    # than trapped in an unread PIPE buffer.
    stderr_fh = open(_CAMERA_STDERR_LOG, 'ab')
    return subprocess.Popen(
        cmd, shell=True, executable='/bin/bash',
        stdout=subprocess.DEVNULL, stderr=stderr_fh,
        preexec_fn=os.setsid
    )


def _launch_tf_static():
    """Publish camera-to-lidar static transform if extrinsics exist."""
    if not os.path.isfile(EXTRINSICS_FILE):
        return None

    try:
        with open(EXTRINSICS_FILE) as f:
            ext = yaml.safe_load(f)
        t = ext['translation']
        r = ext['rotation']
    except Exception as e:
        print(f'Warning: Failed to read extrinsics: {e}')
        return None

    cmd = (
        f'source {ROS_SETUP} && '
        f'ros2 run tf2_ros static_transform_publisher '
        f'--x {t["x"]} --y {t["y"]} --z {t["z"]} '
        f'--qx {r["x"]} --qy {r["y"]} --qz {r["z"]} --qw {r["w"]} '
        f'--frame-id unilidar_lidar --child-frame-id camera_optical_frame'
    )
    return subprocess.Popen(
        cmd, shell=True, executable='/bin/bash',
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        preexec_fn=os.setsid
    )


def _wait_for_topics(timeout=30):
    """Wait for /unilidar/cloud topic to appear."""
    return _wait_for_topic('/unilidar/cloud', timeout=timeout)


def _wait_for_topic(topic, timeout=15):
    """Wait for a specific ROS2 topic using a fast rclpy subprocess."""
    # Use a single subprocess that sources ROS2 and polls with rclpy
    # This is ~10x faster than `ros2 topic list` CLI
    script = f'''
import rclpy, time, sys
rclpy.init()
node = rclpy.create_node("_topic_wait")
deadline = time.time() + {timeout}
while time.time() < deadline:
    topics = [t[0] for t in node.get_topic_names_and_types()]
    if "{topic}" in topics:
        node.destroy_node()
        rclpy.shutdown()
        sys.exit(0)
    time.sleep(0.5)
node.destroy_node()
rclpy.shutdown()
sys.exit(1)
'''
    try:
        result = subprocess.run(
            f'source {ROS_SETUP} && python3 -c {shlex.quote(script)}',
            shell=True, executable='/bin/bash',
            capture_output=True, timeout=timeout + 5
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _trim_bag_to_sync_start(bag_dir, required_topics):
    """Invoke trim_bag.py with ROS2 sourced (rosbag2_py not available in app.py env)."""
    script = os.path.join(SCRIPT_DIR, 'trim_bag.py')
    topics_arg = ' '.join(shlex.quote(t) for t in required_topics)
    cmd = (
        f'source {ROS_SETUP} && '
        f'python3 {shlex.quote(script)} {shlex.quote(bag_dir)} {topics_arg}'
    )
    try:
        result = subprocess.run(
            cmd, shell=True, executable='/bin/bash',
            capture_output=True, timeout=60, text=True
        )
        out = (result.stdout or '').strip()
        err = (result.stderr or '').strip()
        if out:
            _log(out)
        if err:
            _log(f'trim stderr: {err}')
    except Exception as e:
        _log(f'trim: invocation failed: {e}')


def _start_bag_record(output_dir, topics=None):
    """Start ros2 bag record process."""
    if topics is None:
        topics = ['/unilidar/cloud', '/unilidar/imu']
    os.makedirs(output_dir, exist_ok=True)
    bag_path = os.path.join(output_dir, 'rosbag')
    topics_str = ' '.join(topics)
    qos_override = os.path.join(SCRIPT_DIR, 'bag_qos_override.yaml')
    qos_arg = f'--qos-profile-overrides-path {shlex.quote(qos_override)} ' if os.path.isfile(qos_override) else ''
    cmd = (
        f'source {ROS_SETUP} && '
        f'ros2 bag record '
        f'-o {shlex.quote(bag_path)} '
        f'--storage mcap '
        f'--max-cache-size 200000000 '
        f'{qos_arg}'
        f'{topics_str}'
    )
    return subprocess.Popen(
        cmd, shell=True, executable='/bin/bash',
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        preexec_fn=os.setsid
    )


def _kill_process_group(proc, timeout=5):
    """Kill a process and its entire group. `timeout` is the SIGTERM grace period."""
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=timeout)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
            pass


def _start_camera_monitor():
    """Spawn a long-lived rclpy subscriber that prints per-second frame counts.

    Sensor data QoS (BEST_EFFORT) is required — most camera drivers publish with
    that profile, and a default RELIABLE subscriber would silently receive nothing.
    """
    script = '''
import sys, rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import CompressedImage

class Counter(Node):
    def __init__(self):
        super().__init__("_camera_monitor")
        self.count = 0
        self.create_subscription(
            CompressedImage, "/camera/image_raw/compressed",
            self._cb, qos_profile_sensor_data)
        self.create_timer(1.0, self._emit)
    def _cb(self, _msg):
        self.count += 1
    def _emit(self):
        sys.stdout.write("FRAMES:%d\\n" % self.count)
        sys.stdout.flush()

rclpy.init()
n = Counter()
try:
    rclpy.spin(n)
except KeyboardInterrupt:
    pass
n.destroy_node()
rclpy.shutdown()
'''
    cmd = f'source {ROS_SETUP} && python3 -u -c {shlex.quote(script)}'
    return subprocess.Popen(
        cmd, shell=True, executable='/bin/bash',
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )


def _camera_monitor_reader(proc):
    """Read FRAMES:N lines from the monitor and update _active_recording."""
    try:
        for raw in proc.stdout:
            line = raw.decode('utf-8', errors='ignore').strip()
            if not line.startswith('FRAMES:'):
                continue
            try:
                count = int(line.split(':', 1)[1])
            except ValueError:
                continue
            _active_recording['camera_frames'] = count
            _active_recording['camera_streaming'] = count > _active_recording['_camera_frames_prev']
            _active_recording['_camera_frames_prev'] = count
    except Exception:
        pass
    finally:
        # If the monitor subprocess dies or its stdout closes, don't let
        # camera_streaming stay stuck True — surface the failure to the UI.
        _active_recording['camera_streaming'] = False


# ---------------------------------------------------------------------------
# Routes — API
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
def api_status():
    """Get current system status (read-only, no side effects)."""
    net_ok, net_msg = _check_network()
    lidar_reachable = _ping_lidar() if net_ok else False
    camera_ok, camera_msg = _check_camera()

    recording = _is_recording()
    elapsed = 0
    active_session = None
    camera_recording = None
    camera_frames = None
    in_flight = bool(_active_recording.get('starting')) or recording
    inflight_status = 'idle'
    if recording and _active_recording['start_time']:
        elapsed = round(time.time() - _active_recording['start_time'], 1)
        active_session = _active_recording['session_id']
        sess = _get_session(active_session) if active_session else None
        inflight_status = (sess.get('status', 'recording') if sess else 'recording')
        if _active_recording.get('camera_ok'):
            camera_recording = bool(_active_recording.get('camera_streaming'))
            camera_frames = int(_active_recording.get('camera_frames') or 0)
        else:
            camera_recording = False
            camera_frames = 0
    elif in_flight:
        # Pre-bag window: surface the in-flight session's status so the SPA
        # fallback poll can flip the Stop button on without waiting for SSE.
        active_session = _active_recording.get('session_id')
        sess = _get_session(active_session) if active_session else None
        inflight_status = (sess.get('status', 'starting') if sess else 'starting')

    # Mirror the SSE processing-snapshot so the SPA's polling fallback can
    # keep the timer + stage label fresh when EventSource drops on Chromium
    # kiosk. Same lock discipline as `/api/events` (Step 5 / plan §22b).
    processing = {}
    with _processing_lock:
        proc_snapshot = {sid: dict(proc) for sid, proc in _active_processing.items()}
    for sid, proc in proc_snapshot.items():
        stage = proc.get('stage') or proc.get('status') or ''
        err_msg = None
        sess_for_err = _get_session(sid)
        if isinstance(sess_for_err, dict):
            err_msg = sess_for_err.get('slam_error')
        processing[sid] = {
            'status': proc.get('status'),
            'stage': stage,
            'progress': stage,
            'job_id': proc.get('job_id'),
            'elapsed': round(time.time() - proc.get('start_time', time.time()), 1),
            'run_id': proc.get('run_id'),
            'error': err_msg,
        }

    return jsonify({
        'network': {'ok': net_ok, 'message': net_msg},
        'lidar_reachable': lidar_reachable,
        'camera': {'ok': camera_ok, 'message': camera_msg},
        'calibrated': {
            'intrinsics': os.path.isfile(INTRINSICS_FILE),
            'extrinsics': os.path.isfile(EXTRINSICS_FILE),
        },
        'recording': recording,
        'in_flight': in_flight,
        'inflight_status': inflight_status,
        'elapsed': elapsed,
        'active_session': active_session,
        'camera_recording': camera_recording,
        'camera_frames': camera_frames,
        'processing': processing,
    })


@app.route('/api/sessions')
def api_sessions():
    """List all recording sessions."""
    sessions = _get_sessions()
    result = []
    for sid, s in sorted(sessions.items(), key=lambda x: x[1].get('created', ''), reverse=True):
        bag_path = os.path.join(RECORDINGS_DIR, s['name'], 'rosbag')
        bag_size = None
        if os.path.isdir(bag_path):
            total = sum(
                os.path.getsize(os.path.join(bag_path, f))
                for f in os.listdir(bag_path)
                if os.path.isfile(os.path.join(bag_path, f))
            )
            bag_size = f'{total / (1024*1024):.1f} MB'

        # Build a small summary from the persisted result envelope so the
        # session list is cheap to render. The full envelope is fetched
        # on-demand via /api/session/<id>/result.
        result_summary = _result_summary_from_session(s)

        result.append({
            'id': sid,
            'name': s['name'],
            'created': s.get('created', ''),
            'status': s.get('status', 'unknown'),
            'bag_size': bag_size,
            'duration': s.get('duration'),
            'scp_path': os.path.join(RECORDINGS_DIR, s['name']),
            'slam_status': s.get('slam_status'),
            'slam_stage': s.get('slam_stage'),
            'slam_error': s.get('slam_error'),
            'job_id': s.get('job_id'),
            'result_summary': result_summary,
        })
    return jsonify(result)


def _result_summary_from_session(session):
    """Compute the small headline-numbers summary shown on each session card.

    Source of truth is the on-disk `processed/<name>/result.json` (so a
    re-process refreshes it), but we fall back to whatever's stored on the
    session record if the file is missing.
    """
    envelope = None
    name = session.get('name')
    if name:
        result_path = os.path.join(_processed_dir_for_session(name),
                                   'result.json')
        if os.path.isfile(result_path):
            try:
                with open(result_path, 'r') as f:
                    envelope = json.load(f)
            except (OSError, json.JSONDecodeError):
                envelope = None
    if envelope is None:
        envelope = session.get('slam_result')
    if not envelope or not isinstance(envelope, dict):
        return None
    fp = envelope.get('floorplan') or {}
    metrics = envelope.get('metrics') or {}
    return {
        'num_walls': len(fp.get('walls') or []),
        'num_doors': len(fp.get('doors') or []),
        'num_windows': len(fp.get('windows') or []),
        'num_furniture': len(envelope.get('furniture') or []),
        'total_duration_s': metrics.get('total_duration_s'),
    }


@app.route('/api/record/start', methods=['POST'])
def api_record_start():
    """Start a new recording session."""
    with _record_lock:
        if _is_busy():
            return jsonify({'error': 'Already recording or starting'}), 409

        data = request.get_json(force=True, silent=True) or {}
        raw_name = data.get('name', '')
        session_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', raw_name).strip('_') if raw_name else ''
        if not session_name:
            session_name = f'scan_{datetime.now().strftime("%Y%m%d_%H%M%S")}'

        # Setup network
        net_ok, net_msg = _setup_network()
        if not net_ok:
            return jsonify({'error': net_msg}), 500

        _active_recording['starting'] = True

        session_id = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        session = {
            'name': session_name,
            'created': datetime.now().isoformat(),
            'status': 'starting',
        }
        _put_session(session_id, session)

        # Launch in background thread
        thread = threading.Thread(
            target=_recording_thread,
            args=(session_id, session_name),
            daemon=True,
        )
        thread.start()

        return jsonify({'session_id': session_id, 'name': session_name})


def _recording_thread(session_id, session_name):
    """Background thread: launch driver + camera, wait for topics, start bag record."""
    driver_proc = None
    camera_proc = None
    tf_proc = None
    try:
        # Launch LiDAR driver
        session = _get_session(session_id)
        session['status'] = 'launching_driver'
        _put_session(session_id, session)

        driver_proc = _launch_driver()
        _active_recording['driver_proc'] = driver_proc

        # Launch camera (optional)
        camera_ok, camera_msg = _check_camera()
        _log(f'Camera check: ok={camera_ok}, msg={camera_msg}, device={CAMERA_DEVICE}')
        if camera_ok:
            camera_proc = _launch_camera()
            _active_recording['camera_proc'] = camera_proc
            _log(f'Camera launched: PID={camera_proc.pid}')
            tf_proc = _launch_tf_static()
            _active_recording['tf_proc'] = tf_proc
        _active_recording['camera_ok'] = camera_ok

        # Brief warmup — just enough to catch immediate crashes
        time.sleep(2)
        if driver_proc.poll() is not None:
            session['status'] = 'error'
            session['error'] = 'Driver exited during warmup'
            _put_session(session_id, session)
            _kill_process_group(camera_proc, timeout=10)
            _kill_process_group(tf_proc)
            _active_recording['driver_proc'] = None
            _active_recording['camera_proc'] = None
            _active_recording['tf_proc'] = None
            _active_recording['camera_ok'] = False
            return

        # Wait for LiDAR topics
        session['status'] = 'waiting_for_topics'
        _put_session(session_id, session)

        _log('Waiting for LiDAR topics...')
        if not _wait_for_topics(timeout=30):
            _log('LiDAR topics NOT found after 30s')
            session['status'] = 'error'
            session['error'] = 'Topics not found after 30s'
            _put_session(session_id, session)
            _kill_process_group(driver_proc)
            _kill_process_group(camera_proc, timeout=10)
            _kill_process_group(tf_proc)
            _active_recording['driver_proc'] = None
            _active_recording['camera_proc'] = None
            _active_recording['tf_proc'] = None
            return

        _log('LiDAR topics found!')

        # Wait for camera topics (non-fatal if missing) and verify frames actually flow.
        # Topic advertisement alone isn't enough — a stalled camera node still advertises.
        camera_monitor_proc = None
        if camera_ok:
            _log('Waiting for camera topic...')
            camera_topic_ok = _wait_for_topic('/camera/image_raw/compressed', timeout=15)
            _log(f'Camera topic found: {camera_topic_ok}')
            if not camera_topic_ok:
                _log('WARNING: Camera topic not found, recording without camera')
                camera_ok = False
                _active_recording['camera_ok'] = False

            if camera_ok:
                _log('Verifying camera frames are actually flowing...')
                _active_recording['camera_frames'] = 0
                _active_recording['_camera_frames_prev'] = 0
                _active_recording['camera_streaming'] = False
                camera_monitor_proc = _start_camera_monitor()
                _active_recording['camera_monitor_proc'] = camera_monitor_proc
                threading.Thread(
                    target=_camera_monitor_reader, args=(camera_monitor_proc,),
                    daemon=True,
                ).start()
                # Allow up to 20s for the first frame. Must cover the recovery
                # path: if the first ffmpeg session inside camera_node fails
                # (device busy), camera_node runs a USB unbind/rebind (~5–10s)
                # and restarts ffmpeg (probe adds ~3s) before the first publish.
                # 10s was too tight and caused "recording without camera".
                deadline = time.time() + 20.0
                while time.time() < deadline:
                    if _active_recording['camera_frames'] > 0:
                        break
                    time.sleep(0.1)
                if _active_recording['camera_frames'] == 0:
                    _log('WARNING: No camera frames received in 20s — recording without camera')
                    _kill_process_group(camera_monitor_proc)
                    _active_recording['camera_monitor_proc'] = None
                    camera_monitor_proc = None
                    camera_ok = False
                    _active_recording['camera_ok'] = False
                else:
                    _log(f'Camera streaming OK ({_active_recording["camera_frames"]} frames received)')

        # Build topic list
        topics = ['/unilidar/cloud', '/unilidar/imu']
        if camera_ok:
            topics.extend([
                '/camera/image_raw/compressed',
                '/camera/camera_info',
                '/tf_static',
            ])

        # Start recording
        output_dir = os.path.join(RECORDINGS_DIR, session_name)
        bag_proc = _start_bag_record(output_dir, topics)

        _active_recording['session_id'] = session_id
        _active_recording['bag_proc'] = bag_proc
        _active_recording['start_time'] = time.time()

        session['status'] = 'recording'
        session['camera'] = camera_ok
        _put_session(session_id, session)

    except Exception as e:
        session = _get_session(session_id)
        if session:
            session['status'] = 'error'
            session['error'] = str(e)
            _put_session(session_id, session)
        _kill_process_group(driver_proc)
        _kill_process_group(camera_proc, timeout=10)
        _kill_process_group(tf_proc)
        _kill_process_group(_active_recording.get('camera_monitor_proc'))
        _active_recording['driver_proc'] = None
        _active_recording['camera_proc'] = None
        _active_recording['tf_proc'] = None
        _active_recording['bag_proc'] = None
        _active_recording['camera_monitor_proc'] = None
        _active_recording['session_id'] = None
        _active_recording['start_time'] = None
        _active_recording['camera_ok'] = False
        _active_recording['camera_frames'] = 0
        _active_recording['camera_streaming'] = False
    finally:
        _active_recording['starting'] = False


@app.route('/api/record/stop', methods=['POST'])
def api_record_stop():
    """Stop the current recording."""
    with _record_lock:
        if not _is_recording():
            return jsonify({'error': 'Not recording'}), 409

        session_id = _active_recording['session_id']
        bag_proc = _active_recording['bag_proc']
        driver_proc = _active_recording['driver_proc']
        camera_proc = _active_recording['camera_proc']
        tf_proc = _active_recording['tf_proc']
        start_time = _active_recording['start_time']
        camera_ok = _active_recording['camera_ok']

        # Stop bag recording first, giving MCAP time to flush footer + metadata.yaml.
        # A premature SIGKILL here corrupts the bag (missing index, 0-byte metadata).
        _kill_process_group(bag_proc, timeout=30)

        # Stop driver, camera, and the camera monitor subscriber.
        # camera_proc gets 10s to run its finally block (ffmpeg cleanup +
        # rclpy shutdown). A shorter grace lets SIGKILL hit before cleanup,
        # which can leave ffmpeg orphaned holding /dev/video0.
        camera_monitor_proc = _active_recording.get('camera_monitor_proc')
        _kill_process_group(driver_proc)
        _kill_process_group(camera_proc, timeout=10)
        _kill_process_group(tf_proc)
        _kill_process_group(camera_monitor_proc)

        duration = round(time.time() - start_time, 1) if start_time else 0

        # Trim leading single-sensor window so the bag starts with all
        # required topics already live (cleaner for time-sync SLAM fusion).
        session_preview = _get_session(session_id)
        if session_preview and camera_ok:
            bag_path_preview = os.path.join(
                RECORDINGS_DIR, session_preview['name'], 'rosbag')
            if os.path.isdir(bag_path_preview):
                _trim_bag_to_sync_start(
                    bag_path_preview,
                    required_topics=['/unilidar/cloud',
                                     '/camera/image_raw/compressed']
                )

        # Update session
        session = _get_session(session_id)
        if session:
            session['status'] = 'stopped'
            session['duration'] = duration

            # Get bag size
            bag_path = os.path.join(RECORDINGS_DIR, session['name'], 'rosbag')
            if os.path.isdir(bag_path):
                total = sum(
                    os.path.getsize(os.path.join(bag_path, f))
                    for f in os.listdir(bag_path)
                    if os.path.isfile(os.path.join(bag_path, f))
                )
                session['bag_size'] = f'{total / (1024*1024):.1f} MB'

            # Build topic list for metadata
            topics = ['/unilidar/cloud', '/unilidar/imu']
            if camera_ok:
                topics.extend(['/camera/image_raw/compressed',
                               '/camera/camera_info', '/tf_static'])

            # Save metadata file
            session_dir = os.path.join(RECORDINGS_DIR, session['name'])
            meta_path = os.path.join(session_dir, 'metadata.txt')
            try:
                with open(meta_path, 'w') as f:
                    f.write(f"Session: {session['name']}\n")
                    f.write(f"Date: {datetime.now().isoformat()}\n")
                    f.write(f"Duration: {duration}s\n")
                    f.write(f"Bag size: {session.get('bag_size', 'unknown')}\n")
                    f.write(f"Topics: {' '.join(topics)}\n")
                    f.write(f"Camera: {'yes' if camera_ok else 'no'}\n")
                    if camera_ok:
                        f.write(f"Camera resolution: {CAMERA_WIDTH}x{CAMERA_HEIGHT}@{CAMERA_FPS_CAPTURE}fps\n")
                        f.write(f"Intrinsics: {'calibrated' if os.path.isfile(INTRINSICS_FILE) else 'uncalibrated'}\n")
                        f.write(f"Extrinsics: {'calibrated' if os.path.isfile(EXTRINSICS_FILE) else 'uncalibrated'}\n")
                    f.write(f"Notes:\n")
            except OSError:
                pass

            # Copy calibration files into session directory
            calib_dest = os.path.join(session_dir, 'calibration')
            os.makedirs(calib_dest, exist_ok=True)
            for src_file in [INTRINSICS_FILE, EXTRINSICS_FILE]:
                if os.path.isfile(src_file):
                    shutil.copy2(src_file, calib_dest)

            _put_session(session_id, session)

        # Reset state
        _active_recording['session_id'] = None
        _active_recording['bag_proc'] = None
        _active_recording['driver_proc'] = None
        _active_recording['camera_proc'] = None
        _active_recording['tf_proc'] = None
        _active_recording['camera_monitor_proc'] = None
        _active_recording['start_time'] = None
        _active_recording['starting'] = False
        _active_recording['camera_ok'] = False
        _active_recording['camera_frames'] = 0
        _active_recording['camera_streaming'] = False

        return jsonify({
            'session_id': session_id,
            'duration': duration,
            'bag_size': session.get('bag_size') if session else None,
        })


@app.route('/api/camera/preview')
def api_camera_preview():
    """MJPEG stream for camera preview (only when not recording)."""
    if _is_busy():
        return jsonify({'error': 'Camera busy during recording'}), 409

    camera_ok, _ = _check_camera()
    if not camera_ok:
        return jsonify({'error': 'No camera connected'}), 404

    def generate():
        # Lock AE/AWB/AF on the device so preview matches recording
        _lock_camera_controls()

        # ffmpeg MJPG passthrough: camera emits JPEG, we forward bytes with
        # no decode/re-encode so CPU stays low even at 30 fps.
        proc = subprocess.Popen(
            ['ffmpeg', '-hide_banner', '-loglevel', 'error',
             '-f', 'v4l2', '-input_format', 'mjpeg',
             '-video_size', '640x360', '-framerate', '30',
             '-i', CAMERA_DEVICE,
             '-c:v', 'copy', '-f', 'mjpeg', 'pipe:1'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        _active_preview['proc'] = proc

        try:
            buf = bytearray()
            while True:
                if _is_busy():
                    break
                chunk = proc.stdout.read(16384)
                if not chunk:
                    break
                buf.extend(chunk)
                while True:
                    soi = buf.find(b'\xff\xd8')
                    if soi < 0:
                        buf.clear()
                        break
                    eoi = buf.find(b'\xff\xd9', soi + 2)
                    if eoi < 0:
                        if soi > 0:
                            del buf[:soi]
                        break
                    jpeg = bytes(buf[soi:eoi + 2])
                    del buf[:eoi + 2]
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' +
                           jpeg + b'\r\n')
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)  # always reap to avoid holding /dev/video1
            if _active_preview.get('proc') is proc:
                _active_preview['proc'] = None

    return Response(generate(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


# ---------------------------------------------------------------------------
# Calibration API
# ---------------------------------------------------------------------------

_calibration_lock = threading.Lock()
_active_calibration = {'proc': None, 'type': None}


@app.route('/api/calibration/intrinsics/start', methods=['POST'])
def api_calibrate_intrinsics_start():
    """Start intrinsic camera calibration."""
    with _calibration_lock:
        if _active_calibration['proc'] is not None:
            return jsonify({'error': 'Calibration already running'}), 409
        if _is_recording():
            return jsonify({'error': 'Cannot calibrate during recording'}), 409

        script = os.path.join(SCRIPT_DIR, 'calibrate_camera.py')
        if not os.path.isfile(script):
            return jsonify({'error': 'calibrate_camera.py not found'}), 404

        proc = subprocess.Popen(
            ['python3', script, '--headless',
             '--device', CAMERA_DEVICE,
             '--width', str(CAMERA_WIDTH),
             '--height', str(CAMERA_HEIGHT),
             '--output', INTRINSICS_FILE],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            preexec_fn=os.setsid
        )
        _active_calibration['proc'] = proc
        _active_calibration['type'] = 'intrinsics'

    return jsonify({'status': 'started'})


@app.route('/api/calibration/intrinsics/status')
def api_calibrate_intrinsics_status():
    """Get intrinsic calibration progress."""
    with _calibration_lock:
        proc = _active_calibration['proc']
        if proc is None or _active_calibration['type'] != 'intrinsics':
            done = os.path.isfile(INTRINSICS_FILE)
            return jsonify({'running': False, 'calibrated': done})

        if proc.poll() is not None:
            _active_calibration['proc'] = None
            _active_calibration['type'] = None
            done = os.path.isfile(INTRINSICS_FILE)
            return jsonify({'running': False, 'calibrated': done})

    # Try to read last progress line from stdout (outside lock — IO)
    frames = None
    target = None
    try:
        import select
        while select.select([proc.stdout], [], [], 0)[0]:
            line = proc.stdout.readline()
            if line:
                info = json.loads(line.decode().strip())
                frames = info.get('frames')
                target = info.get('target')
    except Exception:
        pass

    return jsonify({'running': True, 'frames': frames, 'target': target})


@app.route('/api/calibration/intrinsics/stop', methods=['POST'])
def api_calibrate_intrinsics_stop():
    """Stop intrinsic calibration."""
    with _calibration_lock:
        proc = _active_calibration['proc']
        if proc and _active_calibration['type'] == 'intrinsics':
            _kill_process_group(proc)
            _active_calibration['proc'] = None
            _active_calibration['type'] = None
    return jsonify({'status': 'stopped'})


@app.route('/api/calibration/extrinsics/start', methods=['POST'])
def api_calibrate_extrinsics_start():
    """Start automatic extrinsic calibration (headless)."""
    with _calibration_lock:
        if _active_calibration['proc'] is not None:
            return jsonify({'error': 'Calibration already running'}), 409
        if _is_recording():
            return jsonify({'error': 'Cannot calibrate during recording'}), 409
        if not os.path.isfile(INTRINSICS_FILE):
            return jsonify({'error': 'Intrinsics must be calibrated first'}), 400

        script = os.path.join(SCRIPT_DIR, 'calibrate_extrinsics.py')
        if not os.path.isfile(script):
            return jsonify({'error': 'calibrate_extrinsics.py not found'}), 404

        proc = subprocess.Popen(
            ['python3', script, '--headless', '--device', CAMERA_DEVICE],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            preexec_fn=os.setsid
        )
        _active_calibration['proc'] = proc
        _active_calibration['type'] = 'extrinsics'

    return jsonify({'status': 'started'})


@app.route('/api/calibration/extrinsics')
def api_get_extrinsics():
    """Return current extrinsic calibration."""
    if not os.path.isfile(EXTRINSICS_FILE):
        return jsonify({'calibrated': False})
    with open(EXTRINSICS_FILE) as f:
        data = yaml.safe_load(f)
    return jsonify({'calibrated': True, 'data': data})


@app.route('/api/calibration/extrinsics', methods=['POST'])
def api_set_extrinsics():
    """Set extrinsic calibration from manual JSON input."""
    data = request.get_json(force=True, silent=True)
    if not data or 'translation' not in data:
        return jsonify({'error': 'Need translation field'}), 400

    os.makedirs(CALIBRATION_DIR, exist_ok=True)

    # If RPY provided, convert to quaternion
    rotation = data.get('rotation', {'x': 0, 'y': 0, 'z': 0, 'w': 1})
    rpy = data.get('rpy_degrees')
    if rpy and 'roll' in rpy:
        from scipy.spatial.transform import Rotation
        q = Rotation.from_euler('xyz',
            [rpy['roll'], rpy['pitch'], rpy['yaw']], degrees=True).as_quat()
        rotation = {'x': float(q[0]), 'y': float(q[1]),
                     'z': float(q[2]), 'w': float(q[3])}

    ext = {
        'parent_frame': 'unilidar_lidar',
        'child_frame': 'camera_optical_frame',
        'translation': data['translation'],
        'rotation': rotation,
        'calibration_date': datetime.now().isoformat(),
        'method': 'manual',
    }
    if rpy:
        ext['rpy_degrees'] = rpy

    with open(EXTRINSICS_FILE, 'w') as f:
        yaml.dump(ext, f, default_flow_style=False)

    return jsonify({'status': 'saved', 'data': ext})


@app.route('/api/session/<session_id>', methods=['DELETE'])
def api_delete_session(session_id):
    """Delete a recording session and its files.

    If the session is mid-processing, fire the cancel Event and wait up to 5 s
    for the worker thread to clear `_active_processing`. The worker is
    responsible for telling Modal to free its container (via _modal_cancel).
    Local files are then removed unconditionally.
    """
    session = _get_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    # Don't delete active recording.
    if _active_recording['session_id'] == session_id:
        return jsonify({'error': 'Cannot delete active recording'}), 409

    # If a processing job is active, ask it to cancel and wait briefly.
    with _processing_lock:
        entry = _active_processing.get(session_id)
        if entry is not None:
            ev = entry.get('cancel')
            if ev is not None:
                ev.set()
    if entry is not None:
        deadline = time.time() + 5.0
        while time.time() < deadline:
            with _processing_lock:
                if session_id not in _active_processing:
                    break
            time.sleep(0.1)

    # Remove raw recording files.
    session_dir = os.path.join(RECORDINGS_DIR, session['name'])
    if os.path.isdir(session_dir):
        shutil.rmtree(session_dir, ignore_errors=True)
    # Remove processed results.
    processed_dir = _processed_dir_for_session(session['name'])
    if os.path.isdir(processed_dir):
        shutil.rmtree(processed_dir, ignore_errors=True)

    _delete_session(session_id)
    return jsonify({'deleted': session_id})


@app.route('/api/events')
def api_events():
    """SSE endpoint for live status updates."""
    def stream():
        while True:
            recording = _is_recording()
            elapsed = 0
            status = 'idle'
            session_id = None
            # `in_flight` covers the pre-bag window: the recording thread is
            # alive (driver launching / waiting for LiDAR topics) but
            # `_is_recording()` is still False because the bag subprocess
            # hasn't spawned. Without this the SPA's Stop button never
            # appears for 30-50 s after Start. Mirrors `_is_busy()`.
            in_flight = bool(_active_recording.get('starting')) or recording

            if recording and _active_recording['start_time']:
                elapsed = round(time.time() - _active_recording['start_time'], 1)
                session_id = _active_recording['session_id']
                session = _get_session(session_id) if session_id else None
                status = session.get('status', 'recording') if session else 'recording'
            elif in_flight:
                # Pre-bag window: surface the in-flight session's current
                # status (`launching_driver` / `waiting_for_topics` / `starting`)
                # so the SPA can render the Stop button + intermediate stage
                # rather than sitting on `idle`.
                session_id = _active_recording.get('session_id')
                session = _get_session(session_id) if session_id else None
                status = (session.get('status', 'starting') if session
                          else 'starting')

            # Collect processing status for active SLAM jobs.
            # Snapshot under lock so we don't race with _process_thread.
            processing = {}
            with _processing_lock:
                snapshot = {sid: dict(proc) for sid, proc in _active_processing.items()}
            for sid, proc in snapshot.items():
                stage = proc.get('stage') or proc.get('status') or ''
                # Step 5 / plan §22b: surface `run_id` (already populated
                # by the scoped worker) and `error` (sourced from the
                # session record's `slam_error`, which is the canonical
                # write-side per `_set_session_slam_error`). Both are
                # additive — the off-limits status-mutation helpers
                # (`_set_active_stage`, `_set_session_slam_error`) are
                # unchanged.
                err_msg = None
                sess_for_err = _get_session(sid)
                if isinstance(sess_for_err, dict):
                    err_msg = sess_for_err.get('slam_error')
                processing[sid] = {
                    'status': proc.get('status'),
                    'stage': stage,
                    # `progress` is kept as a human string for back-compat
                    # with old browser tabs that read procInfo.progress.
                    'progress': stage,
                    'job_id': proc.get('job_id'),
                    'elapsed': round(time.time() - proc.get('start_time', time.time()), 1),
                    'run_id': proc.get('run_id'),
                    'error': err_msg,
                }

            data = json.dumps({
                'recording': recording,
                'in_flight': in_flight,
                'elapsed': elapsed,
                'status': status,
                'session_id': session_id,
                'processing': processing,
            })
            yield f'data: {data}\n\n'
            time.sleep(1)

    return Response(stream(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ---------------------------------------------------------------------------
# SLAM via Modal cloud-slam-icp (async API)
# ---------------------------------------------------------------------------
#
# Pipeline:
#   1. zstd-compress the .mcap to scan.mcap.zst (next to the source).
#   2. POST /jobs?filename=scan.mcap.zst with X-API-Key + X-Idempotency-Key.
#   3. Poll GET /jobs/{id} until status is `done` or `failed`.
#   4. On done: pull result.json + layout_merged.txt + best_views/<idx>.jpg
#      into PROCESSED_DIR/<session_name>/. PLY artifacts stay remote and are
#      surfaced via the Flask proxy at /api/session/<id>/artifact/<name>.
#
# Cancellation is implemented with a threading.Event stored on each
# `_active_processing[sid]` entry, so the poller's `event.wait(retry_after_s)`
# wakes immediately when the user hits Delete on a running session.

# Network-error retry counts.
_MODAL_SUBMIT_RETRIES = 3            # 1, 2, 4 s exponential backoff between tries.
_MODAL_POLL_MAX_FAILURES = 5         # Consecutive poll failures before aborting.

# Whitelist of artifacts the proxy will stream from Modal. A request for
# anything else returns 404; this keeps user-controlled paths from
# wandering the Modal volume.
_ARTIFACT_WHITELIST = frozenset({
    'colored_map.ply',
    'scene_with_boxes.ply',
    'result.json',
    'layout_merged.txt',
})


def _find_mcap(session):
    """Find the MCAP file for a session."""
    bag_dir = os.path.join(RECORDINGS_DIR, session['name'], 'rosbag')
    if not os.path.isdir(bag_dir):
        return None
    for f in os.listdir(bag_dir):
        if f.endswith('.mcap'):
            return os.path.join(bag_dir, f)
    return None


def _processed_dir_for_session(session_name):
    """Return the processed/<name>/ directory, with a mid-run mount fallback.

    If the boot-time PROCESSED_DIR was on `/mnt/slam_data` but the flash drive
    has since unmounted, we fall back to the in-repo `processed/` directory
    so a long Modal job doesn't lose its output.
    """
    if PROCESSED_DIR.startswith('/mnt/slam_data') and not os.path.ismount('/mnt/slam_data'):
        fallback = os.path.join(SCRIPT_DIR, 'processed', session_name)
        return fallback
    return os.path.join(PROCESSED_DIR, session_name)


def _zstd_compress(src_mcap, dst_path):
    """Compress `src_mcap` to `dst_path` with `zstd -1 -q`.

    Returns the destination path on success. Raises RuntimeError on a
    non-zero exit (with a stderr tail in the message).
    """
    if not _ZSTD_BIN:
        raise RuntimeError('zstd not installed')
    res = subprocess.run(
        [_ZSTD_BIN, '-1', '-q', '-f', '-o', dst_path, src_mcap],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        tail = (res.stderr or '').strip()[-300:]
        raise RuntimeError(f'compression failed: {tail}')
    return dst_path


def _modal_submit(zst_path, idem_key):
    """POST `zst_path` to /jobs with X-API-Key and X-Idempotency-Key.

    Returns the new job_id. Retries up to _MODAL_SUBMIT_RETRIES times on
    HTTP 5xx / network failure with exponential backoff (1, 2, 4 s).
    Reuses the same idempotency key across retries so we don't double-submit.
    """
    url = f'{MODAL_API_URL}/jobs?filename=scan.mcap.zst'
    last_err = None
    for attempt in range(_MODAL_SUBMIT_RETRIES):
        if attempt > 0:
            time.sleep(2 ** (attempt - 1))   # 1, 2, 4
        try:
            res = subprocess.run(
                ['curl', '-sS', '-X', 'POST',
                 '-H', f'X-API-Key: {MODAL_API_KEY}',
                 '-H', f'X-Idempotency-Key: {idem_key}',
                 '-H', 'Content-Type: application/zstd',
                 '--data-binary', f'@{zst_path}',
                 '-w', '\n%{http_code}',
                 url],
                capture_output=True, text=True, timeout=900,
            )
        except subprocess.TimeoutExpired as e:
            last_err = f'submit timeout: {e}'
            continue
        if res.returncode != 0:
            last_err = f'curl rc={res.returncode}: {(res.stderr or "")[:200]}'
            continue
        body, _, code = (res.stdout or '').rpartition('\n')
        try:
            http_code = int(code.strip())
        except ValueError:
            last_err = f'unparsable status line: {res.stdout[:200]!r}'
            continue
        if 500 <= http_code < 600:
            last_err = f'HTTP {http_code}: {body[:200]}'
            continue
        if http_code >= 400:
            raise RuntimeError(f'submit HTTP {http_code}: {body[:200]}')
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(f'submit returned non-JSON: {body[:200]!r}')
        job_id = payload.get('job_id') or payload.get('id')
        if not job_id:
            raise RuntimeError(f'submit response missing job_id: {body[:200]}')
        return job_id
    raise RuntimeError(f'submit failed after {_MODAL_SUBMIT_RETRIES} tries: {last_err}')


def _modal_poll(job_id):
    """GET /jobs/{job_id}. Returns (envelope_dict, retry_after_seconds).

    `retry_after_seconds` is parsed from the `Retry-After` response header and
    falls back to POLL_INTERVAL_S when absent. Raises on non-2xx / curl error.
    """
    url = f'{MODAL_API_URL}/jobs/{job_id}'
    res = subprocess.run(
        ['curl', '-sS', '-D', '-',
         '-H', f'X-API-Key: {MODAL_API_KEY}',
         url],
        capture_output=True, text=True, timeout=60,
    )
    if res.returncode != 0:
        raise RuntimeError(f'poll curl rc={res.returncode}: {(res.stderr or "")[:200]}')

    raw = res.stdout or ''
    # Split header block from body. With `-D -` headers come before body
    # separated by a blank line. Curl may emit multiple header blocks for
    # redirects; take the last one.
    head, _, body = raw.rpartition('\r\n\r\n')
    if not head:
        head, _, body = raw.rpartition('\n\n')
    status_code = None
    retry_after = POLL_INTERVAL_S
    for line in head.splitlines():
        s = line.strip()
        if s.upper().startswith('HTTP/'):
            parts = s.split()
            if len(parts) >= 2 and parts[1].isdigit():
                status_code = int(parts[1])
        elif s.lower().startswith('retry-after:'):
            v = s.split(':', 1)[1].strip()
            try:
                retry_after = max(1, int(float(v)))
            except (ValueError, TypeError):
                pass
    if status_code is None:
        raise RuntimeError(f'poll: could not parse HTTP status from {raw[:200]!r}')
    if status_code >= 500:
        raise RuntimeError(f'poll HTTP {status_code}')
    if status_code >= 400:
        raise RuntimeError(f'poll HTTP {status_code}: {body[:200]}')
    try:
        envelope = json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f'poll body not JSON: {e}; body={body[:200]!r}')
    return envelope, retry_after


def _modal_cancel(job_id):
    """Best-effort DELETE /jobs/{job_id}. Errors are logged, never raised."""
    if not job_id:
        return
    try:
        subprocess.run(
            ['curl', '-sS', '-X', 'DELETE',
             '-H', f'X-API-Key: {MODAL_API_KEY}',
             f'{MODAL_API_URL}/jobs/{job_id}'],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        _log(f'_modal_cancel({job_id}) failed: {e}')


def _modal_fetch(job_id, path, dest):
    """GET an artifact or image to `dest` via curl. `path` is e.g. `image/3`
    or `artifact/result.json`. Always sends X-API-Key.
    """
    url = f'{MODAL_API_URL}/jobs/{job_id}/{path}'
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    res = subprocess.run(
        ['curl', '-sSf', '-L',
         '-H', f'X-API-Key: {MODAL_API_KEY}',
         '-o', dest, url],
        capture_output=True, text=True, timeout=300,
    )
    if res.returncode != 0:
        raise IOError(f'fetch {path} failed: rc={res.returncode}: {(res.stderr or "")[:200]}')
    if not os.path.isfile(dest) or os.path.getsize(dest) == 0:
        raise IOError(f'fetch {path} produced empty file at {dest}')
    return dest


# ---------------------------------------------------------------------------

def _process_thread(session_id, mcap_path):
    """Background worker for a Modal SLAM job.

    Lifecycle (all `_active_processing[session_id]` mutations under
    `_processing_lock`):
      1. Pre-submit cancel check.
      2. zstd-compress the .mcap to scan.mcap.zst (next to the source).
      3. Generate fresh idempotency key, persist on session.
      4. Submit; record job_id on both `_active_processing[sid]` and the
         session record.
      5. Poll loop. `cancel.is_set()` mid-loop → DELETE /jobs/{id} and bail.
      6. On `done`: download result.json → layout_merged.txt → best_views/.
      7. `finally`: remove the .zst, pop the active-processing entry, clear
         `idem_key` on the session.
    """
    sess0 = _get_session(session_id) or {}
    proc_dir = _processed_dir_for_session(sess0.get('name', session_id))
    os.makedirs(proc_dir, exist_ok=True)
    zst_path = os.path.join(proc_dir, 'scan.mcap.zst')
    job_id = None
    try:
        # Step 1: Pre-submit cancel check.
        with _processing_lock:
            entry = _active_processing.get(session_id)
            if entry is None:
                return
            cancel_event = entry['cancel']

        if cancel_event.is_set():
            _set_session_slam_status(session_id, 'cancelled')
            return

        # Step 2: Compress.
        _set_active_stage(session_id, 'compressing')
        _set_session_slam_status(session_id, 'compressing')
        try:
            _zstd_compress(mcap_path, zst_path)
        except Exception as e:
            _set_session_slam_error(session_id, str(e))
            return

        if cancel_event.is_set():
            _set_session_slam_status(session_id, 'cancelled')
            return

        # Step 3: Submit with a fresh idempotency key.
        idem_key = uuid.uuid4().hex
        sess = _get_session(session_id) or {}
        sess['idem_key'] = idem_key
        # Wipe stale slam fields from a previous run.
        sess.pop('slam_error', None)
        sess.pop('slam_result', None)
        sess['slam_status'] = 'uploading'
        sess['slam_stage'] = 'uploading'
        _put_session(session_id, sess)

        _set_active_stage(session_id, 'uploading')

        try:
            job_id = _modal_submit(zst_path, idem_key)
        except Exception as e:
            _set_session_slam_error(session_id, f'submit: {e}')
            return

        with _processing_lock:
            entry = _active_processing.get(session_id)
            if entry is not None:
                entry['job_id'] = job_id
                entry['stage'] = 'queued'
        sess = _get_session(session_id) or {}
        sess['job_id'] = job_id
        sess['slam_status'] = 'queued'
        sess['slam_stage'] = 'queued'
        sess['submitted_at'] = datetime.utcnow().isoformat() + 'Z'
        _put_session(session_id, sess)

        # Step 4-5: Poll loop.
        consecutive_failures = 0
        retry_after = POLL_INTERVAL_S
        while True:
            # `event.wait` returns True the moment cancel is fired.
            if cancel_event.wait(retry_after):
                _modal_cancel(job_id)
                _set_session_slam_status(session_id, 'cancelled')
                return

            try:
                envelope, retry_after = _modal_poll(job_id)
                consecutive_failures = 0
            except Exception as e:
                consecutive_failures += 1
                _log(f'_modal_poll failed ({consecutive_failures}/{_MODAL_POLL_MAX_FAILURES}): {e}')
                if consecutive_failures >= _MODAL_POLL_MAX_FAILURES:
                    _set_session_slam_error(
                        session_id, f'poll failed after {consecutive_failures} tries: {e}')
                    return
                retry_after = max(retry_after, POLL_INTERVAL_S)
                continue

            stage = envelope.get('status', 'unknown')

            # Mirror stage so SSE shows e.g. `stage_5_infer`.
            _set_active_stage(session_id, stage)
            sess = _get_session(session_id) or {}
            sess['slam_status'] = stage
            sess['slam_stage'] = stage
            _put_session(session_id, sess)

            if stage == 'failed':
                err = envelope.get('error') or {}
                err_stage = err.get('stage') or 'unknown'
                err_tail = err.get('stderr_tail') or err.get('message') or ''
                _set_session_slam_error(
                    session_id, f'{err_stage}: {err_tail}')
                return

            if stage == 'done':
                _save_done_artifacts(session_id, job_id, envelope)
                return

            # Otherwise it's an in-progress stage; keep polling.
            continue

    except Exception as e:
        _log(f'_process_thread crashed for {session_id}: {e}')
        _set_session_slam_error(session_id, f'internal: {e}')
    finally:
        # Always clean up the .zst and the active-processing entry.
        try:
            if os.path.isfile(zst_path):
                os.remove(zst_path)
        except OSError:
            pass
        with _processing_lock:
            _active_processing.pop(session_id, None)
        sess = _get_session(session_id)
        if sess and 'idem_key' in sess:
            sess.pop('idem_key', None)
            _put_session(session_id, sess)


def _set_active_stage(session_id, stage):
    """Mirror a status string into `_active_processing[sid]['stage']`."""
    with _processing_lock:
        entry = _active_processing.get(session_id)
        if entry is not None:
            entry['stage'] = stage


def _set_session_slam_status(session_id, status):
    """Persist `slam_status` on the session record."""
    sess = _get_session(session_id)
    if not sess:
        return
    sess['slam_status'] = status
    sess['slam_stage'] = status
    _put_session(session_id, sess)


def _set_session_slam_error(session_id, message):
    """Persist a SLAM error message on the session record."""
    sess = _get_session(session_id)
    if not sess:
        return
    sess['slam_status'] = 'error'
    sess['slam_stage'] = 'error'
    sess['slam_error'] = message
    _put_session(session_id, sess)


def _save_done_artifacts(session_id, job_id, envelope):
    """Persist a `done` envelope locally.

    Order matters: `result.json` first (the floorplan SVG render needs only
    this), then `layout_merged.txt`, then per-bbox best_views. Per-image
    IOError is logged and skipped so a partial gallery still ships when the
    disk fills up. The best_views directory is wiped first to avoid stale
    files leaking into a re-process.
    """
    sess = _get_session(session_id) or {}
    name = sess.get('name', session_id)
    proc_dir = _processed_dir_for_session(name)
    os.makedirs(proc_dir, exist_ok=True)

    # 1. result.json (most useful — floorplan/furniture geometry).
    result_path = os.path.join(proc_dir, 'result.json')
    try:
        with open(result_path, 'w') as f:
            json.dump(envelope, f, indent=2)
    except IOError as e:
        _log(f'failed to write result.json for {session_id}: {e}')

    # 2. layout_merged.txt.
    layout_path = os.path.join(proc_dir, 'layout_merged.txt')
    try:
        _modal_fetch(job_id, 'artifact/layout_merged.txt', layout_path)
    except IOError as e:
        _log(f'failed to fetch layout_merged.txt for {session_id}: {e}')

    # 3. best_views — wipe first to avoid leak from a previous run.
    bv_dir = os.path.join(proc_dir, 'best_views')
    shutil.rmtree(bv_dir, ignore_errors=True)
    os.makedirs(bv_dir, exist_ok=True)
    best_images = envelope.get('best_images') or []
    for idx, _bbox in enumerate(best_images):
        dest = os.path.join(bv_dir, f'{idx}.jpg')
        try:
            _modal_fetch(job_id, f'image/{idx}', dest)
        except IOError as e:
            _log(f'failed to fetch image {idx} for {session_id}: {e}')

    # Persist completion on the session.
    sess = _get_session(session_id) or {}
    sess['status'] = 'processed'
    sess['slam_status'] = 'done'
    sess['slam_stage'] = 'done'
    sess['slam_result'] = envelope
    sess.pop('slam_error', None)
    _put_session(session_id, sess)


@app.route('/api/session/<session_id>/process', methods=['POST'])
def api_process(session_id):
    """Submit the session's MCAP to Modal for SLAM + layout + best-views."""
    if not MODAL_API_KEY:
        return jsonify({'error': 'MODAL_API not configured on server'}), 503
    if not _ZSTD_BIN:
        return jsonify({'error': 'zstd not installed on server'}), 503

    session = _get_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    mcap_path = _find_mcap(session)
    if not mcap_path:
        return jsonify({'error': 'No MCAP file found for session'}), 404

    # Insert the active-processing entry under lock BEFORE spawning the
    # thread (matches the existing recording-start pattern). This way two
    # concurrent /process requests can't both succeed in racing the worker.
    with _processing_lock:
        if session_id in _active_processing:
            return jsonify({'error': 'Already processing'}), 409
        _active_processing[session_id] = {
            'status': 'processing',
            'stage': 'compressing',
            'start_time': time.time(),
            'cancel': threading.Event(),
            'job_id': None,
        }

    # Mark the session record up front so the UI flips immediately.
    session['status'] = 'processing'
    session['slam_status'] = 'compressing'
    session['slam_stage'] = 'compressing'
    session.pop('slam_error', None)
    _put_session(session_id, session)

    thread = threading.Thread(
        target=_process_thread,
        args=(session_id, mcap_path),
        daemon=True,
    )
    thread.start()

    return jsonify({
        'session_id': session_id,
        'status': 'started',
        'job_id_pending': True,
    })


@app.route('/api/session/<session_id>/result')
def api_result(session_id):
    """Return a unified result envelope for the session.

    Shape: `{status, stage, elapsed, error?, result?}`. `result` (the full
    Modal envelope) is populated only when slam_status == 'done'.
    """
    session = _get_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    # Active job: read from the in-memory entry so we don't miss the most
    # recent stage transition that hasn't been persisted yet.
    with _processing_lock:
        entry = _active_processing.get(session_id)
        if entry is not None:
            elapsed = round(time.time() - entry.get('start_time', time.time()), 1)
            return jsonify({
                'status': 'processing',
                'stage': entry.get('stage') or 'starting',
                'elapsed': elapsed,
                'job_id': entry.get('job_id'),
            })

    slam_status = session.get('slam_status')
    elapsed = session.get('duration')

    if slam_status == 'done':
        # Prefer the on-disk result.json (re-process refreshes it).
        envelope = None
        proc_path = os.path.join(_processed_dir_for_session(session['name']),
                                 'result.json')
        if os.path.isfile(proc_path):
            try:
                with open(proc_path, 'r') as f:
                    envelope = json.load(f)
            except (OSError, json.JSONDecodeError):
                envelope = None
        if envelope is None:
            envelope = session.get('slam_result')
        return jsonify({
            'status': 'done',
            'stage': 'done',
            'elapsed': elapsed,
            'result': envelope,
        })

    if slam_status == 'error':
        return jsonify({
            'status': 'error',
            'stage': 'error',
            'elapsed': elapsed,
            'error': session.get('slam_error', 'unknown'),
        })

    if slam_status == 'cancelled':
        return jsonify({
            'status': 'cancelled',
            'stage': 'cancelled',
            'elapsed': elapsed,
        })

    return jsonify({
        'status': slam_status or 'not_processed',
        'stage': session.get('slam_stage') or slam_status,
        'elapsed': elapsed,
    })


@app.route('/api/session/<session_id>/best_view/<int:idx>.jpg')
def api_best_view(session_id, idx):
    """Serve `processed/<name>/best_views/<idx>.jpg` from local disk."""
    session = _get_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    path = os.path.join(_processed_dir_for_session(session['name']),
                        'best_views', f'{idx}.jpg')
    if not os.path.isfile(path):
        return jsonify({'error': 'Image not found'}), 404
    return send_file(path, mimetype='image/jpeg')


@app.route('/api/session/<session_id>/layout.txt')
def api_layout_txt(session_id):
    """Serve `processed/<name>/layout_merged.txt` from local disk."""
    session = _get_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    path = os.path.join(_processed_dir_for_session(session['name']),
                        'layout_merged.txt')
    if not os.path.isfile(path):
        return jsonify({'error': 'Layout not found'}), 404
    return send_file(path, mimetype='text/plain')


@app.route('/api/session/<session_id>/artifact/<name>')
def api_artifact_proxy(session_id, name):
    """Server-side proxy that streams a Modal artifact with the API key.

    Browser links target this Flask route rather than Modal directly,
    because Modal would 401 on bare-browser GETs (no X-API-Key header).
    Only whitelisted artifact names are accepted.
    """
    if name not in _ARTIFACT_WHITELIST:
        return jsonify({'error': 'Unknown artifact'}), 404

    session = _get_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    job_id = session.get('job_id')
    if not job_id:
        return jsonify({'error': 'No job_id for this session'}), 404
    if not MODAL_API_KEY:
        return jsonify({'error': 'MODAL_API not configured on server'}), 503

    url = f'{MODAL_API_URL}/jobs/{job_id}/artifact/{name}'
    proc = subprocess.Popen(
        ['curl', '-sSf', '-L', '--max-time', '600',
         '-H', f'X-API-Key: {MODAL_API_KEY}', url],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )

    mime = 'application/octet-stream'
    if name.endswith('.json'):
        mime = 'application/json'
    elif name.endswith('.txt'):
        mime = 'text/plain'
    elif name.endswith('.ply'):
        mime = 'application/octet-stream'

    @stream_with_context
    def gen():
        try:
            while True:
                chunk = proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                proc.kill()

    return Response(gen(), mimetype=mime,
                    headers={'Content-Disposition':
                             f'inline; filename="{name}"'})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _cleanup_on_exit():
    """Kill any running driver/bag/camera processes on app shutdown."""
    _kill_process_group(_active_recording.get('bag_proc'))
    _kill_process_group(_active_recording.get('driver_proc'))
    _kill_process_group(_active_recording.get('camera_proc'), timeout=10)
    _kill_process_group(_active_recording.get('tf_proc'))
    _kill_process_group(_active_recording.get('camera_monitor_proc'))
    if _active_calibration.get('proc'):
        _kill_process_group(_active_calibration['proc'])


def _recover_stuck_sessions():
    """Reset sessions stuck in intermediate states from an unclean shutdown.

    Recording-state recovery (existing): `status` field.
    SLAM recovery (new, Modal): `slam_status` field — anything in the
    pre-`done` lifecycle is considered stuck. Each recovered session with a
    persisted `job_id` triggers a best-effort `DELETE /jobs/{job_id}` so a
    long-running H100 container is freed instead of burning credits.
    """
    stuck_recording = {'processing', 'launching_driver', 'waiting_for_topics',
                       'starting', 'recording'}
    # 'cancelled' is a terminal state (peer of 'done' / 'error') written
    # deliberately after a clean user cancel — never sweep it on boot.
    stuck_slam_exact = {'compressing', 'uploading', 'queued', 'decoding'}
    sessions = _get_sessions()
    for sid, s in sessions.items():
        changed = False

        if s.get('status') in stuck_recording:
            s['status'] = 'stopped'
            changed = True

        slam_status = s.get('slam_status')
        is_stuck_slam = (
            slam_status in stuck_slam_exact
            or (isinstance(slam_status, str) and slam_status.startswith('stage_'))
        )
        if is_stuck_slam:
            # Best-effort tell Modal to drop the container.
            job_id = s.get('job_id')
            if job_id and MODAL_API_KEY:
                try:
                    _modal_cancel(job_id)
                except Exception as e:
                    _log(f'recovery: _modal_cancel({job_id}) failed: {e}')
            s['slam_status'] = 'error'
            s['slam_stage'] = 'error'
            s['slam_error'] = 'Interrupted by shutdown'
            changed = True

        if changed:
            _put_session(sid, s)
            print(f'Recovered stuck session: {s["name"]}')


# ===========================================================================
# === Athathi proxy routes ==================================================
# ===========================================================================
#
# Pi-side Flask wrappers around the upstream Athathi backend. Every route
# delegates the actual HTTP forwarding to `athathi_proxy.<fn>` (which uses
# `curl` for parity with `_modal_*`) and translates the upstream response
# into something the Pi's frontend can consume:
#
#   - 200 with JSON on success.
#   - 401 + `auth.clear_token()` on upstream 401 (so the SPA redirects to login).
#   - 502 with `{error, upstream_status, upstream_body_tail}` on upstream 5xx
#     that survived retries.
#   - 503 with `{error: 'no network', detail: ...}` when curl failed; if a
#     fresh-enough cache exists, we serve it with `X-Cached: true` instead.
#
# Auth posture:
#   - `/api/auth/login` is the only route that accepts unauthenticated input.
#   - `/api/auth/me` is a pure-local read (cached `auth.json` + JWT-exp check);
#     no upstream call.
#   - Every other route requires `auth.is_logged_in()` and returns 401 if not.
#
# The token NEVER leaves the Pi's filesystem — successful login writes it via
# `auth.write_token()`; subsequent calls read it back. The frontend never sees
# the JWT, only the user envelope (id, username, role).
# ===========================================================================

def _athathi_token_or_401():
    """Return the on-disk token if logged in, else a Flask (response, code) tuple.

    Helper for routes that gate on `auth.is_logged_in()`. Use as:
        tok = _athathi_token_or_401()
        if not isinstance(tok, str):
            return tok    # already-built 401 response
    """
    if not auth.is_logged_in():
        return jsonify({'error': 'not logged in'}), 401
    return auth.read_token()


def _athathi_handle_error(e, *, cache_key=None):
    """Translate an `AthathiError` into a (response, status, headers) triple.

    Returns a Flask response object directly so the route can `return` it.

    - 401 → clears token, returns 401 to caller.
    - 5xx (after retries exhausted) → 502 to caller.
    - status_code == 0 (network failure) → if `cache_key` is set and a
      cached blob is on disk, serve it with `X-Cached: true`. Otherwise 503.
    """
    status = e.status_code
    body_tail = (e.body or '')[:200]

    if status == 401:
        auth.clear_token()
        return jsonify({'error': 'unauthorized'}), 401

    if status == 0:
        # Network failure — try to serve a stale cache if one exists.
        if cache_key:
            path = athathi_proxy._cache_path(cache_key)
            ts, blob = athathi_proxy._read_cache(path)
            if blob is not None:
                resp = jsonify(blob)
                resp.headers['X-Cached'] = 'true'
                resp.headers['X-Stale-Reason'] = 'network'
                return resp, 200
        return jsonify({
            'error': 'no network',
            'detail': body_tail,
        }), 503

    if 500 <= status < 600:
        return jsonify({
            'error': 'upstream error',
            'upstream_status': status,
            'upstream_body_tail': body_tail,
        }), 502

    # Other 4xx — pass through verbatim.
    return jsonify({
        'error': str(e),
        'upstream_status': status,
        'upstream_body_tail': body_tail,
    }), status if status >= 400 else 502


def _exp_iso_for(token):
    """Best-effort ISO-8601 timestamp of the JWT `exp` claim, or None."""
    if not token:
        return None
    payload = auth.decode_jwt_payload(token) or {}
    exp = payload.get('exp')
    if not isinstance(exp, (int, float)):
        return None
    try:
        return datetime.utcfromtimestamp(exp).isoformat() + 'Z'
    except (OSError, OverflowError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.route('/api/auth/login', methods=['POST'])
def api_auth_login():
    """Accept {username, password}, forward to Athathi, persist token locally.

    On success:
      - Writes the JWT to `<ATHATHI_DIR>/token` (chmod 600).
      - Writes the user envelope (id, username, user_type, exp_iso) to auth.json.
      - Updates `config.last_user` so the login screen can pre-fill it.
      - Returns `{user: {...}}` to the frontend (the JWT is NOT echoed back).
    """
    data = request.get_json(force=True, silent=True) or {}
    username = data.get('username') or ''
    password = data.get('password') or ''
    if not isinstance(username, str) or not username.strip():
        return jsonify({'error': 'username required'}), 400
    if not isinstance(password, str) or not password:
        return jsonify({'error': 'password required'}), 400

    try:
        body = athathi_proxy.login(username.strip(), password)
    except athathi_proxy.AthathiError as e:
        return _athathi_handle_error(e)

    token = body.get('token') if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        return jsonify({
            'error': 'upstream login response missing token',
            'upstream_body_tail': json.dumps(body)[:200] if body else '',
        }), 502

    # Persist locally. write_token will fail if token is empty; we already checked.
    try:
        auth.write_token(token)
    except (OSError, ValueError) as e:
        return jsonify({'error': f'failed to persist token: {e!s}'}), 500

    user_obj = body.get('user') if isinstance(body, dict) else None
    user_envelope = {}
    if isinstance(user_obj, dict):
        # Best-effort copy of the named fields from §3b.
        for k in ('id', 'user_id', 'username', 'user_type', 'role', 'email'):
            if k in user_obj:
                user_envelope[k] = user_obj[k]
    else:
        user_envelope = {'username': username.strip()}
    # Normalise: prefer `user_id` over raw `id`.
    if 'user_id' not in user_envelope and 'id' in user_envelope:
        user_envelope['user_id'] = user_envelope.pop('id')
    exp_iso = _exp_iso_for(token)
    if exp_iso:
        user_envelope['exp_iso'] = exp_iso

    try:
        auth.write_auth(user_envelope)
    except (OSError, TypeError) as e:
        _log(f'api_auth_login: write_auth failed: {e!r}')

    try:
        auth.update_config(last_user=user_envelope.get('username') or username.strip())
    except (OSError, ValueError) as e:
        _log(f'api_auth_login: update_config failed: {e!r}')

    # Return the user envelope only — DO NOT echo the token to the browser.
    return jsonify({'user': user_envelope}), 200


@app.route('/api/auth/logout', methods=['POST'])
def api_auth_logout():
    """Best-effort upstream logout; always clears the local token + auth.json."""
    token = auth.read_token()
    # BE-6: when there's no local token, there's nothing for the upstream
    # to invalidate either. Skip the upstream call so we don't waste a
    # network round-trip (and don't risk a transient 401 in the logs).
    if token is None:
        auth.clear_token()
        return jsonify({'ok': True}), 200
    # Upstream call is best-effort; never blocks the local clear.
    try:
        athathi_proxy.logout(token)
    except Exception as e:
        # athathi_proxy.logout() already swallows AthathiError; this is belt-and-braces.
        _log(f'api_auth_logout: upstream logout failed: {e!r}')
    auth.clear_token()
    return jsonify({'ok': True}), 200


@app.route('/api/auth/me', methods=['GET'])
def api_auth_me():
    """Local-only auth introspection. NO upstream call (no GET /me/ exists).

    Returns the cached `auth.json` envelope plus an `is_logged_in` flag and
    `exp_at` ISO timestamp. If the token is on disk but expired, the boot
    path will have already cleared it — but defensively we also clear here.
    """
    token = auth.read_token()
    cached = auth.read_auth() or {}

    if token and auth.jwt_expired(token):
        auth.clear_token()
        token = None
        cached = {}

    payload = dict(cached)
    payload['is_logged_in'] = bool(token) and auth.is_logged_in()
    payload['exp_at'] = _exp_iso_for(token)
    return jsonify(payload), 200


# ---------------------------------------------------------------------------
# Athathi data — schedule / history / categories (cached read-only)
# ---------------------------------------------------------------------------

def _athathi_cached_route(endpoint_path, fetch_fn, *, cache_endpoint_key):
    """Shared helper for read-only Athathi GETs that benefit from caching.

    `endpoint_path` is only used for log messages. `fetch_fn(token)` returns
    the parsed body. `cache_endpoint_key` is the stable key used to scope the
    on-disk cache (one file per endpoint+token-hash).
    """
    tok = _athathi_token_or_401()
    if not isinstance(tok, str):
        return tok

    key = athathi_proxy.cache_key_for(cache_endpoint_key, tok)

    def _fetch():
        return fetch_fn(tok)

    try:
        result = athathi_proxy.cached_get(key, ttl=300, fetch_fn=_fetch)
    except athathi_proxy.AthathiError as e:
        return _athathi_handle_error(e, cache_key=key)

    if isinstance(result, athathi_proxy.StaleCacheResult):
        # When the upstream blob is a JSON object we additively attach
        # `fetched_at` so the frontend banner can show the real last-refresh
        # time. For bare lists (schedule/history) we leave the body alone
        # and rely on the X-Fetched-At header instead.
        body = result.blob
        fetched_at = getattr(result, 'fetched_at_iso', None)
        if isinstance(body, dict) and fetched_at:
            body = dict(body)
            body.setdefault('fetched_at', fetched_at)
        resp = jsonify(body)
        resp.headers['X-Cached'] = 'true'
        resp.headers['X-Stale-Reason'] = result.reason
        if fetched_at:
            resp.headers['X-Fetched-At'] = fetched_at
        return resp, 200
    return jsonify(result), 200


@app.route('/api/athathi/schedule', methods=['GET'])
def api_athathi_schedule():
    """Forward to upstream schedule. Returns the bare list verbatim."""
    return _athathi_cached_route(
        '/api/technician/scans/schedule/',
        athathi_proxy.get_schedule,
        cache_endpoint_key='schedule',
    )


@app.route('/api/athathi/history', methods=['GET'])
def api_athathi_history():
    """Forward to upstream history. Returns the bare list verbatim."""
    return _athathi_cached_route(
        '/api/technician/scans/history/',
        athathi_proxy.get_history,
        cache_endpoint_key='history',
    )


@app.route('/api/athathi/categories', methods=['GET'])
def api_athathi_categories():
    """Forward to upstream `/api/categories/`. Returns `{categories: [...]}`."""
    return _athathi_cached_route(
        '/api/categories/',
        athathi_proxy.get_categories,
        cache_endpoint_key='categories',
    )


# ---------------------------------------------------------------------------
# Athathi mutations — complete / cancel scan
# ---------------------------------------------------------------------------

@app.route('/api/athathi/scans/<int:scan_id>/complete', methods=['POST'])
def api_athathi_scan_complete(scan_id):
    """Forward to upstream `/api/technician/scans/<id>/complete/`."""
    tok = _athathi_token_or_401()
    if not isinstance(tok, str):
        return tok
    try:
        body = athathi_proxy.complete_scan(tok, scan_id)
    except athathi_proxy.AthathiError as e:
        return _athathi_handle_error(e)
    return jsonify(body), 200


@app.route('/api/athathi/scans/<int:scan_id>/cancel', methods=['POST'])
def api_athathi_scan_cancel(scan_id):
    """Forward to upstream `/api/technician/scans/<id>/cancel/`."""
    tok = _athathi_token_or_401()
    if not isinstance(tok, str):
        return tok
    try:
        body = athathi_proxy.cancel_scan(tok, scan_id)
    except athathi_proxy.AthathiError as e:
        return _athathi_handle_error(e)
    return jsonify(body), 200


# ---------------------------------------------------------------------------
# Athathi visual search (multipart passthrough)
# ---------------------------------------------------------------------------

@app.route('/api/athathi/visual-search/search-full', methods=['POST'])
def api_athathi_visual_search():
    """Forward a multipart image to `/api/visual-search/search-full/`.

    Saves the inbound `file` field to a tempfile (curl needs a file path for
    `-F file=@<path>`), forwards it, returns the upstream JSON verbatim.
    Cleans the tempfile in `finally` regardless of success.
    """
    tok = _athathi_token_or_401()
    if not isinstance(tok, str):
        return tok

    fobj = request.files.get('file')
    if fobj is None:
        return jsonify({'error': 'multipart field "file" required'}), 400

    import tempfile as _tempfile
    suffix = ''
    if fobj.filename:
        ext = os.path.splitext(fobj.filename)[1]
        if ext and len(ext) <= 8:
            suffix = ext
    fd, tmp_path = _tempfile.mkstemp(prefix='vs_', suffix=suffix or '.bin')
    os.close(fd)
    try:
        fobj.save(tmp_path)
        try:
            body = athathi_proxy.visual_search_full(tok, tmp_path)
        except athathi_proxy.AthathiError as e:
            return _athathi_handle_error(e)
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        except OSError as e:
            _log(f'api_athathi_visual_search: tempfile unlink failed: {e!r}')

    return jsonify(body), 200


# ===========================================================================
# === End Athathi proxy routes ==============================================
# ===========================================================================


# ===========================================================================
# === Project list route ====================================================
# ===========================================================================
#
# Plan §16 step 3. Pure-additive: imports `projects` (new module), reuses
# `athathi_proxy.cached_get` for the upstream calls, and `_athathi_handle_error`
# for the error mapping. This route is the merged view of:
#   - upstream Athathi schedule (mirrored locally as project dirs)
#   - upstream Athathi history (mirrored locally; manifest.completed_at set)
#   - local-only "ad-hoc" projects (e.g. virtual project 0 / negative ids)
#
# Caching:
#   Each upstream call is wrapped in `cached_get` with a 5-minute TTL. On a
#   total network failure (BOTH calls returned StaleCacheResult) we still
#   render the merged view with `cached: true` and the X-Cached / X-Stale
#   headers. On a non-network upstream error we route through
#   `_athathi_handle_error` which already maps 401 / 5xx / 4xx correctly.
# ===========================================================================

import projects as _projects_mod


def _projects_athathi_id(item):
    """Best-effort extraction of an integer scan id from an unknown-shape item."""
    if not isinstance(item, dict):
        return None
    raw = _projects_mod.field_extract(item, 'scan_id', 'id', 'scanId')
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _projects_render_merged(scheduled_raw, history_raw):
    """Build the response dict given upstream schedule + history blobs.

    Both inputs are the parsed bodies (lists, possibly empty). Mirrors each
    item into the local PROJECTS_ROOT via `ensure_project`, then composes the
    three buckets the frontend wants (`scheduled`, `history`, `ad_hoc`).
    """
    if not isinstance(scheduled_raw, list):
        scheduled_raw = []
    if not isinstance(history_raw, list):
        history_raw = []

    scheduled_ids = set()
    history_ids = set()

    # Mirror schedule items first.
    for item in scheduled_raw:
        sid = _projects_athathi_id(item)
        if sid is None:
            continue
        try:
            _projects_mod.ensure_project(sid, athathi_meta=item)
        except (OSError, TypeError) as e:
            _log(f'_projects_render_merged: ensure_project({sid}) failed: {e!r}')
            continue
        scheduled_ids.add(sid)

    # Mirror history items; mark completed_at if not already set.
    for item in history_raw:
        sid = _projects_athathi_id(item)
        if sid is None:
            continue
        try:
            manifest = _projects_mod.ensure_project(sid, athathi_meta=item)
        except (OSError, TypeError) as e:
            _log(f'_projects_render_merged: ensure_project({sid}) failed: {e!r}')
            continue
        if not manifest.get('completed_at'):
            # Only honour explicit completion-timestamp keys. Fallbacks to
            # status/state strings would silently land literals like
            # "completed" or "in-progress" in a timestamp field.
            completed = _projects_mod.field_extract(
                item, 'completed_at', 'completedAt',
                'finished_at', 'finishedAt',
            )
            if completed:
                manifest = dict(manifest)
                looks_iso = (
                    isinstance(completed, str)
                    and 'T' in completed
                    and ('Z' in completed or '+' in completed
                         or '-' in completed[10:])
                )
                manifest['completed_at'] = (
                    completed if looks_iso
                    else datetime.utcnow().isoformat() + 'Z'
                )
                try:
                    _projects_mod.write_manifest(sid, manifest)
                except (OSError, TypeError) as e:
                    _log(f'_projects_render_merged: write completed_at failed: {e!r}')
        history_ids.add(sid)

    # Now read EVERY local project (manifest + computed augmentations) and
    # bucket them.
    all_local = _projects_mod.list_projects()
    by_id = {int(m.get('scan_id')): m for m in all_local
             if isinstance(m.get('scan_id'), int)}

    scheduled_out = []
    for sid in sorted(scheduled_ids, reverse=True):
        m = by_id.get(sid)
        if m is not None:
            scheduled_out.append(m)

    history_out = []
    for sid in sorted(history_ids, reverse=True):
        m = by_id.get(sid)
        if m is not None and sid not in scheduled_ids:
            history_out.append(m)

    ad_hoc = []
    for sid, m in sorted(by_id.items(), key=lambda kv: kv[0], reverse=True):
        if sid in scheduled_ids or sid in history_ids:
            continue
        # Local-only: includes virtual project 0 / negative ids AND any
        # project whose scan_id isn't (yet) in the upstream schedule/history.
        # Per the brief: scan_id < 0 OR no athathi_meta → ad_hoc.
        if sid < 0 or not m.get('athathi_meta'):
            ad_hoc.append(m)
        else:
            # Project that used to be on the schedule but has aged out — still
            # local-only from the technician's POV. Treat as ad-hoc rather
            # than dropping it.
            ad_hoc.append(m)

    return {
        'scheduled': scheduled_out,
        'history':   history_out,
        'ad_hoc':    ad_hoc,
    }


@app.route('/api/projects', methods=['GET'])
def api_projects():
    """Merged view of Athathi schedule + locally-known projects.

    Returns:
      {
        now: <ISO-now>,
        scheduled: [<project>...],   # from Athathi /schedule, mirrored locally
        history:   [<project>...],   # from Athathi /history (post-deduped)
        ad_hoc:    [<project>...],   # local projects with no upstream entry
        cached: bool,                # true iff both upstream calls served stale
      }
    """
    tok = _athathi_token_or_401()
    if not isinstance(tok, str):
        return tok

    sched_key = athathi_proxy.cache_key_for('schedule', tok)
    hist_key = athathi_proxy.cache_key_for('history', tok)

    # BE-2: when an upstream call raises a network failure AND a stale cache
    # is on disk, build a synthetic StaleCacheResult so the route's merge
    # logic still produces the expected `{scheduled, history, ad_hoc, cached,
    # fetched_at}` envelope. Without this the bare-blob shape from
    # `_athathi_handle_error` confuses the SPA. For 401 / 5xx / no-cache
    # network failures, we still defer to `_athathi_handle_error`.
    def _project_feed_cache_fallback(err, cache_key):
        if err.status_code != 0:
            return None  # not a network failure — caller falls through
        path = athathi_proxy._cache_path(cache_key)
        ts, blob = athathi_proxy._read_cache(path)
        if blob is None:
            return None
        fetched_at_iso = None
        try:
            if isinstance(ts, (int, float)):
                fetched_at_iso = datetime.fromtimestamp(
                    ts, tz=timezone.utc,
                ).isoformat().replace('+00:00', 'Z')
        except (OverflowError, OSError, ValueError):
            fetched_at_iso = None
        return athathi_proxy.StaleCacheResult(
            blob, reason='network', fetched_at_iso=fetched_at_iso,
        )

    # Fetch schedule.
    try:
        sched_result = athathi_proxy.cached_get(
            sched_key, ttl=300,
            fetch_fn=lambda: athathi_proxy.get_schedule(tok),
        )
    except athathi_proxy.AthathiError as e:
        sched_result = _project_feed_cache_fallback(e, sched_key)
        if sched_result is None:
            return _athathi_handle_error(e, cache_key=sched_key)

    # Fetch history.
    try:
        hist_result = athathi_proxy.cached_get(
            hist_key, ttl=300,
            fetch_fn=lambda: athathi_proxy.get_history(tok),
        )
    except athathi_proxy.AthathiError as e:
        hist_result = _project_feed_cache_fallback(e, hist_key)
        if hist_result is None:
            return _athathi_handle_error(e, cache_key=hist_key)

    sched_stale = isinstance(sched_result, athathi_proxy.StaleCacheResult)
    hist_stale = isinstance(hist_result, athathi_proxy.StaleCacheResult)

    sched_blob = sched_result.blob if sched_stale else sched_result
    hist_blob = hist_result.blob if hist_stale else hist_result

    merged = _projects_render_merged(sched_blob, hist_blob)
    merged['now'] = datetime.utcnow().isoformat() + 'Z'
    # BE-1: surface a top-level `cached` flag whenever EITHER feed is stale,
    # not only when both are. The frontend banner reflects "any stale data
    # is being shown". Granular `scheduled_cached` / `history_cached` flags
    # let the UI distinguish a partial outage if it ever wants to.
    merged['scheduled_cached'] = bool(sched_stale)
    merged['history_cached'] = bool(hist_stale)
    merged['cached'] = bool(sched_stale or hist_stale)

    # When ANY call served stale cache, surface the actual last-refresh
    # time so the frontend banner can render an honest "last refreshed <ago>"
    # (instead of using `now`, which is just the response timestamp). We
    # take the OLDEST of the available cache timestamps — the banner reflects
    # the least-fresh data the user is currently viewing.
    if merged['cached']:
        ts_candidates = []
        for r, is_stale in ((sched_result, sched_stale), (hist_result, hist_stale)):
            if not is_stale:
                continue
            ts = getattr(r, 'fetched_at_iso', None)
            if isinstance(ts, str) and ts:
                ts_candidates.append(ts)
        if ts_candidates:
            # ISO 8601 UTC strings sort lexicographically as time order; the
            # min() is the oldest.
            merged['fetched_at'] = min(ts_candidates)

    resp = jsonify(merged)
    if merged['cached']:
        resp.headers['X-Cached'] = 'true'
        resp.headers['X-Stale-Reason'] = 'network'
    return resp, 200


# ===========================================================================
# === End project list route ================================================
# ===========================================================================


# ===========================================================================
# === Scoped recording + processing routes (Step 4, plan §16) ===============
# ===========================================================================
#
# Wraps the existing recording + Modal subsystems for the new project +
# scan filesystem layout. NEVER edits the legacy routes (`/api/record/*`,
# `/api/session/<id>/*`); both code paths coexist.
#
# Approach: route-redirection happens at the WRAPPER level — we write
# thin scoped versions of `_recording_thread`, `_process_thread`, and the
# stop / result / artifact routes that target `<projects_root>/<id>/scans/
# <name>/...` instead of the legacy `RECORDINGS_DIR` / `PROCESSED_DIR`.
#
# The scoped recording reuses every primitive from the off-limits block:
# `_setup_network`, `_launch_driver`, `_check_camera`, `_launch_camera`,
# `_launch_tf_static`, `_wait_for_topics`, `_wait_for_topic`,
# `_start_bag_record`, `_start_camera_monitor`, `_camera_monitor_reader`,
# `_kill_process_group`, `_trim_bag_to_sync_start`, the global
# `_active_recording` dict, `_record_lock`, `_active_preview`,
# `_release_camera_device`, `_lock_camera_controls`, `_set_brio_fov`. The
# scoped processing reuses `_zstd_compress`, `_modal_submit`, `_modal_poll`,
# `_modal_cancel`, `_modal_fetch`, `_active_processing`, `_processing_lock`,
# the SSE feed.
#
# Synthetic session naming: scoped recordings still register a row in
# `sessions.json` (so existing helpers like `_get_session` / `_put_session`
# work without changes), but with a recognisable shape:
#   `name`        = '<scan_id>__<scan_name>'
#   `project_scoped` = True
#   `scan_id`     = int
#   `scan_name`   = str
#   `run_id`      = str | None  (set when /process is fired)
# The session_id is the same `YYYYMMDD_HHMMSS_ffffff` shape the legacy
# route uses, so SSE / cancel / recovery sweeps Just Work.
# ===========================================================================

import projects as _projects   # noqa: E402  — intentionally late, scoped block.


def _scoped_session_record(scan_id, scan_name):
    """Build the synthetic legacy-shaped session record for a scoped scan.

    Returns (session_id, session_dict). Caller persists via `_put_session`.
    """
    sid_int = int(scan_id)
    session_id = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    name = f'{sid_int}__{scan_name}'
    session = {
        'name': name,
        'created': datetime.now().isoformat(),
        'status': 'starting',
        'project_scoped': True,
        'scan_id': sid_int,
        'scan_name': str(scan_name),
        'run_id': None,
    }
    return session_id, session


def _scoped_recording_thread(session_id, scan_id, scan_name):
    """Mirror of `_recording_thread` that writes the bag into the scan tree.

    The body MUST stay structurally identical to `_recording_thread` for
    correctness — every cleanup path, every `_active_recording` mutation,
    every `_kill_process_group` call has been hardened by prior commits.
    The only difference is `output_dir`.
    """
    driver_proc = None
    camera_proc = None
    tf_proc = None
    try:
        session = _get_session(session_id)
        session['status'] = 'launching_driver'
        _put_session(session_id, session)

        driver_proc = _launch_driver()
        _active_recording['driver_proc'] = driver_proc

        camera_ok, camera_msg = _check_camera()
        _log(f'[scoped] Camera check: ok={camera_ok}, msg={camera_msg}, device={CAMERA_DEVICE}')
        if camera_ok:
            camera_proc = _launch_camera()
            _active_recording['camera_proc'] = camera_proc
            _log(f'[scoped] Camera launched: PID={camera_proc.pid}')
            tf_proc = _launch_tf_static()
            _active_recording['tf_proc'] = tf_proc
        _active_recording['camera_ok'] = camera_ok

        time.sleep(2)
        if driver_proc.poll() is not None:
            session['status'] = 'error'
            session['error'] = 'Driver exited during warmup'
            _put_session(session_id, session)
            _kill_process_group(camera_proc, timeout=10)
            _kill_process_group(tf_proc)
            _active_recording['driver_proc'] = None
            _active_recording['camera_proc'] = None
            _active_recording['tf_proc'] = None
            _active_recording['camera_ok'] = False
            return

        session['status'] = 'waiting_for_topics'
        _put_session(session_id, session)

        _log('[scoped] Waiting for LiDAR topics...')
        if not _wait_for_topics(timeout=30):
            _log('[scoped] LiDAR topics NOT found after 30s')
            session['status'] = 'error'
            session['error'] = 'Topics not found after 30s'
            _put_session(session_id, session)
            _kill_process_group(driver_proc)
            _kill_process_group(camera_proc, timeout=10)
            _kill_process_group(tf_proc)
            _active_recording['driver_proc'] = None
            _active_recording['camera_proc'] = None
            _active_recording['tf_proc'] = None
            return

        camera_monitor_proc = None
        if camera_ok:
            camera_topic_ok = _wait_for_topic('/camera/image_raw/compressed', timeout=15)
            if not camera_topic_ok:
                camera_ok = False
                _active_recording['camera_ok'] = False

            if camera_ok:
                _active_recording['camera_frames'] = 0
                _active_recording['_camera_frames_prev'] = 0
                _active_recording['camera_streaming'] = False
                camera_monitor_proc = _start_camera_monitor()
                _active_recording['camera_monitor_proc'] = camera_monitor_proc
                threading.Thread(
                    target=_camera_monitor_reader, args=(camera_monitor_proc,),
                    daemon=True,
                ).start()
                deadline = time.time() + 20.0
                while time.time() < deadline:
                    if _active_recording['camera_frames'] > 0:
                        break
                    time.sleep(0.1)
                if _active_recording['camera_frames'] == 0:
                    _kill_process_group(camera_monitor_proc)
                    _active_recording['camera_monitor_proc'] = None
                    camera_monitor_proc = None
                    camera_ok = False
                    _active_recording['camera_ok'] = False

        topics = ['/unilidar/cloud', '/unilidar/imu']
        if camera_ok:
            topics.extend([
                '/camera/image_raw/compressed',
                '/camera/camera_info',
                '/tf_static',
            ])

        # The ONLY divergence from legacy: bag goes under the scan tree.
        output_dir = _projects.scan_dir(scan_id, scan_name)
        # Ensure the directory exists; create_scan made it but a manual
        # delete + ad-hoc start could race here. Keep the recording resilient.
        os.makedirs(output_dir, exist_ok=True)
        # ros2 bag record refuses to overwrite an existing output dir and
        # exits ~1 s after launch ("Output directory already exists"). Scan
        # dirs use fixed names like `48__scan_1` so every re-record on the
        # same scan would collide. Wipe the prior rosbag/ subtree so the
        # technician's fresh "Start recording" tap actually starts.
        prior_bag_dir = os.path.join(output_dir, 'rosbag')
        if os.path.isdir(prior_bag_dir):
            try:
                shutil.rmtree(prior_bag_dir)
                _log(f'[scoped] Wiped prior bag dir {prior_bag_dir}')
            except OSError as e:
                _log(f'[scoped] Warning: could not wipe {prior_bag_dir}: {e!r}')
        bag_proc = _start_bag_record(output_dir, topics)

        _active_recording['session_id'] = session_id
        _active_recording['bag_proc'] = bag_proc
        _active_recording['start_time'] = time.time()

        session['status'] = 'recording'
        session['camera'] = camera_ok
        _put_session(session_id, session)

    except Exception as e:
        session = _get_session(session_id)
        if session:
            session['status'] = 'error'
            session['error'] = str(e)
            _put_session(session_id, session)
        _kill_process_group(driver_proc)
        _kill_process_group(camera_proc, timeout=10)
        _kill_process_group(tf_proc)
        _kill_process_group(_active_recording.get('camera_monitor_proc'))
        _active_recording['driver_proc'] = None
        _active_recording['camera_proc'] = None
        _active_recording['tf_proc'] = None
        _active_recording['bag_proc'] = None
        _active_recording['camera_monitor_proc'] = None
        _active_recording['session_id'] = None
        _active_recording['start_time'] = None
        _active_recording['camera_ok'] = False
        _active_recording['camera_frames'] = 0
        _active_recording['camera_streaming'] = False
    finally:
        _active_recording['starting'] = False


def _scoped_stop_recording():
    """Mirror of `/api/record/stop` body, writing trim/metadata under the scan.

    Returns (response_payload, status_code). Designed to be called inside
    `with _record_lock:` by the wrapping route, mirroring the existing
    /api/record/stop locking semantics.
    """
    if not _is_recording():
        return {'error': 'Not recording'}, 409

    session_id = _active_recording['session_id']
    bag_proc = _active_recording['bag_proc']
    driver_proc = _active_recording['driver_proc']
    camera_proc = _active_recording['camera_proc']
    tf_proc = _active_recording['tf_proc']
    start_time = _active_recording['start_time']
    camera_ok = _active_recording['camera_ok']

    _kill_process_group(bag_proc, timeout=30)

    camera_monitor_proc = _active_recording.get('camera_monitor_proc')
    _kill_process_group(driver_proc)
    _kill_process_group(camera_proc, timeout=10)
    _kill_process_group(tf_proc)
    _kill_process_group(camera_monitor_proc)

    duration = round(time.time() - start_time, 1) if start_time else 0

    session_preview = _get_session(session_id)
    if session_preview and session_preview.get('project_scoped') and camera_ok:
        bag_path_preview = _projects.scan_rosbag_dir(
            session_preview['scan_id'], session_preview['scan_name'])
        if os.path.isdir(bag_path_preview):
            _trim_bag_to_sync_start(
                bag_path_preview,
                required_topics=['/unilidar/cloud',
                                 '/camera/image_raw/compressed']
            )

    session = _get_session(session_id)
    if session and session.get('project_scoped'):
        session['status'] = 'stopped'
        session['duration'] = duration

        scan_root = _projects.scan_dir(session['scan_id'], session['scan_name'])
        bag_path = _projects.scan_rosbag_dir(session['scan_id'], session['scan_name'])
        if os.path.isdir(bag_path):
            total = sum(
                os.path.getsize(os.path.join(bag_path, f))
                for f in os.listdir(bag_path)
                if os.path.isfile(os.path.join(bag_path, f))
            )
            session['bag_size'] = f'{total / (1024*1024):.1f} MB'

        topics = ['/unilidar/cloud', '/unilidar/imu']
        if camera_ok:
            topics.extend(['/camera/image_raw/compressed',
                           '/camera/camera_info', '/tf_static'])

        meta_path = os.path.join(scan_root, 'metadata.txt')
        try:
            with open(meta_path, 'w') as f:
                f.write(f"Session: {session['name']}\n")
                f.write(f"Date: {datetime.now().isoformat()}\n")
                f.write(f"Duration: {duration}s\n")
                f.write(f"Bag size: {session.get('bag_size', 'unknown')}\n")
                f.write(f"Topics: {' '.join(topics)}\n")
                f.write(f"Camera: {'yes' if camera_ok else 'no'}\n")
                if camera_ok:
                    f.write(f"Camera resolution: {CAMERA_WIDTH}x{CAMERA_HEIGHT}@{CAMERA_FPS_CAPTURE}fps\n")
                    f.write(f"Intrinsics: {'calibrated' if os.path.isfile(INTRINSICS_FILE) else 'uncalibrated'}\n")
                    f.write(f"Extrinsics: {'calibrated' if os.path.isfile(EXTRINSICS_FILE) else 'uncalibrated'}\n")
                f.write(f"Notes:\n")
        except OSError:
            pass

        calib_dest = os.path.join(scan_root, 'calibration')
        os.makedirs(calib_dest, exist_ok=True)
        for src_file in [INTRINSICS_FILE, EXTRINSICS_FILE]:
            if os.path.isfile(src_file):
                shutil.copy2(src_file, calib_dest)

        _put_session(session_id, session)

    _active_recording['session_id'] = None
    _active_recording['bag_proc'] = None
    _active_recording['driver_proc'] = None
    _active_recording['camera_proc'] = None
    _active_recording['tf_proc'] = None
    _active_recording['camera_monitor_proc'] = None
    _active_recording['start_time'] = None
    _active_recording['starting'] = False
    _active_recording['camera_ok'] = False
    _active_recording['camera_frames'] = 0
    _active_recording['camera_streaming'] = False

    return {
        'session_id': session_id,
        'duration': duration,
        'bag_size': session.get('bag_size') if session else None,
    }, 200


def _scoped_find_mcap(scan_id, scan_name):
    """Locate the .mcap inside `<scan>/rosbag/`. Returns None if missing."""
    bag_dir = _projects.scan_rosbag_dir(scan_id, scan_name)
    if not os.path.isdir(bag_dir):
        return None
    for f in os.listdir(bag_dir):
        if f.endswith('.mcap'):
            return os.path.join(bag_dir, f)
    return None


def _scoped_save_done_artifacts(session_id, job_id, envelope, run_dir):
    """Persist a `done` envelope under `<run_dir>/`. Mirrors `_save_done_artifacts`
    but keyed on a project-scoped run directory.
    """
    os.makedirs(run_dir, exist_ok=True)

    # 1. result.json
    result_path = os.path.join(run_dir, 'result.json')
    try:
        with open(result_path, 'w') as f:
            json.dump(envelope, f, indent=2)
    except IOError as e:
        _log(f'[scoped] failed to write result.json for {session_id}: {e}')

    # 2. layout_merged.txt
    layout_path = os.path.join(run_dir, 'layout_merged.txt')
    try:
        _modal_fetch(job_id, 'artifact/layout_merged.txt', layout_path)
    except IOError as e:
        _log(f'[scoped] failed to fetch layout for {session_id}: {e}')

    # 3. best_views — wipe first.
    bv_dir = os.path.join(run_dir, 'best_views')
    shutil.rmtree(bv_dir, ignore_errors=True)
    os.makedirs(bv_dir, exist_ok=True)
    best_images = envelope.get('best_images') or []
    for idx, _bbox in enumerate(best_images):
        dest = os.path.join(bv_dir, f'{idx}.jpg')
        try:
            _modal_fetch(job_id, f'image/{idx}', dest)
        except IOError as e:
            _log(f'[scoped] failed to fetch image {idx} for {session_id}: {e}')

    # Persist completion on the session.
    sess = _get_session(session_id) or {}
    sess['status'] = 'processed'
    sess['slam_status'] = 'done'
    sess['slam_stage'] = 'done'
    sess['slam_result'] = envelope
    sess.pop('slam_error', None)
    _put_session(session_id, sess)


def _scoped_apply_carry_over(scan_id, scan_name, new_run_dir, prior_run_dir):
    """BE-4: migrate the technician's review from the previous active run
    onto the freshly-completed run.

    No-ops when no prior review exists. On success, persists the new
    review.json (with `carry_over_warnings` populated). Read-modify-write
    is serialised under the per-scan review-write lock.
    """
    if not prior_run_dir or not os.path.isdir(prior_run_dir):
        return
    # `_review` is the late-imported module above (Step 5 block).
    old_review = _review.read_review(prior_run_dir)
    if not isinstance(old_review, dict):
        return
    old_result_path = os.path.join(prior_run_dir, 'result.json')
    new_result_path = os.path.join(new_run_dir, 'result.json')
    try:
        with open(old_result_path, 'r') as f:
            old_result = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    try:
        with open(new_result_path, 'r') as f:
            new_result = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    try:
        new_review, warnings = _review.carry_over_review(
            old_review, old_result, new_result,
        )
    except (TypeError, ValueError) as e:
        _log(f'[scoped] carry_over_review rejected: {e!r}')
        return
    if isinstance(warnings, list) and warnings:
        new_review = dict(new_review)
        new_review['carry_over_warnings'] = warnings
    if scan_id is not None and scan_name is not None:
        with _review_lock_for(scan_id, scan_name):
            _review.write_review(new_run_dir, new_review)
    else:
        _review.write_review(new_run_dir, new_review)


def _scoped_process_thread(session_id, mcap_path, run_dir,
                           scan_id=None, scan_name=None, prior_run_dir=None):
    """Mirror of `_process_thread`, writing artifacts under `run_dir`.

    Locking + cancellation semantics are identical so the SSE / cancel /
    recovery code paths Just Work.

    BE-4: when the new run completes successfully and a `prior_run_dir`
    was passed (i.e. there was a previous active run with a technician
    review on disk), call `review.carry_over_review` to migrate the prior
    review state onto the new run, persisting any warnings to the new
    `review.json["carry_over_warnings"]`.
    """
    os.makedirs(run_dir, exist_ok=True)
    zst_path = os.path.join(run_dir, 'scan.mcap.zst')
    job_id = None
    try:
        with _processing_lock:
            entry = _active_processing.get(session_id)
            if entry is None:
                return
            cancel_event = entry['cancel']

        if cancel_event.is_set():
            _set_session_slam_status(session_id, 'cancelled')
            return

        _set_active_stage(session_id, 'compressing')
        _set_session_slam_status(session_id, 'compressing')
        try:
            _zstd_compress(mcap_path, zst_path)
        except Exception as e:
            _set_session_slam_error(session_id, str(e))
            return

        if cancel_event.is_set():
            _set_session_slam_status(session_id, 'cancelled')
            return

        idem_key = uuid.uuid4().hex
        sess = _get_session(session_id) or {}
        sess['idem_key'] = idem_key
        sess.pop('slam_error', None)
        sess.pop('slam_result', None)
        sess['slam_status'] = 'uploading'
        sess['slam_stage'] = 'uploading'
        _put_session(session_id, sess)

        _set_active_stage(session_id, 'uploading')

        try:
            job_id = _modal_submit(zst_path, idem_key)
        except Exception as e:
            _set_session_slam_error(session_id, f'submit: {e}')
            return

        with _processing_lock:
            entry = _active_processing.get(session_id)
            if entry is not None:
                entry['job_id'] = job_id
                entry['stage'] = 'queued'
        sess = _get_session(session_id) or {}
        sess['job_id'] = job_id
        sess['slam_status'] = 'queued'
        sess['slam_stage'] = 'queued'
        sess['submitted_at'] = datetime.utcnow().isoformat() + 'Z'
        _put_session(session_id, sess)

        consecutive_failures = 0
        retry_after = POLL_INTERVAL_S
        while True:
            if cancel_event.wait(retry_after):
                _modal_cancel(job_id)
                _set_session_slam_status(session_id, 'cancelled')
                return

            try:
                envelope, retry_after = _modal_poll(job_id)
                consecutive_failures = 0
            except Exception as e:
                consecutive_failures += 1
                _log(f'[scoped] _modal_poll failed ({consecutive_failures}/{_MODAL_POLL_MAX_FAILURES}): {e}')
                if consecutive_failures >= _MODAL_POLL_MAX_FAILURES:
                    _set_session_slam_error(
                        session_id, f'poll failed after {consecutive_failures} tries: {e}')
                    return
                retry_after = max(retry_after, POLL_INTERVAL_S)
                continue

            stage = envelope.get('status', 'unknown')

            _set_active_stage(session_id, stage)
            sess = _get_session(session_id) or {}
            sess['slam_status'] = stage
            sess['slam_stage'] = stage
            _put_session(session_id, sess)

            if stage == 'failed':
                err = envelope.get('error') or {}
                err_stage = err.get('stage') or 'unknown'
                err_tail = err.get('stderr_tail') or err.get('message') or ''
                _set_session_slam_error(
                    session_id, f'{err_stage}: {err_tail}')
                return

            if stage == 'done':
                _scoped_save_done_artifacts(session_id, job_id, envelope, run_dir)
                # BE-4: carry over the prior run's review state, if any.
                try:
                    _scoped_apply_carry_over(
                        scan_id, scan_name, run_dir, prior_run_dir,
                    )
                except Exception as carry_err:
                    _log(f'[scoped] carry-over failed for {session_id}: {carry_err!r}')
                return

            continue

    except Exception as e:
        _log(f'[scoped] _scoped_process_thread crashed for {session_id}: {e}')
        _set_session_slam_error(session_id, f'internal: {e}')
    finally:
        try:
            if os.path.isfile(zst_path):
                os.remove(zst_path)
        except OSError:
            pass
        with _processing_lock:
            _active_processing.pop(session_id, None)
        sess = _get_session(session_id)
        if sess and 'idem_key' in sess:
            sess.pop('idem_key', None)
            _put_session(session_id, sess)


# ---------------------------------------------------------------------------
# Auth + project gates
# ---------------------------------------------------------------------------

def _scoped_require_login():
    """Return None if logged in, else a Flask response tuple."""
    if not auth.is_logged_in():
        return jsonify({'error': 'not logged in'}), 401
    return None


def _scoped_require_project(scan_id):
    """Return None if the local project manifest exists, else (resp, 404)."""
    if _projects.read_manifest(scan_id) is None:
        return jsonify({'error': 'project not found locally'}), 404
    return None


def _scoped_require_scan(scan_id, scan_name):
    """Return None if the scan dir exists, else (resp, 404)."""
    if not os.path.isdir(_projects.scan_dir(scan_id, scan_name)):
        return jsonify({'error': 'scan not found'}), 404
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/api/project/<int:scan_id>/scan', methods=['POST'])
def api_scoped_create_scan(scan_id):
    """Create a new scan inside an existing project.

    Body: `{name: "<snake_case>"}`. Returns the created summary on 200,
    400 on a bad name, 404 when the project isn't local, 409 if a scan with
    that name already exists.
    """
    gate = _scoped_require_login()
    if gate:
        return gate
    gate = _scoped_require_project(scan_id)
    if gate:
        return gate

    data = request.get_json(force=True, silent=True) or {}
    name = data.get('name', '')
    try:
        summary = _projects.create_scan(scan_id, name)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except FileExistsError:
        return jsonify({'error': 'scan already exists'}), 409
    return jsonify(summary), 200


@app.route('/api/project/<int:scan_id>/scan/<scan_name>', methods=['DELETE'])
def api_scoped_delete_scan(scan_id, scan_name):
    """Delete an entire scan subdir.

    Refuses with 409 if a recording is in progress (any recording — the
    box is single-tenant). 404 if the scan isn't on disk.
    """
    gate = _scoped_require_login()
    if gate:
        return gate
    gate = _scoped_require_project(scan_id)
    if gate:
        return gate
    gate = _scoped_require_scan(scan_id, scan_name)
    if gate:
        return gate

    # Single-tenant box: any recording in progress means deletion is unsafe.
    if _is_recording():
        return jsonify({'error': 'recording in progress'}), 409

    try:
        _projects.delete_scan(scan_id, scan_name)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ok': True}), 200


@app.route('/api/project/<int:scan_id>/scans', methods=['GET'])
def api_scoped_list_scans(scan_id):
    """List the scans currently on disk for a project."""
    gate = _scoped_require_login()
    if gate:
        return gate
    gate = _scoped_require_project(scan_id)
    if gate:
        return gate
    return jsonify({'scans': _projects.list_scans(scan_id)}), 200


@app.route('/api/project/<int:scan_id>/scan/<scan_name>/start_recording',
           methods=['POST'])
def api_scoped_start_recording(scan_id, scan_name):
    """Kick off a recording into `<scan>/rosbag/`.

    409 when the box is busy (`_is_busy()` covers both 'recording' and the
    'starting' grace window).
    """
    gate = _scoped_require_login()
    if gate:
        return gate
    gate = _scoped_require_project(scan_id)
    if gate:
        return gate
    gate = _scoped_require_scan(scan_id, scan_name)
    if gate:
        return gate

    with _record_lock:
        if _is_busy():
            return jsonify({'error': 'Already recording or starting'}), 409

        net_ok, net_msg = _setup_network()
        if not net_ok:
            return jsonify({'error': net_msg}), 500

        _active_recording['starting'] = True

        session_id, session = _scoped_session_record(scan_id, scan_name)
        _put_session(session_id, session)

        thread = threading.Thread(
            target=_scoped_recording_thread,
            args=(session_id, scan_id, scan_name),
            daemon=True,
        )
        thread.start()

        return jsonify({
            'session_id': session_id,
            'name': session['name'],
            'scan_id': int(scan_id),
            'scan_name': scan_name,
        }), 200


@app.route('/api/project/<int:scan_id>/scan/<scan_name>/stop_recording',
           methods=['POST'])
def api_scoped_stop_recording(scan_id, scan_name):
    """Stop the current recording.

    Note: the box only runs one recording at a time (`_active_recording`
    is global). The `<scan_id>/<scan_name>` in the URL identifies *which*
    scan the caller expected — we 409 if the active recording isn't the
    one the caller asked about. The recording subsystem itself remains
    untouched.
    """
    gate = _scoped_require_login()
    if gate:
        return gate
    gate = _scoped_require_project(scan_id)
    if gate:
        return gate
    gate = _scoped_require_scan(scan_id, scan_name)
    if gate:
        return gate

    with _record_lock:
        active_sid = _active_recording.get('session_id')
        active_session = _get_session(active_sid) if active_sid else None
        # 409 unless the active recording is BOTH project-scoped AND for
        # the exact same scan. A legacy (non-project-scoped) recording is
        # always a mismatch from a scoped URL — the legacy `/api/record/stop`
        # is the right tool there.
        if active_session is not None and not (
            active_session.get('project_scoped')
            and active_session.get('scan_id') == int(scan_id)
            and active_session.get('scan_name') == scan_name
        ):
            return jsonify({
                'error': 'active recording belongs to a different scan',
                'active_scan_id': active_session.get('scan_id'),
                'active_scan_name': active_session.get('scan_name'),
                'active_project_scoped': bool(
                    active_session.get('project_scoped')),
            }), 409

        payload, code = _scoped_stop_recording()
        return jsonify(payload), code


@app.route('/api/project/<int:scan_id>/scan/<scan_name>/process',
           methods=['POST'])
def api_scoped_process(scan_id, scan_name):
    """Submit the scan's MCAP to Modal, writing artifacts under the active run."""
    gate = _scoped_require_login()
    if gate:
        return gate
    gate = _scoped_require_project(scan_id)
    if gate:
        return gate
    gate = _scoped_require_scan(scan_id, scan_name)
    if gate:
        return gate

    if not MODAL_API_KEY:
        return jsonify({'error': 'MODAL_API not configured on server'}), 503
    if not _ZSTD_BIN:
        return jsonify({'error': 'zstd not installed on server'}), 503

    mcap_path = _scoped_find_mcap(scan_id, scan_name)
    if not mcap_path:
        return jsonify({'error': 'No MCAP file found for scan'}), 404

    # Hold `_processing_lock` across the dedupe check + the run-id +
    # active-run + session reservation. The previous code keyed dedupe on
    # a freshly-minted per-request session_id, which is unique by
    # construction, so the check was structurally a no-op — two concurrent
    # POSTs would both pass, allocate distinct run dirs, race on
    # `set_active_run`, and spawn two H100 Modal jobs.
    with _processing_lock:
        for _sid, _entry in _active_processing.items():
            if (
                _entry.get('scan_id') == int(scan_id)
                and _entry.get('scan_name') == scan_name
            ):
                return jsonify({'error': 'Already processing'}), 409

        # BE-4: capture the prior active-run info BEFORE we override it so
        # the processing thread can carry-over the technician's review
        # state from the old run to the new run on completion.
        prior_run_id = _projects.read_active_run(scan_id, scan_name)
        prior_run_dir = (
            _projects.processed_dir_for_run(scan_id, scan_name, prior_run_id)
            if prior_run_id else None
        )

        # Allocate a fresh run id and make it the active run.
        run_id = _projects.allocate_run_id(scan_id, scan_name)
        run_dir = _projects.processed_dir_for_run(scan_id, scan_name, run_id)
        os.makedirs(run_dir, exist_ok=True)
        _projects.set_active_run(scan_id, scan_name, run_id)

        # Synthesize a session record so the SSE feed and cancel sweep see
        # this run. Keyed by a fresh session_id, distinct from the
        # recording id.
        session_id = datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '_proc'
        session = {
            'name': f'{int(scan_id)}__{scan_name}',
            'created': datetime.now().isoformat(),
            'status': 'processing',
            'project_scoped': True,
            'scan_id': int(scan_id),
            'scan_name': str(scan_name),
            'run_id': run_id,
            'slam_status': 'compressing',
            'slam_stage': 'compressing',
        }
        _put_session(session_id, session)

        _active_processing[session_id] = {
            'status': 'processing',
            'stage': 'compressing',
            'start_time': time.time(),
            'cancel': threading.Event(),
            'job_id': None,
            'run_id': run_id,
            'scan_id': int(scan_id),
            'scan_name': str(scan_name),
        }

    thread = threading.Thread(
        target=_scoped_process_thread,
        args=(session_id, mcap_path, run_dir),
        kwargs={
            'scan_id': int(scan_id),
            'scan_name': str(scan_name),
            'prior_run_dir': prior_run_dir,
        },
        daemon=True,
    )
    thread.start()

    return jsonify({
        'session_id': session_id,
        'run_id': run_id,
        'status': 'started',
        'job_id_pending': True,
    }), 200


def _scoped_find_active_processing_for(scan_id, scan_name):
    """Return the (session_id, entry) of the most-recent active processing
    for this scan, or (None, None) if nothing is in flight.

    Multiple legacy / scoped jobs can coexist; we filter by scan match.
    """
    with _processing_lock:
        for sid, entry in _active_processing.items():
            if (
                entry.get('scan_id') == int(scan_id)
                and entry.get('scan_name') == scan_name
            ):
                # Return a shallow snapshot so the caller doesn't hold the
                # lock while reading other fields.
                return sid, dict(entry)
    return None, None


@app.route('/api/project/<int:scan_id>/scan/<scan_name>/result',
           methods=['GET'])
def api_scoped_result(scan_id, scan_name):
    """Same envelope shape as `/api/session/<id>/result`, scoped to the active run.

    Returns `{status: 'not_processed'}` when the scan has no active run yet.
    """
    gate = _scoped_require_login()
    if gate:
        return gate
    gate = _scoped_require_project(scan_id)
    if gate:
        return gate
    gate = _scoped_require_scan(scan_id, scan_name)
    if gate:
        return gate

    # In-flight job for this scan?
    sid, entry = _scoped_find_active_processing_for(scan_id, scan_name)
    if entry is not None:
        elapsed = round(time.time() - entry.get('start_time', time.time()), 1)
        return jsonify({
            'status': 'processing',
            'stage': entry.get('stage') or 'starting',
            'elapsed': elapsed,
            'job_id': entry.get('job_id'),
            'run_id': entry.get('run_id'),
        })

    run_id = _projects.read_active_run(scan_id, scan_name)
    if not run_id:
        return jsonify({'status': 'not_processed'})

    run_dir = _projects.processed_dir_for_run(scan_id, scan_name, run_id)
    result_path = os.path.join(run_dir, 'result.json')
    envelope = None
    if os.path.isfile(result_path):
        try:
            with open(result_path, 'r') as f:
                envelope = json.load(f)
        except (OSError, json.JSONDecodeError):
            envelope = None

    if envelope is not None:
        return jsonify({
            'status': 'done',
            'stage': 'done',
            'run_id': run_id,
            'result': envelope,
        })

    # Run exists but result.json missing — most likely an error / cancelled
    # state. Fall back to the last persisted session record for this scan
    # (best-effort).
    return jsonify({
        'status': 'unknown',
        'run_id': run_id,
    })


@app.route('/api/project/<int:scan_id>/scan/<scan_name>/best_view/<int:idx>.jpg',
           methods=['GET'])
def api_scoped_best_view(scan_id, scan_name, idx):
    """Serve `<run_dir>/best_views/<idx>.jpg` from disk.

    Prefers `<idx>_recapture.jpg` if present (forward-compat with step 5).
    """
    gate = _scoped_require_login()
    if gate:
        return gate
    gate = _scoped_require_project(scan_id)
    if gate:
        return gate
    gate = _scoped_require_scan(scan_id, scan_name)
    if gate:
        return gate

    run_id = _projects.read_active_run(scan_id, scan_name)
    if not run_id:
        return jsonify({'error': 'No active run'}), 404
    run_dir = _projects.processed_dir_for_run(scan_id, scan_name, run_id)
    bv_dir = os.path.join(run_dir, 'best_views')

    recap = os.path.join(bv_dir, f'{idx}_recapture.jpg')
    if os.path.isfile(recap):
        return send_file(recap, mimetype='image/jpeg')

    orig = os.path.join(bv_dir, f'{idx}.jpg')
    if os.path.isfile(orig):
        return send_file(orig, mimetype='image/jpeg')

    return jsonify({'error': 'Image not found'}), 404


@app.route('/api/project/<int:scan_id>/scan/<scan_name>/layout.txt',
           methods=['GET'])
def api_scoped_layout(scan_id, scan_name):
    """Serve `<run_dir>/layout_merged.txt` from disk."""
    gate = _scoped_require_login()
    if gate:
        return gate
    gate = _scoped_require_project(scan_id)
    if gate:
        return gate
    gate = _scoped_require_scan(scan_id, scan_name)
    if gate:
        return gate

    run_id = _projects.read_active_run(scan_id, scan_name)
    if not run_id:
        return jsonify({'error': 'No active run'}), 404
    path = os.path.join(
        _projects.processed_dir_for_run(scan_id, scan_name, run_id),
        'layout_merged.txt',
    )
    if not os.path.isfile(path):
        return jsonify({'error': 'Layout not found'}), 404
    return send_file(path, mimetype='text/plain')


@app.route('/api/project/<int:scan_id>/scan/<scan_name>/artifact/<name>',
           methods=['GET'])
def api_scoped_artifact_proxy(scan_id, scan_name, name):
    """Proxy a Modal artifact, gated on the same whitelist as the legacy
    route. Reads job_id from the most-recent session record matching this
    scan + active run.
    """
    gate = _scoped_require_login()
    if gate:
        return gate
    gate = _scoped_require_project(scan_id)
    if gate:
        return gate
    gate = _scoped_require_scan(scan_id, scan_name)
    if gate:
        return gate

    if name not in _ARTIFACT_WHITELIST:
        return jsonify({'error': 'Unknown artifact'}), 404

    run_id = _projects.read_active_run(scan_id, scan_name)
    if not run_id:
        return jsonify({'error': 'No active run'}), 404

    # Find a session record for this scan + run to dig out the job_id.
    job_id = None
    for sid, sess in _get_sessions().items():
        if (
            sess.get('project_scoped')
            and sess.get('scan_id') == int(scan_id)
            and sess.get('scan_name') == scan_name
            and sess.get('run_id') == run_id
        ):
            jid = sess.get('job_id')
            if jid:
                job_id = jid
                break
    if not job_id:
        return jsonify({'error': 'No job_id for this run'}), 404

    if not MODAL_API_KEY:
        return jsonify({'error': 'MODAL_API not configured on server'}), 503

    url = f'{MODAL_API_URL}/jobs/{job_id}/artifact/{name}'
    proc = subprocess.Popen(
        ['curl', '-sSf', '-L', '--max-time', '600',
         '-H', f'X-API-Key: {MODAL_API_KEY}', url],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )

    mime = 'application/octet-stream'
    if name.endswith('.json'):
        mime = 'application/json'
    elif name.endswith('.txt'):
        mime = 'text/plain'

    @stream_with_context
    def gen():
        try:
            while True:
                chunk = proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                proc.kill()

    return Response(gen(), mimetype=mime,
                    headers={'Content-Disposition':
                             f'inline; filename="{name}"'})


# ===========================================================================
# === End scoped recording + processing routes ==============================
# ===========================================================================


# ===========================================================================
# === Technician review routes (Step 5, plan §16) ===========================
# ===========================================================================
#
# Wraps the new `review.py` module. Pure functions live there; this block is
# the Flask glue: schema CRUD, atomic merge, Brio recapture, run inventory,
# active-run switch, run delete. NEVER edits the recording or Modal
# subsystems — `_is_recording()`, `_active_recording`, `_active_processing`,
# etc. are read-only callers.
#
# Recapture flow (§7c): the route guards against `_is_recording()` OR
# `_active_recording['starting']` — `camera_node.py` holds /dev/video0
# exclusively during recording. With the guard, a one-shot ffmpeg snapshot
# is safe.
#
# Run inventory + active-run pointer + run delete: read-only on the Modal
# subsystem; refuses to delete the active run or any submitted run (audit
# trail per §19b).
# ===========================================================================

import review as _review     # noqa: E402  — intentionally late, scoped block.


# ---------------------------------------------------------------------------
# Per-(scan_id, scan_name) recapture serialisation
# ---------------------------------------------------------------------------
# Two simultaneous POSTs to /review/recapture/<idx> for the same scan/idx
# would otherwise call ffmpeg with `-y` against the same path and race on
# the JPEG truncate + review.json write. We serialise per-scan with a small
# in-memory lock map. The map is allowed to grow unbounded — at the scale
# of this app (a handful of projects, finitely many scans), evicting on
# scan delete isn't worth the bookkeeping.

_recapture_locks_outer = threading.Lock()
_recapture_locks = {}  # (scan_id:int, scan_name:str) -> threading.Lock()


def _recapture_lock_for(scan_id, scan_name):
    key = (int(scan_id), scan_name)
    with _recapture_locks_outer:
        lock = _recapture_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _recapture_locks[key] = lock
        return lock


# BE-3: per-(scan_id, scan_name) review-write lock. Mirrors the recapture
# pattern; the recapture lock STAYS (it serialises the ffmpeg call), and this
# new lock layers under it for the JSON-write phase. Two simultaneous PATCH
# / merge / mark-reviewed / link-product / recapture writes against the
# SAME review.json would otherwise race on read-modify-write.
_review_locks_outer = threading.Lock()
_review_locks = {}  # (scan_id:int, scan_name:str) -> threading.Lock()


def _review_lock_for(scan_id, scan_name):
    key = (int(scan_id), scan_name)
    with _review_locks_outer:
        lock = _review_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _review_locks[key] = lock
        return lock


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _review_active_run_dir(scan_id, scan_name):
    """Return (run_id, run_dir) for the active run, or (None, None)."""
    rid = _projects.read_active_run(scan_id, scan_name)
    if not rid:
        return None, None
    return rid, _projects.processed_dir_for_run(scan_id, scan_name, rid)


def _review_load_or_init(scan_id, scan_name, run_dir):
    """Read review.json; if missing, build an initial review from result.json.

    Returns the review dict. Does NOT persist the initial review — the
    caller decides (the GET endpoint just returns a freshly-computed dict;
    the PATCH endpoint persists after mutation).
    """
    rv = _review.read_review(run_dir)
    if rv is not None:
        return rv
    result_path = os.path.join(run_dir, 'result.json')
    result = None
    if os.path.isfile(result_path):
        try:
            with open(result_path, 'r') as f:
                result = json.load(f)
        except (OSError, json.JSONDecodeError):
            result = None
    if result is None:
        # No result.json on disk yet — return a stub.
        return {
            'scan_id': int(scan_id),
            'room_name': str(scan_name),
            'result_job_id': None,
            'started_at': None,
            'reviewed_at': None,
            'submitted_at': None,
            'version': 1,
            'bboxes': {},
            'notes': '',
        }
    return _review.initial_review(scan_id, scan_name, result)


def _review_load_result(run_dir):
    """Best-effort read of `<run_dir>/result.json`. None on missing/parse error."""
    p = os.path.join(run_dir, 'result.json')
    if not os.path.isfile(p):
        return None
    try:
        with open(p, 'r') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _review_require_active_run(scan_id, scan_name):
    """Return (run_id, run_dir, None) on success, or (None, None, error_resp).

    `error_resp` is a (jsonify-tuple, status_code) on failure.
    """
    rid, rdir = _review_active_run_dir(scan_id, scan_name)
    if rid is None:
        return None, None, (jsonify({'error': 'No active run'}), 404)
    if not os.path.isdir(rdir):
        return None, None, (jsonify({'error': 'Active run dir missing'}), 404)
    return rid, rdir, None


def _review_brio_snapshot(dst_path):
    """Spawn ffmpeg to grab one Brio frame at 1920x1080.

    Returns (ok, stderr_tail). On success, the JPEG is at `dst_path`. On
    failure, the file is NOT cleaned up (the caller does, if needed).

    Time-budget: 10 s. /dev/video0 is hard-coded — `camera_node.py` lives
    with that assumption too.

    /dev/video0 contention: the recapture overlay's <img src=/api/camera/preview>
    holds /dev/video0 via `_active_preview`. ffmpeg here would otherwise fail
    with "Device or resource busy". Release the preview first, then take the
    snapshot. The next preview GET re-spawns it cleanly.
    """
    _release_camera_device(timeout=3.0)
    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error',
        '-f', 'v4l2', '-video_size', '1920x1080',
        '-i', '/dev/video0',
        '-frames:v', '1', '-q:v', '2',
        '-y', dst_path,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=10,
        )
    except subprocess.TimeoutExpired as e:
        tail = (e.stderr.decode('utf-8', 'replace') if e.stderr else '')[-2000:]
        return False, tail or 'ffmpeg timed out after 10s'
    except FileNotFoundError:
        return False, 'ffmpeg not installed on server'

    if proc.returncode != 0:
        tail = (proc.stderr.decode('utf-8', 'replace')
                if proc.stderr else '')[-2000:]
        return False, tail or f'ffmpeg exited with code {proc.returncode}'
    if not os.path.isfile(dst_path) or os.path.getsize(dst_path) == 0:
        return False, 'ffmpeg returned 0 but produced no output'
    return True, ''


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/api/project/<int:scan_id>/scan/<scan_name>/review',
           methods=['GET'])
def api_scoped_review_get(scan_id, scan_name):
    """Return the review for the active run.

    On a fresh scan with no review.json yet, synthesises the initial
    schema (every bbox `{status: untouched}`) without persisting.
    """
    gate = _scoped_require_login()
    if gate:
        return gate
    gate = _scoped_require_project(scan_id)
    if gate:
        return gate
    gate = _scoped_require_scan(scan_id, scan_name)
    if gate:
        return gate

    rid, rdir, err = _review_require_active_run(scan_id, scan_name)
    if err is not None:
        return err

    rv = _review_load_or_init(scan_id, scan_name, rdir)
    return jsonify({
        'run_id': rid,
        'review': rv,
    }), 200


@app.route('/api/project/<int:scan_id>/scan/<scan_name>/review',
           methods=['PATCH'])
def api_scoped_review_patch(scan_id, scan_name):
    """Apply a single mutation to the review.

    Body shape (one of):
      - `{bbox_id: "bbox_4", status: "deleted", reason?: "..."}`
      - `{bbox_id: "bbox_4", class_override: "armchair"}`
      - `{bbox_id: "bbox_4", image_override: "best_views/4_recapture.jpg"}`
      - `{bbox_id: "bbox_4", linked_product: {...} | null}`
      - `{notes: "free text"}`

    Allow at most one mutation per call (clean audit; reject otherwise).
    """
    gate = _scoped_require_login()
    if gate:
        return gate
    gate = _scoped_require_project(scan_id)
    if gate:
        return gate
    gate = _scoped_require_scan(scan_id, scan_name)
    if gate:
        return gate

    rid, rdir, err = _review_require_active_run(scan_id, scan_name)
    if err is not None:
        return err

    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'error': 'body must be a JSON object'}), 400

    rv = _review_load_or_init(scan_id, scan_name, rdir)

    # Identify which mutation is being requested. Exactly one allowed.
    bbox_mutations = ('status', 'class_override', 'image_override',
                      'linked_product')
    has_bbox_id = 'bbox_id' in data
    bbox_field_count = sum(1 for k in bbox_mutations if k in data)
    has_notes = 'notes' in data

    if has_notes and (has_bbox_id or bbox_field_count):
        return jsonify({'error': 'only one mutation per call'}), 400
    if has_bbox_id and bbox_field_count != 1:
        return jsonify({
            'error': 'exactly one of status/class_override/image_override/'
                     'linked_product must be set with bbox_id',
        }), 400
    if not has_bbox_id and bbox_field_count:
        return jsonify({'error': 'bbox_id required for bbox mutations'}), 400
    if not has_bbox_id and not has_notes:
        return jsonify({'error': 'no mutation specified'}), 400

    # Validate bbox_id against the ACTIVE run's result.json: a PATCH that
    # invents a ghost id would otherwise write `review.bboxes["bbox_99"]`
    # without ever being rendered (the renderer drops unknown ids).
    if has_bbox_id:
        result = _review_load_result(rdir)
        valid_bbox_ids = set()
        if isinstance(result, dict):
            for f in result.get('furniture') or []:
                if isinstance(f, dict):
                    fid = f.get('id')
                    if isinstance(fid, str) and fid:
                        valid_bbox_ids.add(fid)
        target_id = data.get('bbox_id')
        if target_id not in valid_bbox_ids:
            return jsonify({
                'error': f'unknown bbox_id: {target_id}',
                'valid_count': len(valid_bbox_ids),
            }), 400

    # BE-3: serialise the read-modify-write phase per (scan_id, scan_name)
    # so concurrent PATCHes don't clobber each other.
    with _review_lock_for(scan_id, scan_name):
        rv = _review_load_or_init(scan_id, scan_name, rdir)
        try:
            if has_notes:
                rv = _review.set_notes(rv, data.get('notes'))
            else:
                bid = data['bbox_id']
                if not isinstance(bid, str) or not bid:
                    return jsonify({'error': 'bbox_id must be a non-empty string'}), 400
                if 'status' in data:
                    status = data['status']
                    # Pass any extra bookkeeping fields (e.g. reason) through.
                    extras = {k: v for k, v in data.items()
                              if k not in ('bbox_id', 'status')}
                    rv = _review.set_bbox_status(rv, bid, status, **extras)
                elif 'class_override' in data:
                    rv = _review.set_class_override(rv, bid, data['class_override'])
                elif 'image_override' in data:
                    rv = _review.set_image_override(rv, bid, data['image_override'])
                elif 'linked_product' in data:
                    rv = _review.set_linked_product(rv, bid, data['linked_product'])
        except (ValueError, TypeError) as e:
            return jsonify({'error': str(e)}), 400

        _review.write_review(rdir, rv)
    return jsonify({'ok': True, 'run_id': rid, 'review': rv}), 200


@app.route('/api/project/<int:scan_id>/scan/<scan_name>/review/merge',
           methods=['POST'])
def api_scoped_review_merge(scan_id, scan_name):
    """Atomic merge of N bboxes into one primary.

    Body: `{primary_id, member_ids: [...], chosen_class?}`.
    """
    gate = _scoped_require_login()
    if gate:
        return gate
    gate = _scoped_require_project(scan_id)
    if gate:
        return gate
    gate = _scoped_require_scan(scan_id, scan_name)
    if gate:
        return gate

    rid, rdir, err = _review_require_active_run(scan_id, scan_name)
    if err is not None:
        return err

    data = request.get_json(force=True, silent=True) or {}
    primary_id = data.get('primary_id')
    member_ids = data.get('member_ids')
    chosen_class = data.get('chosen_class')

    result = _review_load_result(rdir)
    if result is None:
        return jsonify({'error': 'result.json missing for run'}), 404

    # BE-3: per-scan review-write lock around read-modify-write.
    with _review_lock_for(scan_id, scan_name):
        rv = _review_load_or_init(scan_id, scan_name, rdir)
        existing_bxs = rv.get('bboxes') or {}

        try:
            delta = _review.merge_bboxes(
                result, primary_id, member_ids,
                chosen_class=chosen_class,
                existing_review=existing_bxs)
        except TypeError as e:
            return jsonify({'error': str(e)}), 400
        except ValueError as e:
            msg = str(e)
            # Re-merge of an already-merged member into a DIFFERENT primary
            # is a state conflict, not a malformed request: surface as 409.
            if 'already merged into' in msg:
                return jsonify({'error': msg}), 409
            return jsonify({'error': msg}), 400

        bxs = dict(existing_bxs)
        # The delta's `merged_from` list contains only the NEW members for this
        # call (idempotent skips were stripped by review.merge_bboxes). Fold
        # them into the primary's existing list rather than overwriting, so a
        # follow-up merge of additional members onto the same primary keeps
        # earlier members intact.
        primary_state = dict(delta.get(primary_id) or {})
        new_members = list(primary_state.get('merged_from') or [])
        prior_primary = bxs.get(primary_id)
        if isinstance(prior_primary, dict):
            prior_members = prior_primary.get('merged_from')
            if isinstance(prior_members, list) and prior_members:
                seen = set(prior_members)
                combined = list(prior_members)
                for m in new_members:
                    if m not in seen:
                        combined.append(m)
                        seen.add(m)
                primary_state['merged_from'] = combined
        cur_primary = dict(bxs.get(primary_id) or {})
        cur_primary.update(primary_state)
        bxs[primary_id] = cur_primary
        for bid, state in delta.items():
            if bid == primary_id:
                continue
            cur = dict(bxs.get(bid) or {})
            cur.update(state)
            bxs[bid] = cur
        rv['bboxes'] = bxs
        _review.write_review(rdir, rv)

    return jsonify({
        'ok': True,
        'run_id': rid,
        'review': rv,
        'merged': {'primary_id': primary_id, 'members': member_ids},
    }), 200


@app.route('/api/project/<int:scan_id>/scan/<scan_name>/review/recapture/'
           '<int:idx>', methods=['POST'])
def api_scoped_review_recapture(scan_id, scan_name, idx):
    """Grab one Brio frame for `best_images[idx]` and persist as
    `best_views/<idx>_recapture.jpg`. Sets `image_override` on the
    matching bbox in review.json.

    409 when a recording is in flight (camera_node.py owns /dev/video0).
    503 when ffmpeg fails (busy / missing / disk full).
    """
    gate = _scoped_require_login()
    if gate:
        return gate
    gate = _scoped_require_project(scan_id)
    if gate:
        return gate
    gate = _scoped_require_scan(scan_id, scan_name)
    if gate:
        return gate

    # Plan §7c: hard guard against recording (incl. the 'starting' grace
    # window — `camera_node.py` holds /dev/video0 exclusively).
    # BACKEND-I1: this OUTER check is a fast-fail for the obvious case;
    # the authoritative check is repeated INSIDE `_recapture_lock_for`
    # below, so a `POST /start_recording` racing in between can't flip
    # `starting=True` between our check and the ffmpeg invocation.
    if _is_recording() or _active_recording.get('starting'):
        return jsonify({
            'error': 'camera in use by recording',
        }), 409

    rid, rdir, err = _review_require_active_run(scan_id, scan_name)
    if err is not None:
        return err

    result = _review_load_result(rdir)
    if result is None:
        return jsonify({'error': 'result.json missing for run'}), 404

    best_images = result.get('best_images') or []
    if not (0 <= idx < len(best_images)):
        return jsonify({'error': f'no best_image at idx {idx}'}), 404
    bbox_id = best_images[idx].get('bbox_id')
    if not isinstance(bbox_id, str) or not bbox_id:
        return jsonify({'error': 'best_image has no bbox_id'}), 500

    bv_dir = os.path.join(rdir, 'best_views')
    os.makedirs(bv_dir, exist_ok=True)
    rel = os.path.join('best_views', f'{idx}_recapture.jpg')
    dst = os.path.join(rdir, rel)

    # Serialise concurrent recaptures on the same (scan_id, scan_name) so
    # ffmpeg's `-y` overwrite + review.json write don't race when two
    # POSTs land at once.
    with _recapture_lock_for(scan_id, scan_name):
        # BACKEND-I1: re-check the recording flags INSIDE the lock so a
        # concurrent /start_recording that flipped `starting=True`
        # between the outer check and now doesn't see ffmpeg fight
        # camera_node for /dev/video0. We don't take `_record_lock` —
        # that would couple this subsystem to the recording one — but
        # the lock-then-recheck pattern is sufficient for the narrow
        # window we care about.
        if _is_recording() or _active_recording.get('starting'):
            return jsonify({
                'error': 'camera in use by recording',
            }), 409
        ok, tail = _review_brio_snapshot(dst)
        if not ok:
            # Best-effort: clean up a partial file so a subsequent retry
            # gets a clean slate.
            try:
                if os.path.isfile(dst):
                    os.remove(dst)
            except OSError:
                pass
            return jsonify({
                'error': 'recapture failed',
                'stderr_tail': tail,
            }), 503

        # Persist the override on review.json.
        # BE-3: layer the review-write lock under the recapture lock so
        # the JSON-write phase serialises against PATCH / merge / mark.
        with _review_lock_for(scan_id, scan_name):
            rv = _review_load_or_init(scan_id, scan_name, rdir)
            try:
                rv = _review.set_image_override(rv, bbox_id, rel)
            except (ValueError, TypeError) as e:
                return jsonify({'error': str(e)}), 500
            _review.write_review(rdir, rv)

        size = 0
        try:
            size = os.path.getsize(dst)
        except OSError:
            pass

    return jsonify({
        'ok': True,
        'run_id': rid,
        'bbox_id': bbox_id,
        'path': rel,
        'size_bytes': size,
    }), 200


@app.route('/api/project/<int:scan_id>/scan/<scan_name>/review/mark_reviewed',
           methods=['POST'])
def api_scoped_review_mark_reviewed(scan_id, scan_name):
    """Stamp `reviewed_at = now()` on the review. Idempotent."""
    gate = _scoped_require_login()
    if gate:
        return gate
    gate = _scoped_require_project(scan_id)
    if gate:
        return gate
    gate = _scoped_require_scan(scan_id, scan_name)
    if gate:
        return gate

    rid, rdir, err = _review_require_active_run(scan_id, scan_name)
    if err is not None:
        return err

    # BE-3: per-scan review-write lock.
    with _review_lock_for(scan_id, scan_name):
        rv = _review_load_or_init(scan_id, scan_name, rdir)
        rv = _review.mark_reviewed(rv)
        _review.write_review(rdir, rv)
    return jsonify({'ok': True, 'run_id': rid, 'review': rv}), 200


@app.route('/api/project/<int:scan_id>/scan/<scan_name>/preview_reviewed.json',
           methods=['GET'])
def api_scoped_review_preview(scan_id, scan_name):
    """Return the rendered reviewed envelope WITHOUT writing to disk."""
    gate = _scoped_require_login()
    if gate:
        return gate
    gate = _scoped_require_project(scan_id)
    if gate:
        return gate
    gate = _scoped_require_scan(scan_id, scan_name)
    if gate:
        return gate

    rid, rdir, err = _review_require_active_run(scan_id, scan_name)
    if err is not None:
        return err

    result = _review_load_result(rdir)
    if result is None:
        return jsonify({'error': 'result.json missing for run'}), 404

    rv = _review_load_or_init(scan_id, scan_name, rdir)
    rendered = _review.render_reviewed(result, rv)
    return jsonify(rendered), 200


@app.route('/api/project/<int:scan_id>/scan/<scan_name>/review/runs',
           methods=['GET'])
def api_scoped_review_runs(scan_id, scan_name):
    """List every run on disk under this scan + which one is active.

    Read-only inventory: walks `runs/` and reads each run's `meta.json`
    (best-effort — missing meta is tolerated).
    """
    gate = _scoped_require_login()
    if gate:
        return gate
    gate = _scoped_require_project(scan_id)
    if gate:
        return gate
    gate = _scoped_require_scan(scan_id, scan_name)
    if gate:
        return gate

    rdir = _projects.runs_dir(scan_id, scan_name)
    active = _projects.read_active_run(scan_id, scan_name)
    out = []
    if os.path.isdir(rdir):
        for entry in sorted(os.listdir(rdir)):
            full = os.path.join(rdir, entry)
            if not os.path.isdir(full):
                continue
            meta_path = os.path.join(full, 'meta.json')
            meta = None
            if os.path.isfile(meta_path):
                try:
                    with open(meta_path, 'r') as f:
                        meta = json.load(f)
                except (OSError, json.JSONDecodeError):
                    meta = None
            review_path = os.path.join(full, _review.REVIEW_FILENAME)
            reviewed_at = None
            submitted_at = None
            if os.path.isfile(review_path):
                try:
                    with open(review_path, 'r') as f:
                        rv_data = json.load(f)
                    if isinstance(rv_data, dict):
                        reviewed_at = rv_data.get('reviewed_at')
                        submitted_at = rv_data.get('submitted_at')
                except (OSError, json.JSONDecodeError):
                    pass
            out.append({
                'run_id': entry,
                'is_active': entry == active,
                'meta': meta or {},
                'reviewed_at': reviewed_at,
                'submitted_at': submitted_at,
                'has_result': os.path.isfile(os.path.join(full, 'result.json')),
            })
    return jsonify({'active_run_id': active, 'runs': out}), 200


@app.route('/api/project/<int:scan_id>/scan/<scan_name>/review/active_run',
           methods=['POST'])
def api_scoped_review_set_active_run(scan_id, scan_name):
    """Switch the active run pointer. Body `{run_id}`.

    Does NOT mutate the previous run's review — each run owns its own.
    """
    gate = _scoped_require_login()
    if gate:
        return gate
    gate = _scoped_require_project(scan_id)
    if gate:
        return gate
    gate = _scoped_require_scan(scan_id, scan_name)
    if gate:
        return gate

    data = request.get_json(force=True, silent=True) or {}
    new_rid = data.get('run_id')
    if not isinstance(new_rid, str) or not new_rid:
        return jsonify({'error': 'run_id must be a non-empty string'}), 400

    target = _projects.processed_dir_for_run(scan_id, scan_name, new_rid)
    if not os.path.isdir(target):
        return jsonify({'error': 'run not found'}), 404

    _projects.set_active_run(scan_id, scan_name, new_rid)
    return jsonify({'ok': True, 'active_run_id': new_rid}), 200


@app.route('/api/project/<int:scan_id>/scan/<scan_name>/runs/<run_id>',
           methods=['DELETE'])
def api_scoped_review_delete_run(scan_id, scan_name, run_id):
    """Delete a run's directory.

    Refuses if `run_id == active_run_id` OR `review.json.submitted_at` is
    non-null (audit trail). Otherwise `shutil.rmtree`.

    BACKEND-I2: takes the per-scan_id submit lock (defined in the Step 6
    block) so a delete can't race a foreground POST /submit — they read
    the same `runs[*].run_dir` and we want one to wait for the other.
    """
    gate = _scoped_require_login()
    if gate:
        return gate
    gate = _scoped_require_project(scan_id)
    if gate:
        return gate
    gate = _scoped_require_scan(scan_id, scan_name)
    if gate:
        return gate

    if not isinstance(run_id, str) or not run_id:
        return jsonify({'error': 'run_id required'}), 400

    target = _projects.processed_dir_for_run(scan_id, scan_name, run_id)
    if not os.path.isdir(target):
        return jsonify({'error': 'run not found'}), 404

    with _submit_lock_for(scan_id):
        # Re-check existence inside the lock — a parallel delete may have
        # raced us between the early `isdir` and now.
        if not os.path.isdir(target):
            return jsonify({'error': 'run not found'}), 404

        active = _projects.read_active_run(scan_id, scan_name)
        if active == run_id:
            return jsonify({'error': 'cannot delete active run'}), 409

        rv = _review.read_review(target)
        if isinstance(rv, dict) and rv.get('submitted_at'):
            return jsonify({'error': 'cannot delete a submitted run'}), 409

        shutil.rmtree(target, ignore_errors=False)
    return jsonify({'ok': True}), 200


# ===========================================================================
# === End technician review routes ==========================================
# ===========================================================================


# ===========================================================================
# === Submit pipeline routes (Step 6, plan §8 / §21 / §22c) =================
# ===========================================================================
#
# Wraps the new `submit.py` module. Pure helpers live there; this block is
# the Flask glue: gating, render-every-run, multipart upload, complete_scan,
# post-submit hook, queue-on-network-failure.
#
# Atomic call sequence on POST /submit:
#   1. 401 / 404 / 400 gates (login → project → gating_message).
#   2. For each scan: render_run_outputs → write reviewed + upload JSONs.
#   3. For each scan: submit_run_outputs → POST multipart bundle (or
#      no-op when upload_endpoint is None).
#      - SubmitNetworkError → set submit_pending=True, return 202.
#   4. complete_scan upstream call (existing 3-retry profile).
#      - AthathiError(0) → set submit_pending=True, return 202.
#      - 5xx after retries → set submit_pending=True, return 502.
#   5. run_post_submit_hook (best-effort; failures logged not blocking).
#   6. stamp_submit_outcome → manifest.submitted_at + per-run review.json.
#
# Idempotency: if manifest.submitted_at is already set, return 200 with
# `already_submitted_at` instead of re-submitting (per plan §8 idempotency).
# ===========================================================================

import submit as _submit          # noqa: E402  — late, scoped block.


# ---------------------------------------------------------------------------
# BACKEND-B3: per-scan_id submit lock — race-free idempotency check
# ---------------------------------------------------------------------------
# Two concurrent POST /submit requests for the same scan_id would both pass
# the `manifest.submitted_at == None` check, both upload, both complete_scan.
# Mirroring `_recapture_lock_for`, we serialise the entire submit body with a
# per-scan_id lock. The same lock is exposed to `submit_pending_retry` (via
# `lock_provider`) so the periodic sweep can't race the foreground submit,
# and it is taken by `delete_run` (BACKEND-I2) to keep run-delete out of the
# submit critical section.

_submit_locks_outer = threading.Lock()
_submit_locks = {}  # scan_id:int -> threading.Lock()


def _submit_lock_for(scan_id):
    """Return a per-scan_id lock. Lazy-initialised under the outer lock."""
    sid = int(scan_id)
    with _submit_locks_outer:
        lock = _submit_locks.get(sid)
        if lock is None:
            lock = threading.Lock()
            _submit_locks[sid] = lock
        return lock


def _submit_token_or_401():
    """Return the token or a 401 tuple. Mirrors `_athathi_token_or_401`."""
    if not auth.is_logged_in():
        return jsonify({'error': 'not logged in'}), 401
    return auth.read_token()


def _submit_username():
    """Best-effort technician username for stamping the upload envelope."""
    payload = auth.read_auth() or {}
    name = payload.get('username')
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


@app.route('/api/project/<int:scan_id>/submit', methods=['POST'])
def api_submit_project(scan_id):
    """Submit pipeline — render every reviewed run, upload, complete, hook.

    See block header for the full atomic sequence.

    BACKEND-B3: the entire body runs under `_submit_lock_for(scan_id)` so
    two concurrent POSTs can't both pass the `submitted_at == None` check.
    The first wins; the second sees `already_submitted_at` and short-circuits.
    """
    tok = _submit_token_or_401()
    if not isinstance(tok, str):
        return tok

    gate = _scoped_require_project(scan_id)
    if gate:
        return gate

    with _submit_lock_for(scan_id):
        return _api_submit_project_locked(scan_id, tok)


def _api_submit_project_locked(scan_id, tok):
    """Locked body of `api_submit_project`. See header for the sequence."""
    manifest = _projects.read_manifest(scan_id)

    # 7. Idempotency — already submitted is a 200 no-op for the upload +
    # complete_scan path. But per plan §22c / §8 step 4, if the prior
    # post-submit hook failed, re-submitting MUST re-run the hook so an
    # operator who fixed the underlying problem (missing tool, bad
    # credentials, etc.) can drive it green without manually editing the
    # manifest. If the prior hook was 'ok' (or no hook is configured),
    # this path is a true no-op.
    already = manifest.get('submitted_at') if isinstance(manifest, dict) else None
    if isinstance(already, str) and already:
        cfg_idem = auth.load_config() or {}
        hook_command_idem = cfg_idem.get('post_submit_hook')
        prior_hook_status = (
            manifest.get('post_submit_hook_status')
            if isinstance(manifest, dict) else None
        )
        prior_hook_log = (
            manifest.get('post_submit_hook_log')
            if isinstance(manifest, dict) else None
        )
        # Statuses we treat as "needs re-run" — anything that looks like
        # a prior failure (the route writes 'ok'/'failed'; defensive
        # against synonyms an operator may have edited in by hand or
        # that earlier code might have written).
        _failure_statuses = {'failed', 'error', 'timeout', 'hook_failed'}
        should_rehook = bool(
            hook_command_idem
            and isinstance(prior_hook_status, str)
            and prior_hook_status.lower() in _failure_statuses
        )
        if not should_rehook:
            return jsonify({
                'ok': True,
                'already_submitted_at': already,
                'rehooked': False,
                'post_submit_hook_status': prior_hook_status,
                'post_submit_hook_log': prior_hook_log,
            }), 200

        # Re-run the hook only.
        project_dir_idem = _projects.project_dir(scan_id)
        rehook_result = _submit.run_post_submit_hook(
            hook_command_idem, project_dir_idem, scan_id,
        )
        new_status = prior_hook_status
        new_log = prior_hook_log
        if rehook_result.get('ran') or rehook_result.get('error'):
            try:
                m_re = _projects.read_manifest(scan_id) or {}
                new_log = {
                    'ok': rehook_result.get('ok'),
                    'returncode': rehook_result.get('returncode'),
                    'stdout_tail': rehook_result.get('stdout_tail', '')[:4096],
                    'stderr_tail': rehook_result.get('stderr_tail', '')[:4096],
                    'error': rehook_result.get('error'),
                }
                new_status = 'ok' if rehook_result.get('ok') else 'failed'
                m_re['post_submit_hook_log'] = new_log
                m_re['post_submit_hook_status'] = new_status
                _projects.write_manifest(scan_id, m_re)
            except OSError as e:
                _log(f'api_submit_project: rehook log write failed: {e!r}')
        return jsonify({
            'ok': True,
            'already_submitted_at': already,
            'rehooked': True,
            'hook': rehook_result,
            'post_submit_hook_status': new_status,
            'post_submit_hook_log': new_log,
        }), 200

    # 3. Gating.
    reason = _submit.gating_message(scan_id)
    if reason:
        return jsonify({'error': reason}), 400

    runs = _submit.gather_runs_for_submit(scan_id)
    cfg = auth.load_config() or {}
    upload_endpoint = cfg.get('upload_endpoint')
    image_transport = cfg.get('image_transport') or 'multipart'
    hook_command = cfg.get('post_submit_hook')
    technician = _submit_username()

    # 4. Render every reviewed run; collect bundles.
    bundles = []  # list of {run_dir, upload_path, image_files, scan_name}
    for entry in runs:
        run_dir = entry.get('run_dir')
        if not run_dir or not os.path.isdir(run_dir):
            return jsonify({
                'error': f"{entry['scan_name']} has no run dir on disk",
            }), 500
        try:
            rendered = _submit.render_run_outputs(run_dir, technician)
        except FileNotFoundError as e:
            return jsonify({
                'error': f"{entry['scan_name']}: result.json missing ({e})",
            }), 500
        bundles.append({
            'scan_name': entry['scan_name'],
            'run_dir': run_dir,
            'run_id': entry['run_id'],
            'upload_path': rendered['upload_path'],
            'image_files': rendered['image_files'],
        })

    # 6a. Upload each bundle. Network failure → queue.
    upload_responses = []
    for b in bundles:
        try:
            outcome = _submit.submit_run_outputs(
                tok, upload_endpoint,
                envelope_path=b['upload_path'],
                image_files=b['image_files'],
                image_transport=image_transport,
            )
        except _submit.SubmitNetworkError as e:
            _submit.stamp_submit_outcome(
                scan_id, runs=runs, response=None,
                error=f'upload network failure on {b["scan_name"]}: {e!s}',
                queued=True,
            )
            # BACKEND-B2: tag the stage so the retry sweep re-uploads
            # before re-completing — Athathi never received the bundle.
            # Persist the names of scans whose bundles already uploaded
            # so the retry sweep does NOT re-upload them (which would
            # double-bill Athathi for everything before the failure).
            try:
                m_stage = _projects.read_manifest(scan_id) or {}
                m_stage['submit_pending_stage'] = 'upload'
                m_stage['submit_pending_uploads'] = [
                    r['scan_name'] for r in upload_responses
                ]
                _projects.write_manifest(scan_id, m_stage)
            except OSError as _e:
                _log(f'api_submit_project: stage stamp failed: {_e!r}')
            return jsonify({
                'queued': True,
                'reason': 'no network',
                'scan_name': b['scan_name'],
            }), 202
        except athathi_proxy.AthathiError as e:
            # Non-network upload failure — surface verbatim.
            tail = (e.body or '')[:200]
            return jsonify({
                'error': f'upload failed for {b["scan_name"]}: {e!s}',
                'upstream_status': e.status_code,
                'upstream_body_tail': tail,
            }), 502
        upload_responses.append({
            'scan_name': b['scan_name'],
            'outcome': outcome,
        })

    # 6b. complete_scan call.
    completed_at = None
    completed_response = None
    if upload_endpoint or any(r['outcome'].get('uploaded') for r in upload_responses):
        # Always call complete when an endpoint is configured; also when
        # NOT configured we still want the back-office to know the scan
        # is done — that's the point of the proxy.
        pass
    try:
        completed_response = athathi_proxy.complete_scan(tok, scan_id)
        completed_at = datetime.utcnow().isoformat() + 'Z'
    except athathi_proxy.AthathiError as e:
        if e.status_code == 0:
            _submit.stamp_submit_outcome(
                scan_id, runs=runs, response=None,
                error=f'complete network failure: {e!s}',
                queued=True,
            )
            # BACKEND-B2: upload already succeeded, tag stage='complete'
            # so the retry sweep doesn't re-push the multi-MB bundle.
            try:
                m_stage = _projects.read_manifest(scan_id) or {}
                m_stage['submit_pending_stage'] = 'complete'
                _projects.write_manifest(scan_id, m_stage)
            except OSError as _e:
                _log(f'api_submit_project: stage stamp failed: {_e!r}')
            return jsonify({
                'queued': True,
                'reason': 'no network',
            }), 202
        if 500 <= e.status_code < 600:
            _submit.stamp_submit_outcome(
                scan_id, runs=runs, response=None,
                error=f'complete 5xx: {e!s}',
                queued=True,
            )
            try:
                m_stage = _projects.read_manifest(scan_id) or {}
                m_stage['submit_pending_stage'] = 'complete'
                _projects.write_manifest(scan_id, m_stage)
            except OSError as _e:
                _log(f'api_submit_project: stage stamp failed: {_e!r}')
            return jsonify({
                'error': 'upstream error on complete',
                'upstream_status': e.status_code,
                'upstream_body_tail': (e.body or '')[:200],
            }), 502
        if e.status_code == 401:
            auth.clear_token()
            return jsonify({'error': 'unauthorized'}), 401
        return jsonify({
            'error': str(e),
            'upstream_status': e.status_code,
            'upstream_body_tail': (e.body or '')[:200],
        }), e.status_code if e.status_code >= 400 else 502

    # 6c. Post-submit hook (best-effort).
    project_dir = _projects.project_dir(scan_id)
    hook_result = _submit.run_post_submit_hook(
        hook_command, project_dir, scan_id,
    )
    # Persist hook log on the manifest (truncated).
    if hook_result.get('ran') or hook_result.get('error'):
        try:
            m = _projects.read_manifest(scan_id) or {}
            m['post_submit_hook_log'] = {
                'ok': hook_result.get('ok'),
                'returncode': hook_result.get('returncode'),
                'stdout_tail': hook_result.get('stdout_tail', '')[:4096],
                'stderr_tail': hook_result.get('stderr_tail', '')[:4096],
                'error': hook_result.get('error'),
            }
            m['post_submit_hook_status'] = (
                'ok' if hook_result.get('ok') else 'failed'
            )
            _projects.write_manifest(scan_id, m)
        except OSError as e:
            _log(f'api_submit_project: hook-log write failed: {e!r}')

    # 6d. Stamp success on disk.
    _submit.stamp_submit_outcome(
        scan_id, runs=runs, response=completed_response,
        error=None, queued=False,
    )

    return jsonify({
        'ok': True,
        'completed_at': completed_at,
        'hook': hook_result,
        'uploads': upload_responses,
        'response': completed_response,
    }), 200


@app.route('/api/project/<int:scan_id>/submit/preview', methods=['GET'])
def api_submit_preview(scan_id):
    """Dry-run: render every scan WITHOUT contacting upstream.

    Returns `{scans: [{scan_name, run_id, reviewed_size, upload_size,
    n_images}, ...]}`.
    """
    gate = _scoped_require_login()
    if gate:
        return gate
    gate = _scoped_require_project(scan_id)
    if gate:
        return gate

    technician = _submit_username()
    runs = _submit.gather_runs_for_submit(scan_id)
    out = []
    for entry in runs:
        run_dir = entry.get('run_dir')
        if not run_dir or not os.path.isdir(run_dir):
            out.append({
                'scan_name': entry.get('scan_name'),
                'run_id': entry.get('run_id'),
                'error': 'no run dir',
            })
            continue
        try:
            rendered = _submit.render_run_outputs(run_dir, technician)
        except FileNotFoundError as e:
            out.append({
                'scan_name': entry.get('scan_name'),
                'run_id': entry.get('run_id'),
                'error': f'result.json missing: {e}',
            })
            continue
        try:
            reviewed_size = os.path.getsize(rendered['reviewed_path'])
        except OSError:
            reviewed_size = None
        try:
            upload_size = os.path.getsize(rendered['upload_path'])
        except OSError:
            upload_size = None
        out.append({
            'scan_name': entry.get('scan_name'),
            'run_id': entry.get('run_id'),
            'reviewed_size': reviewed_size,
            'upload_size': upload_size,
            'n_images': len(rendered['image_files']),
        })
    return jsonify({'scans': out}), 200


@app.route('/api/project/<int:scan_id>/submit/retry', methods=['POST'])
def api_submit_retry(scan_id):
    """Retry queued submits. Walks every project marked `submit_pending=True`.

    The `scan_id` in the URL is informational — the retry sweep walks all
    pending projects. We accept the param so the frontend can name the
    project that triggered the retry, but the action is global.
    """
    tok = _submit_token_or_401()
    if not isinstance(tok, str):
        return tok

    def _provider():
        return tok

    # BACKEND-B2: thread the providers needed for stage='upload' retries
    # (re-render + re-upload before re-completing).
    def _endpoint_provider():
        cfg = auth.load_config() or {}
        return cfg.get('upload_endpoint')

    def _technician_provider():
        return _submit_username()

    cfg_now = auth.load_config() or {}
    transport = cfg_now.get('image_transport') or 'multipart'

    results = _submit.submit_pending_retry(
        _provider,
        upload_endpoint_provider=_endpoint_provider,
        technician_provider=_technician_provider,
        image_transport=transport,
        lock_provider=_submit_lock_for,
    )
    return jsonify({'results': results}), 200


# ---------------------------------------------------------------------------
# Settings: upload_filter
# ---------------------------------------------------------------------------

@app.route('/api/settings/upload_filter', methods=['GET'])
def api_settings_upload_filter_get():
    """Return the on-disk upload filter (or the default if missing)."""
    gate = _scoped_require_login()
    if gate:
        return gate
    return jsonify(_review.load_filter()), 200


@app.route('/api/settings/upload_filter', methods=['PATCH'])
def api_settings_upload_filter_patch():
    """Replace the upload filter on disk. Body is the full filter dict."""
    gate = _scoped_require_login()
    if gate:
        return gate
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'body must be a JSON object'}), 400
    try:
        _review.save_filter(data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except OSError as e:
        return jsonify({'error': f'write failed: {e!s}'}), 500
    return jsonify({'ok': True, 'filter': _review.load_filter()}), 200


# ===========================================================================
# === End submit pipeline routes ============================================
# ===========================================================================


# ===========================================================================
# === Step 7 taxonomy + visual-search persistence ===========================
# ===========================================================================
#
# Adds the auto-grown class taxonomy (plan §7d) and the bbox-card
# "Find product" visual-search persistence layer (plan §20). The Athathi
# visual-search proxy (`POST /api/athathi/visual-search/search-full`) is
# Step 2's; this block adds:
#
#   GET    /api/taxonomy/classes
#   POST   /api/taxonomy/learned
#   POST   /api/project/<scan_id>/scan/<scan_name>/review/find_product/<idx>
#   POST   /api/project/<scan_id>/scan/<scan_name>/review/link_product
#   GET    /api/visual-search/cache/<sha1>
#   DELETE /api/visual-search/cache
#
# `find_product` resolves the bbox's on-disk image (recapture preferred
# over original — same logic as `submit.build_image_files_for_upload`),
# computes sha1(file_bytes), and consults
# `<auth.ATHATHI_DIR>/cache/visual_search/<sha1>.json` before forwarding
# to Athathi. TTL comes from `auth.load_config()['visual_search_cache_ttl_s']`
# (default 86400 from Step 1's config). `0` disables the cache entirely.
#
# `link_product` writes the technician's chosen product (or `null` for
# "no match") into review.json via `review.set_linked_product`, stamping
# `linked_at` + `linked_by` from `auth.read_auth()` first.
#
# All routes auth-gate via `_scoped_require_login` / `_athathi_token_or_401`.
# ===========================================================================

import hashlib as _hashlib   # noqa: E402 — late, scoped block.
import taxonomy as _taxonomy  # noqa: E402 — late, scoped block.


# ---------------------------------------------------------------------------
# Visual-search cache helpers
# ---------------------------------------------------------------------------

def _vs_cache_dir():
    """`<auth.ATHATHI_DIR>/cache/visual_search/`. Created on demand."""
    d = os.path.join(auth.ATHATHI_DIR, 'cache', 'visual_search')
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _vs_cache_path(sha1):
    """Sanitised cache file path for a sha1 hex string."""
    safe = ''.join(c if (c.isalnum() or c in '_-.') else '_' for c in sha1)
    return os.path.join(_vs_cache_dir(), safe + '.json')


def _vs_token_salt():
    """Return the per-technician cache salt (`sha256(tok)[:8]` or `'anon'`).

    BACKEND-B1: the visual-search cache is keyed `<sha1>__<salt>` so two
    technicians on the same Pi (rare, but possible) don't see each other's
    upstream results. We reuse `athathi_proxy._cache_token_suffix`, which
    is the existing per-token namespacing pattern used elsewhere in the
    codebase (its bytes-of-token-sha256 prefix is unique per token, where a
    raw `tok[:8]` would collide on the JWT header). The flush route stays
    global by design.
    """
    tok = auth.read_token() or ''
    return athathi_proxy._cache_token_suffix(tok)


def _vs_salted_key(sha1):
    """Compose the on-disk cache key as `<sha1>__<salt>` (per-technician)."""
    return f'{sha1}__{_vs_token_salt()}'


def _vs_cache_ttl():
    """Read the cache TTL from config (default 86400). 0 disables caching."""
    cfg = auth.load_config() or {}
    ttl = cfg.get('visual_search_cache_ttl_s')
    try:
        return int(ttl)
    except (TypeError, ValueError):
        return 86400


def _vs_cache_read(sha1):
    """Return (cached_at, blob) or (None, None) on miss / parse error."""
    p = _vs_cache_path(sha1)
    if not os.path.isfile(p):
        return None, None
    try:
        with open(p, 'r') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    ts = data.get('cached_at')
    blob = data.get('blob')
    if not isinstance(ts, (int, float)):
        return None, None
    return ts, blob


def _vs_cache_write(sha1, blob):
    """Atomic temp+rename of `{cached_at, blob}`. Best-effort on failure."""
    p = _vs_cache_path(sha1)
    parent = os.path.dirname(p) or '.'
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError:
        pass
    wrapper = {'cached_at': time.time(), 'blob': blob}
    tmp = p + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(wrapper, f)
        os.replace(tmp, p)
    except OSError as e:
        _log(f'_vs_cache_write({sha1}): {e!r}')


def _sha1_of_file(path):
    """Return the hex sha1 of a file's bytes. Streams in 64 KiB chunks."""
    h = _hashlib.sha1()
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _resolve_bbox_image(run_dir, idx):
    """Mirror `submit.build_image_files_for_upload`'s recapture-preferred picker.

    Returns the absolute path to the bbox's image at `best_images[idx]`,
    or None if no on-disk file exists.
    """
    bv_dir = os.path.join(run_dir, 'best_views')
    recap = os.path.join(bv_dir, f'{idx}_recapture.jpg')
    original = os.path.join(bv_dir, f'{idx}.jpg')
    if os.path.isfile(recap):
        return recap
    if os.path.isfile(original):
        return original
    return None


# ---------------------------------------------------------------------------
# /api/taxonomy/classes — merged auto-grown taxonomy
# ---------------------------------------------------------------------------

@app.route('/api/taxonomy/classes', methods=['GET'])
def api_taxonomy_classes():
    """Return the merged taxonomy (model + Athathi + technician).

    Strategy:
      1. Logged-in + cached fresh categories on disk → use them.
      2. Logged-in + cache stale → call `athathi_proxy.get_categories`;
         on success refresh the cache; on network failure fall back to
         the last cached snapshot (even if stale).
      3. No cache and no network → just local + learned; the Athathi
         categories are absent from the merge.
    """
    if not auth.is_logged_in():
        return jsonify({'error': 'not logged in'}), 401

    # Try fresh cache first (the brief: "logged in AND categories cached
    # fresh → uses cached"). The TTL of 1 h here is intentionally tight
    # so the dropdown reflects new admin-side categories within an hour.
    categories = _taxonomy.load_cached_athathi_categories(max_age_s=3600)
    if categories is None:
        # Cache stale or missing → try upstream.
        tok = auth.read_token()
        try:
            fetched = athathi_proxy.get_categories(tok)
        except athathi_proxy.AthathiError as e:
            # Network failure → fall back to whatever's on disk, even if
            # stale. Non-network errors (401/5xx) we tolerate by serving
            # local-only — the technician can still relabel without the
            # upstream seed.
            if e.status_code == 0:
                stale = _taxonomy.load_cached_athathi_categories(
                    max_age_s=10 ** 9)
                categories = stale  # may be None
            else:
                _log(f'api_taxonomy_classes: upstream error {e!r}')
                categories = None
        else:
            # Cache the fresh upstream snapshot for next time.
            try:
                _taxonomy.cache_athathi_categories(fetched)
            except OSError as e:
                _log(f'api_taxonomy_classes: cache_write failed {e!r}')
            categories = fetched

    classes = _taxonomy.merged_taxonomy(categories)
    return jsonify({'classes': classes}), 200


# ---------------------------------------------------------------------------
# /api/taxonomy/learned — add a free-text class
# ---------------------------------------------------------------------------

@app.route('/api/taxonomy/learned', methods=['POST'])
def api_taxonomy_learned_add():
    """Body `{name: "ottoman"}`. Bumps the count and returns the file."""
    if not auth.is_logged_in():
        return jsonify({'error': 'not logged in'}), 401
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'error': 'body must be a JSON object'}), 400
    name = data.get('name')
    if not isinstance(name, str) or not name.strip():
        return jsonify({'error': 'name must be a non-empty string'}), 400
    _taxonomy.add_learned_class(name)
    return jsonify({
        'ok': True,
        'learned_classes': _taxonomy.load_learned_classes(),
    }), 200


# ---------------------------------------------------------------------------
# /review/find_product/<idx> — visual-search a bbox's image
# ---------------------------------------------------------------------------

@app.route('/api/project/<int:scan_id>/scan/<scan_name>/review/find_product/'
           '<int:idx>', methods=['POST'])
def api_scoped_review_find_product(scan_id, scan_name, idx):
    """Visual-search the bbox image at `best_images[idx]`.

    Cache: `<ATHATHI_DIR>/cache/visual_search/<sha1>.json` with a TTL
    pulled from `config.visual_search_cache_ttl_s` (default 86400). On
    cache hit returns the cached body with `cached: true` and an
    `X-Cached: true` header.
    """
    gate = _scoped_require_login()
    if gate:
        return gate
    gate = _scoped_require_project(scan_id)
    if gate:
        return gate
    gate = _scoped_require_scan(scan_id, scan_name)
    if gate:
        return gate

    rid, rdir, err = _review_require_active_run(scan_id, scan_name)
    if err is not None:
        return err

    image_path = _resolve_bbox_image(rdir, idx)
    if image_path is None:
        return jsonify({
            'error': f'no image on disk for best_image {idx}',
        }), 404

    try:
        sha1 = _sha1_of_file(image_path)
    except OSError as e:
        return jsonify({'error': f'failed to hash image: {e!s}'}), 500

    # BACKEND-B1: salt with `tok[:8]` so per-technician caches don't bleed.
    cache_key = _vs_salted_key(sha1)

    ttl = _vs_cache_ttl()
    if ttl > 0:
        ts, cached_blob = _vs_cache_read(cache_key)
        if cached_blob is not None and ts is not None:
            if (time.time() - ts) <= ttl:
                payload = dict(cached_blob) if isinstance(cached_blob, dict) \
                    else cached_blob
                if isinstance(payload, dict):
                    payload['cached'] = True
                resp = jsonify(payload)
                resp.headers['X-Cached'] = 'true'
                return resp, 200

    # Cache miss / disabled → forward upstream.
    tok = auth.read_token()
    try:
        body = athathi_proxy.visual_search_full(tok, image_path)
    except athathi_proxy.AthathiError as e:
        return _athathi_handle_error(e)

    # Cache the upstream response when caching is enabled. BE-5: skip the
    # cache write for empty / failed responses — caching a result-less reply
    # would lock the user into "no matches" until the TTL expires.
    if ttl > 0:
        results_list = (body.get('results') if isinstance(body, dict) else None)
        has_results = isinstance(results_list, list) and len(results_list) > 0
        upstream_error = isinstance(body, dict) and bool(body.get('error'))
        if has_results and not upstream_error:
            try:
                _vs_cache_write(cache_key, body)
            except OSError as e:
                _log(f'find_product: cache write failed: {e!r}')

    payload = dict(body) if isinstance(body, dict) else body
    if isinstance(payload, dict):
        payload['cached'] = False
    return jsonify(payload), 200


# ---------------------------------------------------------------------------
# /review/link_product — persist the technician's chosen product
# ---------------------------------------------------------------------------

@app.route('/api/project/<int:scan_id>/scan/<scan_name>/review/link_product',
           methods=['POST'])
def api_scoped_review_link_product(scan_id, scan_name):
    """Persist `{bbox_id, product: <dict|null>}` into review.json.

    Adds `linked_at` (UTC ISO-8601) and `linked_by` (technician username
    from `auth.read_auth()`) to the product dict. `product=null` is the
    "no match" path (forwarded verbatim to `review.set_linked_product`).
    """
    gate = _scoped_require_login()
    if gate:
        return gate
    gate = _scoped_require_project(scan_id)
    if gate:
        return gate
    gate = _scoped_require_scan(scan_id, scan_name)
    if gate:
        return gate

    rid, rdir, err = _review_require_active_run(scan_id, scan_name)
    if err is not None:
        return err

    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'error': 'body must be a JSON object'}), 400

    bbox_id = data.get('bbox_id')
    if not isinstance(bbox_id, str) or not bbox_id:
        return jsonify({'error': 'bbox_id must be a non-empty string'}), 400

    if 'product' not in data:
        return jsonify({'error': 'product required (dict or null)'}), 400
    product = data.get('product')
    if product is not None and not isinstance(product, dict):
        return jsonify({'error': 'product must be a dict or null'}), 400

    # Stamp metadata when the product is a dict.
    if isinstance(product, dict):
        product = dict(product)
        product['linked_at'] = (
            datetime.utcnow().isoformat() + 'Z'
        )
        ae = auth.read_auth() or {}
        username = ae.get('username')
        if isinstance(username, str) and username.strip():
            product['linked_by'] = username.strip()

    # BE-3: per-scan review-write lock around read-modify-write.
    with _review_lock_for(scan_id, scan_name):
        rv = _review_load_or_init(scan_id, scan_name, rdir)
        try:
            rv = _review.set_linked_product(rv, bbox_id, product)
        except (ValueError, TypeError) as e:
            return jsonify({'error': str(e)}), 400
        _review.write_review(rdir, rv)

    return jsonify({
        'ok': True,
        'run_id': rid,
        'bbox_id': bbox_id,
        'bbox_state': rv['bboxes'].get(bbox_id),
    }), 200


# ---------------------------------------------------------------------------
# /api/visual-search/cache — read-only blob lookup + flush
# ---------------------------------------------------------------------------

@app.route('/api/visual-search/cache/<sha1>', methods=['GET'])
def api_vs_cache_get(sha1):
    """Return the cached upstream blob, or 404 when absent.

    BACKEND-B1: the on-disk filename is `<sha1>__<tok[:8]>.json` (salted by
    technician token). The URL stays the bare `<sha1>` for client simplicity
    — we resolve the salted key server-side from the current login token.
    """
    if not auth.is_logged_in():
        return jsonify({'error': 'not logged in'}), 401
    # Always re-salt server-side from the current technician's token. We
    # used to honour an already-salted `<sha1>__<...>` URL, but that
    # silently let one technician read another's cache by supplying their
    # salt — defeating BACKEND-B1's per-user partitioning.
    key = _vs_salted_key(sha1)
    ts, blob = _vs_cache_read(key)
    if blob is None:
        return jsonify({'error': 'not cached'}), 404
    return jsonify(blob), 200


@app.route('/api/visual-search/cache', methods=['DELETE'])
def api_vs_cache_flush():
    """Remove every file under `<ATHATHI_DIR>/cache/visual_search/`."""
    if not auth.is_logged_in():
        return jsonify({'error': 'not logged in'}), 401
    d = _vs_cache_dir()
    removed = 0
    if os.path.isdir(d):
        try:
            shutil.rmtree(d)
            removed = 1
        except OSError as e:
            return jsonify({'error': f'flush failed: {e!s}'}), 500
    return jsonify({'ok': True, 'removed_dir': bool(removed)}), 200


# ===========================================================================
# === End Step 7 taxonomy + visual-search persistence =======================
# ===========================================================================


# ===========================================================================
# === Step 11 settings config route =========================================
# ===========================================================================
#
# The Settings sheet (TECHNICIAN_REVIEW_PLAN.md §16 Step 11) needs a way
# to read AND write the auth.config.json keys (api_url, upload_endpoint,
# post_submit_hook, image_transport, visual_search_cache_ttl_s, last_user).
# Step 1 wired the storage layer in `auth.py` — `auth.load_config()` and
# `auth.update_config(**kwargs)` — but no HTTP surface existed yet because
# every previous flow that needed to mutate config (login → last_user)
# went through Python directly.
#
# Both routes are auth-gated. The PATCH delegates the whitelist check to
# `auth.update_config`, which raises ValueError on unknown keys (it only
# accepts keys present in `auth.DEFAULT_CONFIG`). Trailing slashes on
# URL fields are stripped server-side by `auth.save_config`.
# ===========================================================================

@app.route('/api/settings/config', methods=['GET'])
def api_settings_config_get():
    """Return the current auth config dict (no token, no secrets)."""
    if not auth.is_logged_in():
        return jsonify({'error': 'not logged in'}), 401
    return jsonify(auth.load_config()), 200


@app.route('/api/settings/config', methods=['PATCH'])
def api_settings_config_patch():
    """Merge a partial config dict. 400 on unknown / invalid keys."""
    if not auth.is_logged_in():
        return jsonify({'error': 'not logged in'}), 401
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'body must be a JSON object'}), 400
    if not data:
        # Nothing to do — return the current config without touching disk.
        return jsonify(auth.load_config()), 200
    try:
        merged = auth.update_config(**data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except (OSError, TypeError) as e:
        return jsonify({'error': f'write failed: {e!s}'}), 500
    return jsonify(merged), 200


# ===========================================================================
# === Step 11 fix: API URL probe ============================================
# ===========================================================================
#
# FRONTEND-B1: the Settings → Account "Test" buttons used to do a raw
# `fetch(<api_url>/api/users/me/, {mode: 'cors'})` from the browser. That
# bypasses `AppShell.fetchJson` (the plan §-1 contract) and assumes the
# remote server is CORS-permissive — it usually isn't. We move the probe
# server-side. The browser POSTs a candidate URL; the Pi `curl`s it and
# returns `{ok, status_code, error?}`.
#
# Any 2xx-5xx response means the server is reachable (status 405 is
# common since `/api/users/me/` is a GET-only endpoint and probing
# without auth typically gives 401/403 — that still proves the URL is
# alive). Network failure / unreachable → ok=False with `error`.
# ===========================================================================

@app.route('/api/settings/probe_api_url', methods=['POST'])
def api_settings_probe_api_url():
    """Probe a candidate Athathi API URL via curl. Auth-gated."""
    if not auth.is_logged_in():
        return jsonify({'error': 'not logged in'}), 401
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'body must be a JSON object'}), 400
    raw = data.get('url')
    if not isinstance(raw, str) or not raw.strip():
        return jsonify({'error': 'url required'}), 400
    base = raw.strip().rstrip('/')
    target = base + '/api/users/me/'
    try:
        proc = subprocess.run(
            [
                'curl', '-sS',
                '-o', '/dev/null',
                '-w', '%{http_code}',
                '--connect-timeout', '5',
                '--max-time', '10',
                '-H', 'Accept: application/json',
                target,
            ],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        return jsonify({
            'ok': False, 'status_code': 0,
            'error': 'curl not installed on the Pi',
        }), 200
    except subprocess.TimeoutExpired:
        return jsonify({
            'ok': False, 'status_code': 0,
            'error': f'timed out probing {target}',
        }), 200
    except OSError as e:
        return jsonify({
            'ok': False, 'status_code': 0,
            'error': f'curl spawn failed: {e!s}',
        }), 200

    code_str = (proc.stdout or '').strip()
    try:
        code = int(code_str)
    except ValueError:
        code = 0
    if code == 0:
        # curl exited non-zero (network unreachable, DNS fail, etc).
        err = (proc.stderr or '').strip()[:200] or 'unreachable'
        return jsonify({
            'ok': False, 'status_code': 0, 'error': err,
        }), 200
    # Any HTTP response (2xx-5xx) proves the server is reachable.
    return jsonify({'ok': True, 'status_code': code}), 200


# ===========================================================================
# === End Step 11 settings config route =====================================
# ===========================================================================


if __name__ == '__main__':
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    os.makedirs(PROCESSED_DIR, exist_ok=True)
    os.makedirs(CALIBRATION_DIR, exist_ok=True)
    auth.boot_init()
    _recover_stuck_sessions()
    atexit.register(_cleanup_on_exit)

    parser = argparse.ArgumentParser(description='LiDAR SLAM Recording Web App')
    parser.add_argument('--port', type=int, default=5000)
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()

    print(f'Starting on http://0.0.0.0:{args.port}')
    print(f'Recordings dir: {RECORDINGS_DIR}')
    app.run(host='0.0.0.0', port=args.port, debug=args.debug, threaded=True)
