#!/usr/bin/env python3
"""Standalone floor plan generation from LiDAR PLY point clouds.

Bundles PLY I/O, floor leveling (RANSAC), and full wall detection + floor plan
rendering into a single module.  Each step is independently testable via CLI
and callable as a library function from the Flask app.

Usage:
    python3 floorplan.py level  result_box.ply          # level only
    python3 floorplan.py plan   result_box_leveled.ply   # floor plan only
    python3 floorplan.py auto   result_box.ply           # level + floor plan

Extracted from lidar_v2/{preprocess,level,floorplan}.py for standalone use.
"""

import argparse
import os
import sys

import cv2
import matplotlib
matplotlib.use('Agg')  # headless — no display on Pi
import matplotlib.pyplot as plt
import numpy as np
from scipy.spatial.transform import Rotation


# ---------------------------------------------------------------------------
# PLY I/O  (from preprocess.py)
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
        # All same dtype: fast path
        if prop_dtypes and all(d == prop_dtypes[0] for d in prop_dtypes):
            data = np.fromfile(f, dtype=prop_dtypes[0],
                               count=vertex_count * n_props)
            points = data.reshape(vertex_count, n_props).astype(np.float32)
        else:
            # Mixed dtypes: read row by row
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
# Floor leveling  (extracted to level.py — re-exported here for back-compat)
# ---------------------------------------------------------------------------

from level import _numpy_to_o3d, detect_floor_plane, level_points, level_ply


# ---------------------------------------------------------------------------
# Floor plane axis detection
# ---------------------------------------------------------------------------

def get_world_frame_vertical(comments):
    """Parse vertical axis from world_frame comment."""
    mapping = {'x': 0, 'y': 1, 'z': 2}
    for c in comments:
        if 'world_frame' in c and 'vertical_axis=' in c:
            for part in c.split():
                if part.startswith('vertical_axis='):
                    axis = part.split('=')[1].strip().lower()
                    return mapping.get(axis)
    return None


def detect_floor_axes(points):
    """Auto-detect the two floor-plane axes.

    The axis with the smallest range is assumed vertical; the other two span
    the floor.
    """
    ranges = np.ptp(points[:, :3], axis=0)
    vertical = int(np.argmin(ranges))
    floor = [i for i in range(3) if i != vertical]
    labels = 'xyz'
    print(f'  Vertical axis: {labels[vertical]}  '
          f'(range {ranges[vertical]:.2f}m vs '
          f'{ranges[floor[0]]:.2f}m, {ranges[floor[1]]:.2f}m)')
    return tuple(floor)


# ---------------------------------------------------------------------------
# Height-band slicing
# ---------------------------------------------------------------------------

def _detect_ceiling_height(points, vertical_axis, floor_margin=0.3,
                           bin_size=0.05, min_fraction=0.02):
    """Detect ceiling height from a leveled point cloud.

    After leveling the floor is near Z=0.  This function builds a height
    histogram, skips the floor region, and finds the highest peak which
    corresponds to the ceiling plane.

    Returns ceiling_z (float) or None if no clear ceiling detected.
    """
    v = points[:, vertical_axis]
    lo, hi = float(v.min()), float(v.max())
    if hi - lo < 1.0:
        return None

    bins = np.arange(lo, hi + bin_size, bin_size)
    counts, edges = np.histogram(v, bins=bins)
    centers = (edges[:-1] + edges[1:]) / 2

    # Only look above the floor region
    above_floor = centers > floor_margin
    if not above_floor.any():
        return None

    counts_above = counts.copy()
    counts_above[~above_floor] = 0

    # Ceiling is the tallest peak in the upper half of the height range
    mid = lo + 0.5 * (hi - lo)
    upper = centers > mid
    counts_upper = counts_above.copy()
    counts_upper[~upper] = 0

    threshold = len(points) * min_fraction
    if counts_upper.max() >= threshold:
        peak_idx = int(np.argmax(counts_upper))
        ceiling_z = float(centers[peak_idx])
        return ceiling_z

    # Fallback: highest peak above floor
    if counts_above.max() >= threshold:
        peak_idx = int(np.argmax(counts_above))
        return float(centers[peak_idx])

    return None


def slice_height_band(points, vertical_axis, mode='auto'):
    """Extract points within a height band along the vertical axis.

    mode='auto': detects floor/ceiling planes and keeps the wall region
                 between them (floor+0.3m to ceiling-0.3m).
    mode='off':  no filtering.
    mode='lo-hi': explicit range.
    """
    if mode == 'off':
        return points

    labels = 'xyz'
    v = points[:, vertical_axis]

    if mode == 'auto':
        lo, hi = float(v.min()), float(v.max())
        trim = 0.10
        trim_lo = lo + trim * (hi - lo)
        trim_hi = hi - trim * (hi - lo)

        # Detect floor/ceiling for a smarter band
        # Floor: histogram peak near the bottom of the range
        bins = np.arange(lo, lo + 2.0, 0.05)
        if len(bins) > 2:
            counts, edges = np.histogram(v, bins=bins)
            floor_z = float((edges[np.argmax(counts)]
                             + edges[np.argmax(counts) + 1]) / 2)
        else:
            floor_z = lo
        ceiling_z = _detect_ceiling_height(points, vertical_axis)

        if ceiling_z is not None and ceiling_z > floor_z + 1.0:
            margin = 0.15
            band_lo = floor_z + margin
            band_hi = ceiling_z - margin
            print(f'  Floor: {floor_z:.2f}m, Ceiling: {ceiling_z:.2f}m '
                  f'(room height: {ceiling_z - floor_z:.2f}m)')
        else:
            band_lo = trim_lo
            band_hi = trim_hi
            print(f'  No clear ceiling detected, using 10% trim')
    else:
        parts = mode.split('-')
        if len(parts) != 2:
            raise ValueError(f'height-band must be "auto", "off", or "lo-hi"')
        band_lo, band_hi = float(parts[0]), float(parts[1])

    mask = (v >= band_lo) & (v <= band_hi)
    filtered = points[mask]

    print(f'  Height band: {labels[vertical_axis]} = '
          f'[{band_lo:.2f}, {band_hi:.2f}]m')
    print(f'  Points: {len(points):,} -> {len(filtered):,} '
          f'({len(filtered)/len(points)*100:.1f}%)')

    if len(filtered) == 0:
        raise ValueError('0 points in height band')

    return filtered


def multiheight_vote_filter(points, vertical_axis, floor_axes,
                            n_slices=5, slice_half=0.10,
                            vote_resolution=0.05, min_votes=3):
    """Filter points to keep only wall-consistent surfaces."""
    v = points[:, vertical_axis]
    lo, hi = float(v.min()), float(v.max())
    span_lo = lo + 0.10 * (hi - lo)
    span_hi = hi - 0.10 * (hi - lo)
    centers = np.linspace(span_lo + slice_half, span_hi - slice_half, n_slices)

    x = points[:, floor_axes[0]]
    y = points[:, floor_axes[1]]
    x_min, x_max = float(x.min()) - 0.05, float(x.max()) + 0.05
    y_min, y_max = float(y.min()) - 0.05, float(y.max()) + 0.05
    nx = int(np.ceil((x_max - x_min) / vote_resolution))
    ny = int(np.ceil((y_max - y_min) / vote_resolution))

    vote_grid = np.zeros((ny, nx), dtype=np.int32)
    for sc in centers:
        smask = (v >= sc - slice_half) & (v <= sc + slice_half)
        grid, _, _ = np.histogram2d(
            x[smask], y[smask],
            bins=[nx, ny],
            range=[[x_min, x_max], [y_min, y_max]])
        vote_grid += (grid.T >= 1).astype(np.int32)

    band_mask = (v >= span_lo) & (v <= span_hi)
    band_pts = points[band_mask]
    bx = band_pts[:, floor_axes[0]]
    by = band_pts[:, floor_axes[1]]
    px = np.clip(((bx - x_min) / vote_resolution).astype(int), 0, nx - 1)
    py = np.clip(((by - y_min) / vote_resolution).astype(int), 0, ny - 1)
    keep = vote_grid[py, px] >= min_votes
    filtered = band_pts[keep]

    n_before = len(band_pts)
    n_after = len(filtered)
    pct_removed = (1 - n_after / n_before) * 100 if n_before else 0
    print(f'  Multi-height voting: {n_slices} slices, '
          f'kept {n_after:,}/{n_before:,} pts '
          f'({pct_removed:.0f}% furniture removed)')

    return filtered


# ---------------------------------------------------------------------------
# RANSAC wall detection
# ---------------------------------------------------------------------------

def ransac_detect_lines(xy, distance_thresh=0.03, min_inliers=50,
                        max_lines=20, n_iter=1000, subsample=100_000):
    """Detect wall lines directly from 2D points via iterative RANSAC."""
    rng = np.random.default_rng(42)
    remaining = xy.copy()
    lines = []
    consecutive_failures = 0

    for _ in range(max_lines):
        if len(remaining) < min_inliers:
            break

        if len(remaining) > subsample:
            idx = rng.choice(len(remaining), subsample, replace=False)
            sample_pts = remaining[idx]
        else:
            sample_pts = remaining

        best_inlier_count = 0
        best_normal = None
        best_offset = None

        for _ in range(n_iter):
            ii = rng.choice(len(sample_pts), 2, replace=False)
            p0, p1 = sample_pts[ii[0]], sample_pts[ii[1]]
            d = p1 - p0
            length = np.hypot(d[0], d[1])
            if length < 1e-9:
                continue
            normal = np.array([-d[1] / length, d[0] / length])
            offset = normal @ p0
            dists = np.abs(sample_pts @ normal - offset)
            count = int(np.sum(dists < distance_thresh))
            if count > best_inlier_count:
                best_inlier_count = count
                best_normal = normal
                best_offset = offset

        if best_normal is None or best_inlier_count < min_inliers:
            break

        dists_full = np.abs(remaining @ best_normal - best_offset)
        inlier_mask = dists_full < distance_thresh
        inlier_pts = remaining[inlier_mask]

        if len(inlier_pts) < min_inliers:
            consecutive_failures += 1
            if consecutive_failures >= 3:
                break
            continue
        consecutive_failures = 0

        if best_normal[0] < 0 or (best_normal[0] == 0 and best_normal[1] < 0):
            best_normal = -best_normal
            best_offset = -best_offset

        direction = np.array([best_normal[1], -best_normal[0]])
        projections = inlier_pts @ direction
        t_min, t_max = float(projections.min()), float(projections.max())
        base = best_normal * best_offset
        seg_p1 = base + direction * t_min
        seg_p2 = base + direction * t_max
        angle = float(np.arctan2(direction[1], direction[0]))

        lines.append({
            'normal': best_normal,
            'offset': best_offset,
            'p1': seg_p1,
            'p2': seg_p2,
            'angle': angle,
            'inlier_count': len(inlier_pts),
            'inlier_pts': inlier_pts,
        })

        remaining = remaining[~inlier_mask]

    return lines


def score_wall_lines(lines):
    """Compute confidence metrics for each detected wall line.

    Adds to each line dict:
      fit_error   - mean distance of inliers from line (metres, lower=better)
      density     - inlier points per metre of wall length (higher=better)
      coverage    - fraction of wall length with point support (0-1, higher=better)
      confidence  - combined score 0-100 (higher=better)
    """
    if not lines:
        return lines

    max_inliers = max(ln['inlier_count'] for ln in lines)

    for ln in lines:
        pts = ln['inlier_pts']
        n_pts = ln['inlier_count']
        normal, offset = ln['normal'], ln['offset']
        direction = np.array([normal[1], -normal[0]])
        seg_len = float(np.linalg.norm(ln['p2'] - ln['p1']))

        # 1. Fit error: how tightly points cluster on the line
        dists = np.abs(pts @ normal - offset)
        fit_error = float(dists.mean()) if len(dists) > 0 else 1.0

        # 2. Density: points per metre
        density = n_pts / max(seg_len, 0.1)

        # 3. Coverage: what fraction of wall length has points
        #    Bin projections along wall, count non-empty bins
        projs = pts @ direction
        bin_size = 0.10  # 10cm bins
        if seg_len > bin_size:
            p_min, p_max = float(projs.min()), float(projs.max())
            n_bins = max(1, int(np.ceil((p_max - p_min) / bin_size)))
            counts, _ = np.histogram(projs, bins=n_bins)
            coverage = float(np.count_nonzero(counts)) / n_bins
        else:
            coverage = 1.0

        # 4. Combined confidence (0-100)
        inlier_score = n_pts / max(max_inliers, 1)  # 0-1
        fit_score = max(0, 1.0 - fit_error / 0.03)  # 1.0 at 0mm, 0 at 30mm
        confidence = (inlier_score * 40
                      + fit_score * 20
                      + coverage * 30
                      + min(density / 200, 1.0) * 10)

        ln['fit_error'] = round(fit_error * 1000, 1)  # mm
        ln['density'] = round(density, 1)
        ln['coverage'] = round(coverage * 100, 1)
        ln['confidence'] = round(confidence, 1)

    return lines


def merge_lines(lines, angle_thresh_deg=5.0, dist_thresh=0.10):
    """Merge nearly-parallel nearby lines (same wall fragmented by gaps)."""
    if len(lines) <= 1:
        return lines

    for ln in lines:
        a = ln['angle'] % np.pi
        ln['_norm_angle'] = a

    lines_sorted = sorted(lines, key=lambda l: l['_norm_angle'])

    angle_thresh = np.radians(angle_thresh_deg)
    groups = []
    current_group = [lines_sorted[0]]
    for ln in lines_sorted[1:]:
        diff = abs(ln['_norm_angle'] - current_group[-1]['_norm_angle'])
        diff = min(diff, np.pi - diff)
        if diff < angle_thresh:
            current_group.append(ln)
        else:
            groups.append(current_group)
            current_group = [ln]
    groups.append(current_group)

    merged = []
    for group in groups:
        group.sort(key=lambda l: l['offset'])
        sub = [group[0]]
        for ln in group[1:]:
            if abs(ln['offset'] - sub[-1]['offset']) < dist_thresh:
                a, b = sub[-1], ln
                wa, wb = a['inlier_count'], b['inlier_count']
                wt = wa + wb
                new_normal = (a['normal'] * wa + b['normal'] * wb) / wt
                new_normal /= np.linalg.norm(new_normal)
                new_offset = (a['offset'] * wa + b['offset'] * wb) / wt
                if new_normal[0] < 0 or (new_normal[0] == 0 and new_normal[1] < 0):
                    new_normal = -new_normal
                    new_offset = -new_offset
                direction = np.array([new_normal[1], -new_normal[0]])
                all_pts = np.vstack([a['inlier_pts'], b['inlier_pts']])
                projections = all_pts @ direction
                t_min, t_max = float(projections.min()), float(projections.max())
                base = new_normal * new_offset
                new_p1 = base + direction * t_min
                new_p2 = base + direction * t_max
                new_angle = float(np.arctan2(direction[1], direction[0]))
                sub[-1] = {
                    'normal': new_normal,
                    'offset': new_offset,
                    'p1': new_p1,
                    'p2': new_p2,
                    'angle': new_angle,
                    'inlier_count': wt,
                    'inlier_pts': all_pts,
                }
            else:
                sub.append(ln)
        merged.extend(sub)

    for ln in merged:
        ln.pop('_norm_angle', None)

    return merged


def filter_lines_by_support(lines, min_support_ratio=0.10):
    """Keep lines with inlier count >= min_support_ratio * max_inliers."""
    if not lines:
        return lines
    max_inliers = max(ln['inlier_count'] for ln in lines)
    threshold = max_inliers * min_support_ratio
    kept = [ln for ln in lines if ln['inlier_count'] >= threshold]
    dropped = len(lines) - len(kept)
    if dropped:
        print(f'  Support filter: dropped {dropped} noise line(s) '
              f'(< {min_support_ratio*100:.0f}% of max={max_inliers})')
    return kept


def orthogonalize_lines(lines, ortho_tol_deg=15.0):
    """Snap line directions to 90-degree grid relative to dominant line."""
    if ortho_tol_deg <= 0 or len(lines) == 0:
        return lines

    dominant = max(lines, key=lambda l: l['inlier_count'])
    dom_angle = dominant['angle'] % np.pi

    half_pi = np.pi / 2
    tol = np.radians(ortho_tol_deg)

    kept = []
    for ln in lines:
        a = ln['angle'] % np.pi
        rel = a - dom_angle
        rel = (rel + np.pi / 4) % np.pi - np.pi / 4
        snapped = round(rel / half_pi) * half_pi
        if abs(rel - snapped) <= tol:
            new_angle = dom_angle + snapped
            direction = np.array([np.cos(new_angle), np.sin(new_angle)])
            new_normal = np.array([-direction[1], direction[0]])
            if new_normal[0] < 0 or (new_normal[0] == 0 and new_normal[1] < 0):
                new_normal = -new_normal
            centroid = ln['inlier_pts'].mean(axis=0)
            new_offset = new_normal @ centroid
            projections = ln['inlier_pts'] @ direction
            t_min, t_max = float(projections.min()), float(projections.max())
            base = new_normal * new_offset
            ln['normal'] = new_normal
            ln['offset'] = new_offset
            ln['angle'] = float(new_angle)
            ln['p1'] = base + direction * t_min
            ln['p2'] = base + direction * t_max
            kept.append(ln)

    dropped = len(lines) - len(kept)
    if dropped:
        print(f'  Dropped {dropped} non-orthogonal noise lines')

    return kept


def _uncovered_span(cand_span, outer_spans):
    """Length of cand_span not covered by any interval in outer_spans."""
    c_lo, c_hi = cand_span
    if c_hi <= c_lo:
        return 0.0
    covered = []
    for s_lo, s_hi in outer_spans:
        lo = max(c_lo, s_lo)
        hi = min(c_hi, s_hi)
        if lo < hi:
            covered.append((lo, hi))
    if not covered:
        return c_hi - c_lo
    covered.sort()
    merged = [covered[0]]
    for lo, hi in covered[1:]:
        if lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return (c_hi - c_lo) - sum(hi - lo for lo, hi in merged)


def _wall_proj_span(ln, direction, bin_size=0.05):
    """Project wall inlier points onto direction, return (lo, hi) span."""
    projs = ln['inlier_pts'] @ direction
    # Bin to smooth out sparse coverage
    lo, hi = float(projs.min()), float(projs.max())
    return (round(lo / bin_size) * bin_size, round(hi / bin_size) * bin_size)


def select_boundary_lines(lines, max_per_direction=4):
    """Within each direction group, keep walls that cover the room boundary.

    Starts with the outermost pair (works for rectangular rooms), then
    iteratively adds inner walls that fill coverage gaps (L / T / U shapes).
    """
    if len(lines) <= 2:
        return lines

    angle_groups = {}
    for ln in lines:
        a = round((ln['angle'] % np.pi) * 180 / np.pi, 0)
        if a not in angle_groups:
            angle_groups[a] = []
        angle_groups[a].append(ln)

    result = []
    for a, group in angle_groups.items():
        if len(group) <= 2:
            result.extend(group)
            continue

        max_inliers = max(ln['inlier_count'] for ln in group)
        threshold = max_inliers * 0.15
        significant = [ln for ln in group if ln['inlier_count'] >= threshold]

        if len(significant) <= 2:
            result.extend(significant)
            continue

        # Pick outermost pair (same logic as before)
        significant.sort(key=lambda l: l['offset'])
        selected = [significant[0], significant[-1]]
        remaining = significant[1:-1]

        # Wall direction vector (perpendicular to normal)
        direction = np.array([selected[0]['normal'][1],
                              -selected[0]['normal'][0]])

        # Iteratively add walls that fill coverage gaps
        min_gap_fill = 0.5   # metres
        min_offset_sep = 0.3  # metres -- reject furniture
        changed = True
        while changed and len(selected) < max_per_direction and remaining:
            changed = False
            sel_spans = [_wall_proj_span(ln, direction) for ln in selected]
            best_idx, best_uncov = -1, 0.0
            for ci, cand in enumerate(remaining):
                # Reject if too close in offset to any selected wall
                if any(abs(cand['offset'] - s['offset']) < min_offset_sep
                       for s in selected):
                    continue
                cand_span = _wall_proj_span(cand, direction)
                # Check uncovered span against each selected wall
                # individually -- for L-shapes, the inner wall fills
                # a gap in ONE outer wall, not in the union of all.
                max_uncov = max(
                    _uncovered_span(cand_span, [sp])
                    for sp in sel_spans
                )
                if max_uncov >= min_gap_fill and max_uncov > best_uncov:
                    best_idx, best_uncov = ci, max_uncov
            if best_idx >= 0:
                promoted = remaining.pop(best_idx)
                selected.append(promoted)
                seg_len_ = np.linalg.norm(promoted['p2'] - promoted['p1'])
                print(f'    Promoted inner wall: offset={promoted["offset"]:.3f}, '
                      f'len={seg_len_:.2f}m, uncovered={best_uncov:.2f}m')
                changed = True

        result.extend(selected)

    return result


# ---------------------------------------------------------------------------
# Contour + room extraction
# ---------------------------------------------------------------------------

def build_occupancy_grid(xy, resolution, padding=0.05):
    """Bin 2D points into a density grid."""
    x_min, x_max = xy[:, 0].min() - padding, xy[:, 0].max() + padding
    y_min, y_max = xy[:, 1].min() - padding, xy[:, 1].max() + padding

    nx = int(np.ceil((x_max - x_min) / resolution))
    ny = int(np.ceil((y_max - y_min) / resolution))

    grid, _, _ = np.histogram2d(
        xy[:, 0], xy[:, 1],
        bins=[nx, ny],
        range=[[x_min, x_max], [y_min, y_max]],
    )

    grid = grid.T
    grid = np.clip(grid, 0, 255).astype(np.uint8)
    return grid, (x_min, x_max, y_min, y_max)


def threshold_grid(grid, resolution=0.05):
    """Percentile threshold + morphological close + dilate."""
    non_empty_vals = grid[grid > 0]
    if len(non_empty_vals) > 0:
        thresh = max(1, int(np.percentile(non_empty_vals, 25)))
    else:
        thresh = 1
    binary = (grid >= thresh).astype(np.uint8) * 255
    print(f'  Threshold: {thresh} (p25 of non-empty cells)')

    close_px = max(3, int(round(0.10 / resolution)))
    dilate_px = max(3, int(round(0.05 / resolution)))
    close_px |= 1
    dilate_px |= 1

    close_kernel = cv2.getStructuringElement(cv2.MORPH_RECT,
                                             (close_px, close_px))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, close_kernel)

    dilate_kernel = cv2.getStructuringElement(cv2.MORPH_RECT,
                                              (dilate_px, dilate_px))
    binary = cv2.dilate(binary, dilate_kernel, iterations=1)

    dilation_offset_m = (dilate_px // 2) * resolution
    print(f'  Morph kernels: close={close_px}x{close_px}, '
          f'dilate={dilate_px}x{dilate_px} px')

    return binary, dilation_offset_m


def detect_doorways(lines, min_gap=0.5, max_gap=1.5):
    """Find doorway-sized gaps in wall coverage."""
    doorways = []
    for ln in lines:
        direction = np.array([ln['normal'][1], -ln['normal'][0]])
        projections = ln['inlier_pts'] @ direction
        projections.sort()

        diffs = np.diff(projections)
        gap_indices = np.where(diffs > min_gap)[0]

        base = ln['normal'] * ln['offset']
        for gi in gap_indices:
            gap_width = float(diffs[gi])
            if gap_width > max_gap:
                continue
            t_start = float(projections[gi])
            t_end = float(projections[gi + 1])
            doorways.append({
                'line': ln,
                'gap_start': base + direction * t_start,
                'gap_end': base + direction * t_end,
                'direction': direction,
                'width': gap_width,
            })
    return doorways


def _world_to_pixel(x, y, extent, resolution):
    """Convert world coords (metres) to pixel coords."""
    x_min, _, y_min, _ = extent
    px = int(round((x - x_min) / resolution))
    py = int(round((y - y_min) / resolution))
    return px, py


def build_room_mask(xy, resolution, doorways, all_lines=None, seed_xy=None):
    """Build occupancy grid, close doorways, extract room-only component."""
    grid, extent = build_occupancy_grid(xy, resolution)
    print(f'  Grid size: {grid.shape[1]}x{grid.shape[0]} px')

    binary, dilation_offset = threshold_grid(grid, resolution=resolution)

    if all_lines:
        thickness_px = max(3, int(round(0.06 / resolution))) | 1

        # Extend each wall beyond its endpoints to help close corners.
        ext_m = 0.15  # metres beyond each endpoint
        for ln in all_lines:
            d = ln['p2'] - ln['p1']
            seg_len = np.linalg.norm(d)
            if seg_len > 0:
                ext = d / seg_len * ext_m
                p1_ext = ln['p1'] - ext
                p2_ext = ln['p2'] + ext
            else:
                p1_ext, p2_ext = ln['p1'], ln['p2']
            px1 = _world_to_pixel(p1_ext[0], p1_ext[1], extent, resolution)
            px2 = _world_to_pixel(p2_ext[0], p2_ext[1], extent, resolution)
            cv2.line(binary, tuple(px1), tuple(px2), 255,
                     thickness=thickness_px)

    x_min, x_max, y_min, y_max = extent
    for dw in doorways:
        normal = dw['line']['normal']
        wall_half_px = max(3, int(round(0.05 / resolution)))
        p1 = dw['gap_start']
        p2 = dw['gap_end']
        perp = normal * wall_half_px * resolution
        corners = np.array([p1 - perp, p1 + perp, p2 + perp, p2 - perp])
        corners_px = np.array([
            _world_to_pixel(c[0], c[1], extent, resolution) for c in corners
        ], dtype=np.int32)
        h, w = binary.shape
        corners_px[:, 0] = np.clip(corners_px[:, 0], 0, w - 1)
        corners_px[:, 1] = np.clip(corners_px[:, 1], 0, h - 1)
        cv2.fillPoly(binary, [corners_px], 255)

    closed_count = len(doorways)
    if closed_count:
        print(f'  Closed {closed_count} doorway gap(s)')

    if all_lines:
        corner_px = max(7, int(round(0.30 / resolution))) | 1
        corner_kernel = cv2.getStructuringElement(cv2.MORPH_RECT,
                                                   (corner_px, corner_px))
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, corner_kernel)

    h, w = binary.shape
    padded_inv = np.zeros((h + 2, w + 2), np.uint8)
    padded_inv[1:-1, 1:-1] = cv2.bitwise_not(binary)
    padded_inv[0, :] = 255
    padded_inv[-1, :] = 255
    padded_inv[:, 0] = 255
    padded_inv[:, -1] = 255

    ff_mask = np.zeros((h + 4, w + 4), np.uint8)
    cv2.floodFill(padded_inv, ff_mask, (0, 0), 0)

    interior = padded_inv[1:-1, 1:-1]

    n_labels, labels = cv2.connectedComponents(interior)

    # Use point cloud centroid as seed (scanner was inside the room).
    # Falls back to origin (0,0) if no seed provided.
    if seed_xy is not None:
        sx, sy = float(seed_xy[0]), float(seed_xy[1])
    else:
        sx, sy = 0.0, 0.0
    ox, oy = _world_to_pixel(sx, sy, extent, resolution)
    ox = np.clip(ox, 0, w - 1)
    oy = np.clip(oy, 0, h - 1)

    room_label = labels[oy, ox]
    if room_label == 0:
        search_r = max(10, int(1.0 / resolution))
        best_dist = float('inf')
        for dy in range(-search_r, search_r + 1):
            for dx in range(-search_r, search_r + 1):
                ny_, nx_ = oy + dy, ox + dx
                if 0 <= ny_ < h and 0 <= nx_ < w and labels[ny_, nx_] > 0:
                    d = dx * dx + dy * dy
                    if d < best_dist:
                        best_dist = d
                        room_label = labels[ny_, nx_]

    # Check if enclosed area is reasonable (>25% of bounding box)
    enclosed = room_label > 0
    if enclosed:
        room_px = np.count_nonzero(labels == room_label)
        bbox_px = h * w
        if room_px < bbox_px * 0.05:
            print(f'  Room too small ({room_px} px / {bbox_px} px), retrying')
            enclosed = False
            room_label = 0

    # If first attempt failed, retry with aggressive morphological close
    # to bridge larger corner gaps (L/T/U-shaped rooms).
    if not enclosed and all_lines:
        retry_px = max(11, int(round(2.0 / resolution))) | 1
        retry_kernel = cv2.getStructuringElement(cv2.MORPH_RECT,
                                                  (retry_px, retry_px))
        retry_bin = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, retry_kernel)
        # Re-do flood fill on the aggressively closed mask
        h2, w2 = retry_bin.shape
        pad2 = np.zeros((h2 + 2, w2 + 2), np.uint8)
        pad2[1:-1, 1:-1] = cv2.bitwise_not(retry_bin)
        pad2[0, :] = 255; pad2[-1, :] = 255
        pad2[:, 0] = 255; pad2[:, -1] = 255
        ff2 = np.zeros((h2 + 4, w2 + 4), np.uint8)
        cv2.floodFill(pad2, ff2, (0, 0), 0)
        interior2 = pad2[1:-1, 1:-1]
        n2, labels2 = cv2.connectedComponents(interior2)
        room_label2 = labels2[oy, ox]
        if room_label2 == 0:
            sr = max(10, int(1.0 / resolution))
            bd = float('inf')
            for dy in range(-sr, sr + 1):
                for dx in range(-sr, sr + 1):
                    ny_, nx_ = oy + dy, ox + dx
                    if 0 <= ny_ < h2 and 0 <= nx_ < w2 and labels2[ny_, nx_] > 0:
                        d = dx * dx + dy * dy
                        if d < bd:
                            bd = d
                            room_label2 = labels2[ny_, nx_]
        if room_label2 > 0:
            labels = labels2
            room_label = room_label2
            enclosed = True
            # Track extra dilation from the aggressive close
            dilation_offset += (retry_px // 2) * resolution
            print(f'  Retry with {retry_px}px close succeeded')

    if enclosed:
        room_interior = (labels == room_label).astype(np.uint8) * 255
        dk = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        interior_dilate_iters = 2
        room_mask = cv2.dilate(room_interior, dk,
                                iterations=interior_dilate_iters)
        dilation_offset += interior_dilate_iters * resolution
        room_area_px = np.count_nonzero(room_interior)
        print(f'  Room interior: {room_area_px:,} px '
              f'({n_labels - 1} enclosed regions)')
    else:
        print(f'  No enclosed room found -- will use geometric fallback')
        room_mask = binary

    return room_mask, extent, dilation_offset, enclosed


def _polygon_from_wall_lines(all_lines, xy=None):
    """Build room polygon using cell complex decomposition.

    Extends each wall line across the bounding box, creating a planar
    subdivision (cell complex).  Uses shapely.polygonize to extract all
    closed faces, then picks the face containing the most scanned points
    (the room).  Handles L-shapes, T-shapes, hallways, and any geometry.

    Returns polygon vertices (Nx2 ndarray) or None on failure.
    """
    from shapely.geometry import LineString, MultiLineString, Point
    from shapely.ops import polygonize, unary_union

    n = len(all_lines)
    if n < 3:
        return None

    # Compute bounding box of all inlier points (with padding)
    all_pts = np.vstack([ln['inlier_pts'] for ln in all_lines])
    pad = 2.0
    x_min, y_min = all_pts.min(axis=0) - pad
    x_max, y_max = all_pts.max(axis=0) + pad

    # Extend each wall line across the bounding box
    lines = []
    for ln in all_lines:
        d = np.array([ln['normal'][1], -ln['normal'][0]])  # wall direction
        base = ln['normal'] * ln['offset']
        # Find how far the line extends within the bbox
        # Solve for t: base + t*d hits bbox edges
        t_vals = []
        for axis in [0, 1]:
            if abs(d[axis]) > 1e-9:
                t1 = ([x_min, y_min][axis] - base[axis]) / d[axis]
                t2 = ([x_max, y_max][axis] - base[axis]) / d[axis]
                t_vals.extend([t1, t2])
        if len(t_vals) < 2:
            continue
        t_lo, t_hi = min(t_vals), max(t_vals)
        p1 = base + d * t_lo
        p2 = base + d * t_hi
        lines.append(LineString([(p1[0], p1[1]), (p2[0], p2[1])]))

    if len(lines) < 3:
        return None

    # Add bounding box edges to close the arrangement
    bbox_lines = [
        LineString([(x_min, y_min), (x_max, y_min)]),
        LineString([(x_max, y_min), (x_max, y_max)]),
        LineString([(x_max, y_max), (x_min, y_max)]),
        LineString([(x_min, y_max), (x_min, y_min)]),
    ]

    # Polygonize: split all lines at intersections, extract closed faces
    all_geom = unary_union(lines + bbox_lines)
    faces = list(polygonize(all_geom))

    if not faces:
        return None

    # Score each face by point density (pts/m2).  Room interior has
    # high density; exterior cells have near-zero.
    pts_to_check = xy if xy is not None else all_pts
    # Subsample for speed
    step = max(1, len(pts_to_check) // 2000)
    pts_sub = pts_to_check[::step]
    from shapely import prepare

    face_density = []
    for face in faces:
        if face.area < 0.5:
            continue
        prepare(face)
        count = sum(1 for p in pts_sub
                    if face.contains(Point(p[0], p[1])))
        density = count / face.area
        face_density.append((face, density, count))

    if not face_density:
        return None

    # Find the densest face (the room core)
    face_density.sort(key=lambda x: -x[1])
    max_density = face_density[0][1]
    if max_density < 1.0:
        return None

    # Merge faces with density > 20% of the densest face
    density_thresh = max_density * 0.25
    room_faces = [f for f, d, c in face_density if d >= density_thresh]

    if not room_faces:
        return None

    room = unary_union(room_faces)
    if room.geom_type == 'MultiPolygon':
        room = max(room.geoms, key=lambda g: g.area)

    # Simplify the boundary
    coords = np.array(room.exterior.coords[:-1])  # drop closing duplicate
    if len(coords) < 3:
        return None

    area = float(room.area)
    if area < 5.0:
        return None

    # Ensure CCW winding
    sa = 0.0
    for i in range(len(coords)):
        j = (i + 1) % len(coords)
        sa += coords[i][0] * coords[j][1] - coords[j][0] * coords[i][1]
    if sa < 0:
        coords = coords[::-1]

    print(f'  Cell complex polygon: {len(coords)} vertices, '
          f'area={area:.1f}m2 ({len(room_faces)} cells merged)')
    return coords


# ---------------------------------------------------------------------------
# Polygon refinement
# ---------------------------------------------------------------------------

def inset_polygon(verts, offset):
    """Shrink a polygon inward by *offset* metres (compensate dilation)."""
    n = len(verts)
    if n < 3 or offset <= 0:
        return verts

    verts = np.asarray(verts, dtype=np.float64)
    edges = np.roll(verts, -1, axis=0) - verts
    signed_area = 0.5 * np.sum(edges[:, 0] * (np.roll(verts, -1, axis=0)[:, 1]
                                                + verts[:, 1])
                                - edges[:, 1] * (np.roll(verts, -1, axis=0)[:, 0]
                                                  + verts[:, 0]))
    sign = 1.0 if signed_area > 0 else -1.0

    shifted_p1 = np.empty_like(verts)
    shifted_p2 = np.empty_like(verts)
    for i in range(n):
        j = (i + 1) % n
        dx, dy = float(edges[i, 0]), float(edges[i, 1])
        length = np.hypot(dx, dy)
        if length < 1e-12:
            shifted_p1[i] = verts[i]
            shifted_p2[i] = verts[j]
            continue
        nx = sign * dy / length
        ny = sign * (-dx) / length
        shift = np.array([nx * offset, ny * offset])
        shifted_p1[i] = verts[i] + shift
        shifted_p2[i] = verts[j] + shift

    new_verts = np.empty_like(verts)
    for i in range(n):
        prev = (i - 1) % n
        d1 = shifted_p2[prev] - shifted_p1[prev]
        d2 = shifted_p2[i] - shifted_p1[i]
        cross = d1[0] * d2[1] - d1[1] * d2[0]
        if abs(cross) < 1e-12:
            new_verts[i] = (shifted_p1[prev] + shifted_p1[i]) / 2
        else:
            dp = shifted_p1[i] - shifted_p1[prev]
            t = (dp[0] * d2[1] - dp[1] * d2[0]) / cross
            new_verts[i] = shifted_p1[prev] + t * d1

    new_edges = np.roll(new_verts, -1, axis=0) - new_verts
    new_signed_area = 0.5 * np.sum(
        new_edges[:, 0] * (np.roll(new_verts, -1, axis=0)[:, 1]
                           + new_verts[:, 1])
        - new_edges[:, 1] * (np.roll(new_verts, -1, axis=0)[:, 0]
                              + new_verts[:, 0]))

    if abs(new_signed_area) >= abs(signed_area):
        print(f'  Dilation compensation skipped (area grew)')
        return verts

    for i in range(n):
        dot = (edges[i, 0] * new_edges[i, 0] + edges[i, 1] * new_edges[i, 1])
        if dot < 0:
            print(f'  Dilation compensation skipped (self-intersection)')
            return verts

    return new_verts


def orthogonalize_polygon(verts_m, angle_tol_deg=15.0, min_wall_m=0.3):
    """Snap edges to orthogonal directions and remove short edges."""
    if len(verts_m) < 3:
        return verts_m

    verts = verts_m.copy().astype(np.float64)
    n = len(verts)

    if angle_tol_deg > 0:
        edges = [verts[(i + 1) % n] - verts[i] for i in range(n)]
        orig_lengths = np.array([np.linalg.norm(e) for e in edges])

        longest_idx = int(np.argmax(orig_lengths))
        principal = np.arctan2(edges[longest_idx][1],
                               edges[longest_idx][0])

        half_pi = np.pi / 2
        directions = np.empty((n, 2))
        for i, e in enumerate(edges):
            edge_angle = np.arctan2(e[1], e[0])
            rel = edge_angle - principal
            rel = (rel + np.pi) % (2 * np.pi) - np.pi
            snapped_rel = round(rel / half_pi) * half_pi
            if abs(np.degrees(rel - snapped_rel)) <= angle_tol_deg:
                a = principal + snapped_rel
            else:
                a = edge_angle
            directions[i] = [np.cos(a), np.sin(a)]

        D = directions
        DtD = D.T @ D
        DtL = D.T @ orig_lengths
        try:
            correction = D @ np.linalg.solve(DtD, DtL)
        except np.linalg.LinAlgError:
            correction = np.zeros(n)
        new_lengths = orig_lengths - correction
        new_lengths = np.maximum(new_lengths, 0.01)

        new_verts = np.empty((n, 2))
        new_verts[0] = verts[0]
        for i in range(n - 1):
            new_verts[i + 1] = new_verts[i] + directions[i] * new_lengths[i]

        verts = new_verts

    if min_wall_m > 0:
        changed = True
        while changed and len(verts) > 3:
            changed = False
            lengths = np.array([
                np.linalg.norm(verts[(i + 1) % len(verts)] - verts[i])
                for i in range(len(verts))
            ])
            shortest_idx = int(np.argmin(lengths))
            if lengths[shortest_idx] < min_wall_m:
                j = (shortest_idx + 1) % len(verts)
                prev = (shortest_idx - 1) % len(verts)
                next_ = (j + 1) % len(verts)
                d_prev = verts[shortest_idx] - verts[prev]
                d_next = verts[next_] - verts[j]
                cross = d_prev[0] * d_next[1] - d_prev[1] * d_next[0]
                if abs(cross) > 1e-9:
                    # Extend neighboring edges to meet at intersection
                    dp = verts[j] - verts[prev]
                    t = (dp[0] * d_next[1] - dp[1] * d_next[0]) / cross
                    corner = verts[prev] + t * d_prev
                    verts[shortest_idx] = corner
                else:
                    # Parallel neighbors -- midpoint fallback
                    verts[shortest_idx] = (verts[shortest_idx] + verts[j]) / 2
                verts = np.delete(verts, j, axis=0)
                changed = True

    return verts


def merge_colinear_edges(verts_m, angle_tol_deg=5.0):
    """Remove vertices where adjacent edges are nearly colinear."""
    changed = True
    while changed and len(verts_m) >= 4:
        changed = False
        n = len(verts_m)
        for i in range(n):
            prev = (i - 1) % n
            next_ = (i + 1) % n
            d1 = verts_m[i] - verts_m[prev]
            d2 = verts_m[next_] - verts_m[i]
            l1, l2 = np.linalg.norm(d1), np.linalg.norm(d2)
            if l1 < 1e-9 or l2 < 1e-9:
                verts_m = np.delete(verts_m, i, axis=0)
                changed = True
                break
            cos_a = np.clip(np.dot(d1 / l1, d2 / l2), -1, 1)
            angle = np.degrees(np.arccos(cos_a))
            if angle < angle_tol_deg:
                verts_m = np.delete(verts_m, i, axis=0)
                changed = True
                break
    return verts_m


def _refine_wall_offset(xy, normal, all_parallel_lines, boundary_offset,
                         distance_thresh=0.03, max_depth=1.0,
                         min_furniture_depth=0.20, min_peak_density=50):
    """Search for an actual wall hidden behind furniture."""
    from scipy.ndimage import gaussian_filter1d as _smooth
    from scipy.signal import find_peaks as _peaks

    projections = xy @ normal

    keep = np.ones(len(xy), dtype=bool)
    for ln in all_parallel_lines:
        keep &= (np.abs(projections - ln['offset']) >= distance_thresh)

    remaining = projections[keep]

    margin = 0.05
    if boundary_offset >= 0:
        beyond = remaining[(remaining > boundary_offset + margin) &
                           (remaining < boundary_offset + max_depth)]
    else:
        beyond = remaining[(remaining < boundary_offset - margin) &
                           (remaining > boundary_offset - max_depth)]

    if len(beyond) < min_peak_density:
        return None

    n_bins = max(10, int(np.ptp(beyond) / 0.01) + 1)
    hist, edges = np.histogram(beyond, bins=n_bins)
    centers = (edges[:-1] + edges[1:]) / 2

    if len(hist) < 3:
        return None

    smoothed = _smooth(hist.astype(float), sigma=3)
    pks, _ = _peaks(smoothed, prominence=20, distance=5)

    if len(pks) == 0:
        return None

    valid = [(centers[p], float(smoothed[p])) for p in pks
             if smoothed[p] >= min_peak_density]
    if not valid:
        return None

    if boundary_offset >= 0:
        closest = min(valid, key=lambda x: x[0])
    else:
        closest = max(valid, key=lambda x: x[0])

    depth = abs(closest[0] - boundary_offset)
    if depth < min_furniture_depth:
        return None

    return closest[0]


def snap_to_wall_lines(polygon_m, lines, xy, snap_dist=0.35,
                       max_furniture_depth=1.0, distance_thresh=0.03):
    """Snap polygon edges to accurate RANSAC wall positions."""
    n = len(polygon_m)
    if n < 3 or len(lines) == 0:
        return polygon_m

    verts = polygon_m.copy().astype(np.float64)

    edge_normals = np.empty((n, 2))
    edge_offsets = np.empty(n)

    for i in range(n):
        j = (i + 1) % n
        dx, dy = verts[j][0] - verts[i][0], verts[j][1] - verts[i][1]
        length = np.hypot(dx, dy)
        if length < 1e-9:
            edge_normals[i] = [0, 0]
            edge_offsets[i] = 0
            continue
        normal = np.array([-dy / length, dx / length])
        mid = (verts[i] + verts[j]) / 2
        edge_offsets[i] = normal @ mid
        edge_normals[i] = normal

    angle_map = {}
    for ln in lines:
        a = round((ln['angle'] % np.pi) * 180 / np.pi, 0)
        if a not in angle_map:
            angle_map[a] = []
        angle_map[a].append(ln)

    snapped = 0
    for i in range(n):
        if np.linalg.norm(edge_normals[i]) < 0.5:
            continue

        j = (i + 1) % n
        dx, dy = verts[j][0] - verts[i][0], verts[j][1] - verts[i][1]
        edge_angle = np.arctan2(dy, dx) % np.pi
        mid = (verts[i] + verts[j]) / 2

        best_match = None
        best_offset_diff = snap_dist

        for a, group in angle_map.items():
            line_angle_rad = np.radians(a)
            angle_diff = abs(edge_angle - line_angle_rad)
            angle_diff = min(angle_diff, np.pi - angle_diff)
            if angle_diff > np.radians(20):
                continue
            for ln in group:
                offset_at_mid = ln['normal'] @ mid
                diff = abs(offset_at_mid - ln['offset'])
                if diff < best_offset_diff:
                    best_offset_diff = diff
                    best_match = ln

        if best_match is None:
            continue

        match_angle_norm = (best_match['angle'] % np.pi) * 180 / np.pi
        same_dir = []
        for a, group in angle_map.items():
            a_diff = abs(a - match_angle_norm)
            a_diff = min(a_diff, 180 - a_diff)
            if a_diff < 10:
                same_dir.extend(group)

        has_line_beyond = False
        for l in same_dir:
            if l is best_match:
                continue
            if best_match['offset'] >= 0 and l['offset'] > best_match['offset'] + 0.10:
                has_line_beyond = True
            elif best_match['offset'] < 0 and l['offset'] < best_match['offset'] - 0.10:
                has_line_beyond = True

        if has_line_beyond:
            refined = _refine_wall_offset(
                xy, best_match['normal'], same_dir,
                best_match['offset'], distance_thresh,
                max_furniture_depth)
        else:
            refined = None

        target_offset = refined if refined is not None else best_match['offset']

        edge_normals[i] = best_match['normal']
        edge_offsets[i] = target_offset
        snapped += 1

    if snapped == 0:
        return polygon_m

    print(f'  Snapped {snapped}/{n} edges to RANSAC wall lines')

    new_verts = np.empty_like(verts)
    for i in range(n):
        prev = (i - 1) % n
        n1, d1 = edge_normals[prev], edge_offsets[prev]
        n2, d2 = edge_normals[i], edge_offsets[i]
        cross = n1[0] * n2[1] - n1[1] * n2[0]
        if abs(cross) < 1e-9:
            new_verts[i] = verts[i]
        else:
            A = np.array([n1, n2])
            b = np.array([d1, d2])
            new_verts[i] = np.linalg.solve(A, b)

    return new_verts


def collapse_corner_artifacts(verts_m, all_lines, max_artifact_len=1.5):
    """Remove polygon edges that don't align with any RANSAC wall line."""
    if len(verts_m) < 4 or len(all_lines) == 0:
        return verts_m

    verts = verts_m.copy()
    angle_tol = np.radians(15)

    def _matches(i):
        n = len(verts)
        j = (i + 1) % n
        dx, dy = verts[j][0] - verts[i][0], verts[j][1] - verts[i][1]
        edge_angle = np.arctan2(dy, dx) % np.pi
        mid = (verts[i] + verts[j]) / 2
        for ln in all_lines:
            line_angle = ln['angle'] % np.pi
            diff = abs(edge_angle - line_angle)
            diff = min(diff, np.pi - diff)
            if diff < angle_tol:
                perp_dist = abs(ln['normal'] @ mid - ln['offset'])
                if perp_dist < 0.50:
                    return True
        return False

    collapsed = 0
    changed = True
    while changed and len(verts) > 3:
        changed = False
        n = len(verts)
        matched = [_matches(i) for i in range(n)]
        best_idx, best_len = -1, float('inf')
        for i in range(n):
            j = (i + 1) % n
            elen = np.linalg.norm(verts[j] - verts[i])
            if elen >= max_artifact_len:
                continue
            removable = False
            if not matched[i]:
                removable = True
            else:
                prev = (i - 1) % n
                next_ = (i + 1) % n
                d_prev = verts[i] - verts[prev]
                d_next = verts[(next_ + 1) % n] - verts[next_]
                lp = np.linalg.norm(d_prev)
                ln_ = np.linalg.norm(d_next)
                if lp > 1e-9 and ln_ > 1e-9:
                    cos_a = np.dot(d_prev / lp, d_next / ln_)
                    if abs(cos_a) > 0.97 and lp > elen and ln_ > elen:
                        removable = True
            if removable and elen < best_len:
                best_idx, best_len = i, elen
        if best_idx < 0:
            break
        i = best_idx
        j = (i + 1) % n
        prev = (i - 1) % n
        next_ = (j + 1) % n
        d_prev = verts[i] - verts[prev]
        d_next = verts[next_] - verts[j]
        cross = d_prev[0] * d_next[1] - d_prev[1] * d_next[0]
        if abs(cross) > 0.05:
            dp = verts[j] - verts[prev]
            t = (dp[0] * d_next[1] - dp[1] * d_next[0]) / cross
            corner = verts[prev] + t * d_prev
            verts[i] = corner
            verts = np.delete(verts, j if j != 0 else len(verts) - 1,
                              axis=0)
        else:
            verts = np.delete(verts, j if j != 0 else len(verts) - 1,
                              axis=0)
        collapsed += 1
        changed = True

    if collapsed:
        print(f'  Collapsed {collapsed} corner artifact(s)')
    return verts


def polygon_to_wall_segments(verts_m):
    """Convert closed polygon vertices (in metres) to wall segment list."""
    segments = []
    n = len(verts_m)
    for i in range(n):
        x1, y1 = verts_m[i]
        x2, y2 = verts_m[(i + 1) % n]
        length = np.hypot(x2 - x1, y2 - y1)
        segments.append((float(x1), float(y1), float(x2), float(y2),
                         float(length)))
    return segments


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_floorplan(verts_m, wall_segments, extent, output_path, dpi=150):
    """Render closed room polygon with dimension labels to a PNG."""
    from matplotlib.figure import Figure
    x_min, x_max, y_min, y_max = extent

    fig = Figure(figsize=(14, 14))
    ax = fig.add_subplot(111)
    ax.set_facecolor('white')
    ax.set_aspect('equal')

    from matplotlib.patches import Polygon
    poly = Polygon(verts_m, closed=True, facecolor='#f0f0f0',
                   edgecolor='black', linewidth=2.5)
    ax.add_patch(poly)

    for x1, y1, x2, y2, length in wall_segments:
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        if angle > 90:
            angle -= 180
        elif angle < -90:
            angle += 180

        ax.text(
            mx, my, f'{length:.2f}m',
            fontsize=16, color='#2563eb', fontweight='bold',
            ha='center', va='center',
            rotation=angle, rotation_mode='anchor',
            bbox=dict(boxstyle='round,pad=0.3', facecolor='white',
                      edgecolor='#2563eb', linewidth=1, alpha=0.9),
        )

    margin = 0.3
    ax.set_xlim(x_min - margin, x_max + margin)
    ax.set_ylim(y_min - margin, y_max + margin)
    ax.set_xlabel('metres', fontsize=12)
    ax.set_ylabel('metres', fontsize=12)
    ax.set_title('Floor Plan', fontsize=16, fontweight='bold')
    ax.tick_params(labelsize=10)
    ax.grid(True, linewidth=0.3, alpha=0.5)

    fig.savefig(output_path, dpi=dpi, bbox_inches='tight',
                facecolor='white', edgecolor='none')


def render_debug_panels(points_3d, band_points, xy, lines, polygon,
                        vertical_axis, floor_axes, band_lo, band_hi,
                        output_path, dpi=150):
    """Render 4-panel debug diagnostic PNG."""
    fig, axes = plt.subplots(2, 2, figsize=(16, 16))

    subsample_3d = 50_000
    subsample_2d = 50_000

    if len(points_3d) > subsample_3d:
        idx = np.random.default_rng(0).choice(len(points_3d), subsample_3d, replace=False)
        pts_sub = points_3d[idx]
    else:
        pts_sub = points_3d
    if len(band_points) > subsample_3d:
        idx = np.random.default_rng(1).choice(len(band_points), subsample_3d, replace=False)
        band_sub = band_points[idx]
    else:
        band_sub = band_points

    labels = 'xyz'
    v_ax = vertical_axis

    ax0 = axes[0, 0]
    h_ax0 = floor_axes[0]
    ax0.scatter(pts_sub[:, h_ax0], pts_sub[:, v_ax], s=0.1, c='lightgray', alpha=0.3)
    ax0.scatter(band_sub[:, h_ax0], band_sub[:, v_ax], s=0.1, c='steelblue', alpha=0.5)
    ax0.axhline(band_lo, color='red', linewidth=1, linestyle='--', label=f'band lo={band_lo:.2f}')
    ax0.axhline(band_hi, color='red', linewidth=1, linestyle='--', label=f'band hi={band_hi:.2f}')
    ax0.set_xlabel(f'{labels[h_ax0]} (m)')
    ax0.set_ylabel(f'{labels[v_ax]} (m)')
    ax0.set_title(f'Side view: {labels[h_ax0]}{labels[v_ax]}')
    ax0.legend(fontsize=8)
    ax0.set_aspect('equal')

    ax1 = axes[0, 1]
    h_ax1 = floor_axes[1]
    ax1.scatter(pts_sub[:, h_ax1], pts_sub[:, v_ax], s=0.1, c='lightgray', alpha=0.3)
    ax1.scatter(band_sub[:, h_ax1], band_sub[:, v_ax], s=0.1, c='steelblue', alpha=0.5)
    ax1.axhline(band_lo, color='red', linewidth=1, linestyle='--', label=f'band lo={band_lo:.2f}')
    ax1.axhline(band_hi, color='red', linewidth=1, linestyle='--', label=f'band hi={band_hi:.2f}')
    ax1.set_xlabel(f'{labels[h_ax1]} (m)')
    ax1.set_ylabel(f'{labels[v_ax]} (m)')
    ax1.set_title(f'Side view: {labels[h_ax1]}{labels[v_ax]}')
    ax1.legend(fontsize=8)
    ax1.set_aspect('equal')

    ax2 = axes[1, 0]
    if len(xy) > subsample_2d:
        idx = np.random.default_rng(2).choice(len(xy), subsample_2d, replace=False)
        xy_sub = xy[idx]
    else:
        xy_sub = xy
    ax2.scatter(xy_sub[:, 0], xy_sub[:, 1], s=0.1, c='lightgray', alpha=0.3)
    colors = plt.cm.tab10(np.linspace(0, 1, max(len(lines), 1)))
    for k, ln in enumerate(lines):
        ax2.plot([ln['p1'][0], ln['p2'][0]], [ln['p1'][1], ln['p2'][1]],
                 color=colors[k % len(colors)], linewidth=2,
                 label=f'L{k} ({ln["inlier_count"]} pts)')
        ax2.plot(*ln['p1'], 'o', color=colors[k % len(colors)], markersize=5)
        ax2.plot(*ln['p2'], 'o', color=colors[k % len(colors)], markersize=5)
    ax2.set_xlabel(f'{labels[floor_axes[0]]} (m)')
    ax2.set_ylabel(f'{labels[floor_axes[1]]} (m)')
    ax2.set_title('Top-down: RANSAC lines')
    if len(lines) <= 10:
        ax2.legend(fontsize=7, loc='best')
    ax2.set_aspect('equal')

    ax3 = axes[1, 1]
    if polygon is not None and len(polygon) >= 3:
        closed = np.vstack([polygon, polygon[0]])
        ax3.plot(closed[:, 0], closed[:, 1], 'k-', linewidth=2)
        ax3.plot(polygon[:, 0], polygon[:, 1], 'ro', markersize=6)
        for i in range(len(polygon)):
            p_a = polygon[i]
            p_b = polygon[(i + 1) % len(polygon)]
            length = np.linalg.norm(p_b - p_a)
            mx, my = (p_a + p_b) / 2
            ax3.text(mx, my, f'{length:.2f}m', fontsize=7, color='#2563eb',
                     ha='center', va='center',
                     bbox=dict(boxstyle='round,pad=0.15', facecolor='white',
                               edgecolor='none', alpha=0.85))
    else:
        ax3.text(0.5, 0.5, 'No polygon', transform=ax3.transAxes,
                 ha='center', va='center', fontsize=14, color='red')
    ax3.set_xlabel(f'{labels[floor_axes[0]]} (m)')
    ax3.set_ylabel(f'{labels[floor_axes[1]]} (m)')
    ax3.set_title('Final polygon')
    ax3.set_aspect('equal')

    fig.tight_layout()
    fig.savefig(output_path, dpi=dpi, bbox_inches='tight',
                facecolor='white', edgecolor='none')
    plt.close(fig)


# ---------------------------------------------------------------------------
# Top-level API functions
# ---------------------------------------------------------------------------

def _prepare_2d_points(ply_path, height_band='auto', ransac_dist=0.03,
                       ransac_min_inliers=50, ortho_tol=15.0):
    """Shared pipeline: read PLY, detect axes, slice, filter, RANSAC.

    Returns a dict with all intermediate results needed by both
    detect_walls() and generate_floorplan().
    """
    points, properties, comments = read_ply(ply_path)
    print(f'  Loaded {len(points):,} points')

    # Detect floor axes
    wf_vertical = get_world_frame_vertical(comments)
    if wf_vertical is not None:
        floor_axes = tuple(i for i in range(3) if i != wf_vertical)
    elif any(c.startswith('floor_leveled') for c in comments):
        floor_axes = (0, 1)
    else:
        floor_axes = detect_floor_axes(points)

    vertical_axis = [i for i in range(3) if i not in floor_axes][0]
    all_points = points

    # Height band
    points = slice_height_band(points, vertical_axis, mode=height_band)
    v = all_points[:, vertical_axis]
    if height_band == 'off':
        band_lo, band_hi = float(v.min()), float(v.max())
    elif height_band == 'auto':
        lo, hi = float(v.min()), float(v.max())
        band_lo = lo + 0.10 * (hi - lo)
        band_hi = hi - 0.10 * (hi - lo)
    else:
        parts = height_band.split('-')
        band_lo, band_hi = float(parts[0]), float(parts[1])

    # Voxel downsample 3D for faster furniture voting (5cm grid)
    voxel_3d = 0.05
    q3d = (points[:, :3] / voxel_3d).astype(np.int32)
    _, u3d_idx = np.unique(q3d, axis=0, return_index=True)
    points_down = points[u3d_idx]
    print(f'  3D voxel downsample: {len(points):,} -> {len(points_down):,} pts')

    # Multi-height voting removes furniture (keeps surfaces visible at 3+ heights)
    all_band_points = points_down
    points = multiheight_vote_filter(points_down, vertical_axis, floor_axes)

    # Project to 2D
    xy = points[:, [floor_axes[0], floor_axes[1]]]
    xy_full = all_band_points[:, [floor_axes[0], floor_axes[1]]]

    # Voxel downsample 2D for fast RANSAC (3cm grid)
    voxel_size = 0.03
    quantized = (xy / voxel_size).astype(np.int32)
    _, unique_idx = np.unique(quantized, axis=0, return_index=True)
    xy_down = xy[unique_idx]
    print(f'  Voxel downsample: {len(xy):,} -> {len(xy_down):,} pts '
          f'({voxel_size*100:.0f}cm grid)')

    # RANSAC wall detection on downsampled points
    raw_lines = ransac_detect_lines(
        xy_down, distance_thresh=ransac_dist, min_inliers=max(5, ransac_min_inliers // 10))

    # Merge + filter + orthogonalize
    room_diagonal = float(np.linalg.norm(xy.max(axis=0) - xy.min(axis=0)))
    merge_dist = max(0.10, room_diagonal * 0.008)
    lines = merge_lines(raw_lines, dist_thresh=merge_dist)
    lines = filter_lines_by_support(lines)
    if ortho_tol > 0:
        lines = orthogonalize_lines(lines, ortho_tol_deg=ortho_tol)
    remerge_dist = max(0.20, room_diagonal * 0.02)
    all_lines = merge_lines(lines, angle_thresh_deg=5.0, dist_thresh=remerge_dist)
    all_lines = score_wall_lines(all_lines)

    # Detect ceiling for ceiling-based floor plan approach
    ceiling_z = _detect_ceiling_height(all_points, vertical_axis)

    return {
        'all_points': all_points,
        'points': points,
        'properties': properties,
        'comments': comments,
        'floor_axes': floor_axes,
        'vertical_axis': vertical_axis,
        'xy': xy,            # voted 2D points
        'xy_full': xy_full,  # full band 2D for contour extraction
        'all_lines': all_lines,
        'raw_lines': raw_lines,
        'room_diagonal': room_diagonal,
        'ortho_tol': ortho_tol,
        'band_lo': band_lo,
        'band_hi': band_hi,
        'ceiling_z': ceiling_z,
    }


def detect_walls(ply_path, preview_path=None, ortho_tol=15.0,
                 height_band='auto', ransac_dist=0.03,
                 ransac_min_inliers=50):
    """Detect all candidate walls and render a labeled preview.

    Returns (walls, preview_png_path) where walls is a list of dicts:
      [ { id, angle_deg, offset, inlier_count, length, direction_group }, ... ]

    Each wall is a RANSAC line. Walls in the same direction_group are
    parallel (same angle) — the user picks one from each side per group
    to define the room.
    """
    if preview_path is None:
        base, _ = os.path.splitext(ply_path)
        preview_path = f'{base}_walls_preview.png'

    print(f'[DetectWalls] Processing {ply_path}...')
    ctx = _prepare_2d_points(ply_path, height_band, ransac_dist,
                             ransac_min_inliers, ortho_tol)
    all_lines = ctx['all_lines']
    xy = ctx['xy']
    floor_axes = ctx['floor_axes']

    # Group by direction, tracking original index in all_lines
    angle_groups = {}
    for orig_idx, ln in enumerate(all_lines):
        a = round((ln['angle'] % np.pi) * 180 / np.pi, 0)
        if a not in angle_groups:
            angle_groups[a] = []
        angle_groups[a].append((orig_idx, ln))

    # Build wall list with direction group labels
    walls = []
    group_id = 0
    group_angles = sorted(angle_groups.keys())
    for angle_key in group_angles:
        group = angle_groups[angle_key]
        # Sort by offset so walls are ordered from one side to the other
        group.sort(key=lambda l: l[1]['offset'])
        for orig_idx, ln in group:
            seg_len = float(np.linalg.norm(ln['p2'] - ln['p1']))
            walls.append({
                'id': len(walls),
                'line_index': orig_idx,  # index into all_lines
                'angle_deg': round(float(np.degrees(ln['angle'])), 1),
                'offset': round(float(ln['offset']), 3),
                'inlier_count': int(ln['inlier_count']),
                'length': round(seg_len, 2),
                'direction_group': group_id,
            })
        group_id += 1

    # Compute pairwise distances within each direction group
    for w in walls:
        w['distances_to'] = {}
    for i in range(len(walls)):
        for j in range(i + 1, len(walls)):
            if walls[i]['direction_group'] == walls[j]['direction_group']:
                dist = round(abs(walls[j]['offset'] - walls[i]['offset']), 3)
                walls[i]['distances_to'][walls[j]['id']] = dist
                walls[j]['distances_to'][walls[i]['id']] = dist

    print(f'[DetectWalls] Found {len(walls)} wall candidates '
          f'in {group_id} direction(s)')

    # Render preview
    _render_wall_preview(xy, all_lines, walls, floor_axes, preview_path)
    print(f'[DetectWalls] Preview -> {preview_path}')

    return walls, preview_path


def _render_wall_preview(xy, all_lines, walls, floor_axes, output_path,
                         dpi=150):
    """Render a top-down view with all wall candidates labeled."""
    from matplotlib.figure import Figure
    fig = Figure(figsize=(12, 12))
    ax = fig.add_subplot(111)
    ax.set_facecolor('#1a1a2e')

    # Subsample points for scatter
    if len(xy) > 60_000:
        idx = np.random.default_rng(0).choice(len(xy), 60_000, replace=False)
        xy_sub = xy[idx]
    else:
        xy_sub = xy
    ax.scatter(xy_sub[:, 0], xy_sub[:, 1], s=0.2, c='#444466', alpha=0.4)

    # Distinct colors per direction group
    group_colors = ['#e94560', '#4ecca3', '#f0a500', '#6c63ff',
                    '#00d2ff', '#ff6b6b', '#51cf66', '#ffd43b']

    # Build lookup: wall_id -> wall dict for quick access
    wall_by_id = {w['id']: w for w in walls}
    # Build lookup: line_index -> wall for rendering
    wall_by_line_idx = {w['line_index']: w for w in walls}

    labels = 'xyz'
    for i, ln in enumerate(all_lines):
        # Find matching wall entry by original line index
        wall = wall_by_line_idx.get(i)
        gid = wall['direction_group'] if wall else 0
        color = group_colors[gid % len(group_colors)]

        # Draw the line segment, extended a bit
        d = ln['p2'] - ln['p1']
        seg_len = np.linalg.norm(d)
        if seg_len > 0:
            ext = d / seg_len * 0.3
            p1 = ln['p1'] - ext
            p2 = ln['p2'] + ext
        else:
            p1, p2 = ln['p1'], ln['p2']

        ax.plot([p1[0], p2[0]], [p1[1], p2[1]],
                color=color, linewidth=3, alpha=0.9)

        # Label at midpoint
        mid = (ln['p1'] + ln['p2']) / 2
        label_text = f'W{i}'
        if wall:
            label_text = f'W{wall["id"]} ({wall["length"]:.1f}m, {wall["inlier_count"]//1000}k pts)'
        ax.annotate(label_text, mid, fontsize=8, fontweight='bold',
                    color='white', ha='center', va='bottom',
                    bbox=dict(boxstyle='round,pad=0.3', facecolor=color,
                              alpha=0.85, edgecolor='none'))

    # Show distances between parallel walls
    done_pairs = set()
    for wall in walls:
        for other_id_str, dist in wall.get('distances_to', {}).items():
            other_id = int(other_id_str) if isinstance(other_id_str, str) else other_id_str
            pair = tuple(sorted([wall['id'], other_id]))
            if pair in done_pairs:
                continue
            done_pairs.add(pair)
            # Use line_index to get the correct line from all_lines
            other_wall = wall_by_id.get(other_id)
            if not other_wall:
                continue
            ln1 = all_lines[wall['line_index']]
            ln2 = all_lines[other_wall['line_index']]
            mid1 = (ln1['p1'] + ln1['p2']) / 2
            mid2 = (ln2['p1'] + ln2['p2']) / 2
            center = (mid1 + mid2) / 2
            ax.annotate(f'{dist:.2f}m', center, fontsize=9,
                        fontweight='bold', color='white', ha='center',
                        va='center',
                        bbox=dict(boxstyle='round,pad=0.3',
                                  facecolor='#333355', alpha=0.9,
                                  edgecolor='white', linewidth=0.5))

    ax.set_xlabel(f'{labels[floor_axes[0]]} (m)')
    ax.set_ylabel(f'{labels[floor_axes[1]]} (m)')
    ax.set_title('Detected Walls — select which ones to keep', color='white',
                 fontsize=14, fontweight='bold')
    ax.set_aspect('equal')
    ax.tick_params(colors='#888')
    for spine in ax.spines.values():
        spine.set_color('#444')

    fig.set_facecolor('#1a1a2e')
    fig.savefig(output_path, dpi=dpi, bbox_inches='tight',
                facecolor='#1a1a2e', edgecolor='none')


def generate_candidates(ply_path, output_dir=None, ortho_tol=15.0,
                        height_band='auto', ransac_dist=0.03,
                        ransac_min_inliers=50, min_wall_length=0.3,
                        resolution=0.05):
    """Generate multiple candidate floor plans from all plausible wall combos.

    Runs RANSAC once, does two merges (standard + tight) to find more wall
    candidates, then generates floor plans in parallel (one per CPU core).

    Returns list of candidate dicts:
      [ { id, dimensions, area_m2, num_walls, wall_lengths, png_path }, ... ]
    """
    from itertools import product
    from concurrent.futures import as_completed

    if output_dir is None:
        base, _ = os.path.splitext(ply_path)
        output_dir = f'{base}_candidates'
    os.makedirs(output_dir, exist_ok=True)

    print(f'[Candidates] Processing {ply_path}...')
    ctx = _prepare_2d_points(ply_path, height_band, ransac_dist,
                             ransac_min_inliers, ortho_tol)

    # Reuse raw RANSAC lines from ctx — do a TIGHTER merge to keep
    # close parallel walls as separate candidates (no second RANSAC!)
    raw_lines = ctx['raw_lines']
    room_diagonal = ctx['room_diagonal']

    merge_dist_tight = max(0.08, room_diagonal * 0.005)
    tight_lines = merge_lines(raw_lines, dist_thresh=merge_dist_tight)
    tight_lines = filter_lines_by_support(tight_lines, min_support_ratio=0.05)
    if ortho_tol > 0:
        tight_lines = orthogonalize_lines(tight_lines, ortho_tol_deg=ortho_tol)
    remerge_tight = max(0.10, room_diagonal * 0.008)
    all_lines = merge_lines(tight_lines, angle_thresh_deg=5.0,
                            dist_thresh=remerge_tight)

    print(f'  Candidate wall lines: {len(all_lines)} '
          f'(vs {len(ctx["all_lines"])} after standard merge)')
    for i, ln in enumerate(all_lines):
        seg_len = np.linalg.norm(ln['p2'] - ln['p1'])
        print(f'    L{i}: {ln["inlier_count"]:,} inliers, '
              f'len={seg_len:.2f}m, offset={ln["offset"]:.3f}')

    xy = ctx['xy']
    xy_full = ctx['xy_full']

    # Group lines by direction
    groups = {}
    for idx, ln in enumerate(all_lines):
        a = round((ln['angle'] % np.pi) * 180 / np.pi, 0)
        if a not in groups:
            groups[a] = []
        groups[a].append(idx)

    # For each direction, find wall pairs (one from each side of scanner)
    direction_pairs = []
    for angle_key in sorted(groups.keys()):
        indices = groups[angle_key]
        lines_in_group = [(i, all_lines[i]) for i in indices]
        lines_in_group.sort(key=lambda x: x[1]['offset'])

        neg = [(i, ln) for i, ln in lines_in_group if ln['offset'] < 0]
        pos = [(i, ln) for i, ln in lines_in_group if ln['offset'] >= 0]

        pairs = []
        if neg and pos:
            for ni, nln in neg:
                for pi, pln in pos:
                    dist = abs(pln['offset'] - nln['offset'])
                    if dist > 0.5:
                        pairs.append({
                            'line_indices': [ni, pi],
                            'dimension': round(dist, 3),
                            'inliers': nln['inlier_count'] + pln['inlier_count'],
                        })
        elif len(lines_in_group) >= 2:
            for a in range(len(lines_in_group)):
                for b in range(a + 1, len(lines_in_group)):
                    i_a, ln_a = lines_in_group[a]
                    i_b, ln_b = lines_in_group[b]
                    dist = abs(ln_b['offset'] - ln_a['offset'])
                    if dist > 0.5:
                        pairs.append({
                            'line_indices': [i_a, i_b],
                            'dimension': round(dist, 3),
                            'inliers': ln_a['inlier_count'] + ln_b['inlier_count'],
                        })

        # Add full-group config for L/T/U-shaped rooms
        if len(lines_in_group) > 2:
            full_group = {
                'line_indices': [i for i, _ in lines_in_group],
                'dimension': round(abs(lines_in_group[-1][1]['offset']
                                       - lines_in_group[0][1]['offset']), 3),
                'inliers': sum(ln['inlier_count'] for _, ln in lines_in_group),
            }
            pairs.insert(0, full_group)

        if pairs:
            pairs.sort(key=lambda p: -p['inliers'])
            direction_pairs.append(pairs[:3])

    if not direction_pairs:
        raise ValueError('No wall pairs found')

    combos = list(product(*direction_pairs))
    # Rank by total inlier confidence, keep top 4
    combos.sort(key=lambda c: -sum(p['inliers'] for p in c))
    combos = combos[:4]

    print(f'[Candidates] Generating {len(combos)} candidate floor plan(s) '
          f'in parallel...')

    # Build args for each candidate
    jobs = []
    for ci, combo in enumerate(combos):
        line_indices = sorted(set(
            idx for pair in combo for idx in pair['line_indices']))
        dimensions = [pair['dimension'] for pair in combo]
        selected = [all_lines[i] for i in line_indices]
        png_path = os.path.join(output_dir, f'candidate_{ci}.png')
        jobs.append((ci, selected, dimensions, png_path))

    # Generate floor plans in parallel using multiprocessing
    # Pi 4 has 4 cores; use 2 workers to avoid OOM (each needs ~300MB)
    def _build_one(args):
        ci, selected, dimensions, png_path = args
        try:
            _, meta = _build_floorplan_from_lines(
                selected, xy, xy_full, resolution,
                ortho_tol, ransac_dist, min_wall_length, png_path)
            return {
                'id': ci,
                'dimensions': dimensions,
                'area_m2': meta['area_m2'],
                'num_walls': meta['num_walls'],
                'wall_lengths': meta['wall_lengths'],
                'png_path': png_path,
            }
        except Exception as e:
            print(f'  Candidate {ci} failed: {e}')
            return None

    # Use threads — numpy/cv2 release the GIL, matplotlib with Agg is safe
    from concurrent.futures import ThreadPoolExecutor
    candidates = []
    with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:
        futures = {pool.submit(_build_one, j): j[0] for j in jobs}
        for future in as_completed(futures):
            result = future.result()
            if result:
                candidates.append(result)
                dim_str = ' x '.join(f'{d:.2f}m' for d in sorted(result['dimensions']))
                print(f'  Candidate {result["id"]}: {dim_str}, '
                      f'area={result["area_m2"]}m2')

    # Sort by ID for stable ordering
    candidates.sort(key=lambda c: c['id'])

    if not candidates:
        raise ValueError('All candidate floor plans failed')

    print(f'[Candidates] Done: {len(candidates)} candidate(s)')
    return candidates


def generate_floorplan_from_selection(ply_path, selected_wall_ids,
                                      output_path=None, ortho_tol=15.0,
                                      resolution=0.05, height_band='auto',
                                      ransac_dist=0.03,
                                      ransac_min_inliers=50,
                                      min_wall_length=0.3,
                                      walls_data=None):
    """Generate floor plan using only user-selected walls.

    selected_wall_ids: list of wall IDs from detect_walls() to use.
    walls_data: the walls list from detect_walls() (with line_index).
                If provided, maps wall IDs to line indices directly.
                If None, re-detects walls to rebuild the mapping.
    Returns (png_path, metadata).
    """
    if output_path is None:
        base, _ = os.path.splitext(ply_path)
        output_path = f'{base}_floorplan.png'

    print(f'[FloorPlan] Generating from {len(selected_wall_ids)} selected walls...')
    ctx = _prepare_2d_points(ply_path, height_band, ransac_dist,
                             ransac_min_inliers, ortho_tol)

    all_lines_full = ctx['all_lines']
    xy = ctx['xy']
    xy_full = ctx['xy_full']

    # Map wall IDs to original line indices
    if walls_data:
        id_to_line_idx = {w['id']: w['line_index'] for w in walls_data}
    else:
        # No walls_data — wall IDs are line indices (backward compat)
        id_to_line_idx = {i: i for i in range(len(all_lines_full))}

    line_indices = set()
    for wid in selected_wall_ids:
        li = id_to_line_idx.get(wid)
        if li is not None and li < len(all_lines_full):
            line_indices.add(li)

    all_lines = [all_lines_full[i] for i in sorted(line_indices)]

    if len(all_lines) < 2:
        raise ValueError(f'Need at least 2 walls, got {len(all_lines)}')

    print(f'  Using {len(all_lines)} of {len(all_lines_full)} walls')
    for i, ln in enumerate(all_lines):
        seg_len = np.linalg.norm(ln['p2'] - ln['p1'])
        print(f'    L{i}: {ln["inlier_count"]:,} inliers, '
              f'length={seg_len:.2f}m, offset={ln["offset"]:.3f}')

    # --- Room contour extraction (same as generate_floorplan steps 7+) ---
    return _build_floorplan_from_lines(
        all_lines, xy, xy_full, resolution, ortho_tol,
        ransac_dist, min_wall_length, output_path)


def _build_floorplan_from_lines(all_lines, xy, xy_full, resolution,
                                ortho_tol, ransac_dist, min_wall_length,
                                output_path):
    """Shared: build floor plan polygon + render from RANSAC lines.

    Used by both generate_floorplan() and generate_floorplan_from_selection().
    Returns (png_path, metadata).
    """
    doorways = detect_doorways(all_lines)
    if doorways:
        print(f'  Detected {len(doorways)} doorway(s)')

    seed = xy.mean(axis=0)  # point cloud centroid = scanner position
    room_mask, extent, dilation_offset, enclosed = build_room_mask(
        xy, resolution, doorways, all_lines=all_lines, seed_xy=seed)
    x_min, x_max, y_min, y_max = extent

    # If flood-fill found an enclosed room, extract contour from mask.
    # Otherwise fall back to alpha shape from the full point cloud.
    verts_m = None
    from_alpha = False
    bbox_area = float(np.ptp(xy[:, 0]) * np.ptp(xy[:, 1]))

    if enclosed:
        contours, _ = cv2.findContours(room_mask, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            largest = max(contours, key=cv2.contourArea)
            mask_area = cv2.contourArea(largest) * resolution * resolution
            if mask_area > bbox_area * 0.25:
                perimeter = cv2.arcLength(largest, closed=True)
                epsilon = min(0.04 * perimeter, 0.20 / resolution)
                approx = cv2.approxPolyDP(largest, epsilon, closed=True)
                poly_px = approx.reshape(-1, 2)
                verts_m = np.column_stack([
                    x_min + poly_px[:, 0] * resolution,
                    y_min + poly_px[:, 1] * resolution,
                ])
                if dilation_offset > 0:
                    verts_m = inset_polygon(verts_m, dilation_offset)
            else:
                print(f'  Enclosed area too small ({mask_area:.1f}m2 vs '
                      f'bbox {bbox_area:.1f}m2), trying alpha shape')

    if verts_m is None:
        # First fallback: try geometric polygon from wall-line intersections.
        # For L/T/U shapes this can directly produce the correct polygon.
        geo = _polygon_from_wall_lines(all_lines, xy=xy_full)
        if geo is not None and len(geo) >= 4:
            verts_m = geo
            print(f'  Geometric polygon: {len(verts_m)} vertices')

    if verts_m is None:
        # Second fallback: alpha shape from full point cloud.
        # room boundary -- furniture fills the interior, giving shape.
        alpha_res = 0.10
        grid_a, extent_a = build_occupancy_grid(xy_full, alpha_res, padding=0.5)
        thresh_a = max(1, int(np.percentile(grid_a[grid_a > 0], 10)))
        mask_a = (grid_a >= thresh_a).astype(np.uint8) * 255
        k_a = max(5, int(round(0.50 / alpha_res))) | 1
        kernel_a = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_a, k_a))
        mask_a = cv2.morphologyEx(mask_a, cv2.MORPH_CLOSE, kernel_a)
        mask_a = cv2.morphologyEx(mask_a, cv2.MORPH_OPEN, kernel_a)
        contours_a, _ = cv2.findContours(mask_a, cv2.RETR_EXTERNAL,
                                          cv2.CHAIN_APPROX_SIMPLE)
        if contours_a:
            largest_a = max(contours_a, key=cv2.contourArea)
            peri_a = cv2.arcLength(largest_a, closed=True)
            eps_a = min(0.08 * peri_a, 1.0 / alpha_res)
            approx_a = cv2.approxPolyDP(largest_a, eps_a, closed=True)
            poly_a = approx_a.reshape(-1, 2)
            verts_m = np.column_stack([
                extent_a[0] + poly_a[:, 0] * alpha_res,
                extent_a[2] + poly_a[:, 1] * alpha_res,
            ])
            from_alpha = True
            print(f'  Alpha shape fallback: {len(verts_m)} vertices')

    if verts_m is None:
        hull = cv2.convexHull(xy.astype(np.float32))
        verts_m = hull.reshape(-1, 2)

    print(f'  Contour vertices: {len(verts_m)}')

    # Refinement pipeline.
    if from_alpha:
        # Alpha shapes: orthogonalize aggressively to straighten the rough
        # boundary, then snap once to align with precise RANSAC positions.
        refine_tol = 15.0 if ortho_tol == 0 else ortho_tol
        edge_lens = [np.linalg.norm(verts_m[(i + 1) % len(verts_m)] - verts_m[i])
                     for i in range(len(verts_m))]
        adaptive_min = max(2.0,
                           min(max(edge_lens) * 0.20, 3.0)) if edge_lens else 2.0
        verts_m = orthogonalize_polygon(
            verts_m, angle_tol_deg=refine_tol, min_wall_m=adaptive_min)
        verts_m = merge_colinear_edges(verts_m, angle_tol_deg=15.0)
        # Now snap the clean orthogonal polygon to RANSAC wall positions
        verts_m = snap_to_wall_lines(verts_m, all_lines, xy_full,
                                      distance_thresh=ransac_dist,
                                      snap_dist=1.5)
        verts_m = collapse_corner_artifacts(verts_m, all_lines)
        # Post-snap cleanup: snap can create new short edges
        verts_m = orthogonalize_polygon(
            verts_m, angle_tol_deg=refine_tol, min_wall_m=adaptive_min)
        verts_m = merge_colinear_edges(verts_m, angle_tol_deg=15.0)
    else:
        # Mask-extracted polygons: standard refinement pipeline.
        verts_m = snap_to_wall_lines(verts_m, all_lines, xy_full,
                                      distance_thresh=ransac_dist)
        verts_m = collapse_corner_artifacts(verts_m, all_lines)
        edge_lens = [np.linalg.norm(verts_m[(i + 1) % len(verts_m)] - verts_m[i])
                     for i in range(len(verts_m))]
        adaptive_min = max(min_wall_length,
                           min(max(edge_lens) * 0.08, 0.6)) if edge_lens else min_wall_length
        refine_tol = 15.0 if ortho_tol == 0 else ortho_tol
        verts_m = orthogonalize_polygon(
            verts_m, angle_tol_deg=refine_tol, min_wall_m=adaptive_min)
        verts_m = merge_colinear_edges(verts_m)

    wall_segments = polygon_to_wall_segments(verts_m)
    print(f'  Wall segments: {len(wall_segments)}')
    for i, (x1, y1, x2, y2, length) in enumerate(wall_segments):
        angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        print(f'    W{i}: {length:.3f}m  ({angle:.1f} deg)')

    # Render
    margin = max(np.ptp(verts_m[:, 0]), np.ptp(verts_m[:, 1])) * 0.05
    render_extent = (
        float(verts_m[:, 0].min()) - margin,
        float(verts_m[:, 0].max()) + margin,
        float(verts_m[:, 1].min()) - margin,
        float(verts_m[:, 1].max()) + margin,
    )
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    render_floorplan(verts_m, wall_segments, render_extent, output_path)

    size_kb = os.path.getsize(output_path) / 1024
    print(f'[FloorPlan] Saved {len(wall_segments)} wall segments -> {output_path} '
          f'({size_kb:.0f} KB)')

    # Compute area
    n = len(verts_m)
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += verts_m[i][0] * verts_m[j][1]
        area -= verts_m[j][0] * verts_m[i][1]
    area = abs(area) / 2.0

    metadata = {
        'num_walls': len(wall_segments),
        'wall_lengths': [round(s[4], 3) for s in wall_segments],
        'vertices': [[round(float(v[0]), 3), round(float(v[1]), 3)]
                     for v in verts_m],
        'area_m2': round(area, 2),
        '_verts': verts_m,
    }

    return output_path, metadata


def _ceiling_based_floorplan(all_points, vertical_axis, floor_axes,
                             ceiling_z, all_lines, output_path,
                             resolution=0.05, xy_clip=None):
    """Build floor plan from ceiling points, refined by RANSAC walls.

    The ceiling defines the room shape.  RANSAC wall lines only refine
    the precise wall positions.

    Returns (output_path, metadata) or None if ceiling approach fails.
    """
    if ceiling_z is None:
        return None

    # 1. Extract ceiling points
    v = all_points[:, vertical_axis]
    ceil_mask = (v > ceiling_z - 0.5) & (v < ceiling_z + 0.5)
    ceil_pts = all_points[ceil_mask]
    if len(ceil_pts) < 500:
        print(f'  Ceiling: only {len(ceil_pts)} points, skipping')
        return None

    ceil_xy = ceil_pts[:, [floor_axes[0], floor_axes[1]]]

    # Clip ceiling to wall bounding box (ceiling can extend into adjacent
    # rooms through doorways; walls define the actual room extent)
    if xy_clip is not None:
        pad = 1.0
        clip_min = xy_clip.min(axis=0) - pad
        clip_max = xy_clip.max(axis=0) + pad
        clip_mask = ((ceil_xy[:, 0] >= clip_min[0]) &
                     (ceil_xy[:, 0] <= clip_max[0]) &
                     (ceil_xy[:, 1] >= clip_min[1]) &
                     (ceil_xy[:, 1] <= clip_max[1]))
        ceil_xy = ceil_xy[clip_mask]
        if len(ceil_xy) < 500:
            return None
    print(f'  Ceiling: {len(ceil_xy):,} points at z={ceiling_z:.2f}m')

    # 2. Occupancy grid with binary threshold
    grid, extent = build_occupancy_grid(ceil_xy, resolution, padding=0.3)
    binary = (grid >= 1).astype(np.uint8) * 255  # binary: any point = occupied

    # Morphological close/open (11x11 = 55cm to bridge scan gaps)
    k = 11
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)

    # 3. Extract contour
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    largest = max(contours, key=cv2.contourArea)
    peri = cv2.arcLength(largest, True)
    approx = cv2.approxPolyDP(largest, 0.02 * peri, True)
    poly = approx.reshape(-1, 2)

    x_min, _, y_min, _ = extent
    verts = np.column_stack([
        x_min + poly[:, 0] * resolution,
        y_min + poly[:, 1] * resolution,
    ])
    if len(verts) < 4:
        return None

    print(f'  Ceiling contour: {len(verts)} vertices')

    # 4. Re-extract contour in the principal-direction-aligned frame.
    #    This gives naturally axis-aligned edges (no snapping needed).
    if all_lines:
        principal = float(max(all_lines,
                              key=lambda l: l['inlier_count'])['angle'])
    else:
        principal = 0.0

    # Rotate ceiling points so principal direction becomes horizontal
    cos_a, sin_a = np.cos(-principal), np.sin(-principal)
    rot = np.array([[cos_a, -sin_a], [sin_a, cos_a]])
    ceil_xy_rot = ceil_xy @ rot.T

    # Re-do occupancy grid in the rotated frame
    grid_r, ext_r = build_occupancy_grid(ceil_xy_rot, resolution, padding=0.3)
    binary_r = (grid_r >= 1).astype(np.uint8) * 255
    binary_r = cv2.morphologyEx(binary_r, cv2.MORPH_CLOSE, kernel)
    binary_r = cv2.morphologyEx(binary_r, cv2.MORPH_OPEN, kernel)
    # Adaptive smoothing: kernel = 5% of shorter room dimension
    # Small rooms get small kernels, large rooms get larger ones
    short_dim = min(grid_r.shape[0], grid_r.shape[1]) * resolution
    smooth_m = max(0.3, min(short_dim * 0.15, 2.0))
    k2 = max(5, int(round(smooth_m / resolution))) | 1
    kernel2 = cv2.getStructuringElement(cv2.MORPH_RECT, (k2, k2))
    binary_r = cv2.morphologyEx(binary_r, cv2.MORPH_CLOSE, kernel2)
    binary_r = cv2.morphologyEx(binary_r, cv2.MORPH_OPEN, kernel2)

    contours_r, _ = cv2.findContours(binary_r, cv2.RETR_EXTERNAL,
                                      cv2.CHAIN_APPROX_SIMPLE)
    if not contours_r:
        return None
    largest_r = max(contours_r, key=cv2.contourArea)
    peri_r = cv2.arcLength(largest_r, True)
    approx_r = cv2.approxPolyDP(largest_r, 0.03 * peri_r, True)
    poly_r = approx_r.reshape(-1, 2)

    xr_min, _, yr_min, _ = ext_r
    verts_rot = np.column_stack([
        xr_min + poly_r[:, 0] * resolution,
        yr_min + poly_r[:, 1] * resolution,
    ])

    # Rotate back to original frame
    rot_back = np.array([[cos_a, sin_a], [-sin_a, cos_a]])
    verts = verts_rot @ rot_back.T

    if len(verts) < 4:
        return None
    # Clean grid staircase: remove tiny steps, merge colinear fragments
    for _ in range(3):  # iterate to handle cascading merges
        # Remove pixelation steps (<0.5m)
        changed = True
        while changed and len(verts) > 4:
            changed = False
            lens = [np.linalg.norm(verts[(i+1) % len(verts)] - verts[i])
                    for i in range(len(verts))]
            si = int(np.argmin(lens))
            if lens[si] < 0.5:
                j = (si + 1) % len(verts)
                prev = (si - 1) % len(verts)
                nxt = (j + 1) % len(verts)
                d_prev = verts[si] - verts[prev]
                d_next = verts[nxt] - verts[j]
                cross = d_prev[0] * d_next[1] - d_prev[1] * d_next[0]
                if abs(cross) > 1e-9:
                    dp = verts[j] - verts[prev]
                    t = (dp[0] * d_next[1] - dp[1] * d_next[0]) / cross
                    verts[si] = verts[prev] + t * d_prev
                else:
                    verts[si] = (verts[si] + verts[j]) / 2
                verts = np.delete(verts, j, axis=0)
                changed = True
        verts = merge_colinear_edges(verts, angle_tol_deg=10.0)
    print(f'  Aligned contour: {len(verts)} vertices')

    # 4. Fine-tune each edge with nearest RANSAC wall (same wall count).
    #    Ceiling contour edges ARE the walls. RANSAC only nudges them.
    n = len(verts)
    edge_normals = np.empty((n, 2))
    edge_offsets = np.empty(n)
    snap_count = 0

    for i in range(n):
        j = (i + 1) % n
        dx, dy = verts[j] - verts[i]
        length = np.hypot(dx, dy)
        if length < 1e-9:
            edge_normals[i] = [0, 0]
            edge_offsets[i] = 0
            continue
        normal = np.array([-dy / length, dx / length])
        mid = (verts[i] + verts[j]) / 2
        edge_normals[i] = normal
        edge_offsets[i] = float(normal @ mid)

        if not all_lines:
            continue

        # Find nearest RANSAC wall within 18° angle (10% of 180)
        edge_angle = np.arctan2(dy, dx) % np.pi
        best_dist = float('inf')
        best_ln = None
        for ln in all_lines:
            la = ln['angle'] % np.pi
            diff = abs(edge_angle - la)
            diff = min(diff, np.pi - diff)
            if diff > np.radians(18):
                continue
            perp_dist = abs(float(ln['normal'] @ mid) - ln['offset'])
            if perp_dist < best_dist:
                best_dist = perp_dist
                best_ln = ln

        if best_ln is not None:
            edge_normals[i] = best_ln['normal']
            edge_offsets[i] = best_ln['offset']
            snap_count += 1

    # Recompute vertices from refined edge lines
    for i in range(n):
        j = (i + 1) % n
        A = np.array([edge_normals[i], edge_normals[j]])
        b = np.array([edge_offsets[i], edge_offsets[j]])
        det = A[0, 0] * A[1, 1] - A[0, 1] * A[1, 0]
        if abs(det) > 1e-9:
            new_pt = np.array([(b[0]*A[1,1] - b[1]*A[0,1]) / det,
                               (A[0,0]*b[1] - A[1,0]*b[0]) / det])
            if np.linalg.norm(new_pt - verts[j]) < 1.0:
                verts[j] = new_pt

    print(f'  RANSAC refined {snap_count}/{n} edges')

    # 7. Compute area + render
    wall_segments = polygon_to_wall_segments(verts)
    print(f'  Wall segments: {len(wall_segments)}')
    for i, (x1, y1, x2, y2, length) in enumerate(wall_segments):
        angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        print(f'    W{i}: {length:.3f}m  ({angle:.1f} deg)')

    margin = max(np.ptp(verts[:, 0]), np.ptp(verts[:, 1])) * 0.05
    render_extent = (
        float(verts[:, 0].min()) - margin,
        float(verts[:, 0].max()) + margin,
        float(verts[:, 1].min()) - margin,
        float(verts[:, 1].max()) + margin,
    )
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    render_floorplan(verts, wall_segments, render_extent, output_path)

    n = len(verts)
    area = abs(sum(verts[i][0] * verts[(i+1) % n][1]
                   - verts[(i+1) % n][0] * verts[i][1]
                   for i in range(n))) / 2.0

    metadata = {
        'num_walls': len(wall_segments),
        'wall_lengths': [round(s[4], 3) for s in wall_segments],
        'vertices': [[round(float(v[0]), 3), round(float(v[1]), 3)]
                     for v in verts],
        'area_m2': round(area, 2),
        '_verts': verts,
    }

    # Sanity check: reject if area is too small
    # Sanity: area should be reasonable relative to ceiling bbox
    ceil_bbox = float(np.ptp(ceil_xy[:, 0]) * np.ptp(ceil_xy[:, 1]))
    if area < max(3.0, ceil_bbox * 0.15):
        print(f'  Ceiling area too small ({area:.1f}m2), skipping')
        return None

    size_kb = os.path.getsize(output_path) / 1024
    print(f'[FloorPlan] Ceiling-based: {len(wall_segments)} walls, '
          f'area={area:.1f}m2 -> {output_path} ({size_kb:.0f} KB)')
    return output_path, metadata


def generate_floorplan(ply_path, output_path=None, ortho_tol=15.0,
                       debug=False, resolution=0.05, height_band='auto',
                       ransac_dist=0.03, ransac_min_inliers=50,
                       min_wall_length=0.3):
    """Full auto floor plan pipeline: detect walls + render dimensioned PNG.

    Automatically selects boundary walls (2 per direction).
    The PLY should already be leveled (floor at Z=0) for best results.

    Returns (png_path, metadata).
    """
    if output_path is None:
        base, _ = os.path.splitext(ply_path)
        output_path = f'{base}_floorplan.png'

    print(f'[FloorPlan] Auto pipeline on {ply_path}...')
    ctx = _prepare_2d_points(ply_path, height_band, ransac_dist,
                             ransac_min_inliers, ortho_tol)

    all_lines = ctx['all_lines']
    print(f'  RANSAC lines after merge: {len(all_lines)}')

    # Auto-select boundary walls (innermost pair per direction)
    all_lines = select_boundary_lines(all_lines)
    print(f'  After boundary selection: {len(all_lines)} lines')

    for i, ln in enumerate(all_lines):
        seg_len = np.linalg.norm(ln['p2'] - ln['p1'])
        conf = ln.get('confidence', 0)
        fit = ln.get('fit_error', 0)
        cov = ln.get('coverage', 0)
        dens = ln.get('density', 0)
        print(f'    L{i}: {seg_len:.2f}m, {ln["inlier_count"]:,} pts, '
              f'fit={fit:.1f}mm, cov={cov:.0f}%, '
              f'density={dens:.0f}pt/m  -> conf={conf:.0f}')

    # Primary: ceiling-based floor plan (ceiling defines the room)
    result = _ceiling_based_floorplan(
        ctx['all_points'], ctx['vertical_axis'], ctx['floor_axes'],
        ctx['ceiling_z'], all_lines, output_path, resolution,
        xy_clip=ctx['xy_full'])

    # Fallback: wall-based approach if ceiling fails
    if result is None:
        print('  Ceiling approach failed, using wall-based fallback')
        result = _build_floorplan_from_lines(
            all_lines, ctx['xy'], ctx['xy_full'], resolution, ortho_tol,
            ransac_dist, min_wall_length, output_path)

    if debug:
        debug_path = output_path.replace('.png', '_debug.png')
        if debug_path == output_path:
            debug_path = output_path + '_debug.png'
        print(f'[FloorPlan] Rendering debug panels -> {debug_path}...')
        render_debug_panels(
            ctx['all_points'], ctx['points'], ctx['xy'],
            all_lines, result[1].get('_verts', None),
            ctx['vertical_axis'], ctx['floor_axes'],
            ctx['band_lo'], ctx['band_hi'], debug_path)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Floor plan generation from LiDAR PLY point clouds.')
    parser.add_argument('command', choices=['level', 'plan', 'auto'],
                        help='level=level only, plan=floor plan only, '
                             'auto=level + floor plan')
    parser.add_argument('input', help='Input PLY file path')
    parser.add_argument('-o', '--output', default=None,
                        help='Output path (default: auto-named)')
    parser.add_argument('--ortho-tol', type=float, default=15.0,
                        help='Angle tolerance for 90-degree snapping '
                             '(default: 15, set to 0 for arbitrary shapes)')
    parser.add_argument('--debug', action='store_true',
                        help='Save debug diagnostic PNG')

    args = parser.parse_args()

    if not os.path.isfile(args.input):
        print(f'Error: {args.input} not found.')
        sys.exit(1)

    if args.command == 'level':
        level_ply(args.input, output_path=args.output)

    elif args.command == 'plan':
        png_path, meta = generate_floorplan(
            args.input, output_path=args.output,
            ortho_tol=args.ortho_tol, debug=args.debug)
        print(f'\nResult: {meta["num_walls"]} walls, '
              f'area={meta["area_m2"]:.1f}m2')
        for i, length in enumerate(meta['wall_lengths']):
            print(f'  Wall {i}: {length:.3f}m')

    elif args.command == 'auto':
        # Level first
        leveled_path = level_ply(args.input)
        print()
        # Then floor plan
        png_path, meta = generate_floorplan(
            leveled_path, output_path=args.output,
            ortho_tol=args.ortho_tol, debug=args.debug)
        print(f'\nResult: {meta["num_walls"]} walls, '
              f'area={meta["area_m2"]:.1f}m2')
        for i, length in enumerate(meta['wall_lengths']):
            print(f'  Wall {i}: {length:.3f}m')


if __name__ == '__main__':
    main()
