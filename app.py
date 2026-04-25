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
from datetime import datetime


import cv2
import yaml
from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

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
    if recording and _active_recording['start_time']:
        elapsed = round(time.time() - _active_recording['start_time'], 1)
        active_session = _active_recording['session_id']
        if _active_recording.get('camera_ok'):
            camera_recording = bool(_active_recording.get('camera_streaming'))
            camera_frames = int(_active_recording.get('camera_frames') or 0)
        else:
            camera_recording = False
            camera_frames = 0

    return jsonify({
        'network': {'ok': net_ok, 'message': net_msg},
        'lidar_reachable': lidar_reachable,
        'camera': {'ok': camera_ok, 'message': camera_msg},
        'calibrated': {
            'intrinsics': os.path.isfile(INTRINSICS_FILE),
            'extrinsics': os.path.isfile(EXTRINSICS_FILE),
        },
        'recording': recording,
        'elapsed': elapsed,
        'active_session': active_session,
        'camera_recording': camera_recording,
        'camera_frames': camera_frames,
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

            if recording and _active_recording['start_time']:
                elapsed = round(time.time() - _active_recording['start_time'], 1)
                session_id = _active_recording['session_id']
                session = _get_session(session_id) if session_id else None
                status = session.get('status', 'recording') if session else 'recording'

            # Collect processing status for active SLAM jobs.
            # Snapshot under lock so we don't race with _process_thread.
            processing = {}
            with _processing_lock:
                snapshot = {sid: dict(proc) for sid, proc in _active_processing.items()}
            for sid, proc in snapshot.items():
                stage = proc.get('stage') or proc.get('status') or ''
                processing[sid] = {
                    'status': proc.get('status'),
                    'stage': stage,
                    # `progress` is kept as a human string for back-compat
                    # with old browser tabs that read procInfo.progress.
                    'progress': stage,
                    'job_id': proc.get('job_id'),
                    'elapsed': round(time.time() - proc.get('start_time', time.time()), 1),
                }

            data = json.dumps({
                'recording': recording,
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
    stuck_slam_exact = {'compressing', 'uploading', 'queued', 'decoding', 'cancelled'}
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


if __name__ == '__main__':
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    os.makedirs(PROCESSED_DIR, exist_ok=True)
    os.makedirs(CALIBRATION_DIR, exist_ok=True)
    _recover_stuck_sessions()
    atexit.register(_cleanup_on_exit)

    parser = argparse.ArgumentParser(description='LiDAR SLAM Recording Web App')
    parser.add_argument('--port', type=int, default=5000)
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()

    print(f'Starting on http://0.0.0.0:{args.port}')
    print(f'Recordings dir: {RECORDINGS_DIR}')
    app.run(host='0.0.0.0', port=args.port, debug=args.debug, threaded=True)
