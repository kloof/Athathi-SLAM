"""Unit tests for `taxonomy.py` — auto-grown class taxonomy.

Plan §7d (auto-grow taxonomy from result.json class counts), §23a
(`/api/categories/` returns 10 entries including BOTH "Chair" id 24 AND
"Chairs" id 26 — we DO NOT silently dedupe).

NO real network. Tests aggregate against an in-tmpdir `PROJECTS_ROOT`
plus a synthetic legacy `PROCESSED_DIR` so `_legacy_processed_root` is
overridden via monkey-patching.

Coverage (per the §16 step 7 brief):
  1. aggregate_local_classes returns {} when no result.json files exist.
  2. aggregate_local_classes walks both new project tree AND legacy.
  3. aggregate_local_classes ignores parse errors silently.
  4. add_learned_class strips whitespace; rejects empty / whitespace.
  5. add_learned_class increments count on repeat.
  6. merged_taxonomy with all three sources sorts by count desc.
  7. merged_taxonomy dedups case-insensitively, keeps most-common spelling.
  8. merged_taxonomy keeps "Chair" (id 24) AND "Chairs" (id 26) separate.
  9. cache_athathi_categories round-trips with load_cached_athathi_categories.
  10. load_cached_athathi_categories returns None on stale cache.
  11. load_cached_athathi_categories returns None on missing cache.
  12. merged_taxonomy `source` field has the right value for each class.
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

import auth      # noqa: E402
import projects  # noqa: E402
import taxonomy  # noqa: E402


# ---------------------------------------------------------------------------
# Base — every test gets its own ATHATHI_DIR and PROJECTS_ROOT tempdirs.
# ---------------------------------------------------------------------------

class _TaxonomyTestBase(unittest.TestCase):
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

        # Legacy processed dir is resolved inside taxonomy via
        # `_legacy_processed_root`; we override that helper for the
        # duration of each test by patching the module attribute.
        self.tmp_legacy = tempfile.mkdtemp()
        self._legacy_patcher = mock.patch(
            'taxonomy._legacy_processed_root',
            return_value=self.tmp_legacy,
        )
        self._legacy_patcher.start()

    def tearDown(self):
        self._legacy_patcher.stop()
        for k, v in self._orig_auth.items():
            setattr(auth, k, v)
        projects.PROJECTS_ROOT = self._orig_projects_root
        shutil.rmtree(self.tmp_auth, ignore_errors=True)
        shutil.rmtree(self.tmp_projects, ignore_errors=True)
        shutil.rmtree(self.tmp_legacy, ignore_errors=True)

    # ----- helpers ---------------------------------------------------

    def _seed_new_run(self, scan_id, scan_name, run_id, furniture):
        """Drop a result.json under the canonical `projects/.../runs/<id>/`."""
        rd = projects.processed_dir_for_run(scan_id, scan_name, run_id)
        os.makedirs(rd, exist_ok=True)
        with open(os.path.join(rd, 'result.json'), 'w') as f:
            json.dump({'furniture': furniture}, f)
        return rd

    def _seed_legacy_run(self, session_name, furniture):
        """Drop a result.json under the legacy `processed/<session>/`."""
        rd = os.path.join(self.tmp_legacy, session_name)
        os.makedirs(rd, exist_ok=True)
        with open(os.path.join(rd, 'result.json'), 'w') as f:
            json.dump({'furniture': furniture}, f)
        return rd


# ---------------------------------------------------------------------------
# 1. aggregate_local_classes — empty
# ---------------------------------------------------------------------------

class TestAggregateEmpty(_TaxonomyTestBase):
    def test_returns_empty_dict_when_no_result_json(self):
        self.assertEqual(taxonomy.aggregate_local_classes(), {})


# ---------------------------------------------------------------------------
# 2. aggregate_local_classes — walks both trees
# ---------------------------------------------------------------------------

class TestAggregateBothTrees(_TaxonomyTestBase):
    def test_walks_new_and_legacy_trees(self):
        # New tree: project 42 / scan living_room / run A.
        projects.ensure_project(42)
        projects.create_scan(42, 'living_room')
        self._seed_new_run(42, 'living_room', '20260425_100000',
                           [{'id': 'b0', 'class': 'sofa'},
                            {'id': 'b1', 'class': 'chair'}])
        # New tree: project 99 / scan kitchen / run X — adds another sofa.
        projects.ensure_project(99)
        projects.create_scan(99, 'kitchen')
        self._seed_new_run(99, 'kitchen', '20260425_110000',
                           [{'id': 'c0', 'class': 'sofa'}])
        # Legacy: scan_old.
        self._seed_legacy_run('scan_old',
                              [{'id': 'd0', 'class': 'chair'},
                               {'id': 'd1', 'class': 'lamp'}])

        counts = taxonomy.aggregate_local_classes()
        self.assertEqual(counts.get('sofa'), 2)
        self.assertEqual(counts.get('chair'), 2)
        self.assertEqual(counts.get('lamp'), 1)


# ---------------------------------------------------------------------------
# 3. aggregate_local_classes — silently ignores parse errors
# ---------------------------------------------------------------------------

class TestAggregateParseErrors(_TaxonomyTestBase):
    def test_ignores_corrupt_result_json(self):
        # One good, one bad.
        projects.ensure_project(42)
        projects.create_scan(42, 'a')
        rd_good = self._seed_new_run(
            42, 'a', '20260425_120000',
            [{'id': 'b0', 'class': 'sofa'}],
        )
        # Drop a corrupt result.json under another run.
        rd_bad = projects.processed_dir_for_run(42, 'a', '20260425_130000')
        os.makedirs(rd_bad, exist_ok=True)
        with open(os.path.join(rd_bad, 'result.json'), 'w') as f:
            f.write('{not valid json')

        counts = taxonomy.aggregate_local_classes()
        # The good file's count is preserved.
        self.assertEqual(counts, {'sofa': 1})

    def test_ignores_unexpected_shapes(self):
        # `furniture` not a list -> skipped.
        projects.ensure_project(42)
        projects.create_scan(42, 'a')
        rd = projects.processed_dir_for_run(42, 'a', '20260425_140000')
        os.makedirs(rd, exist_ok=True)
        with open(os.path.join(rd, 'result.json'), 'w') as f:
            json.dump({'furniture': 'not a list'}, f)
        # `class` field non-string -> skipped.
        rd2 = projects.processed_dir_for_run(42, 'a', '20260425_150000')
        os.makedirs(rd2, exist_ok=True)
        with open(os.path.join(rd2, 'result.json'), 'w') as f:
            json.dump({'furniture': [{'id': 'x', 'class': 12345}]}, f)

        self.assertEqual(taxonomy.aggregate_local_classes(), {})


# ---------------------------------------------------------------------------
# 4. add_learned_class — strip + reject blank
# ---------------------------------------------------------------------------

class TestLearnedStripsAndRejectsBlank(_TaxonomyTestBase):
    def test_strips_whitespace(self):
        taxonomy.add_learned_class('  ottoman  ')
        d = taxonomy.load_learned_classes()
        self.assertEqual(d, {'ottoman': 1})

    def test_blank_silently_rejected(self):
        taxonomy.add_learned_class('')
        taxonomy.add_learned_class('   ')
        taxonomy.add_learned_class(None)
        # No file written → load returns {}.
        self.assertEqual(taxonomy.load_learned_classes(), {})


# ---------------------------------------------------------------------------
# 5. add_learned_class — increments
# ---------------------------------------------------------------------------

class TestLearnedIncrement(_TaxonomyTestBase):
    def test_counts_accumulate(self):
        taxonomy.add_learned_class('ottoman')
        taxonomy.add_learned_class('ottoman')
        taxonomy.add_learned_class('ottoman')
        self.assertEqual(taxonomy.load_learned_classes(), {'ottoman': 3})


# ---------------------------------------------------------------------------
# 6. merged_taxonomy — sorts by count desc
# ---------------------------------------------------------------------------

class TestMergeSortByCount(_TaxonomyTestBase):
    def test_three_sources_sorted_desc(self):
        # Local: sofa x5, chair x2.
        projects.ensure_project(1)
        projects.create_scan(1, 'r')
        self._seed_new_run(1, 'r', 'A', [
            {'id': 'b0', 'class': 'sofa'},
            {'id': 'b1', 'class': 'sofa'},
            {'id': 'b2', 'class': 'sofa'},
            {'id': 'b3', 'class': 'sofa'},
            {'id': 'b4', 'class': 'sofa'},
            {'id': 'b5', 'class': 'chair'},
            {'id': 'b6', 'class': 'chair'},
        ])
        # Athathi: adds Bed (id 4) — count 0 from upstream.
        cats = [{'id': 4, 'name': 'Bed', 'description': 'a bed'}]
        # Technician: adds ottoman.
        taxonomy.add_learned_class('ottoman')

        merged = taxonomy.merged_taxonomy(cats)
        names = [d['name'] for d in merged]
        # sofa (5) first, chair (2) second, then ottoman (1) and Bed (0)
        # in some order — sorted by count desc with name asc tie-break.
        self.assertEqual(names[0], 'sofa')
        self.assertEqual(names[1], 'chair')
        self.assertIn('ottoman', names)
        self.assertIn('Bed', names)
        # ottoman count 1 should rank ahead of Bed count 0.
        self.assertLess(names.index('ottoman'), names.index('Bed'))


# ---------------------------------------------------------------------------
# 7. merged_taxonomy — case-insensitive dedup with most-common spelling
# ---------------------------------------------------------------------------

class TestMergeCaseInsensitive(_TaxonomyTestBase):
    def test_dedup_keeps_most_common_spelling(self):
        # Local sees 'chair' x5 AND 'Chair' x2. Most-common: 'chair'.
        projects.ensure_project(1)
        projects.create_scan(1, 'r')
        self._seed_new_run(1, 'r', 'A', [
            {'id': 'b0', 'class': 'chair'},
            {'id': 'b1', 'class': 'chair'},
            {'id': 'b2', 'class': 'chair'},
            {'id': 'b3', 'class': 'chair'},
            {'id': 'b4', 'class': 'chair'},
            {'id': 'b5', 'class': 'Chair'},
            {'id': 'b6', 'class': 'Chair'},
        ])
        merged = taxonomy.merged_taxonomy(None)
        # Both spellings collapse into ONE entry.
        chair_entries = [d for d in merged
                         if d['name'].lower() == 'chair']
        self.assertEqual(len(chair_entries), 1)
        self.assertEqual(chair_entries[0]['name'], 'chair')
        self.assertEqual(chair_entries[0]['count'], 7)


# ---------------------------------------------------------------------------
# 8. merged_taxonomy — Chair (24) vs Chairs (26)
# ---------------------------------------------------------------------------

class TestMergeChairVsChairs(_TaxonomyTestBase):
    def test_chair_and_chairs_remain_separate(self):
        cats = [
            {'id': 24, 'name': 'Chair'},
            {'id': 26, 'name': 'Chairs'},
        ]
        merged = taxonomy.merged_taxonomy(cats)
        names = {d['name'] for d in merged}
        self.assertIn('Chair', names)
        self.assertIn('Chairs', names)
        # And both have their respective Athathi ids preserved.
        chair = next(d for d in merged if d['name'] == 'Chair')
        chairs = next(d for d in merged if d['name'] == 'Chairs')
        self.assertEqual(chair['athathi_id'], 24)
        self.assertEqual(chairs['athathi_id'], 26)


# ---------------------------------------------------------------------------
# 9. cache_athathi_categories round-trip
# ---------------------------------------------------------------------------

class TestCacheRoundTrip(_TaxonomyTestBase):
    def test_round_trip_within_ttl(self):
        cats = [{'id': 1, 'name': 'Sofa'}, {'id': 2, 'name': 'Bed'}]
        taxonomy.cache_athathi_categories(cats)
        loaded = taxonomy.load_cached_athathi_categories(max_age_s=3600)
        self.assertEqual(loaded, cats)


# ---------------------------------------------------------------------------
# 10. load_cached_athathi_categories — stale
# ---------------------------------------------------------------------------

class TestCacheStale(_TaxonomyTestBase):
    def test_stale_returns_none(self):
        cats = [{'id': 1, 'name': 'Sofa'}]
        taxonomy.cache_athathi_categories(cats)
        # Walk back the on-disk timestamp by 2 hours so the freshness gate
        # rejects it.
        path = os.path.join(self.tmp_auth, taxonomy.TAXONOMY_CACHE_NAME)
        with open(path, 'r') as f:
            data = json.load(f)
        data['cached_at'] = _time.time() - 7200
        with open(path, 'w') as f:
            json.dump(data, f)
        # max_age_s=3600 → 7200 s old is stale.
        self.assertIsNone(
            taxonomy.load_cached_athathi_categories(max_age_s=3600))


# ---------------------------------------------------------------------------
# 11. load_cached_athathi_categories — missing
# ---------------------------------------------------------------------------

class TestCacheMissing(_TaxonomyTestBase):
    def test_missing_returns_none(self):
        # Nothing on disk yet.
        self.assertIsNone(
            taxonomy.load_cached_athathi_categories(max_age_s=3600))


# ---------------------------------------------------------------------------
# 12. merged_taxonomy — `source` field correctness per class
# ---------------------------------------------------------------------------

class TestMergeSourceField(_TaxonomyTestBase):
    def test_source_per_class(self):
        # Local sees `sofa` x3.
        projects.ensure_project(1)
        projects.create_scan(1, 'r')
        self._seed_new_run(1, 'r', 'A', [
            {'id': 'b0', 'class': 'sofa'},
            {'id': 'b1', 'class': 'sofa'},
            {'id': 'b2', 'class': 'sofa'},
        ])
        # Athathi: Bed only-from-upstream.
        cats = [{'id': 4, 'name': 'Bed'}]
        # Technician: ottoman only-from-technician.
        taxonomy.add_learned_class('ottoman')

        merged = taxonomy.merged_taxonomy(cats)
        by_name = {d['name']: d for d in merged}
        self.assertEqual(by_name['sofa']['source'], 'model')
        self.assertEqual(by_name['Bed']['source'], 'athathi')
        self.assertEqual(by_name['ottoman']['source'], 'technician')

    def test_local_overrides_source_when_athathi_also_has_it(self):
        # Both local and Athathi contribute 'Sofa'/'sofa'. The merged
        # source must reflect the model contribution (3 > 0 > 0).
        projects.ensure_project(1)
        projects.create_scan(1, 'r')
        self._seed_new_run(1, 'r', 'A', [
            {'id': 'b0', 'class': 'sofa'},
            {'id': 'b1', 'class': 'sofa'},
            {'id': 'b2', 'class': 'sofa'},
        ])
        cats = [{'id': 1, 'name': 'Sofa'}]
        merged = taxonomy.merged_taxonomy(cats)
        sofa = next(d for d in merged if d['name'].lower() == 'sofa')
        # The model contribution beats Athathi for the source field.
        self.assertEqual(sofa['source'], 'model')


if __name__ == '__main__':
    unittest.main()
