"""Tests for the Step 4 additions to `projects.py` (plan §22d).

Style mirrors `tests/test_projects.py`: per-test PROJECTS_ROOT under a
fresh tempdir, no real /mnt/slam_data.

Coverage (per the §16 step 4 brief):
  1. create_scan rejects bad names.
  2. create_scan creates rosbag/ + processed/ subdirectories.
  3. create_scan raises FileExistsError on second call.
  4. delete_scan removes the directory tree.
  5. new_run_id produces distinct ids; allocate_run_id collision-resolves
     with `_2` suffix.
  6. read_active_run / set_active_run round-trip; the write is atomic.
  7. processed_dir_for_run returns the right path.
  8. _validate_scan_name accepts the easy positives.
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone

# Make the repo importable regardless of where pytest is invoked from.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import projects  # noqa: E402


# ---------------------------------------------------------------------------
# Base — every test points projects.PROJECTS_ROOT at a fresh tempdir.
# ---------------------------------------------------------------------------

class _ScanHelperBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_root = projects.PROJECTS_ROOT
        projects.PROJECTS_ROOT = self.tmp
        # Pre-create the project so create_scan can drop scans under it.
        projects.ensure_project(42, athathi_meta={'customer_name': 'Smith'})

    def tearDown(self):
        projects.PROJECTS_ROOT = self._orig_root
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1. create_scan: bad names
# ---------------------------------------------------------------------------

class TestCreateScanBadNames(_ScanHelperBase):
    def test_rejects_uppercase_with_space(self):
        with self.assertRaises(ValueError):
            projects.create_scan(42, 'Living Room')

    def test_rejects_dotdot(self):
        with self.assertRaises(ValueError):
            projects.create_scan(42, '..')

    def test_rejects_reserved_runs(self):
        with self.assertRaises(ValueError):
            projects.create_scan(42, 'runs')

    def test_rejects_reserved_processed(self):
        with self.assertRaises(ValueError):
            projects.create_scan(42, 'processed')

    def test_rejects_reserved_rosbag(self):
        with self.assertRaises(ValueError):
            projects.create_scan(42, 'rosbag')

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            projects.create_scan(42, '')

    def test_rejects_too_long(self):
        with self.assertRaises(ValueError):
            projects.create_scan(42, 'a' * 41)

    def test_rejects_leading_underscore(self):
        with self.assertRaises(ValueError):
            projects.create_scan(42, '_hidden')

    def test_rejects_dash(self):
        with self.assertRaises(ValueError):
            projects.create_scan(42, 'living-room')

    def test_rejects_non_string(self):
        with self.assertRaises(ValueError):
            projects.create_scan(42, 12345)


# ---------------------------------------------------------------------------
# 2. create_scan: layout produced
# ---------------------------------------------------------------------------

class TestCreateScanLayout(_ScanHelperBase):
    def test_creates_rosbag_and_processed(self):
        summary = projects.create_scan(42, 'living_room')

        self.assertEqual(summary['name'], 'living_room')
        self.assertFalse(summary['has_rosbag'])
        self.assertIsNone(summary['active_run_id'])

        sd = projects.scan_dir(42, 'living_room')
        self.assertTrue(os.path.isdir(sd))
        self.assertTrue(os.path.isdir(projects.scan_rosbag_dir(42, 'living_room')))
        self.assertTrue(os.path.isdir(projects.scan_processed_root(42, 'living_room')))

    def test_accepts_digits_and_underscores(self):
        summary = projects.create_scan(42, 'kitchen_2')
        self.assertEqual(summary['name'], 'kitchen_2')


# ---------------------------------------------------------------------------
# 3. create_scan: duplicate
# ---------------------------------------------------------------------------

class TestCreateScanDuplicate(_ScanHelperBase):
    def test_second_call_raises_file_exists(self):
        projects.create_scan(42, 'living_room')
        with self.assertRaises(FileExistsError):
            projects.create_scan(42, 'living_room')


# ---------------------------------------------------------------------------
# 4. delete_scan
# ---------------------------------------------------------------------------

class TestDeleteScan(_ScanHelperBase):
    def test_removes_directory_tree(self):
        projects.create_scan(42, 'living_room')
        sd = projects.scan_dir(42, 'living_room')
        # Drop a file inside to prove we recursively delete.
        with open(os.path.join(sd, 'rosbag', 'fake.mcap'), 'wb') as f:
            f.write(b'x' * 16)

        projects.delete_scan(42, 'living_room')
        self.assertFalse(os.path.exists(sd))

    def test_idempotent_on_missing(self):
        # Should be a no-op, not an error.
        projects.delete_scan(42, 'nonexistent')

    def test_rejects_bad_name(self):
        # Even on delete we validate so a stray '..' can't escape.
        with self.assertRaises(ValueError):
            projects.delete_scan(42, '..')


# ---------------------------------------------------------------------------
# 5. new_run_id / allocate_run_id
# ---------------------------------------------------------------------------

class TestRunIds(_ScanHelperBase):
    def test_new_run_id_format(self):
        rid = projects.new_run_id(now=datetime(2026, 4, 25, 14, 21, 3, tzinfo=timezone.utc))
        self.assertEqual(rid, '20260425_142103')

    def test_allocate_run_id_collision_resolves(self):
        projects.create_scan(42, 'living_room')
        # Pre-create a runs/<base>/ directory.
        base_now = datetime(2026, 4, 25, 14, 21, 3, tzinfo=timezone.utc)
        base = projects.new_run_id(now=base_now)
        rdir = projects.runs_dir(42, 'living_room')
        os.makedirs(os.path.join(rdir, base))

        # First allocation should bump to _2.
        chosen = projects.allocate_run_id(42, 'living_room', now=base_now)
        self.assertEqual(chosen, f'{base}_2')

        # Now create _2 too — next should be _3.
        os.makedirs(os.path.join(rdir, f'{base}_2'))
        chosen2 = projects.allocate_run_id(42, 'living_room', now=base_now)
        self.assertEqual(chosen2, f'{base}_3')

    def test_allocate_run_id_first_call_returns_base(self):
        projects.create_scan(42, 'living_room')
        base_now = datetime(2026, 4, 25, 14, 21, 3, tzinfo=timezone.utc)
        chosen = projects.allocate_run_id(42, 'living_room', now=base_now)
        self.assertEqual(chosen, '20260425_142103')


# ---------------------------------------------------------------------------
# 6. active_run round-trip
# ---------------------------------------------------------------------------

class TestActiveRun(_ScanHelperBase):
    def test_read_returns_none_when_missing(self):
        projects.create_scan(42, 'living_room')
        self.assertIsNone(projects.read_active_run(42, 'living_room'))

    def test_round_trip(self):
        projects.create_scan(42, 'living_room')
        projects.set_active_run(42, 'living_room', '20260425_142103')
        self.assertEqual(
            projects.read_active_run(42, 'living_room'),
            '20260425_142103',
        )

    def test_atomic_rewrite(self):
        projects.create_scan(42, 'living_room')
        projects.set_active_run(42, 'living_room', 'r1')
        projects.set_active_run(42, 'living_room', 'r2')
        # Final state reflects the second write; no partial / .tmp leftovers.
        self.assertEqual(projects.read_active_run(42, 'living_room'), 'r2')
        # No stray temp files in the parent dir.
        parent = os.path.dirname(projects.active_run_path(42, 'living_room'))
        leftovers = [n for n in os.listdir(parent) if n.startswith('.tmp.')]
        self.assertEqual(leftovers, [])

    def test_set_rejects_empty(self):
        projects.create_scan(42, 'living_room')
        with self.assertRaises(ValueError):
            projects.set_active_run(42, 'living_room', '')


# ---------------------------------------------------------------------------
# 7. processed_dir_for_run
# ---------------------------------------------------------------------------

class TestProcessedDirForRun(_ScanHelperBase):
    def test_path_shape(self):
        projects.create_scan(42, 'living_room')
        path = projects.processed_dir_for_run(42, 'living_room', '20260425_142103')
        expected_parts = (
            self.tmp, '42', 'scans', 'living_room',
            'processed', 'runs', '20260425_142103',
        )
        self.assertEqual(path, os.path.join(*expected_parts))

    def test_rejects_empty_run_id(self):
        with self.assertRaises(ValueError):
            projects.processed_dir_for_run(42, 'living_room', '')


if __name__ == '__main__':
    unittest.main()
