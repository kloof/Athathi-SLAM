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
import gzip
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime

import cv2
import yaml
from flask import Flask, Response, jsonify, redirect, render_template, request, send_file

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_USB_RECORDINGS = '/mnt/slam_data/recordings'
RECORDINGS_DIR = _USB_RECORDINGS if os.path.isdir('/mnt/slam_data') else os.path.join(SCRIPT_DIR, 'recordings')
SESSIONS_FILE = os.path.join(SCRIPT_DIR, 'sessions.json')

SLAM_API_URL = 'https://slam-service-348149010358.us-central1.run.app'

IFACE = 'eth0'
HOST_IP = '192.168.1.2'
LIDAR_IP = '192.168.1.62'

ROS_SETUP = '/opt/ros/humble/setup.bash'
DRIVER_SETUP = '/home/talal/unilidar_sdk2/unitree_lidar_ros2/install/setup.bash'

# Camera settings (Logitech Brio)
CAMERA_DEVICE = '/dev/video0'
CAMERA_WIDTH = 1280
CAMERA_HEIGHT = 720
CAMERA_FPS = 10
CALIBRATION_DIR = os.path.join(SCRIPT_DIR, 'calibration')
INTRINSICS_FILE = os.path.join(CALIBRATION_DIR, 'intrinsics.yaml')
EXTRINSICS_FILE = os.path.join(CALIBRATION_DIR, 'extrinsics.yaml')

app = Flask(__name__)

_LOG_FILE = '/tmp/slam_app_debug.log'
def _log(msg):
    with open(_LOG_FILE, 'a') as f:
        f.write(f'{datetime.now().isoformat()} {msg}\n')

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
_active_recording = {
    'session_id': None,
    'driver_proc': None,
    'camera_proc': None,
    'tf_proc': None,
    'bag_proc': None,
    'start_time': None,
    'starting': False,
    'camera_ok': False,
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
                _kill_process_group(_active_recording.get('camera_proc'))
                _kill_process_group(_active_recording.get('tf_proc'))
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
                _active_recording['start_time'] = None
                _active_recording['session_id'] = None
                _active_recording['camera_ok'] = False
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


def _launch_camera():
    """Launch the v4l2_camera ROS2 node for Brio."""
    _set_brio_fov(90)
    time.sleep(1)  # Wait for cameractrls to release the device

    camera_info_param = ''
    if os.path.isfile(INTRINSICS_FILE):
        camera_info_param = f'-p camera_info_url:="file://{INTRINSICS_FILE}" '

    cmd = (
        f'source {ROS_SETUP} && '
        f'ros2 run v4l2_camera v4l2_camera_node '
        f'--ros-args '
        f'-p video_device:={CAMERA_DEVICE} '
        f'-p image_size:="[{CAMERA_WIDTH},{CAMERA_HEIGHT}]" '
        f'-p pixel_format:=YUYV '
        f'{camera_info_param}'
        f'-p auto_exposure:=3 '
        f'-r __ns:=/camera '
    )
    return subprocess.Popen(
        cmd, shell=True, executable='/bin/bash',
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
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


def _extract_mp4_from_mcap(session_dir, session_id=None):
    """Extract camera frames from MCAP rosbag into a standalone MP4."""
    try:
        from mcap.reader import make_reader
        from mcap_ros2.decoder import DecoderFactory
        import numpy as np

        bag_dir = os.path.join(session_dir, 'rosbag')
        mcap_file = None
        for f in os.listdir(bag_dir):
            if f.endswith('.mcap'):
                mcap_file = os.path.join(bag_dir, f)
                break
        if not mcap_file:
            return

        mp4_path = os.path.join(session_dir, 'video.mp4')
        writer = None

        with open(mcap_file, 'rb') as fh:
            reader = make_reader(fh, decoder_factories=[DecoderFactory()])
            for schema, channel, message, decoded in reader.iter_decoded_messages(
                topics=['/camera/image_raw/compressed']
            ):
                jpg_data = np.frombuffer(bytes(decoded.data), dtype=np.uint8)
                frame = cv2.imdecode(jpg_data, cv2.IMREAD_COLOR)
                if frame is None:
                    continue
                if writer is None:
                    h, w = frame.shape[:2]
                    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                    writer = cv2.VideoWriter(mp4_path, fourcc, CAMERA_FPS, (w, h))
                writer.write(frame)

        if writer:
            writer.release()
            print(f'Extracted video: {mp4_path}')

        # Update session with extraction result
        if session_id:
            session = _get_session(session_id)
            if session:
                session['video_extracted'] = writer is not None
                _put_session(session_id, session)

    except Exception as e:
        print(f'MP4 extraction failed: {e}')
        if session_id:
            session = _get_session(session_id)
            if session:
                session['video_extracted'] = False
                _put_session(session_id, session)


def _wait_for_topics(timeout=30):
    """Wait for /unilidar/cloud topic to appear."""
    cmd = f'source {ROS_SETUP} && ros2 topic list'
    for _ in range(timeout):
        try:
            result = subprocess.run(
                cmd, shell=True, executable='/bin/bash',
                capture_output=True, text=True, timeout=20
            )
            if '/unilidar/cloud' in result.stdout:
                return True
        except subprocess.TimeoutExpired:
            pass
        time.sleep(1)
    return False


def _wait_for_topic(topic, timeout=15):
    """Wait for a specific ROS2 topic to appear."""
    cmd = f'source {ROS_SETUP} && ros2 topic list'
    for _ in range(timeout):
        try:
            result = subprocess.run(
                cmd, shell=True, executable='/bin/bash',
                capture_output=True, text=True, timeout=20
            )
            if topic in result.stdout:
                return True
        except subprocess.TimeoutExpired:
            pass
        time.sleep(1)
    return False


def _start_bag_record(output_dir, topics=None):
    """Start ros2 bag record process."""
    if topics is None:
        topics = ['/unilidar/cloud', '/unilidar/imu']
    os.makedirs(output_dir, exist_ok=True)
    bag_path = os.path.join(output_dir, 'rosbag')
    topics_str = ' '.join(topics)
    cmd = (
        f'source {ROS_SETUP} && '
        f'ros2 bag record '
        f'-o {shlex.quote(bag_path)} '
        f'--storage mcap '
        f'--max-cache-size 200000000 '
        f'{topics_str}'
    )
    return subprocess.Popen(
        cmd, shell=True, executable='/bin/bash',
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        preexec_fn=os.setsid
    )


def _kill_process_group(proc):
    """Kill a process and its entire group."""
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
            pass


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
    if recording and _active_recording['start_time']:
        elapsed = round(time.time() - _active_recording['start_time'], 1)
        active_session = _active_recording['session_id']

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

        slam_result = s.get('slam_result')
        slam_info = None
        if slam_result:
            slam_info = {
                'num_points': slam_result.get('num_points'),
                'bounding_box': slam_result.get('bounding_box_m'),
                'slam_time': slam_result.get('slam_time_s'),
                'total_time': slam_result.get('total_time_s'),
                'download_url': slam_result.get('download_url'),
                'job_id': slam_result.get('job_id'),
            }

        result.append({
            'id': sid,
            'name': s['name'],
            'created': s.get('created', ''),
            'status': s.get('status', 'unknown'),
            'bag_size': bag_size,
            'duration': s.get('duration'),
            'scp_path': os.path.join(RECORDINGS_DIR, s['name']),
            'slam_status': s.get('slam_status'),
            'slam_result': slam_info,
            'slam_error': s.get('slam_error'),
            'floorplan_status': s.get('floorplan_status'),
            'floorplan_meta': s.get('floorplan_meta'),
            'floorplan_error': s.get('floorplan_error'),
            'floorplan_candidates': s.get('floorplan_candidates'),
        })
    return jsonify(result)


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
            time.sleep(3)
            poll = camera_proc.poll()
            _log(f'Camera proc status after 3s: poll={poll}')
            if poll is not None and camera_proc.stderr:
                stderr = camera_proc.stderr.read().decode()[:500]
                _log(f'Camera stderr: {stderr}')
            tf_proc = _launch_tf_static()
            _active_recording['tf_proc'] = tf_proc
        _active_recording['camera_ok'] = camera_ok

        # Wait for driver warmup
        time.sleep(5)
        if driver_proc.poll() is not None:
            session['status'] = 'error'
            session['error'] = 'Driver exited during warmup'
            _put_session(session_id, session)
            _kill_process_group(camera_proc)
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
            _kill_process_group(camera_proc)
            _kill_process_group(tf_proc)
            _active_recording['driver_proc'] = None
            _active_recording['camera_proc'] = None
            _active_recording['tf_proc'] = None
            return

        _log('LiDAR topics found!')

        # Wait for camera topics (non-fatal if missing)
        if camera_ok:
            _log('Waiting for camera topic...')
            camera_topic_ok = _wait_for_topic('/camera/image_raw', timeout=15)
            _log(f'Camera topic found: {camera_topic_ok}')
            if not camera_topic_ok:
                _log('WARNING: Camera topic not found, recording without camera')
                camera_ok = False
                _active_recording['camera_ok'] = False

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
        _kill_process_group(camera_proc)
        _kill_process_group(tf_proc)
        _active_recording['driver_proc'] = None
        _active_recording['camera_proc'] = None
        _active_recording['tf_proc'] = None
        _active_recording['bag_proc'] = None
        _active_recording['session_id'] = None
        _active_recording['start_time'] = None
        _active_recording['camera_ok'] = False
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

        # Stop bag recording
        _kill_process_group(bag_proc)

        # Stop driver and camera
        _kill_process_group(driver_proc)
        _kill_process_group(camera_proc)
        _kill_process_group(tf_proc)

        duration = round(time.time() - start_time, 1) if start_time else 0

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
                        f.write(f"Camera resolution: {CAMERA_WIDTH}x{CAMERA_HEIGHT}@{CAMERA_FPS}fps\n")
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

            # Extract MP4 from MCAP in background (if camera was active)
            if camera_ok:
                threading.Thread(
                    target=_extract_mp4_from_mcap,
                    args=(session_dir, session_id),
                    daemon=True,
                ).start()

        # Reset state
        _active_recording['session_id'] = None
        _active_recording['bag_proc'] = None
        _active_recording['driver_proc'] = None
        _active_recording['camera_proc'] = None
        _active_recording['tf_proc'] = None
        _active_recording['start_time'] = None
        _active_recording['starting'] = False
        _active_recording['camera_ok'] = False

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
        cap = cv2.VideoCapture(CAMERA_DEVICE, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            return
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 360)
        cap.set(cv2.CAP_PROP_FPS, 3)
        try:
            while True:
                if _is_busy():
                    break
                ret, frame = cap.read()
                if not ret:
                    break
                _, jpeg = cv2.imencode('.jpg', frame,
                                       [cv2.IMWRITE_JPEG_QUALITY, 50])
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' +
                       jpeg.tobytes() + b'\r\n')
                time.sleep(0.33)
        finally:
            cap.release()

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
    """Delete a recording session and its files."""
    session = _get_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    # Don't delete active recording or processing session
    if _active_recording['session_id'] == session_id:
        return jsonify({'error': 'Cannot delete active recording'}), 409
    if session_id in _active_processing:
        return jsonify({'error': 'Cannot delete while processing'}), 409

    # Remove files
    session_dir = os.path.join(RECORDINGS_DIR, session['name'])
    if os.path.isdir(session_dir):
        shutil.rmtree(session_dir, ignore_errors=True)

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

            # Collect processing status for active SLAM jobs
            processing = {}
            for sid, proc in dict(_active_processing).items():
                processing[sid] = {
                    'status': proc['status'],
                    'progress': proc['progress'],
                    'elapsed': round(time.time() - proc['start_time'], 1),
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
# SLAM Cloud API
# ---------------------------------------------------------------------------

def _find_mcap(session):
    """Find the MCAP file for a session."""
    bag_dir = os.path.join(RECORDINGS_DIR, session['name'], 'rosbag')
    if not os.path.isdir(bag_dir):
        return None
    for f in os.listdir(bag_dir):
        if f.endswith('.mcap'):
            return os.path.join(bag_dir, f)
    return None


DIRECT_UPLOAD_LIMIT = 30 * 1024 * 1024   # 30 MB — use GCS path for larger files
COMPRESS_THRESHOLD = 200 * 1024 * 1024   # 200 MB — only gzip files larger than this


def _process_thread(session_id, mcap_path, voxel_size):
    """Background thread: upload MCAP to cloud API, store result."""
    gz_path = None
    slam_succeeded = False
    try:
        mcap_size = os.path.getsize(mcap_path)

        _active_processing[session_id] = {
            'status': 'uploading',
            'start_time': time.time(),
            'progress': f'Uploading {mcap_size / (1024*1024):.0f} MB...',
        }

        session = _get_session(session_id)
        session['status'] = 'processing'
        session['slam_status'] = 'uploading'
        _put_session(session_id, session)

        # Only compress very large files (>200 MB)
        if mcap_size > COMPRESS_THRESHOLD:
            _active_processing[session_id]['status'] = 'compressing'
            _active_processing[session_id]['progress'] = f'Compressing {mcap_size / (1024*1024):.0f} MB...'
            session['slam_status'] = 'compressing'
            _put_session(session_id, session)

            gz_path = mcap_path + '.gz'
            with open(mcap_path, 'rb') as f_in, gzip.open(gz_path, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)

            upload_path = gz_path
            upload_size = os.path.getsize(gz_path)
            upload_filename = 'scan.mcap.gz'
        else:
            upload_path = mcap_path
            upload_size = mcap_size
            upload_filename = 'scan.mcap'

        _active_processing[session_id]['status'] = 'uploading'
        _active_processing[session_id]['progress'] = f'Uploading {upload_size / (1024*1024):.0f} MB...'
        session = _get_session(session_id)
        session['slam_status'] = 'uploading'
        _put_session(session_id, session)

        if upload_size <= DIRECT_UPLOAD_LIMIT:
            result = _upload_direct(upload_path, upload_filename, voxel_size)
        else:
            result = _upload_via_gcs(upload_path, voxel_size, session_id)

        # Store result in session
        session = _get_session(session_id)
        session['status'] = 'processed'
        session['slam_status'] = 'done'
        session['slam_result'] = result
        _put_session(session_id, session)

        slam_succeeded = True

    except Exception as e:
        session = _get_session(session_id)
        if session:
            session['status'] = 'stopped'
            session['slam_status'] = 'error'
            session['slam_error'] = str(e)
            _put_session(session_id, session)
    finally:
        # Clean up gz file if we created one
        if gz_path:
            try:
                os.remove(gz_path)
            except OSError:
                pass

        if slam_succeeded:
            # Auto-trigger wall detection — transition the
            # _active_processing entry to floorplan status (no gap)
            _active_processing[session_id] = {
                'status': 'floorplan',
                'start_time': time.time(),
                'progress': 'Detecting walls...',
            }
            thread = threading.Thread(
                target=_detect_walls_thread,
                args=(session_id,),
                daemon=True,
            )
            thread.start()
        else:
            _active_processing.pop(session_id, None)


def _upload_direct(file_path, filename, voxel_size):
    """Upload MCAP directly to /api/slam."""
    url = f'{SLAM_API_URL}/api/slam'
    if voxel_size:
        url += f'?voxel_size={voxel_size}'

    curl_result = subprocess.run(
        ['curl', '-s', '-X', 'POST',
         '-F', f'file=@{file_path};filename={filename}',
         url],
        capture_output=True, text=True, timeout=660,
    )

    if curl_result.returncode != 0:
        raise RuntimeError(f'curl failed: {curl_result.stderr[:200]}')

    return json.loads(curl_result.stdout)


def _upload_via_gcs(file_path, voxel_size, session_id):
    """Upload large file to GCS, then trigger processing via /api/slam/from-gcs."""
    # Step 1: Get upload URL / blob name from API
    upload_info_result = subprocess.run(
        ['curl', '-s', f'{SLAM_API_URL}/api/upload-url'],
        capture_output=True, text=True, timeout=30,
    )
    if upload_info_result.returncode != 0:
        raise RuntimeError(f'Failed to get upload URL: {upload_info_result.stderr[:200]}')

    upload_info = json.loads(upload_info_result.stdout)
    blob_name = upload_info['blob_name']
    bucket = upload_info['bucket']
    gcs_uri = f'gs://{bucket}/{blob_name}'

    _active_processing[session_id]['progress'] = f'Uploading to GCS ({os.path.getsize(file_path) / (1024*1024):.0f} MB)...'

    # Step 2: Upload file to GCS via gsutil (run as talal for credentials)
    gsutil_result = subprocess.run(
        ['sudo', '-u', 'talal', 'gsutil', 'cp', file_path, gcs_uri],
        capture_output=True, text=True, timeout=600,
    )
    if gsutil_result.returncode != 0:
        raise RuntimeError(f'gsutil upload failed: {gsutil_result.stderr[:300]}')

    _active_processing[session_id]['status'] = 'processing'
    _active_processing[session_id]['progress'] = 'Processing on cloud...'

    # Step 3: Trigger processing via /api/slam/from-gcs
    process_url = f'{SLAM_API_URL}/api/slam/from-gcs?blob_name={blob_name}'
    if voxel_size:
        process_url += f'&voxel_size={voxel_size}'

    process_result = subprocess.run(
        ['curl', '-s', '-X', 'POST', process_url],
        capture_output=True, text=True, timeout=660,
    )

    if process_result.returncode != 0:
        raise RuntimeError(f'Processing request failed: {process_result.stderr[:200]}')

    return json.loads(process_result.stdout)


@app.route('/api/session/<session_id>/process', methods=['POST'])
def api_process(session_id):
    """Upload session MCAP to SLAM Cloud API for processing."""
    session = _get_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    with _processing_lock:
        if session_id in _active_processing:
            return jsonify({'error': 'Already processing'}), 409
        _active_processing[session_id] = {
            'status': 'uploading', 'start_time': time.time(),
            'progress': 'Starting...',
        }

    mcap_path = _find_mcap(session)
    if not mcap_path:
        _active_processing.pop(session_id, None)
        return jsonify({'error': 'No MCAP file found for session'}), 404

    data = request.get_json(force=True, silent=True) or {}
    voxel_size = data.get('voxel_size')

    thread = threading.Thread(
        target=_process_thread,
        args=(session_id, mcap_path, voxel_size),
        daemon=True,
    )
    thread.start()

    return jsonify({'session_id': session_id, 'status': 'started'})


@app.route('/api/session/<session_id>/result')
def api_result(session_id):
    """Get SLAM processing result for a session."""
    session = _get_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    # Check if currently processing
    if session_id in _active_processing:
        proc = _active_processing[session_id]
        elapsed = round(time.time() - proc['start_time'], 1)
        return jsonify({
            'status': proc['status'],
            'progress': proc['progress'],
            'elapsed': elapsed,
        })

    # Return stored result
    if session.get('slam_result'):
        return jsonify({
            'status': 'done',
            'result': session['slam_result'],
        })

    if session.get('slam_error'):
        return jsonify({
            'status': 'error',
            'error': session['slam_error'],
        })

    return jsonify({'status': 'not_processed'})


@app.route('/api/session/<session_id>/download')
def api_download(session_id):
    """Redirect to the PLY download URL."""
    session = _get_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    result = session.get('slam_result')
    if not result:
        return jsonify({'error': 'No SLAM result available'}), 404

    url = result.get('download_url')
    if not url:
        return jsonify({'error': 'No download URL'}), 404

    return redirect(url)


# ---------------------------------------------------------------------------
# Floor Plan Generation
# ---------------------------------------------------------------------------

def _ply_path_for_session(session):
    """Return the local PLY path for a session."""
    return os.path.join(RECORDINGS_DIR, session['name'], 'result.ply')


def _leveled_ply_path_for_session(session):
    """Return the leveled PLY path for a session."""
    return os.path.join(RECORDINGS_DIR, session['name'], 'result_leveled.ply')


def _floorplan_path_for_session(session):
    """Return the floor plan PNG path for a session."""
    return os.path.join(RECORDINGS_DIR, session['name'], 'floorplan.png')


def _download_ply(session, session_id):
    """Download PLY from cloud to local storage. Returns local path."""
    ply_path = _ply_path_for_session(session)
    if os.path.isfile(ply_path):
        return ply_path

    result = session.get('slam_result')
    if not result:
        raise RuntimeError('No SLAM result available')

    url = result.get('download_url')
    if not url:
        raise RuntimeError('No download URL in SLAM result')

    os.makedirs(os.path.dirname(ply_path), exist_ok=True)

    _active_processing[session_id]['progress'] = 'Downloading PLY...'

    curl_result = subprocess.run(
        ['curl', '-s', '-L', '-o', ply_path, url],
        capture_output=True, text=True, timeout=300,
    )
    if curl_result.returncode != 0:
        raise RuntimeError(f'PLY download failed: {curl_result.stderr[:200]}')

    if not os.path.isfile(ply_path) or os.path.getsize(ply_path) == 0:
        raise RuntimeError('PLY download produced empty file')

    size_mb = os.path.getsize(ply_path) / (1024 * 1024)
    print(f'Downloaded PLY: {size_mb:.1f} MB -> {ply_path}')
    return ply_path


def _floorplan_thread(session_id):
    """Background thread: download PLY, level, generate floor plan."""
    try:
        from floorplan import level_ply, generate_floorplan

        session = _get_session(session_id)
        if not session:
            return

        # Ensure _active_processing entry exists (may already be set by
        # auto-trigger in _process_thread; needed for manual trigger)
        if session_id not in _active_processing:
            _active_processing[session_id] = {
                'status': 'floorplan',
                'start_time': time.time(),
                'progress': 'Starting floor plan generation...',
            }

        session['floorplan_status'] = 'downloading'
        session.pop('floorplan_error', None)
        session.pop('floorplan_meta', None)
        _put_session(session_id, session)

        # Step 1: Download PLY
        _active_processing[session_id]['progress'] = 'Downloading PLY...'
        ply_path = _download_ply(session, session_id)

        # Step 2: Level
        session = _get_session(session_id)
        session['floorplan_status'] = 'leveling'
        _put_session(session_id, session)
        _active_processing[session_id]['progress'] = 'Leveling point cloud...'

        leveled_path = _leveled_ply_path_for_session(session)
        level_ply(ply_path, output_path=leveled_path)

        # Step 3: Generate floor plan
        session = _get_session(session_id)
        session['floorplan_status'] = 'generating'
        _put_session(session_id, session)
        _active_processing[session_id]['progress'] = 'Detecting walls...'

        floorplan_path = _floorplan_path_for_session(session)
        png_path, metadata = generate_floorplan(
            leveled_path, output_path=floorplan_path,
            ortho_tol=15.0, debug=False)

        # Done
        session = _get_session(session_id)
        session['floorplan_status'] = 'done'
        session['floorplan_meta'] = metadata
        _put_session(session_id, session)

        print(f'Floor plan generated: {metadata["num_walls"]} walls, '
              f'{metadata["area_m2"]}m2 -> {png_path}')

    except Exception as e:
        import traceback
        traceback.print_exc()
        session = _get_session(session_id)
        if session:
            session['floorplan_status'] = 'error'
            session['floorplan_error'] = str(e)
            _put_session(session_id, session)
    finally:
        _active_processing.pop(session_id, None)


@app.route('/api/session/<session_id>/floorplan', methods=['POST'])
def api_floorplan(session_id):
    """Generate floor plan from session's PLY. Runs in background thread."""
    session = _get_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    with _processing_lock:
        if session_id in _active_processing:
            return jsonify({'error': 'Already processing'}), 409
        if not session.get('slam_result'):
            return jsonify({'error': 'No SLAM result — process the scan first'}), 400
        _active_processing[session_id] = {
            'status': 'floorplan', 'start_time': time.time(),
            'progress': 'Starting...',
        }

    thread = threading.Thread(
        target=_floorplan_thread,
        args=(session_id,),
        daemon=True,
    )
    thread.start()

    return jsonify({'session_id': session_id, 'status': 'started'})


@app.route('/api/session/<session_id>/floorplan.png')
def api_floorplan_image(session_id):
    """Serve the generated floor plan PNG."""
    session = _get_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    png_path = _floorplan_path_for_session(session)
    if not os.path.isfile(png_path):
        return jsonify({'error': 'Floor plan not generated yet'}), 404

    return send_file(png_path, mimetype='image/png')


# ---------------------------------------------------------------------------
# Wall Detection + Selection
# ---------------------------------------------------------------------------

def _walls_preview_path_for_session(session):
    return os.path.join(RECORDINGS_DIR, session['name'], 'walls_preview.png')


def _candidates_dir_for_session(session):
    return os.path.join(RECORDINGS_DIR, session['name'], 'candidates')


def _detect_walls_thread(session_id, ortho_tol=15.0):
    """Background thread: level PLY, generate candidate floor plans."""
    try:
        from floorplan import level_ply, generate_candidates

        session = _get_session(session_id)
        if not session:
            return

        if session_id not in _active_processing:
            _active_processing[session_id] = {
                'status': 'floorplan',
                'start_time': time.time(),
                'progress': 'Detecting walls...',
            }

        # Download PLY if needed
        _active_processing[session_id]['progress'] = 'Downloading PLY...'
        ply_path = _download_ply(session, session_id)

        # Level if needed
        leveled_path = _leveled_ply_path_for_session(session)
        if not os.path.isfile(leveled_path):
            _active_processing[session_id]['progress'] = 'Leveling point cloud...'
            level_ply(ply_path, output_path=leveled_path)

        # Generate candidate floor plans
        _active_processing[session_id]['progress'] = 'Generating floor plan candidates...'
        cand_dir = _candidates_dir_for_session(session)
        candidates = generate_candidates(leveled_path, output_dir=cand_dir,
                                          ortho_tol=ortho_tol)

        # Strip png_path (not needed in JSON) — compute from id instead
        for c in candidates:
            c.pop('png_path', None)
            c.pop('wall_line_indices', None)

        # Store candidates in session
        session = _get_session(session_id)
        session['floorplan_candidates'] = candidates
        session['floorplan_status'] = 'candidates_ready'
        session.pop('floorplan_error', None)
        session.pop('floorplan_meta', None)
        _put_session(session_id, session)

        print(f'Generated {len(candidates)} floor plan candidates '
              f'for session {session_id}')

    except Exception as e:
        import traceback
        traceback.print_exc()
        session = _get_session(session_id)
        if session:
            session['floorplan_status'] = 'error'
            session['floorplan_error'] = str(e)
            _put_session(session_id, session)
    finally:
        _active_processing.pop(session_id, None)


def _generate_from_selection_thread(session_id, selected_ids):
    """Background thread: generate floor plan from selected walls."""
    try:
        from floorplan import generate_floorplan_from_selection

        session = _get_session(session_id)
        if not session:
            return

        if session_id not in _active_processing:
            _active_processing[session_id] = {
                'status': 'floorplan',
                'start_time': time.time(),
                'progress': 'Generating floor plan...',
            }

        leveled_path = _leveled_ply_path_for_session(session)
        floorplan_path = _floorplan_path_for_session(session)

        session['floorplan_status'] = 'generating'
        _put_session(session_id, session)

        # Pass walls_data so wall IDs map correctly to line indices
        walls_data = session.get('detected_walls', [])
        png_path, metadata = generate_floorplan_from_selection(
            leveled_path, selected_ids, output_path=floorplan_path,
            ortho_tol=15.0, walls_data=walls_data)

        session = _get_session(session_id)
        session['floorplan_status'] = 'done'
        session['floorplan_meta'] = metadata
        session.pop('floorplan_error', None)
        _put_session(session_id, session)

        print(f'Floor plan generated: {metadata["num_walls"]} walls, '
              f'{metadata["area_m2"]}m2')

    except Exception as e:
        import traceback
        traceback.print_exc()
        session = _get_session(session_id)
        if session:
            session['floorplan_status'] = 'error'
            session['floorplan_error'] = str(e)
            _put_session(session_id, session)
    finally:
        _active_processing.pop(session_id, None)


@app.route('/api/session/<session_id>/floorplan/detect', methods=['POST'])
def api_detect_walls(session_id):
    """Generate candidate floor plans. Returns after processing."""
    session = _get_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    with _processing_lock:
        if session_id in _active_processing:
            return jsonify({'error': 'Already processing'}), 409
        if not session.get('slam_result'):
            return jsonify({'error': 'No SLAM result'}), 400
        _active_processing[session_id] = {
            'status': 'floorplan', 'start_time': time.time(),
            'progress': 'Starting wall detection...',
        }

    data = request.get_json(force=True, silent=True) or {}
    ortho_tol = data.get('ortho_tol', 15.0)

    thread = threading.Thread(
        target=_detect_walls_thread,
        args=(session_id, ortho_tol),
        daemon=True,
    )
    thread.start()

    return jsonify({'session_id': session_id, 'status': 'detecting'})


@app.route('/api/session/<session_id>/floorplan/candidate/<int:cand_id>.png')
def api_candidate_image(session_id, cand_id):
    """Serve a candidate floor plan PNG."""
    session = _get_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    cand_dir = _candidates_dir_for_session(session)
    png_path = os.path.join(cand_dir, f'candidate_{cand_id}.png')
    if not os.path.isfile(png_path):
        return jsonify({'error': 'Candidate not found'}), 404

    return send_file(png_path, mimetype='image/png')


@app.route('/api/session/<session_id>/floorplan/pick', methods=['POST'])
def api_pick_candidate(session_id):
    """Pick a candidate floor plan as the final result."""
    session = _get_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    if session_id in _active_processing:
        return jsonify({'error': 'Still processing'}), 409

    data = request.get_json(force=True, silent=True) or {}
    cand_id = data.get('candidate_id')
    if cand_id is None:
        return jsonify({'error': 'candidate_id required'}), 400

    candidates = session.get('floorplan_candidates', [])
    chosen = None
    for c in candidates:
        if c['id'] == cand_id:
            chosen = c
            break
    if not chosen:
        return jsonify({'error': f'Candidate {cand_id} not found'}), 404

    # Copy candidate PNG to the final floorplan path
    cand_dir = _candidates_dir_for_session(session)
    src = os.path.join(cand_dir, f'candidate_{cand_id}.png')
    dst = _floorplan_path_for_session(session)
    if os.path.isfile(src):
        import shutil
        shutil.copy2(src, dst)

    session['floorplan_status'] = 'done'
    session['floorplan_meta'] = {
        'num_walls': chosen['num_walls'],
        'wall_lengths': chosen['wall_lengths'],
        'area_m2': chosen['area_m2'],
        'dimensions': chosen['dimensions'],
    }
    _put_session(session_id, session)

    return jsonify({'status': 'done', 'meta': session['floorplan_meta']})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _cleanup_on_exit():
    """Kill any running driver/bag/camera processes on app shutdown."""
    _kill_process_group(_active_recording.get('bag_proc'))
    _kill_process_group(_active_recording.get('driver_proc'))
    _kill_process_group(_active_recording.get('camera_proc'))
    _kill_process_group(_active_recording.get('tf_proc'))
    if _active_calibration.get('proc'):
        _kill_process_group(_active_calibration['proc'])


def _recover_stuck_sessions():
    """Reset sessions stuck in intermediate states from unclean shutdown."""
    stuck_statuses = {'processing', 'launching_driver', 'waiting_for_topics',
                      'starting', 'recording'}
    stuck_fp = {'downloading', 'leveling', 'generating'}
    sessions = _get_sessions()
    for sid, s in sessions.items():
        changed = False
        original_status = s.get('status')
        if original_status in stuck_statuses:
            s['status'] = 'stopped'
            if original_status == 'processing':
                s['slam_status'] = 'error'
                s['slam_error'] = 'Interrupted by shutdown'
            changed = True
        if s.get('slam_status') in ('uploading', 'compressing'):
            s['slam_status'] = 'error'
            s['slam_error'] = 'Interrupted by shutdown'
            s['status'] = 'stopped'
            changed = True
        if s.get('floorplan_status') in stuck_fp:
            s['floorplan_status'] = 'error'
            s['floorplan_error'] = 'Interrupted by shutdown'
            changed = True
        if changed:
            _put_session(sid, s)
            print(f'Recovered stuck session: {s["name"]}')


if __name__ == '__main__':
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
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
