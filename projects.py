"""Per-project filesystem + manifest helpers.

Plan §2 (filesystem layout), §6a (manifest schema), §22d (helper functions)
and §0 (terminology: project = scan_id, scan = one room recording, run =
one Modal run).

Canonical layout:

    <PROJECTS_ROOT>/<scan_id>/
    +- manifest.json
    +- settings.json
    +- scans/<scan_name>/
       +- rosbag/                          (created lazily by recording)
       +- processed/active_run.json        (managed by step 5)
          runs/<run_id>/...                (managed by step 5)

`PROJECTS_ROOT` is resolved at import time the same way `RECORDINGS_DIR`
and `PROCESSED_DIR` are in `app.py`:
  1. `<repo>/projects` when `/mnt/slam_data` is NOT a directory (dev / CI).
  2. `/mnt/slam_data/projects` when it is.

Tests can override the resolution by either:
  - setting `PROJECTS_ROOT_OVERRIDE` in the env BEFORE importing this module,
    or
  - assigning `projects.PROJECTS_ROOT = '...'` at runtime (the module reads
    it via the module-level name on every call, so monkeypatching works).

This module performs zero network I/O and never touches the recording /
Modal subsystems. It is purely additive — see plan §-1 hard constraints.
"""

import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _resolve_projects_root():
    """Pick the directory we keep all per-project state in.

    Resolution order (first match wins):
      1. PROJECTS_ROOT_OVERRIDE env var (used by tests).
      2. /mnt/slam_data/projects  (when /mnt/slam_data exists as a dir).
      3. <repo>/projects          (dev / CI fallback).
    """
    override = os.environ.get('PROJECTS_ROOT_OVERRIDE')
    if override:
        return override
    if os.path.isdir('/mnt/slam_data'):
        return '/mnt/slam_data/projects'
    return os.path.join(_SCRIPT_DIR, 'projects')


# Cached at import time; tests can monkeypatch the module attribute directly.
PROJECTS_ROOT = _resolve_projects_root()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso():
    """ISO-8601 UTC timestamp suitable for manifest fields."""
    return datetime.now(tz=timezone.utc).isoformat().replace('+00:00', 'Z')


def _ensure_dir(path, mode=0o755):
    """Create `path` (mkdir -p). Best-effort chmod after."""
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _atomic_write_json(path, payload, mode=0o644):
    """Write JSON atomically: temp file in the same dir, then `os.replace`.

    Same dir is important because `os.replace` across filesystems isn't atomic.
    The chmod runs after rename so the final file ends up with the requested
    permissions even if the umask dropped them on the temp file.
    """
    parent = os.path.dirname(path) or '.'
    _ensure_dir(parent)
    fd, tmp = tempfile.mkstemp(prefix='.tmp.', dir=parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(payload, f, indent=2)
            f.write('\n')
        os.replace(tmp, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json_or_none(path):
    """Return parsed JSON dict, or None on missing / unreadable / unparseable."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data


# ---------------------------------------------------------------------------
# Path helpers (no mkdir)
# ---------------------------------------------------------------------------

def project_dir(scan_id):
    """`<PROJECTS_ROOT>/<scan_id>/`. Does NOT create."""
    # Look up PROJECTS_ROOT via the module so tests can monkeypatch it.
    return os.path.join(sys.modules[__name__].PROJECTS_ROOT, str(int(scan_id)))


def scan_dir(scan_id, scan_name):
    """`<PROJECTS_ROOT>/<scan_id>/scans/<scan_name>/`. Does NOT create."""
    return os.path.join(project_dir(scan_id), 'scans', str(scan_name))


def manifest_path(scan_id):
    return os.path.join(project_dir(scan_id), 'manifest.json')


def project_settings_path(scan_id):
    return os.path.join(project_dir(scan_id), 'settings.json')


def _active_run_path(scan_id, scan_name):
    return os.path.join(scan_dir(scan_id, scan_name), 'processed', 'active_run.json')


# ---------------------------------------------------------------------------
# Step 4 helpers (additive — see plan §22d)
#
# These wrap the canonical scan-scoped layout for the recording + Modal
# subsystems. Step 5 will refine sub-run versioning; for now we expose a
# minimal API: one active run per scan, identified by a UTC timestamp string.
# ---------------------------------------------------------------------------

# Snake_case scan name regex. Anchored, 1-40 chars, lowercase a-z / 0-9 / _.
# `runs` is a reserved name (collides with the runs/ subdirectory below
# processed/). `processed`, `rosbag`, `.` and `..` would also produce broken
# layouts; reject them up front.
_SCAN_NAME_RE = re.compile(r'^[a-z0-9_]+$')
_RESERVED_SCAN_NAMES = frozenset({
    'runs', 'processed', 'rosbag', 'review', 'meta',
    '.', '..',
})


def _validate_scan_name(scan_name):
    """Raise ValueError on a name we won't accept on disk.

    Rules:
      - 1..40 chars
      - Matches ^[a-z0-9_]+$ (lowercase, digits, underscore)
      - Not in `_RESERVED_SCAN_NAMES`
      - No leading underscore (cosmetic — keeps hidden-style names off the FS)

    Returns the normalised name (currently unchanged — we don't lowercase
    automatically; the technician must commit to a snake_case name).
    """
    if not isinstance(scan_name, str):
        raise ValueError('scan name must be a string')
    s = scan_name.strip()
    if not s:
        raise ValueError('scan name must not be empty')
    if len(s) > 40:
        raise ValueError('scan name must be 1..40 chars')
    if not _SCAN_NAME_RE.match(s):
        raise ValueError(
            'scan name must be lowercase snake_case (a-z, 0-9, _)'
        )
    if s.startswith('_'):
        raise ValueError('scan name must not start with an underscore')
    if s in _RESERVED_SCAN_NAMES:
        raise ValueError(f'scan name {s!r} is reserved')
    return s


def scan_processed_root(scan_id, scan_name):
    """`<scan_dir>/processed/`. Does NOT create."""
    return os.path.join(scan_dir(scan_id, scan_name), 'processed')


def scan_rosbag_dir(scan_id, scan_name):
    """`<scan_dir>/rosbag/`. Does NOT create."""
    return os.path.join(scan_dir(scan_id, scan_name), 'rosbag')


def runs_dir(scan_id, scan_name):
    """`<scan_dir>/processed/runs/`. Does NOT create."""
    return os.path.join(scan_processed_root(scan_id, scan_name), 'runs')


def active_run_path(scan_id, scan_name):
    """`<scan_dir>/processed/active_run.json`. Does NOT create."""
    return _active_run_path(scan_id, scan_name)


def processed_dir_for_run(scan_id, scan_name, run_id):
    """`<scan_dir>/processed/runs/<run_id>/`. Does NOT create."""
    if not isinstance(run_id, str) or not run_id:
        raise ValueError('run_id must be a non-empty string')
    return os.path.join(runs_dir(scan_id, scan_name), run_id)


def read_active_run(scan_id, scan_name):
    """Return the active run id, or None if missing / unparseable."""
    data = _read_json_or_none(active_run_path(scan_id, scan_name))
    if not isinstance(data, dict):
        return None
    rid = data.get('active_run_id')
    if isinstance(rid, str) and rid:
        return rid
    return None


def set_active_run(scan_id, scan_name, run_id):
    """Atomic-write the active-run pointer. Mkdir parent if missing."""
    if not isinstance(run_id, str) or not run_id:
        raise ValueError('run_id must be a non-empty string')
    _atomic_write_json(
        active_run_path(scan_id, scan_name),
        {'active_run_id': run_id},
        mode=0o644,
    )


def new_run_id(now=None):
    """Format: `YYYYMMDD_HHMMSS` UTC.

    Caller is responsible for collision-resolving with the runs/ directory.
    See `allocate_run_id` for the disk-aware variant.
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)
    # Strip tz info so strftime doesn't include offset noise.
    return now.strftime('%Y%m%d_%H%M%S')


def allocate_run_id(scan_id, scan_name, now=None):
    """Pick a fresh run_id, suffixing with _2/_3/... on disk collision.

    Walks the runs/ dir to find a non-colliding name. Returns the chosen id
    (does NOT create the directory).
    """
    base = new_run_id(now=now)
    rdir = runs_dir(scan_id, scan_name)
    candidate = base
    suffix = 2
    while os.path.isdir(os.path.join(rdir, candidate)):
        candidate = f'{base}_{suffix}'
        suffix += 1
    return candidate


def create_scan(scan_id, scan_name):
    """Create a scan subdir under an existing project. Idempotent only on
    the project itself; a duplicate scan name raises FileExistsError so the
    caller can prompt the technician.

    Layout produced:
        <scan_dir>/rosbag/
        <scan_dir>/processed/

    Returns the per-scan summary `{name, has_rosbag, active_run_id}`.
    Does NOT touch `manifest.json` — the project must already exist.

    Raises:
        ValueError on a malformed name (see `_validate_scan_name`).
        FileExistsError if `<scan_dir>` already exists.
    """
    sid = int(scan_id)
    name = _validate_scan_name(scan_name)
    sd = scan_dir(sid, name)
    if os.path.exists(sd):
        raise FileExistsError(sd)
    _ensure_dir(sd)
    _ensure_dir(scan_rosbag_dir(sid, name))
    _ensure_dir(scan_processed_root(sid, name))
    return {
        'name': name,
        'has_rosbag': False,
        'active_run_id': None,
    }


def delete_scan(scan_id, scan_name):
    """Remove `<scan_dir>/` and everything under it.

    Caller is responsible for guarding against in-progress recordings; this
    function does NOT consult the recording subsystem (would couple
    `projects.py` to Flask state). The Flask route checks `_is_recording()`
    before delegating here.

    Idempotent: calling on an already-missing scan is a no-op.
    """
    sid = int(scan_id)
    # Validate the name shape so a stray `..` can't escape the project tree
    # even via an ill-formed call. Wrap the validator so the no-op semantic
    # for a missing directory still holds for a never-created legitimate name.
    name = _validate_scan_name(scan_name)
    sd = scan_dir(sid, name)
    if not os.path.isdir(sd):
        return
    shutil.rmtree(sd, ignore_errors=False)


# ---------------------------------------------------------------------------
# Field extraction (heuristic mapping from unknown-shape Athathi item)
# ---------------------------------------------------------------------------

def field_extract(item, *candidates):
    """Try each candidate key in order; first present non-empty value wins.

    `item` can be any dict (the Athathi schedule shape isn't pinned — see
    §23a). `""` is treated as "not present" so a server placeholder doesn't
    overwrite a real value. Returns None when nothing matches.
    """
    if not isinstance(item, dict):
        return None
    for key in candidates:
        if key in item:
            v = item[key]
            if v is None:
                continue
            if isinstance(v, str) and not v.strip():
                continue
            return v
    return None


def _project_named_fields(athathi_meta):
    """Extract the four named manifest fields from a schedule/history item.

    Returns a dict with `customer_name`, `slot_start`, `slot_end`, `address`.
    Anything that doesn't match is left for the verbatim `athathi_meta` copy.
    """
    return {
        'customer_name': field_extract(
            athathi_meta, 'customer_name', 'customerName', 'customer', 'name',
        ),
        'slot_start': field_extract(
            athathi_meta, 'slot_start', 'slotStart', 'start', 'starts_at',
        ),
        'slot_end': field_extract(
            athathi_meta, 'slot_end', 'slotEnd', 'end', 'ends_at',
        ),
        'address': field_extract(
            athathi_meta, 'address', 'location', 'addr',
        ),
    }


# ---------------------------------------------------------------------------
# Manifest read / write
# ---------------------------------------------------------------------------

def read_manifest(scan_id):
    """Return the parsed manifest dict, or None if missing / unparseable."""
    return _read_json_or_none(manifest_path(scan_id))


def write_manifest(scan_id, manifest):
    """Atomic temp+rename, mode 0644. Mkdir parent if missing."""
    if not isinstance(manifest, dict):
        raise TypeError('manifest must be a dict')
    _atomic_write_json(manifest_path(scan_id), manifest, mode=0o644)


# ---------------------------------------------------------------------------
# Project creation / merge
# ---------------------------------------------------------------------------

def ensure_project(scan_id, athathi_meta=None):
    """Create the project dir + scans/ + settings.json + manifest.json.

    Returns the (possibly newly-created) manifest dict.

    If `athathi_meta` is provided, the four named manifest fields
    (customer_name / slot_start / slot_end / address) are best-effort
    extracted from it, and the verbatim dict is mirrored into
    `manifest.athathi_meta`.

    Idempotent: a second call with `athathi_meta=None` (or a sparser meta)
    does NOT clobber technician-set or previously-extracted fields.

    Merge rules when the manifest already exists and `athathi_meta` is given:
      - `manifest.athathi_meta` is updated to the union of old + new (new
        keys win; missing keys preserved).
      - For each named field, only fill it in when the on-disk value is
        currently None / empty AND the new extraction yields something.
    """
    sid = int(scan_id)
    pdir = project_dir(sid)
    sdir = os.path.join(pdir, 'scans')
    _ensure_dir(pdir)
    _ensure_dir(sdir)

    # Settings stub (don't clobber if it exists).
    settings = project_settings_path(sid)
    if not os.path.isfile(settings):
        _atomic_write_json(settings, {}, mode=0o644)

    existing = read_manifest(sid)
    extracted = _project_named_fields(athathi_meta) if athathi_meta else {
        'customer_name': None, 'slot_start': None, 'slot_end': None, 'address': None,
    }

    if existing is None:
        manifest = {
            'scan_id': sid,
            'customer_name': extracted.get('customer_name'),
            'slot_start':    extracted.get('slot_start'),
            'slot_end':      extracted.get('slot_end'),
            'address':       extracted.get('address'),
            'athathi_meta':  dict(athathi_meta) if isinstance(athathi_meta, dict) else {},
            'created_at':    _now_iso(),
            'completed_at':  None,
            'submitted_at':  None,
            'post_submit_hook_status': None,
        }
        write_manifest(sid, manifest)
        return manifest

    # Existing manifest — merge richer athathi_meta in without clobbering.
    changed = False
    manifest = dict(existing)
    # Always make sure scan_id is present + matches (defensive — older builds
    # may have written one without it).
    if manifest.get('scan_id') != sid:
        manifest['scan_id'] = sid
        changed = True

    if isinstance(athathi_meta, dict) and athathi_meta:
        cur_meta = manifest.get('athathi_meta')
        if not isinstance(cur_meta, dict):
            cur_meta = {}
        merged_meta = dict(cur_meta)
        for k, v in athathi_meta.items():
            # New keys always added; existing keys only filled in when blank.
            if k not in merged_meta:
                merged_meta[k] = v
            else:
                cur = merged_meta[k]
                if cur is None or (isinstance(cur, str) and not cur.strip()):
                    merged_meta[k] = v
        if merged_meta != cur_meta:
            manifest['athathi_meta'] = merged_meta
            changed = True

        # Named fields — only fill in when missing.
        for field in ('customer_name', 'slot_start', 'slot_end', 'address'):
            cur = manifest.get(field)
            if cur is None or (isinstance(cur, str) and not cur.strip()):
                new = extracted.get(field)
                if new is not None and not (isinstance(new, str) and not new.strip()):
                    manifest[field] = new
                    changed = True

    # Backfill any missing schema keys (e.g. older manifests from before this
    # field set was finalised). Don't overwrite existing values.
    for key, default in (
        ('customer_name', None),
        ('slot_start', None),
        ('slot_end', None),
        ('address', None),
        ('athathi_meta', {}),
        ('created_at', _now_iso()),
        ('completed_at', None),
        ('submitted_at', None),
        ('post_submit_hook_status', None),
    ):
        if key not in manifest:
            manifest[key] = default
            changed = True

    if changed:
        write_manifest(sid, manifest)
    return manifest


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------

def _list_scan_names(scan_id):
    """Return sorted scan-name directories under `<project>/scans/`."""
    sdir = os.path.join(project_dir(scan_id), 'scans')
    if not os.path.isdir(sdir):
        return []
    out = []
    for name in os.listdir(sdir):
        if name.startswith('.'):
            continue
        full = os.path.join(sdir, name)
        if os.path.isdir(full):
            out.append(name)
    out.sort()
    return out


def list_scans(scan_id):
    """Return per-scan summary dicts for one project.

    Shape: `{name, has_rosbag, active_run_id, reviewed}`.

    `reviewed` is always False for now — step 5 will refine it once
    review.json exists.
    """
    sid = int(scan_id)
    out = []
    for name in _list_scan_names(sid):
        sd = scan_dir(sid, name)
        rosbag_dir = os.path.join(sd, 'rosbag')
        has_rosbag = False
        if os.path.isdir(rosbag_dir):
            try:
                # Any non-hidden entry (file or subdir) means a recording
                # exists. We avoid listing the contents to keep this O(1) on
                # large bag dirs.
                for entry in os.listdir(rosbag_dir):
                    if not entry.startswith('.'):
                        has_rosbag = True
                        break
            except OSError:
                has_rosbag = False

        active_run_id = None
        ar_data = _read_json_or_none(_active_run_path(sid, name))
        if isinstance(ar_data, dict):
            ar = ar_data.get('active_run_id')
            if isinstance(ar, str) and ar:
                active_run_id = ar

        # A scan is reviewed iff its active run has a review.json with
        # `reviewed_at` stamped. Without this the SPA's Submit button is
        # permanently disabled.
        reviewed = False
        if active_run_id:
            rv = _read_json_or_none(
                os.path.join(
                    processed_dir_for_run(sid, name, active_run_id),
                    'review.json',
                )
            )
            if isinstance(rv, dict) and rv.get('reviewed_at'):
                reviewed = True

        out.append({
            'name': name,
            'has_rosbag': has_rosbag,
            'active_run_id': active_run_id,
            'reviewed': reviewed,
        })
    return out


def list_projects():
    """Walk PROJECTS_ROOT and return manifest dicts in id-descending order.

    Each entry is augmented with computed:
      - `rooms_local`:    int — number of scan dirs on disk.
      - `rooms_reviewed`: int — number of scans whose `reviewed` is True.
      - `submitted`:      bool — manifest.submitted_at is non-null.

    Skips entries where the manifest is missing or unparseable. Skips entries
    whose folder name isn't an integer.
    """
    root = sys.modules[__name__].PROJECTS_ROOT
    if not os.path.isdir(root):
        return []

    out = []
    for entry in os.listdir(root):
        full = os.path.join(root, entry)
        if not os.path.isdir(full):
            continue
        try:
            sid = int(entry)
        except (TypeError, ValueError):
            continue
        manifest = read_manifest(sid)
        if manifest is None:
            continue
        scans = list_scans(sid)
        rooms_reviewed = sum(1 for s in scans if s.get('reviewed'))
        augmented = dict(manifest)
        augmented['rooms_local'] = len(scans)
        augmented['rooms_reviewed'] = rooms_reviewed
        augmented['submitted'] = bool(manifest.get('submitted_at'))
        out.append(augmented)

    # Descending by scan_id (newest assignments first).
    out.sort(key=lambda m: int(m.get('scan_id') or 0), reverse=True)
    return out
