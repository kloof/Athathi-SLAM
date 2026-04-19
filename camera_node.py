#!/usr/bin/env python3
"""Custom ROS2 camera publisher for the Logitech Brio.

Replaces usb_cam, which on this Pi's Humble/ARM64 stack:
  - raw_mjpeg strips chroma (produces 1-component grayscale JPEGs)
  - mjpeg2rgb crashes with 'Unable to exchange buffer with the driver'
  - hardcodes brightness=50 on init, overriding v4l2 locks

Architecture:
  - ffmpeg subprocess reads MJPG from /dev/video* and writes to a 1 MB pipe
  - Dedicated reader thread parses JPEG SOI/EOI markers and pushes bytes into
    a bounded ring queue (drops oldest on overflow)
  - Main thread pops from queue and publishes CompressedImage (BEST_EFFORT)
  - On ffmpeg failure, session loop unbinds/rebinds the USB port and retries
"""
import array
import collections
import fcntl
import glob
import os
import signal
import subprocess
import sys
import threading
import time
import yaml

import rclpy
from rclpy.qos import (QoSProfile, QoSReliabilityPolicy, QoSHistoryPolicy,
                       QoSDurabilityPolicy)
from sensor_msgs.msg import CompressedImage, CameraInfo

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from camera_config import (  # noqa: E402
    CAMERA_WIDTH, CAMERA_HEIGHT, CAMERA_FPS_CAPTURE,
    lock_camera_controls, find_brio_device,
)

INTRINSICS_FILE = os.path.join(SCRIPT_DIR, 'calibration', 'intrinsics.yaml')
FRAME_ID = 'camera_optical_frame'

F_SETPIPE_SZ = 1031
FIONREAD = 0x541B
PIPE_SIZE = 1 << 20       # 1 MB, capped by /proc/sys/fs/pipe-max-size
READ_CHUNK = 131072       # 128 KB — typically one full 720p MJPG frame
QUEUE_SIZE = 5            # ~165 ms of buffered frames at 30 fps


def load_camera_info(path):
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        d = yaml.safe_load(f)
    info = CameraInfo()
    info.width = int(d.get('image_width', CAMERA_WIDTH))
    info.height = int(d.get('image_height', CAMERA_HEIGHT))
    info.distortion_model = d.get('distortion_model', 'plumb_bob')
    info.d = list(d['distortion_coefficients']['data'])
    info.k = list(d['camera_matrix']['data'])
    info.r = list(d['rectification_matrix']['data'])
    info.p = list(d['projection_matrix']['data'])
    return info


def find_brio_usb_port():
    """Return the sysfs USB bus identifier for the Brio (e.g. '1-1.3')."""
    for idVendor_path in glob.glob('/sys/bus/usb/devices/*/idVendor'):
        try:
            if open(idVendor_path).read().strip() != '046d':
                continue
            dev_dir = os.path.dirname(idVendor_path)
            if open(os.path.join(dev_dir, 'idProduct')).read().strip() == '085e':
                return os.path.basename(dev_dir)
        except OSError:
            continue
    return None


def reset_brio_usb(logger):
    """Unbind and rebind the Brio's USB port to clear a wedged UVC state.

    Typically triggered when ffmpeg reports 'Protocol error' or 'No such
    device' after a stream interruption. The Brio sometimes lands in a
    state where v4l2 ioctls fail until the USB stack re-enumerates it.
    """
    port = find_brio_usb_port()
    if port is None:
        logger.warn('USB reset: Brio not enumerated, cannot reset')
        return False
    try:
        logger.info(f'USB reset: unbinding {port}')
        with open('/sys/bus/usb/drivers/usb/unbind', 'w') as f:
            f.write(port)
    except OSError as e:
        logger.warn(f'USB reset: unbind failed ({e})')
        return False
    time.sleep(3)
    try:
        with open('/sys/bus/usb/drivers/usb/bind', 'w') as f:
            f.write(port)
        logger.info(f'USB reset: rebound {port}')
    except OSError as e:
        logger.warn(f'USB reset: rebind failed ({e})')
        return False
    # Wait for /dev/video* to reappear
    deadline = time.time() + 10.0
    while time.time() < deadline:
        dev = find_brio_device()
        if dev and os.path.exists(dev):
            # Give uvcvideo a moment to finalize control negotiation
            time.sleep(2)
            return True
        time.sleep(0.5)
    logger.warn('USB reset: device did not reappear within 10s')
    return False


def start_ffmpeg(device):
    """Launch an ffmpeg MJPG reader piping raw JPEG frames to stdout."""
    ffmpeg = subprocess.Popen(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error',
         '-analyzeduration', '3000000', '-probesize', '3000000',
         '-f', 'v4l2', '-input_format', 'mjpeg',
         '-video_size', f'{CAMERA_WIDTH}x{CAMERA_HEIGHT}',
         '-framerate', str(CAMERA_FPS_CAPTURE),
         '-i', device,
         '-c:v', 'copy', '-f', 'mjpeg', 'pipe:1'],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        bufsize=0, preexec_fn=os.setsid,
    )
    try:
        fcntl.fcntl(ffmpeg.stdout.fileno(), F_SETPIPE_SZ, PIPE_SIZE)
    except (OSError, AttributeError):
        pass
    return ffmpeg


def kill_ffmpeg(ffmpeg):
    try:
        os.killpg(os.getpgid(ffmpeg.pid), signal.SIGTERM)
        ffmpeg.wait(timeout=2)
    except Exception:
        try:
            os.killpg(os.getpgid(ffmpeg.pid), signal.SIGKILL)
        except Exception:
            pass


def run_session(node, img_pub, info_pub, info, stop, device, logger):
    """One ffmpeg session: read → parse → publish until ffmpeg dies or stop.

    Returns the number of frames published. Non-zero return signals a
    successful session; zero suggests ffmpeg never produced a valid frame
    (caller may trigger a USB reset and retry).
    """
    lock_camera_controls(device)
    time.sleep(0.5)

    ffmpeg = start_ffmpeg(device)
    queue = collections.deque(maxlen=QUEUE_SIZE)
    queue_cond = threading.Condition()
    reader_done = threading.Event()

    def reader():
        buf = bytearray()
        while not stop.is_set():
            chunk = ffmpeg.stdout.read(READ_CHUNK)
            if not chunk:
                break
            buf.extend(chunk)
            while True:
                soi = buf.find(b'\xff\xd8')
                if soi < 0:
                    buf.clear(); break
                eoi = buf.find(b'\xff\xd9', soi + 2)
                if eoi < 0:
                    if soi > 0:
                        del buf[:soi]
                    break
                jpeg = bytes(buf[soi:eoi + 2])
                del buf[:eoi + 2]
                with queue_cond:
                    queue.append(jpeg)     # deque with maxlen auto-drops oldest
                    queue_cond.notify()
        reader_done.set()
        with queue_cond:
            queue_cond.notify_all()

    rd = threading.Thread(target=reader, name='brio_reader', daemon=True)
    rd.start()

    frames = 0
    try:
        while not stop.is_set():
            with queue_cond:
                while not queue and not stop.is_set() and not reader_done.is_set():
                    queue_cond.wait(timeout=0.1)
                if not queue:
                    if reader_done.is_set():
                        break
                    continue
                jpeg = queue.popleft()

            stamp = node.get_clock().now().to_msg()
            msg = CompressedImage()
            msg.header.stamp = stamp
            msg.header.frame_id = FRAME_ID
            msg.format = 'jpeg'
            # array.array('B', …) side-steps rclpy's byte-by-byte uint8[]
            # conversion that caps Python publish at ~25 Hz on 60 KB payloads.
            msg.data = array.array('B', jpeg)
            img_pub.publish(msg)

            if info is not None and frames % 30 == 0:
                info.header.stamp = stamp
                info.header.frame_id = FRAME_ID
                info_pub.publish(info)
            frames += 1
    finally:
        kill_ffmpeg(ffmpeg)
        rd.join(timeout=2)
    return frames


def main():
    rclpy.init()
    node = rclpy.create_node('brio_camera')
    logger = node.get_logger()

    # BEST_EFFORT publish: non-blocking enqueue, no per-frame ACK wait.
    # rosbag2 defaults to RELIABLE sub (incompatible); app.py passes a
    # QoS override yaml to force BEST_EFFORT on the bag side.
    img_qos = QoSProfile(
        history=QoSHistoryPolicy.KEEP_LAST, depth=5,
        reliability=QoSReliabilityPolicy.BEST_EFFORT,
        durability=QoSDurabilityPolicy.VOLATILE,
    )
    info_qos = QoSProfile(
        history=QoSHistoryPolicy.KEEP_LAST, depth=1,
        reliability=QoSReliabilityPolicy.RELIABLE,
        durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
    )
    img_pub = node.create_publisher(
        CompressedImage, '/camera/image_raw/compressed', img_qos)
    info_pub = node.create_publisher(
        CameraInfo, '/camera/camera_info', info_qos)
    info = load_camera_info(INTRINSICS_FILE)

    logger.info(
        f'Brio camera: {CAMERA_WIDTH}x{CAMERA_HEIGHT}@{CAMERA_FPS_CAPTURE}fps, '
        f'intrinsics={"yes" if info else "no"}'
    )

    stop = threading.Event()
    def _sig(*_): stop.set()
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    # Session loop: on ffmpeg failure, try USB reset and continue. Limit
    # consecutive USB resets to prevent thrash if the device is truly gone.
    consec_resets = 0
    total_frames = 0
    while not stop.is_set():
        device = find_brio_device()
        if device is None:
            logger.warn('no Brio device found; waiting 2s')
            time.sleep(2)
            continue
        logger.info(f'session start on {device} (total_frames={total_frames})')
        frames = run_session(node, img_pub, info_pub, info, stop, device, logger)
        total_frames += frames
        logger.warn(f'session ended after {frames} frames')
        if stop.is_set():
            break
        # If the session produced many frames before dying, it was a real
        # USB hiccup — reset + retry. If it produced ~zero, either fresh
        # enumeration is still settling or something is very wrong;
        # exponential backoff before giving up.
        if frames > 30:
            consec_resets = 0
        else:
            consec_resets += 1
            if consec_resets >= 3:
                logger.error('3 consecutive empty sessions; giving up')
                break
        if not reset_brio_usb(logger):
            time.sleep(2)

    logger.info(f'shutting down (total_frames={total_frames})')
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
