"""Technician review schema, render, merge math, carry-over and upload filter.

Pure functions (no Flask). All file I/O routed through `os.path.join`; no
hardcoded slashes. The Flask routes live in `app.py` and call into this
module — see plan §16 step 5 + §6 + §19b + §21.

Public surface:

    REVIEW_FILENAME, REVIEWED_OUT_FILENAME, UPLOAD_OUT_FILENAME
    DEFAULT_UPLOAD_FILTER

    STATUS_KEPT, STATUS_DELETED, STATUS_MERGED_INTO, STATUS_UNTOUCHED

    read_review(run_dir)                   -> dict | None
    write_review(run_dir, review)          -> None
    initial_review(scan_id, scan_name, result)             -> dict

    set_bbox_status(review, bbox_id, status, **fields)     -> dict
    set_class_override(review, bbox_id, class_name)        -> dict
    set_image_override(review, bbox_id, relative_path)     -> dict
    set_linked_product(review, bbox_id, product_or_none)   -> dict
    set_notes(review, text)                                -> dict
    mark_reviewed(review)                                  -> dict

    merge_bboxes(result, primary_id, member_ids, chosen_class=None) -> dict

    carry_over_review(old_review, old_result, new_result,
                      max_dist_m=0.3, scale_tolerance=0.25
                     ) -> (new_review, warnings)

    render_reviewed(result, review)         -> dict
    apply_upload_filter(envelope, filter)   -> dict

    render_for_run(run_dir)                 -> (reviewed_envelope, upload_envelope)

    load_filter()  -> dict
    save_filter(f) -> None

This module performs zero network I/O and never touches the recording or
Modal subsystems (plan §-1 hard constraints).
"""

import copy
import json
import os
import sys
import tempfile
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REVIEW_FILENAME = 'review.json'
REVIEWED_OUT_FILENAME = 'result_reviewed.json'
UPLOAD_OUT_FILENAME = 'result_for_upload.json'

# Status enum (just strings).
STATUS_KEPT = 'kept'
STATUS_DELETED = 'deleted'
STATUS_MERGED_INTO = 'merged_into'
STATUS_UNTOUCHED = 'untouched'

_VALID_STATUSES = frozenset({
    STATUS_KEPT, STATUS_DELETED, STATUS_MERGED_INTO, STATUS_UNTOUCHED,
})


# Default upload filter — verbatim from plan §21a.
DEFAULT_UPLOAD_FILTER = {
    "version": 1,
    "include_paths": [
        "schema_version",
        "scan_id",
        "room_name",
        "submitted_at",
        "submitted_by",

        "floorplan.walls[*].id",
        "floorplan.walls[*].start",
        "floorplan.walls[*].end",
        "floorplan.walls[*].height",
        "floorplan.walls[*].thickness",

        "floorplan.doors[*].id",
        "floorplan.doors[*].wall",
        "floorplan.doors[*].center",
        "floorplan.doors[*].width",
        "floorplan.doors[*].height",

        "floorplan.windows[*]",

        "furniture[*].id",
        "furniture[*].class",
        "furniture[*].center",
        "furniture[*].size",
        "furniture[*].yaw",
        "furniture[*].linked_product.id",
        "furniture[*].linked_product.name",
        "furniture[*].linked_product.price",
        "furniture[*].linked_product.thumbnail_url",
        "furniture[*].linked_product.model_url",
        "furniture[*].linked_product.model_usdz",
        "furniture[*].linked_product.width",
        "furniture[*].linked_product.height",
        "furniture[*].linked_product.depth",
        "furniture[*].linked_product.category",
        "furniture[*].linked_product.store_name",
        "furniture[*].linked_product.similarity",

        "best_images[*].bbox_id",
        "best_images[*].class",
        "best_images[*].camera_distance_m",
        "best_images[*].local_path",
        "best_images[*].image_source",

        "review_meta.bbox_count_original",
        "review_meta.bbox_count_reviewed",
        "review_meta.merged_count",
        "review_meta.deleted_count",
        "review_meta.recaptured_count",
        "review_meta.notes",
    ],
    "exclude_paths": [
        "metrics",
        "artifacts",
        "best_images[*].pixel_aabb",
        "best_images[*].frame_timestamp_ns",
        "best_images[*].url",
        "furniture[*].linked_product._raw",
    ],
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _now_iso():
    """ISO-8601 UTC timestamp suitable for review.json fields."""
    return datetime.now(tz=timezone.utc).isoformat().replace('+00:00', 'Z')


def _ensure_dir(path, mode=0o755):
    """`mkdir -p` with best-effort chmod (silent on failure)."""
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _atomic_write_json(path, payload, mode=0o644):
    """Atomic temp-file + rename, then chmod. Same dir for replace atomicity."""
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
    """Parsed JSON dict, or None on missing / unreadable / unparseable."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Review.json read / write
# ---------------------------------------------------------------------------

def review_path(run_dir):
    """Path to the review.json inside `run_dir`."""
    return os.path.join(run_dir, REVIEW_FILENAME)


def reviewed_out_path(run_dir):
    """Path to result_reviewed.json inside `run_dir`."""
    return os.path.join(run_dir, REVIEWED_OUT_FILENAME)


def upload_out_path(run_dir):
    """Path to result_for_upload.json inside `run_dir`."""
    return os.path.join(run_dir, UPLOAD_OUT_FILENAME)


def read_review(run_dir):
    """Return the parsed review dict, or None if missing / unparseable."""
    return _read_json_or_none(review_path(run_dir))


def write_review(run_dir, review):
    """Atomic temp+rename to `<run_dir>/review.json`, mode 0644.

    `run_dir` must exist; we don't create it (the run dir is owned by the
    Modal worker). We do, however, create the file's parent if missing —
    a defensive belt-and-braces for the unusual case where the run dir
    was deleted between read and write.
    """
    if not isinstance(review, dict):
        raise TypeError('review must be a dict')
    _atomic_write_json(review_path(run_dir), review, mode=0o644)


# ---------------------------------------------------------------------------
# initial_review — schema seed
# ---------------------------------------------------------------------------

def _bbox_ids_from_result(result):
    """Yield bbox_ids found in result.furniture, in input order.

    Tolerant: a furniture entry without an `id` key is skipped.
    """
    if not isinstance(result, dict):
        return
    furniture = result.get('furniture')
    if not isinstance(furniture, list):
        return
    for item in furniture:
        if not isinstance(item, dict):
            continue
        bid = item.get('id')
        if isinstance(bid, str) and bid:
            yield bid


def initial_review(scan_id, scan_name, result):
    """Build a fresh review.json dict from a result envelope.

    Schema per plan §6b verbatim:
      { scan_id, room_name, result_job_id, started_at, reviewed_at,
        submitted_at, version: 1,
        bboxes: { <id>: {status: STATUS_UNTOUCHED}, ... },
        notes: "" }
    """
    job_id = None
    if isinstance(result, dict):
        jid = result.get('job_id')
        if isinstance(jid, str) and jid:
            job_id = jid

    bboxes = {}
    for bid in _bbox_ids_from_result(result):
        bboxes[bid] = {'status': STATUS_UNTOUCHED}

    return {
        'scan_id': int(scan_id) if scan_id is not None else None,
        'room_name': str(scan_name) if scan_name is not None else None,
        'result_job_id': job_id,
        'started_at': _now_iso(),
        'reviewed_at': None,
        'submitted_at': None,
        'version': 1,
        'bboxes': bboxes,
        'notes': '',
    }


# ---------------------------------------------------------------------------
# Bbox state mutations (idempotent, return new dict)
# ---------------------------------------------------------------------------

def _ensure_bboxes(review):
    """Return a copy of `review` with a guaranteed `bboxes` dict.

    Never mutates the caller's dict (review.py is value-stable).
    """
    out = dict(review) if isinstance(review, dict) else {}
    bxs = out.get('bboxes')
    if not isinstance(bxs, dict):
        bxs = {}
    out['bboxes'] = dict(bxs)
    return out


def set_bbox_status(review, bbox_id, status, **fields):
    """Set `bboxes[bbox_id].status = status` and merge any extra fields.

    Idempotent — calling twice with the same payload yields the same dict.
    Validates the status against the enum.
    Returns a new review dict (does NOT mutate the input).
    """
    if status not in _VALID_STATUSES:
        raise ValueError(
            f'invalid status {status!r}; allowed: {sorted(_VALID_STATUSES)}'
        )
    if not isinstance(bbox_id, str) or not bbox_id:
        raise ValueError('bbox_id must be a non-empty string')

    out = _ensure_bboxes(review)
    cur = dict(out['bboxes'].get(bbox_id) or {})
    cur['status'] = status
    for k, v in fields.items():
        cur[k] = v
    out['bboxes'][bbox_id] = cur
    return out


def set_class_override(review, bbox_id, class_name):
    """Set `class_override` on a bbox; preserves existing status.

    Empty / non-string class names are rejected (plan §14: "Class override
    is empty string → Reject").
    """
    if not isinstance(class_name, str) or not class_name.strip():
        raise ValueError('class_name must be a non-empty string')
    if not isinstance(bbox_id, str) or not bbox_id:
        raise ValueError('bbox_id must be a non-empty string')

    out = _ensure_bboxes(review)
    cur = dict(out['bboxes'].get(bbox_id) or {})
    if 'status' not in cur:
        cur['status'] = STATUS_KEPT
    cur['class_override'] = class_name.strip()
    out['bboxes'][bbox_id] = cur
    return out


def set_image_override(review, bbox_id, relative_path):
    """Set `image_override` on a bbox; preserves existing status."""
    if not isinstance(relative_path, str) or not relative_path:
        raise ValueError('relative_path must be a non-empty string')
    if not isinstance(bbox_id, str) or not bbox_id:
        raise ValueError('bbox_id must be a non-empty string')

    out = _ensure_bboxes(review)
    cur = dict(out['bboxes'].get(bbox_id) or {})
    if 'status' not in cur:
        cur['status'] = STATUS_KEPT
    cur['image_override'] = relative_path
    out['bboxes'][bbox_id] = cur
    return out


def set_linked_product(review, bbox_id, product):
    """Attach (or detach, when `product` is None) a linked product.

    `product=None` means "no match" — sets `linked_product=null` and adds
    `search_attempted: True` plus `search_attempted_at` (plan §20c).
    """
    if not isinstance(bbox_id, str) or not bbox_id:
        raise ValueError('bbox_id must be a non-empty string')

    out = _ensure_bboxes(review)
    cur = dict(out['bboxes'].get(bbox_id) or {})
    if 'status' not in cur:
        cur['status'] = STATUS_KEPT
    if product is None:
        cur['linked_product'] = None
        cur['search_attempted'] = True
        cur['search_attempted_at'] = _now_iso()
    else:
        if not isinstance(product, dict):
            raise TypeError('product must be a dict or None')
        cur['linked_product'] = dict(product)
        cur['search_attempted'] = True
        cur['search_attempted_at'] = _now_iso()
    out['bboxes'][bbox_id] = cur
    return out


def set_notes(review, text):
    """Replace the free-text notes field."""
    if text is None:
        text = ''
    if not isinstance(text, str):
        raise TypeError('notes must be a string')
    out = _ensure_bboxes(review)
    out['notes'] = text
    return out


def mark_reviewed(review):
    """Stamp `reviewed_at = now()`. Idempotent — overwrites on each call."""
    out = _ensure_bboxes(review)
    out['reviewed_at'] = _now_iso()
    return out


# ---------------------------------------------------------------------------
# Merge math (AABB union)
# ---------------------------------------------------------------------------

def _furniture_index(result):
    """Map id -> furniture dict for fast lookup."""
    if not isinstance(result, dict):
        return {}
    out = {}
    for f in result.get('furniture') or []:
        if isinstance(f, dict):
            fid = f.get('id')
            if isinstance(fid, str):
                out[fid] = f
    return out


def _aabb_extents(box):
    """Return (lo[3], hi[3]) extents of a furniture box.

    Honours center + size as 3-element lists. Missing pieces fall back to
    [0,0,0]; the caller is responsible for not feeding garbage in.
    """
    center = box.get('center') if isinstance(box, dict) else None
    size = box.get('size') if isinstance(box, dict) else None
    if not isinstance(center, list) or len(center) < 3:
        center = [0.0, 0.0, 0.0]
    if not isinstance(size, list) or len(size) < 3:
        size = [0.0, 0.0, 0.0]
    lo = [float(center[i]) - float(size[i]) / 2.0 for i in range(3)]
    hi = [float(center[i]) + float(size[i]) / 2.0 for i in range(3)]
    return lo, hi


def merge_bboxes(result, primary_id, member_ids, chosen_class=None,
                 existing_review=None):
    """Compute the review-state delta for merging `member_ids` into `primary_id`.

    Returns a dict suitable for merging into review.bboxes:

        { primary_id: { status: STATUS_KEPT, merged_from: [...],
                        class_override: chosen_class? },
          <each member>: { status: STATUS_MERGED_INTO, target: primary_id } }

    Math (plan §6c step 4):
      - All members + primary contribute (center ± size/2) box extents.
      - New center = midpoint of union extents.
      - New size = (max - min) per axis.
      - yaw retained verbatim from primary (members' yaw IGNORED).
      - chosen_class None → defaults to primary's class.

    NOTE: This function does NOT compute a new center/size for review.json
    — the merged geometry is rebuilt at render-time by `render_reviewed`
    from `result + review.merged_from` so it stays a pure function of the
    inputs (re-running with a different result envelope re-derives). This
    function is responsible only for the review-state mutation surface.

    `existing_review` (optional): the current review dict (or just its
    `bboxes` map). When provided, this function rejects re-merges where a
    requested member is ALREADY merged into a DIFFERENT primary — without
    that guard, the route would silently corrupt the prior primary's
    `merged_from` list. If a member is already merged into the same
    primary the request is idempotent and the member is simply omitted
    from the returned delta (so the primary's `merged_from` is not
    duplicated when the route folds it in).

    Returns: a new bbox-state dict (never mutates `result`).

    Raises:
        ValueError if `primary_id` not in result.furniture or `member_ids`
        contains an unknown id, or member_ids is empty, or member_ids
        contains primary_id (a self-merge), or a member is already merged
        into a different primary (with `existing_review` set).
    """
    if not isinstance(primary_id, str) or not primary_id:
        raise ValueError('primary_id must be a non-empty string')
    if not isinstance(member_ids, list) or not member_ids:
        raise ValueError('member_ids must be a non-empty list')
    if primary_id in member_ids:
        raise ValueError('primary_id must not appear in member_ids')

    idx = _furniture_index(result)
    if primary_id not in idx:
        raise ValueError(f'primary_id {primary_id!r} not in result.furniture')
    for mid in member_ids:
        if not isinstance(mid, str) or not mid:
            raise ValueError('member_ids entries must be non-empty strings')
        if mid not in idx:
            raise ValueError(
                f'member_id {mid!r} not in result.furniture'
            )

    # Conflict check against any prior merge state. Accepts either a full
    # review dict (with a `bboxes` key) OR a bare bboxes map; the route
    # passes the latter for clarity.
    existing_bxs = {}
    if isinstance(existing_review, dict):
        if 'bboxes' in existing_review and isinstance(
                existing_review.get('bboxes'), dict):
            existing_bxs = existing_review['bboxes']
        else:
            existing_bxs = existing_review
    # Members that are already merged into THIS primary are no-ops: omit
    # them from the delta so the route doesn't duplicate them in
    # `merged_from`.
    idempotent_skips = set()
    for mid in member_ids:
        entry = existing_bxs.get(mid)
        if not isinstance(entry, dict):
            continue
        if entry.get('status') != STATUS_MERGED_INTO:
            continue
        prior_target = entry.get('target')
        if prior_target == primary_id:
            idempotent_skips.add(mid)
            continue
        # Already merged into a DIFFERENT primary — refuse.
        raise ValueError(
            f'{mid} is already merged into {prior_target}; '
            f'unmerge it first'
        )

    if chosen_class is None:
        cls = idx[primary_id].get('class')
    else:
        if not isinstance(chosen_class, str) or not chosen_class.strip():
            raise ValueError('chosen_class must be a non-empty string when set')
        cls = chosen_class.strip()

    # Build the bbox-state delta. Render-time picks up the geometry math;
    # we simply record the merge intention here. Members that were already
    # merged into THIS primary are skipped — the primary's existing
    # `merged_from` already contains them.
    effective_members = [m for m in member_ids if m not in idempotent_skips]

    # Auto-pick the best image among (primary + effective members) by
    # `camera_distance_m` — closer = sharper, less perspective distortion.
    # Only sets `image_override` if a member's photo wins; if the primary
    # was already closest, no override is added (its existing JPEG wins).
    # Skipped entirely when the primary already has an `image_override`
    # the technician set manually (recapture / explicit override).
    primary_existing = (existing_bxs.get(primary_id) or {}) if existing_bxs else {}
    candidate_ids = [primary_id] + effective_members
    best_id, best_idx, best_dist = _pick_best_image(result, candidate_ids)

    primary_delta = {
        'status': STATUS_KEPT,
        'merged_from': list(effective_members),
        'class_override': cls,
    }
    # Auto-pick is sticky ONLY when the technician explicitly recaptured.
    # A prior auto-pick override (`best_views/<idx>.jpg`) gets recomputed on
    # every merge so adding a new closer member updates the picture.
    prior_override = primary_existing.get('image_override')
    has_recapture = (isinstance(prior_override, str)
                     and prior_override.endswith('_recapture.jpg'))
    if (best_id is not None
            and best_id != primary_id
            and best_idx is not None
            and not has_recapture):
        primary_delta['image_override'] = f'best_views/{best_idx}.jpg'

    delta = {primary_id: primary_delta}
    for mid in effective_members:
        delta[mid] = {
            'status': STATUS_MERGED_INTO,
            'target': primary_id,
        }
    return delta


def _pick_best_image(result, bbox_ids):
    """Return (bbox_id, best_image_idx, distance_m) for the bbox with the
    smallest `camera_distance_m` in `result.best_images[]`. Ties broken
    alphabetically on bbox_id for determinism.

    Returns (None, None, None) if none of the candidates have a best_image
    entry with a parseable distance.
    """
    if not isinstance(result, dict):
        return None, None, None
    images = result.get('best_images') or []
    by_bbox = {}
    for i, im in enumerate(images):
        if not isinstance(im, dict):
            continue
        bid = im.get('bbox_id')
        if not isinstance(bid, str):
            continue
        dist = im.get('camera_distance_m')
        try:
            dist = float(dist)
        except (TypeError, ValueError):
            continue
        by_bbox[bid] = (i, dist)

    best_id, best_idx, best_dist = None, None, None
    for bid in bbox_ids:
        if bid not in by_bbox:
            continue
        idx, dist = by_bbox[bid]
        if (best_dist is None
                or dist < best_dist
                or (dist == best_dist and bid < best_id)):
            best_id, best_idx, best_dist = bid, idx, dist
    return best_id, best_idx, best_dist


def _merge_geometry(result, primary_id, member_ids):
    """Compute the merged (center, size, yaw) tuple at render time.

    Math is identical to the description in `merge_bboxes` — this helper
    isolates it so the render path can re-derive deterministically.
    """
    idx = _furniture_index(result)
    primary = idx.get(primary_id)
    if primary is None:
        return None, None, None

    boxes = [primary]
    for mid in member_ids or ():
        m = idx.get(mid)
        if isinstance(m, dict):
            boxes.append(m)

    lo = [float('inf')] * 3
    hi = [float('-inf')] * 3
    for b in boxes:
        blo, bhi = _aabb_extents(b)
        for i in range(3):
            if blo[i] < lo[i]:
                lo[i] = blo[i]
            if bhi[i] > hi[i]:
                hi[i] = bhi[i]

    center = [(lo[i] + hi[i]) / 2.0 for i in range(3)]
    size = [hi[i] - lo[i] for i in range(3)]
    yaw = primary.get('yaw')
    return center, size, yaw


# ---------------------------------------------------------------------------
# Spatial-proximity carry-over (plan §19b)
# ---------------------------------------------------------------------------

def _effective_class(review, bbox_id, fallback_class):
    """Return class_override if set, else the input fallback (model class)."""
    bxs = (review or {}).get('bboxes') or {}
    entry = bxs.get(bbox_id)
    if isinstance(entry, dict):
        co = entry.get('class_override')
        if isinstance(co, str) and co.strip():
            return co
    return fallback_class


def _max_footprint(size):
    """max(size[0], size[1]) — the floor footprint scale (X/Y)."""
    if not isinstance(size, list) or len(size) < 2:
        return 0.0
    return max(float(size[0]), float(size[1]))


def _euclid(a, b):
    if not isinstance(a, list) or not isinstance(b, list):
        return float('inf')
    if len(a) < 3 or len(b) < 3:
        return float('inf')
    return ((float(a[0]) - float(b[0])) ** 2
            + (float(a[1]) - float(b[1])) ** 2
            + (float(a[2]) - float(b[2])) ** 2) ** 0.5


def carry_over_review(old_review, old_result, new_result,
                      max_dist_m=0.3, scale_tolerance=0.25):
    """Spatial-proximity carry-over of review state between Modal runs.

    For each new bbox, find old bboxes whose center is within `max_dist_m`
    AND whose effective class (class_override or model class) matches AND
    whose footprint scale (max(size[0], size[1])) is within
    ±`scale_tolerance` × new max-footprint.

    Tie-break: nearest distance wins. On a tie, alphabetical bbox_id wins
    deterministically (e.g. `bbox_0` beats `bbox_1`).

    Carries over: class_override, image_override, linked_product, status
    (incl. merged_from + member status). Does NOT carry merged_into pointers
    (those are rebuilt locally if both endpoints survived; otherwise dropped
    to a warning). Unmatched new bboxes -> STATUS_UNTOUCHED.

    Returns: (new_review, warnings) where warnings is a list of dicts:
        {old_bbox_id, reason}
      reasons are: 'unmatched_old', 'multi_match' (loser ids), 'broken_merge'
      (a merged_into pointer whose target didn't survive).
    """
    if not isinstance(new_result, dict):
        raise TypeError('new_result must be a dict')

    new_furniture = new_result.get('furniture') or []
    old_furniture = (old_result or {}).get('furniture') or []
    old_review = old_review or {}

    # Index old furniture by id for fast lookup.
    old_idx = _furniture_index(old_result)
    old_bxs = (old_review.get('bboxes') or {})

    # Build the mapping: new_id -> old_id (or None when unmatched). Also
    # track the reverse mapping: old_id -> new_id (None for unmatched olds).
    new_to_old = {}
    matched_olds = set()  # old_ids that matched at least one new
    multi_match_warnings = []  # accumulated as we resolve ties

    for new_box in new_furniture:
        if not isinstance(new_box, dict):
            continue
        new_id = new_box.get('id')
        if not isinstance(new_id, str) or not new_id:
            continue
        new_center = new_box.get('center')
        new_class = new_box.get('class')
        new_size = new_box.get('size') or []
        new_fp = _max_footprint(new_size)
        new_eff_class = new_class

        # Collect candidates that pass all gates.
        candidates = []  # list of (dist, old_id)
        for old_id, old_box in old_idx.items():
            old_center = old_box.get('center')
            old_class = old_box.get('class')
            old_size = old_box.get('size') or []
            old_override = _effective_class(old_review, old_id, old_class)

            # Plan §19b: "same `class` (or class_override)". A new bbox's
            # class matches an old bbox if it equals EITHER the old model
            # class OR the old class_override. This is the most useful
            # behaviour: a technician who relabelled an old bbox should
            # still see it carry over when the model relabels the new
            # detection back to the old's original class on a re-run, AND
            # vice versa.
            if (new_eff_class != old_class
                    and new_eff_class != old_override):
                continue
            d = _euclid(new_center, old_center)
            if d > max_dist_m:
                continue

            old_fp = _max_footprint(old_size)
            # Scale tolerance is relative to the new bbox's footprint.
            # For new_fp == 0 we degenerate to "any old footprint within
            # the same absolute slack" by treating max(new_fp, old_fp) as
            # the reference; a zero-vs-zero compare matches.
            ref = max(new_fp, old_fp, 1e-9)
            if abs(old_fp - new_fp) > scale_tolerance * ref:
                continue

            candidates.append((d, old_id))

        if not candidates:
            new_to_old[new_id] = None
            continue

        # Sort: nearest first; alphabetical tie-break.
        candidates.sort(key=lambda t: (t[0], t[1]))
        best_dist, best_old = candidates[0]
        new_to_old[new_id] = best_old
        matched_olds.add(best_old)

        if len(candidates) > 1:
            # Record the losers for traceability — only for true ties on
            # distance (the §19b spec is "deterministic alphabetical
            # tie-break on bbox_id when distances are equal"). We surface
            # the winner+loser pairs so the technician can confirm.
            losers = [oid for d, oid in candidates[1:] if d == best_dist]
            if losers:
                multi_match_warnings.append({
                    'old_bbox_id': best_old,
                    'reason': 'multi_match',
                    'losers': losers,
                    'new_bbox_id': new_id,
                })

    # Build the new review.
    new_bxs = {}
    for new_box in new_furniture:
        if not isinstance(new_box, dict):
            continue
        new_id = new_box.get('id')
        if not isinstance(new_id, str) or not new_id:
            continue

        old_id = new_to_old.get(new_id)
        if old_id is None:
            new_bxs[new_id] = {'status': STATUS_UNTOUCHED}
            continue

        old_entry = dict(old_bxs.get(old_id) or {})
        # Drop merge_into (member) pointers — we rebuild them below.
        old_entry.pop('target', None)
        # Drop image_override (and recapture_at): the recapture JPEG lives
        # in the OLD run dir under the old positional index and isn't
        # copied into the new run; the new positional index is also wrong
        # because Modal regenerates bbox positions. Surface a warning so
        # the technician knows their recapture work needs redoing.
        dropped_override = old_entry.pop('image_override', None)
        old_entry.pop('recapture_at', None)
        if isinstance(dropped_override, str) and dropped_override:
            multi_match_warnings.append({
                'old_bbox_id': old_id,
                'new_bbox_id': new_id,
                'reason': 'carried_over_recapture_lost',
                'old_image_override': dropped_override,
            })
        old_status = old_entry.get('status', STATUS_UNTOUCHED)

        # If the old bbox was a merged_into member, its target may or may
        # not have survived. We check below; for now, leave it unchanged
        # but strip the dangling target. We turn it into kept by default
        # if its old status was merged_into.
        if old_status == STATUS_MERGED_INTO:
            old_entry['status'] = STATUS_KEPT

        # Drop merged_from list — we rebuild only if all members survived.
        members = old_entry.pop('merged_from', None)
        if isinstance(members, list):
            mapped = []
            broken = False
            # Inverse mapping: old_id -> new_id (best-effort; pick any
            # new_id that mapped to this old_id; ties broken alphabetically).
            inverse = {}
            for nid, oid in new_to_old.items():
                if oid is None:
                    continue
                if oid in inverse:
                    if nid < inverse[oid]:
                        inverse[oid] = nid
                else:
                    inverse[oid] = nid
            for m in members:
                mapped_new = inverse.get(m)
                if mapped_new is None:
                    broken = True
                    break
                mapped.append(mapped_new)
            if not broken and mapped:
                old_entry['merged_from'] = mapped

        new_bxs[new_id] = old_entry

    # Second pass: re-stamp merge MEMBERS. The per-bbox loop above
    # demotes old MERGED_INTO members to KEPT (line ~807) and rebuilds
    # the primary's `merged_from` with new ids — but never re-points the
    # member entries back at the new primary. Without this pass, both
    # the merged primary AND every member render as standalone furniture
    # in the upload envelope, shipping ghost duplicates.
    for new_primary_id, primary_entry in new_bxs.items():
        if not isinstance(primary_entry, dict):
            continue
        members = primary_entry.get('merged_from')
        if not isinstance(members, list):
            continue
        for member_id in members:
            if not isinstance(member_id, str) or member_id not in new_bxs:
                continue
            new_bxs[member_id] = {
                'status': STATUS_MERGED_INTO,
                'target': new_primary_id,
            }

    # Warnings:
    #  - unmatched_old: every old bbox that didn't match anything in new.
    #  - multi_match: losers we already collected.
    #  - broken_merge: any old merged_into entry whose target old_id
    #    didn't survive.
    warnings = []
    for old_id in old_idx.keys():
        if old_id not in matched_olds:
            warnings.append({
                'old_bbox_id': old_id,
                'reason': 'unmatched_old',
            })
    warnings.extend(multi_match_warnings)
    for old_id, old_entry in old_bxs.items():
        if not isinstance(old_entry, dict):
            continue
        if old_entry.get('status') != STATUS_MERGED_INTO:
            continue
        target = old_entry.get('target')
        if not isinstance(target, str):
            continue
        # Did both this member AND its target survive into new?
        if old_id not in matched_olds or target not in matched_olds:
            warnings.append({
                'old_bbox_id': old_id,
                'reason': 'broken_merge',
                'old_target': target,
            })

    # Build the new review skeleton, preserving identity fields from old.
    new_review = {
        'scan_id': old_review.get('scan_id'),
        'room_name': old_review.get('room_name'),
        'result_job_id': new_result.get('job_id'),
        'started_at': _now_iso(),
        'reviewed_at': None,
        'submitted_at': None,
        'version': 1,
        'bboxes': new_bxs,
        'notes': old_review.get('notes', ''),
    }
    return new_review, warnings


# ---------------------------------------------------------------------------
# Render reviewed envelope (plan §6c steps 1-6)
# ---------------------------------------------------------------------------

def _is_image_recaptured(review_entry):
    """True if `image_override` ends with `_recapture.jpg` (per plan §6c.3)."""
    if not isinstance(review_entry, dict):
        return False
    over = review_entry.get('image_override')
    if not isinstance(over, str):
        return False
    return over.endswith('_recapture.jpg')


def render_reviewed(result, review):
    """Pure render of the reviewed envelope.

    Implements plan §6c steps 1-6 verbatim:
      1. Drop deleted/merged_into/untouched furniture & best_images.
      2. Apply class_override to furniture[i].class & best_images[i].class.
      3. Apply image_override; add `local_path`; preserve `url`; drop
         `pixel_aabb` for recaptured images. Adds `image_source`
         ("model" or "recapture").
      4. Merge: expand size to AABB union of merged_from members, yaw stays
         from primary, center recomputed.
      5. Add review_meta block with counts.
      6. Preserve everything else verbatim.

    Inputs are deep-copied before mutation; the caller's dicts are unchanged
    after the call.
    """
    if not isinstance(result, dict):
        raise TypeError('result must be a dict')
    if not isinstance(review, dict):
        raise TypeError('review must be a dict')

    out = copy.deepcopy(result)
    rev = copy.deepcopy(review)
    bxs = rev.get('bboxes') or {}

    original_furniture = out.get('furniture') or []
    original_count = len(original_furniture)

    # Drop predicate: any bbox whose review status is in {deleted,
    # merged_into, untouched} is removed from the output. A bbox missing
    # from the review map is treated as 'untouched' (per plan §6b).
    def _kept(bid):
        entry = bxs.get(bid)
        if not isinstance(entry, dict):
            return False  # untouched ⇒ drop
        return entry.get('status') == STATUS_KEPT

    # 1+2+4: furniture pass.
    new_furniture = []
    merged_count = 0
    for f in original_furniture:
        if not isinstance(f, dict):
            continue
        bid = f.get('id')
        if not _kept(bid):
            continue
        entry = bxs.get(bid) or {}

        nf = dict(f)
        # 2. class override
        co = entry.get('class_override')
        if isinstance(co, str) and co.strip():
            nf['class'] = co

        # 4. merge geometry
        members = entry.get('merged_from')
        if isinstance(members, list) and members:
            center, size, yaw = _merge_geometry(result, bid, members)
            if center is not None:
                nf['center'] = center
            if size is not None:
                nf['size'] = size
            if yaw is not None:
                nf['yaw'] = yaw
            merged_count += 1

        # Linked product (forward-compat with plan §20).
        lp = entry.get('linked_product')
        if isinstance(lp, dict):
            nf['linked_product'] = dict(lp)
        # If linked_product is explicitly None we still propagate it so the
        # back-office can see "search attempted but no match".
        elif 'linked_product' in entry:
            nf['linked_product'] = None

        new_furniture.append(nf)

    out['furniture'] = new_furniture

    # 1+3: best_images pass.
    new_best = []
    recaptured_count = 0
    for img in (out.get('best_images') or []):
        if not isinstance(img, dict):
            continue
        bid = img.get('bbox_id')
        if not _kept(bid):
            continue
        entry = bxs.get(bid) or {}

        ni = dict(img)
        co = entry.get('class_override')
        if isinstance(co, str) and co.strip():
            ni['class'] = co

        # Resolve idx for the local path. The Modal envelope numbers
        # best_images by their position in the array — we use that index
        # for the `<idx>.jpg` filename written by `_scoped_save_done_artifacts`.
        idx_pos = (out.get('best_images') or []).index(img)
        local_default = os.path.join('best_views', f'{idx_pos}.jpg')

        # `image_override` can come from two sources:
        #   1. Brio recapture (`<idx>_recapture.jpg`) — a fresh photo;
        #      `pixel_aabb` is no longer meaningful, count toward
        #      `recaptured_count` for telemetry.
        #   2. Auto-best-image-on-merge (`<idx>.jpg`) — a SIBLING bbox's
        #      original Modal frame, swapped in because it's sharper.
        #      Still a model photo; `pixel_aabb` stays valid; does NOT
        #      count as a recapture.
        over = entry.get('image_override')
        if isinstance(over, str) and over.strip():
            ni['local_path'] = over
            if _is_image_recaptured(entry):
                ni['image_source'] = 'recapture'
                ni.pop('pixel_aabb', None)
                recaptured_count += 1
            else:
                ni['image_source'] = 'model'
        else:
            ni['local_path'] = local_default
            ni['image_source'] = 'model'

        new_best.append(ni)

    out['best_images'] = new_best

    # 5. review_meta provenance block.
    deleted_count = 0
    merged_into_count = 0
    for entry in bxs.values():
        if not isinstance(entry, dict):
            continue
        s = entry.get('status')
        if s == STATUS_DELETED:
            deleted_count += 1
        elif s == STATUS_MERGED_INTO:
            merged_into_count += 1

    out['review_meta'] = {
        'reviewed_at': rev.get('reviewed_at'),
        'technician': rev.get('technician'),
        'notes': rev.get('notes', ''),
        'bbox_count_original': original_count,
        'bbox_count_reviewed': len(new_furniture),
        'merged_count': merged_count,
        # `merged_count_members` is the count of bboxes folded INTO
        # primaries (i.e. STATUS_MERGED_INTO entries). Useful for
        # back-office display ("3 boxes merged into 1").
        'merged_count_members': merged_into_count,
        'deleted_count': deleted_count,
        'recaptured_count': recaptured_count,
    }

    return out


# ---------------------------------------------------------------------------
# Upload filter (plan §21)
# ---------------------------------------------------------------------------

def _parse_path(path):
    """Tokenise a path string like 'a.b[*].c' into list of (kind, key) tuples.

    Tokens:
      ('key', '<name>')   — dict key
      ('star', None)      — array wildcard (every element)

    `b[*]` becomes ('key', 'b'), ('star', None).
    """
    tokens = []
    if not isinstance(path, str) or not path:
        return tokens
    parts = path.split('.')
    for part in parts:
        # Each part may end with one or more `[*]`.
        # We only support `[*]` (no indexing); plan §21a says trailing `*`
        # not supported either, but we tolerate `a[*][*]` for nested arrays
        # because it's strictly more general and harmless.
        while True:
            i = part.find('[*]')
            if i == -1:
                break
            head = part[:i]
            if head:
                tokens.append(('key', head))
                head = ''
            tokens.append(('star', None))
            part = part[i + 3:]
            if not part:
                break
        if part:
            tokens.append(('key', part))
    return tokens


def _set_path(target, tokens, value):
    """Insert `value` at the path described by `tokens` into `target`.

    `target` may be a dict or a list. The structure is created on demand.
    Returns the (possibly new) root.
    """
    if not tokens:
        return value

    kind, key = tokens[0]
    rest = tokens[1:]

    if kind == 'key':
        if not isinstance(target, dict):
            target = {}
        target[key] = _set_path(target.get(key), rest, value)
        return target

    # star: target must be a list of the same length as the value list.
    if not isinstance(value, list):
        return target  # nothing to broadcast over
    if not isinstance(target, list):
        target = [None] * len(value)
    while len(target) < len(value):
        target.append(None)
    for i, item in enumerate(value):
        target[i] = _set_path(target[i], rest, item)
    return target


def _get_path(source, tokens):
    """Read the value at `tokens` from `source`, mirroring `_set_path`.

    Returns a "shape" that mirrors source's array layout — e.g.
    `furniture[*].id` returns a list of ids in source order. Missing keys
    inside an iterated dict produce a list slot of MISSING (returned as
    `_PATH_MISSING` sentinel) so the caller can decide whether to still
    create the slot.
    """
    if not tokens:
        return source

    kind, key = tokens[0]
    rest = tokens[1:]

    if kind == 'key':
        if not isinstance(source, dict):
            return _PATH_MISSING
        if key not in source:
            return _PATH_MISSING
        return _get_path(source[key], rest)

    # star
    if not isinstance(source, list):
        return _PATH_MISSING
    out = []
    for item in source:
        v = _get_path(item, rest)
        out.append(v)
    return out


_PATH_MISSING = object()


def _set_path_kept(target, tokens, value):
    """Insert `value` into `target` at `tokens`, skipping MISSING slots.

    Used during the include-pass: we want to materialise a parallel
    skeleton that contains only the included paths. A MISSING leaf is
    silently dropped (the source didn't have that key).
    """
    if not tokens:
        if value is _PATH_MISSING:
            return target  # don't overwrite with missing
        return value

    kind, key = tokens[0]
    rest = tokens[1:]

    if kind == 'key':
        if not isinstance(target, dict):
            target = {}
        existing = target.get(key)
        merged = _set_path_kept(existing, rest, value)
        if merged is not None or key in target:
            target[key] = merged
        return target

    # star
    if not isinstance(value, list):
        return target
    if not isinstance(target, list):
        target = []
    while len(target) < len(value):
        target.append(None)
    for i, item in enumerate(value):
        if item is _PATH_MISSING:
            continue
        target[i] = _set_path_kept(target[i], rest, item)
    return target


def _delete_path(target, tokens):
    """Remove the slot at `tokens` from `target` in place. Tolerant of
    missing keys.
    """
    if not tokens:
        return

    kind, key = tokens[0]
    rest = tokens[1:]

    if kind == 'key':
        if not isinstance(target, dict):
            return
        if not rest:
            target.pop(key, None)
            return
        if key not in target:
            return
        _delete_path(target[key], rest)
        return

    # star
    if not isinstance(target, list):
        return
    for item in target:
        _delete_path(item, rest)


def _validate_filter(f):
    """Cheap structural check; raises ValueError on bad shape."""
    if not isinstance(f, dict):
        raise ValueError('filter must be a dict')
    inc = f.get('include_paths')
    exc = f.get('exclude_paths')
    if inc is not None and not (isinstance(inc, list)
                                and all(isinstance(p, str) for p in inc)):
        raise ValueError('include_paths must be a list of strings')
    if exc is not None and not (isinstance(exc, list)
                                and all(isinstance(p, str) for p in exc)):
        raise ValueError('exclude_paths must be a list of strings')


def apply_upload_filter(envelope, upload_filter):
    """Filter `envelope` per the include/exclude paths.

    Pass 1: build a parallel skeleton containing only paths matched by
    `include_paths`. Pass 2: drop sub-paths matched by `exclude_paths`.

    The filter input is validated; on a structural failure the function
    raises ValueError. Use `load_filter()` for a tolerant loader that
    falls back to defaults.

    Always preserves the input's `schema_version` if present (fail-safe).
    """
    if not isinstance(envelope, dict):
        raise TypeError('envelope must be a dict')
    _validate_filter(upload_filter)

    src = copy.deepcopy(envelope)

    # Pass 1: include.
    out = None
    inc = upload_filter.get('include_paths') or []
    for path in inc:
        tokens = _parse_path(path)
        if not tokens:
            continue
        value = _get_path(src, tokens)
        if value is _PATH_MISSING:
            continue
        if out is None:
            out = {}
        out = _set_path_kept(out, tokens, value)
    if out is None:
        out = {}

    # Schema version fail-safe — always preserved if the source has one.
    if 'schema_version' in src and 'schema_version' not in out:
        out['schema_version'] = src['schema_version']

    # Pass 2: exclude.
    exc = upload_filter.get('exclude_paths') or []
    for path in exc:
        tokens = _parse_path(path)
        if not tokens:
            continue
        _delete_path(out, tokens)

    return out


# ---------------------------------------------------------------------------
# Filter file load / save (plan §21a — `.athathi/upload_filter.json`)
# ---------------------------------------------------------------------------

def _athathi_dir():
    """Re-resolve the athathi dir at call time so tests can monkeypatch
    `auth.ATHATHI_DIR`.
    """
    try:
        import auth as _auth  # local import to avoid hard dep at module import
        return _auth.ATHATHI_DIR
    except Exception:  # pragma: no cover - never happens in this repo
        return os.path.join(_SCRIPT_DIR, '.athathi')


def _filter_path():
    return os.path.join(_athathi_dir(), 'upload_filter.json')


def load_filter():
    """Return the on-disk upload filter, falling back to DEFAULT_UPLOAD_FILTER.

    Tolerant by design: parse errors / structural failures log a stderr
    warning and return a deep copy of DEFAULT_UPLOAD_FILTER. The filter is
    a single source of truth that operators may edit on the device — a
    typo there should NEVER take the submit pipeline down (§21g).
    """
    path = _filter_path()
    if not os.path.isfile(path):
        return copy.deepcopy(DEFAULT_UPLOAD_FILTER)
    try:
        with open(path, 'r') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(
            f'[review] load_filter: failed to read {path}: {e!r}; '
            f'falling back to DEFAULT_UPLOAD_FILTER',
            file=sys.stderr,
        )
        return copy.deepcopy(DEFAULT_UPLOAD_FILTER)
    try:
        _validate_filter(data)
    except ValueError as e:
        print(
            f'[review] load_filter: structural error in {path}: {e}; '
            f'falling back to DEFAULT_UPLOAD_FILTER',
            file=sys.stderr,
        )
        return copy.deepcopy(DEFAULT_UPLOAD_FILTER)
    return data


def save_filter(f):
    """Persist `f` to the on-disk filter path (atomic temp+rename)."""
    _validate_filter(f)
    _atomic_write_json(_filter_path(), f, mode=0o644)


# ---------------------------------------------------------------------------
# Convenience for routes
# ---------------------------------------------------------------------------

def render_for_run(run_dir):
    """Read result.json + review.json from `run_dir`; return the rendered pair.

    Returns (reviewed_envelope, upload_envelope). Caller decides whether to
    persist either or both (the GET preview route just returns one; the
    Submit pipeline writes both to disk).

    Raises:
      FileNotFoundError if `result.json` is missing — the run hasn't
      produced anything reviewable yet.
    """
    result = _read_json_or_none(os.path.join(run_dir, 'result.json'))
    if result is None:
        raise FileNotFoundError(
            os.path.join(run_dir, 'result.json'))
    review = read_review(run_dir)
    if review is None:
        # No review yet — render against the implicit "everything untouched"
        # state. The result is a no-op-style envelope: every bbox dropped,
        # but with review_meta still stamped. That's exactly what the UI
        # expects for an unreviewed preview.
        review = initial_review(
            None,
            None,
            result,
        )

    reviewed = render_reviewed(result, review)
    upload = apply_upload_filter(reviewed, load_filter())
    return reviewed, upload
