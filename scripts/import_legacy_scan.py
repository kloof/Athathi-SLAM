#!/usr/bin/env python3
"""Import a legacy `processed/<name>/` envelope into the new
`projects/<scan_id>/scans/<scan_name>/processed/runs/<run_id>/` layout.

Use this to seed the technician UI with a known-good scan from a previous
Modal run — no Modal call, no recording, pure file manipulation. The
imported scan shows up as `done — review pending` so the technician can
exercise the review tool (delete / merge / class-edit / recapture-with-Brio
/ find-product) against real data without burning credits.

Usage
-----
    python3 scripts/import_legacy_scan.py \\
        --source scan_20260423_230731 \\
        --project-id 48 \\
        --scan-name demo_28_furniture

Then in the SPA: navigate to project 48, open the new scan, see
the floorplan + 28 furniture cards.

To remove: tap "Delete scan" in the SPA, OR
    rm -rf /mnt/slam_data/projects/<id>/scans/<scan_name>/
"""
import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import projects   # noqa: E402


def _resolve_legacy_root():
    """Mirror the resolution logic in app.py — flash drive if mounted,
    else repo-local."""
    if os.path.isdir('/mnt/slam_data'):
        return '/mnt/slam_data/processed'
    return os.path.join(ROOT, 'processed')


def _new_run_id():
    """ISO-shaped run id: YYYYMMDD_HHMMSS UTC."""
    return datetime.utcnow().strftime('%Y%m%d_%H%M%S')


def import_legacy(source_name, project_id, scan_name):
    legacy_root = _resolve_legacy_root()
    src_dir = os.path.join(legacy_root, source_name)
    if not os.path.isdir(src_dir):
        print(f'ERROR: source not found: {src_dir}', file=sys.stderr)
        return 2

    src_result = os.path.join(src_dir, 'result.json')
    if not os.path.isfile(src_result):
        print(f'ERROR: no result.json under {src_dir}', file=sys.stderr)
        return 3

    # Create the project scan dir (and its parents).
    try:
        projects.create_scan(project_id, scan_name)
        print(f'Created scan dir: {projects.scan_dir(project_id, scan_name)}')
    except FileExistsError:
        print(f'Scan dir already exists — appending a new run instead')
    except ValueError as e:
        print(f'ERROR: bad scan name: {e}', file=sys.stderr)
        return 4

    # Touch a placeholder rosbag file so the SPA's `has_rosbag` check
    # returns True. The Pi never reads this file — it just needs to exist
    # so the workspace renders the scan as "recorded" (and then
    # `done_unreviewed` once active_run is set).
    rosbag_dir = projects.scan_rosbag_dir(project_id, scan_name)
    os.makedirs(rosbag_dir, exist_ok=True)
    placeholder = os.path.join(rosbag_dir, 'IMPORTED_FROM_LEGACY')
    with open(placeholder, 'w') as f:
        f.write(f'imported from {src_dir} at {datetime.utcnow().isoformat()}Z\n')

    # Allocate a run dir under the new layout.
    run_id = _new_run_id()
    run_dir = projects.processed_dir_for_run(project_id, scan_name, run_id)
    os.makedirs(run_dir, exist_ok=True)
    print(f'Run dir: {run_dir}')

    # Copy the envelope.
    dst_result = os.path.join(run_dir, 'result.json')
    shutil.copy2(src_result, dst_result)
    print(f'  copied result.json ({os.path.getsize(dst_result)} bytes)')

    # Copy layout_merged.txt if present (cosmetic — referenced by the SPA's
    # "View raw layout" link if we ever wire one up).
    src_layout = os.path.join(src_dir, 'layout_merged.txt')
    if os.path.isfile(src_layout):
        shutil.copy2(src_layout, os.path.join(run_dir, 'layout_merged.txt'))
        print(f'  copied layout_merged.txt')

    # Copy best_views/ (the per-bbox JPEGs).
    src_best = os.path.join(src_dir, 'best_views')
    dst_best = os.path.join(run_dir, 'best_views')
    if os.path.isdir(src_best):
        if os.path.isdir(dst_best):
            shutil.rmtree(dst_best)
        shutil.copytree(src_best, dst_best)
        n = len([x for x in os.listdir(dst_best) if x.endswith('.jpg')])
        print(f'  copied best_views/  ({n} JPEGs)')
    else:
        os.makedirs(dst_best, exist_ok=True)
        print(f'  no best_views/ in source — gallery will be empty')

    # Synthesise meta.json so the Run pill / runs list look right.
    with open(dst_result) as f:
        env = json.load(f)
    meta = {
        'job_id': env.get('job_id', f'imported_{run_id}'),
        'status': 'done',
        'started_at': env.get('submitted_at') or env.get('finished_at'),
        'finished_at': env.get('finished_at'),
        'duration_s': env.get('metrics', {}).get('total_duration_s'),
        'imported_from': src_dir,
        'imported_at': datetime.utcnow().isoformat() + 'Z',
        'note': 'imported from legacy processed/ tree — no Modal call',
    }
    with open(os.path.join(run_dir, 'meta.json'), 'w') as f:
        json.dump(meta, f, indent=2)
    print(f'  wrote meta.json')

    # Set this as the active run.
    projects.set_active_run(project_id, scan_name, run_id)
    print(f'  set active_run -> {run_id}')

    print()
    print(f'Done. Project {project_id} → scans/{scan_name} now points at')
    print(f'  {run_dir}')
    print(f'Reload the SPA and look in project #{project_id} for the new scan.')
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--source', required=True,
        help='legacy processed/<name>/ folder (e.g. scan_20260423_230731)')
    p.add_argument('--project-id', required=True, type=int,
        help='target project scan_id (must already exist in projects/)')
    p.add_argument('--scan-name', required=True,
        help='target scan name under that project (snake_case)')
    args = p.parse_args()
    sys.exit(import_legacy(args.source, args.project_id, args.scan_name))


if __name__ == '__main__':
    main()
