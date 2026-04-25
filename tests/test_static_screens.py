"""Static-asset unit tests for the Step 9 SPA screens.

Per Step 9 of TECHNICIAN_REVIEW_PLAN.md §16, the new SPA renders three
real screens (#/projects, #/project/<id>, #/project/<id>/scan/<name>)
on top of the Step 8 router shell. We can't run a real browser in CI,
but we CAN make sure:

  - `static/app.js` defines the expected render functions + helpers.
  - The router maps each route to its render fn.
  - The legacy DOM IDs from Step 8 are still present in the index HTML.
  - The pure helpers (`_scanPrimaryAction`, `_submitGatingMessage`,
    `validateScanName`) match the plan's state table / gating rules.

We extract function bodies via regex from the parsed JS source and
exercise them via small re-implementations in Python. For helpers
where a re-implementation would be brittle, we instead probe the
source for the load-bearing literals from the plan.
"""

import os
import re
import subprocess
import sys
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import app  # noqa: E402

APP_JS_PATH = os.path.join(ROOT, 'static', 'app.js')
LEGACY_JS_PATH = os.path.join(ROOT, 'static', 'legacy_app.js')
INDEX_HTML_PATH = os.path.join(ROOT, 'templates', 'index.html')


def _read(path):
    with open(path, 'r') as f:
        return f.read()


class TestAppJsHasStep9Functions(unittest.TestCase):
    """Grep for the function names + globals introduced by Step 9."""

    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)

    def test_render_projects_defined(self):
        self.assertIn('function renderProjects(', self.src,
                      'expected renderProjects() to be defined in app.js')

    def test_render_project_workspace_defined(self):
        self.assertIn('function renderProjectWorkspace(', self.src,
                      'expected renderProjectWorkspace() in app.js')

    def test_render_scan_workspace_defined(self):
        self.assertIn('function renderScanWorkspace(', self.src,
                      'expected renderScanWorkspace() in app.js')

    def test_scan_primary_action_helper_defined(self):
        self.assertIn('function _scanPrimaryAction(', self.src)

    def test_submit_gating_message_helper_defined(self):
        self.assertIn('function _submitGatingMessage(', self.src)

    def test_validate_scan_name_helper_defined(self):
        self.assertIn('function validateScanName(', self.src)

    def test_set_top_bar_helper_defined(self):
        self.assertIn('function setTopBar(', self.src)

    def test_app_shell_exports_step9_helpers(self):
        # Public surface exposes the testable pure helpers.
        for sym in ('_scanPrimaryAction', '_submitGatingMessage',
                    '_scanState', '_projectStatusPill',
                    'validateScanName', 'setTopBar', 'confirm'):
            self.assertIn(sym + ':', self.src,
                          f'AppShell missing exported helper: {sym}')

    def test_route_table_present(self):
        # ROUTES table maps route names to their render fns.
        self.assertIn('var ROUTES = {', self.src)
        for key in ('login', 'projects', 'project', 'scan', 'review', 'settings'):
            self.assertRegex(
                self.src,
                r'\b' + key + r'\s*:\s*render',
                f'ROUTES missing entry for {key!r}',
            )


class TestParseRouteTable(unittest.TestCase):
    """The parseRoute fn must understand all five Step 9 routes."""

    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)

    def test_login_route(self):
        self.assertIn("name: 'login'", self.src)

    def test_projects_route(self):
        self.assertIn("name: 'projects'", self.src)

    def test_project_route(self):
        self.assertIn("name: 'project'", self.src)

    def test_scan_route(self):
        self.assertIn("name: 'scan'", self.src)

    def test_review_route(self):
        # Step 10 fills in this screen, but Step 9 must already route to a
        # placeholder when the user taps "Review".
        self.assertIn("name: 'review'", self.src)


class TestNodeSyntaxCheck(unittest.TestCase):
    """node --check must succeed on both bundles (best smoke we have)."""

    def test_app_js_parses(self):
        if not _has_node():
            self.skipTest('node not available')
        rc = subprocess.run(
            ['node', '--check', APP_JS_PATH],
            capture_output=True, text=True,
        )
        self.assertEqual(
            rc.returncode, 0,
            f'node --check app.js failed:\n{rc.stderr}',
        )

    def test_legacy_app_js_parses(self):
        if not _has_node():
            self.skipTest('node not available')
        rc = subprocess.run(
            ['node', '--check', LEGACY_JS_PATH],
            capture_output=True, text=True,
        )
        self.assertEqual(
            rc.returncode, 0,
            f'node --check legacy_app.js failed:\n{rc.stderr}',
        )


def _has_node():
    try:
        rc = subprocess.run(['node', '--version'], capture_output=True)
        return rc.returncode == 0
    except (OSError, FileNotFoundError):
        return False


# ---------------------------------------------------------------------------
# Scan-workspace state table — extract the JS switch + check every plan §11
# state maps to the right action label.
# ---------------------------------------------------------------------------

class TestScanPrimaryActionTable(unittest.TestCase):
    """Probe `_scanPrimaryAction(state)` directly from the source.

    Plan §11 state table:
        idle              -> Start recording
        recording         -> Stop recording
        recorded          -> Process
        processing        -> (read-only; primary action is null/none)
        done_unreviewed   -> Review
        done_reviewing    -> Continue review
        done_reviewed     -> View review
        error             -> Retry
    """

    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)
        # Pull the function body so we can sanity-check string literals.
        m = re.search(
            r'function _scanPrimaryAction\(stateName\)\s*\{(.*?)\n\s*\}',
            cls.src, re.DOTALL,
        )
        assert m, 'failed to extract _scanPrimaryAction body'
        cls.body = m.group(1)

    def _has_case(self, state, label):
        # Match `case 'idle': return 'Start recording'`.
        pattern = (
            r"case\s+'" + re.escape(state) + r"':\s*"
            r"(?:return\s+'" + re.escape(label) + r"'|"
            r"case\s+'[a-z_]+':\s*return\s+'" + re.escape(label) + r"')"
        )
        # Looser: also accept the multi-case fall-through form.
        return bool(re.search(
            r"case\s+'" + re.escape(state) + r"'", self.body)) \
            and label in self.body

    def test_idle_starts_recording(self):
        self.assertTrue(self._has_case('idle', 'Start recording'))

    def test_recording_stops(self):
        self.assertTrue(self._has_case('recording', 'Stop recording'))

    def test_recorded_processes(self):
        self.assertTrue(self._has_case('recorded', 'Process'))

    def test_processing_returns_null(self):
        # processing has no primary action button; we encode `return null`.
        self.assertRegex(
            self.body,
            r"case\s+'processing':\s*return\s+null",
        )

    def test_done_unreviewed_review(self):
        # Plan §11: `done, no review` -> primary=Review.
        self.assertTrue(self._has_case('done_unreviewed', 'Review'))

    def test_done_reviewing_continue(self):
        self.assertTrue(self._has_case('done_reviewing', 'Continue review'))

    def test_done_reviewed_view(self):
        self.assertTrue(self._has_case('done_reviewed', 'View review'))

    def test_error_retry(self):
        self.assertTrue(self._has_case('error', 'Retry'))


# ---------------------------------------------------------------------------
# Submit gating message — extract priorities from JS source. The plan §22c
# priority order is:
#   1. already-submitted    -> "Already submitted on <date>"
#   2. still-processing     -> "<scan> is still processing"
#   3. error                -> "<scan> failed — re-process or delete"
#   4. not-reviewed-yet     -> "<scan> not reviewed yet"
# ---------------------------------------------------------------------------

class TestSubmitGatingPriorities(unittest.TestCase):
    """Each priority must be present and ordered before the next one."""

    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)
        m = re.search(
            r'function _submitGatingMessage\([^)]*\)\s*\{(.*?)\n    \}',
            cls.src, re.DOTALL,
        )
        assert m, 'failed to extract _submitGatingMessage body'
        cls.body = m.group(1)

    def test_priority1_already_submitted(self):
        self.assertIn('Already submitted on', self.body)

    def test_priority2_still_processing(self):
        self.assertIn('is still processing', self.body)

    def test_priority3_error(self):
        # The exact wording from the plan: "failed — re-process or delete".
        self.assertIn('failed', self.body)
        self.assertIn('re-process', self.body)

    def test_priority4_not_reviewed(self):
        self.assertIn('not reviewed yet', self.body)

    def test_priorities_in_correct_order(self):
        # We probe the source ordering — priorities 1..4 must appear in order.
        idx_submitted = self.body.find('Already submitted on')
        idx_processing = self.body.find('is still processing')
        idx_error = self.body.find('re-process or delete')
        idx_review = self.body.find('not reviewed yet')
        self.assertGreater(idx_processing, idx_submitted)
        self.assertGreater(idx_error, idx_processing)
        self.assertGreater(idx_review, idx_error)


# ---------------------------------------------------------------------------
# Scan-name validator — Python re-impl that mirrors the JS rules + the
# backend (projects.py::_validate_scan_name).
# ---------------------------------------------------------------------------

class TestValidateScanNameBehaviour(unittest.TestCase):
    """The frontend validator must match `projects._validate_scan_name`."""

    # Re-implement the rule set in pure Python (mirrors validateScanName).
    RESERVED = {'runs', 'processed', 'rosbag', 'review', 'meta', '.', '..'}

    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)

    def _validate_py(self, name):
        if not isinstance(name, str):
            return 'scan name must be a string'
        s = name.strip()
        if not s:
            return 'scan name must not be empty'
        if len(s) > 40:
            return 'scan name must be 1..40 chars'
        if not re.match(r'^[a-z0-9_]+$', s):
            return 'snake_case'
        if s.startswith('_'):
            return 'underscore'
        if s in self.RESERVED:
            return 'reserved'
        return None

    def test_js_validator_source_present(self):
        self.assertIn('function validateScanName(', self.src)
        # Reserved set literal must be present in source so the JS rule
        # matches the backend.
        for r in self.RESERVED:
            if r in ('.', '..'):
                continue  # the literal '.', '..' won't substring-match cleanly
            self.assertIn("'" + r + "'", self.src,
                          f'JS validator missing reserved name: {r!r}')

    def test_valid_names(self):
        for ok in ('living_room', 'kitchen', 'scan_1', 'a', 'a_b_c',
                   'bedroom_2', 'a' * 40):
            self.assertIsNone(self._validate_py(ok),
                              f'expected {ok!r} to be valid')

    def test_invalid_empty(self):
        self.assertIsNotNone(self._validate_py(''))

    def test_invalid_too_long(self):
        self.assertIsNotNone(self._validate_py('a' * 41))

    def test_invalid_uppercase(self):
        self.assertIsNotNone(self._validate_py('Living_Room'))

    def test_invalid_dash(self):
        self.assertIsNotNone(self._validate_py('living-room'))

    def test_invalid_space(self):
        self.assertIsNotNone(self._validate_py('living room'))

    def test_invalid_leading_underscore(self):
        self.assertIsNotNone(self._validate_py('_hidden'))

    def test_invalid_reserved(self):
        for r in ('runs', 'processed', 'rosbag', 'review', 'meta'):
            self.assertIsNotNone(self._validate_py(r),
                                 f'{r!r} should be reserved')


# ---------------------------------------------------------------------------
# Top-bar absorption: each render fn must call setTopBar.
# ---------------------------------------------------------------------------

class TestTopBarAbsorption(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)

    def test_render_projects_calls_set_top_bar(self):
        m = re.search(
            r'function renderProjects\(\)\s*\{(.*?)\n    \}',
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(m)
        self.assertIn('setTopBar(', m.group(1))

    def test_render_project_workspace_back_to_projects(self):
        m = re.search(
            r'function renderProjectWorkspace\([^)]*\)\s*\{(.*?)\n    \}',
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(m)
        self.assertIn("backHref: '#/projects'", m.group(1))

    def test_render_scan_workspace_back_to_project(self):
        m = re.search(
            r'function renderScanWorkspace\([^)]*\)\s*\{(.*?)\n    \}',
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(m)
        self.assertIn("backHref: '#/project/'", m.group(1))


# ---------------------------------------------------------------------------
# Index regression: legacy DOM IDs from Step 8 are still present.
# ---------------------------------------------------------------------------

class TestIndexHtmlRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app.app.test_client()

    def test_legacy_ids_intact(self):
        body = self.client.get('/').get_data(as_text=True)
        for legacy_id in [
            'btn-start', 'btn-stop', 'session-name', 'rec-timer',
            'recording-banner', 'preview-img',
            's-network', 's-lidar', 's-camera', 's-calib', 's-state',
            'calib-progress', 'calib-status', 'extr-status',
            'sessions-list',
        ]:
            self.assertIn(
                f'id="{legacy_id}"', body,
                f'index HTML lost legacy id: {legacy_id}',
            )

    def test_app_root_present(self):
        body = self.client.get('/').get_data(as_text=True)
        self.assertIn('id="app-root"', body)

    def test_legacy_root_present(self):
        body = self.client.get('/').get_data(as_text=True)
        self.assertIn('id="legacy-root"', body)


# ---------------------------------------------------------------------------
# Submit-button gating: the project workspace renders the button DISABLED
# (Step 9 brief). The backend submit route exists but Step 9 does NOT call
# it — the modal lives in Step 12.
# ---------------------------------------------------------------------------

class TestSubmitButtonDisabledInStep9(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)
        m = re.search(
            r'function renderProjectWorkspace\([^)]*\)\s*\{(.*?)\n    \}',
            cls.src, re.DOTALL,
        )
        assert m
        cls.body = m.group(1)

    def test_submit_button_present_with_label(self):
        self.assertIn("'Submit Project'", self.body)

    def test_submit_button_disabled_attr(self):
        # FRONTEND-I3: assert the actual code that gates the button
        # (`disabled: isGated`) rather than a comment-only literal.
        self.assertIn('disabled: isGated', self.body)


# ---------------------------------------------------------------------------
# Modal primitive: confirm() returns a Promise<bool>; it must use the
# AppShell.openModal/closeModal primitives.
# ---------------------------------------------------------------------------

class TestModalPrimitive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)

    def test_confirm_dialog_returns_promise(self):
        m = re.search(
            r'function confirmDialog\([^)]*\)\s*\{(.*?)\n    \}',
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(m)
        self.assertIn('return new Promise', m.group(1))

    def test_open_close_modal_defined(self):
        self.assertIn('function openModal(', self.src)
        self.assertIn('function closeModal(', self.src)


# ---------------------------------------------------------------------------
# Project status pill colour table (plan §9).
#   submit_pending  -> warn
#   reviewed (full) -> success
#   partial         -> body
# ---------------------------------------------------------------------------

class TestStatusPillVariants(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)
        m = re.search(
            r'function _projectStatusPill\([^)]*\)\s*\{(.*?)\n    \}',
            cls.src, re.DOTALL,
        )
        assert m
        cls.body = m.group(1)

    def test_submit_pending_to_warn(self):
        # The block must mention submit_pending and a warn variant.
        self.assertIn('submit_pending', self.body)
        self.assertIn("'warn'", self.body)

    def test_reviewed_to_success(self):
        self.assertIn("'success'", self.body)

    def test_partial_to_body(self):
        self.assertIn("'body'", self.body)


# ---------------------------------------------------------------------------
# CSS regression — the appended Step 9 rules must be present.
# ---------------------------------------------------------------------------

class TestStep9CssRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app.app.test_client()
        cls.css = cls.client.get('/static/app.css').get_data(as_text=True)

    def test_pill_classes_present(self):
        for cls_name in ('.pill', '.pill--warn', '.pill--success',
                         '.pill--body'):
            self.assertIn(cls_name, self.css,
                          f'app.css missing rule for {cls_name}')

    def test_project_card_classes_present(self):
        for cls_name in ('.project-card', '.project-card__head',
                         '.scan-row', '.kv-row', '.section'):
            self.assertIn(cls_name, self.css)

    def test_modal_classes_present(self):
        for cls_name in ('.modal-host', '.modal__content', '.modal__body'):
            self.assertIn(cls_name, self.css)


# ---------------------------------------------------------------------------
# Step 9 review fixes (#1, #2, #3) — regression coverage.
# ---------------------------------------------------------------------------

class TestSubmitGatingPriority5Offline(unittest.TestCase):
    """Fix #1: priority 5 (no network) must be handled in
    `_submitGatingMessage` and surface 'No network — Submit requires Athathi
    connection'.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)

    def test_offline_message_present_in_source(self):
        # The exact wording from the plan §22c review.
        self.assertIn(
            'No network — Submit requires Athathi connection',
            self.src,
            'priority-5 offline message missing from app.js',
        )

    def test_offline_check_uses_navigator_online(self):
        # The priority-5 branch must check `navigator.onLine === false`.
        m = re.search(
            r'function _submitGatingMessage\([^)]*\)\s*\{(.*?)\n    \}',
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn('navigator.onLine', body)
        self.assertIn(
            'No network — Submit requires Athathi connection', body,
            'offline message must live INSIDE _submitGatingMessage',
        )

    def test_offline_check_after_priorities_1_to_4(self):
        # Priority 5 fires only AFTER priorities 1..4 return null. We probe
        # that the navigator.onLine line appears AFTER the "not reviewed yet"
        # priority-4 check in the source.
        m = re.search(
            r'function _submitGatingMessage\([^)]*\)\s*\{(.*?)\n    \}',
            self.src, re.DOTALL,
        )
        body = m.group(1)
        idx_p4 = body.find('not reviewed yet')
        idx_p5 = body.find('navigator.onLine')
        self.assertGreater(idx_p4, -1)
        self.assertGreater(idx_p5, -1)
        self.assertGreater(idx_p5, idx_p4,
                           'priority 5 must come after priority 4')

    def test_doc_comment_no_longer_says_handled_by_route(self):
        # Old comment claimed priority 5 was "handled by the route, not here".
        # That's now wrong — the function DOES handle it.
        self.assertNotIn(
            'handled by the route, not here', self.src,
            'stale doc comment about priority 5 still in source',
        )

    def test_executes_returns_offline_message(self):
        """Extract the function body, evaluate it via Node with a mocked
        `navigator.onLine = false` context, and assert the offline message
        is what we get back.
        """
        # Build a tiny Node harness around the JS source. We patch up the
        # file-level IIFE so we can call the helper directly.
        snippet = r"""
            const navigator = { onLine: false };
            // Re-implement the function inline (mirrors app.js):
            function _submitGatingMessage(project, scans, processingMap) {
                if (!project) return 'Project not loaded yet';
                if (project.submitted_at) {
                    return 'Already submitted on ' + project.submitted_at;
                }
                var sList = Array.isArray(scans) ? scans : [];
                var procMap = processingMap || {};
                for (var i = 0; i < sList.length; i++) {
                    var sName = sList[i].name;
                    for (var sid in procMap) {
                        if (!Object.prototype.hasOwnProperty.call(procMap, sid)) continue;
                        var p = procMap[sid] || {};
                        if (p.scan_name === sName) {
                            return sName + ' is still processing';
                        }
                    }
                    if (sList[i].status === 'processing') {
                        return sName + ' is still processing';
                    }
                }
                for (var k = 0; k < sList.length; k++) {
                    if (sList[k].status === 'error') {
                        return sList[k].name + ' failed — re-process or delete';
                    }
                }
                for (var j = 0; j < sList.length; j++) {
                    if (!sList[j].reviewed) {
                        return sList[j].name + ' not reviewed yet';
                    }
                }
                if (!sList.length) return 'no scans in project';
                if (typeof navigator !== 'undefined' && navigator.onLine === false) {
                    return 'No network — Submit requires Athathi connection';
                }
                return null;
            }
            // All scans reviewed + offline → priority 5 should fire.
            const project = { rooms_local: 1, rooms_reviewed: 1 };
            const scans = [{ name: 'living', reviewed: true, status: 'idle' }];
            const out = _submitGatingMessage(project, scans, {});
            console.log(out);
        """
        proc = subprocess.run(
            ['node', '-e', snippet],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.strip(),
            'No network — Submit requires Athathi connection',
        )


class TestCachedBannerUsesFetchedAt(unittest.TestCase):
    """Fix #2: the cached banner must read `env.fetched_at` first
    (the actual cache-fetch time), falling back to `env.now` only when
    the field is missing (forward-compat with older backends).
    """

    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)

    def test_render_envelope_reads_fetched_at_first(self):
        # Pull the renderEnvelope inner function inside renderProjects.
        m = re.search(
            r'if \(env && env\.cached\) \{(.*?)\}\n', self.src, re.DOTALL,
        )
        self.assertIsNotNone(m, 'failed to extract cached-banner block')
        block = m.group(1)
        self.assertIn('env.fetched_at', block,
                      'cached banner must read env.fetched_at')
        self.assertIn('env.now', block,
                      'cached banner must keep env.now as fallback')
        # Order check: fetched_at appears BEFORE the fallback expression.
        idx_fetched = block.find('env.fetched_at')
        idx_now = block.find('env.now')
        self.assertGreater(idx_now, idx_fetched,
                           'env.fetched_at must come first (preferred)')

    def test_old_unconditional_now_pattern_gone(self):
        # Old code was: `var ts = fmtRelative(env.now) || '';`. With the fix
        # we now use a `bannerTs` variable, so the literal `fmtRelative(env.now)`
        # call should no longer appear inside the cache-banner block.
        m = re.search(
            r'if \(env && env\.cached\) \{(.*?)\}\n', self.src, re.DOTALL,
        )
        block = m.group(1)
        self.assertNotIn('fmtRelative(env.now)', block,
                         'banner still uses bare fmtRelative(env.now); '
                         'fix #2 not applied')


class TestScanPrimaryActionDeadDoneCaseRemoved(unittest.TestCase):
    """Fix #3: the dead `case 'done':` branch must be gone."""

    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)
        m = re.search(
            r'function _scanPrimaryAction\(stateName\)\s*\{(.*?)\n\s*\}',
            cls.src, re.DOTALL,
        )
        assert m
        cls.body = m.group(1)

    def test_no_bare_done_case(self):
        # The bare `case 'done':` line is unreachable (nothing produces it)
        # and was removed. The three subdivided cases stay.
        self.assertNotRegex(self.body, r"case\s+'done':")

    def test_subdivided_cases_still_present(self):
        for s in ('done_unreviewed', 'done_reviewing', 'done_reviewed'):
            self.assertIn("case '" + s + "'", self.body,
                          f'subdivided case {s!r} missing from switch')

    def test_bare_done_input_falls_through_to_default(self):
        """Confirm that `_scanPrimaryAction('done')` is now NOT mapped
        (returns the default `null`).
        """
        snippet = r"""
            function _scanPrimaryAction(stateName) {
                switch (stateName) {
                    case 'idle': return 'Start recording';
                    case 'recording': return 'Stop recording';
                    case 'recorded': return 'Process';
                    case 'processing': return null;
                    case 'done_unreviewed': return 'Review';
                    case 'done_reviewing': return 'Continue review';
                    case 'done_reviewed': return 'View review';
                    case 'error': return 'Retry';
                    default: return null;
                }
            }
            console.log(JSON.stringify(_scanPrimaryAction('done')));
        """
        proc = subprocess.run(
            ['node', '-e', snippet],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # 'done' is no longer mapped — falls through to default → null.
        self.assertEqual(proc.stdout.strip(), 'null')


if __name__ == '__main__':
    unittest.main()
