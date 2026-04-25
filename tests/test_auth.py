"""Tests for the local auth + config + token persistence module (`auth.py`).

NO network calls — this whole layer is local-only by design (plan §3, Step 1).
The Athathi proxy that hits `/api/users/login/` etc. lives in a separate
module added in Step 2 and has its own test file.

Strategy:
  Each test class points `auth.ATHATHI_DIR` (and the derived path constants)
  at a fresh `tempfile.TemporaryDirectory`. We can't just rely on
  `ATHATHI_DIR_OVERRIDE` because that env var is read once at import time —
  fine for the production resolution path, but tests run after import. We
  patch the module-level constants directly in `setUp` and restore them in
  `tearDown`. `auth.py`'s helpers all read these constants at call time,
  so this works.
"""

import base64
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

# Make the repo importable regardless of where pytest is invoked from.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import auth  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_jwt(payload, header=None, sig=b'sig'):
    """Build a JWT-shaped token (header.payload.sig) — signature is garbage.

    `auth.decode_jwt_payload` doesn't verify signatures, so we don't need a
    real one. We DO need correct base64url-without-padding encoding so the
    decoder's padding fix is exercised.
    """
    if header is None:
        header = {'alg': 'HS256', 'typ': 'JWT'}

    def b64url(b):
        return base64.urlsafe_b64encode(b).rstrip(b'=').decode('ascii')

    h = b64url(json.dumps(header).encode('utf-8'))
    p = b64url(json.dumps(payload).encode('utf-8'))
    s = b64url(sig)
    return f'{h}.{p}.{s}'


# ---------------------------------------------------------------------------
# Base test case — redirects ATHATHI_DIR to a tempdir
# ---------------------------------------------------------------------------

class _AuthTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = {
            'ATHATHI_DIR':           auth.ATHATHI_DIR,
            'CONFIG_PATH':           auth.CONFIG_PATH,
            'TOKEN_PATH':            auth.TOKEN_PATH,
            'AUTH_PATH':             auth.AUTH_PATH,
            'LEARNED_CLASSES_PATH':  auth.LEARNED_CLASSES_PATH,
        }
        auth.ATHATHI_DIR = self.tmp
        auth.CONFIG_PATH = os.path.join(self.tmp, 'config.json')
        auth.TOKEN_PATH = os.path.join(self.tmp, 'token')
        auth.AUTH_PATH = os.path.join(self.tmp, 'auth.json')
        auth.LEARNED_CLASSES_PATH = os.path.join(self.tmp, 'learned_classes.json')

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(auth, k, v)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# load_config / save_config / update_config
# ---------------------------------------------------------------------------

class TestConfig(_AuthTestBase):
    def test_load_config_seeds_defaults_on_first_call(self):
        # File doesn't exist yet.
        self.assertFalse(os.path.isfile(auth.CONFIG_PATH))
        cfg = auth.load_config()
        self.assertEqual(cfg, auth.DEFAULT_CONFIG)
        # And it persisted them so a textfile-friendly edit is now possible.
        self.assertTrue(os.path.isfile(auth.CONFIG_PATH))
        with open(auth.CONFIG_PATH) as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk, auth.DEFAULT_CONFIG)

    def test_load_config_returns_existing_without_rewriting(self):
        custom = dict(auth.DEFAULT_CONFIG)
        custom['last_user'] = 'alice'
        with open(auth.CONFIG_PATH, 'w') as f:
            json.dump(custom, f)
        mtime_before = os.path.getmtime(auth.CONFIG_PATH)
        # Sleep a hair so any spurious rewrite would bump mtime.
        time.sleep(0.01)
        cfg = auth.load_config()
        self.assertEqual(cfg['last_user'], 'alice')
        mtime_after = os.path.getmtime(auth.CONFIG_PATH)
        self.assertEqual(mtime_before, mtime_after)

    def test_load_config_handles_corrupt_file(self):
        with open(auth.CONFIG_PATH, 'w') as f:
            f.write('{not json')
        cfg = auth.load_config()
        # Returns defaults but does NOT clobber the corrupt file (manual fix).
        self.assertEqual(cfg, auth.DEFAULT_CONFIG)
        with open(auth.CONFIG_PATH) as f:
            self.assertEqual(f.read(), '{not json')

    def test_load_config_backfills_missing_keys(self):
        partial = {'api_url': 'http://example.com'}
        with open(auth.CONFIG_PATH, 'w') as f:
            json.dump(partial, f)
        cfg = auth.load_config()
        # Caller can rely on every default key being present.
        for k in auth.DEFAULT_CONFIG:
            self.assertIn(k, cfg)
        self.assertEqual(cfg['api_url'], 'http://example.com')

    def test_update_config_strips_trailing_slash(self):
        new = auth.update_config(api_url='http://x.example.com/')
        self.assertEqual(new['api_url'], 'http://x.example.com')
        # And it persisted that way.
        with open(auth.CONFIG_PATH) as f:
            self.assertEqual(json.load(f)['api_url'], 'http://x.example.com')

    def test_update_config_strips_upload_endpoint_slash(self):
        new = auth.update_config(upload_endpoint='http://up.example.com/')
        self.assertEqual(new['upload_endpoint'], 'http://up.example.com')

    def test_update_config_persists_and_returns_full_dict(self):
        new = auth.update_config(last_user='bob')
        self.assertEqual(new['last_user'], 'bob')
        # All other keys retained at their defaults.
        for k, v in auth.DEFAULT_CONFIG.items():
            if k != 'last_user':
                self.assertEqual(new[k], v)

    def test_update_config_rejects_unknown_keys(self):
        with self.assertRaises(ValueError):
            auth.update_config(definitely_not_a_real_key=42)

    def test_save_config_creates_dir_if_missing(self):
        # Whack the dir first.
        import shutil
        shutil.rmtree(self.tmp)
        auth.save_config(auth.DEFAULT_CONFIG)
        self.assertTrue(os.path.isdir(self.tmp))
        self.assertTrue(os.path.isfile(auth.CONFIG_PATH))


# ---------------------------------------------------------------------------
# `_DEFAULTS_FILE` (auth_config_defaults.json) seed precedence
# ---------------------------------------------------------------------------

class TestDefaultsFile(_AuthTestBase):
    """The repo-side defaults file is the editable source of truth.

    On first boot it gets deep-merged ON TOP of the in-code `DEFAULT_CONFIG`
    and written to CONFIG_PATH. The in-code dict stays as a fallback for the
    case where the file is missing or unparseable.
    """

    def setUp(self):
        super().setUp()
        # Stash and override _DEFAULTS_FILE so we don't touch the real one.
        self._orig_defaults_file = auth._DEFAULTS_FILE
        self._defaults_path = os.path.join(self.tmp, 'auth_config_defaults.json')
        auth._DEFAULTS_FILE = self._defaults_path

    def tearDown(self):
        auth._DEFAULTS_FILE = self._orig_defaults_file
        super().tearDown()

    def test_defaults_file_overrides_in_code_on_first_seed(self):
        # File overrides only `api_url` and `last_user`; the rest must come
        # from the in-code DEFAULT_CONFIG.
        overrides = {
            'api_url': 'http://override.example.com:9000',
            'last_user': 'preset-tech',
        }
        with open(self._defaults_path, 'w') as f:
            json.dump(overrides, f)
        self.assertFalse(os.path.isfile(auth.CONFIG_PATH))

        cfg = auth.load_config()

        # File-provided keys win.
        self.assertEqual(cfg['api_url'], 'http://override.example.com:9000')
        self.assertEqual(cfg['last_user'], 'preset-tech')
        # In-code defaults fill in for keys the file didn't specify.
        self.assertEqual(
            cfg['upload_endpoint'],
            auth.DEFAULT_CONFIG['upload_endpoint'],
        )
        self.assertEqual(
            cfg['image_transport'],
            auth.DEFAULT_CONFIG['image_transport'],
        )
        self.assertEqual(
            cfg['visual_search_cache_ttl_s'],
            auth.DEFAULT_CONFIG['visual_search_cache_ttl_s'],
        )
        self.assertEqual(
            cfg['post_submit_hook'],
            auth.DEFAULT_CONFIG['post_submit_hook'],
        )

        # And the merged dict was persisted to CONFIG_PATH so a textfile-
        # friendly edit is now possible.
        self.assertTrue(os.path.isfile(auth.CONFIG_PATH))
        with open(auth.CONFIG_PATH) as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk['api_url'], 'http://override.example.com:9000')
        self.assertEqual(on_disk['last_user'], 'preset-tech')
        self.assertEqual(
            on_disk['upload_endpoint'],
            auth.DEFAULT_CONFIG['upload_endpoint'],
        )

    def test_defaults_file_corrupt_falls_back_to_in_code(self):
        # Corrupt JSON — must not raise, must seed from in-code defaults,
        # and must log a warning so the failure isn't silent.
        with open(self._defaults_path, 'w') as f:
            f.write('{not json,,,')
        self.assertFalse(os.path.isfile(auth.CONFIG_PATH))

        err_buf = io.StringIO()
        out_buf = io.StringIO()
        with redirect_stdout(out_buf), \
                mock.patch.object(sys, 'stderr', err_buf):
            cfg = auth.load_config()

        # Falls back cleanly to the in-code DEFAULT_CONFIG.
        self.assertEqual(cfg, auth.DEFAULT_CONFIG)
        # And persisted those exact defaults.
        with open(auth.CONFIG_PATH) as f:
            self.assertEqual(json.load(f), auth.DEFAULT_CONFIG)
        # A warning hit stderr (don't pin the exact wording — just that it
        # mentions the defaults file).
        warn = err_buf.getvalue()
        self.assertIn('auth_config_defaults.json', warn)

    def test_defaults_file_missing_uses_in_code(self):
        # No defaults file at all — load_config seeds from in-code
        # DEFAULT_CONFIG without warning (this is the "we shipped without
        # a repo-side overrides file" path, fully expected).
        self.assertFalse(os.path.isfile(self._defaults_path))
        self.assertFalse(os.path.isfile(auth.CONFIG_PATH))
        cfg = auth.load_config()
        self.assertEqual(cfg, auth.DEFAULT_CONFIG)
        with open(auth.CONFIG_PATH) as f:
            self.assertEqual(json.load(f), auth.DEFAULT_CONFIG)


# ---------------------------------------------------------------------------
# Token round-trip
# ---------------------------------------------------------------------------

class TestToken(_AuthTestBase):
    def test_read_missing_token(self):
        self.assertIsNone(auth.read_token())

    def test_token_round_trip_and_perms(self):
        auth.write_token('abc.def.ghi')
        self.assertEqual(auth.read_token(), 'abc.def.ghi')
        mode = stat.S_IMODE(os.stat(auth.TOKEN_PATH).st_mode)
        # 0o600 — owner read/write only.
        self.assertEqual(mode, stat.S_IRUSR | stat.S_IWUSR)

    def test_write_token_rejects_empty(self):
        with self.assertRaises(ValueError):
            auth.write_token('')
        with self.assertRaises(ValueError):
            auth.write_token(None)  # type: ignore[arg-type]

    def test_clear_token_removes_both_files(self):
        auth.write_token('xx.yy.zz')
        auth.write_auth({'user_id': 1, 'username': 'eve'})
        auth.clear_token()
        self.assertFalse(os.path.exists(auth.TOKEN_PATH))
        self.assertFalse(os.path.exists(auth.AUTH_PATH))

    def test_clear_token_tolerates_missing_files(self):
        # Neither file exists yet — should not raise.
        auth.clear_token()
        self.assertFalse(os.path.exists(auth.TOKEN_PATH))
        self.assertFalse(os.path.exists(auth.AUTH_PATH))

    def test_clear_token_tolerates_one_missing(self):
        auth.write_token('only.token.here')
        # auth.json absent → should still remove token without raising.
        auth.clear_token()
        self.assertFalse(os.path.exists(auth.TOKEN_PATH))


# ---------------------------------------------------------------------------
# Auth envelope round-trip
# ---------------------------------------------------------------------------

class TestAuthEnvelope(_AuthTestBase):
    def test_read_missing_auth(self):
        self.assertIsNone(auth.read_auth())

    def test_auth_round_trip_and_perms(self):
        env = {'user_id': 7, 'username': 'test2', 'user_type': 'technician'}
        auth.write_auth(env)
        self.assertEqual(auth.read_auth(), env)
        mode = stat.S_IMODE(os.stat(auth.AUTH_PATH).st_mode)
        self.assertEqual(mode, stat.S_IRUSR | stat.S_IWUSR)

    def test_read_auth_handles_garbage(self):
        with open(auth.AUTH_PATH, 'w') as f:
            f.write('{not json')
        self.assertIsNone(auth.read_auth())


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

class TestJwt(unittest.TestCase):
    def test_decode_payload_round_trip(self):
        payload = {'user_id': 7, 'username': 'test2', 'exp': 1_900_000_000}
        token = _make_jwt(payload)
        self.assertEqual(auth.decode_jwt_payload(token), payload)

    def test_decode_payload_handles_missing_padding(self):
        # Force a payload whose base64 needs padding fix-up.
        payload = {'a': 1}  # short payload → b64 length not multiple of 4.
        token = _make_jwt(payload)
        # Strip any '=' that might leak in (we already do, but assert).
        self.assertNotIn('=', token)
        self.assertEqual(auth.decode_jwt_payload(token), payload)

    def test_decode_payload_garbage(self):
        for bad in ('', 'not-a-jwt', 'a.b', 'a.b.c.d.e', '...', None):
            self.assertEqual(auth.decode_jwt_payload(bad), {})

    def test_decode_payload_non_dict(self):
        # JWT payload that decodes to a JSON list, not a dict — caller can't
        # use it as `{user_id, username, ...}`, so we return {}.
        def b64url(b):
            return base64.urlsafe_b64encode(b).rstrip(b'=').decode('ascii')
        token = '{}.{}.{}'.format(
            b64url(b'{}'),
            b64url(b'[1,2,3]'),
            b64url(b'sig'),
        )
        self.assertEqual(auth.decode_jwt_payload(token), {})

    def test_jwt_expired_past(self):
        token = _make_jwt({'exp': 1_000_000})
        self.assertTrue(auth.jwt_expired(token, now_ts=2_000_000))

    def test_jwt_expired_future(self):
        token = _make_jwt({'exp': 2_000_000})
        self.assertFalse(auth.jwt_expired(token, now_ts=1_000_000))

    def test_jwt_expired_at_boundary_is_expired(self):
        # `now >= exp` is expired (per plan §3b "if now >= exp → drop the token").
        token = _make_jwt({'exp': 1234})
        self.assertTrue(auth.jwt_expired(token, now_ts=1234))

    def test_jwt_expired_missing_exp(self):
        token = _make_jwt({'user_id': 7})
        self.assertTrue(auth.jwt_expired(token))

    def test_jwt_expired_garbage(self):
        self.assertTrue(auth.jwt_expired('garbage'))
        self.assertTrue(auth.jwt_expired(''))
        self.assertTrue(auth.jwt_expired(None))


# ---------------------------------------------------------------------------
# is_logged_in
# ---------------------------------------------------------------------------

class TestIsLoggedIn(_AuthTestBase):
    def test_no_token(self):
        self.assertFalse(auth.is_logged_in())

    def test_valid_token(self):
        token = _make_jwt({'exp': int(time.time()) + 3600})
        auth.write_token(token)
        self.assertTrue(auth.is_logged_in())

    def test_expired_token(self):
        token = _make_jwt({'exp': int(time.time()) - 60})
        auth.write_token(token)
        self.assertFalse(auth.is_logged_in())

    def test_garbage_token(self):
        auth.write_token('this.is.not.a.real.jwt')
        self.assertFalse(auth.is_logged_in())


# ---------------------------------------------------------------------------
# boot_init
# ---------------------------------------------------------------------------

class TestBootInit(_AuthTestBase):
    def test_boot_init_creates_dir_and_defaults(self):
        # Whack the dir so we can prove boot_init recreates it.
        import shutil
        shutil.rmtree(self.tmp)
        buf = io.StringIO()
        with redirect_stdout(buf):
            auth.boot_init()
        self.assertTrue(os.path.isdir(self.tmp))
        self.assertTrue(os.path.isfile(auth.CONFIG_PATH))
        with open(auth.CONFIG_PATH) as f:
            self.assertEqual(json.load(f), auth.DEFAULT_CONFIG)

    def test_boot_init_idempotent(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            auth.boot_init()
            mtime_after_first = os.path.getmtime(auth.CONFIG_PATH)
            time.sleep(0.01)
            auth.boot_init()
            mtime_after_second = os.path.getmtime(auth.CONFIG_PATH)
        # No rewrite on the second call (config already existed).
        self.assertEqual(mtime_after_first, mtime_after_second)

    def test_boot_init_logs_no_session(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            auth.boot_init()
        out = buf.getvalue()
        self.assertIn('no session', out)

    def test_boot_init_logs_logged_in(self):
        token = _make_jwt({'exp': int(time.time()) + 3600})
        auth.write_token(token)
        auth.write_auth({'user_id': 1, 'username': 'test2',
                         'user_type': 'technician'})
        buf = io.StringIO()
        with redirect_stdout(buf):
            auth.boot_init()
        out = buf.getvalue()
        self.assertIn('logged in as test2', out)

    def test_boot_init_logs_no_session_when_token_expired(self):
        token = _make_jwt({'exp': int(time.time()) - 60})
        auth.write_token(token)
        buf = io.StringIO()
        with redirect_stdout(buf):
            auth.boot_init()
        out = buf.getvalue()
        # Expired → treated as no session for the boot banner.
        self.assertIn('no session', out)

    def test_boot_init_does_not_make_network_calls(self):
        # Belt-and-braces: import urllib + mock the openers; if boot_init ever
        # grows a network call, this test screams.
        import urllib.request
        with mock.patch.object(urllib.request, 'urlopen',
                               side_effect=AssertionError(
                                   'boot_init must not call the network')):
            buf = io.StringIO()
            with redirect_stdout(buf):
                auth.boot_init()


if __name__ == '__main__':
    unittest.main()
