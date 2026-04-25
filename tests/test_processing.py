"""Tests for the Modal-based SLAM processing pipeline in app.py.

NO real network calls. Every test mocks `subprocess.run` (or higher-level
helpers) so we never hit Modal during development.
"""

import json
import os
import sys
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

# Make the repo importable regardless of where pytest is invoked from.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import app  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers — fake subprocess.CompletedProcess values
# ---------------------------------------------------------------------------

def _cp(stdout='', stderr='', rc=0):
    res = mock.Mock()
    res.stdout = stdout
    res.stderr = stderr
    res.returncode = rc
    return res


# ---------------------------------------------------------------------------
# .env loader
# ---------------------------------------------------------------------------

class TestEnvLoader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, '.env')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, content):
        with open(self.path, 'w') as f:
            f.write(content)

    def test_basic_unquoted(self):
        self._write('FOO=bar\nBAZ=qux\n')
        env = app._load_env_file(self.path)
        self.assertEqual(env, {'FOO': 'bar', 'BAZ': 'qux'})

    def test_double_quotes_stripped(self):
        self._write('FOO="hello world"\n')
        self.assertEqual(app._load_env_file(self.path), {'FOO': 'hello world'})

    def test_single_quotes_stripped(self):
        self._write("FOO='hello world'\n")
        self.assertEqual(app._load_env_file(self.path), {'FOO': 'hello world'})

    def test_mismatched_quotes_kept(self):
        # Only matched pairs are stripped.
        self._write('FOO="oops\'\n')
        self.assertEqual(app._load_env_file(self.path), {'FOO': '"oops\''})

    def test_comments_ignored(self):
        self._write('# this is a comment\nFOO=bar\n# trailing\n')
        self.assertEqual(app._load_env_file(self.path), {'FOO': 'bar'})

    def test_blank_lines_ignored(self):
        self._write('\nFOO=bar\n\n\nBAZ=qux\n')
        self.assertEqual(app._load_env_file(self.path),
                         {'FOO': 'bar', 'BAZ': 'qux'})

    def test_crlf_tolerated(self):
        self._write('FOO=bar\r\nBAZ=qux\r\n')
        self.assertEqual(app._load_env_file(self.path),
                         {'FOO': 'bar', 'BAZ': 'qux'})

    def test_missing_file_returns_empty(self):
        self.assertEqual(app._load_env_file('/no/such/file'), {})

    def test_missing_key_returns_empty(self):
        self._write('FOO=bar\n')
        env = app._load_env_file(self.path)
        self.assertEqual(env.get('NOPE', ''), '')

    def test_no_equals_skipped(self):
        self._write('this is junk\nFOO=bar\n')
        self.assertEqual(app._load_env_file(self.path), {'FOO': 'bar'})

    def test_value_with_equals_kept(self):
        self._write('FOO=key=value=more\n')
        self.assertEqual(app._load_env_file(self.path),
                         {'FOO': 'key=value=more'})


# ---------------------------------------------------------------------------
# _zstd_compress
# ---------------------------------------------------------------------------

class TestZstdCompress(unittest.TestCase):
    def test_happy_path(self):
        with mock.patch.object(app, '_ZSTD_BIN', '/usr/bin/zstd'), \
             mock.patch.object(app.subprocess, 'run',
                               return_value=_cp(rc=0)) as m_run:
            out = app._zstd_compress('/in/scan.mcap', '/out/scan.mcap.zst')
            self.assertEqual(out, '/out/scan.mcap.zst')
            args = m_run.call_args[0][0]
            self.assertEqual(args[0], '/usr/bin/zstd')
            self.assertIn('-1', args)
            self.assertIn('-o', args)

    def test_bad_returncode_raises(self):
        with mock.patch.object(app, '_ZSTD_BIN', '/usr/bin/zstd'), \
             mock.patch.object(app.subprocess, 'run',
                               return_value=_cp(stderr='disk full', rc=1)):
            with self.assertRaises(RuntimeError) as cm:
                app._zstd_compress('/in/scan.mcap', '/out/x.zst')
            self.assertIn('compression failed', str(cm.exception))
            self.assertIn('disk full', str(cm.exception))

    def test_no_zstd_binary_raises(self):
        with mock.patch.object(app, '_ZSTD_BIN', None):
            with self.assertRaises(RuntimeError):
                app._zstd_compress('/in/scan.mcap', '/out/x.zst')


# ---------------------------------------------------------------------------
# _modal_submit  (retry on 5xx)
# ---------------------------------------------------------------------------

class TestModalSubmit(unittest.TestCase):
    def test_happy_path_first_try(self):
        body = json.dumps({'job_id': 'j_abc'})
        with mock.patch.object(app.subprocess, 'run',
                               return_value=_cp(stdout=body + '\n201', rc=0)):
            jid = app._modal_submit('/tmp/x.zst', 'idem-1')
            self.assertEqual(jid, 'j_abc')

    def test_retry_on_5xx_then_success(self):
        body_ok = json.dumps({'job_id': 'j_xyz'})
        responses = [
            _cp(stdout='oops\n503', rc=0),     # 1st: 503 → retry
            _cp(stdout='oops\n502', rc=0),     # 2nd: 502 → retry
            _cp(stdout=body_ok + '\n201', rc=0),  # 3rd: ok
        ]
        with mock.patch.object(app.subprocess, 'run',
                               side_effect=responses), \
             mock.patch.object(app.time, 'sleep'):  # skip backoff sleeps
            jid = app._modal_submit('/tmp/x.zst', 'idem-1')
            self.assertEqual(jid, 'j_xyz')

    def test_aborts_after_max_retries(self):
        responses = [_cp(stdout='oops\n503', rc=0)] * 3
        with mock.patch.object(app.subprocess, 'run',
                               side_effect=responses), \
             mock.patch.object(app.time, 'sleep'):
            with self.assertRaises(RuntimeError):
                app._modal_submit('/tmp/x.zst', 'idem-1')

    def test_4xx_does_not_retry(self):
        with mock.patch.object(app.subprocess, 'run',
                               return_value=_cp(stdout='bad key\n401', rc=0)) as m_run, \
             mock.patch.object(app.time, 'sleep'):
            with self.assertRaises(RuntimeError):
                app._modal_submit('/tmp/x.zst', 'idem-1')
            self.assertEqual(m_run.call_count, 1)

    def test_idem_key_reused_across_retries(self):
        body = json.dumps({'job_id': 'j_ok'})
        responses = [
            _cp(stdout='\n503', rc=0),
            _cp(stdout=body + '\n200', rc=0),
        ]
        with mock.patch.object(app.subprocess, 'run',
                               side_effect=responses) as m_run, \
             mock.patch.object(app.time, 'sleep'):
            app._modal_submit('/tmp/x.zst', 'idem-fixed')
            for call in m_run.call_args_list:
                args = call[0][0]
                self.assertIn('X-Idempotency-Key: idem-fixed', args)


# ---------------------------------------------------------------------------
# _modal_poll  (Retry-After parsing)
# ---------------------------------------------------------------------------

class TestModalPoll(unittest.TestCase):
    def test_parses_retry_after_header(self):
        head = ('HTTP/1.1 200 OK\r\n'
                'Retry-After: 7\r\n'
                'Content-Type: application/json\r\n')
        body = json.dumps({'status': 'stage_5_infer', 'job_id': 'j_x'})
        raw = head + '\r\n' + body
        with mock.patch.object(app.subprocess, 'run',
                               return_value=_cp(stdout=raw, rc=0)):
            env, ra = app._modal_poll('j_x')
            self.assertEqual(env['status'], 'stage_5_infer')
            self.assertEqual(ra, 7)

    def test_retry_after_missing_uses_default(self):
        head = 'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
        body = json.dumps({'status': 'queued'})
        with mock.patch.object(app.subprocess, 'run',
                               return_value=_cp(stdout=head + '\r\n' + body, rc=0)):
            env, ra = app._modal_poll('j_x')
            self.assertEqual(ra, app.POLL_INTERVAL_S)

    def test_5xx_raises(self):
        head = 'HTTP/1.1 503 Service Unavailable\r\n'
        body = 'oops'
        with mock.patch.object(app.subprocess, 'run',
                               return_value=_cp(stdout=head + '\r\n' + body, rc=0)):
            with self.assertRaises(RuntimeError):
                app._modal_poll('j_x')


# ---------------------------------------------------------------------------
# _process_thread integration
# ---------------------------------------------------------------------------

class TestProcessThread(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Pre-create an "mcap file"
        self.recordings = os.path.join(self.tmp, 'recordings', 'sess1', 'rosbag')
        os.makedirs(self.recordings)
        self.mcap = os.path.join(self.recordings, 'scan.mcap')
        with open(self.mcap, 'wb') as f:
            f.write(b'fakebag')
        self.processed = os.path.join(self.tmp, 'processed')

        # Patch app.PROCESSED_DIR + sessions store. We use a fresh sessions
        # dict per test to avoid touching the real sessions.json.
        self._patches = [
            mock.patch.object(app, 'PROCESSED_DIR', self.processed),
            mock.patch.object(app, 'SCRIPT_DIR', self.tmp),
            mock.patch.object(app, 'MODAL_API_KEY', 'test-key'),
            mock.patch.object(app, '_ZSTD_BIN', '/usr/bin/zstd'),
        ]
        for p in self._patches:
            p.start()

        # Ephemeral sessions store.
        self._sessions = {}
        self._get_p = mock.patch.object(
            app, '_get_session',
            side_effect=lambda sid: dict(self._sessions[sid]) if sid in self._sessions else None,
        )
        self._put_p = mock.patch.object(
            app, '_put_session',
            side_effect=lambda sid, s: self._sessions.__setitem__(sid, dict(s)),
        )
        self._get_all = mock.patch.object(
            app, '_get_sessions',
            side_effect=lambda: dict(self._sessions),
        )
        self._del_p = mock.patch.object(
            app, '_delete_session',
            side_effect=lambda sid: self._sessions.pop(sid, None),
        )
        self._get_p.start(); self._put_p.start()
        self._get_all.start(); self._del_p.start()

        # Reset module-level processing state.
        with app._processing_lock:
            app._active_processing.clear()

        self._sessions['sess1'] = {
            'name': 'sess1',
            'status': 'stopped',
            'created': '2026-01-01',
        }

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._get_p.stop(); self._put_p.stop()
        self._get_all.stop(); self._del_p.stop()
        with app._processing_lock:
            app._active_processing.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_envelope_done(self, n_images=2):
        return {
            'status': 'done',
            'job_id': 'j_done',
            'best_images': [
                {'bbox_id': f'bbox_{i}', 'class': 'thing'}
                for i in range(n_images)
            ],
            'floorplan': {'walls': [], 'doors': [], 'windows': []},
            'furniture': [],
            'metrics': {'total_duration_s': 42},
        }

    def _make_processing_entry(self):
        with app._processing_lock:
            app._active_processing['sess1'] = {
                'status': 'processing',
                'stage': 'compressing',
                'start_time': time.time(),
                'cancel': threading.Event(),
                'job_id': None,
            }

    def test_happy_path_writes_artifacts_and_clears_state(self):
        self._make_processing_entry()
        envelope = self._make_envelope_done(n_images=3)

        # zstd: just create the .zst file (so the cleanup branch hits).
        def _zstd_side(src, dst):
            with open(dst, 'wb') as f:
                f.write(b'zst')
            return dst

        # Track which fetches happened; create the local file as side effect
        # so existence-checks pass.
        fetched = []
        def _fetch_side(job_id, path, dest):
            fetched.append(path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, 'wb') as f:
                f.write(b'data')
            return dest

        with mock.patch.object(app, '_zstd_compress', side_effect=_zstd_side), \
             mock.patch.object(app, '_modal_submit', return_value='j_done'), \
             mock.patch.object(app, '_modal_poll',
                               return_value=(envelope, 0)), \
             mock.patch.object(app, '_modal_fetch',
                               side_effect=_fetch_side), \
             mock.patch.object(app, '_modal_cancel') as m_cancel:
            app._process_thread('sess1', self.mcap)

        # Check session ended in `done` state.
        self.assertEqual(self._sessions['sess1']['slam_status'], 'done')
        self.assertEqual(self._sessions['sess1']['slam_result']['status'], 'done')

        # _active_processing cleared.
        with app._processing_lock:
            self.assertNotIn('sess1', app._active_processing)

        # result.json exists.
        result_path = os.path.join(self.processed, 'sess1', 'result.json')
        self.assertTrue(os.path.isfile(result_path))
        with open(result_path) as f:
            self.assertEqual(json.load(f)['status'], 'done')

        # best_views directory wiped+repopulated with one file per image.
        bv = os.path.join(self.processed, 'sess1', 'best_views')
        self.assertTrue(os.path.isdir(bv))
        self.assertEqual(sorted(os.listdir(bv)), ['0.jpg', '1.jpg', '2.jpg'])

        # layout_merged + per-image fetches were attempted.
        self.assertIn('artifact/layout_merged.txt', fetched)
        self.assertIn('image/0', fetched)
        self.assertIn('image/2', fetched)

        # Cancel was never called (no cancel event fired).
        m_cancel.assert_not_called()

        # .zst cleanup ran.
        self.assertFalse(os.path.isfile(self.mcap + '.zst'))

    def test_cancel_pre_submit(self):
        """Event set before submit → no submit, slam_status='cancelled'."""
        self._make_processing_entry()
        # Fire the cancel event before the thread starts.
        with app._processing_lock:
            app._active_processing['sess1']['cancel'].set()

        with mock.patch.object(app, '_zstd_compress',
                               side_effect=lambda s, d: (open(d, 'wb').close() or d)), \
             mock.patch.object(app, '_modal_submit') as m_submit, \
             mock.patch.object(app, '_modal_poll'), \
             mock.patch.object(app, '_modal_cancel') as m_cancel:
            app._process_thread('sess1', self.mcap)

        m_submit.assert_not_called()
        m_cancel.assert_not_called()
        self.assertEqual(self._sessions['sess1']['slam_status'], 'cancelled')
        with app._processing_lock:
            self.assertNotIn('sess1', app._active_processing)

    def test_cancel_mid_poll(self):
        """Cancel fired during the wait loop → _modal_cancel(job_id) once."""
        self._make_processing_entry()

        # First poll returns in-progress; on the second `wait`, the test
        # fires cancel, which makes wait() return True and break the loop.
        polls = iter([
            ({'status': 'stage_5_infer'}, 0),
            ({'status': 'stage_6_layout'}, 0),  # never reached
        ])

        cancel_event = app._active_processing['sess1']['cancel']
        wait_calls = {'n': 0}
        original_wait = cancel_event.wait

        def _wait_side(timeout=None):
            wait_calls['n'] += 1
            if wait_calls['n'] >= 2:
                cancel_event.set()
            return original_wait(0)

        with mock.patch.object(app, '_zstd_compress',
                               side_effect=lambda s, d: (open(d, 'wb').close() or d)), \
             mock.patch.object(app, '_modal_submit', return_value='j_mid'), \
             mock.patch.object(app, '_modal_poll',
                               side_effect=lambda jid: next(polls)), \
             mock.patch.object(app, '_modal_cancel') as m_cancel, \
             mock.patch.object(cancel_event, 'wait', side_effect=_wait_side):
            app._process_thread('sess1', self.mcap)

        m_cancel.assert_called_once_with('j_mid')
        self.assertEqual(self._sessions['sess1']['slam_status'], 'cancelled')

    def test_failed_envelope_records_error(self):
        self._make_processing_entry()
        env_failed = {
            'status': 'failed',
            'error': {'stage': 'stage_3_decode',
                      'stderr_tail': 'mcap parse error'},
        }
        with mock.patch.object(app, '_zstd_compress',
                               side_effect=lambda s, d: (open(d, 'wb').close() or d)), \
             mock.patch.object(app, '_modal_submit', return_value='j_fail'), \
             mock.patch.object(app, '_modal_poll',
                               return_value=(env_failed, 0)), \
             mock.patch.object(app, '_modal_fetch'), \
             mock.patch.object(app, '_modal_cancel'):
            app._process_thread('sess1', self.mcap)

        self.assertEqual(self._sessions['sess1']['slam_status'], 'error')
        err = self._sessions['sess1']['slam_error']
        self.assertIn('stage_3_decode', err)
        self.assertIn('mcap parse error', err)


# ---------------------------------------------------------------------------
# /api/session/<id>/process route — guards
# ---------------------------------------------------------------------------

class TestProcessRoute(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.recordings = os.path.join(self.tmp, 'recordings', 's1', 'rosbag')
        os.makedirs(self.recordings)
        with open(os.path.join(self.recordings, 'scan.mcap'), 'wb') as f:
            f.write(b'x')
        self.client = app.app.test_client()
        self._patches = [
            mock.patch.object(app, 'RECORDINGS_DIR',
                              os.path.join(self.tmp, 'recordings')),
            mock.patch.object(app, '_ZSTD_BIN', '/usr/bin/zstd'),
        ]
        for p in self._patches:
            p.start()
        self._sessions = {'sid1': {'name': 's1', 'status': 'stopped'}}
        self._get_p = mock.patch.object(
            app, '_get_session',
            side_effect=lambda sid: dict(self._sessions[sid]) if sid in self._sessions else None,
        )
        self._put_p = mock.patch.object(
            app, '_put_session',
            side_effect=lambda sid, s: self._sessions.__setitem__(sid, dict(s)),
        )
        self._get_p.start(); self._put_p.start()
        with app._processing_lock:
            app._active_processing.clear()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._get_p.stop(); self._put_p.stop()
        with app._processing_lock:
            app._active_processing.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_503_when_modal_api_missing(self):
        with mock.patch.object(app, 'MODAL_API_KEY', ''):
            r = self.client.post('/api/session/sid1/process', json={})
        self.assertEqual(r.status_code, 503)

    def test_503_when_zstd_missing(self):
        with mock.patch.object(app, 'MODAL_API_KEY', 'k'), \
             mock.patch.object(app, '_ZSTD_BIN', None):
            r = self.client.post('/api/session/sid1/process', json={})
        self.assertEqual(r.status_code, 503)

    def test_409_when_already_processing(self):
        with app._processing_lock:
            app._active_processing['sid1'] = {
                'status': 'processing',
                'stage': 'queued',
                'start_time': time.time(),
                'cancel': threading.Event(),
                'job_id': 'j',
            }
        with mock.patch.object(app, 'MODAL_API_KEY', 'k'):
            r = self.client.post('/api/session/sid1/process', json={})
        self.assertEqual(r.status_code, 409)


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

class TestRecoverStuckSessions(unittest.TestCase):
    def setUp(self):
        self._sessions = {}
        self._get_all = mock.patch.object(
            app, '_get_sessions',
            side_effect=lambda: dict(self._sessions),
        )
        self._put_p = mock.patch.object(
            app, '_put_session',
            side_effect=lambda sid, s: self._sessions.__setitem__(sid, dict(s)),
        )
        self._get_all.start(); self._put_p.start()

    def tearDown(self):
        self._get_all.stop(); self._put_p.stop()

    def test_recovers_stuck_slam_and_calls_modal_cancel(self):
        self._sessions['a'] = {
            'name': 'a', 'status': 'stopped',
            'slam_status': 'stage_5_infer', 'job_id': 'j_a',
        }
        self._sessions['b'] = {
            'name': 'b', 'status': 'stopped',
            'slam_status': 'queued', 'job_id': 'j_b',
        }
        self._sessions['c'] = {
            'name': 'c', 'status': 'stopped',
            'slam_status': 'done', 'job_id': 'j_c',
        }
        with mock.patch.object(app, 'MODAL_API_KEY', 'k'), \
             mock.patch.object(app, '_modal_cancel') as m_cancel:
            app._recover_stuck_sessions()

        cancelled = sorted(c[0][0] for c in m_cancel.call_args_list)
        self.assertEqual(cancelled, ['j_a', 'j_b'])
        self.assertEqual(self._sessions['a']['slam_status'], 'error')
        self.assertEqual(self._sessions['b']['slam_status'], 'error')
        self.assertEqual(self._sessions['c']['slam_status'], 'done')

    def test_cancelled_state_is_recovered(self):
        self._sessions['x'] = {
            'name': 'x', 'status': 'stopped',
            'slam_status': 'cancelled', 'job_id': 'j_x',
        }
        with mock.patch.object(app, 'MODAL_API_KEY', 'k'), \
             mock.patch.object(app, '_modal_cancel'):
            app._recover_stuck_sessions()
        self.assertEqual(self._sessions['x']['slam_status'], 'error')


# ---------------------------------------------------------------------------
# Artifact proxy whitelist
# ---------------------------------------------------------------------------

class TestArtifactProxy(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self._sessions = {'s1': {'name': 's1', 'job_id': 'j_test'}}
        self._get_p = mock.patch.object(
            app, '_get_session',
            side_effect=lambda sid: dict(self._sessions[sid]) if sid in self._sessions else None,
        )
        self._get_p.start()

    def tearDown(self):
        self._get_p.stop()

    def test_unknown_artifact_404(self):
        with mock.patch.object(app, 'MODAL_API_KEY', 'k'):
            r = self.client.get('/api/session/s1/artifact/secret.txt')
        self.assertEqual(r.status_code, 404)

    def test_path_traversal_404(self):
        with mock.patch.object(app, 'MODAL_API_KEY', 'k'):
            # Flask routing for `<name>` won't match a slash, so this is
            # actually a 404 from Flask routing, but verify anyway.
            r = self.client.get('/api/session/s1/artifact/..%2Fetc%2Fpasswd')
        self.assertEqual(r.status_code, 404)


if __name__ == '__main__':
    unittest.main()
