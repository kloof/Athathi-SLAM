#!/usr/bin/env python3
"""Standalone Camera-LiDAR Extrinsic Calibration Tool.

A desktop PyQt5 application for visually aligning a Logitech Brio camera
with a Unitree L2 LiDAR. Shows a 2D overlay (LiDAR points projected onto
camera image) and a 3D point cloud view with camera frustum. Adjust the
6-DOF transform (tx, ty, tz, roll, pitch, yaw) via sliders or keyboard
until alignment looks correct, then save.

Requirements (all pre-installed on the Pi):
    PyQt5, OpenCV, numpy, scipy, PyYAML, rclpy

Usage:
    source /opt/ros/humble/setup.bash
    source ~/unilidar_sdk2/unitree_lidar_ros2/install/setup.bash
    DISPLAY=:0 python3 calibration_tool.py
"""

import os
import random
import subprocess
import sys
import time
from datetime import datetime

import cv2
import numpy as np
import yaml
from scipy.spatial.transform import Rotation

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QImage, QPixmap, QKeySequence
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QSlider, QDoubleSpinBox,
    QHBoxLayout, QVBoxLayout, QPushButton, QComboBox,
    QSplitter, QGroupBox, QMessageBox, QCheckBox,
    QShortcut, QSizePolicy,
)

# ---------------------------------------------------------------------------
# Paths (relative to this script)
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CALIBRATION_DIR = os.path.join(SCRIPT_DIR, 'calibration')
INTRINSICS_FILE = os.path.join(CALIBRATION_DIR, 'intrinsics.yaml')
EXTRINSICS_FILE = os.path.join(CALIBRATION_DIR, 'extrinsics.yaml')

ROS_SETUP = '/opt/ros/humble/setup.bash'
DRIVER_SETUP = '/home/talal/unilidar_sdk2/unitree_lidar_ros2/install/setup.bash'
LIDAR_IP = '192.168.1.62'
HOST_IP = '192.168.1.2'

# ---------------------------------------------------------------------------
# Calibration I/O
# ---------------------------------------------------------------------------

def load_intrinsics(path=INTRINSICS_FILE):
    with open(path) as f:
        d = yaml.safe_load(f)
    if d is None or 'camera_matrix' not in d:
        raise ValueError(f"Missing 'camera_matrix' in {path}")
    data = d['camera_matrix'].get('data', [])
    if len(data) != 9:
        raise ValueError(f"camera_matrix.data needs 9 elements, got {len(data)}")
    K = np.array(data).reshape(3, 3)
    D = np.array(d['distortion_coefficients']['data'])
    return K, D, d['image_width'], d['image_height']


def load_extrinsics(path=EXTRINSICS_FILE):
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        d = yaml.safe_load(f)
    if d is None or 'translation' not in d or 'rotation' not in d:
        return None
    t = d['translation']
    r = d['rotation']
    return {
        'translation': np.array([t['x'], t['y'], t['z']]),
        'rotation': np.array([r['x'], r['y'], r['z'], r['w']]),
    }


def save_extrinsics(translation, quaternion, path=EXTRINSICS_FILE,
                     method='manual_visual_alignment'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rot = Rotation.from_quat(quaternion)
    rpy = rot.as_euler('xyz', degrees=True)
    data = {
        'parent_frame': 'unilidar_lidar',
        'child_frame': 'camera_optical_frame',
        'translation': {
            'x': float(translation[0]),
            'y': float(translation[1]),
            'z': float(translation[2]),
        },
        'rotation': {
            'x': float(quaternion[0]),
            'y': float(quaternion[1]),
            'z': float(quaternion[2]),
            'w': float(quaternion[3]),
        },
        'rpy_degrees': {
            'roll': float(rpy[0]),
            'pitch': float(rpy[1]),
            'yaw': float(rpy[2]),
        },
        'calibration_date': datetime.now().strftime('%Y-%m-%d'),
        'method': method,
    }
    with open(path, 'w') as f:
        yaml.dump(data, f, default_flow_style=False)
    return data


# ---------------------------------------------------------------------------
# Projection math
# ---------------------------------------------------------------------------

def project_points(points_3d, K, D, R, t):
    """Project LiDAR points into camera pixel coordinates.
    Returns (Nx2 pixels for masked points, bool mask over all input points).
    """
    pts_cam = (R @ points_3d.T).T + t.flatten()
    mask = pts_cam[:, 2] > 0.1
    if not mask.any():
        return np.zeros((0, 2), dtype=np.float64), mask
    rvec = cv2.Rodrigues(R)[0]
    tvec = t.reshape(3, 1)
    px, _ = cv2.projectPoints(points_3d[mask], rvec, tvec, K, D.flatten())
    return px.reshape(-1, 2), mask


def colorize_by_depth(values):
    """Rainbow colormap returning Nx3 RGB uint8."""
    if len(values) == 0:
        return np.zeros((0, 3), dtype=np.uint8)
    mn, mx = values.min(), max(values.max(), values.min() + 0.1)
    norm = ((values - mn) / (mx - mn) * 255).astype(np.uint8)
    lut = cv2.applyColorMap(np.arange(256, dtype=np.uint8).reshape(1, -1),
                            cv2.COLORMAP_JET).reshape(256, 3)
    bgr = lut[norm]
    return bgr[:, ::-1]  # BGR -> RGB


# ---------------------------------------------------------------------------
# LiDAR point cloud parsing (fully vectorized)
# ---------------------------------------------------------------------------

def parse_pointcloud2(msg):
    """Parse a ROS2 PointCloud2 message into (Nx3 xyz, N intensities).
    Uses numpy vectorized ops — no Python per-point loops.
    """
    data = bytes(msg.data)
    n = msg.width * msg.height
    ps = msg.point_step
    if n == 0 or len(data) < n * ps:
        return np.zeros((0, 3), dtype=np.float32), np.zeros(0, dtype=np.float32)

    raw = np.frombuffer(data, dtype=np.uint8).reshape(n, ps)
    x = np.frombuffer(raw[:, 0:4].tobytes(), dtype='<f4')
    y = np.frombuffer(raw[:, 4:8].tobytes(), dtype='<f4')
    z = np.frombuffer(raw[:, 8:12].tobytes(), dtype='<f4')

    xyz = np.column_stack([x, y, z])
    finite = np.isfinite(xyz).all(axis=1)
    r2 = x * x + y * y + z * z
    valid = finite & (r2 > 0.01) & (r2 < 2500)

    # Unitree L2 layout: x(0) y(4) z(8) [pad](12) intensity(16) ring(20) time(24), ps=32
    # Find intensity offset from message fields
    intensity = np.zeros(n, dtype=np.float32)
    int_offset = 16  # default for Unitree L2
    for f in msg.fields:
        if f.name == 'intensity':
            int_offset = f.offset
            break
    if ps > int_offset + 3:
        intensity = np.frombuffer(raw[:, int_offset:int_offset+4].tobytes(), dtype='<f4')

    return xyz[valid], intensity[valid]


def voxel_downsample(points, intensities=None, voxel_size=0.03):
    """Grid-based voxel downsampling using numpy."""
    if len(points) == 0:
        return points, intensities
    grid = np.floor(points / voxel_size).astype(np.int32)
    _, idx = np.unique(grid, axis=0, return_index=True)
    if intensities is not None:
        return points[idx], intensities[idx]
    return points[idx], None


# ---------------------------------------------------------------------------
# Camera + LiDAR device helpers
# ---------------------------------------------------------------------------

def find_camera_device():
    # Prefer stable symlink from udev rule
    if os.path.exists('/dev/brio'):
        return '/dev/brio'
    # Fallback: scan sysfs
    sysfs = '/sys/class/video4linux'
    if not os.path.isdir(sysfs):
        return None
    for dev in sorted(os.listdir(sysfs)):
        try:
            with open(os.path.join(sysfs, dev, 'name')) as f:
                name = f.read().strip()
            with open(os.path.join(sysfs, dev, 'index')) as f:
                idx = int(f.read().strip())
            if 'Logitech BRIO' in name and idx == 0:
                return f'/dev/{dev}'
        except (OSError, ValueError):
            continue
    return None


def set_brio_fov(device, fov=90):
    try:
        subprocess.run(
            ['python3', '/tmp/cameractrls/cameractrls.py',
             '-d', device, '-c', f'logitech_brio_fov={fov}'],
            capture_output=True, timeout=5)
    except Exception:
        pass


def check_driver_running():
    r = subprocess.run(['pgrep', '-f', 'unitree_lidar_ros2_node'],
                       capture_output=True)
    return r.returncode == 0


def launch_driver():
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
        preexec_fn=os.setsid)


# ---------------------------------------------------------------------------
# Live feed worker (QThread — continuous camera + LiDAR)
# ---------------------------------------------------------------------------

class LiveFeedWorker(QThread):
    """Continuously captures camera frames and LiDAR clouds."""
    new_frame = pyqtSignal(object)           # BGR numpy array
    new_cloud = pyqtSignal(object, object)   # xyz, intensity arrays
    new_imu = pyqtSignal(object)             # dict with orientation, gravity, angular_vel
    status = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._running = True

    def stop(self):
        self._running = False

    def _open_camera(self):
        """Open camera, returns VideoCapture or None."""
        dev = find_camera_device()
        if not dev:
            return None
        set_brio_fov(dev, 90)
        time.sleep(0.2)
        cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
        if not cap.isOpened():
            return None
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # minimal buffer = latest frame only
        # Warmup
        for _ in range(5):
            cap.read()
            time.sleep(0.03)
        return cap

    def run(self):
        # --- Camera setup (with retry) ---
        cap = self._open_camera()
        if cap:
            self.status.emit('Camera ready')
        else:
            self.status.emit('No camera — will retry...')

        cam_fail_count = 0

        # --- LiDAR setup ---
        node = None
        cloud_msgs = []
        all_xyz = []
        all_int = []
        last_cloud_time = 0
        try:
            import rclpy
            from sensor_msgs.msg import PointCloud2, Imu

            node_name = f'_calib_live_{random.randint(1000, 9999)}'
            node = rclpy.create_node(node_name)

            def cb(msg):
                cloud_msgs.append(msg)

            last_imu = [None]
            def imu_cb(msg):
                last_imu[0] = msg

            node.create_subscription(PointCloud2, '/unilidar/cloud', cb, 10)
            node.create_subscription(Imu, '/unilidar/imu', imu_cb, 10)
            self.status.emit('Camera + LiDAR ready' if cap else 'LiDAR ready (no camera)')
            time.sleep(1)  # DDS discovery
        except ImportError:
            self.status.emit('Camera ready (no rclpy)' if cap else 'No camera, no rclpy')
        except Exception as e:
            self.status.emit(f'LiDAR error: {e}')

        # --- Main loop ---
        while self._running:
            # Camera frame (auto-reconnect on failure)
            if cap is not None:
                ret, frame = cap.read()
                if ret:
                    self.new_frame.emit(frame)
                    cam_fail_count = 0
                else:
                    cam_fail_count += 1
                    if cam_fail_count > 10:
                        cap.release()
                        cap = None
                        self.status.emit('Camera lost — reconnecting...')
            else:
                # Try to reconnect every ~2 seconds
                cam_fail_count += 1
                if cam_fail_count % 60 == 0:
                    cap = self._open_camera()
                    if cap:
                        self.status.emit('Camera reconnected')
                        cam_fail_count = 0

            # IMU
            if node is not None and last_imu[0] is not None:
                m = last_imu[0]
                q = [m.orientation.x, m.orientation.y, m.orientation.z, m.orientation.w]
                R_imu = Rotation.from_quat(q).as_matrix()
                gravity_in_lidar = R_imu.T @ np.array([0, 0, -9.81])
                gravity_dir = gravity_in_lidar / np.linalg.norm(gravity_in_lidar)
                rpy_imu = Rotation.from_quat(q).as_euler('xyz', degrees=True)
                self.new_imu.emit({
                    'quat': q,
                    'rpy': rpy_imu,
                    'gravity': gravity_dir,
                    'accel': [m.linear_acceleration.x, m.linear_acceleration.y,
                              m.linear_acceleration.z],
                    'gyro': [m.angular_velocity.x, m.angular_velocity.y,
                             m.angular_velocity.z],
                })
                last_imu[0] = None

            # LiDAR: spin and emit accumulated cloud
            if node is not None:
                try:
                    for _ in range(3):
                        rclpy.spin_once(node, timeout_sec=0.05)
                except Exception:
                    pass

                # Parse new messages, tag with timestamp
                now = time.time()
                if cloud_msgs:
                    for msg in cloud_msgs:
                        xyz, intensity = parse_pointcloud2(msg)
                        all_xyz.append((now, xyz))
                        all_int.append((now, intensity))
                    cloud_msgs.clear()

                # Drop data older than 10 seconds
                cutoff = now - 10.0
                all_xyz = [(t, d) for t, d in all_xyz if t > cutoff]
                all_int = [(t, d) for t, d in all_int if t > cutoff]

                # Emit updated cloud every 0.5s
                if all_xyz and now - last_cloud_time > 0.5:
                    cloud = np.concatenate([d for _, d in all_xyz])
                    ints = np.concatenate([d for _, d in all_int])
                    cloud, ints = voxel_downsample(cloud, ints, 0.02)
                    self.new_cloud.emit(cloud, ints)
                    last_cloud_time = now

            time.sleep(0.03)

        # Cleanup
        if cap is not None:
            cap.release()
        if node is not None:
            node.destroy_node()


# ---------------------------------------------------------------------------
# Slider + Spinbox compound widget
# ---------------------------------------------------------------------------

class SliderWithSpinbox(QWidget):
    valueChanged = pyqtSignal(float)

    def __init__(self, label, min_val, max_val, default, decimals=3,
                 step=0.001, parent=None):
        super().__init__(parent)
        self._block = False
        self._min = min_val
        self._max = max_val
        self._ticks = 10000

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        lbl = QLabel(label)
        lbl.setFixedWidth(32)
        lbl.setStyleSheet('color: #ccc; font-size: 11px;')
        lay.addWidget(lbl)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, self._ticks)
        self.slider.setValue(self._val_to_tick(default))
        self.slider.valueChanged.connect(self._slider_moved)
        lay.addWidget(self.slider, stretch=1)

        self.spin = QDoubleSpinBox()
        self.spin.setRange(min_val, max_val)
        self.spin.setDecimals(decimals)
        self.spin.setSingleStep(step)
        self.spin.setValue(default)
        self.spin.setFixedWidth(68)
        self.spin.valueChanged.connect(self._spin_changed)
        lay.addWidget(self.spin)

    def _val_to_tick(self, v):
        rng = self._max - self._min
        if rng == 0:
            return 0
        return int((v - self._min) / rng * self._ticks)

    def _tick_to_val(self, t):
        return self._min + (t / self._ticks) * (self._max - self._min)

    def _slider_moved(self, t):
        if self._block:
            return
        v = self._tick_to_val(t)
        self._block = True
        self.spin.setValue(v)
        self._block = False
        self.valueChanged.emit(v)

    def _spin_changed(self, v):
        if self._block:
            return
        self._block = True
        self.slider.setValue(self._val_to_tick(v))
        self._block = False
        self.valueChanged.emit(v)

    def value(self):
        return self.spin.value()

    def setValue(self, v):
        self._block = True
        self.spin.setValue(v)
        self.slider.setValue(self._val_to_tick(v))
        self._block = False

    def nudge(self, delta):
        self.spin.setValue(self.spin.value() + delta)


# ---------------------------------------------------------------------------
# 3D orthographic point cloud renderer (all numpy vectorized)
# ---------------------------------------------------------------------------

class CloudView3D(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(200, 200)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet('background: #111;')
        self.setAlignment(Qt.AlignCenter)

        self.cloud = None
        self.cloud_colors = None
        self._display_idx = None  # fixed subsample indices
        self.cam_R = np.eye(3)
        self.cam_t = np.zeros(3)

        self.azimuth = 45.0
        self.elevation = 30.0
        self.zoom = 1.0
        self.pan = np.array([0.0, 0.0])
        self._drag_start = None
        self._drag_az = 0
        self._drag_el = 0
        self._pan_start = None
        self._pan_orig = np.array([0.0, 0.0])

        self._render_timer = QTimer()
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(40)
        self._render_timer.timeout.connect(self._render)

    def set_data(self, cloud, colors, cam_R, cam_t):
        self.cloud = cloud
        self.cloud_colors = colors
        self.cam_R = cam_R
        self.cam_t = cam_t
        # Deterministic subsample — no flicker
        if cloud is not None and len(cloud) > 12000:
            stride = max(1, len(cloud) // 12000)
            self._display_idx = np.arange(0, len(cloud), stride)[:12000]
        else:
            self._display_idx = None
        self._render()

    def _view_matrix(self):
        az = np.radians(self.azimuth)
        el = np.radians(self.elevation)
        ca, sa = np.cos(az), np.sin(az)
        ce, se = np.cos(el), np.sin(el)
        Raz = np.array([[ca, -sa, 0], [sa, ca, 0], [0, 0, 1.0]])
        Rel = np.array([[1, 0, 0], [0, ce, -se], [0, se, ce]])
        return Rel @ Raz

    def _render(self):
        w = max(self.width(), 100)
        h = max(self.height(), 100)
        img = np.full((h, w, 3), 20, dtype=np.uint8)

        Rv = self._view_matrix()
        cx, cy = w // 2, h // 2
        scale = min(w, h) * 0.08 * self.zoom

        self._draw_grid(img, Rv, scale, cx, cy)
        self._draw_axes(img, Rv, scale, cx, cy)
        self._draw_frustum(img, Rv, scale, cx, cy)

        if self.cloud is not None and len(self.cloud) > 0:
            pts = self.cloud
            cols = self.cloud_colors
            if self._display_idx is not None:
                pts = pts[self._display_idx]
                if cols is not None:
                    cols = cols[self._display_idx]

            projected = (Rv @ pts.T).T
            sx = (projected[:, 0] * scale + cx + self.pan[0]).astype(int)
            sy = (-projected[:, 1] * scale + cy + self.pan[1]).astype(int)

            # Depth sort
            order = np.argsort(-projected[:, 2])
            sx, sy = sx[order], sy[order]
            if cols is not None:
                cols = cols[order]

            valid = (sx >= 0) & (sx < w) & (sy >= 0) & (sy < h)
            sx, sy = sx[valid], sy[valid]
            if cols is not None:
                cols = cols[valid]

            if len(sx) > 0:
                # Vectorized color write (no Python loop)
                if cols is not None:
                    img[sy, sx] = cols[:, ::-1]  # RGB -> BGR
                else:
                    img[sy, sx] = (0, 200, 255)

                # Dilate for visibility
                color_layer = np.zeros_like(img)
                color_layer[sy, sx] = img[sy, sx]
                kernel = np.ones((3, 3), np.uint8)
                dilated = cv2.dilate(color_layer, kernel, iterations=1)
                mask = np.zeros((h, w), dtype=np.uint8)
                mask[sy, sx] = 255
                mask_d = cv2.dilate(mask, kernel, iterations=1)
                apply = mask_d > 0
                img[apply] = dilated[apply]

        # QImage.copy() ensures Qt owns the buffer (no dangling pointer)
        qimg = QImage(img.data, w, h, 3 * w, QImage.Format_BGR888).copy()
        self.setPixmap(QPixmap.fromImage(qimg))

    def _draw_grid(self, img, Rv, scale, cx, cy):
        for i in range(-5, 6):
            for start, end in [
                (np.array([i, 0, -5.0]), np.array([i, 0, 5.0])),
                (np.array([-5.0, 0, i]), np.array([5.0, 0, i])),
            ]:
                p1, p2 = Rv @ start, Rv @ end
                x1 = int(p1[0] * scale + cx + self.pan[0])
                y1 = int(-p1[1] * scale + cy + self.pan[1])
                x2 = int(p2[0] * scale + cx + self.pan[0])
                y2 = int(-p2[1] * scale + cy + self.pan[1])
                cv2.line(img, (x1, y1), (x2, y2), (35, 35, 35), 1)

    def _draw_axes(self, img, Rv, scale, cx, cy):
        po = Rv @ np.zeros(3)
        ox = int(po[0] * scale + cx + self.pan[0])
        oy = int(-po[1] * scale + cy + self.pan[1])
        for direction, color, name in [
            (np.array([1, 0, 0.0]), (0, 0, 255), 'X'),
            (np.array([0, 1, 0.0]), (0, 255, 0), 'Y'),
            (np.array([0, 0, 1.0]), (255, 0, 0), 'Z'),
        ]:
            pe = Rv @ direction
            ex = int(pe[0] * scale + cx + self.pan[0])
            ey = int(-pe[1] * scale + cy + self.pan[1])
            cv2.arrowedLine(img, (ox, oy), (ex, ey), color, 2, tipLength=0.15)
            cv2.putText(img, name, (ex + 3, ey - 3),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, color, 1)

    def _draw_frustum(self, img, Rv, scale, cx, cy):
        R_lc = self.cam_R.T
        cam_pos = -R_lc @ self.cam_t
        cam_fwd = R_lc @ np.array([0, 0, 1.0])
        cam_up = R_lc @ np.array([0, -1, 0.0])
        cam_right = R_lc @ np.array([1, 0, 0.0])
        d, hw, hh = 0.5, 0.5, 0.28  # 90deg HFOV, ~16:9 aspect
        corners = [
            cam_pos + d * cam_fwd + hw * cam_right + hh * cam_up,
            cam_pos + d * cam_fwd - hw * cam_right + hh * cam_up,
            cam_pos + d * cam_fwd - hw * cam_right - hh * cam_up,
            cam_pos + d * cam_fwd + hw * cam_right - hh * cam_up,
        ]

        def proj(pt):
            p = Rv @ pt
            return (int(p[0] * scale + cx + self.pan[0]),
                    int(-p[1] * scale + cy + self.pan[1]))

        pp = proj(cam_pos)
        pc = [proj(c) for c in corners]
        color = (0, 200, 200)
        for c in pc:
            cv2.line(img, pp, c, color, 1)
        for i in range(4):
            cv2.line(img, pc[i], pc[(i + 1) % 4], color, 2)
        cv2.circle(img, pp, 3, (0, 255, 255), -1)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_start = e.pos()
            self._drag_az = self.azimuth
            self._drag_el = self.elevation
        elif e.button() == Qt.MiddleButton:
            self._pan_start = e.pos()
            self._pan_orig = self.pan.copy()

    def mouseMoveEvent(self, e):
        if self._drag_start is not None and e.buttons() & Qt.LeftButton:
            dx = e.x() - self._drag_start.x()
            dy = e.y() - self._drag_start.y()
            self.azimuth = self._drag_az + dx * 0.5
            self.elevation = np.clip(self._drag_el + dy * 0.5, -89, 89)
            if not self._render_timer.isActive():
                self._render_timer.start()
        elif self._pan_start is not None and e.buttons() & Qt.MiddleButton:
            dx = e.x() - self._pan_start.x()
            dy = e.y() - self._pan_start.y()
            self.pan = self._pan_orig + np.array([dx, dy])
            if not self._render_timer.isActive():
                self._render_timer.start()
        else:
            self._drag_start = None
            self._pan_start = None

    def mouseReleaseEvent(self, e):
        self._drag_start = None
        self._pan_start = None

    def wheelEvent(self, e):
        delta = e.angleDelta().y()
        self.zoom = np.clip(self.zoom * (1.1 if delta > 0 else 0.9), 0.1, 50.0)
        if not self._render_timer.isActive():
            self._render_timer.start()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.cloud is not None:
            self._render()


# ---------------------------------------------------------------------------
# 2D overlay view
# ---------------------------------------------------------------------------

class OverlayView(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(250, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet('background: #111;')
        self.setAlignment(Qt.AlignCenter)
        self._frame = None

    def update_overlay(self, frame, pixels, colors_rgb, point_size=3, opacity=0.8):
        if frame is None:
            self.setText('No camera — click Capture')
            self._frame = None
            return

        display = frame.copy()
        h, w = display.shape[:2]

        if pixels is not None and len(pixels) > 0:
            pxi = pixels.astype(int)
            valid = ((pxi[:, 0] >= 0) & (pxi[:, 0] < w) &
                     (pxi[:, 1] >= 0) & (pxi[:, 1] < h))
            pxi = pxi[valid]
            cols = colors_rgb[valid] if colors_rgb is not None else None

            if len(pxi) > 0:
                xs, ys = pxi[:, 0], pxi[:, 1]
                bgr = cols[:, ::-1] if cols is not None else np.full(
                    (len(xs), 3), [0, 200, 255], dtype=np.uint8)

                # Build dilated color layer
                color_layer = np.zeros_like(display)
                color_layer[ys, xs] = bgr

                if point_size > 1:
                    k = max(1, int(point_size))
                    kernel = cv2.getStructuringElement(
                        cv2.MORPH_ELLIPSE, (k * 2 + 1, k * 2 + 1))
                else:
                    kernel = np.ones((3, 3), np.uint8)

                color_dilated = cv2.dilate(color_layer, kernel, iterations=1)
                apply = color_dilated.any(axis=2)

                if opacity >= 0.99:
                    display[apply] = color_dilated[apply]
                else:
                    overlay = display.copy()
                    overlay[apply] = color_dilated[apply]
                    display = cv2.addWeighted(overlay, opacity, display,
                                              1 - opacity, 0)

        self._frame = display
        self._show_scaled(display)

    def _show_scaled(self, img):
        h, w = img.shape[:2]
        if w < 1 or h < 1:
            return
        lw, lh = self.width(), self.height()
        if lw < 10 or lh < 10:
            return
        scale = min(lw / w, lh / h, 1.0)
        nw, nh = int(w * scale), int(h * scale)
        if nw < 1 or nh < 1:
            return
        scaled = cv2.resize(img, (nw, nh))
        qimg = QImage(scaled.data, nw, nh, 3 * nw, QImage.Format_BGR888).copy()
        self.setPixmap(QPixmap.fromImage(qimg))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._frame is not None:
            self._show_scaled(self._frame)


# ---------------------------------------------------------------------------
# Main application window (optimized for 800x640 monitor)
# ---------------------------------------------------------------------------

class CalibrationTool(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Camera-LiDAR Calibration')
        self.setMinimumSize(780, 560)
        self.resize(800, 640)

        # State
        self.camera_frame = None
        self.cloud_xyz = np.zeros((0, 3), dtype=np.float32)
        self.cloud_intensity = np.zeros(0, dtype=np.float32)
        self.K = None
        self.D = None
        self.img_w = 1280
        self.img_h = 720
        self._restoring = False  # guard for undo during programmatic set
        self.undo_stack = []
        self.redo_stack = []
        self._live_worker = None

        # Debounce timer for slider changes
        self._update_timer = QTimer()
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(50)
        self._update_timer.timeout.connect(self._do_update)

        self._live_timer = QTimer()
        self._live_timer.setInterval(300)  # ~3 fps live refresh (light on Pi+RDP)
        self._live_timer.timeout.connect(self._do_update)

        self._slider_dragging = False

        self._undo_timer = QTimer()
        self._undo_timer.setSingleShot(True)
        self._undo_timer.setInterval(400)
        self._undo_timer.timeout.connect(self._commit_undo)
        self._pending_undo_before = None

        # Load intrinsics
        try:
            self.K, self.D, self.img_w, self.img_h = load_intrinsics()
        except Exception as e:
            self.K = np.eye(3)
            self.D = np.zeros(5)
            QTimer.singleShot(100, lambda: QMessageBox.warning(
                self, 'Warning',
                f'Could not load intrinsics:\n{e}\n\n'
                'Projection will not work correctly.'))

        self._init_ui()
        self._init_shortcuts()

        # Load existing extrinsics
        try:
            ext = load_extrinsics()
        except Exception:
            ext = None
        if ext:
            rpy = Rotation.from_quat(ext['rotation']).as_euler('xyz', degrees=True)
            t = ext['translation']
            self._restoring = True
            self.sl_tx.setValue(t[0])
            self.sl_ty.setValue(t[1])
            self.sl_tz.setValue(t[2])
            self.sl_roll.setValue(rpy[0])
            self.sl_pitch.setValue(rpy[1])
            self.sl_yaw.setValue(rpy[2])
            self._restoring = False

        # Push initial state for undo
        self.undo_stack.append(self._get_state())
        self.statusBar().showMessage('Starting live feed...')

        # Auto-start live feed
        QTimer.singleShot(200, self._start_live)

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(4)

        # --- Top: views ---
        splitter = QSplitter(Qt.Horizontal)
        self.overlay_view = OverlayView()
        self.cloud_view = CloudView3D()
        splitter.addWidget(self.overlay_view)
        splitter.addWidget(self.cloud_view)
        splitter.setSizes([480, 300])
        main_layout.addWidget(splitter, stretch=1)

        # --- Bottom: compact controls for 800x640 ---
        controls = QWidget()
        ctrl_layout = QHBoxLayout(controls)
        ctrl_layout.setContentsMargins(0, 0, 0, 0)
        ctrl_layout.setSpacing(4)

        # Translation group
        trans_group = QGroupBox('Translation (m)')
        trans_group.setMaximumHeight(120)
        trans_lay = QVBoxLayout(trans_group)
        trans_lay.setSpacing(2)
        trans_lay.setContentsMargins(4, 14, 4, 4)
        self.sl_tx = SliderWithSpinbox('X:', -0.50, 0.50, 0.0, 3, 0.001)
        self.sl_ty = SliderWithSpinbox('Y:', -0.50, 0.50, 0.0, 3, 0.001)
        self.sl_tz = SliderWithSpinbox('Z:', -0.50, 0.50, 0.0, 3, 0.001)
        trans_lay.addWidget(self.sl_tx)
        trans_lay.addWidget(self.sl_ty)
        trans_lay.addWidget(self.sl_tz)
        ctrl_layout.addWidget(trans_group)

        # Rotation group
        rot_group = QGroupBox('Rotation (deg)')
        rot_group.setMaximumHeight(120)
        rot_lay = QVBoxLayout(rot_group)
        rot_lay.setSpacing(2)
        rot_lay.setContentsMargins(4, 14, 4, 4)
        self.sl_roll = SliderWithSpinbox('R:', -180, 180, 0.0, 1, 0.1)
        self.sl_pitch = SliderWithSpinbox('P:', -180, 180, 0.0, 1, 0.1)
        self.sl_yaw = SliderWithSpinbox('Y:', -180, 180, 0.0, 1, 0.1)
        rot_lay.addWidget(self.sl_roll)
        rot_lay.addWidget(self.sl_pitch)
        rot_lay.addWidget(self.sl_yaw)
        ctrl_layout.addWidget(rot_group)

        # Compact right panel: display + actions
        right_panel = QWidget()
        right_lay = QVBoxLayout(right_panel)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(2)

        # Display row
        disp_row = QHBoxLayout()
        disp_row.setSpacing(4)
        self.color_combo = QComboBox()
        self.color_combo.addItems(['Depth', 'Distance', 'Camera'])
        self.color_combo.setFixedWidth(80)
        self.color_combo.currentIndexChanged.connect(self._schedule_update)
        disp_row.addWidget(QLabel('Color:'))
        disp_row.addWidget(self.color_combo)

        self.step_combo = QComboBox()
        self.step_combo.addItems(['Coarse', 'Fine', 'Ultra'])
        self.step_combo.setCurrentIndex(1)
        self.step_combo.setFixedWidth(62)
        disp_row.addWidget(QLabel('Step:'))
        disp_row.addWidget(self.step_combo)
        right_lay.addLayout(disp_row)

        # Point size + opacity row
        size_row = QHBoxLayout()
        size_row.setSpacing(4)
        self.sl_ptsize = SliderWithSpinbox('Pt:', 1, 10, 3, 0, 1)
        self.sl_ptsize.valueChanged.connect(self._schedule_update)
        size_row.addWidget(self.sl_ptsize)
        right_lay.addLayout(size_row)

        opacity_row = QHBoxLayout()
        opacity_row.setSpacing(4)
        self.sl_opacity = SliderWithSpinbox('Op:', 0.1, 1.0, 0.8, 2, 0.05)
        self.sl_opacity.valueChanged.connect(self._schedule_update)
        opacity_row.addWidget(self.sl_opacity)
        right_lay.addLayout(opacity_row)

        self.chk_undistort = QCheckBox('Undistort')
        self.chk_undistort.setStyleSheet('font-size: 10px;')
        self.chk_undistort.stateChanged.connect(self._schedule_update)
        right_lay.addWidget(self.chk_undistort)

        ctrl_layout.addWidget(right_panel)

        # Action buttons column
        btn_panel = QWidget()
        btn_lay = QVBoxLayout(btn_panel)
        btn_lay.setContentsMargins(0, 0, 0, 0)
        btn_lay.setSpacing(3)

        self.btn_capture = QPushButton('Restart')
        self.btn_capture.setStyleSheet(
            'QPushButton{background:#e94560;color:white;padding:6px;'
            'font-weight:bold;border-radius:4px;font-size:11px;}'
            'QPushButton:hover{background:#ff5a7a;}')
        self.btn_capture.clicked.connect(self._capture)
        btn_lay.addWidget(self.btn_capture)

        self.btn_save = QPushButton('Save')
        self.btn_save.setStyleSheet(
            'QPushButton{background:#4ecca3;color:#1a1a2e;padding:5px;'
            'font-weight:bold;border-radius:4px;font-size:11px;}')
        self.btn_save.clicked.connect(self._save)
        btn_lay.addWidget(self.btn_save)

        self.btn_reset = QPushButton('Reset')
        self.btn_reset.clicked.connect(self._reset)
        btn_lay.addWidget(self.btn_reset)

        self.btn_ply = QPushButton('Gen PLY')
        self.btn_ply.clicked.connect(self._generate_ply)
        btn_lay.addWidget(self.btn_ply)

        ctrl_layout.addWidget(btn_panel)

        # IMU info panel
        imu_group = QGroupBox('IMU')
        imu_group.setMaximumHeight(120)
        imu_lay = QVBoxLayout(imu_group)
        imu_lay.setSpacing(1)
        imu_lay.setContentsMargins(4, 14, 4, 4)
        self.lbl_imu_gravity = QLabel('Gravity: --')
        self.lbl_imu_gravity.setStyleSheet('color: #4ecca3; font-size: 10px;')
        self.lbl_imu_rpy = QLabel('RPY: --')
        self.lbl_imu_rpy.setStyleSheet('color: #f0a500; font-size: 10px;')
        self.lbl_imu_accel = QLabel('Accel: --')
        self.lbl_imu_accel.setStyleSheet('color: #888; font-size: 10px;')
        self.lbl_imu_gyro = QLabel('Gyro: --')
        self.lbl_imu_gyro.setStyleSheet('color: #888; font-size: 10px;')
        imu_lay.addWidget(self.lbl_imu_gravity)
        imu_lay.addWidget(self.lbl_imu_rpy)
        imu_lay.addWidget(self.lbl_imu_accel)
        imu_lay.addWidget(self.lbl_imu_gyro)
        ctrl_layout.addWidget(imu_group)

        main_layout.addWidget(controls)

        # Connect slider changes
        for sl in [self.sl_tx, self.sl_ty, self.sl_tz,
                   self.sl_roll, self.sl_pitch, self.sl_yaw]:
            sl.valueChanged.connect(self._on_slider_changed)

        # Style
        self.setStyleSheet('''
            QMainWindow { background: #1a1a2e; }
            QWidget { background: #1a1a2e; color: #eee; font-size: 11px; }
            QGroupBox {
                border: 1px solid #333; border-radius: 4px;
                margin-top: 8px; padding-top: 12px;
                font-weight: bold; color: #e94560; font-size: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin; left: 6px; padding: 0 2px;
            }
            QSlider::groove:horizontal {
                background: #333; height: 4px; border-radius: 2px;
            }
            QSlider::handle:horizontal {
                background: #e94560; width: 12px; margin: -4px 0;
                border-radius: 6px;
            }
            QDoubleSpinBox, QComboBox {
                background: #16213e; color: #eee; border: 1px solid #333;
                border-radius: 3px; padding: 1px 3px; font-size: 11px;
            }
            QPushButton {
                background: #16213e; color: #eee; border: 1px solid #333;
                border-radius: 4px; padding: 4px; font-size: 10px;
            }
            QPushButton:hover { background: #1f3060; }
            QCheckBox { color: #ccc; }
            QStatusBar { color: #888; font-size: 10px; }
            QLabel { font-size: 11px; }
        ''')

    def _init_shortcuts(self):
        # Ctrl+ shortcuts (safe, won't conflict with spinbox input)
        QShortcut(QKeySequence('Ctrl+S'), self, self._save)
        QShortcut(QKeySequence('Ctrl+Z'), self, self._undo)
        QShortcut(QKeySequence('Ctrl+Shift+Z'), self, self._redo)
        QShortcut(QKeySequence('Ctrl+R'), self, self._reset)
        # Single-key shortcuts handled via keyPressEvent to avoid
        # intercepting spinbox/combo text input

    def keyPressEvent(self, event):
        focus = QApplication.focusWidget()
        if isinstance(focus, (QDoubleSpinBox, QComboBox)):
            super().keyPressEvent(event)
            return

        key = event.key()
        handled = True
        if key == Qt.Key_Space:
            self._capture()
        elif key == Qt.Key_W:
            self._nudge_rot('pitch', 1)
        elif key == Qt.Key_S:
            self._nudge_rot('pitch', -1)
        elif key == Qt.Key_A:
            self._nudge_rot('yaw', 1)
        elif key == Qt.Key_D:
            self._nudge_rot('yaw', -1)
        elif key == Qt.Key_Q:
            self._nudge_rot('roll', 1)
        elif key == Qt.Key_E:
            self._nudge_rot('roll', -1)
        elif key == Qt.Key_I:
            self._nudge_trans('x', 1)
        elif key == Qt.Key_K:
            self._nudge_trans('x', -1)
        elif key == Qt.Key_J:
            self._nudge_trans('y', 1)
        elif key == Qt.Key_L:
            self._nudge_trans('y', -1)
        elif key == Qt.Key_U:
            self._nudge_trans('z', 1)
        elif key == Qt.Key_O:
            self._nudge_trans('z', -1)
        elif key == Qt.Key_F:
            self._toggle_step()
        elif key == Qt.Key_1:
            self.color_combo.setCurrentIndex(0)
        elif key == Qt.Key_2:
            self.color_combo.setCurrentIndex(1)
        elif key == Qt.Key_3:
            self.color_combo.setCurrentIndex(2)
        else:
            handled = False

        if not handled:
            super().keyPressEvent(event)

    def _get_step(self):
        idx = self.step_combo.currentIndex()
        if idx == 0:
            return 0.005, 1.0
        elif idx == 1:
            return 0.001, 0.1
        else:
            return 0.0002, 0.05

    def _toggle_step(self):
        idx = (self.step_combo.currentIndex() + 1) % 3
        self.step_combo.setCurrentIndex(idx)
        names = ['Coarse', 'Fine', 'Ultra-fine']
        self.statusBar().showMessage(f'Step: {names[idx]}', 2000)

    def _nudge_trans(self, axis, direction):
        step_t, _ = self._get_step()
        sl = {'x': self.sl_tx, 'y': self.sl_ty, 'z': self.sl_tz}[axis]
        sl.nudge(step_t * direction)

    def _nudge_rot(self, axis, direction):
        _, step_r = self._get_step()
        sl = {'roll': self.sl_roll, 'pitch': self.sl_pitch, 'yaw': self.sl_yaw}[axis]
        sl.nudge(step_r * direction)

    def _get_transform(self):
        rpy = [self.sl_roll.value(), self.sl_pitch.value(), self.sl_yaw.value()]
        t = np.array([self.sl_tx.value(), self.sl_ty.value(), self.sl_tz.value()])
        R = Rotation.from_euler('xyz', rpy, degrees=True).as_matrix()
        return R, t, rpy

    # --- Live feed ---

    def _start_live(self):
        if self._live_worker is not None and self._live_worker.isRunning():
            return

        if not check_driver_running():
            reply = QMessageBox.question(
                self, 'LiDAR Driver',
                'LiDAR driver not running. Start it?',
                QMessageBox.Yes | QMessageBox.No)
            if reply == QMessageBox.Yes:
                self.statusBar().showMessage('Starting LiDAR driver...')
                launch_driver()
                QTimer.singleShot(5000, self._start_live)
                return

        self._live_worker = LiveFeedWorker()
        self._live_worker.new_frame.connect(self._on_new_frame)
        self._live_worker.new_cloud.connect(self._on_new_cloud)
        self._live_worker.new_imu.connect(self._on_new_imu)
        self._live_worker.status.connect(
            lambda msg: self.statusBar().showMessage(msg))
        self._live_worker.start()
        self._live_timer.start()
        self.btn_capture.setText('Live')
        self.btn_capture.setEnabled(False)

    def _stop_live(self):
        if self._live_worker is not None:
            self._live_worker.stop()
            self._live_worker.wait(3000)
            self._live_worker = None
        self._live_timer.stop()

    def _on_new_frame(self, frame):
        self.camera_frame = frame

    def _on_new_cloud(self, cloud_xyz, cloud_intensity):
        self.cloud_xyz = cloud_xyz
        self.cloud_intensity = cloud_intensity

    def _on_new_imu(self, data):
        g = data['gravity']
        rpy = data['rpy']
        accel = data.get('accel', [0, 0, 0])
        gyro = data.get('gyro', [0, 0, 0])
        self.lbl_imu_gravity.setText(f'Grav: [{g[0]:.2f}, {g[1]:.2f}, {g[2]:.2f}]')
        self.lbl_imu_rpy.setText(f'RPY: [{rpy[0]:.1f}, {rpy[1]:.1f}, {rpy[2]:.1f}]')
        self.lbl_imu_accel.setText(f'Acc: [{accel[0]:.1f}, {accel[1]:.1f}, {accel[2]:.1f}]')
        self.lbl_imu_gyro.setText(f'Gyr: [{gyro[0]:.3f}, {gyro[1]:.3f}, {gyro[2]:.3f}]')

    def _capture(self):
        """Manual re-capture (restarts live feed)."""
        self._stop_live()
        QTimer.singleShot(500, self._start_live)

    # --- Update views ---

    def _on_slider_changed(self, _=None):
        if self._restoring:
            return
        if self._pending_undo_before is None:
            self._pending_undo_before = self._get_state()
        self._undo_timer.start()
        self._slider_dragging = True
        self._schedule_update()

    def _commit_undo(self):
        current = self._get_state()
        if (self._pending_undo_before is not None
                and self._pending_undo_before != current):
            self.undo_stack.append(current)
            if len(self.undo_stack) > 100:
                self.undo_stack.pop(0)
            self.redo_stack.clear()
        self._pending_undo_before = None
        # Slider drag ended — do a full quality update with 3D
        self._slider_dragging = False
        self._do_update()

    def _schedule_update(self, _=None):
        self._update_timer.start()

    def _do_update(self):
        R, t, rpy = self._get_transform()

        frame = self.camera_frame
        if frame is not None and self.chk_undistort.isChecked():
            frame = cv2.undistort(frame, self.K, self.D)

        pixels = None
        colors = None
        n_visible = 0
        color_mode = self.color_combo.currentIndex()

        cloud = self.cloud_xyz
        n_total = len(cloud)

        # Subsample for fast updates during slider drag
        MAX_DISPLAY = 8000 if self._slider_dragging else 25000
        if n_total > MAX_DISPLAY:
            stride = max(1, n_total // MAX_DISPLAY)
            cloud_disp = cloud[::stride]
        else:
            cloud_disp = cloud

        pts_cam = None
        if len(cloud_disp) > 0:
            pts_cam = (R @ cloud_disp.T).T + t.flatten()
            px, mask = project_points(cloud_disp, self.K, self.D, R, t)

            if len(px) > 0:
                depths_vis = pts_cam[mask, 2]

                if color_mode == 0:
                    colors = colorize_by_depth(depths_vis)
                elif color_mode == 1:
                    dists_vis = np.linalg.norm(cloud_disp[mask], axis=1)
                    colors = colorize_by_depth(dists_vis)
                else:
                    if frame is not None:
                        h, w = frame.shape[:2]
                        pxi = px.astype(int)
                        valid = ((pxi[:, 0] >= 0) & (pxi[:, 0] < w) &
                                 (pxi[:, 1] >= 0) & (pxi[:, 1] < h))
                        colors = np.full((len(px), 3), 128, dtype=np.uint8)
                        valid_idx = np.where(valid)[0]
                        if len(valid_idx) > 0:
                            bgr = frame[pxi[valid_idx, 1], pxi[valid_idx, 0]]
                            colors[valid_idx] = bgr[:, ::-1]
                    else:
                        colors = colorize_by_depth(depths_vis)

                pixels = px
                if frame is not None:
                    h, w = frame.shape[:2]
                    pxi = px.astype(int)
                    in_frame = ((pxi[:, 0] >= 0) & (pxi[:, 0] < w) &
                                (pxi[:, 1] >= 0) & (pxi[:, 1] < h))
                    n_visible = int(in_frame.sum())

        pt_size = int(self.sl_ptsize.value())
        opacity = self.sl_opacity.value()
        self.overlay_view.update_overlay(frame, pixels, colors, pt_size, opacity)

        # 3D view — skip during slider drag for speed
        if not self._slider_dragging:
            if len(cloud_disp) > 0 and pts_cam is not None:
                if color_mode == 0:
                    cloud_colors = colorize_by_depth(pts_cam[:, 2])
                else:
                    dists = np.linalg.norm(cloud_disp, axis=1)
                    cloud_colors = colorize_by_depth(dists)
                self.cloud_view.set_data(cloud_disp, cloud_colors, R, t)
            else:
                self.cloud_view.set_data(None, None, R, t)

        total = len(self.cloud_xyz)
        pct = (n_visible / total * 100) if total > 0 else 0
        self.statusBar().showMessage(
            f'{total:,}pts | {n_visible:,} FOV ({pct:.0f}%) | '
            f'T=[{t[0]:.3f},{t[1]:.3f},{t[2]:.3f}] '
            f'RPY=[{rpy[0]:.1f},{rpy[1]:.1f},{rpy[2]:.1f}]')

    # --- Undo/Redo ---

    def _get_state(self):
        return (self.sl_tx.value(), self.sl_ty.value(), self.sl_tz.value(),
                self.sl_roll.value(), self.sl_pitch.value(), self.sl_yaw.value())

    def _set_state(self, state):
        self._restoring = True
        self.sl_tx.setValue(state[0])
        self.sl_ty.setValue(state[1])
        self.sl_tz.setValue(state[2])
        self.sl_roll.setValue(state[3])
        self.sl_pitch.setValue(state[4])
        self.sl_yaw.setValue(state[5])
        self._restoring = False

    def _undo(self):
        # Commit any pending undo first
        if self._pending_undo_before is not None:
            self._commit_undo()
        if len(self.undo_stack) < 2:
            return
        self.redo_stack.append(self.undo_stack.pop())
        self._set_state(self.undo_stack[-1])
        self._schedule_update()

    def _redo(self):
        if not self.redo_stack:
            return
        state = self.redo_stack.pop()
        self.undo_stack.append(state)
        self._set_state(state)
        self._schedule_update()

    # --- Save/Load/Reset ---

    def _save(self):
        R, t, rpy = self._get_transform()
        q = Rotation.from_euler('xyz', rpy, degrees=True).as_quat()
        save_extrinsics(t, q)
        self.statusBar().showMessage(f'Saved to {EXTRINSICS_FILE}', 5000)
        QMessageBox.information(self, 'Saved',
                                f'Extrinsics saved to:\n{EXTRINSICS_FILE}')

    def _reset(self):
        try:
            ext = load_extrinsics()
        except Exception:
            ext = None
        if ext:
            rpy = Rotation.from_quat(ext['rotation']).as_euler('xyz', degrees=True)
            self._set_state((ext['translation'][0], ext['translation'][1],
                             ext['translation'][2], rpy[0], rpy[1], rpy[2]))
        else:
            self._set_state((0, 0, 0, 0, 0, 0))
        self._schedule_update()
        self.statusBar().showMessage('Reset to saved values', 3000)

    # --- PLY export (vectorized) ---

    def _generate_ply(self):
        cloud = self.cloud_xyz
        if len(cloud) == 0:
            QMessageBox.warning(self, 'No Data', 'Capture LiDAR data first.')
            return
        if self.camera_frame is None:
            QMessageBox.warning(self, 'No Camera', 'Capture a camera frame first.')
            return

        self.statusBar().showMessage('Generating PLY...')
        QApplication.processEvents()

        R, t, _ = self._get_transform()
        frame = self.camera_frame
        h, w = frame.shape[:2]

        # Vectorized color assignment
        colors = np.full((len(cloud), 3), 128, dtype=np.uint8)  # default gray
        px, mask = project_points(cloud, self.K, self.D, R, t)
        colored = 0
        if len(px) > 0:
            pxi = px.astype(int)
            valid = ((pxi[:, 0] >= 0) & (pxi[:, 0] < w) &
                     (pxi[:, 1] >= 0) & (pxi[:, 1] < h))
            valid_idx = np.where(valid)[0]
            if len(valid_idx) > 0:
                vx = pxi[valid_idx, 0]
                vy = pxi[valid_idx, 1]
                bgr = frame[vy, vx]
                # Map back to original cloud indices
                mask_indices = np.where(mask)[0]
                cloud_idx = mask_indices[valid_idx]
                colors[cloud_idx] = bgr[:, ::-1]  # BGR -> RGB
                colored = len(valid_idx)

        out_path = '/tmp/calibration_test.ply'
        with open(out_path, 'w') as f:
            f.write('ply\nformat ascii 1.0\n')
            f.write(f'element vertex {len(cloud)}\n')
            f.write('property float x\nproperty float y\nproperty float z\n')
            f.write('property uchar red\nproperty uchar green\nproperty uchar blue\n')
            f.write('end_header\n')
            # Write without float64 cast
            for i in range(len(cloud)):
                p = cloud[i]
                c = colors[i]
                f.write(f'{p[0]:.4f} {p[1]:.4f} {p[2]:.4f} {c[0]} {c[1]} {c[2]}\n')

        size_mb = os.path.getsize(out_path) / (1024 * 1024)
        self.statusBar().showMessage(
            f'PLY: {out_path} ({size_mb:.1f}MB, {colored}/{len(cloud)} colored)', 10000)
        QMessageBox.information(
            self, 'PLY Generated',
            f'{out_path}\n{size_mb:.1f} MB, {colored}/{len(cloud)} colored\n\n'
            f'Open in CloudCompare to verify.')

    # --- Window close ---

    def closeEvent(self, event):
        self._stop_live()
        event.accept()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if not os.environ.get('DISPLAY'):
        print('Error: No DISPLAY set. If using VNC, run:')
        print('  DISPLAY=:0 python3 calibration_tool.py')
        sys.exit(1)

    if not os.path.isfile(INTRINSICS_FILE):
        print(f'Warning: No intrinsics at {INTRINSICS_FILE}')

    try:
        import rclpy
        if not rclpy.ok():
            rclpy.init()
        print('rclpy: OK')
    except ImportError:
        print('Warning: rclpy not available. Source ROS2 setup:')
        print(f'  source {ROS_SETUP}')
        print(f'  source {DRIVER_SETUP}')
        print('LiDAR capture disabled.')
    except Exception:
        pass  # already initialized

    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    window = CalibrationTool()
    window.show()
    ret = app.exec_()

    try:
        import rclpy
        if rclpy.ok():
            rclpy.shutdown()
    except Exception:
        pass

    sys.exit(ret)


if __name__ == '__main__':
    main()
