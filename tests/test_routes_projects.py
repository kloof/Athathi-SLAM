"""Tests for the new `/api/projects` Flask route added in app.py.

Strategy mirrors `tests/test_routes_auth.py`:
  - Drive the route with `app.app.test_client()`.
  - Patch `athathi_proxy.cached_get` so we never make real network calls.
  - Point `auth.ATHATHI_DIR` AND `projects.PROJECTS_ROOT` at fresh tempdirs
    per test so the on-disk token / cache files / project folders don't
    leak between tests.

Coverage (per the §16 step 3 brief):
  1. /api/projects returns 401 when not logged in.
  2. Happy path: schedule has 2 items, history has 1; route mirrors local
     dirs and returns merged shape with rooms_local: 0 for newly-created.
  3. Network down on both: cached: true and X-Cached header.
  4. Schedule 5xx (non-network AthathiError): route returns 502.
  5. Unknown fields in a schedule item are preserved verbatim in athathi_meta.
  6. New routes don't shadow existing — sanity check /api/sessions.
"""

import base64
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
import projects  # noqa: E402
import app  # noqa: E402


# ---------------------------------------------------------------------------
# JWT helpers (copied from test_routes_auth so the suites are independent).
# ---------------------------------------------------------------------------

def _make_jwt(payload):
    def b64url(b):
        return base64.urlsafe_b64encode(b).rstrip(b'=').decode('ascii')

    header = {'alg': 'HS256', 'typ': 'JWT'}
    h = b64url(json.dumps(header).encode('utf-8'))
    p = b64url(json.dumps(payload).encode('utf-8'))
    s = b64url(b'sig')
    return f'{h}.{p}.{s}'


def _fresh_jwt():
    return _make_jwt({'exp': int(_time.time()) + 3600,
                      'user_id': 7, 'username': 'tech'})


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class _RouteTestBase(unittest.TestCase):
    def setUp(self):
        # Athathi state (token + config + cache) in its own tempdir.
        self.tmp_auth = tempfile.mkdtemp()
        self._orig_auth = {
            'ATHATHI_DIR':           auth.ATHATHI_DIR,
            'CONFIG_PATH':           auth.CONFIG_PATH,
            'TOKEN_PATH':            auth.TOKEN_PATH,
            'AUTH_PATH':             auth.AUTH_PATH,
            'LEARNED_CLASSES_PATH':  auth.LEARNED_CLASSES_PATH,
        }
        auth.ATHATHI_DIR = self.tmp_auth
        auth.CONFIG_PATH = os.path.join(self.tmp_auth, 'config.json')
        auth.TOKEN_PATH = os.path.join(self.tmp_auth, 'token')
        auth.AUTH_PATH = os.path.join(self.tmp_auth, 'auth.json')
        auth.LEARNED_CLASSES_PATH = os.path.join(self.tmp_auth, 'learned_classes.json')
        auth.save_config({
            'api_url': 'http://test.athathi.local',
            'upload_endpoint': 'http://test.athathi.local',
            'last_user': '',
            'post_submit_hook': None,
            'image_transport': 'multipart',
            'visual_search_cache_ttl_s': 86400,
        })

        # Projects on-disk state in a separate tempdir.
        self.tmp_projects = tempfile.mkdtemp()
        self._orig_projects_root = projects.PROJECTS_ROOT
        projects.PROJECTS_ROOT = self.tmp_projects

        self.client = app.app.test_client()

    def tearDown(self):
        for k, v in self._orig_auth.items():
            setattr(auth, k, v)
        projects.PROJECTS_ROOT = self._orig_projects_root
        shutil.rmtree(self.tmp_auth, ignore_errors=True)
        shutil.rmtree(self.tmp_projects, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1. Auth gate
# ---------------------------------------------------------------------------

class TestAuthGate(_RouteTestBase):
    def test_returns_401_when_not_logged_in(self):
        # No token on disk.
        with mock.patch.object(athathi_proxy, 'cached_get') as m_cg:
            r = self.client.get('/api/projects')
        self.assertEqual(r.status_code, 401)
        m_cg.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Happy path
# ---------------------------------------------------------------------------

class TestHappyPath(_RouteTestBase):
    def test_schedule_two_history_one_creates_local_dirs(self):
        auth.write_token(_fresh_jwt())

        scheduled = [
            {'scan_id': 42, 'customer_name': 'Smith', 'address': '1 Maple'},
            {'scan_id': 43, 'customer_name': 'Jones', 'address': '2 Oak'},
        ]
        history = [
            {'scan_id': 7, 'customer_name': 'Old', 'completed_at': '2026-04-20T12:00:00Z'},
        ]

        def _fake_cached_get(key, ttl, fetch_fn):
            if 'schedule' in key:
                return scheduled
            if 'history' in key:
                return history
            raise AssertionError(f'unexpected cache key: {key}')

        with mock.patch.object(athathi_proxy, 'cached_get',
                               side_effect=_fake_cached_get):
            r = self.client.get('/api/projects')

        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        # Top-level shape.
        self.assertIn('now', body)
        self.assertIn('scheduled', body)
        self.assertIn('history', body)
        self.assertIn('ad_hoc', body)
        self.assertFalse(body['cached'])

        sched_ids = [m['scan_id'] for m in body['scheduled']]
        self.assertEqual(sched_ids, [43, 42])  # descending

        hist_ids = [m['scan_id'] for m in body['history']]
        self.assertEqual(hist_ids, [7])

        # rooms_local should be 0 for all newly-created (no scans yet).
        for m in body['scheduled'] + body['history']:
            self.assertEqual(m['rooms_local'], 0)
            self.assertEqual(m['rooms_reviewed'], 0)
            self.assertFalse(m['submitted'])

        # Local mirroring happened.
        self.assertTrue(os.path.isfile(projects.manifest_path(42)))
        self.assertTrue(os.path.isfile(projects.manifest_path(43)))
        self.assertTrue(os.path.isfile(projects.manifest_path(7)))

        # History entry should have completed_at set on the manifest.
        m_hist = projects.read_manifest(7)
        self.assertEqual(m_hist['completed_at'], '2026-04-20T12:00:00Z')


# ---------------------------------------------------------------------------
# 3. Both upstream calls stale
# ---------------------------------------------------------------------------

class TestNetworkDownBothStale(_RouteTestBase):
    def test_cached_true_and_x_cached_header(self):
        auth.write_token(_fresh_jwt())

        # The route calls cached_get with each (schedule/history) key. We
        # hand it back StaleCacheResult to simulate "served from stale cache".
        sched_blob = [{'scan_id': 42, 'customer_name': 'Cached'}]
        hist_blob = [{'scan_id': 7,  'customer_name': 'Old'}]

        def _fake_cached_get(key, ttl, fetch_fn):
            if 'schedule' in key:
                return athathi_proxy.StaleCacheResult(sched_blob, reason='network')
            if 'history' in key:
                return athathi_proxy.StaleCacheResult(hist_blob, reason='network')
            raise AssertionError(f'unexpected cache key: {key}')

        with mock.patch.object(athathi_proxy, 'cached_get',
                               side_effect=_fake_cached_get):
            r = self.client.get('/api/projects')

        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['cached'])
        self.assertEqual(r.headers.get('X-Cached'), 'true')
        self.assertEqual(r.headers.get('X-Stale-Reason'), 'network')

        # Mirroring still happened from the cached blob.
        self.assertEqual([m['scan_id'] for m in body['scheduled']], [42])
        self.assertEqual([m['scan_id'] for m in body['history']], [7])

    def test_cached_response_carries_fetched_at(self):
        """When both schedule + history return StaleCacheResult, the
        response body must include a `fetched_at` ISO timestamp so the
        frontend can render an honest last-refreshed banner."""
        auth.write_token(_fresh_jwt())

        sched_blob = [{'scan_id': 42}]
        hist_blob = [{'scan_id': 7}]

        # Two different timestamps — the route should pick the OLDER one
        # (the user's banner reflects the least-fresh data on screen).
        sched_iso = '2026-04-25T11:00:00Z'
        hist_iso = '2026-04-25T10:30:00Z'

        def _fake_cached_get(key, ttl, fetch_fn):
            if 'schedule' in key:
                return athathi_proxy.StaleCacheResult(
                    sched_blob, reason='network',
                    fetched_at_iso=sched_iso,
                )
            if 'history' in key:
                return athathi_proxy.StaleCacheResult(
                    hist_blob, reason='network',
                    fetched_at_iso=hist_iso,
                )
            raise AssertionError(f'unexpected cache key: {key}')

        with mock.patch.object(athathi_proxy, 'cached_get',
                               side_effect=_fake_cached_get):
            r = self.client.get('/api/projects')

        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['cached'])
        self.assertIn('fetched_at', body)
        # Older of the two (history) — the banner reflects least-fresh.
        self.assertEqual(body['fetched_at'], hist_iso)


# ---------------------------------------------------------------------------
# 4. Non-network upstream error → 502
# ---------------------------------------------------------------------------

class TestUpstream5xx(_RouteTestBase):
    def test_schedule_5xx_returns_502(self):
        auth.write_token(_fresh_jwt())

        def _fake_cached_get(key, ttl, fetch_fn):
            # cached_get re-raises non-network errors verbatim.
            raise athathi_proxy.AthathiError(503, 'oops', 'Upstream 503')

        with mock.patch.object(athathi_proxy, 'cached_get',
                               side_effect=_fake_cached_get):
            r = self.client.get('/api/projects')

        self.assertEqual(r.status_code, 502)
        body = r.get_json()
        self.assertEqual(body.get('error'), 'upstream error')
        self.assertEqual(body.get('upstream_status'), 503)

    def test_upstream_401_clears_token(self):
        auth.write_token(_fresh_jwt())
        self.assertIsNotNone(auth.read_token())

        def _fake_cached_get(key, ttl, fetch_fn):
            raise athathi_proxy.AthathiError(401, 'expired', 'Unauthorized')

        with mock.patch.object(athathi_proxy, 'cached_get',
                               side_effect=_fake_cached_get):
            r = self.client.get('/api/projects')

        self.assertEqual(r.status_code, 401)
        self.assertIsNone(auth.read_token())


# ---------------------------------------------------------------------------
# 5. Unknown fields preserved
# ---------------------------------------------------------------------------

class TestUnknownFieldsPreserved(_RouteTestBase):
    def test_unknown_fields_end_up_in_athathi_meta(self):
        auth.write_token(_fresh_jwt())

        scheduled = [{
            'scan_id': 42,
            'customer_name': 'Smith',
            'mystery_field_a': 'value-A',
            'nested': {'k': 'v'},
            'list_field': [1, 2, 3],
        }]
        history = []

        def _fake_cached_get(key, ttl, fetch_fn):
            return scheduled if 'schedule' in key else history

        with mock.patch.object(athathi_proxy, 'cached_get',
                               side_effect=_fake_cached_get):
            r = self.client.get('/api/projects')

        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(len(body['scheduled']), 1)
        m = body['scheduled'][0]
        self.assertEqual(m['scan_id'], 42)
        self.assertEqual(m['customer_name'], 'Smith')

        # Mystery fields preserved verbatim.
        meta = m['athathi_meta']
        self.assertEqual(meta['mystery_field_a'], 'value-A')
        self.assertEqual(meta['nested'], {'k': 'v'})
        self.assertEqual(meta['list_field'], [1, 2, 3])

        # And on-disk manifest agrees.
        on_disk = projects.read_manifest(42)
        self.assertEqual(on_disk['athathi_meta']['mystery_field_a'], 'value-A')


# ---------------------------------------------------------------------------
# 6. New routes don't shadow existing
# ---------------------------------------------------------------------------

class TestExistingRoutesIntact(_RouteTestBase):
    def test_get_sessions_still_responds(self):
        r = self.client.get('/api/sessions')
        self.assertNotIn(r.status_code, (404, 405))

    def test_get_status_still_responds(self):
        r = self.client.get('/api/status')
        self.assertNotIn(r.status_code, (404, 405))


# ---------------------------------------------------------------------------
# 7. Local-only project shows up under ad_hoc
# ---------------------------------------------------------------------------

class TestAdHocBucket(_RouteTestBase):
    def test_local_only_project_under_ad_hoc(self):
        auth.write_token(_fresh_jwt())

        # Pre-create a local-only project (no athathi_meta).
        projects.ensure_project(0, athathi_meta=None)

        def _fake_cached_get(key, ttl, fetch_fn):
            return []  # both schedule + history empty

        with mock.patch.object(athathi_proxy, 'cached_get',
                               side_effect=_fake_cached_get):
            r = self.client.get('/api/projects')

        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual([m['scan_id'] for m in body['scheduled']], [])
        self.assertEqual([m['scan_id'] for m in body['history']], [])
        ad_hoc_ids = [m['scan_id'] for m in body['ad_hoc']]
        self.assertIn(0, ad_hoc_ids)


if __name__ == '__main__':
    unittest.main()
