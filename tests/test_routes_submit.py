"""Flask-route tests for the Step 6 submit pipeline.

Mirrors the pattern from `tests/test_routes_review.py`:
  - Drive routes via `app.app.test_client()`.
  - Patch `auth.ATHATHI_DIR` and `projects.PROJECTS_ROOT` to per-test
    tmpdirs.
  - Mock `athathi_proxy.upload_bundle` and `athathi_proxy.complete_scan`
    everywhere — never touch the network.

Coverage (per the §16 step 6 brief):
  1.  POST /submit                         401 when not logged in.
  2.  POST /submit                         400 on gating (unreviewed scan).
  3.  POST /submit happy path with upload  upload + complete mocked OK
                                           → manifest stamped, 200.
  4.  POST /submit happy path no endpoint  upload skipped, complete OK
                                           → manifest stamped, 200.
  5.  POST /submit network failure on upload → 202 + submit_pending=True.
  6.  POST /submit upload OK + complete 5xx → 502 + submit_pending=True.
  7.  POST /submit when already submitted   → 200 with already_submitted_at.
  8.  GET  /submit/preview                 returns a summary, no upstream
                                           call.
  9.  POST /submit/retry                   walks pending projects.
  10. GET / PATCH /api/settings/upload_filter round-trip.
  11. Hook execution: surfaces stdout_tail/stderr_tail/returncode.
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

import auth          # noqa: E402
import athathi_proxy # noqa: E402
import projects      # noqa: E402
import review        # noqa: E402
import submit        # noqa: E402
import app           # noqa: E402


# ---------------------------------------------------------------------------
# JWT helpers (copied from sister test files for independence).
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

class _SubmitRouteBase(unittest.TestCase):
    def setUp(self):
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
        # Default config: upload_endpoint configured.
        auth.save_config({
            'api_url': 'http://upstream.test',
            'upload_endpoint': 'http://upload.test/api/upload',
            'last_user': 'tech',
            'post_submit_hook': None,
            'image_transport': 'multipart',
            'visual_search_cache_ttl_s': 86400,
        })

        self.tmp_projects = tempfile.mkdtemp()
        self._orig_projects_root = projects.PROJECTS_ROOT
        projects.PROJECTS_ROOT = self.tmp_projects

        self.tmp_sessions = tempfile.mkdtemp()
        self._orig_sessions_file = app.SESSIONS_FILE
        app.SESSIONS_FILE = os.path.join(self.tmp_sessions, 'sessions.json')

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

    # ------------------------------------------------------------------

    def _login(self):
        auth.write_token(_fresh_jwt())
        auth.write_auth({'user_id': 7, 'username': 'tech_alice'})

    def _seed_full_project(self, scan_id=42, scan_name='living_room',
                           run_id='20260425_142103',
                           with_review=True, reviewed_at='2026-04-25T13:00:00Z'):
        """Project + one scan + one active run + result.json + review.json."""
        projects.ensure_project(scan_id, athathi_meta={'customer_name': 'Smith'})
        if not os.path.isdir(projects.scan_dir(scan_id, scan_name)):
            projects.create_scan(scan_id, scan_name)
        run_dir = projects.processed_dir_for_run(scan_id, scan_name, run_id)
        os.makedirs(run_dir, exist_ok=True)
        envelope = {
            'job_id': 'j_test',
            'status': 'done',
            'schema_version': 1,
            'scan_id': scan_id,
            'room_name': scan_name,
            'furniture': [
                {'id': 'bbox_0', 'class': 'sofa',
                 'center': [0.0, 0.0, 0.5], 'size': [1.0, 1.0, 1.0],
                 'yaw': 0.0},
            ],
            'best_images': [
                {'bbox_id': 'bbox_0', 'class': 'sofa',
                 'pixel_aabb': [0, 0, 100, 100]},
            ],
        }
        with open(os.path.join(run_dir, 'result.json'), 'w') as f:
            json.dump(envelope, f)
        bv = os.path.join(run_dir, 'best_views')
        os.makedirs(bv, exist_ok=True)
        with open(os.path.join(bv, '0.jpg'), 'wb') as f:
            f.write(b'\xff\xd8fake')
        projects.set_active_run(scan_id, scan_name, run_id)
        if with_review:
            rv = review.initial_review(scan_id, scan_name, envelope)
            for bid in rv['bboxes']:
                rv['bboxes'][bid]['status'] = review.STATUS_KEPT
            if reviewed_at:
                rv['reviewed_at'] = reviewed_at
            review.write_review(run_dir, rv)
        return run_dir


# ---------------------------------------------------------------------------
# 1. POST /submit 401
# ---------------------------------------------------------------------------

class TestSubmitAuthGate(_SubmitRouteBase):
    def test_401_when_not_logged_in(self):
        self._seed_full_project()
        r = self.client.post('/api/project/42/submit')
        self.assertEqual(r.status_code, 401)


# ---------------------------------------------------------------------------
# 2. POST /submit 400 on gating
# ---------------------------------------------------------------------------

class TestSubmitGating(_SubmitRouteBase):
    def test_400_when_scan_not_reviewed(self):
        self._login()
        self._seed_full_project(with_review=False)
        r = self.client.post('/api/project/42/submit')
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertIn('not reviewed', body['error'])

    def test_404_when_project_missing(self):
        self._login()
        # No project on disk.
        r = self.client.post('/api/project/999/submit')
        self.assertEqual(r.status_code, 404)


# ---------------------------------------------------------------------------
# 3. POST /submit happy path with upload_endpoint
# ---------------------------------------------------------------------------

class TestSubmitHappyPathWithUpload(_SubmitRouteBase):
    def test_uploads_and_completes_and_stamps(self):
        self._login()
        self._seed_full_project()
        with mock.patch.object(athathi_proxy, 'upload_bundle',
                               return_value={'received': 1}) as m_up, \
             mock.patch.object(athathi_proxy, 'complete_scan',
                               return_value={'message': 'completed',
                                             'scan_id': 42}) as m_done:
            r = self.client.post('/api/project/42/submit')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertTrue(body['ok'])
        self.assertIsNotNone(body.get('completed_at'))
        # Both upstream calls happened.
        m_up.assert_called_once()
        m_done.assert_called_once_with(auth.read_token(), 42)
        # Manifest stamped.
        m = projects.read_manifest(42)
        self.assertTrue(m.get('submitted_at'))
        # review.submitted_at stamped.
        rd = projects.processed_dir_for_run(42, 'living_room', '20260425_142103')
        rv = review.read_review(rd)
        self.assertTrue(rv.get('submitted_at'))


# ---------------------------------------------------------------------------
# 4. POST /submit happy path with no upload_endpoint
# ---------------------------------------------------------------------------

class TestSubmitHappyPathNoEndpoint(_SubmitRouteBase):
    def test_renders_locally_calls_complete_stamps(self):
        self._login()
        self._seed_full_project()
        # Disable upload_endpoint.
        cfg = auth.load_config()
        cfg['upload_endpoint'] = None
        auth.save_config(cfg)

        with mock.patch.object(athathi_proxy, 'upload_bundle') as m_up, \
             mock.patch.object(athathi_proxy, 'complete_scan',
                               return_value={'message': 'completed'}) as m_done:
            r = self.client.post('/api/project/42/submit')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # upload_bundle never called.
        m_up.assert_not_called()
        m_done.assert_called_once()
        m = projects.read_manifest(42)
        self.assertTrue(m.get('submitted_at'))
        # Local files written.
        rd = projects.processed_dir_for_run(42, 'living_room', '20260425_142103')
        self.assertTrue(os.path.isfile(os.path.join(rd, 'result_reviewed.json')))
        self.assertTrue(os.path.isfile(os.path.join(rd, 'result_for_upload.json')))


# ---------------------------------------------------------------------------
# 5. POST /submit network failure on upload → 202
# ---------------------------------------------------------------------------

class TestSubmitNetworkFailureOnUpload(_SubmitRouteBase):
    def test_network_error_returns_202_and_marks_pending(self):
        self._login()
        self._seed_full_project()
        with mock.patch.object(athathi_proxy, 'upload_bundle',
                               side_effect=athathi_proxy.AthathiError(
                                   0, 'no net', 'Network error')), \
             mock.patch.object(athathi_proxy, 'complete_scan') as m_done:
            r = self.client.post('/api/project/42/submit')
        self.assertEqual(r.status_code, 202)
        body = r.get_json()
        self.assertTrue(body.get('queued'))
        # complete_scan must NOT have been called when upload failed first.
        m_done.assert_not_called()
        m = projects.read_manifest(42)
        self.assertIs(m.get('submit_pending'), True)
        self.assertFalse(m.get('submitted_at'))
        # BACKEND-B2: stage marker stamped so the retry sweep re-uploads.
        self.assertEqual(m.get('submit_pending_stage'), 'upload')


# ---------------------------------------------------------------------------
# 6. POST /submit upload OK + complete 5xx → 502 + pending
# ---------------------------------------------------------------------------

class TestSubmitCompleteServerError(_SubmitRouteBase):
    def test_5xx_on_complete_returns_502_and_marks_pending(self):
        self._login()
        self._seed_full_project()
        with mock.patch.object(athathi_proxy, 'upload_bundle',
                               return_value={'received': 1}), \
             mock.patch.object(athathi_proxy, 'complete_scan',
                               side_effect=athathi_proxy.AthathiError(
                                   503, 'svc down', 'Upstream 503')):
            r = self.client.post('/api/project/42/submit')
        self.assertEqual(r.status_code, 502)
        body = r.get_json()
        self.assertEqual(body.get('upstream_status'), 503)
        m = projects.read_manifest(42)
        self.assertIs(m.get('submit_pending'), True)
        self.assertFalse(m.get('submitted_at'))
        # BACKEND-B2: upload already succeeded → stage='complete'.
        self.assertEqual(m.get('submit_pending_stage'), 'complete')


# ---------------------------------------------------------------------------
# 7. POST /submit when already submitted
# ---------------------------------------------------------------------------

class TestSubmitAlreadyDone(_SubmitRouteBase):
    def test_returns_already_submitted_at(self):
        self._login()
        self._seed_full_project()
        m = projects.read_manifest(42)
        m['submitted_at'] = '2026-04-24T11:22:33Z'
        projects.write_manifest(42, m)
        with mock.patch.object(athathi_proxy, 'upload_bundle') as m_up, \
             mock.patch.object(athathi_proxy, 'complete_scan') as m_done:
            r = self.client.post('/api/project/42/submit')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body.get('already_submitted_at'),
                         '2026-04-24T11:22:33Z')
        self.assertFalse(body.get('rehooked'))
        m_up.assert_not_called()
        m_done.assert_not_called()

    def test_resubmit_with_prior_hook_ok_does_not_rerun_hook(self):
        # Plan §22c: when manifest.submitted_at is already set AND the
        # prior hook status is 'ok', re-submit is a true no-op.
        self._login()
        self._seed_full_project()
        cfg = auth.load_config()
        cfg['post_submit_hook'] = '/bin/echo'
        auth.save_config(cfg)
        m = projects.read_manifest(42)
        m['submitted_at'] = '2026-04-24T11:22:33Z'
        m['post_submit_hook_status'] = 'ok'
        m['post_submit_hook_log'] = {
            'ok': True, 'returncode': 0,
            'stdout_tail': 'prior-good\n', 'stderr_tail': '',
            'error': None,
        }
        projects.write_manifest(42, m)

        with mock.patch.object(athathi_proxy, 'upload_bundle') as m_up, \
             mock.patch.object(athathi_proxy, 'complete_scan') as m_done, \
             mock.patch('submit.subprocess.run') as m_run:
            r = self.client.post('/api/project/42/submit')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body.get('already_submitted_at'),
                         '2026-04-24T11:22:33Z')
        self.assertIs(body.get('rehooked'), False)
        self.assertEqual(body.get('post_submit_hook_status'), 'ok')
        # Hook MUST NOT be invoked.
        m_run.assert_not_called()
        m_up.assert_not_called()
        m_done.assert_not_called()
        # Manifest log preserved verbatim.
        m_after = projects.read_manifest(42)
        self.assertEqual(m_after['post_submit_hook_status'], 'ok')
        self.assertEqual(
            m_after['post_submit_hook_log']['stdout_tail'], 'prior-good\n',
        )

    def test_resubmit_with_prior_hook_failed_reruns_hook(self):
        # Plan §22c: when manifest.submitted_at is set but the prior hook
        # failed, re-submit must re-run the hook (only).
        self._login()
        self._seed_full_project()
        cfg = auth.load_config()
        cfg['post_submit_hook'] = '/bin/echo'
        auth.save_config(cfg)
        m = projects.read_manifest(42)
        m['submitted_at'] = '2026-04-24T11:22:33Z'
        m['post_submit_hook_status'] = 'failed'
        m['post_submit_hook_log'] = {
            'ok': False, 'returncode': 1,
            'stdout_tail': '', 'stderr_tail': 'prior-error\n',
            'error': None,
        }
        projects.write_manifest(42, m)

        # Mock the hook to succeed this time. submit.run_post_submit_hook
        # uses subprocess.run; patch it to a CompletedProcess(returncode=0).
        fake_proc = mock.Mock()
        fake_proc.returncode = 0
        fake_proc.stdout = 'rehook-ok\n'
        fake_proc.stderr = ''

        with mock.patch.object(athathi_proxy, 'upload_bundle') as m_up, \
             mock.patch.object(athathi_proxy, 'complete_scan') as m_done, \
             mock.patch('submit.subprocess.run',
                        return_value=fake_proc) as m_run:
            r = self.client.post('/api/project/42/submit')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body.get('already_submitted_at'),
                         '2026-04-24T11:22:33Z')
        self.assertIs(body.get('rehooked'), True)
        self.assertEqual(body.get('post_submit_hook_status'), 'ok')
        # Hook ran exactly once.
        self.assertEqual(m_run.call_count, 1)
        # Upload + complete still skipped (idempotency).
        m_up.assert_not_called()
        m_done.assert_not_called()
        # Manifest now reflects the successful rehook.
        m_after = projects.read_manifest(42)
        self.assertEqual(m_after['post_submit_hook_status'], 'ok')
        self.assertEqual(m_after['post_submit_hook_log']['returncode'], 0)
        self.assertIn('rehook-ok', m_after['post_submit_hook_log']['stdout_tail'])

    def test_resubmit_with_no_hook_configured_does_not_rerun(self):
        # No post_submit_hook configured → idempotent no-op even if a
        # spurious failure status is somehow on the manifest.
        self._login()
        self._seed_full_project()
        # post_submit_hook stays None (set in setUp).
        m = projects.read_manifest(42)
        m['submitted_at'] = '2026-04-24T11:22:33Z'
        m['post_submit_hook_status'] = 'failed'  # would trigger if hook were set.
        projects.write_manifest(42, m)

        with mock.patch.object(athathi_proxy, 'upload_bundle') as m_up, \
             mock.patch.object(athathi_proxy, 'complete_scan') as m_done, \
             mock.patch('submit.subprocess.run') as m_run:
            r = self.client.post('/api/project/42/submit')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body.get('already_submitted_at'),
                         '2026-04-24T11:22:33Z')
        self.assertIs(body.get('rehooked'), False)
        m_run.assert_not_called()
        m_up.assert_not_called()
        m_done.assert_not_called()


# ---------------------------------------------------------------------------
# 8. GET /submit/preview
# ---------------------------------------------------------------------------

class TestSubmitPreview(_SubmitRouteBase):
    def test_returns_summary_no_upstream(self):
        self._login()
        self._seed_full_project()
        with mock.patch.object(athathi_proxy, 'upload_bundle') as m_up, \
             mock.patch.object(athathi_proxy, 'complete_scan') as m_done:
            r = self.client.get('/api/project/42/submit/preview')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(len(body['scans']), 1)
        scan = body['scans'][0]
        self.assertEqual(scan['scan_name'], 'living_room')
        self.assertEqual(scan['run_id'], '20260425_142103')
        self.assertGreater(scan['reviewed_size'], 0)
        self.assertGreater(scan['upload_size'], 0)
        self.assertEqual(scan['n_images'], 1)
        m_up.assert_not_called()
        m_done.assert_not_called()


# ---------------------------------------------------------------------------
# 9. POST /submit/retry walks pending
# ---------------------------------------------------------------------------

class TestSubmitRetry(_SubmitRouteBase):
    def test_walks_pending_projects(self):
        self._login()
        self._seed_full_project(scan_id=42)
        m = projects.read_manifest(42)
        m['submit_pending'] = True
        projects.write_manifest(42, m)

        with mock.patch.object(athathi_proxy, 'complete_scan',
                               return_value={'message': 'completed'}) as m_done:
            r = self.client.post('/api/project/42/submit/retry')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(len(body['results']), 1)
        self.assertEqual(body['results'][0]['status'], 'submitted')
        m_done.assert_called_once_with(auth.read_token(), 42)
        m_after = projects.read_manifest(42)
        self.assertFalse(m_after.get('submit_pending'))
        self.assertTrue(m_after.get('submitted_at'))

    def test_401_when_not_logged_in(self):
        r = self.client.post('/api/project/42/submit/retry')
        self.assertEqual(r.status_code, 401)

    def test_retry_with_stage_upload_re_uploads_first(self):
        # BACKEND-B2: when manifest.submit_pending_stage == 'upload', the
        # retry route must call upload_bundle BEFORE complete_scan.
        self._login()
        self._seed_full_project(scan_id=42)
        m = projects.read_manifest(42)
        m['submit_pending'] = True
        m['submit_pending_stage'] = 'upload'
        projects.write_manifest(42, m)

        with mock.patch.object(athathi_proxy, 'upload_bundle',
                               return_value={'received': 1}) as m_up, \
             mock.patch.object(athathi_proxy, 'complete_scan',
                               return_value={'message': 'ok'}) as m_done:
            r = self.client.post('/api/project/42/submit/retry')
        self.assertEqual(r.status_code, 200)
        m_up.assert_called_once()
        m_done.assert_called_once_with(auth.read_token(), 42)
        body = r.get_json()
        self.assertEqual(body['results'][0]['status'], 'submitted')
        m_after = projects.read_manifest(42)
        self.assertFalse(m_after.get('submit_pending'))
        self.assertNotIn('submit_pending_stage', m_after)
        self.assertTrue(m_after.get('submitted_at'))

    def test_retry_with_stage_complete_skips_upload(self):
        # BACKEND-B2: stage='complete' → no upload, just complete.
        self._login()
        self._seed_full_project(scan_id=42)
        m = projects.read_manifest(42)
        m['submit_pending'] = True
        m['submit_pending_stage'] = 'complete'
        projects.write_manifest(42, m)

        with mock.patch.object(athathi_proxy, 'upload_bundle') as m_up, \
             mock.patch.object(athathi_proxy, 'complete_scan',
                               return_value={'message': 'ok'}) as m_done:
            r = self.client.post('/api/project/42/submit/retry')
        self.assertEqual(r.status_code, 200)
        m_up.assert_not_called()
        m_done.assert_called_once_with(auth.read_token(), 42)
        m_after = projects.read_manifest(42)
        self.assertFalse(m_after.get('submit_pending'))
        self.assertTrue(m_after.get('submitted_at'))


# ---------------------------------------------------------------------------
# 10. GET / PATCH /api/settings/upload_filter
# ---------------------------------------------------------------------------

class TestUploadFilterRoutes(_SubmitRouteBase):
    def test_get_returns_default_when_missing(self):
        self._login()
        r = self.client.get('/api/settings/upload_filter')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertIn('include_paths', body)
        self.assertEqual(body['version'], 1)

    def test_patch_round_trips(self):
        self._login()
        new_filter = {
            'version': 2,
            'include_paths': ['scan_id', 'room_name'],
            'exclude_paths': [],
        }
        r = self.client.patch('/api/settings/upload_filter', json=new_filter)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['filter']['version'], 2)
        # GET reflects the change.
        r2 = self.client.get('/api/settings/upload_filter')
        self.assertEqual(r2.get_json()['version'], 2)
        self.assertEqual(r2.get_json()['include_paths'],
                         ['scan_id', 'room_name'])

    def test_patch_rejects_bad_shape(self):
        self._login()
        r = self.client.patch('/api/settings/upload_filter',
                              json={'include_paths': 'not-a-list'})
        self.assertEqual(r.status_code, 400)

    def test_get_401_when_not_logged_in(self):
        r = self.client.get('/api/settings/upload_filter')
        self.assertEqual(r.status_code, 401)


# ---------------------------------------------------------------------------
# 11. Hook execution surfaces stdout_tail/stderr_tail/returncode
# ---------------------------------------------------------------------------

class TestSubmitHookSurfacing(_SubmitRouteBase):
    def test_hook_runs_and_response_includes_log(self):
        self._login()
        self._seed_full_project()
        # Configure a hook that exits 0 with a known stdout.
        cfg = auth.load_config()
        cfg['post_submit_hook'] = '/bin/echo'
        auth.save_config(cfg)

        with mock.patch.object(athathi_proxy, 'upload_bundle',
                               return_value={'received': 1}), \
             mock.patch.object(athathi_proxy, 'complete_scan',
                               return_value={'message': 'completed'}):
            r = self.client.post('/api/project/42/submit')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertIn('hook', body)
        self.assertTrue(body['hook']['ran'])
        self.assertEqual(body['hook']['returncode'], 0)
        # stdout should mention the scan_id 42 (echo's argv).
        self.assertIn('42', body['hook']['stdout_tail'])

        # Manifest captures the hook log too.
        m = projects.read_manifest(42)
        self.assertIn('post_submit_hook_log', m)
        self.assertEqual(m['post_submit_hook_log']['returncode'], 0)
        self.assertEqual(m['post_submit_hook_status'], 'ok')

    def test_hook_failure_does_not_block_submit(self):
        self._login()
        self._seed_full_project()
        cfg = auth.load_config()
        cfg['post_submit_hook'] = '/no/such/hook/binary'
        auth.save_config(cfg)

        with mock.patch.object(athathi_proxy, 'upload_bundle',
                               return_value={'received': 1}), \
             mock.patch.object(athathi_proxy, 'complete_scan',
                               return_value={'message': 'completed'}):
            r = self.client.post('/api/project/42/submit')
        # Submit still 200 — hook is best-effort.
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body['hook']['ok'])
        self.assertIn('error', body['hook'])
        # Manifest still stamped.
        m = projects.read_manifest(42)
        self.assertTrue(m.get('submitted_at'))


# ---------------------------------------------------------------------------
# BACKEND-B3: per-scan_id submit lock — concurrent submits don't race
# ---------------------------------------------------------------------------

import threading as _threading


class TestSubmitConcurrency(_SubmitRouteBase):
    """Two threads racing POST /submit on the same scan_id must serialise.

    The first wins (uploads + completes + stamps `submitted_at`); the
    second sees `already_submitted_at` and short-circuits — `upload_bundle`
    + `complete_scan` are called exactly ONCE between the two requests.
    """

    def test_two_concurrent_submits_only_one_uploads(self):
        self._login()
        self._seed_full_project()

        upload_calls = {'n': 0}
        complete_calls = {'n': 0}
        upload_lock = _threading.Lock()
        complete_lock = _threading.Lock()

        def slow_upload(*_a, **_kw):
            # Slow enough that the second thread will reach the lock
            # while we still hold it.
            with upload_lock:
                upload_calls['n'] += 1
            import time as _t
            _t.sleep(0.1)
            return {'received': 1}

        def slow_complete(*_a, **_kw):
            with complete_lock:
                complete_calls['n'] += 1
            return {'message': 'completed'}

        results = [None, None]
        errors = [None, None]

        def worker(i):
            try:
                local_client = app.app.test_client()
                results[i] = local_client.post('/api/project/42/submit')
            except Exception as e:  # pragma: no cover - asserted below
                errors[i] = e

        with mock.patch.object(athathi_proxy, 'upload_bundle',
                               side_effect=slow_upload), \
             mock.patch.object(athathi_proxy, 'complete_scan',
                               side_effect=slow_complete):
            t1 = _threading.Thread(target=worker, args=(0,))
            t2 = _threading.Thread(target=worker, args=(1,))
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

        self.assertIsNone(errors[0])
        self.assertIsNone(errors[1])
        self.assertIsNotNone(results[0])
        self.assertIsNotNone(results[1])
        # Both 200.
        self.assertEqual(results[0].status_code, 200,
                         results[0].get_data(as_text=True))
        self.assertEqual(results[1].status_code, 200,
                         results[1].get_data(as_text=True))
        # Exactly ONE upload + ONE complete across the two requests.
        self.assertEqual(upload_calls['n'], 1)
        self.assertEqual(complete_calls['n'], 1)
        # One body has `ok: True`, the other has `already_submitted_at`.
        bodies = [results[0].get_json(), results[1].get_json()]
        first_done = [b for b in bodies if b.get('completed_at')]
        idem = [b for b in bodies if b.get('already_submitted_at')]
        self.assertEqual(len(first_done), 1)
        self.assertEqual(len(idem), 1)


if __name__ == '__main__':
    unittest.main()
