#!/usr/bin/env python3
"""
set_brio_fov.py - Set Logitech Brio FOV on Linux via UVC Extension Unit

Usage:
    python3 set_brio_fov.py 90      # Set to 90° (widest)
    python3 set_brio_fov.py 78      # Set to 78° (medium)
    python3 set_brio_fov.py 65      # Set to 65° (narrowest)
    python3 set_brio_fov.py          # Just read current FOV

This directly writes to the Brio's proprietary UVC extension unit (GUID
49e40215-f434-47fe-b158-0e885023e51b), selector 0x05. This is the same
mechanism that Logitech G Hub / Logi Tune uses on Windows.

Works on USB 2.0 AND USB 3.0. The FOV change is real and immediate -
the camera uses a different sensor crop for each FOV setting.
"""
import ctypes
import os
import sys
import time
from fcntl import ioctl

# --- UVC Extension Unit constants ---
UVC_SET_CUR = 0x01
UVC_GET_CUR = 0x81
UVC_GET_LEN = 0x85

# Brio FOV extension unit
LOGITECH_BRIO_GUID = b'\x15\x02\xe4\x49\x34\xf4\xfe\x47\xb1\x58\x0e\x88\x50\x23\xe5\x1b'
FOV_SELECTOR = 0x05

FOV_VALUES = {
    '90': 0x00,
    '78': 0x01, 
    '65': 0x02,
}
FOV_NAMES = {v: k for k, v in FOV_VALUES.items()}

class uvc_xu_control_query(ctypes.Structure):
    _fields_ = [
        ('unit', ctypes.c_uint8),
        ('selector', ctypes.c_uint8),
        ('query', ctypes.c_uint8),
        ('size', ctypes.c_uint16),
        ('data', ctypes.c_void_p),
    ]

def _IOC(d, t, nr, size):
    return (d << 30) | (t << 8) | nr | (size << 16)

UVCIOC_CTRL_QUERY = _IOC(3, ord('u'), 0x21, ctypes.sizeof(uvc_xu_control_query))

def find_unit_id(device):
    """Find the XU unit ID from sysfs descriptors."""
    devname = os.path.basename(os.path.realpath(device))
    descfile = f'/sys/class/video4linux/{devname}/../../../descriptors'
    if not os.path.isfile(descfile):
        return None
    with open(descfile, 'rb') as f:
        data = f.read()
    idx = data.find(LOGITECH_BRIO_GUID)
    if idx > 0:
        return data[idx - 1]
    return None

def xu_query(fd, unit_id, selector, query_type, buf):
    q = uvc_xu_control_query()
    q.unit = unit_id
    q.selector = selector
    q.query = query_type
    q.size = len(buf) - 1  # ctypes adds null byte
    q.data = ctypes.cast(ctypes.pointer(buf), ctypes.c_void_p)
    ioctl(fd, UVCIOC_CTRL_QUERY, q)

def get_fov(fd, unit_id):
    buf = ctypes.create_string_buffer(2)
    xu_query(fd, unit_id, FOV_SELECTOR, UVC_GET_CUR, buf)
    return buf[0][0]

def set_fov(fd, unit_id, value):
    buf = ctypes.create_string_buffer(2)
    buf[0] = value
    xu_query(fd, unit_id, FOV_SELECTOR, UVC_SET_CUR, buf)

def main():
    device = '/dev/video0'
    
    # Allow specifying device as second arg
    args = [a for a in sys.argv[1:] if not a.startswith('/dev/')]
    dev_args = [a for a in sys.argv[1:] if a.startswith('/dev/')]
    if dev_args:
        device = dev_args[0]
    
    unit_id = find_unit_id(device)
    if unit_id is None:
        print(f"ERROR: Could not find Brio extension unit on {device}")
        print("Make sure a Logitech Brio is connected.")
        sys.exit(1)
    
    fd = os.open(device, os.O_RDWR)
    
    try:
        current = get_fov(fd, unit_id)
        current_name = FOV_NAMES.get(current, f'unknown(0x{current:02x})')
        print(f"Current FOV: {current_name}° (unit_id={unit_id}, device={device})")
        
        if args:
            target = args[0]
            if target not in FOV_VALUES:
                print(f"ERROR: Invalid FOV '{target}'. Use 65, 78, or 90.")
                sys.exit(1)
            
            target_val = FOV_VALUES[target]
            if current == target_val:
                print(f"FOV is already set to {target}°. No change needed.")
            else:
                set_fov(fd, unit_id, target_val)
                time.sleep(0.1)
                verify = get_fov(fd, unit_id)
                verify_name = FOV_NAMES.get(verify, f'unknown(0x{verify:02x})')
                
                if verify == target_val:
                    print(f"FOV set to {target}° successfully.")
                else:
                    print(f"WARNING: Set to {target}° but readback is {verify_name}°!")
                    sys.exit(1)
    finally:
        os.close(fd)

if __name__ == '__main__':
    main()
