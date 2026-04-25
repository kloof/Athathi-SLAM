"""Tests for the Step 6 submit-pipeline helpers (`submit.py`).

Pure-helper coverage. No real network. `athathi_proxy.upload_bundle` and
`athathi_proxy.complete_scan` are mocked everywhere.

Coverage (per the §16 step 6 brief):
  1.  `gating_message` returns the right priority message for each
      condition (already-submitted / still-processing / error /
      not-reviewed).
  2.  `gather_runs_for_submit` returns one entry per scan, keyed on the
      active run.
  3.  `render_run_outputs` writes both JSONs and returns the
      image_files list.
  4.  `build_image_files_for_upload` prefers `<idx>_recapture.jpg`.
  5.  `submit_run_outputs` returns `uploaded: False` when
      `upload_endpoint` is None.
  6.  `submit_run_outputs` calls `athathi_proxy.upload_bundle` with the
      right shape (mock the proxy).
  7.  `submit_run_outputs` raises `SubmitNetworkError` on AthathiError(0).
  8.  `stamp_submit_outcome` writes manifest.submitted_at + per-run
      review.json.submitted_at; idempotent on repeat.
  9.  `run_post_submit_hook` returns ok on a `true`/`echo hi` command;
      ok=False on a non-existent command; respects timeout.
  10. `submit_pending_retry` walks projects, retries only those marked
      pending.
"""

import json
import os
import shutil
import sys
import tempfile
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


# ---------------------------------------------------------------------------
# Base — per-test tempdirs for ATHATHI_DIR and PROJECTS_ROOT.
# ---------------------------------------------------------------------------

class _SubmitTestBase(unittest.TestCase):
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
        # Seed config so api_url + upload_endpoint are predictable.
        auth.save_config({
            'api_url': 'http://upstream.test',
            'upload_endpoint': 'http://upload.test/api/upload',
            'last_user': '',
            'post_submit_hook': None,
            'image_transport': 'multipart',
            'visual_search_cache_ttl_s': 86400,
        })

        self.tmp_projects = tempfile.mkdtemp()
        self._orig_projects_root = projects.PROJECTS_ROOT
        projects.PROJECTS_ROOT = self.tmp_projects

    def tearDown(self):
        for k, v in self._orig_auth.items():
            setattr(auth, k, v)
        projects.PROJECTS_ROOT = self._orig_projects_root
        shutil.rmtree(self.tmp_auth, ignore_errors=True)
        shutil.rmtree(self.tmp_projects, ignore_errors=True)

    # Shared seed helpers --------------------------------------------------

    def _seed_project(self, scan_id=42, athathi_meta=None):
        if athathi_meta is None:
            athathi_meta = {'customer_name': 'Smith'}
        projects.ensure_project(scan_id, athathi_meta=athathi_meta)
        return scan_id

    def _seed_scan_with_run(self, scan_id, scan_name='living_room',
                            run_id='20260425_142103',
                            envelope=None, with_review=True,
                            reviewed_at='2026-04-25T13:00:00Z',
                            with_images=True):
        """Create a scan + active run + result.json + (optional) review.json.

        Returns the run directory (absolute path).
        """
        if not os.path.isdir(projects.scan_dir(scan_id, scan_name)):
            projects.create_scan(scan_id, scan_name)
        run_dir = projects.processed_dir_for_run(scan_id, scan_name, run_id)
        os.makedirs(run_dir, exist_ok=True)
        if envelope is None:
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
                    {'id': 'bbox_1', 'class': 'chair',
                     'center': [2.0, 0.0, 0.5], 'size': [0.5, 0.5, 1.0],
                     'yaw': 0.0},
                ],
                'best_images': [
                    {'bbox_id': 'bbox_0', 'class': 'sofa',
                     'pixel_aabb': [0, 0, 100, 100]},
                    {'bbox_id': 'bbox_1', 'class': 'chair',
                     'pixel_aabb': [0, 0, 100, 100]},
                ],
            }
        with open(os.path.join(run_dir, 'result.json'), 'w') as f:
            json.dump(envelope, f)
        # Best-views dir + fake JPEGs.
        if with_images:
            bv = os.path.join(run_dir, 'best_views')
            os.makedirs(bv, exist_ok=True)
            for idx in range(len(envelope.get('best_images') or [])):
                with open(os.path.join(bv, f'{idx}.jpg'), 'wb') as f:
                    f.write(b'\xff\xd8\xff\xe0fake')
        # Drop a meta.json the runs route can pick up.
        with open(os.path.join(run_dir, 'meta.json'), 'w') as f:
            json.dump({'job_id': 'j_test', 'status': 'done'}, f)
        projects.set_active_run(scan_id, scan_name, run_id)
        # Optional review.json with `reviewed_at` set so submit gating
        # passes in default tests.
        if with_review:
            rv = review.initial_review(scan_id, scan_name, envelope)
            for bid in rv['bboxes']:
                rv['bboxes'][bid]['status'] = review.STATUS_KEPT
            if reviewed_at:
                rv['reviewed_at'] = reviewed_at
            review.write_review(run_dir, rv)
        return run_dir


# ---------------------------------------------------------------------------
# 1. gating_message priorities
# ---------------------------------------------------------------------------

class TestGatingMessage(_SubmitTestBase):
    def test_already_submitted_wins(self):
        sid = self._seed_project()
        self._seed_scan_with_run(sid)
        m = projects.read_manifest(sid)
        m['submitted_at'] = '2026-04-25T12:00:00Z'
        projects.write_manifest(sid, m)
        msg = submit.gating_message(sid)
        self.assertIsNotNone(msg)
        self.assertIn('Already submitted', msg)
        self.assertIn('2026-04-25', msg)

    def test_still_processing_blocks(self):
        sid = self._seed_project()
        self._seed_scan_with_run(sid, scan_name='living_room')

        # Patch app._active_processing to mark this scan in-flight.
        import app
        with mock.patch.dict(app._active_processing, {
            'sess1': {'scan_id': sid, 'scan_name': 'living_room',
                      'stage': 'stage_5_infer', 'start_time': 0.0},
        }, clear=False):
            msg = submit.gating_message(sid)
        self.assertIsNotNone(msg)
        self.assertIn('living_room', msg)
        self.assertIn('still processing', msg)

    def test_error_status_blocks(self):
        sid = self._seed_project()
        run_dir = self._seed_scan_with_run(sid, scan_name='bedroom')
        # Rewrite result.json with status=error.
        with open(os.path.join(run_dir, 'result.json')) as f:
            env = json.load(f)
        env['status'] = 'error'
        with open(os.path.join(run_dir, 'result.json'), 'w') as f:
            json.dump(env, f)
        msg = submit.gating_message(sid)
        self.assertIsNotNone(msg)
        self.assertIn('bedroom', msg)
        self.assertIn('failed', msg)

    def test_not_reviewed_blocks(self):
        sid = self._seed_project()
        self._seed_scan_with_run(sid, scan_name='kitchen', with_review=False)
        msg = submit.gating_message(sid)
        self.assertIsNotNone(msg)
        self.assertIn('kitchen', msg)
        self.assertIn('not reviewed', msg)

    def test_happy_path_returns_none(self):
        sid = self._seed_project()
        self._seed_scan_with_run(sid)
        self.assertIsNone(submit.gating_message(sid))

    def test_priority_order_already_beats_processing(self):
        # If both submitted-at AND processing are true, submitted wins.
        sid = self._seed_project()
        self._seed_scan_with_run(sid, scan_name='living_room')
        m = projects.read_manifest(sid)
        m['submitted_at'] = '2026-04-25T12:00:00Z'
        projects.write_manifest(sid, m)
        import app
        with mock.patch.dict(app._active_processing, {
            'sess1': {'scan_id': sid, 'scan_name': 'living_room',
                      'stage': 'stage_5_infer', 'start_time': 0.0},
        }, clear=False):
            msg = submit.gating_message(sid)
        self.assertIn('Already submitted', msg)


# ---------------------------------------------------------------------------
# 2. gather_runs_for_submit
# ---------------------------------------------------------------------------

class TestGatherRuns(_SubmitTestBase):
    def test_returns_active_run_for_each_scan(self):
        sid = self._seed_project()
        self._seed_scan_with_run(sid, scan_name='living_room',
                                 run_id='20260425_142103')
        self._seed_scan_with_run(sid, scan_name='bedroom',
                                 run_id='20260425_152030')
        runs = submit.gather_runs_for_submit(sid)
        self.assertEqual(len(runs), 2)
        names = sorted(r['scan_name'] for r in runs)
        self.assertEqual(names, ['bedroom', 'living_room'])
        for r in runs:
            self.assertIsNotNone(r['run_id'])
            self.assertTrue(os.path.isdir(r['run_dir']))
            self.assertIsNotNone(r['review'])

    def test_scan_with_no_active_run_yields_nones(self):
        sid = self._seed_project()
        projects.create_scan(sid, 'empty_room')
        runs = submit.gather_runs_for_submit(sid)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]['scan_name'], 'empty_room')
        self.assertIsNone(runs[0]['run_id'])
        self.assertIsNone(runs[0]['run_dir'])


# ---------------------------------------------------------------------------
# 3. render_run_outputs
# ---------------------------------------------------------------------------

class TestRenderRunOutputs(_SubmitTestBase):
    def test_writes_both_jsons_and_returns_image_files(self):
        sid = self._seed_project()
        run_dir = self._seed_scan_with_run(sid)
        out = submit.render_run_outputs(run_dir, 'tech1')
        self.assertTrue(os.path.isfile(out['reviewed_path']))
        self.assertTrue(os.path.isfile(out['upload_path']))
        # Both filenames should match the convention.
        self.assertTrue(out['reviewed_path'].endswith('result_reviewed.json'))
        self.assertTrue(out['upload_path'].endswith('result_for_upload.json'))
        # image_files: both bboxes survive (status=kept).
        self.assertEqual(len(out['image_files']), 2)
        # Field names: image_<bbox_id>.
        fields = sorted(p[0] for p in out['image_files'])
        self.assertEqual(fields, ['image_bbox_0', 'image_bbox_1'])
        # Paths are absolute and exist.
        for _, p in out['image_files']:
            self.assertTrue(os.path.isabs(p))
            self.assertTrue(os.path.isfile(p))

    def test_stamps_submitted_by(self):
        sid = self._seed_project()
        run_dir = self._seed_scan_with_run(sid)
        submit.render_run_outputs(run_dir, 'tech_alice')
        with open(os.path.join(run_dir, 'result_reviewed.json')) as f:
            reviewed = json.load(f)
        self.assertEqual(reviewed.get('submitted_by'), 'tech_alice')
        with open(os.path.join(run_dir, 'result_for_upload.json')) as f:
            upload = json.load(f)
        self.assertEqual(upload.get('submitted_by'), 'tech_alice')

    def test_missing_result_json_raises(self):
        # No result.json in run_dir.
        sid = self._seed_project()
        rd = projects.processed_dir_for_run(sid, 'living_room', 'r1')
        os.makedirs(rd, exist_ok=True)
        with self.assertRaises(FileNotFoundError):
            submit.render_run_outputs(rd, 'tech')


# ---------------------------------------------------------------------------
# 4. build_image_files_for_upload — recapture preferred
# ---------------------------------------------------------------------------

class TestBuildImageFiles(_SubmitTestBase):
    def test_uses_local_path_recapture(self):
        sid = self._seed_project()
        run_dir = self._seed_scan_with_run(sid)
        # Drop a recapture next to the originals.
        bv = os.path.join(run_dir, 'best_views')
        recap0 = os.path.join(bv, '0_recapture.jpg')
        with open(recap0, 'wb') as f:
            f.write(b'\xff\xd8recapture')

        # Realistic envelope shape — `render_reviewed` always stamps
        # `local_path`. When `image_override` was set on the review, the
        # path points at the recapture file.
        envelope = {
            'best_images': [
                {'bbox_id': 'bbox_0',
                 'local_path': 'best_views/0_recapture.jpg'},
                {'bbox_id': 'bbox_1', 'local_path': 'best_views/1.jpg'},
            ],
        }
        out = submit.build_image_files_for_upload(envelope, run_dir)
        as_dict = dict(out)
        self.assertEqual(as_dict['image_bbox_0'], recap0)
        self.assertTrue(as_dict['image_bbox_1'].endswith('1.jpg'))

    def test_pairs_by_local_path_after_delete(self):
        # After a delete, the filtered best_images is missing one entry,
        # but each surviving entry's `local_path` still references the
        # original Modal index. Pairing must follow `local_path`, not
        # the filtered position.
        sid = self._seed_project()
        run_dir = self._seed_scan_with_run(sid)
        bv = os.path.join(run_dir, 'best_views')
        # `_seed_scan_with_run` lays down `0.jpg` and `1.jpg`. Add a `2.jpg`.
        with open(os.path.join(bv, '2.jpg'), 'wb') as f:
            f.write(b'\xff\xd8two')

        envelope = {
            'best_images': [
                {'bbox_id': 'bbox_0', 'local_path': 'best_views/0.jpg'},
                # bbox_1 deleted — gap at position 1
                {'bbox_id': 'bbox_2', 'local_path': 'best_views/2.jpg'},
            ],
        }
        out = submit.build_image_files_for_upload(envelope, run_dir)
        as_dict = dict(out)
        self.assertTrue(as_dict['image_bbox_0'].endswith('0.jpg'))
        self.assertTrue(as_dict['image_bbox_2'].endswith('2.jpg'))

    def test_skips_missing_files_and_warns(self):
        sid = self._seed_project()
        run_dir = self._seed_scan_with_run(sid, with_images=False)
        envelope = {
            'best_images': [
                {'bbox_id': 'bbox_0', 'local_path': 'best_views/0.jpg'},
            ],
        }
        out = submit.build_image_files_for_upload(envelope, run_dir)
        self.assertEqual(out, [])

    def test_skips_entries_without_local_path(self):
        sid = self._seed_project()
        run_dir = self._seed_scan_with_run(sid)
        envelope = {
            'best_images': [
                {'bbox_id': 'bbox_0'},  # no local_path
            ],
        }
        out = submit.build_image_files_for_upload(envelope, run_dir)
        self.assertEqual(out, [])


# ---------------------------------------------------------------------------
# 5–7. submit_run_outputs
# ---------------------------------------------------------------------------

class TestSubmitRunOutputs(_SubmitTestBase):
    def _write_envelope(self, run_dir):
        path = os.path.join(run_dir, 'result_for_upload.json')
        with open(path, 'wb') as f:
            f.write(b'{"hello":"world"}')
        return path

    def test_no_upload_endpoint_returns_uploaded_false(self):
        sid = self._seed_project()
        run_dir = self._seed_scan_with_run(sid)
        envelope_path = self._write_envelope(run_dir)
        out = submit.submit_run_outputs(
            'tok', None, envelope_path=envelope_path, image_files=[],
        )
        self.assertFalse(out.get('uploaded'))
        self.assertIn('reason', out)

    def test_calls_upload_bundle_with_right_shape(self):
        sid = self._seed_project()
        run_dir = self._seed_scan_with_run(sid)
        envelope_path = self._write_envelope(run_dir)
        image_files = [('image_bbox_0',
                        os.path.join(run_dir, 'best_views', '0.jpg'))]
        with mock.patch.object(athathi_proxy, 'upload_bundle',
                               return_value={'ok': True, 'received': 1}) as m:
            out = submit.submit_run_outputs(
                'tok-xyz', 'http://upload.test/api/upload',
                envelope_path=envelope_path, image_files=image_files,
            )
        self.assertTrue(out.get('uploaded'))
        self.assertEqual(out['response'], {'ok': True, 'received': 1})
        m.assert_called_once()
        # Inspect call args.
        args, kwargs = m.call_args
        # args = (token, endpoint, envelope_bytes, image_files)
        self.assertEqual(args[0], 'tok-xyz')
        self.assertEqual(args[1], 'http://upload.test/api/upload')
        self.assertEqual(args[2], b'{"hello":"world"}')
        self.assertEqual(args[3], image_files)

    def test_network_error_raises_submit_network_error(self):
        sid = self._seed_project()
        run_dir = self._seed_scan_with_run(sid)
        envelope_path = self._write_envelope(run_dir)
        with mock.patch.object(athathi_proxy, 'upload_bundle',
                               side_effect=athathi_proxy.AthathiError(
                                   0, 'curl: (7) refused', 'Network error')):
            with self.assertRaises(submit.SubmitNetworkError):
                submit.submit_run_outputs(
                    'tok', 'http://upload.test/api/upload',
                    envelope_path=envelope_path, image_files=[],
                )

    def test_5xx_propagates_athathi_error(self):
        sid = self._seed_project()
        run_dir = self._seed_scan_with_run(sid)
        envelope_path = self._write_envelope(run_dir)
        with mock.patch.object(athathi_proxy, 'upload_bundle',
                               side_effect=athathi_proxy.AthathiError(
                                   503, 'svc down', 'Upstream 503')):
            with self.assertRaises(athathi_proxy.AthathiError) as ctx:
                submit.submit_run_outputs(
                    'tok', 'http://upload.test/api/upload',
                    envelope_path=envelope_path, image_files=[],
                )
        self.assertEqual(ctx.exception.status_code, 503)


# ---------------------------------------------------------------------------
# 8. stamp_submit_outcome
# ---------------------------------------------------------------------------

class TestStampSubmitOutcome(_SubmitTestBase):
    def test_success_stamps_manifest_and_review(self):
        sid = self._seed_project()
        run_dir = self._seed_scan_with_run(sid)
        runs = submit.gather_runs_for_submit(sid)
        submit.stamp_submit_outcome(
            sid, runs=runs, response={'message': 'completed'},
            error=None, queued=False,
        )
        m = projects.read_manifest(sid)
        self.assertIsNotNone(m.get('submitted_at'))
        self.assertEqual(m.get('submit_response'),
                         {'message': 'completed'})
        rv = review.read_review(run_dir)
        self.assertIsNotNone(rv.get('submitted_at'))

    def test_success_does_not_backfill_completed_at(self):
        # `completed_at` is owned by the upstream-history mirror (see
        # _projects_render_merged in app.py). Local submit success must
        # leave it untouched — None stays None.
        sid = self._seed_project()
        self._seed_scan_with_run(sid)
        runs = submit.gather_runs_for_submit(sid)
        # Pre-condition: manifest has no completed_at.
        m_before = projects.read_manifest(sid)
        self.assertIsNone(m_before.get('completed_at'))
        submit.stamp_submit_outcome(
            sid, runs=runs, response={'message': 'completed'},
            error=None, queued=False,
        )
        m_after = projects.read_manifest(sid)
        # Submit stamped submitted_at...
        self.assertIsNotNone(m_after.get('submitted_at'))
        # ...but DID NOT backfill completed_at.
        self.assertIsNone(m_after.get('completed_at'))

    def test_success_preserves_prior_completed_at(self):
        # If upstream-history sync had already written completed_at, we
        # must not clobber or refresh it.
        sid = self._seed_project()
        self._seed_scan_with_run(sid)
        runs = submit.gather_runs_for_submit(sid)
        prior = '2026-04-20T09:08:07Z'
        m = projects.read_manifest(sid)
        m['completed_at'] = prior
        projects.write_manifest(sid, m)
        submit.stamp_submit_outcome(
            sid, runs=runs, response={'message': 'completed'},
            error=None, queued=False,
        )
        m_after = projects.read_manifest(sid)
        self.assertEqual(m_after.get('completed_at'), prior)

    def test_idempotent_on_repeat(self):
        sid = self._seed_project()
        run_dir = self._seed_scan_with_run(sid)
        runs = submit.gather_runs_for_submit(sid)
        submit.stamp_submit_outcome(
            sid, runs=runs, response={'a': 1}, error=None, queued=False,
        )
        m1 = projects.read_manifest(sid)
        first_at = m1.get('submitted_at')
        rv1 = review.read_review(run_dir)
        first_rv_at = rv1.get('submitted_at')
        # Second call with a different response — submitted_at should NOT change.
        submit.stamp_submit_outcome(
            sid, runs=runs, response={'b': 2}, error=None, queued=False,
        )
        m2 = projects.read_manifest(sid)
        self.assertEqual(m2.get('submitted_at'), first_at)
        rv2 = review.read_review(run_dir)
        self.assertEqual(rv2.get('submitted_at'), first_rv_at)

    def test_queued_sets_submit_pending(self):
        sid = self._seed_project()
        self._seed_scan_with_run(sid)
        runs = submit.gather_runs_for_submit(sid)
        submit.stamp_submit_outcome(
            sid, runs=runs, response=None, error='no network', queued=True,
        )
        m = projects.read_manifest(sid)
        self.assertIs(m.get('submit_pending'), True)
        self.assertEqual(m.get('submit_pending_error'), 'no network')
        # submitted_at NOT stamped on queue.
        self.assertFalse(m.get('submitted_at'))


# ---------------------------------------------------------------------------
# 9. run_post_submit_hook
# ---------------------------------------------------------------------------

class TestRunPostSubmitHook(_SubmitTestBase):
    def test_no_hook_configured_is_ok_with_ran_false(self):
        out = submit.run_post_submit_hook(None, '/tmp', 42)
        self.assertTrue(out['ok'])
        self.assertFalse(out['ran'])

    def test_existing_command_returns_ok(self):
        # `true` exits 0 — universally available on Linux.
        out = submit.run_post_submit_hook('/bin/true', '/tmp', 42, timeout=5)
        self.assertTrue(out['ok'])
        self.assertTrue(out['ran'])
        self.assertEqual(out['returncode'], 0)

    def test_echo_captures_stdout_tail(self):
        out = submit.run_post_submit_hook('/bin/echo', '/tmp', 42, timeout=5)
        self.assertTrue(out['ok'])
        # echo prints its args: "/tmp 42\n".
        self.assertIn('42', out['stdout_tail'])

    def test_nonexistent_command_returns_not_ok(self):
        out = submit.run_post_submit_hook(
            '/no/such/binary/that/should/never/exist',
            '/tmp', 42, timeout=5,
        )
        self.assertFalse(out['ok'])
        self.assertFalse(out['ran'])
        self.assertIn('error', out)

    def test_timeout_returns_not_ok(self):
        out = submit.run_post_submit_hook(
            '/bin/sleep', '5', 42, timeout=1,
        )
        self.assertFalse(out['ok'])
        self.assertTrue(out['ran'])
        self.assertIn('error', out)
        self.assertIn('timed out', out['error'])


# ---------------------------------------------------------------------------
# 10. submit_pending_retry
# ---------------------------------------------------------------------------

class TestSubmitPendingRetry(_SubmitTestBase):
    def test_walks_only_pending_projects(self):
        # Project A: pending. Project B: not pending.
        sid_a = self._seed_project(scan_id=42)
        self._seed_scan_with_run(sid_a)
        m = projects.read_manifest(sid_a)
        m['submit_pending'] = True
        projects.write_manifest(sid_a, m)

        sid_b = self._seed_project(scan_id=43)
        self._seed_scan_with_run(sid_b)
        # No submit_pending on B.

        with mock.patch.object(athathi_proxy, 'complete_scan',
                               return_value={'message': 'completed'}) as m_call:
            results = submit.submit_pending_retry(lambda: 'tok')

        # Only A should have been retried.
        self.assertEqual(m_call.call_count, 1)
        m_call.assert_called_with('tok', sid_a)

        # Result shape.
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['scan_id'], sid_a)
        self.assertEqual(results[0]['status'], 'submitted')

        # Pending flag cleared on A; submitted_at stamped.
        m_after = projects.read_manifest(sid_a)
        self.assertFalse(m_after.get('submit_pending'))
        self.assertTrue(m_after.get('submitted_at'))

    def test_already_submitted_clears_flag(self):
        sid = self._seed_project()
        self._seed_scan_with_run(sid)
        m = projects.read_manifest(sid)
        m['submit_pending'] = True
        m['submitted_at'] = '2026-04-25T12:00:00Z'
        projects.write_manifest(sid, m)

        with mock.patch.object(athathi_proxy, 'complete_scan') as m_call:
            results = submit.submit_pending_retry(lambda: 'tok')

        m_call.assert_not_called()
        self.assertEqual(results[0]['status'], 'already_submitted')
        m_after = projects.read_manifest(sid)
        self.assertFalse(m_after.get('submit_pending'))

    def test_failed_keeps_pending_with_error(self):
        sid = self._seed_project()
        self._seed_scan_with_run(sid)
        m = projects.read_manifest(sid)
        m['submit_pending'] = True
        projects.write_manifest(sid, m)

        with mock.patch.object(athathi_proxy, 'complete_scan',
                               side_effect=athathi_proxy.AthathiError(
                                   0, 'still no net', 'Network error')):
            results = submit.submit_pending_retry(lambda: 'tok')

        self.assertEqual(results[0]['status'], 'failed')
        m_after = projects.read_manifest(sid)
        self.assertIs(m_after.get('submit_pending'), True)
        self.assertIn('Network error', m_after.get('submit_pending_error', ''))

    def test_no_token_skips(self):
        sid = self._seed_project()
        self._seed_scan_with_run(sid)
        m = projects.read_manifest(sid)
        m['submit_pending'] = True
        projects.write_manifest(sid, m)

        with mock.patch.object(athathi_proxy, 'complete_scan') as m_call:
            results = submit.submit_pending_retry(lambda: None)
        m_call.assert_not_called()
        self.assertEqual(results[0]['status'], 'skipped_no_token')


# ---------------------------------------------------------------------------
# BACKEND-B2: stage-aware retry
# ---------------------------------------------------------------------------

class TestSubmitPendingRetryStageAware(_SubmitTestBase):
    """`submit_pending_stage` drives retry behaviour:
       - 'upload'   → re-render + re-upload + complete
       - 'complete' → just complete
       - missing    → backwards-compat probe (complete first; on no-upload
                       hint fall back to upload+complete).
    """

    def test_stage_complete_calls_complete_only(self):
        sid = self._seed_project()
        self._seed_scan_with_run(sid)
        m = projects.read_manifest(sid)
        m['submit_pending'] = True
        m['submit_pending_stage'] = 'complete'
        projects.write_manifest(sid, m)

        with mock.patch.object(athathi_proxy, 'upload_bundle') as m_up, \
             mock.patch.object(athathi_proxy, 'complete_scan',
                               return_value={'message': 'ok'}) as m_done:
            results = submit.submit_pending_retry(
                lambda: 'tok',
                upload_endpoint_provider=lambda: 'http://upload.test/x',
                technician_provider=lambda: 'tech',
            )
        m_up.assert_not_called()
        m_done.assert_called_once_with('tok', sid)
        self.assertEqual(results[0]['status'], 'submitted')
        m_after = projects.read_manifest(sid)
        self.assertFalse(m_after.get('submit_pending'))
        self.assertNotIn('submit_pending_stage', m_after)
        self.assertTrue(m_after.get('submitted_at'))

    def test_stage_upload_skips_already_uploaded_scans(self):
        # Multi-scan project: bedroom already uploaded successfully on the
        # first attempt; living_room failed mid-loop. The retry sweep must
        # only re-upload living_room — re-uploading bedroom would produce
        # duplicates back-office (the v1 envelope has no scan_id/run_id
        # dedupe key on the wire).
        sid = self._seed_project()
        self._seed_scan_with_run(sid, scan_name='bedroom',
                                 run_id='r_bed')
        self._seed_scan_with_run(sid, scan_name='living_room',
                                 run_id='r_liv')
        m = projects.read_manifest(sid)
        m['submit_pending'] = True
        m['submit_pending_stage'] = 'upload'
        m['submit_pending_uploads'] = ['bedroom']
        projects.write_manifest(sid, m)

        with mock.patch.object(athathi_proxy, 'upload_bundle',
                               return_value={'received': 1}) as m_up, \
             mock.patch.object(athathi_proxy, 'complete_scan',
                               return_value={'message': 'ok'}):
            results = submit.submit_pending_retry(
                lambda: 'tok',
                upload_endpoint_provider=lambda: 'http://upload.test/x',
                technician_provider=lambda: 'tech',
            )
        # Only ONE upload call (living_room only) — bedroom skipped.
        self.assertEqual(m_up.call_count, 1)
        self.assertEqual(results[0]['status'], 'submitted')
        m_after = projects.read_manifest(sid)
        # On success, the tracking field is cleared.
        self.assertNotIn('submit_pending_uploads', m_after)

    def test_stage_upload_re_uploads_then_completes(self):
        sid = self._seed_project()
        self._seed_scan_with_run(sid)
        m = projects.read_manifest(sid)
        m['submit_pending'] = True
        m['submit_pending_stage'] = 'upload'
        projects.write_manifest(sid, m)

        with mock.patch.object(athathi_proxy, 'upload_bundle',
                               return_value={'received': 1}) as m_up, \
             mock.patch.object(athathi_proxy, 'complete_scan',
                               return_value={'message': 'ok'}) as m_done:
            results = submit.submit_pending_retry(
                lambda: 'tok',
                upload_endpoint_provider=lambda: 'http://upload.test/x',
                technician_provider=lambda: 'tech',
            )
        m_up.assert_called_once()
        m_done.assert_called_once_with('tok', sid)
        self.assertEqual(results[0]['status'], 'submitted')
        m_after = projects.read_manifest(sid)
        self.assertFalse(m_after.get('submit_pending'))
        self.assertNotIn('submit_pending_stage', m_after)

    def test_stage_upload_network_failure_keeps_pending(self):
        sid = self._seed_project()
        self._seed_scan_with_run(sid)
        m = projects.read_manifest(sid)
        m['submit_pending'] = True
        m['submit_pending_stage'] = 'upload'
        projects.write_manifest(sid, m)

        with mock.patch.object(athathi_proxy, 'upload_bundle',
                               side_effect=athathi_proxy.AthathiError(
                                   0, 'no net', 'Network error')) as m_up, \
             mock.patch.object(athathi_proxy, 'complete_scan') as m_done:
            results = submit.submit_pending_retry(
                lambda: 'tok',
                upload_endpoint_provider=lambda: 'http://upload.test/x',
                technician_provider=lambda: 'tech',
            )
        m_up.assert_called_once()
        m_done.assert_not_called()
        self.assertEqual(results[0]['status'], 'failed')
        self.assertEqual(results[0].get('stage'), 'upload')
        m_after = projects.read_manifest(sid)
        self.assertIs(m_after.get('submit_pending'), True)
        # Stage marker preserved so the next sweep retries upload again.
        self.assertEqual(m_after.get('submit_pending_stage'), 'upload')

    def test_missing_stage_falls_back_on_no_upload_hint(self):
        # No stage on disk → first try complete; if upstream signals "no
        # upload received" we re-upload + complete.
        sid = self._seed_project()
        self._seed_scan_with_run(sid)
        m = projects.read_manifest(sid)
        m['submit_pending'] = True
        # NO submit_pending_stage.
        projects.write_manifest(sid, m)

        # complete fails 404 first, then succeeds after re-upload.
        complete_calls = {'n': 0}

        def complete_side_effect(_tok, _sid):
            complete_calls['n'] += 1
            if complete_calls['n'] == 1:
                raise athathi_proxy.AthathiError(
                    404, 'no upload received for scan', 'Not found')
            return {'message': 'completed'}

        with mock.patch.object(athathi_proxy, 'upload_bundle',
                               return_value={'received': 1}) as m_up, \
             mock.patch.object(athathi_proxy, 'complete_scan',
                               side_effect=complete_side_effect) as m_done:
            results = submit.submit_pending_retry(
                lambda: 'tok',
                upload_endpoint_provider=lambda: 'http://upload.test/x',
                technician_provider=lambda: 'tech',
            )
        # complete called twice, upload between them.
        self.assertEqual(m_done.call_count, 2)
        m_up.assert_called_once()
        self.assertEqual(results[0]['status'], 'submitted')
        m_after = projects.read_manifest(sid)
        self.assertFalse(m_after.get('submit_pending'))
        self.assertNotIn('submit_pending_stage', m_after)

    def test_missing_stage_other_error_does_not_re_upload(self):
        # If complete fails with a non-no-upload error, we DON'T re-upload.
        sid = self._seed_project()
        self._seed_scan_with_run(sid)
        m = projects.read_manifest(sid)
        m['submit_pending'] = True
        projects.write_manifest(sid, m)

        with mock.patch.object(athathi_proxy, 'upload_bundle') as m_up, \
             mock.patch.object(athathi_proxy, 'complete_scan',
                               side_effect=athathi_proxy.AthathiError(
                                   503, 'svc down', 'Upstream 503')):
            results = submit.submit_pending_retry(
                lambda: 'tok',
                upload_endpoint_provider=lambda: 'http://upload.test/x',
                technician_provider=lambda: 'tech',
            )
        m_up.assert_not_called()
        self.assertEqual(results[0]['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
