#!/usr/bin/env python3
"""Standalone floor leveling for LiDAR PLY point clouds.

Reads a binary PLY, finds the floor plane via RANSAC, rotates the cloud so the
floor normal maps to Z-up, and shifts the floor to Z=0. Self-contained: the
PLY I/O helpers live in this file so the module can be copied and used
elsewhere without pulling in the rest of the floor plan pipeline.

Usage:
    python3 level.py input.ply                # writes input_leveled.ply
    python3 level.py input.ply -o out.ply
"""

import argparse
import os
import sys

import numpy as np
from scipy.spatial.transform import Rotation


# ---------------------------------------------------------------------------
# PLY I/O
# ---------------------------------------------------------------------------

def read_ply(path):
    """Read a binary little-endian PLY file.

    Returns (points, properties, comments) where *points* is a float32 array
    of shape (N, len(properties)), *properties* is a list of property names,
    and *comments* is a list of comment strings (without the 'comment ' prefix).

    Handles both float32 and float64 (double) properties.
    """
    with open(path, 'rb') as f:
        properties = []
        prop_dtypes = []
        comments = []
        vertex_count = 0
        while True:
            raw = f.readline()
            if not raw:
                raise ValueError(f'Unexpected EOF before end_header in {path}')
            line = raw.decode('utf-8', errors='replace').strip()
            if line == 'end_header':
                break
            if line.startswith('element vertex'):
                vertex_count = int(line.split()[-1])
            elif line.startswith('property double') or line.startswith('property float64'):
                properties.append(line.split()[-1])
                prop_dtypes.append('<f8')
            elif line.startswith('property float'):
                properties.append(line.split()[-1])
                prop_dtypes.append('<f4')
            elif line.startswith('comment '):
                comments.append(line[len('comment '):])

        if vertex_count == 0:
            raise ValueError(f'PLY contains 0 vertices: {path}')

        n_props = len(properties)
        if prop_dtypes and all(d == prop_dtypes[0] for d in prop_dtypes):
            data = np.fromfile(f, dtype=prop_dtypes[0],
                               count=vertex_count * n_props)
            points = data.reshape(vertex_count, n_props).astype(np.float32)
        else:
            dt = np.dtype([(p, d) for p, d in zip(properties, prop_dtypes)])
            raw = np.fromfile(f, dtype=dt, count=vertex_count)
            points = np.column_stack(
                [raw[p].astype(np.float32) for p in properties])

    return points, properties, comments


def write_ply(path, points, properties, comments=None):
    """Write a binary little-endian PLY file."""
    n_points = len(points)
    comments_str = ''.join(f'comment {c}\n' for c in (comments or []))
    props_str = ''.join(f'property float {p}\n' for p in properties)
    header = (
        f'ply\nformat binary_little_endian 1.0\n'
        f'{comments_str}'
        f'element vertex {n_points}\n{props_str}end_header\n'
    ).encode('utf-8')

    with open(path, 'wb') as f:
        f.write(header)
        f.write(points.astype(np.float32).tobytes())


# ---------------------------------------------------------------------------
# Floor leveling
# ---------------------------------------------------------------------------

def _numpy_to_o3d(xyz):
    """Convert Nx3 numpy array to Open3D point cloud."""
    import open3d as o3d
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(xyz.astype(np.float64))
    return pcd


def detect_floor_plane(points, distance_thresh=0.03, max_attempts=3):
    """Find the floor plane via iterative RANSAC.

    Tries up to *max_attempts* planes, returning the first one whose normal
    is within 45 deg of the candidate vertical axis (the axis with the smallest
    point cloud extent).

    Returns (normal, inlier_indices) or None if no horizontal plane found.
    """
    import open3d as o3d  # noqa: F401  (used transitively via _numpy_to_o3d)

    xyz = points[:, :3]
    extents = xyz.max(axis=0) - xyz.min(axis=0)
    vert_axis = int(np.argmin(extents))
    axis_names = ['X', 'Y', 'Z']
    print(f'  Candidate vertical axis: {axis_names[vert_axis]} '
          f'(extents: X={extents[0]:.2f}, Y={extents[1]:.2f}, Z={extents[2]:.2f})')

    remaining = _numpy_to_o3d(xyz)

    for attempt in range(max_attempts):
        pts = np.asarray(remaining.points)
        if len(pts) < 100:
            break

        try:
            plane_model, inliers = remaining.segment_plane(
                distance_threshold=distance_thresh,
                ransac_n=3,
                num_iterations=1000)
        except Exception:
            break

        if len(inliers) < 100:
            break

        a, b, c, d = plane_model
        normal = np.array([a, b, c])
        norm = np.linalg.norm(normal)
        if norm < 1e-9:
            break
        normal /= norm

        vert_component = abs(normal[vert_axis])
        angle_from_vert = np.degrees(np.arccos(np.clip(vert_component, 0, 1)))

        if angle_from_vert < 45:
            if normal[vert_axis] < 0:
                normal = -normal
            print(f'  Plane found on attempt {attempt + 1}: '
                  f'normal=[{normal[0]:.3f}, {normal[1]:.3f}, {normal[2]:.3f}], '
                  f'{angle_from_vert:.1f} deg from {axis_names[vert_axis]}-axis, '
                  f'{len(inliers):,} inliers')
            return normal, inliers

        print(f'  Attempt {attempt + 1}: plane is a wall '
              f'({angle_from_vert:.1f} deg from {axis_names[vert_axis]}-axis), skipping')
        remaining = remaining.select_by_index(inliers, invert=True)

    return None


def level_points(points, normal):
    """Rotate points so *normal* maps to Z-up and shift floor to Z=0.

    Returns (leveled_points, rotation_deg, z_shift).
    """
    target = np.array([[0.0, 0.0, 1.0]])
    source = normal.reshape(1, 3)
    R, _ = Rotation.align_vectors(target, source)
    R_mat = R.as_matrix()

    result = points.copy()
    result[:, :3] = (R_mat @ points[:, :3].T).T

    angle_deg = R.magnitude() * 180 / np.pi
    print(f'  Rotation applied: {angle_deg:.2f} deg')

    z_vals = result[:, 2]
    z_min = np.percentile(z_vals, 1)
    z_max = np.percentile(z_vals, 99)
    z_range = z_max - z_min
    bottom_mask = z_vals < (z_min + 0.3 * z_range)
    if np.sum(bottom_mask) > 10:
        hist, edges = np.histogram(z_vals[bottom_mask], bins=100)
        peak_idx = np.argmax(hist)
        floor_z = (edges[peak_idx] + edges[peak_idx + 1]) / 2
    else:
        floor_z = z_min

    result[:, 2] -= floor_z
    print(f'  Z shift: {-floor_z:+.4f} m (floor -> Z=0)')

    return result, angle_deg, floor_z


# ---------------------------------------------------------------------------
# Top-level API
# ---------------------------------------------------------------------------

def level_ply(input_path, output_path=None, distance_thresh=0.03):
    """Level a PLY so floor is at Z=0.

    Returns output path. Skips if already leveled.
    """
    if output_path is None:
        base, ext = os.path.splitext(input_path)
        output_path = f'{base}_leveled{ext}'

    print(f'[Level] Reading {input_path}...')
    points, properties, comments = read_ply(input_path)
    print(f'  Loaded {len(points):,} points')

    if any(c.startswith('floor_leveled') for c in comments):
        print(f'  Already floor-leveled, skipping.')
        if output_path != input_path:
            import shutil
            shutil.copy2(input_path, output_path)
        return output_path

    print(f'[Level] Detecting floor plane (RANSAC)...')
    result = detect_floor_plane(points, distance_thresh=distance_thresh)

    if result is None:
        raise RuntimeError('No horizontal plane found -- cannot level')

    normal, inliers = result

    print(f'[Level] Leveling...')
    points, rotation_deg, z_shift = level_points(points, normal)
    comments.append(f'floor_leveled rotation_deg={rotation_deg:.2f} z_shift={z_shift:.4f}')

    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    write_ply(output_path, points, properties, comments=comments)
    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f'[Level] Saved {len(points):,} points ({size_mb:.1f} MB) -> {output_path}')

    return output_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Level a LiDAR PLY so the floor lies at Z=0.')
    parser.add_argument('input', help='Input PLY file path')
    parser.add_argument('-o', '--output', default=None,
                        help='Output path (default: <input>_leveled.ply)')
    parser.add_argument('--distance-thresh', type=float, default=0.03,
                        help='RANSAC inlier distance threshold (default: 0.03 m)')

    args = parser.parse_args()

    if not os.path.isfile(args.input):
        print(f'Error: {args.input} not found.')
        sys.exit(1)

    level_ply(args.input, output_path=args.output,
              distance_thresh=args.distance_thresh)


if __name__ == '__main__':
    main()
