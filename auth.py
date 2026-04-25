"""Athathi auth + config + token persistence — local-only.

Single source of truth for:
  - `<ATHATHI_DIR>/config.json`  (technician-editable settings)
  - `<ATHATHI_DIR>/token`        (raw JWT, chmod 600)
  - `<ATHATHI_DIR>/auth.json`    (cached login envelope, chmod 600)
  - `<ATHATHI_DIR>/learned_classes.json`  (auto-grown taxonomy, chmod 644)

Also exposes a stdlib-only JWT payload decoder (no signature verification —
we only read the `exp` claim; verification stays on the Athathi server).

This module performs **zero** network I/O. The Athathi proxy that calls
`/api/users/login/`, `/api/technician/scans/schedule/`, etc. lives in a
separate module (added in Step 2 of the technician review plan).

Directory resolution:

  - Default: `/mnt/slam_data/.athathi` when `/mnt/slam_data` is a real mount
    (mirrors the existing `RECORDINGS_DIR` / `PROCESSED_DIR` fallback in
    `app.py`).
  - Dev fallback: `<repo>/.athathi`.
  - Test override: set the environment variable `ATHATHI_DIR_OVERRIDE` BEFORE
    importing this module; the override takes precedence over both. The env
    var is read once at import time (not on every call) so tests should set
    it via `mock.patch.dict(os.environ, ...)` and re-import via `importlib`.

Public surface (see plan §3 + §22d):

    ATHATHI_DIR, CONFIG_PATH, TOKEN_PATH, AUTH_PATH, LEARNED_CLASSES_PATH
    DEFAULT_CONFIG
    load_config(), save_config(), update_config(**kwargs)
    read_token(), write_token(), clear_token()
    read_auth(), write_auth()
    decode_jwt_payload(), jwt_expired(), is_logged_in()
    boot_init()
"""

import base64
import json
import os
import sys
import time as _time
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _resolve_athathi_dir():
    """Pick the directory we keep all Athathi state in.

    Resolution order (first match wins):
      1. ATHATHI_DIR_OVERRIDE env var (used by tests).
      2. /mnt/slam_data/.athathi  (when /mnt/slam_data is mounted).
      3. <repo>/.athathi          (dev / CI fallback).
    """
    override = os.environ.get('ATHATHI_DIR_OVERRIDE')
    if override:
        return override
    if os.path.isdir('/mnt/slam_data'):
        return '/mnt/slam_data/.athathi'
    return os.path.join(_SCRIPT_DIR, '.athathi')


ATHATHI_DIR = _resolve_athathi_dir()
CONFIG_PATH = os.path.join(ATHATHI_DIR, 'config.json')
TOKEN_PATH = os.path.join(ATHATHI_DIR, 'token')
AUTH_PATH = os.path.join(ATHATHI_DIR, 'auth.json')
LEARNED_CLASSES_PATH = os.path.join(ATHATHI_DIR, 'learned_classes.json')

# Where the first-run defaults live in the repo.
_DEFAULTS_FILE = os.path.join(_SCRIPT_DIR, 'auth_config_defaults.json')


DEFAULT_CONFIG = {
    "api_url":          "http://116.203.199.113:8002",
    "upload_endpoint":  "http://116.203.199.113:8002",
    "last_user":        "",
    "post_submit_hook": None,
    "image_transport":  "multipart",
    "visual_search_cache_ttl_s": 86400,
    # `visual_search_top_k`: how many candidate products the modal grid
    # is laid out for. The Athathi backend currently returns ~6 (no top_k
    # query param exposed) — we render whatever it sends, but reserve grid
    # slots up to this number so a future top_k bump just works.
    "visual_search_top_k": 6,
    # `visual_search_prefetch`: when true, the review screen kicks off a
    # background prefetch for every bbox the moment results load — modal
    # opens instantly because the result is already in the disk cache.
    "visual_search_prefetch": True,
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_dir():
    """Create ATHATHI_DIR with mode 0700 if missing.

    Best-effort: never raises. We chmod after mkdir because makedirs honours
    the user's umask and may produce a more permissive directory than we want.
    """
    try:
        os.makedirs(ATHATHI_DIR, exist_ok=True)
        try:
            os.chmod(ATHATHI_DIR, 0o700)
        except OSError:
            pass
    except OSError:
        pass


def _atomic_write(path, data, mode):
    """Write `data` (str) to `path` atomically, then chmod to `mode`.

    Pattern matches `app.py:_save_sessions` — write to `<path>.tmp`, then
    `os.replace`. The chmod runs after the rename so the final file ends up
    with the requested permissions even if the temp file was created with a
    looser umask.
    """
    _ensure_dir()
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(data)
    os.replace(tmp, path)
    try:
        os.chmod(path, mode)
    except OSError:
        # Don't fail a write because the chmod failed — the file is on disk
        # and that is what callers asked for.
        pass


def _strip_trailing_slash(value):
    if isinstance(value, str):
        return value.rstrip('/')
    return value


# ---------------------------------------------------------------------------
# Config (config.json)
# ---------------------------------------------------------------------------

def _load_seed_defaults():
    """Build the first-run seed dict.

    The repo-side `auth_config_defaults.json` is the editable source of truth;
    operators can tweak it before deployment. The in-code `DEFAULT_CONFIG` is
    a hardcoded safety net that ships with the codebase. We deep-merge the
    file ON TOP of the in-code dict so any keys the file specifies override
    the in-code values, while keys missing from the file fall through to the
    in-code defaults. If the file is missing or unreadable / malformed we log
    a warning and return a plain copy of `DEFAULT_CONFIG`.
    """
    seed = dict(DEFAULT_CONFIG)
    if not os.path.isfile(_DEFAULTS_FILE):
        return seed
    try:
        with open(_DEFAULTS_FILE, 'r') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(
            f"[athathi] load_config: failed to read {_DEFAULTS_FILE}: {e!r}; "
            f"falling back to in-code DEFAULT_CONFIG",
            file=sys.stderr,
        )
        return seed
    if not isinstance(data, dict):
        print(
            f"[athathi] load_config: {_DEFAULTS_FILE} is not a JSON object; "
            f"falling back to in-code DEFAULT_CONFIG",
            file=sys.stderr,
        )
        return seed
    # Shallow merge is sufficient — the config schema is one level deep.
    for k, v in data.items():
        seed[k] = v
    return seed


def load_config():
    """Return the current config dict.

    On first call (file missing), seeds CONFIG_PATH from a deep merge of
    `auth_config_defaults.json` over `DEFAULT_CONFIG` (or just
    `DEFAULT_CONFIG` if the file is missing/corrupt) and returns a copy of
    the merged dict. On JSON parse error of the user file, returns a copy of
    `DEFAULT_CONFIG` without overwriting the (corrupt) on-disk file — that's
    a manual recovery situation, not something to silently nuke.
    """
    if not os.path.isfile(CONFIG_PATH):
        seed = _load_seed_defaults()
        save_config(seed)
        return dict(seed)
    try:
        with open(CONFIG_PATH, 'r') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_CONFIG)
    if not isinstance(data, dict):
        return dict(DEFAULT_CONFIG)
    # Backfill missing keys from defaults so callers can rely on every key
    # being present without having to .get()-with-default everywhere.
    merged = dict(DEFAULT_CONFIG)
    for k, v in data.items():
        if k in DEFAULT_CONFIG:
            merged[k] = v
    return merged


def save_config(cfg):
    """Persist `cfg` to CONFIG_PATH atomically (chmod 0644).

    Trailing slashes on URL fields are stripped here so we never have to
    worry about `api_url + "/foo"` vs `api_url + "foo"` downstream.
    """
    if not isinstance(cfg, dict):
        raise TypeError('cfg must be a dict')
    out = dict(cfg)
    if 'api_url' in out:
        out['api_url'] = _strip_trailing_slash(out['api_url'])
    if 'upload_endpoint' in out:
        out['upload_endpoint'] = _strip_trailing_slash(out['upload_endpoint'])
    _atomic_write(CONFIG_PATH, json.dumps(out, indent=2) + '\n', 0o644)


def update_config(**kwargs):
    """Merge the kwargs into the on-disk config and return the full dict.

    Unknown keys (anything not in `DEFAULT_CONFIG`) raise ValueError so a
    typo in Settings → App tab doesn't silently stash junk in config.json.
    """
    unknown = set(kwargs) - set(DEFAULT_CONFIG)
    if unknown:
        raise ValueError(
            f'unknown config key(s): {sorted(unknown)}. '
            f'allowed: {sorted(DEFAULT_CONFIG)}'
        )
    cfg = load_config()
    cfg.update(kwargs)
    save_config(cfg)
    # Re-read so the caller sees what's actually on disk (URL slashes
    # already stripped, etc.).
    return load_config()


# ---------------------------------------------------------------------------
# Token (`token`)
# ---------------------------------------------------------------------------

def read_token():
    """Return the raw JWT, or None if missing / unreadable."""
    if not os.path.isfile(TOKEN_PATH):
        return None
    try:
        with open(TOKEN_PATH, 'r') as f:
            tok = f.read().strip()
        return tok or None
    except OSError:
        return None


def write_token(token):
    """Persist `token` to TOKEN_PATH atomically (chmod 0600)."""
    if not isinstance(token, str) or not token:
        raise ValueError('token must be a non-empty string')
    _atomic_write(TOKEN_PATH, token, 0o600)


def clear_token():
    """Best-effort unlink of TOKEN_PATH and AUTH_PATH.

    Used by both an explicit logout and the boot-time "JWT expired → drop
    it" path. Missing files are not an error.
    """
    for p in (TOKEN_PATH, AUTH_PATH):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Cached login envelope (`auth.json`)
# ---------------------------------------------------------------------------

def read_auth():
    """Return the cached login response envelope, or None.

    The envelope is what the Athathi `/api/users/login/` body contained
    (minus the token, which lives in TOKEN_PATH). Typical keys per plan §3b:
    `user_id`, `username`, `user_type`.
    """
    if not os.path.isfile(AUTH_PATH):
        return None
    try:
        with open(AUTH_PATH, 'r') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def write_auth(payload):
    """Persist the login envelope (chmod 0600 — it has user_id etc.)."""
    if not isinstance(payload, dict):
        raise TypeError('payload must be a dict')
    _atomic_write(AUTH_PATH, json.dumps(payload, indent=2) + '\n', 0o600)


# ---------------------------------------------------------------------------
# JWT helpers (no signature verification)
# ---------------------------------------------------------------------------

def decode_jwt_payload(token):
    """Return the JWT payload as a dict, or `{}` on any parse failure.

    We only need the `exp` claim to gate the login screen; the actual
    signature verification happens on the Athathi server. Doing it here
    would mean shipping the secret to the Pi, which we explicitly don't.
    """
    if not isinstance(token, str) or not token:
        return {}
    parts = token.split('.')
    if len(parts) < 2:
        return {}
    seg = parts[1]
    # base64url padding fix.
    seg = seg + '=' * (-len(seg) % 4)
    try:
        raw = base64.urlsafe_b64decode(seg.encode('ascii'))
    except (ValueError, TypeError, base64.binascii.Error):
        return {}
    try:
        data = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def jwt_expired(token, now_ts=None):
    """True if the token's `exp` claim is missing or in the past.

    `now_ts` is injectable so tests don't have to monkey-patch `time.time`.
    """
    payload = decode_jwt_payload(token)
    if not payload:
        return True
    exp = payload.get('exp')
    if not isinstance(exp, (int, float)):
        return True
    if now_ts is None:
        now_ts = _time.time()
    return now_ts >= exp


def is_logged_in():
    """True iff a token is on disk AND it hasn't expired."""
    tok = read_token()
    if not tok:
        return False
    return not jwt_expired(tok)


# ---------------------------------------------------------------------------
# Boot init (called from app.py main)
# ---------------------------------------------------------------------------

def _exp_iso(token):
    """Best-effort ISO-8601 timestamp for the `exp` claim, or '?'."""
    payload = decode_jwt_payload(token)
    exp = payload.get('exp') if isinstance(payload, dict) else None
    if not isinstance(exp, (int, float)):
        return '?'
    try:
        return datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return '?'


def boot_init():
    """Startup hook — must be safe to call without network or disk errors.

    Responsibilities:
      1. Make sure ATHATHI_DIR exists (mode 0700).
      2. Seed CONFIG_PATH with `DEFAULT_CONFIG` if it doesn't exist yet.
      3. Print a one-line status summary so `journalctl -u slam` shows
         whether the technician is logged in without grepping a token file.

    NEVER:
      - Calls the network. The validity probe (a `GET /schedule/`) belongs
        to the proxy in Step 2.
      - Raises. A boot hook that crashes the app is worse than a boot hook
        that silently does nothing.
    """
    try:
        _ensure_dir()
    except OSError as e:
        # Belt and braces — _ensure_dir already swallows OSError, but a
        # boot hook must never propagate. We narrow to OSError specifically
        # so a future programming bug (NameError, TypeError) bubbles up
        # during testing instead of being silently swallowed.
        print(
            f"[athathi] boot_init: ensure_dir failed: {e!r}",
            file=sys.stderr,
        )

    # Seed config defaults on first run. `save_config` is an atomic-write
    # path, so OSError covers the realistic failure modes (full disk, RO FS,
    # permission denied). Keep `_load_seed_defaults` outside the try so that
    # boot_init seeds from the same merged-defaults source as `load_config`.
    try:
        if not os.path.isfile(CONFIG_PATH):
            save_config(_load_seed_defaults())
    except OSError as e:
        print(
            f"[athathi] boot_init: seed default config failed: {e!r}",
            file=sys.stderr,
        )

    # Status line. read_auth() / read_token() already return None on
    # garbage, so what's left to fail is print(), datetime.fromtimestamp()
    # (OSError / OverflowError / ValueError on bogus exp values),
    # dict indexing on a non-dict surfacing through a helper, and string
    # formatting. Keep the catch narrow enough that a real programming bug
    # (e.g. a NameError introduced by a refactor) still propagates and is
    # caught by tests.
    try:
        tok = read_token()
        if tok and not jwt_expired(tok):
            auth = read_auth() or {}
            user = auth.get('username') or '<unknown>'
            print(f"[athathi] logged in as {user} "
                  f"(token expires {_exp_iso(tok)})")
        else:
            print("[athathi] no session — login required")
    except (OSError, ValueError, TypeError, KeyError) as e:
        print(
            f"[athathi] boot_init: status line failed: {e!r}",
            file=sys.stderr,
        )
