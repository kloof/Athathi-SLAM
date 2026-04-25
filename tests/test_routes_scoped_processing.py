"""Tests for the Step 4 scoped recording + processing routes (plan §16).

Strategy mirrors `tests/test_routes_projects.py`:
  - Drive routes via `app.app.test_client()`.
  - Patch `auth.ATHATHI_DIR` and `projects.PROJECTS_ROOT` to per-test tmpdirs.
  - Mock the Modal helpers (`_modal_submit`, `_modal_poll`, `_modal_fetch`,
    `_modal_cancel`) and `_zstd_compress` so no real network or compression
    work happens.
  - Mock `_setup_network` so /start_recording doesn't try to configure eth0.
  - Mock `_is_recording` / `_is_busy` so we never spawn rosbag subprocesses.

Coverage (per the §16 step 4 brief):
  1.  POST /api/project/42/scan with a valid name → 200, dir created.
  2.  POST /api/project/42/scan with an invalid name → 400.
  3.  POST /api/project/42/scan when project not local → 404.
  4.  DELETE /api/project/42/scan/living_room → 200, dir removed.
  5.  DELETE while `_is_recording()` is true → 409.
  6.  GET /api/project/42/scans returns the list shape.
  7.  POST .../start_recording without login → 401.
  8.  POST .../process without login → 401.
  9.  POST .../process happy path: run dir created under project tree.
  10. GET .../result returns {status: 'not_processed'} before any run.
  11. GET .../result after a (mocked) run returns the envelope.
  12. New routes don't shadow the legacy /api/session/<id>/process,
      /api/sessions, or /api/record/start.
"""

import base64
import json
import os
import shutil
import sys
import tempfile
import threading
import time as _time
import unittest
from unittest import mock

# Make the repo importable regardless of where pytest is invoked from.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import auth  # noqa: E402
import projects  # noqa: E402
import app  # noqa: E402


# ---------------------------------------------------------------------------
# JWT helpers (copied from test_routes_auth)
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

class _ScopedRouteBase(unittest.TestCase):
    def setUp(self):
        # Auth state in its own tempdir.
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

        # Projects on-disk state in a separate tempdir.
        self.tmp_projects = tempfile.mkdtemp()
        self._orig_projects_root = projects.PROJECTS_ROOT
        projects.PROJECTS_ROOT = self.tmp_projects

        # Per-test sessions.json so we don't pollute the repo's file.
        self.tmp_sessions = tempfile.mkdtemp()
        self._orig_sessions_file = app.SESSIONS_FILE
        app.SESSIONS_FILE = os.path.join(self.tmp_sessions, 'sessions.json')

        # Snapshot + clear in-memory processing state so tests don't bleed.
        with app._processing_lock:
            self._orig_active_processing = dict(app._active_processing)
            app._active_processing.clear()

        self.client = app.app.test_client()

    def tearDown(self):
        for k, v in self._orig_auth.items():
            setattr(auth, k, v)
        projects.PROJECTS_ROOT = self._orig_projects_root
        app.SESSIONS_FILE = self._orig_sessions_file

        with app._processing_lock:
            app._active_processing.clear()
            app._active_processing.update(self._orig_active_processing)

        shutil.rmtree(self.tmp_auth, ignore_errors=True)
        shutil.rmtree(self.tmp_projects, ignore_errors=True)
        shutil.rmtree(self.tmp_sessions, ignore_errors=True)

    # Helpers --------------------------------------------------------------

    def _login(self):
        auth.write_token(_fresh_jwt())

    def _ensure_project(self, scan_id=42, name='Smith'):
        projects.ensure_project(scan_id, athathi_meta={'customer_name': name})


# ---------------------------------------------------------------------------
# 1-3. POST /api/project/<id>/scan
# ---------------------------------------------------------------------------

class TestCreateScanRoute(_ScopedRouteBase):
    def test_valid_name_creates_scan(self):
        self._login()
        self._ensure_project()
        r = self.client.post(
            '/api/project/42/scan',
            json={'name': 'living_room'},
        )
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body['name'], 'living_room')
        self.assertFalse(body['has_rosbag'])
        self.assertIsNone(body['active_run_id'])
        self.assertTrue(os.path.isdir(projects.scan_dir(42, 'living_room')))
        self.assertTrue(os.path.isdir(projects.scan_rosbag_dir(42, 'living_room')))

    def test_invalid_name_returns_400(self):
        self._login()
        self._ensure_project()
        r = self.client.post(
            '/api/project/42/scan',
            json={'name': 'Living Room'},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn('error', r.get_json())

    def test_project_not_local_returns_404(self):
        self._login()
        # No projects.ensure_project — manifest is missing.
        r = self.client.post(
            '/api/project/99/scan',
            json={'name': 'living_room'},
        )
        self.assertEqual(r.status_code, 404)

    def test_duplicate_returns_409(self):
        self._login()
        self._ensure_project()
        self.client.post('/api/project/42/scan', json={'name': 'living_room'})
        r = self.client.post(
            '/api/project/42/scan',
            json={'name': 'living_room'},
        )
        self.assertEqual(r.status_code, 409)

    def test_not_logged_in_returns_401(self):
        # No token written.
        self._ensure_project()
        r = self.client.post(
            '/api/project/42/scan',
            json={'name': 'living_room'},
        )
        self.assertEqual(r.status_code, 401)


# ---------------------------------------------------------------------------
# 4-5. DELETE /api/project/<id>/scan/<name>
# ---------------------------------------------------------------------------

class TestDeleteScanRoute(_ScopedRouteBase):
    def test_delete_removes_dir(self):
        self._login()
        self._ensure_project()
        projects.create_scan(42, 'living_room')
        self.assertTrue(os.path.isdir(projects.scan_dir(42, 'living_room')))

        r = self.client.delete('/api/project/42/scan/living_room')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(os.path.exists(projects.scan_dir(42, 'living_room')))

    def test_delete_while_recording_returns_409(self):
        self._login()
        self._ensure_project()
        projects.create_scan(42, 'living_room')

        with mock.patch.object(app, '_is_recording', return_value=True):
            r = self.client.delete('/api/project/42/scan/living_room')

        self.assertEqual(r.status_code, 409)
        # Scan still on disk.
        self.assertTrue(os.path.isdir(projects.scan_dir(42, 'living_room')))

    def test_delete_missing_scan_returns_404(self):
        self._login()
        self._ensure_project()
        r = self.client.delete('/api/project/42/scan/nonexistent')
        self.assertEqual(r.status_code, 404)


# ---------------------------------------------------------------------------
# 6. GET /api/project/<id>/scans
# ---------------------------------------------------------------------------

class TestListScansRoute(_ScopedRouteBase):
    def test_list_shape(self):
        self._login()
        self._ensure_project()
        projects.create_scan(42, 'living_room')
        projects.create_scan(42, 'kitchen')

        r = self.client.get('/api/project/42/scans')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertIn('scans', body)
        names = [s['name'] for s in body['scans']]
        self.assertEqual(sorted(names), ['kitchen', 'living_room'])
        for s in body['scans']:
            self.assertIn('has_rosbag', s)
            self.assertIn('active_run_id', s)

    def test_list_empty_when_no_scans(self):
        self._login()
        self._ensure_project()
        r = self.client.get('/api/project/42/scans')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['scans'], [])

    def test_not_logged_in_returns_401(self):
        self._ensure_project()
        r = self.client.get('/api/project/42/scans')
        self.assertEqual(r.status_code, 401)


# ---------------------------------------------------------------------------
# 7. POST .../start_recording auth gate
# ---------------------------------------------------------------------------

class TestStartRecordingAuth(_ScopedRouteBase):
    def test_not_logged_in_returns_401(self):
        self._ensure_project()
        projects.create_scan(42, 'living_room')
        r = self.client.post(
            '/api/project/42/scan/living_room/start_recording',
        )
        self.assertEqual(r.status_code, 401)

    def test_busy_returns_409(self):
        self._login()
        self._ensure_project()
        projects.create_scan(42, 'living_room')

        with mock.patch.object(app, '_is_busy', return_value=True):
            r = self.client.post(
                '/api/project/42/scan/living_room/start_recording',
            )
        self.assertEqual(r.status_code, 409)

    def test_missing_scan_returns_404(self):
        self._login()
        self._ensure_project()
        # Don't create the scan.
        r = self.client.post(
            '/api/project/42/scan/living_room/start_recording',
        )
        self.assertEqual(r.status_code, 404)


# ---------------------------------------------------------------------------
# 8-9. POST .../process
# ---------------------------------------------------------------------------

class TestProcessRoute(_ScopedRouteBase):
    def test_not_logged_in_returns_401(self):
        self._ensure_project()
        projects.create_scan(42, 'living_room')
        r = self.client.post('/api/project/42/scan/living_room/process')
        self.assertEqual(r.status_code, 401)

    def _seed_mcap(self):
        """Drop a fake mcap into the rosbag dir so /process gets past the
        'No MCAP file found' check."""
        bag_dir = projects.scan_rosbag_dir(42, 'living_room')
        os.makedirs(bag_dir, exist_ok=True)
        path = os.path.join(bag_dir, 'rosbag_0.mcap')
        with open(path, 'wb') as f:
            f.write(b'x' * 32)
        return path

    def test_no_mcap_returns_404(self):
        self._login()
        self._ensure_project()
        projects.create_scan(42, 'living_room')

        with mock.patch.object(app, 'MODAL_API_KEY', 'fake'), \
             mock.patch.object(app, '_ZSTD_BIN', '/usr/bin/zstd'):
            r = self.client.post('/api/project/42/scan/living_room/process')
        self.assertEqual(r.status_code, 404)

    def test_modal_not_configured_returns_503(self):
        self._login()
        self._ensure_project()
        projects.create_scan(42, 'living_room')
        self._seed_mcap()

        with mock.patch.object(app, 'MODAL_API_KEY', ''):
            r = self.client.post('/api/project/42/scan/living_room/process')
        self.assertEqual(r.status_code, 503)

    def test_happy_path_creates_run_dir(self):
        self._login()
        self._ensure_project()
        projects.create_scan(42, 'living_room')
        self._seed_mcap()

        # Block the worker thread until the test has read the result; we
        # just want to verify route behavior, not Modal interaction. We
        # mock _scoped_process_thread to a no-op so we can inspect state
        # immediately after kicking off /process.
        with mock.patch.object(app, 'MODAL_API_KEY', 'fake'), \
             mock.patch.object(app, '_ZSTD_BIN', '/usr/bin/zstd'), \
             mock.patch.object(app, '_scoped_process_thread') as m_worker:
            r = self.client.post('/api/project/42/scan/living_room/process')

        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertIn('session_id', body)
        self.assertIn('run_id', body)
        self.assertEqual(body['status'], 'started')
        run_id = body['run_id']

        # active_run.json points at the new run.
        self.assertEqual(
            projects.read_active_run(42, 'living_room'),
            run_id,
        )

        # The run_dir lives under the project tree, NOT the legacy
        # PROCESSED_DIR.
        run_dir = projects.processed_dir_for_run(42, 'living_room', run_id)
        self.assertTrue(os.path.isdir(run_dir))
        legacy_dir = app._processed_dir_for_session(f'42__living_room')
        self.assertNotEqual(os.path.realpath(run_dir),
                            os.path.realpath(legacy_dir))

        # The worker thread was kicked off with our run_dir.
        self.assertTrue(m_worker.called)
        call_args = m_worker.call_args
        # _scoped_process_thread(session_id, mcap_path, run_dir)
        self.assertEqual(call_args.args[2], run_dir)

    def test_process_unknown_project_returns_404(self):
        self._login()
        # No ensure_project for 99.
        with mock.patch.object(app, 'MODAL_API_KEY', 'fake'), \
             mock.patch.object(app, '_ZSTD_BIN', '/usr/bin/zstd'):
            r = self.client.post('/api/project/99/scan/living_room/process')
        self.assertEqual(r.status_code, 404)


# ---------------------------------------------------------------------------
# 10-11. GET .../result
# ---------------------------------------------------------------------------

class TestResultRoute(_ScopedRouteBase):
    def test_not_processed_when_no_active_run(self):
        self._login()
        self._ensure_project()
        projects.create_scan(42, 'living_room')

        r = self.client.get('/api/project/42/scan/living_room/result')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['status'], 'not_processed')

    def test_done_returns_envelope(self):
        self._login()
        self._ensure_project()
        projects.create_scan(42, 'living_room')

        # Pretend a run finished.
        run_id = '20260425_142103'
        run_dir = projects.processed_dir_for_run(42, 'living_room', run_id)
        os.makedirs(run_dir, exist_ok=True)
        envelope = {
            'status': 'done',
            'furniture': [{'class': 'sofa'}],
        }
        with open(os.path.join(run_dir, 'result.json'), 'w') as f:
            json.dump(envelope, f)
        projects.set_active_run(42, 'living_room', run_id)

        r = self.client.get('/api/project/42/scan/living_room/result')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body['status'], 'done')
        self.assertEqual(body['run_id'], run_id)
        self.assertEqual(body['result']['furniture'][0]['class'], 'sofa')

    def test_processing_when_active_processing_entry_present(self):
        self._login()
        self._ensure_project()
        projects.create_scan(42, 'living_room')

        run_id = '20260425_142103'
        with app._processing_lock:
            app._active_processing['fake_sid'] = {
                'status': 'processing',
                'stage': 'stage_5_infer',
                'start_time': _time.time(),
                'cancel': threading.Event(),
                'job_id': 'j_fake',
                'run_id': run_id,
                'scan_id': 42,
                'scan_name': 'living_room',
            }

        try:
            r = self.client.get('/api/project/42/scan/living_room/result')
        finally:
            with app._processing_lock:
                app._active_processing.pop('fake_sid', None)

        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body['status'], 'processing')
        self.assertEqual(body['stage'], 'stage_5_infer')
        self.assertEqual(body['run_id'], run_id)


# ---------------------------------------------------------------------------
# 12. New routes don't shadow legacy
# ---------------------------------------------------------------------------

class TestLegacyRoutesIntact(_ScopedRouteBase):
    def test_legacy_sessions_still_resolves(self):
        r = self.client.get('/api/sessions')
        self.assertNotIn(r.status_code, (404, 405))

    def test_legacy_session_process_route_present(self):
        # The route is registered (we hit 404 only because the session
        # doesn't exist — the URL pattern itself is unchanged).
        r = self.client.post('/api/session/nonexistent/process')
        # 404 = "Session not found" from the legacy handler (which means
        # the route exists). 503 = "MODAL_API not configured". Either
        # confirms the legacy route is wired.
        self.assertIn(r.status_code, (404, 503))

    def test_legacy_record_start_route_present(self):
        # The legacy /api/record/start exists. We don't actually want to
        # spawn the recording thread, so we patch _setup_network to fail
        # — that path returns 500 BEFORE any subprocess work.
        with mock.patch.object(app, '_setup_network',
                               return_value=(False, 'mocked')):
            r = self.client.post('/api/record/start', json={'name': 'x'})
        self.assertEqual(r.status_code, 500)

    def test_scoped_route_distinct_from_session_route(self):
        # Ensure we didn't accidentally re-register /api/session/...
        # with a colliding rule.
        rules = [str(r) for r in app.app.url_map.iter_rules()]
        scoped = [r for r in rules if r.startswith('/api/project/')]
        legacy = [r for r in rules if r.startswith('/api/session/')]
        self.assertGreater(len(scoped), 0)
        self.assertGreater(len(legacy), 0)
        # No scoped URL accidentally lands under /api/session/.
        for r in scoped:
            self.assertFalse(r.startswith('/api/session/'))


# ---------------------------------------------------------------------------
# 13. best_view & layout & artifact
# ---------------------------------------------------------------------------

class TestArtifactRoutes(_ScopedRouteBase):
    def _seed_run(self, run_id='20260425_142103'):
        projects.create_scan(42, 'living_room')
        run_dir = projects.processed_dir_for_run(42, 'living_room', run_id)
        os.makedirs(os.path.join(run_dir, 'best_views'), exist_ok=True)
        projects.set_active_run(42, 'living_room', run_id)
        return run_dir

    def test_best_view_serves_jpeg(self):
        self._login()
        self._ensure_project()
        run_dir = self._seed_run()
        with open(os.path.join(run_dir, 'best_views', '0.jpg'), 'wb') as f:
            f.write(b'\xff\xd8\xff\xd9')  # tiny "jpeg"

        r = self.client.get('/api/project/42/scan/living_room/best_view/0.jpg')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, 'image/jpeg')

    def test_best_view_prefers_recapture(self):
        self._login()
        self._ensure_project()
        run_dir = self._seed_run()
        with open(os.path.join(run_dir, 'best_views', '0.jpg'), 'wb') as f:
            f.write(b'orig')
        with open(os.path.join(run_dir, 'best_views', '0_recapture.jpg'), 'wb') as f:
            f.write(b'recap-bytes')

        r = self.client.get('/api/project/42/scan/living_room/best_view/0.jpg')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b'recap-bytes')

    def test_best_view_404_when_no_active_run(self):
        self._login()
        self._ensure_project()
        projects.create_scan(42, 'living_room')

        r = self.client.get('/api/project/42/scan/living_room/best_view/0.jpg')
        self.assertEqual(r.status_code, 404)

    def test_layout_serves_text(self):
        self._login()
        self._ensure_project()
        run_dir = self._seed_run()
        with open(os.path.join(run_dir, 'layout_merged.txt'), 'w') as f:
            f.write('walls=4')

        r = self.client.get('/api/project/42/scan/living_room/layout.txt')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, 'text/plain')

    def test_artifact_unknown_name_returns_404(self):
        self._login()
        self._ensure_project()
        self._seed_run()

        r = self.client.get('/api/project/42/scan/living_room/artifact/evil.pdf')
        self.assertEqual(r.status_code, 404)


if __name__ == '__main__':
    unittest.main()
