"""Tests for the new Athathi proxy Flask routes added in app.py.

Strategy:
  - Drive the Flask routes with `app.app.test_client()`.
  - Patch `athathi_proxy.<fn>` so we never make real network calls.
  - Point `auth.ATHATHI_DIR` at a fresh tempdir per test so the on-disk token
    / auth.json / cache files don't leak between tests.

Coverage (per the §16 step 2 brief):
  1. POST /api/auth/login happy path
  2. POST /api/auth/login upstream 401
  3. POST /api/auth/logout (best-effort, always 200)
  4. GET /api/auth/me when logged in (no upstream call)
  5. GET /api/auth/me when expired (clears + reports false)
  6. GET /api/athathi/schedule when not logged in → 401
  7. GET /api/athathi/schedule happy path
  8. GET /api/athathi/schedule network error WITH cached blob → 200 + X-Cached
  9. GET /api/athathi/schedule network error WITHOUT cache → 503
  10. POST /api/athathi/scans/<id>/complete happy path
  11. POST /api/athathi/scans/<id>/complete upstream 5xx → 502
  12. POST /api/athathi/visual-search/search-full happy path
  13. New routes don't shadow existing routes
"""

import base64
import io
import json
import os
import shutil
import sys
import tempfile
import time as _time
import unittest
from unittest import mock

# Make the repo importable regardless of where pytest is invoked from.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import auth  # noqa: E402
import athathi_proxy  # noqa: E402
import app  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_jwt(payload):
    """Build a JWT-shaped token; signature is garbage (not verified locally)."""
    def b64url(b):
        return base64.urlsafe_b64encode(b).rstrip(b'=').decode('ascii')

    header = {'alg': 'HS256', 'typ': 'JWT'}
    h = b64url(json.dumps(header).encode('utf-8'))
    p = b64url(json.dumps(payload).encode('utf-8'))
    s = b64url(b'sig')
    return f'{h}.{p}.{s}'


def _fresh_jwt(seconds_until_expiry=3600):
    """JWT that doesn't expire for a while."""
    return _make_jwt({'exp': int(_time.time()) + seconds_until_expiry,
                      'user_id': 7, 'username': 'tech'})


def _expired_jwt():
    """JWT whose exp is in the past."""
    return _make_jwt({'exp': int(_time.time()) - 3600,
                      'user_id': 7, 'username': 'tech'})


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class _RouteTestBase(unittest.TestCase):
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
        # Seed config.
        auth.save_config({
            'api_url': 'http://test.athathi.local',
            'upload_endpoint': 'http://test.athathi.local',
            'last_user': '',
            'post_submit_hook': None,
            'image_transport': 'multipart',
            'visual_search_cache_ttl_s': 86400,
        })
        self.client = app.app.test_client()

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(auth, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1–2. POST /api/auth/login
# ---------------------------------------------------------------------------

class TestLoginRoute(_RouteTestBase):
    def test_happy_path_persists_token_and_auth_and_last_user(self):
        token = _fresh_jwt()
        upstream_body = {
            'message': 'ok',
            'user': {'id': 7, 'username': 'tech', 'user_type': 'technician'},
            'token': token,
        }
        with mock.patch.object(athathi_proxy, 'login',
                               return_value=upstream_body) as m_login:
            r = self.client.post('/api/auth/login',
                                 json={'username': 'tech', 'password': 'pw'})
        self.assertEqual(r.status_code, 200)
        m_login.assert_called_once_with('tech', 'pw')

        # Token written.
        self.assertEqual(auth.read_token(), token)
        # auth.json written with user envelope.
        envelope = auth.read_auth() or {}
        self.assertEqual(envelope.get('username'), 'tech')
        self.assertEqual(envelope.get('user_type'), 'technician')
        # last_user updated.
        cfg = auth.load_config()
        self.assertEqual(cfg.get('last_user'), 'tech')

        # Response: user only, NO token in body.
        body = r.get_json()
        self.assertIn('user', body)
        self.assertNotIn('token', body)
        # Defensive: token isn't anywhere in the JSON.
        self.assertNotIn(token, json.dumps(body))

    def test_upstream_401_no_token_written(self):
        with mock.patch.object(
            athathi_proxy, 'login',
            side_effect=athathi_proxy.AthathiError(401, 'bad creds', 'Invalid credentials'),
        ):
            r = self.client.post('/api/auth/login',
                                 json={'username': 'tech', 'password': 'wrong'})
        self.assertEqual(r.status_code, 401)
        self.assertIsNone(auth.read_token())
        self.assertIsNone(auth.read_auth())

    def test_missing_fields_400(self):
        with mock.patch.object(athathi_proxy, 'login') as m_login:
            r = self.client.post('/api/auth/login', json={'username': 'x'})
            self.assertEqual(r.status_code, 400)
            r2 = self.client.post('/api/auth/login', json={'password': 'x'})
            self.assertEqual(r2.status_code, 400)
            m_login.assert_not_called()


# ---------------------------------------------------------------------------
# 3. POST /api/auth/logout
# ---------------------------------------------------------------------------

class TestLogoutRoute(_RouteTestBase):
    def test_clears_token_and_calls_proxy_logout(self):
        token = _fresh_jwt()
        auth.write_token(token)
        auth.write_auth({'username': 'tech'})

        with mock.patch.object(athathi_proxy, 'logout') as m_logout:
            r = self.client.post('/api/auth/logout')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body.get('ok'))

        m_logout.assert_called_once_with(token)
        # Token + auth.json gone.
        self.assertIsNone(auth.read_token())
        self.assertIsNone(auth.read_auth())

    def test_logout_returns_200_even_if_upstream_throws(self):
        token = _fresh_jwt()
        auth.write_token(token)
        # athathi_proxy.logout already swallows AthathiError, but be belt-and-braces.
        with mock.patch.object(athathi_proxy, 'logout',
                               side_effect=Exception('boom')):
            r = self.client.post('/api/auth/logout')
        self.assertEqual(r.status_code, 200)
        # Local clear still happened.
        self.assertIsNone(auth.read_token())

    def test_logout_skips_upstream_when_no_token(self):
        # BE-6: with no token on disk, the upstream call is skipped — saves
        # a useless network round-trip and prevents transient auth log noise.
        self.assertIsNone(auth.read_token())
        with mock.patch.object(athathi_proxy, 'logout') as m_logout:
            r = self.client.post('/api/auth/logout')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body.get('ok'))
        m_logout.assert_not_called()


# ---------------------------------------------------------------------------
# 4–5. GET /api/auth/me
# ---------------------------------------------------------------------------

class TestMeRoute(_RouteTestBase):
    def test_logged_in_returns_envelope_no_upstream_call(self):
        token = _fresh_jwt()
        auth.write_token(token)
        auth.write_auth({'user_id': 7, 'username': 'tech', 'user_type': 'technician'})

        # Patch every upstream entry-point we care about; ensure NONE are called.
        with mock.patch.object(athathi_proxy, 'login') as m_login, \
             mock.patch.object(athathi_proxy, 'logout') as m_logout, \
             mock.patch.object(athathi_proxy, 'get_schedule') as m_sched, \
             mock.patch.object(athathi_proxy, 'get_history') as m_hist, \
             mock.patch.object(athathi_proxy, 'get_categories') as m_cat:
            r = self.client.get('/api/auth/me')

        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body.get('is_logged_in'))
        self.assertEqual(body.get('username'), 'tech')
        self.assertEqual(body.get('user_type'), 'technician')
        self.assertIsNotNone(body.get('exp_at'))

        # No upstream call.
        for m in (m_login, m_logout, m_sched, m_hist, m_cat):
            m.assert_not_called()

    def test_expired_token_clears_and_reports_logged_out(self):
        auth.write_token(_expired_jwt())
        auth.write_auth({'user_id': 7, 'username': 'tech'})

        r = self.client.get('/api/auth/me')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body.get('is_logged_in'))
        # Token + auth.json should be cleared after the call.
        self.assertIsNone(auth.read_token())
        self.assertIsNone(auth.read_auth())


# ---------------------------------------------------------------------------
# 6–9. GET /api/athathi/schedule (auth gating + cache fallback)
# ---------------------------------------------------------------------------

class TestScheduleRoute(_RouteTestBase):
    def test_not_logged_in_returns_401(self):
        # No token on disk.
        with mock.patch.object(athathi_proxy, 'get_schedule') as m_get:
            r = self.client.get('/api/athathi/schedule')
        self.assertEqual(r.status_code, 401)
        m_get.assert_not_called()

    def test_happy_path_returns_bare_list(self):
        token = _fresh_jwt()
        auth.write_token(token)

        sched = [{'scan_id': 42}, {'scan_id': 43}]
        with mock.patch.object(athathi_proxy, 'get_schedule',
                               return_value=sched) as m_get:
            r = self.client.get('/api/athathi/schedule')
        self.assertEqual(r.status_code, 200)
        m_get.assert_called_once()
        self.assertEqual(r.get_json(), sched)

    def test_network_error_with_cached_blob_serves_stale(self):
        token = _fresh_jwt()
        auth.write_token(token)

        # Pre-populate the cache.
        sched = [{'scan_id': 42, 'cached': True}]
        key = athathi_proxy.cache_key_for('schedule', token)
        path = athathi_proxy._cache_path(key)
        athathi_proxy._write_cache(path, sched)
        # Force it stale so the route has to hit the upstream.
        with open(path, 'r') as f:
            wrapper = json.load(f)
        wrapper['cached_at'] = _time.time() - 10_000
        with open(path, 'w') as f:
            json.dump(wrapper, f)

        with mock.patch.object(
            athathi_proxy, 'get_schedule',
            side_effect=athathi_proxy.AthathiError(0, 'refused', 'Network error'),
        ):
            r = self.client.get('/api/athathi/schedule')

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), sched)
        self.assertEqual(r.headers.get('X-Cached'), 'true')
        self.assertEqual(r.headers.get('X-Stale-Reason'), 'network')

    def test_network_error_without_cache_returns_503(self):
        token = _fresh_jwt()
        auth.write_token(token)

        with mock.patch.object(
            athathi_proxy, 'get_schedule',
            side_effect=athathi_proxy.AthathiError(0, 'refused', 'Network error'),
        ):
            r = self.client.get('/api/athathi/schedule')

        self.assertEqual(r.status_code, 503)
        body = r.get_json()
        self.assertEqual(body.get('error'), 'no network')
        self.assertIn('detail', body)


# ---------------------------------------------------------------------------
# 10–11. POST /api/athathi/scans/<id>/complete
# ---------------------------------------------------------------------------

class TestCompleteRoute(_RouteTestBase):
    def test_happy_path(self):
        auth.write_token(_fresh_jwt())
        with mock.patch.object(athathi_proxy, 'complete_scan',
                               return_value={'message': 'ok', 'scan_id': 42}) as m:
            r = self.client.post('/api/athathi/scans/42/complete')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body.get('scan_id'), 42)
        m.assert_called_once()
        # Verify scan_id was passed through.
        args, kwargs = m.call_args
        self.assertEqual(args[1], 42)

    def test_upstream_5xx_returns_502(self):
        auth.write_token(_fresh_jwt())
        with mock.patch.object(
            athathi_proxy, 'complete_scan',
            side_effect=athathi_proxy.AthathiError(503, 'oops', 'Upstream 503'),
        ):
            r = self.client.post('/api/athathi/scans/42/complete')
        self.assertEqual(r.status_code, 502)
        body = r.get_json()
        self.assertEqual(body.get('error'), 'upstream error')
        self.assertEqual(body.get('upstream_status'), 503)

    def test_upstream_401_clears_token(self):
        auth.write_token(_fresh_jwt())
        with mock.patch.object(
            athathi_proxy, 'complete_scan',
            side_effect=athathi_proxy.AthathiError(401, 'expired', 'Unauthorized'),
        ):
            r = self.client.post('/api/athathi/scans/42/complete')
        self.assertEqual(r.status_code, 401)
        self.assertIsNone(auth.read_token())


# ---------------------------------------------------------------------------
# 12. POST /api/athathi/visual-search/search-full
# ---------------------------------------------------------------------------

class TestVisualSearchRoute(_RouteTestBase):
    def test_multipart_in_json_out(self):
        auth.write_token(_fresh_jwt())

        captured = {}

        def _fake_vs(token, image_path):
            # Verify the file actually exists when the proxy is called.
            captured['exists'] = os.path.isfile(image_path)
            captured['size'] = os.path.getsize(image_path) if captured['exists'] else 0
            return {'results': [{'id': 1, 'name': 'Sofa'}], 'total_time': 1.5}

        with mock.patch.object(athathi_proxy, 'visual_search_full',
                               side_effect=_fake_vs):
            r = self.client.post(
                '/api/athathi/visual-search/search-full',
                data={'file': (io.BytesIO(b'\xff\xd8\xff\xe0fakejpg'), 'thumb.jpg')},
                content_type='multipart/form-data',
            )

        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body['results'][0]['name'], 'Sofa')
        self.assertTrue(captured.get('exists'))
        self.assertGreater(captured.get('size', 0), 0)

    def test_missing_file_returns_400(self):
        auth.write_token(_fresh_jwt())
        r = self.client.post(
            '/api/athathi/visual-search/search-full',
            data={},
            content_type='multipart/form-data',
        )
        self.assertEqual(r.status_code, 400)


# ---------------------------------------------------------------------------
# 13. New routes don't shadow existing routes
# ---------------------------------------------------------------------------

class TestExistingRoutesIntact(_RouteTestBase):
    def test_get_sessions_still_responds(self):
        # /api/sessions is the legacy listing route. It must still respond.
        r = self.client.get('/api/sessions')
        # Could be 200 with an array, but never 404 / 405.
        self.assertNotIn(r.status_code, (404, 405))

    def test_get_status_still_responds(self):
        r = self.client.get('/api/status')
        self.assertNotIn(r.status_code, (404, 405))

    def test_post_record_start_still_routes(self):
        # We don't actually want to start recording — just verify the route
        # is registered (i.e. it doesn't 404). Body is empty so the handler
        # may return any non-(404/405) code.
        r = self.client.post('/api/record/start', json={})
        self.assertNotIn(r.status_code, (404, 405))

    def test_get_camera_preview_still_routes(self):
        # /api/camera/preview either returns image bytes or a placeholder JPEG;
        # what matters is it's not 404/405.
        r = self.client.get('/api/camera/preview')
        self.assertNotIn(r.status_code, (404, 405))


# ---------------------------------------------------------------------------
# FRONTEND-B1: /api/settings/probe_api_url
# ---------------------------------------------------------------------------

class TestSettingsProbeApiUrl(_RouteTestBase):
    """The Settings 'Test' button hits this server-side route, which curl-probes
    `<url>/api/users/me/`. Auth-gated; never raises on transport failure —
    surfaces `{ok: False, status_code: 0, error: ...}` instead.
    """

    def _login(self):
        auth.write_token(_fresh_jwt())
        auth.write_auth({'user_id': 7, 'username': 'tech'})

    def test_401_when_not_logged_in(self):
        r = self.client.post('/api/settings/probe_api_url',
                             json={'url': 'http://example.test'})
        self.assertEqual(r.status_code, 401)

    def test_400_when_url_missing(self):
        self._login()
        r = self.client.post('/api/settings/probe_api_url', json={})
        self.assertEqual(r.status_code, 400)

    def test_happy_path_returns_status_code(self):
        self._login()
        # Mock subprocess.run to simulate curl returning HTTP 401 (a typical
        # response from /api/users/me/ without credentials).
        fake = mock.Mock()
        fake.stdout = '401'
        fake.stderr = ''
        fake.returncode = 0
        with mock.patch('app.subprocess.run', return_value=fake) as m_run:
            r = self.client.post('/api/settings/probe_api_url',
                                 json={'url': 'http://example.test/'})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['status_code'], 401)
        # The route appended /api/users/me/ to the trimmed base URL.
        argv = m_run.call_args[0][0]
        self.assertEqual(argv[-1], 'http://example.test/api/users/me/')

    def test_curl_network_failure_shape(self):
        self._login()
        # curl exits with no http_code → stdout is empty.
        fake = mock.Mock()
        fake.stdout = ''
        fake.stderr = 'curl: (7) Failed to connect'
        fake.returncode = 7
        with mock.patch('app.subprocess.run', return_value=fake):
            r = self.client.post('/api/settings/probe_api_url',
                                 json={'url': 'http://nope.invalid'})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body['ok'])
        self.assertEqual(body['status_code'], 0)
        self.assertIn('Failed to connect', body['error'])

    def test_curl_missing_returns_shape(self):
        self._login()
        with mock.patch('app.subprocess.run',
                        side_effect=FileNotFoundError('curl')):
            r = self.client.post('/api/settings/probe_api_url',
                                 json={'url': 'http://example.test'})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body['ok'])
        self.assertEqual(body['status_code'], 0)
        self.assertIn('curl', body['error'])


if __name__ == '__main__':
    unittest.main()
