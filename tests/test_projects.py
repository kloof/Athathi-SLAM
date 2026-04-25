"""Tests for `projects.py` — per-project filesystem + manifest helpers.

Style mirrors `tests/test_athathi_proxy.py`: every test patches
`projects.PROJECTS_ROOT` to a fresh `tempfile.TemporaryDirectory` so disk
state is per-test. NO real network calls, NO real `/mnt/slam_data`.

Coverage (per the §16 step 3 brief):
  1. PROJECTS_ROOT falls back to <SCRIPT_DIR>/projects when /mnt/slam_data
     is not a mount.
  2. project_dir(42) returns the correct path; doesn't create.
  3. read_manifest() returns None for missing project; None for unparseable.
  4. write_manifest() is atomic (existing manifest survives a sibling write).
  5. ensure_project(42, {}) creates project dir, scans/, settings.json,
     manifest with sensible defaults.
  6. ensure_project is idempotent — second call doesn't clobber technician-set
     fields like customer_name if the first call set it from athathi_meta.
  7. field_extract finds value via multiple candidate names; returns None
     when none match.
  8. list_projects returns descending by scan_id; computes rooms_local.
  9. list_scans returns the right shape for a project with two scans.
  10. field_extract ignores empty strings (treats "" as "not present").
"""

import importlib
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

import projects  # noqa: E402


# ---------------------------------------------------------------------------
# Base — every test points projects.PROJECTS_ROOT at a fresh tempdir.
# ---------------------------------------------------------------------------

class _ProjectsTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_root = projects.PROJECTS_ROOT
        projects.PROJECTS_ROOT = self.tmp

    def tearDown(self):
        projects.PROJECTS_ROOT = self._orig_root
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1. PROJECTS_ROOT resolution fallback
# ---------------------------------------------------------------------------

class TestProjectsRootResolution(unittest.TestCase):
    def test_falls_back_to_repo_when_mount_absent(self):
        # Force the resolver to NOT see /mnt/slam_data and NO override.
        with mock.patch.object(projects.os.path, 'isdir',
                               side_effect=lambda p: False), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PROJECTS_ROOT_OVERRIDE', None)
            resolved = projects._resolve_projects_root()
        # Should land under the repo dir.
        self.assertTrue(resolved.endswith(os.sep + 'projects'),
                        f'unexpected fallback path: {resolved}')
        self.assertIn(projects._SCRIPT_DIR, resolved)

    def test_uses_mount_when_available(self):
        # /mnt/slam_data exists as a directory.
        def _fake_isdir(p):
            return p == '/mnt/slam_data'
        with mock.patch.object(projects.os.path, 'isdir',
                               side_effect=_fake_isdir), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PROJECTS_ROOT_OVERRIDE', None)
            resolved = projects._resolve_projects_root()
        self.assertEqual(resolved, '/mnt/slam_data/projects')

    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {'PROJECTS_ROOT_OVERRIDE': '/x/y/z'}):
            resolved = projects._resolve_projects_root()
        self.assertEqual(resolved, '/x/y/z')


# ---------------------------------------------------------------------------
# 2. project_dir() pathing
# ---------------------------------------------------------------------------

class TestProjectDir(_ProjectsTestBase):
    def test_returns_correct_path_no_create(self):
        p = projects.project_dir(42)
        self.assertEqual(p, os.path.join(self.tmp, '42'))
        # The function must NOT create the directory.
        self.assertFalse(os.path.exists(p))

    def test_accepts_int_or_string_id(self):
        self.assertEqual(projects.project_dir(42), os.path.join(self.tmp, '42'))
        self.assertEqual(projects.project_dir('42'), os.path.join(self.tmp, '42'))

    def test_scan_dir_pathing(self):
        sd = projects.scan_dir(42, 'living_room')
        self.assertEqual(sd, os.path.join(self.tmp, '42', 'scans', 'living_room'))
        self.assertFalse(os.path.exists(sd))


# ---------------------------------------------------------------------------
# 3. read_manifest() — missing / unparseable
# ---------------------------------------------------------------------------

class TestReadManifest(_ProjectsTestBase):
    def test_missing_project_returns_none(self):
        self.assertIsNone(projects.read_manifest(99))

    def test_unparseable_returns_none(self):
        # Create the dir + a corrupt manifest.
        os.makedirs(projects.project_dir(7))
        with open(projects.manifest_path(7), 'w') as f:
            f.write('this is not json {{{')
        self.assertIsNone(projects.read_manifest(7))


# ---------------------------------------------------------------------------
# 4. write_manifest atomicity
# ---------------------------------------------------------------------------

class TestWriteManifest(_ProjectsTestBase):
    def test_atomic_write_does_not_clobber_sibling(self):
        # Two adjacent manifests.
        projects.write_manifest(1, {'scan_id': 1, 'tag': 'first'})
        projects.write_manifest(2, {'scan_id': 2, 'tag': 'second'})

        # Re-write 2 with new content.
        projects.write_manifest(2, {'scan_id': 2, 'tag': 'second_v2'})

        # 1 must be intact.
        m1 = projects.read_manifest(1)
        self.assertEqual(m1['tag'], 'first')
        m2 = projects.read_manifest(2)
        self.assertEqual(m2['tag'], 'second_v2')

        # No leftover temp files.
        for d in (projects.project_dir(1), projects.project_dir(2)):
            for entry in os.listdir(d):
                self.assertFalse(entry.startswith('.tmp.'),
                                 f'leftover tempfile: {entry}')

    def test_write_creates_parent_dir(self):
        # Project dir doesn't exist yet.
        self.assertFalse(os.path.exists(projects.project_dir(99)))
        projects.write_manifest(99, {'scan_id': 99})
        self.assertTrue(os.path.isfile(projects.manifest_path(99)))


# ---------------------------------------------------------------------------
# 5. ensure_project on first call
# ---------------------------------------------------------------------------

class TestEnsureProjectFirstCall(_ProjectsTestBase):
    def test_creates_dirs_and_manifest_with_defaults(self):
        manifest = projects.ensure_project(42, athathi_meta={})

        self.assertTrue(os.path.isdir(projects.project_dir(42)))
        self.assertTrue(os.path.isdir(os.path.join(projects.project_dir(42), 'scans')))
        self.assertTrue(os.path.isfile(projects.project_settings_path(42)))
        self.assertTrue(os.path.isfile(projects.manifest_path(42)))

        # Settings is empty {}.
        with open(projects.project_settings_path(42), 'r') as f:
            self.assertEqual(json.load(f), {})

        # Manifest has the schema keys.
        for key in ('scan_id', 'customer_name', 'slot_start', 'slot_end',
                    'address', 'athathi_meta', 'created_at', 'completed_at',
                    'submitted_at', 'post_submit_hook_status'):
            self.assertIn(key, manifest, f'missing manifest key: {key}')

        self.assertEqual(manifest['scan_id'], 42)
        self.assertIsNone(manifest['customer_name'])
        self.assertIsNone(manifest['completed_at'])
        self.assertIsNone(manifest['submitted_at'])
        self.assertEqual(manifest['athathi_meta'], {})
        self.assertIsInstance(manifest['created_at'], str)

    def test_extracts_named_fields_from_athathi_meta(self):
        item = {
            'scan_id': 42,
            'customer_name': 'Smith Family',
            'slot_start': '2026-04-25T14:00:00Z',
            'slot_end':   '2026-04-25T16:00:00Z',
            'address':    '123 Maple St',
            'mystery_field': 'preserved verbatim',
        }
        m = projects.ensure_project(42, athathi_meta=item)

        self.assertEqual(m['customer_name'], 'Smith Family')
        self.assertEqual(m['slot_start'], '2026-04-25T14:00:00Z')
        self.assertEqual(m['slot_end'], '2026-04-25T16:00:00Z')
        self.assertEqual(m['address'], '123 Maple St')
        # Unknown fields preserved verbatim in athathi_meta.
        self.assertEqual(m['athathi_meta']['mystery_field'], 'preserved verbatim')
        self.assertEqual(m['athathi_meta']['scan_id'], 42)

    def test_camel_case_aliases_extracted(self):
        item = {
            'scanId': 42,
            'customerName': 'Smith',
            'slotStart': 'a',
            'slotEnd':   'b',
            'location':  'somewhere',
        }
        m = projects.ensure_project(42, athathi_meta=item)
        self.assertEqual(m['customer_name'], 'Smith')
        self.assertEqual(m['slot_start'], 'a')
        self.assertEqual(m['slot_end'], 'b')
        self.assertEqual(m['address'], 'somewhere')


# ---------------------------------------------------------------------------
# 6. ensure_project idempotence
# ---------------------------------------------------------------------------

class TestEnsureProjectIdempotence(_ProjectsTestBase):
    def test_does_not_clobber_technician_fields(self):
        # First call: extract from athathi.
        m1 = projects.ensure_project(42, athathi_meta={
            'customer_name': 'Original',
            'slot_start': 'A',
        })
        self.assertEqual(m1['customer_name'], 'Original')

        # Simulate technician edit on disk: rename customer to something else.
        m1_disk = projects.read_manifest(42)
        m1_disk['customer_name'] = 'Technician Override'
        projects.write_manifest(42, m1_disk)

        # Second ensure call with NEW athathi_meta — must NOT clobber.
        m2 = projects.ensure_project(42, athathi_meta={
            'customer_name': 'Server Says This',
            'slot_start': 'B',
        })
        self.assertEqual(m2['customer_name'], 'Technician Override')
        # slot_start was set on first call so also kept.
        self.assertEqual(m2['slot_start'], 'A')

    def test_second_call_no_meta_preserves_first_call(self):
        m1 = projects.ensure_project(42, athathi_meta={'customer_name': 'X'})
        m2 = projects.ensure_project(42)  # no meta
        self.assertEqual(m2['customer_name'], 'X')

    def test_settings_not_clobbered_on_second_call(self):
        projects.ensure_project(42, athathi_meta={})
        # User stashes something in settings.
        with open(projects.project_settings_path(42), 'w') as f:
            json.dump({'pref': 'dark'}, f)
        # Second ensure call.
        projects.ensure_project(42, athathi_meta={'name': 'X'})
        with open(projects.project_settings_path(42), 'r') as f:
            settings = json.load(f)
        self.assertEqual(settings, {'pref': 'dark'})

    def test_athathi_meta_merge_adds_new_keys(self):
        projects.ensure_project(42, athathi_meta={'a': 1, 'b': 2})
        m = projects.ensure_project(42, athathi_meta={'b': 99, 'c': 3})
        # 'a' preserved, 'b' kept (already set, not blank), 'c' added.
        self.assertEqual(m['athathi_meta']['a'], 1)
        self.assertEqual(m['athathi_meta']['b'], 2)
        self.assertEqual(m['athathi_meta']['c'], 3)


# ---------------------------------------------------------------------------
# 7 + 10. field_extract
# ---------------------------------------------------------------------------

class TestFieldExtract(unittest.TestCase):
    def test_finds_first_present_candidate(self):
        item = {'b': 'second'}
        self.assertEqual(projects.field_extract(item, 'a', 'b', 'c'), 'second')

    def test_returns_none_when_no_match(self):
        item = {'x': 1}
        self.assertIsNone(projects.field_extract(item, 'a', 'b', 'c'))

    def test_first_match_wins(self):
        item = {'a': 'first', 'b': 'second'}
        self.assertEqual(projects.field_extract(item, 'a', 'b'), 'first')
        # And reverse order.
        self.assertEqual(projects.field_extract(item, 'b', 'a'), 'second')

    def test_empty_string_treated_as_absent(self):
        item = {'a': '', 'b': 'real'}
        # 'a' is empty so should fall through to 'b'.
        self.assertEqual(projects.field_extract(item, 'a', 'b'), 'real')

    def test_whitespace_only_treated_as_absent(self):
        item = {'a': '   ', 'b': 'real'}
        self.assertEqual(projects.field_extract(item, 'a', 'b'), 'real')

    def test_none_value_treated_as_absent(self):
        item = {'a': None, 'b': 'real'}
        self.assertEqual(projects.field_extract(item, 'a', 'b'), 'real')

    def test_zero_is_a_real_value(self):
        item = {'a': 0}
        self.assertEqual(projects.field_extract(item, 'a'), 0)

    def test_non_dict_returns_none(self):
        self.assertIsNone(projects.field_extract(None, 'a'))
        self.assertIsNone(projects.field_extract([], 'a'))


# ---------------------------------------------------------------------------
# 8. list_projects ordering + computed fields
# ---------------------------------------------------------------------------

class TestListProjects(_ProjectsTestBase):
    def test_descending_by_scan_id(self):
        for sid in (1, 5, 3, 10, 7):
            projects.ensure_project(sid, athathi_meta={})

        out = projects.list_projects()
        ids = [m['scan_id'] for m in out]
        self.assertEqual(ids, [10, 7, 5, 3, 1])

    def test_rooms_local_count(self):
        projects.ensure_project(42, athathi_meta={})
        # Make two scan dirs.
        os.makedirs(projects.scan_dir(42, 'living_room'))
        os.makedirs(projects.scan_dir(42, 'bedroom'))

        out = projects.list_projects()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['rooms_local'], 2)
        self.assertEqual(out[0]['rooms_reviewed'], 0)
        self.assertFalse(out[0]['submitted'])

    def test_submitted_flag_reflects_manifest(self):
        projects.ensure_project(42, athathi_meta={})
        m = projects.read_manifest(42)
        m['submitted_at'] = '2026-04-25T20:00:00Z'
        projects.write_manifest(42, m)

        out = projects.list_projects()
        self.assertTrue(out[0]['submitted'])

    def test_skips_unparseable_manifest(self):
        # A directory that looks like a project but has bad json.
        os.makedirs(projects.project_dir(1))
        with open(projects.manifest_path(1), 'w') as f:
            f.write('garbage')
        # A real project.
        projects.ensure_project(2, athathi_meta={})

        out = projects.list_projects()
        ids = [m['scan_id'] for m in out]
        self.assertEqual(ids, [2])

    def test_skips_non_integer_dir_names(self):
        os.makedirs(os.path.join(self.tmp, 'README'))
        projects.ensure_project(42, athathi_meta={})
        out = projects.list_projects()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['scan_id'], 42)

    def test_empty_root_returns_empty_list(self):
        out = projects.list_projects()
        self.assertEqual(out, [])

    def test_missing_root_returns_empty_list(self):
        # Point at a non-existent dir.
        projects.PROJECTS_ROOT = os.path.join(self.tmp, 'does_not_exist')
        out = projects.list_projects()
        self.assertEqual(out, [])


# ---------------------------------------------------------------------------
# 9. list_scans shape
# ---------------------------------------------------------------------------

class TestListScans(_ProjectsTestBase):
    def test_two_scan_project_shape(self):
        projects.ensure_project(42, athathi_meta={})
        # living_room: rosbag with one mcap file → has_rosbag True.
        rb = os.path.join(projects.scan_dir(42, 'living_room'), 'rosbag')
        os.makedirs(rb)
        with open(os.path.join(rb, 'rosbag_0.mcap'), 'w') as f:
            f.write('ignored')
        # bedroom: NO rosbag dir → has_rosbag False.
        os.makedirs(projects.scan_dir(42, 'bedroom'))

        out = projects.list_scans(42)
        names = [s['name'] for s in out]
        self.assertEqual(sorted(names), ['bedroom', 'living_room'])

        by_name = {s['name']: s for s in out}
        self.assertTrue(by_name['living_room']['has_rosbag'])
        self.assertFalse(by_name['bedroom']['has_rosbag'])

        # Common shape on every entry.
        for s in out:
            self.assertIn('name', s)
            self.assertIn('has_rosbag', s)
            self.assertIn('active_run_id', s)
            self.assertIn('reviewed', s)
            # active_run_id starts None until step 5 sets it.
            self.assertIsNone(s['active_run_id'])
            # reviewed always False at step 3.
            self.assertFalse(s['reviewed'])

    def test_active_run_id_read_from_active_run_json(self):
        projects.ensure_project(42, athathi_meta={})
        proc = os.path.join(projects.scan_dir(42, 'living_room'), 'processed')
        os.makedirs(proc)
        with open(os.path.join(proc, 'active_run.json'), 'w') as f:
            json.dump({'active_run_id': '20260425_142103'}, f)

        out = projects.list_scans(42)
        self.assertEqual(out[0]['active_run_id'], '20260425_142103')

    def test_corrupt_active_run_json_is_none(self):
        projects.ensure_project(42, athathi_meta={})
        proc = os.path.join(projects.scan_dir(42, 'living_room'), 'processed')
        os.makedirs(proc)
        with open(os.path.join(proc, 'active_run.json'), 'w') as f:
            f.write('not json')

        out = projects.list_scans(42)
        self.assertIsNone(out[0]['active_run_id'])

    def test_no_scans_dir_returns_empty(self):
        # Project exists, but no scans/ subdir.
        os.makedirs(projects.project_dir(42))
        # Don't create scans/.
        out = projects.list_scans(42)
        self.assertEqual(out, [])

    def test_hidden_dirs_skipped(self):
        projects.ensure_project(42, athathi_meta={})
        os.makedirs(projects.scan_dir(42, 'living_room'))
        os.makedirs(projects.scan_dir(42, '.git'))
        names = [s['name'] for s in projects.list_scans(42)]
        self.assertEqual(names, ['living_room'])


if __name__ == '__main__':
    unittest.main()
