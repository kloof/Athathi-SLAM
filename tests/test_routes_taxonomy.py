"""Flask-route tests for the Step 7 taxonomy + visual-search persistence.

NO real network: every test mocks `athathi_proxy.get_categories` and
`athathi_proxy.visual_search_full`.

Coverage (per the §16 step 7 brief):
   1. GET  /api/taxonomy/classes 401 when not logged in.
   2. GET  /api/taxonomy/classes happy: fresh cache used, no upstream.
   3. GET  /api/taxonomy/classes cache stale: refreshes via get_categories.
   4. GET  /api/taxonomy/classes network failure with cache: fallback.
   5. POST /api/taxonomy/learned adds a class.
   6. POST /review/find_product/<idx> 401 when not logged in.
   7. POST /review/find_product/<idx> happy: cache miss → upstream → cached.
   8. POST /review/find_product/<idx> cache hit: cached body + X-Cached header.
   9. POST /review/find_product/<idx> 404 when no image on disk.
  10. POST /review/link_product writes linked_product with linked_at + linked_by.
  11. POST /review/link_product accepts product=null (no-match).
  12. DELETE /api/visual-search/cache removes the cache directory.
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

import auth            # noqa: E402
import projects        # noqa: E402
import review          # noqa: E402
import taxonomy        # noqa: E402
import athathi_proxy   # noqa: E402
import app             # noqa: E402


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

class _TaxonomyRouteBase(unittest.TestCase):
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
        auth.LEARNED_CLASSES_PATH = os.path.join(
            self.tmp_auth, 'learned_classes.json')

        self.tmp_projects = tempfile.mkdtemp()
        self._orig_projects_root = projects.PROJECTS_ROOT
        projects.PROJECTS_ROOT = self.tmp_projects

        # Override legacy processed root to a tempdir so taxonomy walks
        # don't see test_slam/processed/ from the dev tree.
        self.tmp_legacy = tempfile.mkdtemp()
        self._legacy_patcher = mock.patch(
            'taxonomy._legacy_processed_root',
            return_value=self.tmp_legacy,
        )
        self._legacy_patcher.start()

        self.client = app.app.test_client()

    def tearDown(self):
        self._legacy_patcher.stop()
        for k, v in self._orig_auth.items():
            setattr(auth, k, v)
        projects.PROJECTS_ROOT = self._orig_projects_root
        shutil.rmtree(self.tmp_auth, ignore_errors=True)
        shutil.rmtree(self.tmp_projects, ignore_errors=True)
        shutil.rmtree(self.tmp_legacy, ignore_errors=True)

    # ----- helpers ---------------------------------------------------

    def _login(self, username='tech', user_id=7):
        auth.write_token(_fresh_jwt())
        auth.write_auth({'user_id': user_id, 'username': username})

    def _ensure_project(self, scan_id=42, name='Smith'):
        projects.ensure_project(scan_id, athathi_meta={'customer_name': name})

    def _seed_run_with_image(self, scan_id=42, scan_name='living_room',
                             run_id='20260425_142103', image_bytes=None,
                             with_recapture=False):
        if projects.read_manifest(scan_id) is None:
            self._ensure_project(scan_id=scan_id)
        if not os.path.isdir(projects.scan_dir(scan_id, scan_name)):
            projects.create_scan(scan_id, scan_name)
        rd = projects.processed_dir_for_run(scan_id, scan_name, run_id)
        os.makedirs(rd, exist_ok=True)
        envelope = {
            'job_id': 'j_test',
            'status': 'done',
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
        with open(os.path.join(rd, 'result.json'), 'w') as f:
            json.dump(envelope, f)
        bv = os.path.join(rd, 'best_views')
        os.makedirs(bv, exist_ok=True)
        if image_bytes is None:
            image_bytes = b'\xff\xd8\xff' + b'sofa-bytes-' * 8
        if with_recapture:
            with open(os.path.join(bv, '0_recapture.jpg'), 'wb') as f:
                f.write(image_bytes)
        else:
            with open(os.path.join(bv, '0.jpg'), 'wb') as f:
                f.write(image_bytes)
        projects.set_active_run(scan_id, scan_name, run_id)
        return rd


# ---------------------------------------------------------------------------
# 1. GET /api/taxonomy/classes — auth gate
# ---------------------------------------------------------------------------

class TestTaxonomyAuthGate(_TaxonomyRouteBase):
    def test_not_logged_in_returns_401(self):
        r = self.client.get('/api/taxonomy/classes')
        self.assertEqual(r.status_code, 401)


# ---------------------------------------------------------------------------
# 2. GET /api/taxonomy/classes — fresh cache used, no upstream
# ---------------------------------------------------------------------------

class TestTaxonomyFreshCache(_TaxonomyRouteBase):
    def test_fresh_cache_avoids_upstream(self):
        self._login()
        # Pre-cache a fresh upstream snapshot.
        cats = [{'id': 4, 'name': 'Sofa'},
                {'id': 24, 'name': 'Chair'},
                {'id': 26, 'name': 'Chairs'}]
        taxonomy.cache_athathi_categories(cats)
        with mock.patch.object(athathi_proxy, 'get_categories') as gc:
            r = self.client.get('/api/taxonomy/classes')
            self.assertEqual(gc.call_count, 0,
                             'fresh cache must not trigger an upstream call')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        names = {d['name'] for d in body['classes']}
        self.assertIn('Sofa', names)
        self.assertIn('Chair', names)
        self.assertIn('Chairs', names)


# ---------------------------------------------------------------------------
# 3. GET /api/taxonomy/classes — cache stale → refreshes
# ---------------------------------------------------------------------------

class TestTaxonomyStaleRefresh(_TaxonomyRouteBase):
    def test_stale_cache_calls_upstream_and_refreshes(self):
        self._login()
        # Stale on-disk cache.
        cats_old = [{'id': 1, 'name': 'StaleSofa'}]
        taxonomy.cache_athathi_categories(cats_old)
        path = os.path.join(self.tmp_auth, taxonomy.TAXONOMY_CACHE_NAME)
        with open(path, 'r') as f:
            data = json.load(f)
        data['cached_at'] = _time.time() - 7200  # 2 h old → stale
        with open(path, 'w') as f:
            json.dump(data, f)

        cats_new = [{'id': 4, 'name': 'Sofa'}, {'id': 5, 'name': 'Bed'}]
        with mock.patch.object(athathi_proxy, 'get_categories',
                               return_value=cats_new) as gc:
            r = self.client.get('/api/taxonomy/classes')
            self.assertEqual(gc.call_count, 1)

        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        names = {d['name'] for d in body['classes']}
        self.assertIn('Sofa', names)
        self.assertIn('Bed', names)
        # On-disk cache refreshed (post-call).
        self.assertEqual(
            taxonomy.load_cached_athathi_categories(max_age_s=3600),
            cats_new,
        )


# ---------------------------------------------------------------------------
# 4. GET /api/taxonomy/classes — network failure with cache → fallback
# ---------------------------------------------------------------------------

class TestTaxonomyNetworkFallback(_TaxonomyRouteBase):
    def test_network_failure_serves_stale_cache(self):
        self._login()
        cats_old = [{'id': 1, 'name': 'StaleSofa'}]
        taxonomy.cache_athathi_categories(cats_old)
        # Mark stale.
        path = os.path.join(self.tmp_auth, taxonomy.TAXONOMY_CACHE_NAME)
        with open(path, 'r') as f:
            data = json.load(f)
        data['cached_at'] = _time.time() - 7200
        with open(path, 'w') as f:
            json.dump(data, f)

        net_err = athathi_proxy.AthathiError(0, 'curl: connect refused',
                                             'Network error')
        with mock.patch.object(athathi_proxy, 'get_categories',
                               side_effect=net_err):
            r = self.client.get('/api/taxonomy/classes')
        self.assertEqual(r.status_code, 200)
        names = {d['name'] for d in r.get_json()['classes']}
        # Stale entry is still surfaced.
        self.assertIn('StaleSofa', names)


# ---------------------------------------------------------------------------
# 5. POST /api/taxonomy/learned — adds a class
# ---------------------------------------------------------------------------

class TestTaxonomyLearnedAdd(_TaxonomyRouteBase):
    def test_adds_and_returns_dict(self):
        self._login()
        r = self.client.post('/api/taxonomy/learned',
                             json={'name': 'ottoman'})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body['learned_classes'], {'ottoman': 1})
        # Calling twice bumps the count.
        r = self.client.post('/api/taxonomy/learned',
                             json={'name': 'ottoman'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['learned_classes'], {'ottoman': 2})

    def test_blank_name_returns_400(self):
        self._login()
        r = self.client.post('/api/taxonomy/learned', json={'name': '   '})
        self.assertEqual(r.status_code, 400)

    def test_not_logged_in_returns_401(self):
        r = self.client.post('/api/taxonomy/learned', json={'name': 'x'})
        self.assertEqual(r.status_code, 401)


# ---------------------------------------------------------------------------
# 6. POST /review/find_product/<idx> — 401 when not logged in
# ---------------------------------------------------------------------------

class TestFindProductAuthGate(_TaxonomyRouteBase):
    def test_not_logged_in_returns_401(self):
        # Even without seeding the run, the auth check is first.
        r = self.client.post(
            '/api/project/42/scan/living_room/review/find_product/0',
        )
        self.assertEqual(r.status_code, 401)


# ---------------------------------------------------------------------------
# 7. POST /review/find_product/<idx> — cache miss → upstream → cached
# ---------------------------------------------------------------------------

class TestFindProductCacheMiss(_TaxonomyRouteBase):
    def test_cache_miss_calls_upstream_and_caches(self):
        self._login()
        self._seed_run_with_image()

        upstream_body = {
            'results': [
                {'id': 100, 'name': 'Pearla Sofa', 'similarity': 0.91},
            ],
            'total_time': 1.7,
        }
        with mock.patch.object(athathi_proxy, 'visual_search_full',
                               return_value=upstream_body) as vs:
            r = self.client.post(
                '/api/project/42/scan/living_room/review/find_product/0',
            )
            self.assertEqual(vs.call_count, 1)
            args = vs.call_args
            # 2nd positional arg is the on-disk image path.
            self.assertTrue(args[0][1].endswith('0.jpg'))

        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body.get('cached'))
        self.assertEqual(body['total_time'], 1.7)
        self.assertEqual(body['results'][0]['id'], 100)

        # Cache directory now has exactly one file.
        cache_dir = os.path.join(self.tmp_auth, 'cache', 'visual_search')
        self.assertTrue(os.path.isdir(cache_dir))
        cached = [f for f in os.listdir(cache_dir) if f.endswith('.json')]
        self.assertEqual(len(cached), 1)


# ---------------------------------------------------------------------------
# 8. POST /review/find_product/<idx> — cache hit
# ---------------------------------------------------------------------------

class TestFindProductCacheHit(_TaxonomyRouteBase):
    def test_cache_hit_returns_cached_body_with_header(self):
        self._login()
        self._seed_run_with_image()

        upstream_body = {
            'results': [{'id': 200, 'name': 'X'}],
            'total_time': 2.0,
        }
        # First call populates the cache.
        with mock.patch.object(athathi_proxy, 'visual_search_full',
                               return_value=upstream_body) as vs:
            r1 = self.client.post(
                '/api/project/42/scan/living_room/review/find_product/0',
            )
            self.assertEqual(vs.call_count, 1)
        self.assertEqual(r1.status_code, 200)
        self.assertFalse(r1.get_json().get('cached'))

        # Second call must NOT call upstream — it's a cache hit.
        with mock.patch.object(athathi_proxy, 'visual_search_full',
                               return_value=upstream_body) as vs2:
            r2 = self.client.post(
                '/api/project/42/scan/living_room/review/find_product/0',
            )
            self.assertEqual(vs2.call_count, 0)
        self.assertEqual(r2.status_code, 200)
        body = r2.get_json()
        self.assertTrue(body.get('cached'))
        self.assertEqual(r2.headers.get('X-Cached'), 'true')
        # Underlying body is preserved.
        self.assertEqual(body['results'][0]['id'], 200)
        self.assertEqual(body['total_time'], 2.0)


# ---------------------------------------------------------------------------
# 9. POST /review/find_product/<idx> — 404 when no image on disk
# ---------------------------------------------------------------------------

class TestFindProductNoImage(_TaxonomyRouteBase):
    def test_no_image_returns_404(self):
        self._login()
        # Seed a run WITHOUT writing the JPEG.
        self._ensure_project()
        projects.create_scan(42, 'living_room')
        rd = projects.processed_dir_for_run(42, 'living_room', 'X')
        os.makedirs(rd, exist_ok=True)
        with open(os.path.join(rd, 'result.json'), 'w') as f:
            json.dump({'furniture': [{'id': 'bbox_0', 'class': 'sofa'}],
                       'best_images': [{'bbox_id': 'bbox_0',
                                        'class': 'sofa'}]}, f)
        projects.set_active_run(42, 'living_room', 'X')

        with mock.patch.object(athathi_proxy, 'visual_search_full') as vs:
            r = self.client.post(
                '/api/project/42/scan/living_room/review/find_product/0',
            )
            self.assertEqual(vs.call_count, 0)
        self.assertEqual(r.status_code, 404)


# ---------------------------------------------------------------------------
# 10. POST /review/link_product — writes linked_product with linked_at + by
# ---------------------------------------------------------------------------

class TestLinkProductWritesMetadata(_TaxonomyRouteBase):
    def test_writes_linked_product_with_linked_at_and_by(self):
        self._login(username='alice')
        rd = self._seed_run_with_image()

        product = {
            'id': 574, 'name': 'Pearla 3 Seater',
            'price': '279.00', 'similarity': 0.71,
        }
        r = self.client.post(
            '/api/project/42/scan/living_room/review/link_product',
            json={'bbox_id': 'bbox_0', 'product': product},
        )
        self.assertEqual(r.status_code, 200)
        rv = review.read_review(rd)
        self.assertIsNotNone(rv)
        lp = rv['bboxes']['bbox_0']['linked_product']
        self.assertEqual(lp['id'], 574)
        self.assertEqual(lp['name'], 'Pearla 3 Seater')
        # Metadata stamps.
        self.assertIn('linked_at', lp)
        self.assertEqual(lp.get('linked_by'), 'alice')

    def test_unknown_bbox_id_400(self):
        self._login()
        self._seed_run_with_image()
        r = self.client.post(
            '/api/project/42/scan/living_room/review/link_product',
            json={'bbox_id': '', 'product': {'id': 1}},
        )
        self.assertEqual(r.status_code, 400)


# ---------------------------------------------------------------------------
# 11. POST /review/link_product — accepts product=null
# ---------------------------------------------------------------------------

class TestLinkProductNoMatch(_TaxonomyRouteBase):
    def test_null_product_marks_search_attempted(self):
        self._login()
        rd = self._seed_run_with_image()
        r = self.client.post(
            '/api/project/42/scan/living_room/review/link_product',
            json={'bbox_id': 'bbox_0', 'product': None},
        )
        self.assertEqual(r.status_code, 200)
        rv = review.read_review(rd)
        self.assertIsNone(rv['bboxes']['bbox_0']['linked_product'])
        self.assertTrue(rv['bboxes']['bbox_0']['search_attempted'])
        self.assertIn('search_attempted_at', rv['bboxes']['bbox_0'])


# ---------------------------------------------------------------------------
# 12. DELETE /api/visual-search/cache — flushes the cache directory
# ---------------------------------------------------------------------------

class TestVsCacheFlush(_TaxonomyRouteBase):
    def test_flush_removes_cache_dir(self):
        self._login()
        self._seed_run_with_image()
        upstream_body = {'results': [], 'total_time': 0.1}
        with mock.patch.object(athathi_proxy, 'visual_search_full',
                               return_value=upstream_body):
            r = self.client.post(
                '/api/project/42/scan/living_room/review/find_product/0',
            )
            self.assertEqual(r.status_code, 200)

        cache_dir = os.path.join(self.tmp_auth, 'cache', 'visual_search')
        self.assertTrue(os.path.isdir(cache_dir))

        r = self.client.delete('/api/visual-search/cache')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(os.path.isdir(cache_dir))

    def test_flush_when_already_empty(self):
        self._login()
        # No cache yet → DELETE is a no-op success.
        r = self.client.delete('/api/visual-search/cache')
        self.assertEqual(r.status_code, 200)

    def test_flush_not_logged_in_returns_401(self):
        r = self.client.delete('/api/visual-search/cache')
        self.assertEqual(r.status_code, 401)

    def test_get_returns_cached_blob(self):
        self._login()
        self._seed_run_with_image()
        upstream_body = {'results': [{'id': 1}], 'total_time': 0.5}
        with mock.patch.object(athathi_proxy, 'visual_search_full',
                               return_value=upstream_body):
            self.client.post(
                '/api/project/42/scan/living_room/review/find_product/0',
            )
        # Find the sha1 from the cache filename.
        cache_dir = os.path.join(self.tmp_auth, 'cache', 'visual_search')
        cached = [f for f in os.listdir(cache_dir) if f.endswith('.json')]
        self.assertEqual(len(cached), 1)
        # BACKEND-B1: filename is `<sha1>__<tok[:8]>.json`; the URL takes
        # the bare sha1 and the route resolves the salt internally.
        salted_key = cached[0][:-5]  # strip '.json'
        bare_sha1 = salted_key.split('__', 1)[0]

        r = self.client.get(f'/api/visual-search/cache/{bare_sha1}')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        # The route returns the raw cached blob.
        self.assertEqual(body['results'][0]['id'], 1)
        # Backwards-compat: passing the full salted key also works.
        r2 = self.client.get(f'/api/visual-search/cache/{salted_key}')
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.get_json()['results'][0]['id'], 1)

    def test_get_unknown_sha_404(self):
        self._login()
        r = self.client.get('/api/visual-search/cache/deadbeef')
        self.assertEqual(r.status_code, 404)


# ---------------------------------------------------------------------------
# BACKEND-B1: visual-search cache key salted by token (per-technician)
# ---------------------------------------------------------------------------

class TestFindProductCachePerToken(_TaxonomyRouteBase):
    """Two technicians on the same Pi must NOT share visual-search cache."""

    def _new_token(self):
        # Distinct user_id so the JWT bytes are distinct → distinct
        # `tok[:8]` salt → distinct cache key.
        import secrets
        return _make_jwt({'exp': int(_time.time()) + 3600,
                          'user_id': secrets.randbits(20),
                          'username': 'tech',
                          'jti': secrets.token_hex(8)})

    def test_two_tokens_get_independent_cache(self):
        # Technician A logs in → call → cache populated.
        auth.write_token(self._new_token())
        auth.write_auth({'user_id': 7, 'username': 'alice'})
        self._seed_run_with_image()
        upstream = {'results': [{'id': 1, 'name': 'A-only'}], 'total_time': 0.1}
        with mock.patch.object(athathi_proxy, 'visual_search_full',
                               return_value=upstream) as vs:
            r = self.client.post(
                '/api/project/42/scan/living_room/review/find_product/0',
            )
            self.assertEqual(r.status_code, 200)
            self.assertEqual(vs.call_count, 1)

        # Technician B logs in (different token) → SAME image bytes →
        # the cache must be a MISS for B (upstream called again).
        auth.write_token(self._new_token())
        auth.write_auth({'user_id': 8, 'username': 'bob'})
        upstream_b = {'results': [{'id': 2, 'name': 'B-only'}],
                      'total_time': 0.2}
        with mock.patch.object(athathi_proxy, 'visual_search_full',
                               return_value=upstream_b) as vs2:
            r2 = self.client.post(
                '/api/project/42/scan/living_room/review/find_product/0',
            )
            self.assertEqual(r2.status_code, 200)
            # Cache miss → upstream invoked.
            self.assertEqual(vs2.call_count, 1)
        body_b = r2.get_json()
        self.assertFalse(body_b.get('cached'))
        # B sees its own result, not A's.
        self.assertEqual(body_b['results'][0]['id'], 2)

        # Two cache files on disk now (one per token salt).
        cache_dir = os.path.join(self.tmp_auth, 'cache', 'visual_search')
        cached = [f for f in os.listdir(cache_dir) if f.endswith('.json')]
        self.assertEqual(len(cached), 2)

    def test_token_swap_does_not_serve_prior_user_entry(self):
        # Technician A populates the cache.
        tok_a = self._new_token()
        auth.write_token(tok_a)
        auth.write_auth({'user_id': 7, 'username': 'alice'})
        self._seed_run_with_image()
        upstream = {'results': [{'id': 99, 'name': 'A-cached'}],
                    'total_time': 0.1}
        with mock.patch.object(athathi_proxy, 'visual_search_full',
                               return_value=upstream):
            r = self.client.post(
                '/api/project/42/scan/living_room/review/find_product/0',
            )
            self.assertEqual(r.status_code, 200)

        # Technician A's prior entry on disk; figure out its sha1.
        cache_dir = os.path.join(self.tmp_auth, 'cache', 'visual_search')
        files_a = sorted(os.listdir(cache_dir))
        self.assertEqual(len(files_a), 1)
        salted_a = files_a[0][:-5]
        sha1_a, _, salt_a = salted_a.partition('__')
        self.assertTrue(sha1_a)
        self.assertTrue(salt_a)

        # Swap to technician B and GET the cache via the bare-sha1 URL.
        # Resolution uses B's token → different salt → 404 (NOT served A's
        # blob).
        auth.write_token(self._new_token())
        auth.write_auth({'user_id': 8, 'username': 'bob'})
        r_get = self.client.get(f'/api/visual-search/cache/{sha1_a}')
        self.assertEqual(r_get.status_code, 404,
                         'B must NOT see A\'s prior cached entry')


class TestFindProductSkipsEmptyResultsCache(_TaxonomyRouteBase):
    """BE-5: empty / failed visual-search responses MUST NOT be cached.

    Otherwise a transient "no matches" reply locks the technician out of
    real matches until the TTL expires.
    """

    def test_empty_results_not_cached(self):
        self._login()
        self._seed_run_with_image()
        empty_body = {'results': [], 'total_time': 0.5}
        with mock.patch.object(athathi_proxy, 'visual_search_full',
                               return_value=empty_body):
            r = self.client.post(
                '/api/project/42/scan/living_room/review/find_product/0',
            )
        self.assertEqual(r.status_code, 200)
        cache_dir = os.path.join(self.tmp_auth, 'cache', 'visual_search')
        if os.path.isdir(cache_dir):
            cached = [f for f in os.listdir(cache_dir) if f.endswith('.json')]
            self.assertEqual(cached, [],
                             'empty visual-search response was cached')

    def test_upstream_error_dict_not_cached(self):
        # If the upstream returned a dict with `error`, treat as failed.
        self._login()
        self._seed_run_with_image()
        err_body = {'results': [], 'error': 'rate limited'}
        with mock.patch.object(athathi_proxy, 'visual_search_full',
                               return_value=err_body):
            self.client.post(
                '/api/project/42/scan/living_room/review/find_product/0',
            )
        cache_dir = os.path.join(self.tmp_auth, 'cache', 'visual_search')
        if os.path.isdir(cache_dir):
            cached = [f for f in os.listdir(cache_dir) if f.endswith('.json')]
            self.assertEqual(cached, [])


if __name__ == '__main__':
    unittest.main()
