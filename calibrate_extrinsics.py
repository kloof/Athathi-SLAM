#!/usr/bin/env python3
"""Camera-to-LiDAR extrinsic calibration tool.

Hybrid approach:
  1. Automatic: detect a large checkerboard in both camera and LiDAR,
     solve for the 6-DOF transform using point correspondences.
  2. Visual refinement: project LiDAR points onto camera image,
     manually adjust the transform until alignment looks correct.

Requires: camera intrinsics already calibrated (calibration/intrinsics.yaml).

Usage:
    python3 calibrate_extrinsics.py                      # Interactive GUI
    python3 calibrate_extrinsics.py --refine-only         # Skip auto, just refine
    python3 calibrate_extrinsics.py --headless             # Auto phase only, no GUI
"""

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime

import cv2
import numpy as np
import yaml
from scipy.spatial.transform import Rotation


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CALIBRATION_DIR = os.path.join(SCRIPT_DIR, 'calibration')
INTRINSICS_FILE = os.path.join(CALIBRATION_DIR, 'intrinsics.yaml')
EXTRINSICS_FILE = os.path.join(CALIBRATION_DIR, 'extrinsics.yaml')

ROS_SETUP = '/opt/ros/humble/setup.bash'
DRIVER_SETUP = '/home/talal/unilidar_sdk2/unitree_lidar_ros2/install/setup.bash'

# Checkerboard parameters (must match calibrate_camera.py)
BOARD_ROWS = 7    # 8 squares - 1
BOARD_COLS = 10   # 11 squares - 1
SQUARE_SIZE = 0.022  # 22mm measured from print

# LiDAR coordinate frame:
#   Origin: center of L2 bottom mounting surface
#   +X: opposite cable outlet (forward)
#   +Y: 90 deg CCW from X (left)
#   +Z: perpendicular to bottom (up)


def load_intrinsics():
    """Load camera intrinsics from YAML."""
    if not os.path.isfile(INTRINSICS_FILE):
        raise FileNotFoundError(f'Intrinsics not found: {INTRINSICS_FILE}')

    with open(INTRINSICS_FILE) as f:
        data = yaml.safe_load(f)

    K = np.array(data['camera_matrix']['data']).reshape(3, 3)
    D = np.array(data['distortion_coefficients']['data'])
    return K, D, data['image_width'], data['image_height']


def load_extrinsics():
    """Load existing extrinsics if available."""
    if not os.path.isfile(EXTRINSICS_FILE):
        return None

    with open(EXTRINSICS_FILE) as f:
        data = yaml.safe_load(f)

    t = data['translation']
    r = data['rotation']
    return {
        'translation': np.array([t['x'], t['y'], t['z']]),
        'rotation': np.array([r['x'], r['y'], r['z'], r['w']]),
    }


def save_extrinsics(translation, quaternion, method='checkerboard_with_refinement'):
    """Save extrinsics to YAML."""
    os.makedirs(CALIBRATION_DIR, exist_ok=True)

    # Convert quaternion to RPY for human readability
    rot = Rotation.from_quat(quaternion)  # [x, y, z, w] scipy convention
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
        'calibration_date': datetime.now().isoformat(),
        'method': method,
    }

    with open(EXTRINSICS_FILE, 'w') as f:
        yaml.dump(data, f, default_flow_style=False)

    return data


def quat_to_rotation_matrix(q):
    """Convert quaternion [x, y, z, w] to 3x3 rotation matrix."""
    return Rotation.from_quat(q).as_matrix()


def euler_to_quat(roll_deg, pitch_deg, yaw_deg):
    """Convert RPY (degrees) to quaternion [x, y, z, w]."""
    return Rotation.from_euler('xyz', [roll_deg, pitch_deg, yaw_deg],
                                degrees=True).as_quat()


def project_points_to_image(points_3d, K, D, R, t):
    """Project 3D LiDAR points into camera image coordinates.

    Args:
        points_3d: Nx3 array of points in LiDAR frame
        K: 3x3 camera matrix
        D: distortion coefficients
        R: 3x3 rotation matrix (lidar to camera)
        t: 3x1 translation vector (lidar to camera)

    Returns:
        pixels: Nx2 array of pixel coordinates
        mask: boolean array — True for points in front of camera
    """
    # Transform points from LiDAR frame to camera frame
    points_cam = (R @ points_3d.T).T + t.flatten()

    # Points must be in front of camera (positive Z in camera frame)
    mask = points_cam[:, 2] > 0.1

    if not mask.any():
        return np.zeros((0, 2)), mask

    # Project using camera model
    rvec = cv2.Rodrigues(R)[0]
    tvec = t.reshape(3, 1)
    pixels, _ = cv2.projectPoints(points_3d[mask], rvec, tvec, K, D)
    pixels = pixels.reshape(-1, 2)

    return pixels, mask


def capture_lidar_cloud(rclpy_node=None, timeout=5.0):
    """Capture a single LiDAR point cloud via ROS2 subscriber.

    Args:
        rclpy_node: existing rclpy node to reuse, or None to create a temporary one.
        timeout: seconds to wait for a message.

    Returns Nx3 numpy array or None.
    """
    try:
        import rclpy
        from sensor_msgs.msg import PointCloud2
        import struct

        own_init = False
        own_node = rclpy_node is None
        node = rclpy_node

        if own_node:
            if not rclpy.ok():
                rclpy.init()
                own_init = True
            node = rclpy.create_node('_calib_capture')

        cloud_data = {'msg': None}

        def cb(msg):
            cloud_data['msg'] = msg

        sub = node.create_subscription(PointCloud2, '/unilidar/cloud', cb, 1)

        try:
            start = time.time()
            while cloud_data['msg'] is None and time.time() - start < timeout:
                rclpy.spin_once(node, timeout_sec=0.5)
        finally:
            node.destroy_subscription(sub)
            if own_node:
                node.destroy_node()
            if own_init:
                rclpy.shutdown()

        msg = cloud_data['msg']
        if msg is None:
            return None

        # Parse PointCloud2 — extract x, y, z
        points = []
        point_step = msg.point_step
        data = bytes(msg.data)
        for i in range(msg.width * msg.height):
            offset = i * point_step
            x = struct.unpack_from('f', data, offset)[0]
            y = struct.unpack_from('f', data, offset + 4)[0]
            z = struct.unpack_from('f', data, offset + 8)[0]
            if np.isfinite(x) and np.isfinite(y) and np.isfinite(z):
                points.append([x, y, z])

        return np.array(points) if points else None

    except Exception as e:
        print(f'LiDAR capture error: {e}')
        return None


def capture_camera_frame(device='/dev/video0', width=1280, height=720):
    """Capture a single camera frame."""
    cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

    ret, frame = cap.read()
    cap.release()
    return frame if ret else None


def segment_board_plane(cloud, board_distance_range=(0.3, 2.0)):
    """Segment points that likely belong to the checkerboard plane.

    Filters by distance range, then uses RANSAC to find the dominant plane.
    Returns inlier points and the plane model.
    """
    # Filter by distance from sensor
    dists = np.linalg.norm(cloud, axis=1)
    mask = (dists > board_distance_range[0]) & (dists < board_distance_range[1])
    filtered = cloud[mask]

    if len(filtered) < 50:
        return None, None

    # RANSAC plane fitting
    best_inliers = None
    best_model = None
    n_iters = 200
    threshold = 0.01  # 1cm

    for _ in range(n_iters):
        idx = np.random.choice(len(filtered), 3, replace=False)
        p1, p2, p3 = filtered[idx]
        normal = np.cross(p2 - p1, p3 - p1)
        norm = np.linalg.norm(normal)
        if norm < 1e-10:
            continue
        normal /= norm
        d = -normal.dot(p1)

        distances = np.abs(filtered.dot(normal) + d)
        inliers = distances < threshold

        if best_inliers is None or inliers.sum() > best_inliers.sum():
            best_inliers = inliers
            best_model = (normal, d)

    if best_inliers is None or best_inliers.sum() < 20:
        return None, None

    return filtered[best_inliers], best_model


def auto_calibrate(device, K, D, num_captures=3, headless=False):
    """Automatic extrinsic calibration using checkerboard.

    Note: This computes a camera-to-board transform as an initial estimate.
    The visual refinement phase is needed to get the actual camera-to-LiDAR transform.
    The LiDAR plane segmentation validates that the board is visible to both sensors.

    In headless mode, captures are taken automatically with a delay between each.

    Returns (translation, quaternion) or None.
    """
    import rclpy

    obj_p = np.zeros((BOARD_ROWS * BOARD_COLS, 3), np.float32)
    obj_p[:, :2] = np.mgrid[0:BOARD_COLS, 0:BOARD_ROWS].T.reshape(-1, 2) * SQUARE_SIZE

    all_img_points = []
    all_obj_points = []

    print(f'Auto calibration — hold a large checkerboard visible to both sensors')
    print(f'Need {num_captures} captures at different positions')
    print(f'This gives an initial estimate — use visual refinement to fine-tune')

    # Initialize rclpy once for all captures
    own_init = False
    if not rclpy.ok():
        rclpy.init()
        own_init = True
    node = rclpy.create_node('_calib_extrinsics')

    try:
        for i in range(num_captures):
            if headless:
                print(f'\nCapture {i+1}/{num_captures} — auto-capturing in 5 seconds...')
                time.sleep(5)
            else:
                print(f'\nCapture {i+1}/{num_captures} — position the board and press ENTER')
                input()

            # Capture camera frame
            frame = capture_camera_frame(device)
            if frame is None:
                print('  Failed to capture camera frame')
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            found, corners = cv2.findChessboardCorners(
                gray, (BOARD_COLS, BOARD_ROWS),
                cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE)

            if not found:
                print('  Checkerboard not found in camera image')
                continue

            cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1),
                             (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001))

            # Capture LiDAR cloud
            cloud = capture_lidar_cloud(rclpy_node=node, timeout=5.0)
            if cloud is None:
                print('  Failed to capture LiDAR cloud')
                continue

            # Segment board plane from LiDAR (validates board is visible to both)
            plane_points, plane_model = segment_board_plane(cloud)
            if plane_points is None:
                print('  Could not segment board plane from LiDAR')
                continue

            print(f'  Camera: checkerboard found, LiDAR: {len(plane_points)} plane points')
            all_img_points.append(corners.reshape(-1, 2))
            all_obj_points.append(obj_p)
    finally:
        node.destroy_node()
        if own_init:
            rclpy.shutdown()

    if len(all_img_points) < 1:
        print('No valid captures for calibration')
        return None

    # Use solvePnP with all captures — pick the one with lowest reprojection error
    # This gives camera-to-board transform as an initial estimate for the
    # camera-to-lidar extrinsics. Visual refinement phase will fine-tune.
    best_error = float('inf')
    best_rvec = None
    best_tvec = None

    for i in range(len(all_img_points)):
        success, rvec, tvec = cv2.solvePnP(
            all_obj_points[i], all_img_points[i], K, D)
        if not success:
            continue
        projected, _ = cv2.projectPoints(all_obj_points[i], rvec, tvec, K, D)
        error = np.mean(np.linalg.norm(
            projected.reshape(-1, 2) - all_img_points[i], axis=1))
        print(f'  Capture {i+1}: reprojection error = {error:.2f} px')
        if error < best_error:
            best_error = error
            best_rvec = rvec
            best_tvec = tvec

    if best_rvec is None:
        print('solvePnP failed on all captures')
        return None

    print(f'Best capture: {best_error:.2f} px reprojection error')
    R_cam_board, _ = cv2.Rodrigues(best_rvec)
    t_cam_board = best_tvec.flatten()

    q = Rotation.from_matrix(R_cam_board).as_quat()
    return t_cam_board, q


def refine_interactive(K, D, device='/dev/video0'):
    """Interactive visual refinement of extrinsics."""
    import rclpy

    print('\nVisual refinement mode')
    print('Controls:')
    print('  Arrow keys / IJKL: translate X/Y')
    print('  +/-: translate Z')
    print('  W/S: pitch, A/D: yaw, Q/E: roll')
    print('  R: reset to current saved values')
    print('  ENTER: save and exit')
    print('  ESC: exit without saving')

    # Load current extrinsics or start from zero
    ext = load_extrinsics()
    if ext:
        translation = ext['translation'].copy()
        quaternion = ext['rotation'].copy()
    else:
        translation = np.array([0.0, 0.0, 0.0])
        quaternion = np.array([0.0, 0.0, 0.0, 1.0])

    rpy = Rotation.from_quat(quaternion).as_euler('xyz', degrees=True)

    step_t = 0.005  # 5mm translation step
    step_r = 1.0    # 1 degree rotation step

    # Hold camera and rclpy node open for the entire session
    cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

    own_init = False
    if not rclpy.ok():
        rclpy.init()
        own_init = True
    node = rclpy.create_node('_calib_refine')

    try:
      while True:
        # Capture
        ret, frame = cap.read()
        if not ret:
            frame = None
        cloud = capture_lidar_cloud(rclpy_node=node, timeout=3.0)

        if frame is None:
            print('No camera frame')
            time.sleep(0.5)
            continue

        display = frame.copy()

        if cloud is not None and len(cloud) > 0:
            R = Rotation.from_euler('xyz', rpy, degrees=True).as_matrix()
            t = translation

            pixels, mask = project_points_to_image(cloud, K, D.flatten(), R, t)

            if len(pixels) > 0:
                # Color by depth
                depths = cloud[mask][:, 2] if mask.any() else cloud[:, 2]
                d_min, d_max = depths.min(), max(depths.max(), depths.min() + 0.1)
                d_norm = ((depths - d_min) / (d_max - d_min) * 255).astype(np.uint8)
                colors = cv2.applyColorMap(d_norm.reshape(-1, 1), cv2.COLORMAP_JET)

                h, w = frame.shape[:2]
                for j, (px, py) in enumerate(pixels.astype(int)):
                    if 0 <= px < w and 0 <= py < h:
                        c = colors[j][0].tolist()
                        cv2.circle(display, (px, py), 2, c, -1)

        # Draw info
        info = (f'T: [{translation[0]:.3f}, {translation[1]:.3f}, {translation[2]:.3f}] '
                f'RPY: [{rpy[0]:.1f}, {rpy[1]:.1f}, {rpy[2]:.1f}]')
        cv2.putText(display, info, (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

        cv2.imshow('Extrinsic Refinement', display)
        key = cv2.waitKey(100) & 0xFF

        fine = False  # TODO: detect shift key

        s_t = step_t * (0.2 if fine else 1.0)
        s_r = step_r * (0.2 if fine else 1.0)

        if key == 81 or key == ord('j'):     # left arrow
            translation[1] += s_t
        elif key == 83 or key == ord('l'):   # right arrow
            translation[1] -= s_t
        elif key == 82 or key == ord('i'):   # up arrow
            translation[0] += s_t
        elif key == 84 or key == ord('k'):   # down arrow
            translation[0] -= s_t
        elif key == ord('+') or key == ord('='):
            translation[2] += s_t
        elif key == ord('-'):
            translation[2] -= s_t
        elif key == ord('w'):
            rpy[1] += s_r
        elif key == ord('s'):
            rpy[1] -= s_r
        elif key == ord('a'):
            rpy[2] += s_r
        elif key == ord('d'):
            rpy[2] -= s_r
        elif key == ord('q'):
            rpy[0] += s_r
        elif key == ord('e'):
            rpy[0] -= s_r
        elif key == ord('r'):
            ext = load_extrinsics()
            if ext:
                translation = ext['translation'].copy()
                rpy = Rotation.from_quat(ext['rotation']).as_euler('xyz', degrees=True)
                print('Reset to saved values')
        elif key == 13:  # ENTER
            quaternion = Rotation.from_euler('xyz', rpy, degrees=True).as_quat()
            save_extrinsics(translation, quaternion)
            print(f'Saved extrinsics to {EXTRINSICS_FILE}')
            break
        elif key == 27:  # ESC
            print('Exited without saving')
            break

    finally:
        cap.release()
        node.destroy_node()
        if own_init:
            rclpy.shutdown()
        cv2.destroyAllWindows()


def main():
    parser = argparse.ArgumentParser(description='Camera-to-LiDAR extrinsic calibration')
    parser.add_argument('--device', default='/dev/video0')
    parser.add_argument('--refine-only', action='store_true',
                        help='Skip auto calibration, go straight to visual refinement')
    parser.add_argument('--headless', action='store_true',
                        help='Run auto phase only, no GUI (for web integration)')
    args = parser.parse_args()

    K, D, w, h = load_intrinsics()
    print(f'Loaded intrinsics: {w}x{h}, fx={K[0,0]:.1f}, fy={K[1,1]:.1f}')

    if not args.refine_only:
        print('\n--- Auto Calibration Phase ---')
        result = auto_calibrate(args.device, K, D, headless=args.headless)
        if result:
            translation, quaternion = result
            save_extrinsics(translation, quaternion, method='checkerboard_auto')
            print(f'Initial estimate saved to {EXTRINSICS_FILE}')
        else:
            print('Auto calibration failed — proceed to manual refinement')

    if not args.headless:
        print('\n--- Visual Refinement Phase ---')
        refine_interactive(K, D, device=args.device)


if __name__ == '__main__':
    main()
