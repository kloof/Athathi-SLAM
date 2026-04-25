"""Submit-pipeline helpers (Step 6, plan §8 / §21 / §22c).

Pure helpers — framework-free; the Flask routes in `app.py` orchestrate
the end-to-end flow. The single network surface is
`athathi_proxy.upload_bundle` (called from `submit_run_outputs`); the rest
is disk-state / process spawning.

Public surface:

    SubmitNetworkError           — raised on AthathiError(0) so the route
                                   can queue for retry.
    gather_runs_for_submit(scan_id)
    gating_message(scan_id)
    render_run_outputs(run_dir, technician_username)
    build_image_files_for_upload(reviewed_envelope, run_dir)
    submit_run_outputs(token, upload_endpoint, envelope_path, image_files,
                       image_transport='multipart')
    stamp_submit_outcome(scan_id, *, runs, response, error, queued)
    run_post_submit_hook(hook_command, project_dir, scan_id, timeout=60)
    submit_pending_retry(token_provider)

Plan §-1a hard constraint: this module must NEVER touch the recording /
camera / Modal / calibration code paths. It only reads `result.json` +
`review.json`, runs `review.render_for_run`, and POSTs the bundle.
"""

import json
import os
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

import athathi_proxy
import projects as _projects
import review as _review


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class SubmitNetworkError(Exception):
    """Raised by `submit_run_outputs` when `upload_bundle` fails with
    `AthathiError(status_code=0)` — i.e. curl could not reach the upload
    endpoint. The Flask route catches this to set `submit_pending=True` on
    the manifest and return 202. Non-network failures (4xx, 5xx-after-retries)
    surface as the underlying `AthathiError`.
    """

    def __init__(self, body='', message=''):
        self.body = body or ''
        if not message:
            message = 'Network error during upload'
        super().__init__(message)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso():
    """ISO-8601 UTC timestamp suitable for manifest / review fields."""
    return datetime.now(tz=timezone.utc).isoformat().replace('+00:00', 'Z')


def _atomic_write_bytes(path, data, mode=0o644):
    """Write `data` bytes atomically. Mkdir parent."""
    parent = os.path.dirname(path) or '.'
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.tmp.', dir=parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
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


# ---------------------------------------------------------------------------
# gather + gating
# ---------------------------------------------------------------------------

def gather_runs_for_submit(scan_id):
    """Return one entry per scan in this project, keyed on the active run.

    Each entry: `{scan_name, run_dir, run_id, review, result_path}`.
      - `run_dir`: absolute path to `<scan_dir>/processed/runs/<run_id>/`,
        or None if the scan has no active run pointer.
      - `run_id`: the `active_run_id`, or None.
      - `review`: parsed `review.json` for that run, or None if missing.
      - `result_path`: path to `result.json` (may not exist on disk).

    Caller is responsible for validating that every scan is reviewable
    (has an active run + a successful result.json) — see `gating_message`.
    """
    sid = int(scan_id)
    out = []
    for entry in _projects.list_scans(sid):
        scan_name = entry.get('name')
        run_id = entry.get('active_run_id')
        run_dir = None
        review_data = None
        result_path = None
        if isinstance(run_id, str) and run_id:
            run_dir = _projects.processed_dir_for_run(sid, scan_name, run_id)
            review_data = _review.read_review(run_dir)
            result_path = os.path.join(run_dir, 'result.json')
        out.append({
            'scan_name': scan_name,
            'run_dir': run_dir,
            'run_id': run_id,
            'review': review_data,
            'result_path': result_path,
        })
    return out


def _scan_is_processing(scan_id, scan_name):
    """True if `app._active_processing` has an entry for this scan.

    We import `app` lazily so this module stays framework-free at import
    time (and tests can mock it out).
    """
    try:
        import app  # noqa: PLC0415
    except Exception:
        return False
    try:
        with app._processing_lock:
            for entry in app._active_processing.values():
                if (
                    entry.get('scan_id') == int(scan_id)
                    and entry.get('scan_name') == scan_name
                ):
                    return True
    except Exception:
        return False
    return False


def _scan_status_from_result(run_dir):
    """Return the `status` field from `<run_dir>/result.json`, or None."""
    if not run_dir:
        return None
    path = os.path.join(run_dir, 'result.json')
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data.get('status')


def gating_message(scan_id):
    """Return None if Submit is allowed; else a short reason string.

    Priority order per plan §22c:
      1. already-submitted    → `Already submitted on <date>`
      2. still-processing     → `<scan_name> is still processing`
      3. error                → `<scan_name> failed — re-process or delete`
      4. not-reviewed-yet     → `<scan_name> not reviewed yet`

    The plan's priority 5 (no-network) is left to the route — `submit.py`
    only inspects disk state.
    """
    sid = int(scan_id)
    manifest = _projects.read_manifest(sid)
    if manifest is None:
        # The route checks for a missing manifest before us; if we reach
        # here with None we'd just block forever — surface a sane message.
        return 'project not found'

    submitted_at = manifest.get('submitted_at')
    if isinstance(submitted_at, str) and submitted_at:
        return f'Already submitted on {submitted_at}'

    runs = gather_runs_for_submit(sid)

    # Priority 2: still-processing.
    for entry in runs:
        if _scan_is_processing(sid, entry['scan_name']):
            return f"{entry['scan_name']} is still processing"

    # Priority 3: error.
    for entry in runs:
        run_dir = entry.get('run_dir')
        status = _scan_status_from_result(run_dir)
        if status == 'error':
            return (f"{entry['scan_name']} failed — "
                    f"re-process or delete")

    # Priority 4: not-reviewed-yet (also catches scans with no active run
    # at all — those have no review either).
    for entry in runs:
        rv = entry.get('review')
        if not isinstance(rv, dict) or not rv.get('reviewed_at'):
            return f"{entry['scan_name']} not reviewed yet"

    # Defensive: empty project (no scans) → block. The technician should
    # at least have one room before submitting.
    if not runs:
        return 'no scans in project'

    return None


# ---------------------------------------------------------------------------
# Render + bundle helpers
# ---------------------------------------------------------------------------

def build_image_files_for_upload(reviewed_envelope, run_dir):
    """Walk `reviewed.best_images` and return upload field/path pairs.

    For each entry:
      - Field name: `image_<bbox_id>` (preserves the linkage from the
        envelope to the image without depending on positional index).
      - Path: `<run_dir>/<entry.local_path>` — `render_reviewed` already
        resolves the right path (original `<orig_idx>.jpg`, or the
        `image_override` recapture path), using the entry's position in
        the *unfiltered* Modal envelope. We must trust that here:
        re-deriving from `enumerate()` over the filtered list silently
        pairs surviving bboxes with their neighbour's photo whenever any
        bbox has been deleted or merged.

    Skips entries with no `local_path` or whose file is missing on disk —
    logs a stderr warning.

    Returns: list of `(field_name, absolute_path)` tuples.
    """
    if not isinstance(reviewed_envelope, dict):
        return []
    out = []
    for entry in (reviewed_envelope.get('best_images') or []):
        if not isinstance(entry, dict):
            continue
        bbox_id = entry.get('bbox_id')
        if not isinstance(bbox_id, str) or not bbox_id:
            continue
        local = entry.get('local_path')
        if not isinstance(local, str) or not local:
            print(
                f'[submit] best_images entry for bbox {bbox_id!r} has no '
                f'local_path; skipping',
                file=sys.stderr,
            )
            continue
        chosen = os.path.join(run_dir, local)
        if not os.path.isfile(chosen):
            print(
                f'[submit] image missing on disk for bbox {bbox_id!r} '
                f'(expected {chosen}); skipping',
                file=sys.stderr,
            )
            continue
        out.append((f'image_{bbox_id}', os.path.abspath(chosen)))
    return out


def render_run_outputs(run_dir, technician_username):
    """Render `result_reviewed.json` + `result_for_upload.json` for one run.

    Mutates `run_dir` by writing two new files (atomic temp + rename).
    Stamps `submitted_by = technician_username` on both envelopes.

    Returns: `{reviewed_path, upload_path, image_files}`.

    Raises FileNotFoundError if `<run_dir>/result.json` is missing — the
    run hasn't produced anything reviewable yet; the gating step should
    have caught that already.
    """
    if not isinstance(run_dir, str) or not run_dir:
        raise ValueError('run_dir must be a non-empty string')

    reviewed, upload = _review.render_for_run(run_dir)

    # Stamp submitted_by + a placeholder submitted_at (rewritten if/when
    # the upload + complete actually succeed; this gives the upload bundle
    # a deterministic value the back-office can trust).
    if isinstance(technician_username, str) and technician_username:
        reviewed['submitted_by'] = technician_username
        upload['submitted_by'] = technician_username

    reviewed_path = _review.reviewed_out_path(run_dir)
    upload_path = _review.upload_out_path(run_dir)
    _atomic_write_bytes(
        reviewed_path,
        (json.dumps(reviewed, indent=2) + '\n').encode('utf-8'),
    )
    _atomic_write_bytes(
        upload_path,
        (json.dumps(upload, indent=2) + '\n').encode('utf-8'),
    )

    image_files = build_image_files_for_upload(reviewed, run_dir)
    return {
        'reviewed_path': reviewed_path,
        'upload_path': upload_path,
        'image_files': image_files,
    }


# ---------------------------------------------------------------------------
# Upload (one network call)
# ---------------------------------------------------------------------------

def submit_run_outputs(token, upload_endpoint, envelope_path, image_files,
                       image_transport='multipart'):
    """POST one run's bundle to the upload endpoint, or render-only.

    If `upload_endpoint` is None / empty, returns
    `{'uploaded': False, 'reason': 'no upload_endpoint configured'}`
    immediately — the rendered files are already on disk and the
    post-submit hook can pick them up.

    Otherwise:
      - Reads `envelope_path` bytes.
      - Calls `athathi_proxy.upload_bundle(token, upload_endpoint, ...,
        image_files)`.
      - On success returns `{'uploaded': True, 'response': <dict>}`.
      - On `AthathiError(status_code=0)` raises `SubmitNetworkError` so
        the caller can queue.
      - On any other `AthathiError` re-raises verbatim — the route
        translates to a 502.

    `image_transport` is accepted for forward-compat with the inline-base64
    branch (§21c); v1 only ships multipart, so a non-'multipart' value is
    treated as 'multipart'. The arg is preserved so tests can assert it
    was threaded through.
    """
    if not upload_endpoint:
        return {
            'uploaded': False,
            'reason': 'no upload_endpoint configured',
        }
    if not isinstance(envelope_path, str) or not os.path.isfile(envelope_path):
        raise FileNotFoundError(envelope_path)

    with open(envelope_path, 'rb') as f:
        envelope_bytes = f.read()

    try:
        response = athathi_proxy.upload_bundle(
            token, upload_endpoint, envelope_bytes, image_files or [],
        )
    except athathi_proxy.AthathiError as e:
        if e.status_code == 0:
            raise SubmitNetworkError(
                body=(e.body or '')[:400],
                message=f'Upload network error: {e.body[:120]!s}',
            )
        raise
    return {'uploaded': True, 'response': response}


# ---------------------------------------------------------------------------
# Stamp outcome on disk
# ---------------------------------------------------------------------------

def stamp_submit_outcome(scan_id, *, runs, response=None, error=None,
                         queued=False):
    """Update manifest + per-run review.json with the submit outcome.

    Stamps `manifest.submitted_at` (i.e. "we submitted from the Pi at this
    moment") only. The companion field `manifest.completed_at` represents
    "Athathi marked this scan completed" and is owned by the upstream
    history-mirror sync in `_projects_render_merged` — this function does
    NOT touch it.

    Args:
      scan_id: project id.
      runs: list of dicts as returned by `gather_runs_for_submit` (we only
        use `run_dir` and `run_id`).
      response: parsed upstream `complete_scan` body (or None on queue/fail).
      error: short error string (or None on success).
      queued: True if the submit is pending retry (set submit_pending).

    Atomic and idempotent: re-calling on an already-stamped manifest
    refreshes the timestamps but never clears the prior `submitted_at`.
    """
    sid = int(scan_id)
    manifest = _projects.read_manifest(sid)
    if manifest is None:
        return

    now = _now_iso()
    if queued:
        manifest['submit_pending'] = True
        if error:
            manifest['submit_pending_error'] = str(error)[:400]
        else:
            manifest.pop('submit_pending_error', None)
    else:
        # Successful submit. Only stamp `submitted_at`; `completed_at` is
        # owned by the upstream-history mirror, not by us.
        if not manifest.get('submitted_at'):
            manifest['submitted_at'] = now
        manifest.pop('submit_pending', None)
        manifest.pop('submit_pending_error', None)
        manifest.pop('submit_pending_uploads', None)
        if response is not None:
            manifest['submit_response'] = response

    _projects.write_manifest(sid, manifest)

    # Stamp submitted_at on each run's review.json (only on success).
    if not queued:
        for entry in runs:
            run_dir = entry.get('run_dir')
            if not run_dir or not os.path.isdir(run_dir):
                continue
            rv = _review.read_review(run_dir)
            if not isinstance(rv, dict):
                continue
            if not rv.get('submitted_at'):
                rv['submitted_at'] = now
                try:
                    _review.write_review(run_dir, rv)
                except OSError as e:
                    print(
                        f'[submit] could not stamp review.submitted_at '
                        f'for {run_dir}: {e!r}',
                        file=sys.stderr,
                    )


# ---------------------------------------------------------------------------
# Post-submit hook
# ---------------------------------------------------------------------------

_HOOK_LOG_BYTES = 4 * 1024  # truncate stdout/stderr tails to 4 KB.


def run_post_submit_hook(hook_command, project_dir, scan_id, timeout=60):
    """Spawn the configured shell command as a post-submit hook.

    Args:
      hook_command: shell command string from `config.post_submit_hook`,
        or None / empty → returns `{ok: True, ran: False, ...}`.
      project_dir: absolute path passed as `$1` to the hook.
      scan_id: int passed as `$2`.
      timeout: hard subprocess timeout in seconds (default 60).

    Returns:
      {ok, ran, returncode, stdout_tail, stderr_tail, error?}.

      `stdout_tail` / `stderr_tail` are truncated to 4 KB.
      `ok` is False if the hook timed out, exited non-zero, or wasn't
      found on PATH.
    """
    if not hook_command or not isinstance(hook_command, str):
        return {
            'ok': True,
            'ran': False,
            'returncode': None,
            'stdout_tail': '',
            'stderr_tail': '',
        }

    cmd = [hook_command, str(project_dir or ''), str(int(scan_id))]
    try:
        # We use `shell=False` and pass the command as argv[0] — the spec
        # in §8 step 4 is `<hook> <project_dir> <scan_id>` which works
        # with a single-binary path. Operators who need shell features
        # can wrap in /bin/sh themselves: `post_submit_hook = "/bin/sh"`
        # plus their shell fragment in a script file.
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as e:
        return {
            'ok': False,
            'ran': False,
            'returncode': None,
            'stdout_tail': '',
            'stderr_tail': '',
            'error': f'hook not found: {e!s}',
        }
    except subprocess.TimeoutExpired as e:
        return {
            'ok': False,
            'ran': True,
            'returncode': None,
            'stdout_tail': (e.stdout or '')[-_HOOK_LOG_BYTES:]
                if isinstance(e.stdout, str) else '',
            'stderr_tail': (e.stderr or '')[-_HOOK_LOG_BYTES:]
                if isinstance(e.stderr, str) else '',
            'error': f'hook timed out after {timeout}s',
        }
    except OSError as e:
        return {
            'ok': False,
            'ran': False,
            'returncode': None,
            'stdout_tail': '',
            'stderr_tail': '',
            'error': f'hook spawn failed: {e!s}',
        }

    return {
        'ok': proc.returncode == 0,
        'ran': True,
        'returncode': proc.returncode,
        'stdout_tail': (proc.stdout or '')[-_HOOK_LOG_BYTES:],
        'stderr_tail': (proc.stderr or '')[-_HOOK_LOG_BYTES:],
    }


# ---------------------------------------------------------------------------
# Pending-retry sweep
# ---------------------------------------------------------------------------

def submit_pending_retry(token_provider, *,
                         upload_endpoint_provider=None,
                         technician_provider=None,
                         image_transport='multipart',
                         lock_provider=None):
    """Walk PROJECTS_ROOT for any project marked `submit_pending=True`
    and re-drive the submit pipeline.

    BACKEND-B2 (stage-aware retry):
      - If `manifest.submit_pending_stage == 'upload'` we re-render the
        run envelopes, re-`upload_bundle`, then `complete_scan`. This is
        the path for failures that happened BEFORE the upload landed.
      - If `submit_pending_stage == 'complete'` we just re-`complete_scan`
        (the upload already succeeded — no point pushing a multi-MB
        bundle again).
      - Missing/unknown stage → backwards-compat: try `complete_scan`
        first; if Athathi reports it never received the upload (404 or a
        body hint matching `_NO_UPLOAD_HINT_RE`), fall back to the
        upload+complete path.

    Args:
      token_provider:           callable() -> bearer token or None.
      upload_endpoint_provider: callable() -> upload URL or None. If None
                                or returning falsy, the upload-stage retry
                                degrades to `complete_scan`-only (the
                                local render is best-effort).
      technician_provider:      callable() -> username for the envelope
                                stamp; None tolerated.
      image_transport:          'multipart' (only mode shipped today).
      lock_provider:            callable(scan_id) -> threading.Lock for
                                BACKEND-B3 (race-free with foreground
                                /submit). None → no lock.

    Returns: list of `{scan_id, status, stage?, error?}`.

    A run already stamped with `submitted_at` is skipped even if
    `submit_pending` was left set — defensive cleanup.
    """
    import re as _re  # local — keep submit.py's top-level import set tight.
    _NO_UPLOAD_HINT_RE = _re.compile(
        r'(no upload received|missing.*upload|no bundle|'
        r'upload.*not.*found|404)',
        _re.IGNORECASE,
    )

    out = []
    token = None
    try:
        token = token_provider() if callable(token_provider) else None
    except Exception as e:
        print(f'[submit] submit_pending_retry: token_provider raised: {e!r}',
              file=sys.stderr)
        token = None

    upload_endpoint = None
    if callable(upload_endpoint_provider):
        try:
            upload_endpoint = upload_endpoint_provider()
        except Exception as e:
            print(f'[submit] submit_pending_retry: upload_endpoint_provider'
                  f' raised: {e!r}', file=sys.stderr)
            upload_endpoint = None

    technician = None
    if callable(technician_provider):
        try:
            technician = technician_provider()
        except Exception as e:
            print(f'[submit] submit_pending_retry: technician_provider'
                  f' raised: {e!r}', file=sys.stderr)
            technician = None

    def _per_project_lock(_sid):
        if not callable(lock_provider):
            return _NullLock()
        try:
            lk = lock_provider(_sid)
        except Exception:
            return _NullLock()
        return lk if lk is not None else _NullLock()

    def _do_upload_for(sid):
        """Re-render + re-upload every reviewed run for `sid`. Returns
        (ok, error_str_or_None). Network failure → ok=False, no raise.

        Skips scans whose names appear in `manifest.submit_pending_uploads`
        — those bundles already reached Athathi on a prior attempt; re-
        uploading them double-bills the back-office (which has no
        deterministic dedupe key on the v1 envelope).
        """
        try:
            runs_local = gather_runs_for_submit(sid)
        except Exception as e:
            return False, f'gather failed: {e!s}'
        m0 = _projects.read_manifest(sid)
        if isinstance(m0, dict):
            already = list(m0.get('submit_pending_uploads') or [])
        else:
            already = []
        already_set = set(already)
        for entry in runs_local:
            scan_name = entry.get('scan_name')
            if scan_name in already_set:
                continue
            run_dir = entry.get('run_dir')
            if not run_dir or not os.path.isdir(run_dir):
                return False, (
                    f"{scan_name} has no run dir on disk"
                )
            try:
                rendered = render_run_outputs(run_dir, technician)
            except FileNotFoundError as e:
                return False, (
                    f"{scan_name}: result.json missing ({e})"
                )
            try:
                outcome = submit_run_outputs(
                    token, upload_endpoint,
                    envelope_path=rendered['upload_path'],
                    image_files=rendered['image_files'],
                    image_transport=image_transport,
                )
            except SubmitNetworkError as e:
                return False, f'upload network failure: {e!s}'
            except athathi_proxy.AthathiError as e:
                return False, f'upload upstream error: {e!s}'
            # `outcome.uploaded == False` only when no endpoint configured;
            # that's still "did the local render", so let it through.
            _ = outcome
            # Persist the progress so a subsequent retry skips this scan
            # if a later one in the list fails.
            if isinstance(scan_name, str) and scan_name:
                already_set.add(scan_name)
                try:
                    m_progress = _projects.read_manifest(sid)
                    if isinstance(m_progress, dict):
                        m_progress['submit_pending_uploads'] = sorted(already_set)
                        _projects.write_manifest(sid, m_progress)
                except OSError:
                    pass
        return True, None

    for manifest in _projects.list_projects():
        sid = manifest.get('scan_id')
        if not isinstance(sid, int):
            continue
        if not manifest.get('submit_pending'):
            continue
        if manifest.get('submitted_at'):
            # Already done, just clear the flag.
            try:
                m = _projects.read_manifest(sid)
                if isinstance(m, dict):
                    m.pop('submit_pending', None)
                    m.pop('submit_pending_error', None)
                    m.pop('submit_pending_stage', None)
                    m.pop('submit_pending_uploads', None)
                    _projects.write_manifest(sid, m)
            except OSError:
                pass
            out.append({'scan_id': sid, 'status': 'already_submitted'})
            continue

        if not token:
            out.append({'scan_id': sid, 'status': 'skipped_no_token'})
            continue

        stage = manifest.get('submit_pending_stage')
        if not isinstance(stage, str):
            stage = None

        with _per_project_lock(sid):
            # Re-read inside the lock — a foreground /submit may have
            # cleared `submit_pending` between the outer scan and now.
            cur = _projects.read_manifest(sid)
            if not isinstance(cur, dict):
                continue
            if not cur.get('submit_pending'):
                continue
            if cur.get('submitted_at'):
                try:
                    cur.pop('submit_pending', None)
                    cur.pop('submit_pending_error', None)
                    cur.pop('submit_pending_stage', None)
                    cur.pop('submit_pending_uploads', None)
                    _projects.write_manifest(sid, cur)
                except OSError:
                    pass
                out.append({'scan_id': sid, 'status': 'already_submitted'})
                continue

            # ---- Stage 'upload': re-render + re-upload, then complete -----
            if stage == 'upload':
                ok, err = _do_upload_for(sid)
                if not ok:
                    out.append({
                        'scan_id': sid, 'status': 'failed',
                        'stage': 'upload', 'error': (err or '')[:200],
                    })
                    try:
                        m_err = _projects.read_manifest(sid)
                        if isinstance(m_err, dict):
                            m_err['submit_pending_error'] = (err or '')[:400]
                            m_err['submit_pending_stage'] = 'upload'
                            _projects.write_manifest(sid, m_err)
                    except OSError:
                        pass
                    continue
                # Upload succeeded → fall through to complete.

            # ---- Stage 'complete' (or fall-through from upload) -----------
            try:
                resp = athathi_proxy.complete_scan(token, sid)
            except athathi_proxy.AthathiError as e:
                # Stage missing → maybe upload never happened. Check the
                # error body / status for the no-upload hint and retry as
                # an upload+complete pair.
                if (
                    stage is None
                    and (
                        e.status_code == 404
                        or _NO_UPLOAD_HINT_RE.search(str(e.body or ''))
                        or _NO_UPLOAD_HINT_RE.search(str(e))
                    )
                ):
                    ok, err = _do_upload_for(sid)
                    if ok:
                        try:
                            resp = athathi_proxy.complete_scan(token, sid)
                        except athathi_proxy.AthathiError as e2:
                            out.append({
                                'scan_id': sid, 'status': 'failed',
                                'stage': 'complete',
                                'error': str(e2)[:200],
                            })
                            try:
                                m_err = _projects.read_manifest(sid)
                                if isinstance(m_err, dict):
                                    m_err['submit_pending_error'] = (
                                        str(e2)[:400])
                                    m_err['submit_pending_stage'] = 'complete'
                                    _projects.write_manifest(sid, m_err)
                            except OSError:
                                pass
                            continue
                        runs = gather_runs_for_submit(sid)
                        stamp_submit_outcome(
                            sid, runs=runs, response=resp,
                            error=None, queued=False,
                        )
                        # Clear the stage marker too.
                        try:
                            m_done = _projects.read_manifest(sid)
                            if isinstance(m_done, dict):
                                m_done.pop('submit_pending_stage', None)
                                _projects.write_manifest(sid, m_done)
                        except OSError:
                            pass
                        out.append({
                            'scan_id': sid, 'status': 'submitted',
                            'stage': 'upload+complete',
                        })
                        continue
                    # Upload retry failed too — fall through to the generic
                    # failure handler with the original complete error.
                out.append({
                    'scan_id': sid, 'status': 'failed',
                    'error': str(e)[:200],
                })
                try:
                    m_err = _projects.read_manifest(sid)
                    if isinstance(m_err, dict):
                        m_err['submit_pending_error'] = str(e)[:400]
                        if stage:
                            m_err['submit_pending_stage'] = stage
                        _projects.write_manifest(sid, m_err)
                except OSError:
                    pass
                continue

            runs = gather_runs_for_submit(sid)
            stamp_submit_outcome(
                sid, runs=runs, response=resp, error=None, queued=False,
            )
            # Clear stage marker on success.
            try:
                m_done = _projects.read_manifest(sid)
                if isinstance(m_done, dict):
                    m_done.pop('submit_pending_stage', None)
                    _projects.write_manifest(sid, m_done)
            except OSError:
                pass
            out.append({'scan_id': sid, 'status': 'submitted',
                        'stage': stage or 'complete'})

    return out


class _NullLock:
    """No-op context manager used when no lock_provider is supplied."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


# ---------------------------------------------------------------------------
# Backward-compat re-exports — kept so future code doesn't have to import
# both `submit` and `review`/`projects` for trivially-related symbols.
# ---------------------------------------------------------------------------

# Silence unused-import lint when shlex isn't actually used; it's reserved
# for a future shell-quoting branch in run_post_submit_hook should we ever
# accept multi-arg hook strings.
_ = shlex
