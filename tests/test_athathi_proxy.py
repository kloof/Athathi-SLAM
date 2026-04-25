"""Tests for `athathi_proxy.py` — the Pi-side HTTP forwarder.

NO real network calls. Every test mocks `subprocess.run` to simulate curl
responses, mirroring the style of `tests/test_processing.py`.

Coverage (per the §16 step 2 brief):
  1. login happy path
  2. login 401 → AthathiError(401, ...)
  3. login network error (curl returncode != 0) → AthathiError(0, ...)
  4. get_schedule returns bare list verbatim
  5. get_schedule on 5xx — no retries; raises immediately
  6. complete_scan retries 5xx 3 times then raises
  7. complete_scan returns dict on first success
  8. visual_search_full posts multipart with `file=@<path>` and returns body
  9. cached_get returns cached blob within TTL (no fetch_fn call)
  10. cached_get calls fetch_fn after TTL expiry
  11. cached_get falls back to STALE cache on AthathiError(status_code=0, ...)
  12. cached_get does NOT fall back to stale cache on non-network errors
"""

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cp(stdout='', stderr='', rc=0):
    """Build a fake `subprocess.CompletedProcess` shaped object."""
    res = mock.Mock()
    res.stdout = stdout
    res.stderr = stderr
    res.returncode = rc
    return res


def _curl_response(body, http_code):
    """Compose what `curl -w '\\n%{http_code}'` produces."""
    return _cp(stdout=f'{body}\n{http_code}', rc=0)


# ---------------------------------------------------------------------------
# Base — every test points auth.ATHATHI_DIR at a tempdir so cache writes don't
# pollute disk and the api_url is deterministic.
# ---------------------------------------------------------------------------

class _ProxyTestBase(unittest.TestCase):
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
        # Seed config so api_url is known.
        auth.save_config({
            'api_url': 'http://test.athathi.local',
            'upload_endpoint': 'http://test.athathi.local',
            'last_user': '',
            'post_submit_hook': None,
            'image_transport': 'multipart',
            'visual_search_cache_ttl_s': 86400,
        })

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(auth, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1–3. login
# ---------------------------------------------------------------------------

class TestLogin(_ProxyTestBase):
    def test_happy_path_returns_body_with_token(self):
        body = json.dumps({
            'message': 'ok',
            'user': {'id': 7, 'username': 'tech', 'user_type': 'technician'},
            'token': 'jwt-abc.def.ghi',
        })
        with mock.patch.object(athathi_proxy.subprocess, 'run',
                               return_value=_curl_response(body, 200)):
            out = athathi_proxy.login('tech', 'pw')
            self.assertEqual(out['token'], 'jwt-abc.def.ghi')
            self.assertEqual(out['user']['username'], 'tech')

    def test_401_raises_invalid_credentials(self):
        body = json.dumps({'detail': 'Invalid credentials'})
        with mock.patch.object(athathi_proxy.subprocess, 'run',
                               return_value=_curl_response(body, 401)):
            with self.assertRaises(athathi_proxy.AthathiError) as cm:
                athathi_proxy.login('tech', 'wrong')
            self.assertEqual(cm.exception.status_code, 401)
            self.assertIn('Invalid credentials', str(cm.exception))

    def test_network_error_raises_status_zero(self):
        # curl rc != 0 → network error.
        with mock.patch.object(athathi_proxy.subprocess, 'run',
                               return_value=_cp(stderr='curl: (7) Failed to connect',
                                                rc=7)):
            with self.assertRaises(athathi_proxy.AthathiError) as cm:
                athathi_proxy.login('tech', 'pw')
            self.assertEqual(cm.exception.status_code, 0)
            self.assertIn('Network error', str(cm.exception))

    def test_login_passes_no_authorization_header(self):
        body = json.dumps({'token': 't', 'user': {}})
        with mock.patch.object(athathi_proxy.subprocess, 'run',
                               return_value=_curl_response(body, 200)) as m_run:
            athathi_proxy.login('tech', 'pw')
            args = m_run.call_args[0][0]
            for arg in args:
                # No Authorization header should be present on login.
                self.assertNotIn('Authorization:', str(arg))


# ---------------------------------------------------------------------------
# 4–5. get_schedule
# ---------------------------------------------------------------------------

class TestGetSchedule(_ProxyTestBase):
    def test_returns_bare_list(self):
        body = json.dumps([
            {'scan_id': 42, 'customer_name': 'Smith'},
            {'scan_id': 43, 'customer_name': 'Jones'},
        ])
        with mock.patch.object(athathi_proxy.subprocess, 'run',
                               return_value=_curl_response(body, 200)):
            out = athathi_proxy.get_schedule('tok')
            self.assertIsInstance(out, list)
            self.assertEqual(out[0]['scan_id'], 42)

    def test_5xx_does_not_retry(self):
        with mock.patch.object(athathi_proxy.subprocess, 'run',
                               return_value=_curl_response('boom', 503)) as m_run, \
             mock.patch.object(athathi_proxy._time, 'sleep'):
            with self.assertRaises(athathi_proxy.AthathiError) as cm:
                athathi_proxy.get_schedule('tok')
            self.assertEqual(cm.exception.status_code, 503)
            # Exactly ONE call — no retry on read-only endpoints.
            self.assertEqual(m_run.call_count, 1)

    def test_passes_bearer_header(self):
        body = json.dumps([])
        with mock.patch.object(athathi_proxy.subprocess, 'run',
                               return_value=_curl_response(body, 200)) as m_run:
            athathi_proxy.get_schedule('mytok123')
            args = m_run.call_args[0][0]
            self.assertIn('Authorization: Bearer mytok123', args)


# ---------------------------------------------------------------------------
# 6–7. complete_scan retry
# ---------------------------------------------------------------------------

class TestCompleteScan(_ProxyTestBase):
    def test_retries_5xx_three_times_then_raises(self):
        responses = [_curl_response('boom', 503)] * 3
        with mock.patch.object(athathi_proxy.subprocess, 'run',
                               side_effect=responses) as m_run, \
             mock.patch.object(athathi_proxy._time, 'sleep'):
            with self.assertRaises(athathi_proxy.AthathiError) as cm:
                athathi_proxy.complete_scan('tok', 42)
            self.assertEqual(cm.exception.status_code, 503)
            self.assertEqual(m_run.call_count, 3)

    def test_returns_dict_on_first_success(self):
        body = json.dumps({'message': 'completed', 'scan_id': 42})
        with mock.patch.object(athathi_proxy.subprocess, 'run',
                               return_value=_curl_response(body, 200)) as m_run, \
             mock.patch.object(athathi_proxy._time, 'sleep'):
            out = athathi_proxy.complete_scan('tok', 42)
            self.assertEqual(out['scan_id'], 42)
            self.assertEqual(m_run.call_count, 1)

    def test_4xx_does_not_retry(self):
        # 404 should NOT trigger retries — only 5xx do.
        responses = [_curl_response('not found', 404)]
        with mock.patch.object(athathi_proxy.subprocess, 'run',
                               side_effect=responses) as m_run, \
             mock.patch.object(athathi_proxy._time, 'sleep'):
            with self.assertRaises(athathi_proxy.AthathiError) as cm:
                athathi_proxy.complete_scan('tok', 42)
            self.assertEqual(cm.exception.status_code, 404)
            self.assertEqual(m_run.call_count, 1)


# ---------------------------------------------------------------------------
# 8. visual_search_full multipart
# ---------------------------------------------------------------------------

class TestVisualSearchFull(_ProxyTestBase):
    def test_multipart_upload_returns_parsed_body(self):
        # Need a real file on disk for visual_search_full's existence check.
        img_path = os.path.join(self.tmp, 'thumb.jpg')
        with open(img_path, 'wb') as f:
            f.write(b'\xff\xd8\xff\xe0fake-jpeg')

        body = json.dumps({
            'results': [
                {'id': 1, 'name': 'Sofa', 'similarity': '0.91'},
            ],
            'total_time': 1.93,
        })
        with mock.patch.object(athathi_proxy.subprocess, 'run',
                               return_value=_curl_response(body, 200)) as m_run:
            out = athathi_proxy.visual_search_full('tok', img_path)
            self.assertIn('results', out)
            self.assertEqual(out['results'][0]['name'], 'Sofa')

            # Verify the multipart `-F file=@<path>` is passed to curl.
            args = m_run.call_args[0][0]
            joined = ' '.join(args)
            self.assertIn('-F', args)
            self.assertIn(f'file=@{img_path}', joined)

    def test_missing_file_raises_athathi_error(self):
        with self.assertRaises(athathi_proxy.AthathiError) as ctx:
            athathi_proxy.visual_search_full('tok', '/no/such/file.jpg')
        self.assertEqual(ctx.exception.status_code, 0)
        self.assertIn('image file not found', ctx.exception.body)


# ---------------------------------------------------------------------------
# 9–12. cached_get
# ---------------------------------------------------------------------------

class TestCachedGet(_ProxyTestBase):
    def test_returns_cached_blob_within_ttl_no_fetch(self):
        key = 'sched__abc12345'
        path = athathi_proxy._cache_path(key)
        athathi_proxy._write_cache(path, [{'scan_id': 42}])

        fetch_called = []

        def _fetch():
            fetch_called.append(1)
            return [{'scan_id': 99}]

        out = athathi_proxy.cached_get(key, ttl=300, fetch_fn=_fetch)
        self.assertEqual(out, [{'scan_id': 42}])
        self.assertEqual(fetch_called, [])  # never called

    def test_calls_fetch_after_ttl_expiry(self):
        key = 'sched__abc12345'
        path = athathi_proxy._cache_path(key)
        athathi_proxy._write_cache(path, [{'scan_id': 42}])

        # Hand-edit the timestamp to be ancient.
        with open(path, 'r') as f:
            wrapper = json.load(f)
        wrapper['cached_at'] = _time.time() - 10_000
        with open(path, 'w') as f:
            json.dump(wrapper, f)

        fetch_called = []

        def _fetch():
            fetch_called.append(1)
            return [{'scan_id': 99}]

        out = athathi_proxy.cached_get(key, ttl=300, fetch_fn=_fetch)
        self.assertEqual(out, [{'scan_id': 99}])
        self.assertEqual(len(fetch_called), 1)
        # And the new value is now in the cache.
        ts2, blob2 = athathi_proxy._read_cache(path)
        self.assertEqual(blob2, [{'scan_id': 99}])

    def test_falls_back_to_stale_cache_on_network_error(self):
        from datetime import datetime, timezone

        key = 'sched__abc12345'
        path = athathi_proxy._cache_path(key)
        athathi_proxy._write_cache(path, [{'scan_id': 42}])

        # Force the cache to be stale, with a known timestamp.
        forced_ts = _time.time() - 10_000
        with open(path, 'r') as f:
            wrapper = json.load(f)
        wrapper['cached_at'] = forced_ts
        with open(path, 'w') as f:
            json.dump(wrapper, f)

        def _fetch():
            raise athathi_proxy.AthathiError(0, 'connection refused', 'Network error')

        result = athathi_proxy.cached_get(key, ttl=300, fetch_fn=_fetch)
        # cached_get returns a StaleCacheResult sentinel on stale-on-error fallback.
        self.assertIsInstance(result, athathi_proxy.StaleCacheResult)
        self.assertEqual(result.blob, [{'scan_id': 42}])
        self.assertEqual(result.reason, 'network')

        # `fetched_at_iso` must reflect the cache-file's `cached_at` epoch
        # (within rounding) — NOT the current time. The frontend banner uses
        # this to render the actual last-refresh time.
        self.assertIsNotNone(result.fetched_at_iso)
        expected_iso = datetime.fromtimestamp(
            forced_ts, tz=timezone.utc,
        ).isoformat().replace('+00:00', 'Z')
        self.assertEqual(result.fetched_at_iso, expected_iso)
        # And it must NOT be "now" — sanity check it's well in the past.
        now_iso = datetime.now(tz=timezone.utc).isoformat().replace('+00:00', 'Z')
        self.assertLess(result.fetched_at_iso, now_iso)

    def test_does_not_fall_back_on_non_network_errors(self):
        key = 'sched__abc12345'
        path = athathi_proxy._cache_path(key)
        athathi_proxy._write_cache(path, [{'scan_id': 42}])

        # Force the cache to be stale.
        with open(path, 'r') as f:
            wrapper = json.load(f)
        wrapper['cached_at'] = _time.time() - 10_000
        with open(path, 'w') as f:
            json.dump(wrapper, f)

        def _fetch():
            raise athathi_proxy.AthathiError(503, 'oops', 'Upstream 503')

        with self.assertRaises(athathi_proxy.AthathiError) as cm:
            athathi_proxy.cached_get(key, ttl=300, fetch_fn=_fetch)
        self.assertEqual(cm.exception.status_code, 503)


# ---------------------------------------------------------------------------
# Smoke — get_history / get_categories / cancel_scan / logout (light coverage,
# they share plumbing with the above so this is just a wiring check).
# ---------------------------------------------------------------------------

class TestSmoke(_ProxyTestBase):
    def test_get_history_returns_bare_list(self):
        body = json.dumps([{'scan_id': 1}, {'scan_id': 2}])
        with mock.patch.object(athathi_proxy.subprocess, 'run',
                               return_value=_curl_response(body, 200)):
            out = athathi_proxy.get_history('tok')
            self.assertEqual(len(out), 2)

    def test_get_categories_returns_dict(self):
        body = json.dumps({'categories': [
            {'id': 24, 'name': 'Chair', 'description': ''},
            {'id': 26, 'name': 'Chairs', 'description': ''},
        ]})
        with mock.patch.object(athathi_proxy.subprocess, 'run',
                               return_value=_curl_response(body, 200)):
            out = athathi_proxy.get_categories('tok')
            self.assertEqual(len(out['categories']), 2)

    def test_cancel_scan_returns_dict(self):
        body = json.dumps({'message': 'cancelled', 'scan_id': 9})
        with mock.patch.object(athathi_proxy.subprocess, 'run',
                               return_value=_curl_response(body, 200)), \
             mock.patch.object(athathi_proxy._time, 'sleep'):
            out = athathi_proxy.cancel_scan('tok', 9)
            self.assertEqual(out['scan_id'], 9)

    def test_logout_swallows_athathi_errors(self):
        # logout should never raise even if upstream returns 500.
        with mock.patch.object(athathi_proxy.subprocess, 'run',
                               return_value=_curl_response('boom', 500)):
            # Should not raise.
            athathi_proxy.logout('tok')

    def test_logout_swallows_network_errors(self):
        with mock.patch.object(athathi_proxy.subprocess, 'run',
                               return_value=_cp(stderr='refused', rc=7)):
            athathi_proxy.logout('tok')

    def test_logout_with_no_token_is_noop(self):
        # No subprocess call when token is empty.
        with mock.patch.object(athathi_proxy.subprocess, 'run') as m_run:
            athathi_proxy.logout('')
            self.assertEqual(m_run.call_count, 0)

    def test_stream_artifact_raises_notimplemented(self):
        with self.assertRaises(NotImplementedError):
            athathi_proxy.stream_artifact('tok', 1, 'result.json')


# ---------------------------------------------------------------------------
# upload_bundle (Step 6 — multipart submit)
# ---------------------------------------------------------------------------

class TestUploadBundle(_ProxyTestBase):
    def test_happy_path_sends_envelope_and_images(self):
        # Drop a couple of fake image files we can reference.
        img0 = os.path.join(self.tmp, '0.jpg')
        img1 = os.path.join(self.tmp, '1.jpg')
        with open(img0, 'wb') as f:
            f.write(b'\xff\xd80')
        with open(img1, 'wb') as f:
            f.write(b'\xff\xd81')

        body = json.dumps({'received': 2, 'ok': True})
        with mock.patch.object(athathi_proxy.subprocess, 'run',
                               return_value=_curl_response(body, 200)) as m_run:
            out = athathi_proxy.upload_bundle(
                'tok-xyz', 'http://upload.test/api/upload',
                envelope_json_bytes=b'{"hello":"world"}',
                image_files=[('image_bbox_0', img0),
                             ('image_bbox_1', img1)],
            )
        self.assertEqual(out, {'received': 2, 'ok': True})

        # Inspect the curl args.
        args = m_run.call_args[0][0]
        joined = ' '.join(args)
        # Bearer header set.
        self.assertIn('Authorization: Bearer tok-xyz', args)
        # Envelope multipart form field as application/json.
        self.assertIn('-F', args)
        self.assertIn('envelope=@', joined)
        self.assertIn(';type=application/json', joined)
        # Image fields per pair.
        self.assertIn(f'image_bbox_0=@{img0}', joined)
        self.assertIn(f'image_bbox_1=@{img1}', joined)
        # POST + URL.
        self.assertIn('http://upload.test/api/upload', args)

    def test_5xx_retries_three_times_then_raises(self):
        responses = [_curl_response('boom', 503)] * 3
        with mock.patch.object(athathi_proxy.subprocess, 'run',
                               side_effect=responses) as m_run, \
             mock.patch.object(athathi_proxy._time, 'sleep'):
            with self.assertRaises(athathi_proxy.AthathiError) as ctx:
                athathi_proxy.upload_bundle(
                    'tok', 'http://upload.test/api/upload',
                    b'{}', image_files=[],
                )
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(m_run.call_count, 3)

    def test_4xx_does_not_retry(self):
        with mock.patch.object(athathi_proxy.subprocess, 'run',
                               return_value=_curl_response('bad request', 400)) as m_run, \
             mock.patch.object(athathi_proxy._time, 'sleep'):
            with self.assertRaises(athathi_proxy.AthathiError) as ctx:
                athathi_proxy.upload_bundle(
                    'tok', 'http://upload.test/api/upload',
                    b'{}', image_files=[],
                )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(m_run.call_count, 1)

    def test_network_error_raises_status_zero(self):
        with mock.patch.object(athathi_proxy.subprocess, 'run',
                               return_value=_cp(stderr='curl: refused', rc=7)):
            with self.assertRaises(athathi_proxy.AthathiError) as ctx:
                athathi_proxy.upload_bundle(
                    'tok', 'http://upload.test/api/upload',
                    b'{}', image_files=[],
                )
        self.assertEqual(ctx.exception.status_code, 0)

    def test_missing_endpoint_raises(self):
        with self.assertRaises(athathi_proxy.AthathiError):
            athathi_proxy.upload_bundle('tok', '', b'{}', image_files=[])


if __name__ == '__main__':
    unittest.main()
