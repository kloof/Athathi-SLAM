"""Single source of truth for Brio camera settings.

Both the web app (app.py) and the calibration tools import from here so
recording and calibration capture under identical locked conditions.
"""
import os
import subprocess


def find_brio_device():
    """Return /dev/videoN for the Brio's RGB capture interface, or None."""
    sysfs = '/sys/class/video4linux'
    if not os.path.isdir(sysfs):
        return None
    for dev_name in sorted(os.listdir(sysfs)):
        try:
            with open(os.path.join(sysfs, dev_name, 'name')) as f:
                name = f.read().strip()
            with open(os.path.join(sysfs, dev_name, 'index')) as f:
                index = int(f.read().strip())
            if 'Logitech BRIO' in name and index == 0:
                return f'/dev/{dev_name}'
        except (OSError, ValueError):
            continue
    return None


CAMERA_DEVICE = find_brio_device() or '/dev/video0'
CAMERA_WIDTH = 1280
CAMERA_HEIGHT = 720
CAMERA_FPS_CAPTURE = 15        # matches LiDAR's measured ~11.9 Hz (next-supported Brio rate up); clean 1:1 pairing for time sync, half the thermal/data load of 30 fps
CAMERA_EXPOSURE_100US = 80   # 8 ms shutter -- minor blur at handheld pan speeds
CAMERA_GAIN = 128            # 0-255 analog gain -- bumped from 50 to recover shadow detail
CAMERA_WB_KELVIN = 3500      # locked white balance -- cools warm-LED-dominant room (Brio: lower K = cooler output)
CAMERA_FOCUS_ABS = 0         # 0 = infinity, 255 = closest -- room-scale walls
CAMERA_BRIGHTNESS = 128      # sensor default -- previous 50 was crushing shadows
CAMERA_SHARPNESS = 64        # below default 128 -- halos hurt SLAM feature descriptors
CAMERA_POWERLINE_HZ = 1      # 0=disabled, 1=50Hz (Kuwait/EU/Asia), 2=60Hz (Americas)


def lock_camera_controls(device=CAMERA_DEVICE):
    # Brio requires flipping the auto flags OFF first — target controls are
    # `flags=inactive` until manual mode is engaged, so a single combined
    # --set-ctrl silently drops them. Two calls, in order.
    subprocess.run(
        ['v4l2-ctl', '-d', device,
         '--set-ctrl=auto_exposure=1,'
         'white_balance_automatic=0,'
         'focus_automatic_continuous=0'],
        capture_output=True, timeout=2
    )
    subprocess.run(
        ['v4l2-ctl', '-d', device,
         f'--set-ctrl=exposure_time_absolute={CAMERA_EXPOSURE_100US},'
         f'exposure_dynamic_framerate=0,'
         f'gain={CAMERA_GAIN},'
         f'brightness={CAMERA_BRIGHTNESS},'
         f'sharpness={CAMERA_SHARPNESS},'
         f'power_line_frequency={CAMERA_POWERLINE_HZ},'
         f'white_balance_temperature={CAMERA_WB_KELVIN},'
         f'focus_absolute={CAMERA_FOCUS_ABS}'],
        capture_output=True, timeout=2
    )
    try:
        result = subprocess.run(
            ['v4l2-ctl', '-d', device,
             '--get-ctrl=auto_exposure,exposure_time_absolute,'
             'exposure_dynamic_framerate,gain,brightness,sharpness,'
             'power_line_frequency,white_balance_automatic,'
             'white_balance_temperature,focus_automatic_continuous,'
             'focus_absolute'],
            capture_output=True, timeout=2, text=True
        )
        return result.stdout.strip()
    except Exception:
        return ''
