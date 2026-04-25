"""Pi-side HTTP forwarder for the Athathi backend.

This module is a pure HTTP proxy: every public function makes one request
against the Athathi API (`auth.load_config()['api_url']`) using `subprocess.run`
to invoke `curl` — exactly like the existing `app._modal_*` helpers do. We keep
the dependency story lean: the production environment already has `curl` in
`$PATH`, no extra Python packages needed.

Public surface (see plan §3c, §22d, §23a):

    AthathiError                       — raised on any non-2xx / network failure
    login(username, password)          — POST /api/users/login/  (returns body)
    logout(token)                      — POST /api/users/logout/ (best-effort)
    get_schedule(token)                — GET  /api/technician/scans/schedule/
    get_history(token)                 — GET  /api/technician/scans/history/
    complete_scan(token, scan_id)      — POST /api/technician/scans/{id}/complete/
    cancel_scan(token, scan_id)        — POST /api/technician/scans/{id}/cancel/
    get_categories(token)              — GET  /api/categories/
    visual_search_full(token, image)   — POST /api/visual-search/search-full/
    cached_get(key, ttl, fetch_fn)     — on-disk LRU with stale-on-error fallback
    stream_artifact(...)               — placeholder; raises NotImplementedError

Discipline:
  - No `requests` library; all I/O via `curl` for parity with `app._modal_*`.
  - `auth.load_config()` is read on EVERY call — the operator may change
    `api_url` from Settings while the app is running.
  - Authenticated calls use `Authorization: Bearer <token>` (NOT `X-API-Key`).
  - `Accept: application/json` is set on every call.
  - Connect-timeout is 5 s; total timeout per the function spec.
  - On `curl returncode != 0` → `AthathiError(0, "<stderr tail>", "Network error")`.
  - On non-2xx HTTP → `AthathiError(<status>, body, "<reason>")`.
  - Retries are ONLY on the routes that opt in (complete/cancel) — never on
    login, schedule, history, categories, visual-search.
  - Per-(endpoint+token) cache lives at `<auth.ATHATHI_DIR>/cache/<key>.json`
    with a 5-minute default TTL. A network failure with a fresh-enough cache
    falls back to the cache so an offline Pi still shows the last schedule.
"""

import hashlib
import json
import os
import subprocess
import sys
import time as _time
from datetime import datetime, timezone

import auth


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class AthathiError(Exception):
    """Raised when an upstream Athathi call fails.

    `status_code == 0` is reserved for network / DNS / refused / curl errors
    (anything where we never got a response). HTTP error codes are mirrored
    verbatim. `body` is the raw response body (or curl stderr tail for
    network failures), capped at a few hundred chars.
    """

    def __init__(self, status_code, body, message=""):
        self.status_code = int(status_code)
        self.body = body or ""
        if not message:
            message = f"Athathi {self.status_code}: {self.body[:200]}"
        super().__init__(message)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Default connect timeout (handshake) in seconds. Total timeouts are per-call.
_CONNECT_TIMEOUT_S = 5

# Default cache TTL for read-only endpoints (schedule / history / categories).
_CACHE_TTL_S = 300


def _api_url():
    """Read `api_url` from on-disk config every call (operator may have edited it)."""
    cfg = auth.load_config() or {}
    return (cfg.get('api_url') or '').rstrip('/')


def _parse_status_and_body(stdout):
    """Split `curl -w '\\n%{http_code}'` output into (status:int, body:str).

    The `-w '\\n%{http_code}'` flag appends a final line with the HTTP status
    code, e.g. `<body>\\n200`. We split on the LAST newline so JSON bodies
    that contain newlines are preserved verbatim.

    Returns (None, raw) if we can't parse the status line — the caller should
    treat that as a malformed upstream response and raise.
    """
    if not stdout:
        return None, ''
    body, _, code = stdout.rpartition('\n')
    try:
        return int(code.strip()), body
    except (ValueError, AttributeError):
        return None, stdout


def _curl_json(method, url, *, token=None, body=None, timeout=15,
               extra_args=None):
    """Run a `curl` command and return (status:int, body:str).

    On `curl returncode != 0` raises `AthathiError(0, ...)`. On a 2xx the
    caller does the JSON parse — we only do the HTTP envelope.

    Args:
      method: HTTP method (GET/POST/...).
      url: full URL.
      token: optional bearer token (no leading "Bearer").
      body: optional JSON-serialisable dict; sent with `Content-Type:
            application/json` if provided.
      timeout: total subprocess timeout (incl. connect).
      extra_args: optional list of additional curl args (e.g. `['-F', 'file=@/x']`).
    """
    args = [
        'curl', '-sS', '-X', method,
        '--connect-timeout', str(_CONNECT_TIMEOUT_S),
        '--max-time', str(timeout),
        '-H', 'Accept: application/json',
        '-w', '\n%{http_code}',
    ]
    if token:
        args += ['-H', f'Authorization: Bearer {token}']
    if body is not None:
        args += [
            '-H', 'Content-Type: application/json',
            '--data', json.dumps(body),
        ]
    if extra_args:
        args += list(extra_args)
    args.append(url)

    try:
        res = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout + 2,
        )
    except subprocess.TimeoutExpired as e:
        raise AthathiError(
            0, f'subprocess timeout: {e!s}'[:400], 'Network error',
        )
    if res.returncode != 0:
        tail = (res.stderr or '').strip()
        raise AthathiError(0, tail[-400:], 'Network error')

    status, body_text = _parse_status_and_body(res.stdout or '')
    if status is None:
        raise AthathiError(
            0, (res.stdout or '')[:400],
            'Network error: could not parse HTTP status',
        )
    return status, body_text


def _ensure_2xx(status, body, default_reason=''):
    """Raise AthathiError if status is not 2xx."""
    if 200 <= status < 300:
        return
    reason = default_reason
    if not reason:
        if status == 401:
            reason = 'Unauthorized'
        elif status == 403:
            reason = 'Forbidden'
        elif status == 404:
            reason = 'Not found'
        elif 400 <= status < 500:
            reason = f'HTTP {status}'
        else:
            reason = f'Upstream {status}'
    raise AthathiError(status, body, reason)


def _parse_json(body):
    """Parse a JSON body or raise AthathiError(0, ...) — used for 2xx replies."""
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise AthathiError(
            0, body[:400], f'Upstream returned non-JSON: {e!s}',
        )


def _retry_post(method, url, *, token, timeout, attempts=3):
    """POST with up to `attempts` tries, exponential backoff 1/2/4 s on 5xx.

    Used by complete_scan and cancel_scan. The path is constant so retrying
    is idempotency-friendly even though there's no server-side dedupe key.
    """
    last_err = None
    for attempt in range(attempts):
        if attempt > 0:
            _time.sleep(2 ** (attempt - 1))  # 1, 2, 4 s
        try:
            status, body = _curl_json(
                method, url, token=token, timeout=timeout,
            )
        except AthathiError as e:
            # Network errors are NOT retried here — match the _modal_submit
            # convention (treat curl-rc-zero as a hard fail). Schedule/history
            # have the cached_get fallback for that scenario.
            if e.status_code == 0:
                raise
            last_err = e
            continue
        if 500 <= status < 600:
            last_err = AthathiError(status, body, f'Upstream {status}')
            continue
        _ensure_2xx(status, body)
        return _parse_json(body)
    # Exhausted retries
    raise last_err if last_err else AthathiError(0, '', 'Retry budget exhausted')


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def login(username, password):
    """POST /api/users/login/ with JSON body. Returns the parsed body dict.

    The successful response shape (per plan §23a):
      {message, user: {...}, token: "..."}

    On 401 raises AthathiError(401, body, "Invalid credentials").
    No retries — login is interactive; the user can re-tap.
    """
    if not isinstance(username, str) or not isinstance(password, str):
        raise TypeError('username/password must be strings')
    url = f'{_api_url()}/api/users/login/'
    status, body = _curl_json(
        'POST', url, body={'username': username, 'password': password},
        timeout=15,
    )
    if status == 401:
        raise AthathiError(401, body, 'Invalid credentials')
    _ensure_2xx(status, body)
    return _parse_json(body)


def logout(token):
    """POST /api/users/logout/ with Bearer token. Best-effort — never raises.

    Logout is anchored locally: we always clear `token` and `auth.json` after
    this call returns. If the upstream call fails (network down, server says
    401, etc.) we log to stderr and move on.
    """
    if not token:
        return None
    url = f'{_api_url()}/api/users/logout/'
    try:
        _curl_json('POST', url, token=token, timeout=10)
    except AthathiError as e:
        # Best-effort — never surface a logout failure.
        print(f'[athathi_proxy] logout: best-effort failure: {e!r}',
              file=sys.stderr)
    return None


def get_schedule(token):
    """GET /api/technician/scans/schedule/. Returns the bare list verbatim."""
    url = f'{_api_url()}/api/technician/scans/schedule/'
    status, body = _curl_json('GET', url, token=token, timeout=15)
    _ensure_2xx(status, body)
    data = _parse_json(body)
    # Per §23a the upstream returns a bare array. Be defensive: accept dict
    # too (in case the contract evolves) but pass it through verbatim.
    return data


def get_history(token):
    """GET /api/technician/scans/history/. Returns the bare list verbatim."""
    url = f'{_api_url()}/api/technician/scans/history/'
    status, body = _curl_json('GET', url, token=token, timeout=15)
    _ensure_2xx(status, body)
    return _parse_json(body)


def complete_scan(token, scan_id):
    """POST /api/technician/scans/<scan_id>/complete/ with no body.

    Up to 3 retries on 5xx with exponential backoff (1/2/4 s). 15 s total
    timeout per attempt.
    """
    url = f'{_api_url()}/api/technician/scans/{int(scan_id)}/complete/'
    return _retry_post('POST', url, token=token, timeout=15, attempts=3)


def cancel_scan(token, scan_id):
    """POST /api/technician/scans/<scan_id>/cancel/. Same retry profile as complete."""
    url = f'{_api_url()}/api/technician/scans/{int(scan_id)}/cancel/'
    return _retry_post('POST', url, token=token, timeout=15, attempts=3)


def get_categories(token):
    """GET /api/categories/. Returns {categories: [{id, name, description}, ...]}."""
    url = f'{_api_url()}/api/categories/'
    status, body = _curl_json('GET', url, token=token, timeout=15)
    _ensure_2xx(status, body)
    return _parse_json(body)


def visual_search_full(token, image_path):
    """POST /api/visual-search/search-full/ multipart with field name `file`.

    Returns the parsed body — the locked 12-field schema lives under
    `results[].linked_product_*`. 30 s timeout (server reports ~2 s typical).
    No retries — the user can re-tap on failure.
    """
    if not os.path.isfile(image_path):
        raise AthathiError(0, f'image file not found: {image_path}',
                           'visual_search_full: image missing')
    url = f'{_api_url()}/api/visual-search/search-full/'
    status, body = _curl_json(
        'POST', url, token=token, timeout=30,
        extra_args=['-F', f'file=@{image_path}'],
    )
    _ensure_2xx(status, body)
    return _parse_json(body)


def stream_artifact(token, scan_id, artifact_name):
    """Placeholder for later — Step 2 doesn't need this."""
    raise NotImplementedError(
        'stream_artifact is a placeholder; wire up when the artifact endpoints '
        'land in a later step.'
    )


# ---------------------------------------------------------------------------
# Submit-pipeline multipart upload (plan §8 / §21d / step 6)
# ---------------------------------------------------------------------------

import tempfile  # noqa: E402  (kept local to highlight this is submit-only)


def upload_bundle(token, upload_endpoint, envelope_json_bytes, image_files):
    """POST a multipart bundle to `upload_endpoint`.

    The caller has already rendered the upload envelope and gathered the
    on-disk image paths; this function only builds the curl invocation.

    Args:
      token: Bearer JWT (string). Sent as `Authorization: Bearer <token>`.
      upload_endpoint: literal URL to POST to. Used verbatim — the caller
        is responsible for any path under the configured base URL.
      envelope_json_bytes: raw bytes of `result_for_upload.json`. Always
        sent as form field `envelope` with `type=application/json`.
      image_files: list of `(form_field_name, absolute_path)` tuples.
        Each is sent as `-F <field>=@<path>`.

    Returns:
      Parsed upstream JSON (dict).

    Retry profile (mirrors `complete_scan`):
      - Up to 3 attempts on 5xx with 1/2/4 s backoff.
      - 4xx is NOT retried (would just keep failing).
      - Network errors (curl rc != 0) raise AthathiError(0, ...) immediately.
      - Per-attempt timeout: 60 s (large file uploads).

    Raises:
      AthathiError on non-2xx (with status_code mirrored), or on network
      failure (status_code=0). The caller surfaces network failures as a
      "queue-and-retry-later" path; non-network 4xx is surfaced verbatim.
    """
    if not isinstance(token, str) or not token:
        raise AthathiError(0, 'token required', 'upload_bundle: missing token')
    if not isinstance(upload_endpoint, str) or not upload_endpoint:
        raise AthathiError(0, 'upload_endpoint required',
                           'upload_bundle: missing endpoint')
    if not isinstance(envelope_json_bytes, (bytes, bytearray)):
        raise TypeError('envelope_json_bytes must be bytes')
    if image_files is None:
        image_files = []

    # Materialise the envelope to a temp file so curl can send it as a
    # named multipart field with a content-type. Doing this via stdin
    # (`@-`) would conflict with the per-image `@<path>` directives.
    fd, env_tmp = tempfile.mkstemp(prefix='upload_envelope_', suffix='.json')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(envelope_json_bytes)

        extra_args = ['-F', f'envelope=@{env_tmp};type=application/json']
        for pair in image_files:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            field, path = pair
            if not isinstance(field, str) or not field:
                continue
            if not isinstance(path, str) or not path:
                continue
            extra_args += ['-F', f'{field}=@{path}']

        return _retry_post_multipart(
            upload_endpoint, token=token,
            extra_args=extra_args, timeout=60, attempts=3,
        )
    finally:
        try:
            os.unlink(env_tmp)
        except OSError:
            pass


def _retry_post_multipart(url, *, token, extra_args, timeout, attempts=3):
    """POST a multipart payload with up to `attempts` tries.

    Same retry profile as `_retry_post`: 1/2/4 s backoff on 5xx; 4xx and
    network errors raise immediately.
    """
    last_err = None
    for attempt in range(attempts):
        if attempt > 0:
            _time.sleep(2 ** (attempt - 1))  # 1, 2, 4 s
        try:
            status, body = _curl_json(
                'POST', url, token=token, timeout=timeout,
                extra_args=extra_args,
            )
        except AthathiError as e:
            # Network errors propagate immediately — the submit caller
            # queues for retry instead of burning the backoff budget here.
            if e.status_code == 0:
                raise
            last_err = e
            continue
        if 500 <= status < 600:
            last_err = AthathiError(status, body, f'Upstream {status}')
            continue
        _ensure_2xx(status, body)
        return _parse_json(body)
    raise last_err if last_err else AthathiError(
        0, '', 'Retry budget exhausted')


# ---------------------------------------------------------------------------
# On-disk LRU cache for read-only endpoints
# ---------------------------------------------------------------------------

def _cache_dir():
    """Return the cache directory under `auth.ATHATHI_DIR` (created on demand)."""
    d = os.path.join(auth.ATHATHI_DIR, 'cache')
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _cache_path(key):
    """Sanitised cache file path for `key` (alphanumerics + '_-.' only)."""
    safe = ''.join(c if (c.isalnum() or c in '_-.') else '_' for c in key)
    return os.path.join(_cache_dir(), safe + '.json')


def _cache_token_suffix(token):
    """First 8 hex chars of sha256(token) — keeps user A's cache off user B's view."""
    if not token:
        return 'anon'
    h = hashlib.sha256(token.encode('utf-8')).hexdigest()
    return h[:8]


def cache_key_for(endpoint, token):
    """Compose a cache key from `endpoint` + a short hash of the token."""
    return f'{endpoint}__{_cache_token_suffix(token)}'


def _read_cache(path):
    """Read a cache file. Returns (timestamp, blob) or (None, None) on miss."""
    if not os.path.isfile(path):
        return None, None
    try:
        with open(path, 'r') as f:
            wrapper = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(wrapper, dict):
        return None, None
    ts = wrapper.get('cached_at')
    blob = wrapper.get('blob')
    if not isinstance(ts, (int, float)):
        return None, None
    return ts, blob


def _write_cache(path, blob):
    """Persist `blob` to `path` with a fresh timestamp. Best-effort, never raises."""
    wrapper = {'cached_at': _time.time(), 'blob': blob}
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(wrapper, f)
        os.replace(tmp, path)
    except OSError as e:
        print(f'[athathi_proxy] cache write failed for {path}: {e!r}',
              file=sys.stderr)


class StaleCacheResult:
    """Sentinel wrapper returned by `cached_get` when serving a stale blob
    on network failure. Lets callers disambiguate without isinstance-on-tuple
    fragility (a blob that happened to be a 2-tuple of (anything, dict) would
    otherwise be misclassified).

    `fetched_at_iso` carries the ISO 8601 (UTC) timestamp of when the cache
    file was last written — i.e. the last successful upstream fetch. This
    lets the UI render an honest "last refreshed <ago>" banner instead of
    "just now" (which would be the case if it used the response timestamp).
    Defaults to `None` so legacy callers / tests that construct the sentinel
    by hand keep working.
    """
    __slots__ = ('blob', 'reason', 'fetched_at_iso')

    def __init__(self, blob, reason='network', fetched_at_iso=None):
        self.blob = blob
        self.reason = reason
        self.fetched_at_iso = fetched_at_iso


def cached_get(key, ttl, fetch_fn):
    """Disk-cached read. Calls `fetch_fn()` if cache is empty or stale.

    - Cache hit (fresh): return blob without calling `fetch_fn`.
    - Cache miss / stale: call `fetch_fn()`, persist its result, return it.
    - Cache stale + `fetch_fn` raises `AthathiError(status_code=0)`: serve
      the stale cache anyway so an offline Pi still has data.
      Returns a `StaleCacheResult(blob, reason='network')` in that case.
    - Cache miss + network failure: re-raise.
    - Non-network upstream errors (4xx/5xx): re-raise (do NOT serve stale).

    Returns either the fresh blob or a `StaleCacheResult` wrapper.
    Callers test with `isinstance(result, StaleCacheResult)`.
    """
    path = _cache_path(key)
    ts, cached_blob = _read_cache(path)
    now = _time.time()
    fresh = ts is not None and (now - ts) <= ttl

    if fresh:
        return cached_blob

    try:
        blob = fetch_fn()
    except AthathiError as e:
        if e.status_code == 0 and cached_blob is not None:
            print(
                f'[athathi_proxy] cached_get({key}): network fail, '
                f'serving stale cache',
                file=sys.stderr,
            )
            # Convert the cache-file's `cached_at` epoch to an ISO 8601 UTC
            # string so the frontend banner can render the actual last-refresh
            # time. Best-effort: malformed timestamps fall back to None.
            fetched_at_iso = None
            try:
                if isinstance(ts, (int, float)):
                    fetched_at_iso = datetime.fromtimestamp(
                        ts, tz=timezone.utc,
                    ).isoformat().replace('+00:00', 'Z')
            except (OverflowError, OSError, ValueError):
                fetched_at_iso = None
            return StaleCacheResult(
                cached_blob, reason='network',
                fetched_at_iso=fetched_at_iso,
            )
        raise

    _write_cache(path, blob)
    return blob
