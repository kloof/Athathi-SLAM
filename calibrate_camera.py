#!/usr/bin/env python3
"""Intrinsic camera calibration using a checkerboard pattern.

Modes:
  Interactive (default): shows live feed, press spacebar to capture, 'q' to finish.
  Headless (--headless): auto-captures frames when checkerboard detected,
                         prints JSON progress to stdout for web UI integration.

Usage:
    python3 calibrate_camera.py
    python3 calibrate_camera.py --headless --device /dev/video0 --output calibration/intrinsics.yaml
"""

import argparse
import json
import os
import sys
import time

import cv2
import numpy as np
import yaml


# Checkerboard inner corners (one less than squares in each dimension)
BOARD_ROWS = 6
BOARD_COLS = 9
SQUARE_SIZE = 0.025  # 25mm default, adjust to your board

MIN_FRAMES = 15
MAX_FRAMES = 25
DIVERSITY_THRESHOLD = 80  # minimum pixel distance between board centers


def find_corners(gray):
    """Find checkerboard corners in a grayscale image."""
    flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE
    found, corners = cv2.findChessboardCorners(gray, (BOARD_COLS, BOARD_ROWS), flags)
    if found:
        cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1),
                         (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001))
    return found, corners


def board_center(corners):
    """Get the centroid of detected corners."""
    return corners.mean(axis=0)[0]


def is_diverse(new_center, existing_centers, threshold=DIVERSITY_THRESHOLD):
    """Check if the new capture is sufficiently different from existing ones."""
    for c in existing_centers:
        if np.linalg.norm(new_center - c) < threshold:
            return False
    return True


def calibrate(obj_points, img_points, image_size):
    """Run camera calibration and return results."""
    ret, camera_matrix, dist_coeffs, rvecs, tvecs = cv2.calibrateCamera(
        obj_points, img_points, image_size, None, None)
    return ret, camera_matrix, dist_coeffs


def save_intrinsics(path, camera_matrix, dist_coeffs, width, height, reproj_error):
    """Save calibration in ROS2 camera_info_manager YAML format."""
    os.makedirs(os.path.dirname(path), exist_ok=True)

    K = camera_matrix
    D = dist_coeffs.flatten()

    data = {
        'image_width': width,
        'image_height': height,
        'camera_name': 'logitech_brio',
        'camera_matrix': {
            'rows': 3, 'cols': 3,
            'data': [float(K[i, j]) for i in range(3) for j in range(3)],
        },
        'distortion_model': 'plumb_bob',
        'distortion_coefficients': {
            'rows': 1, 'cols': len(D),
            'data': [float(d) for d in D],
        },
        'rectification_matrix': {
            'rows': 3, 'cols': 3,
            'data': [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        },
        'projection_matrix': {
            'rows': 3, 'cols': 4,
            'data': [float(K[0, 0]), 0.0, float(K[0, 2]), 0.0,
                     0.0, float(K[1, 1]), float(K[1, 2]), 0.0,
                     0.0, 0.0, 1.0, 0.0],
        },
    }

    with open(path, 'w') as f:
        yaml.dump(data, f, default_flow_style=False)

    return data


def run_headless(device, width, height, output, square_size):
    """Headless auto-capture mode for web UI integration."""
    cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

    if not cap.isOpened():
        _emit({'status': 'error', 'message': 'Cannot open camera'})
        return 1

    obj_p = np.zeros((BOARD_ROWS * BOARD_COLS, 3), np.float32)
    obj_p[:, :2] = np.mgrid[0:BOARD_COLS, 0:BOARD_ROWS].T.reshape(-1, 2) * square_size

    obj_points = []
    img_points = []
    centers = []
    last_capture = 0

    _emit({'status': 'running', 'frames': 0, 'target': MIN_FRAMES})
    deadline = time.time() + 300  # 5 minute overall timeout

    try:
        while len(obj_points) < MAX_FRAMES and time.time() < deadline:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.1)
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            found, corners = find_corners(gray)

            if found and time.time() - last_capture > 1.5:
                center = board_center(corners)
                if is_diverse(center, centers):
                    obj_points.append(obj_p)
                    img_points.append(corners)
                    centers.append(center)
                    last_capture = time.time()
                    _emit({'status': 'running',
                           'frames': len(obj_points),
                           'target': MIN_FRAMES})

                    if len(obj_points) >= MIN_FRAMES:
                        break

            time.sleep(0.1)

    finally:
        cap.release()

    if len(obj_points) < 5:
        _emit({'status': 'error', 'message': f'Only {len(obj_points)} frames captured, need at least 5'})
        return 1

    _emit({'status': 'calibrating', 'frames': len(obj_points)})
    reproj_error, camera_matrix, dist_coeffs = calibrate(
        obj_points, img_points, (width, height))

    save_intrinsics(output, camera_matrix, dist_coeffs, width, height, reproj_error)
    _emit({'status': 'done', 'reproj_error': round(reproj_error, 4),
           'frames': len(obj_points), 'output': output})
    return 0


def run_interactive(device, width, height, output, square_size):
    """Interactive mode with live preview."""
    cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

    if not cap.isOpened():
        print('Error: Cannot open camera')
        return 1

    obj_p = np.zeros((BOARD_ROWS * BOARD_COLS, 3), np.float32)
    obj_p[:, :2] = np.mgrid[0:BOARD_COLS, 0:BOARD_ROWS].T.reshape(-1, 2) * square_size

    obj_points = []
    img_points = []
    centers = []

    print(f'Camera calibration — capture {MIN_FRAMES}+ frames')
    print('Press SPACE to capture when checkerboard is detected')
    print('Press Q to finish and calibrate')

    while True:
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.1)
            continue

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        found, corners = find_corners(gray)

        display = frame.copy()
        if found:
            cv2.drawChessboardCorners(display, (BOARD_COLS, BOARD_ROWS), corners, found)

        cv2.putText(display, f'Captured: {len(obj_points)}/{MIN_FRAMES}',
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        if found:
            cv2.putText(display, 'DETECTED - press SPACE',
                        (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)

        cv2.imshow('Calibration', display)
        key = cv2.waitKey(30) & 0xFF

        if key == ord(' ') and found:
            center = board_center(corners)
            if is_diverse(center, centers):
                obj_points.append(obj_p)
                img_points.append(corners)
                centers.append(center)
                print(f'  Captured frame {len(obj_points)}')
            else:
                print('  Too similar to existing capture, move the board')

        elif key == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()

    if len(obj_points) < 5:
        print(f'Error: Only {len(obj_points)} frames, need at least 5')
        return 1

    print(f'Calibrating with {len(obj_points)} frames...')
    reproj_error, camera_matrix, dist_coeffs = calibrate(
        obj_points, img_points, (width, height))

    save_intrinsics(output, camera_matrix, dist_coeffs, width, height, reproj_error)
    print(f'Reprojection error: {reproj_error:.4f} pixels')
    print(f'Saved to: {output}')
    return 0


def _emit(data):
    """Emit JSON line to stdout for headless mode."""
    print(json.dumps(data), flush=True)


def main():
    parser = argparse.ArgumentParser(description='Camera intrinsic calibration')
    parser.add_argument('--headless', action='store_true',
                        help='Auto-capture mode (no GUI)')
    parser.add_argument('--device', default='/dev/video0')
    parser.add_argument('--width', type=int, default=1280)
    parser.add_argument('--height', type=int, default=720)
    parser.add_argument('--output', default='calibration/intrinsics.yaml')
    parser.add_argument('--square-size', type=float, default=SQUARE_SIZE,
                        help='Checkerboard square size in meters')
    # Board size is fixed at 9x6 to match calibrate_extrinsics.py
    args = parser.parse_args()

    if args.headless:
        sys.exit(run_headless(args.device, args.width, args.height,
                              args.output, args.square_size))
    else:
        sys.exit(run_interactive(args.device, args.width, args.height,
                                 args.output, args.square_size))


if __name__ == '__main__':
    main()
