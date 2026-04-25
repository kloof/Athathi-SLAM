"""Flask-route tests for the Step 5 technician review surface.

Mirrors the pattern from `tests/test_routes_scoped_processing.py`:
  - Drive routes via `app.app.test_client()`.
  - Patch `auth.ATHATHI_DIR` and `projects.PROJECTS_ROOT` to per-test
    tmpdirs.
  - Mock `subprocess.run` for ffmpeg so the recapture endpoint never
    spawns a real process.
  - Mock `app._is_recording` so we never touch /dev/video0.

Coverage (per the §16 step 5 brief):
  1.  GET  /review                             401 when not logged in.
  2.  GET  /review                             happy path returns
                                               bboxes from result.json
                                               when no review.json exists.
  3.  PATCH /review                            class_override updates and
                                               persists.
  4.  PATCH /review                            rejects multi-mutation.
  5.  POST  /review/merge                      writes the right state.
  6.  POST  /review/recapture/0                409 while recording.
  7.  POST  /review/recapture/0                happy path: ffmpeg mocked,
                                               JPEG path created, review
                                               updated.
  8.  POST  /review/mark_reviewed              stamps reviewed_at.
  9.  GET   /preview_reviewed.json             returns the rendered envelope.
  10. GET   /review/runs                       lists every run.
  11. POST  /review/active_run                 switches the active pointer.
  12. DELETE /runs/<id>                        refuses on active / submitted.
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

import auth     # noqa: E402
import projects # noqa: E402
import review   # noqa: E402
import app      # noqa: E402


# ---------------------------------------------------------------------------
# JWT helpers
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

class _ReviewRouteBase(unittest.TestCase):
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

    # Helpers -----------------------------------------------------------

    def _login(self):
        auth.write_token(_fresh_jwt())

    def _ensure_project(self, scan_id=42, name='Smith'):
        projects.ensure_project(scan_id, athathi_meta={'customer_name': name})

    def _seed_run(self, scan_id=42, scan_name='living_room',
                  run_id='20260425_142103', envelope=None):
        """Create a project + scan + active run with a result.json on disk."""
        if projects.read_manifest(scan_id) is None:
            self._ensure_project(scan_id=scan_id)
        if not os.path.isdir(projects.scan_dir(scan_id, scan_name)):
            projects.create_scan(scan_id, scan_name)
        rd = projects.processed_dir_for_run(scan_id, scan_name, run_id)
        os.makedirs(rd, exist_ok=True)
        if envelope is None:
            envelope = {
                'job_id': 'j_test',
                'status': 'done',
                'furniture': [
                    {'id': 'bbox_0', 'class': 'sofa',
                     'center': [0.0, 0.0, 0.5], 'size': [1.0, 1.0, 1.0],
                     'yaw': 0.0},
                    {'id': 'bbox_1', 'class': 'chair',
                     'center': [2.0, 0.0, 0.5], 'size': [1.0, 1.0, 1.0],
                     'yaw': 0.0},
                ],
                'best_images': [
                    {'bbox_id': 'bbox_0', 'class': 'sofa',
                     'pixel_aabb': [0, 0, 100, 100]},
                    {'bbox_id': 'bbox_1', 'class': 'chair',
                     'pixel_aabb': [0, 0, 100, 100]},
                ],
            }
        with open(os.path.join(rd, 'result.json'), 'w') as f:
            json.dump(envelope, f)
        # Drop a meta.json the runs route can pick up.
        with open(os.path.join(rd, 'meta.json'), 'w') as f:
            json.dump({'job_id': 'j_test', 'status': 'done'}, f)
        projects.set_active_run(scan_id, scan_name, run_id)
        return rd


# ---------------------------------------------------------------------------
# 1. GET /review auth gate
# ---------------------------------------------------------------------------

class TestReviewGetAuthGate(_ReviewRouteBase):
    def test_not_logged_in_returns_401(self):
        self._ensure_project()
        projects.create_scan(42, 'living_room')
        r = self.client.get('/api/project/42/scan/living_room/review')
        self.assertEqual(r.status_code, 401)


# ---------------------------------------------------------------------------
# 2. GET /review happy path with no review.json on disk
# ---------------------------------------------------------------------------

class TestReviewGetHappyPath(_ReviewRouteBase):
    def test_returns_initial_review_from_result(self):
        self._login()
        self._seed_run()
        r = self.client.get('/api/project/42/scan/living_room/review')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body['run_id'], '20260425_142103')
        rv = body['review']
        self.assertEqual(set(rv['bboxes'].keys()), {'bbox_0', 'bbox_1'})
        for v in rv['bboxes'].values():
            self.assertEqual(v['status'], review.STATUS_UNTOUCHED)

    def test_no_active_run_returns_404(self):
        self._login()
        self._ensure_project()
        projects.create_scan(42, 'living_room')
        r = self.client.get('/api/project/42/scan/living_room/review')
        self.assertEqual(r.status_code, 404)


# ---------------------------------------------------------------------------
# 3-4. PATCH /review
# ---------------------------------------------------------------------------

class TestReviewPatch(_ReviewRouteBase):
    def test_class_override_persists(self):
        self._login()
        rd = self._seed_run()
        r = self.client.patch(
            '/api/project/42/scan/living_room/review',
            json={'bbox_id': 'bbox_0', 'class_override': 'armchair'},
        )
        self.assertEqual(r.status_code, 200)
        # Persisted on disk?
        rv = review.read_review(rd)
        self.assertIsNotNone(rv)
        self.assertEqual(rv['bboxes']['bbox_0']['class_override'], 'armchair')

    def test_status_with_extras(self):
        self._login()
        rd = self._seed_run()
        r = self.client.patch(
            '/api/project/42/scan/living_room/review',
            json={'bbox_id': 'bbox_0', 'status': 'deleted',
                  'reason': 'duplicate'},
        )
        self.assertEqual(r.status_code, 200)
        rv = review.read_review(rd)
        self.assertEqual(rv['bboxes']['bbox_0']['status'], 'deleted')
        self.assertEqual(rv['bboxes']['bbox_0']['reason'], 'duplicate')

    def test_notes(self):
        self._login()
        rd = self._seed_run()
        r = self.client.patch(
            '/api/project/42/scan/living_room/review',
            json={'notes': 'a quick note'},
        )
        self.assertEqual(r.status_code, 200)
        rv = review.read_review(rd)
        self.assertEqual(rv['notes'], 'a quick note')

    def test_rejects_multi_mutation_in_one_call(self):
        self._login()
        self._seed_run()
        r = self.client.patch(
            '/api/project/42/scan/living_room/review',
            json={'bbox_id': 'bbox_0',
                  'class_override': 'armchair',
                  'image_override': 'best_views/0_recapture.jpg'},
        )
        self.assertEqual(r.status_code, 400)

    def test_rejects_notes_plus_bbox_mutation(self):
        self._login()
        self._seed_run()
        r = self.client.patch(
            '/api/project/42/scan/living_room/review',
            json={'bbox_id': 'bbox_0', 'class_override': 'armchair',
                  'notes': 'no'},
        )
        self.assertEqual(r.status_code, 400)

    def test_invalid_status_returns_400(self):
        self._login()
        self._seed_run()
        r = self.client.patch(
            '/api/project/42/scan/living_room/review',
            json={'bbox_id': 'bbox_0', 'status': 'banana'},
        )
        self.assertEqual(r.status_code, 400)

    def test_unknown_bbox_id_returns_400_and_does_not_mutate(self):
        # PATCH for an id not present in result.json must be refused
        # with valid_count populated, and review.json on disk must
        # remain untouched (no ghost bbox_999 entry).
        self._login()
        rd = self._seed_run()
        r = self.client.patch(
            '/api/project/42/scan/living_room/review',
            json={'bbox_id': 'bbox_999', 'class_override': 'x'},
        )
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertIn('bbox_999', body.get('error', ''))
        self.assertEqual(body.get('valid_count'), 2)
        # No review.json should have been written; if one exists it
        # must NOT contain bbox_999.
        rv = review.read_review(rd)
        if rv is not None:
            self.assertNotIn('bbox_999', rv.get('bboxes') or {})


# ---------------------------------------------------------------------------
# 5. POST /review/merge
# ---------------------------------------------------------------------------

class TestReviewMerge(_ReviewRouteBase):
    def test_merge_writes_state(self):
        self._login()
        rd = self._seed_run()
        r = self.client.post(
            '/api/project/42/scan/living_room/review/merge',
            json={'primary_id': 'bbox_0',
                  'member_ids': ['bbox_1'],
                  'chosen_class': 'lounge_chair'},
        )
        self.assertEqual(r.status_code, 200)
        rv = review.read_review(rd)
        self.assertEqual(rv['bboxes']['bbox_0']['status'], review.STATUS_KEPT)
        self.assertEqual(rv['bboxes']['bbox_0']['merged_from'], ['bbox_1'])
        self.assertEqual(rv['bboxes']['bbox_0']['class_override'],
                         'lounge_chair')
        self.assertEqual(rv['bboxes']['bbox_1']['status'],
                         review.STATUS_MERGED_INTO)
        self.assertEqual(rv['bboxes']['bbox_1']['target'], 'bbox_0')

    def test_merge_unknown_member_returns_400(self):
        self._login()
        self._seed_run()
        r = self.client.post(
            '/api/project/42/scan/living_room/review/merge',
            json={'primary_id': 'bbox_0', 'member_ids': ['bbox_99']},
        )
        self.assertEqual(r.status_code, 400)

    def test_remerge_into_different_primary_returns_409(self):
        # Seed three bboxes so we can attempt a re-merge of bbox_1 from
        # bbox_0 (its current primary) into bbox_2 — must be refused.
        self._login()
        envelope = {
            'job_id': 'j_test',
            'status': 'done',
            'furniture': [
                {'id': 'bbox_0', 'class': 'sofa',
                 'center': [0.0, 0.0, 0.5], 'size': [1.0, 1.0, 1.0],
                 'yaw': 0.0},
                {'id': 'bbox_1', 'class': 'chair',
                 'center': [2.0, 0.0, 0.5], 'size': [1.0, 1.0, 1.0],
                 'yaw': 0.0},
                {'id': 'bbox_2', 'class': 'chair',
                 'center': [4.0, 0.0, 0.5], 'size': [1.0, 1.0, 1.0],
                 'yaw': 0.0},
            ],
            'best_images': [],
        }
        rd = self._seed_run(envelope=envelope)
        # First merge: bbox_1 -> bbox_0.
        r1 = self.client.post(
            '/api/project/42/scan/living_room/review/merge',
            json={'primary_id': 'bbox_0', 'member_ids': ['bbox_1']},
        )
        self.assertEqual(r1.status_code, 200)
        rv_after_first = review.read_review(rd)
        self.assertEqual(rv_after_first['bboxes']['bbox_1']['target'],
                         'bbox_0')
        self.assertEqual(rv_after_first['bboxes']['bbox_0']['merged_from'],
                         ['bbox_1'])

        # Conflicting second merge: bbox_1 -> bbox_2 must be 409.
        r2 = self.client.post(
            '/api/project/42/scan/living_room/review/merge',
            json={'primary_id': 'bbox_2', 'member_ids': ['bbox_1']},
        )
        self.assertEqual(r2.status_code, 409)
        body = r2.get_json()
        self.assertIn('bbox_1', body.get('error', ''))
        self.assertIn('bbox_0', body.get('error', ''))

        # Review.json on disk must reflect the FIRST merge unchanged.
        rv_now = review.read_review(rd)
        self.assertEqual(rv_now['bboxes']['bbox_1']['target'], 'bbox_0')
        self.assertEqual(rv_now['bboxes']['bbox_0']['merged_from'],
                         ['bbox_1'])
        # bbox_2 must NOT have any merged_from list.
        self.assertNotIn('merged_from',
                         rv_now['bboxes'].get('bbox_2', {}))


# ---------------------------------------------------------------------------
# 6. POST /review/recapture/0 → 409 while recording
# ---------------------------------------------------------------------------

class TestReviewRecaptureRecordingGuard(_ReviewRouteBase):
    def test_409_while_recording(self):
        self._login()
        self._seed_run()
        with mock.patch.object(app, '_is_recording', return_value=True):
            r = self.client.post(
                '/api/project/42/scan/living_room/review/recapture/0')
        self.assertEqual(r.status_code, 409)


# ---------------------------------------------------------------------------
# 7. POST /review/recapture/0 happy path
# ---------------------------------------------------------------------------

class TestReviewRecaptureHappyPath(_ReviewRouteBase):
    def test_recapture_writes_jpeg_and_updates_review(self):
        self._login()
        rd = self._seed_run()

        captured = {}

        def fake_run(cmd, *args, **kwargs):
            # The command writes the JPEG at the `-y <dst>` slot.
            # We mimic that by writing a non-empty file at the path.
            dst = cmd[-1]
            captured['dst'] = dst
            with open(dst, 'wb') as f:
                f.write(b'\xff\xd8' + b'\x00' * 100)  # fake JPEG header
            return mock.MagicMock(returncode=0, stdout=b'', stderr=b'')

        prev_starting = app._active_recording.get('starting')
        app._active_recording['starting'] = False
        try:
            with mock.patch.object(app, '_is_recording', return_value=False), \
                 mock.patch('app.subprocess.run', side_effect=fake_run):
                r = self.client.post(
                    '/api/project/42/scan/living_room/review/recapture/0')
        finally:
            app._active_recording['starting'] = prev_starting

        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['bbox_id'], 'bbox_0')
        self.assertEqual(body['path'], os.path.join('best_views',
                                                    '0_recapture.jpg'))

        # JPEG file ended up under the run's best_views.
        expected = os.path.join(rd, 'best_views', '0_recapture.jpg')
        self.assertTrue(os.path.isfile(expected))
        # ffmpeg was invoked with that destination.
        self.assertEqual(captured['dst'], expected)

        # review.json updated.
        rv = review.read_review(rd)
        self.assertEqual(rv['bboxes']['bbox_0']['image_override'],
                         os.path.join('best_views', '0_recapture.jpg'))

    def test_recapture_ffmpeg_failure_returns_503(self):
        self._login()
        self._seed_run()

        def fake_run(cmd, *args, **kwargs):
            return mock.MagicMock(
                returncode=1, stdout=b'', stderr=b'/dev/video0: device busy')

        prev_starting = app._active_recording.get('starting')
        app._active_recording['starting'] = False
        try:
            with mock.patch.object(app, '_is_recording', return_value=False), \
                 mock.patch('app.subprocess.run', side_effect=fake_run):
                r = self.client.post(
                    '/api/project/42/scan/living_room/review/recapture/0')
        finally:
            app._active_recording['starting'] = prev_starting

        self.assertEqual(r.status_code, 503)
        self.assertIn('stderr_tail', r.get_json())

    def test_recapture_unknown_idx_returns_404(self):
        self._login()
        self._seed_run()

        prev_starting = app._active_recording.get('starting')
        app._active_recording['starting'] = False
        try:
            with mock.patch.object(app, '_is_recording', return_value=False):
                r = self.client.post(
                    '/api/project/42/scan/living_room/review/recapture/99')
        finally:
            app._active_recording['starting'] = prev_starting
        self.assertEqual(r.status_code, 404)


# ---------------------------------------------------------------------------
# 8. POST /review/mark_reviewed
# ---------------------------------------------------------------------------

class TestReviewMarkReviewed(_ReviewRouteBase):
    def test_stamps_reviewed_at(self):
        self._login()
        rd = self._seed_run()
        r = self.client.post(
            '/api/project/42/scan/living_room/review/mark_reviewed')
        self.assertEqual(r.status_code, 200)
        rv = review.read_review(rd)
        self.assertIsNotNone(rv['reviewed_at'])

    def test_idempotent(self):
        self._login()
        rd = self._seed_run()
        r1 = self.client.post(
            '/api/project/42/scan/living_room/review/mark_reviewed')
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.post(
            '/api/project/42/scan/living_room/review/mark_reviewed')
        self.assertEqual(r2.status_code, 200)
        # Both succeed; reviewed_at is non-null after the second call too.
        rv = review.read_review(rd)
        self.assertIsNotNone(rv['reviewed_at'])


# ---------------------------------------------------------------------------
# 9. GET /preview_reviewed.json
# ---------------------------------------------------------------------------

class TestReviewPreview(_ReviewRouteBase):
    def test_preview_returns_envelope(self):
        self._login()
        rd = self._seed_run()
        # Mark bbox_0 kept, bbox_1 deleted.
        rv = review.initial_review(42, 'living_room',
                                   json.load(open(os.path.join(rd, 'result.json'))))
        rv = review.set_bbox_status(rv, 'bbox_0', review.STATUS_KEPT)
        rv = review.set_bbox_status(rv, 'bbox_1', review.STATUS_DELETED)
        review.write_review(rd, rv)

        r = self.client.get(
            '/api/project/42/scan/living_room/preview_reviewed.json')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        # Only bbox_0 kept.
        self.assertEqual([f['id'] for f in body['furniture']], ['bbox_0'])
        self.assertEqual([b['bbox_id'] for b in body['best_images']],
                         ['bbox_0'])
        self.assertIn('review_meta', body)


# ---------------------------------------------------------------------------
# 10. GET /review/runs
# ---------------------------------------------------------------------------

class TestReviewRunsList(_ReviewRouteBase):
    def test_lists_every_run(self):
        self._login()
        self._ensure_project()
        projects.create_scan(42, 'living_room')

        # Two runs, one active.
        r1 = projects.processed_dir_for_run(42, 'living_room', '20260425_100000')
        r2 = projects.processed_dir_for_run(42, 'living_room', '20260425_120000')
        os.makedirs(r1, exist_ok=True)
        os.makedirs(r2, exist_ok=True)
        with open(os.path.join(r1, 'meta.json'), 'w') as f:
            json.dump({'job_id': 'j_old', 'status': 'done'}, f)
        with open(os.path.join(r2, 'result.json'), 'w') as f:
            json.dump({'furniture': []}, f)
        with open(os.path.join(r2, 'meta.json'), 'w') as f:
            json.dump({'job_id': 'j_new', 'status': 'done'}, f)
        # Drop a review.json on r2 so the route can read reviewed_at.
        with open(os.path.join(r2, 'review.json'), 'w') as f:
            json.dump({'reviewed_at': '2026-04-25T13:00:00Z',
                       'submitted_at': None}, f)
        projects.set_active_run(42, 'living_room', '20260425_120000')

        r = self.client.get('/api/project/42/scan/living_room/review/runs')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body['active_run_id'], '20260425_120000')
        self.assertEqual([x['run_id'] for x in body['runs']],
                         ['20260425_100000', '20260425_120000'])
        actives = {x['run_id']: x['is_active'] for x in body['runs']}
        self.assertEqual(actives,
                         {'20260425_100000': False, '20260425_120000': True})


# ---------------------------------------------------------------------------
# 11. POST /review/active_run
# ---------------------------------------------------------------------------

class TestReviewSwitchActiveRun(_ReviewRouteBase):
    def test_switches_pointer(self):
        self._login()
        self._ensure_project()
        projects.create_scan(42, 'living_room')
        # Two runs.
        rA = projects.processed_dir_for_run(42, 'living_room', 'A')
        rB = projects.processed_dir_for_run(42, 'living_room', 'B')
        os.makedirs(rA, exist_ok=True)
        os.makedirs(rB, exist_ok=True)
        projects.set_active_run(42, 'living_room', 'A')

        r = self.client.post(
            '/api/project/42/scan/living_room/review/active_run',
            json={'run_id': 'B'},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(projects.read_active_run(42, 'living_room'), 'B')

    def test_unknown_run_returns_404(self):
        self._login()
        self._ensure_project()
        projects.create_scan(42, 'living_room')
        r = self.client.post(
            '/api/project/42/scan/living_room/review/active_run',
            json={'run_id': 'missing'},
        )
        self.assertEqual(r.status_code, 404)


# ---------------------------------------------------------------------------
# 12. DELETE /runs/<id>
# ---------------------------------------------------------------------------

class TestReviewDeleteRun(_ReviewRouteBase):
    def test_refuses_active_run(self):
        self._login()
        self._ensure_project()
        projects.create_scan(42, 'living_room')
        rA = projects.processed_dir_for_run(42, 'living_room', 'A')
        os.makedirs(rA, exist_ok=True)
        projects.set_active_run(42, 'living_room', 'A')

        r = self.client.delete('/api/project/42/scan/living_room/runs/A')
        self.assertEqual(r.status_code, 409)
        self.assertTrue(os.path.isdir(rA))

    def test_refuses_submitted_run(self):
        self._login()
        self._ensure_project()
        projects.create_scan(42, 'living_room')
        rA = projects.processed_dir_for_run(42, 'living_room', 'A')
        rB = projects.processed_dir_for_run(42, 'living_room', 'B')
        os.makedirs(rA, exist_ok=True)
        os.makedirs(rB, exist_ok=True)
        projects.set_active_run(42, 'living_room', 'B')
        # rA was previously submitted.
        with open(os.path.join(rA, 'review.json'), 'w') as f:
            json.dump({'submitted_at': '2026-04-25T15:00:00Z'}, f)

        r = self.client.delete('/api/project/42/scan/living_room/runs/A')
        self.assertEqual(r.status_code, 409)
        self.assertTrue(os.path.isdir(rA))

    def test_succeeds_otherwise(self):
        self._login()
        self._ensure_project()
        projects.create_scan(42, 'living_room')
        rA = projects.processed_dir_for_run(42, 'living_room', 'A')
        rB = projects.processed_dir_for_run(42, 'living_room', 'B')
        os.makedirs(rA, exist_ok=True)
        os.makedirs(rB, exist_ok=True)
        projects.set_active_run(42, 'living_room', 'B')

        r = self.client.delete('/api/project/42/scan/living_room/runs/A')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(os.path.exists(rA))


# ---------------------------------------------------------------------------
# 13. POST /review/recapture/<idx> concurrency
# ---------------------------------------------------------------------------

class TestReviewRecaptureConcurrency(_ReviewRouteBase):
    def test_two_concurrent_recaptures_serialise(self):
        # Two simultaneous POSTs to /recapture/0 for the same scan/idx
        # must both succeed (200), the JPEG must end up on disk, the
        # review.json image_override must be set, and ffmpeg must be
        # called twice (the second one wins the file overwrite — fine
        # since both write byte-identical content).
        self._login()
        rd = self._seed_run()

        call_count = {'n': 0}
        call_lock = threading.Lock()
        # Track ffmpeg overlap: if the lock works, the two ffmpeg calls
        # are serial — never overlapping.
        active = {'n': 0}
        max_concurrent = {'n': 0}

        def fake_run(cmd, *args, **kwargs):
            with call_lock:
                call_count['n'] += 1
                active['n'] += 1
                if active['n'] > max_concurrent['n']:
                    max_concurrent['n'] = active['n']
            try:
                _time.sleep(0.05)
                dst = cmd[-1]
                with open(dst, 'wb') as f:
                    f.write(b'\xff\xd8' + b'\x00' * 100)
                return mock.MagicMock(returncode=0, stdout=b'', stderr=b'')
            finally:
                with call_lock:
                    active['n'] -= 1

        prev_starting = app._active_recording.get('starting')
        app._active_recording['starting'] = False

        results = [None, None]
        errors = [None, None]

        def worker(i):
            try:
                # Each thread needs its own client; Flask test_client is
                # not thread-safe across requests sharing the same
                # WSGI environ.
                local_client = app.app.test_client()
                results[i] = local_client.post(
                    '/api/project/42/scan/living_room/review/recapture/0')
            except Exception as e:  # pragma: no cover - surfaced in assert
                errors[i] = e

        try:
            with mock.patch.object(app, '_is_recording', return_value=False), \
                 mock.patch('app.subprocess.run', side_effect=fake_run):
                t1 = threading.Thread(target=worker, args=(0,))
                t2 = threading.Thread(target=worker, args=(1,))
                t1.start()
                t2.start()
                t1.join(timeout=5)
                t2.join(timeout=5)
        finally:
            app._active_recording['starting'] = prev_starting

        self.assertIsNone(errors[0])
        self.assertIsNone(errors[1])
        self.assertIsNotNone(results[0])
        self.assertIsNotNone(results[1])
        self.assertEqual(results[0].status_code, 200, results[0].get_json())
        self.assertEqual(results[1].status_code, 200, results[1].get_json())

        # ffmpeg called twice (once per request) — the second OVERWROTE
        # the first; that's fine because both write identical content.
        self.assertEqual(call_count['n'], 2)
        # The lock kept ffmpeg invocations strictly serialised.
        self.assertEqual(max_concurrent['n'], 1)

        # The recapture file is on disk.
        expected = os.path.join(rd, 'best_views', '0_recapture.jpg')
        self.assertTrue(os.path.isfile(expected))
        # And review.json shows the override (not lost to a race).
        rv = review.read_review(rd)
        self.assertIsNotNone(rv)
        self.assertEqual(rv['bboxes']['bbox_0']['image_override'],
                         os.path.join('best_views', '0_recapture.jpg'))


# ---------------------------------------------------------------------------
# BACKEND-I1: recapture vs start-recording race
# ---------------------------------------------------------------------------

class TestReviewRecaptureRecordingRace(_ReviewRouteBase):
    """A POST /start_recording flipping `_active_recording.starting=True`
    while ffmpeg is being spawned must NOT result in concurrent access to
    /dev/video0. With the lock-then-recheck pattern, the recapture sees
    the flag inside its lock and 409s instead of invoking ffmpeg.
    """

    def test_starting_flag_flips_inside_lock_returns_409(self):
        self._login()
        self._seed_run()

        # Snapshot + reset the recording flags.
        prev_starting = app._active_recording.get('starting')
        app._active_recording['starting'] = False

        ffmpeg_called = {'n': 0}

        def fake_run(*_a, **_kw):
            ffmpeg_called['n'] += 1
            return mock.MagicMock(returncode=0, stdout=b'', stderr=b'')

        # The recapture acquires `_recapture_lock_for(...)` then re-checks
        # the recording flags. We patch `_review_brio_snapshot` to flip
        # the flag from outside JUST BEFORE invoking ffmpeg — but with
        # the inside-the-lock recheck, the route returns 409 BEFORE
        # subprocess.run ever runs.
        # Easier path: flip the flag right before the request and verify
        # the lock-protected recheck catches it.
        try:
            app._active_recording['starting'] = True
            with mock.patch.object(app, '_is_recording', return_value=False), \
                 mock.patch('app.subprocess.run', side_effect=fake_run):
                r = self.client.post(
                    '/api/project/42/scan/living_room/review/recapture/0')
        finally:
            app._active_recording['starting'] = prev_starting

        self.assertEqual(r.status_code, 409)
        # ffmpeg was NEVER spawned.
        self.assertEqual(ffmpeg_called['n'], 0)

    def test_flag_flip_between_outer_and_inner_check_is_caught(self):
        # Tighter scenario: outer guard sees `starting=False`; while we're
        # acquiring the per-scan lock, /start_recording flips it; the
        # inside-the-lock recheck catches the flip and 409s — ffmpeg is
        # NEVER invoked.
        self._login()
        self._seed_run()

        prev_starting = app._active_recording.get('starting')
        app._active_recording['starting'] = False

        ffmpeg_called = {'n': 0}

        def fake_run(*_a, **_kw):
            ffmpeg_called['n'] += 1
            return mock.MagicMock(returncode=0, stdout=b'', stderr=b'')

        # Hold the per-scan lock OUTSIDE the request, flip the flag, then
        # release the lock — the request will re-acquire and see the flip.
        scan_lock = app._recapture_lock_for(42, 'living_room')

        result_holder = {'r': None, 'err': None}

        def worker():
            try:
                local_client = app.app.test_client()
                result_holder['r'] = local_client.post(
                    '/api/project/42/scan/living_room/review/recapture/0')
            except Exception as e:  # pragma: no cover
                result_holder['err'] = e

        try:
            with mock.patch.object(app, '_is_recording', return_value=False), \
                 mock.patch('app.subprocess.run', side_effect=fake_run):
                scan_lock.acquire()
                t = threading.Thread(target=worker)
                t.start()
                # Give the worker time to enter the route + the outer
                # guard (which passes since starting=False), then to
                # block on the per-scan lock.
                _time.sleep(0.05)
                # Now flip the recording flag — when we release the lock
                # the worker will re-check INSIDE and 409.
                app._active_recording['starting'] = True
                scan_lock.release()
                t.join(timeout=5)
        finally:
            try:
                # Best-effort lock release in case of test failure path.
                scan_lock.release()
            except RuntimeError:
                pass
            app._active_recording['starting'] = prev_starting

        self.assertIsNone(result_holder['err'])
        self.assertIsNotNone(result_holder['r'])
        self.assertEqual(result_holder['r'].status_code, 409)
        # The inner-recheck caught the flip — ffmpeg was NOT spawned.
        self.assertEqual(ffmpeg_called['n'], 0)


class TestReviewWriteLockExposed(_ReviewRouteBase):
    """BE-3: per-(scan_id, scan_name) review-write lock helper exists."""

    def test_lock_helper_exists(self):
        self.assertTrue(hasattr(app, '_review_lock_for'))
        self.assertTrue(callable(app._review_lock_for))

    def test_lock_is_per_key(self):
        a = app._review_lock_for(42, 'living_room')
        b = app._review_lock_for(42, 'living_room')
        c = app._review_lock_for(42, 'kitchen')
        d = app._review_lock_for(43, 'living_room')
        self.assertIs(a, b)            # same key -> same lock
        self.assertIsNot(a, c)         # different scan name
        self.assertIsNot(a, d)         # different scan id

    def test_concurrent_patches_dont_clobber(self):
        # Two PATCH requests against the same scan should both land —
        # the second's mutation must not erase the first's. Without the
        # lock, a read-modify-write race could lose one.
        self._login()
        self._seed_run()

        def patch_one(field_value):
            local = app.app.test_client()
            return local.patch(
                '/api/project/42/scan/living_room/review',
                json={'bbox_id': 'bbox_0', 'class_override': field_value},
            )

        results = [None, None]
        errors = [None, None]

        def worker(i, val):
            try:
                results[i] = patch_one(val)
            except Exception as e:  # pragma: no cover
                errors[i] = e

        t1 = threading.Thread(target=worker, args=(0, 'armchair'))
        t2 = threading.Thread(target=worker, args=(1, 'recliner'))
        t1.start(); t2.start()
        t1.join(timeout=5); t2.join(timeout=5)

        self.assertIsNone(errors[0])
        self.assertIsNone(errors[1])
        self.assertEqual(results[0].status_code, 200)
        self.assertEqual(results[1].status_code, 200)

        # The final review.json reflects ONE of the two values cleanly —
        # not a mix; not a missing key.
        rv = review.read_review(
            projects.processed_dir_for_run(42, 'living_room', '20260425_142103')
        )
        self.assertIn('bbox_0', rv['bboxes'])
        self.assertIn(rv['bboxes']['bbox_0'].get('class_override'),
                      ('armchair', 'recliner'))


class TestCarryOverHelperWiring(_ReviewRouteBase):
    """BE-4: `_scoped_apply_carry_over` migrates the prior review onto a new run."""

    def test_helper_exposed(self):
        self.assertTrue(hasattr(app, '_scoped_apply_carry_over'))
        self.assertTrue(callable(app._scoped_apply_carry_over))

    def test_apply_carry_over_migrates_class_override(self):
        # Set up an old run with a class_override and a new run with
        # geometrically-overlapping furniture. After the helper runs, the
        # new run's review.json should carry the override.
        self._login()
        # Old run.
        old_dir = self._seed_run(run_id='20260420_120000')
        # Patch the old review with a class override.
        with open(os.path.join(old_dir, 'result.json'), 'r') as _f:
            old_result = json.load(_f)
        old_rv = review.read_review(old_dir) \
            or review.initial_review(42, 'living_room', old_result)
        old_rv = review.set_class_override(old_rv, 'bbox_0', 'armchair')
        review.write_review(old_dir, old_rv)

        # New run with the same furniture geometry.
        new_dir = self._seed_run(run_id='20260425_180000')

        app._scoped_apply_carry_over(42, 'living_room', new_dir, old_dir)

        new_rv = review.read_review(new_dir)
        self.assertIsNotNone(new_rv)
        self.assertEqual(
            new_rv['bboxes']['bbox_0'].get('class_override'), 'armchair',
        )

    def test_apply_carry_over_no_op_without_prior_review(self):
        # No old review.json on disk -> helper is a clean no-op.
        self._login()
        new_dir = self._seed_run()
        # Sanity: no review on disk before.
        self.assertIsNone(review.read_review(new_dir))
        app._scoped_apply_carry_over(42, 'living_room', new_dir, '/no/such/dir')
        self.assertIsNone(review.read_review(new_dir))


if __name__ == '__main__':
    unittest.main()
