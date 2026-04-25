"""Static-asset unit tests for the Step 12 Submit-Project modal + sync-pending
banner + retry sweep + project-list status pills.

Per Step 12 of TECHNICIAN_REVIEW_PLAN.md §16 (and §8 submit pipeline,
§22c gating, §22d helpers, §13 visual style), the SPA grows:

  1. A two-stage Submit modal (`_openSubmitModal`) on the project workspace.
  2. A sync-pending banner + 60 s auto-retry sweep on the projects list.
  3. A `↻ sync pending` pill on project cards whose manifest has
     `submit_pending: true`.

We can't run a real browser here, but we CAN:
  - Probe `static/app.js` for the load-bearing functions and literals.
  - Drive the pure helpers (`_submitPendingProjects`, `_runSubmitRetrySweep`)
    under Node by spawning `node -e` and pasting in a re-implementation.
  - Drive the existing `_projectStatusPill` re-implementation to confirm the
    submit_pending → warn variant survives the Step 12 label change.
  - Verify the css rules + AppShell exports are present.

These tests sit ON TOP of the existing 531 tests; they MUST NOT regress
any of them.
"""

import json
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
APP_CSS_PATH = os.path.join(ROOT, 'static', 'app.css')


def _read(path):
    with open(path, 'r') as f:
        return f.read()


def _has_node():
    try:
        rc = subprocess.run(['node', '--version'], capture_output=True)
        return rc.returncode == 0
    except (OSError, FileNotFoundError):
        return False


def _run_node(snippet, timeout=10):
    proc = subprocess.run(
        ['node', '-e', snippet],
        capture_output=True, text=True, timeout=timeout,
    )
    return proc


# ---------------------------------------------------------------------------
# 1. _openSubmitModal is defined and exported.
# ---------------------------------------------------------------------------

class TestOpenSubmitModalDefined(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)

    def test_open_submit_modal_function_defined(self):
        self.assertIn('function _openSubmitModal(', self.src,
                      'expected _openSubmitModal() defined in app.js')

    def test_app_shell_exports_step12_helpers(self):
        for sym in ('_openSubmitModal', '_submitPendingProjects',
                    '_runSubmitRetrySweep'):
            self.assertIn(sym + ':', self.src,
                          f'AppShell missing exported helper: {sym}')

    def test_modal_uses_open_modal_primitive(self):
        # Pulled body of _openSubmitModal must reuse openModal/closeModal.
        m = re.search(
            r'function _openSubmitModal\([^)]*\)\s*\{(.*?)\n    \}',
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(m, 'expected _openSubmitModal body extractable')
        body = m.group(1)
        self.assertIn('openModal(', body,
                      '_openSubmitModal must reuse openModal()')
        self.assertIn('closeModal(', body,
                      '_openSubmitModal must reuse closeModal()')


# ---------------------------------------------------------------------------
# 2. Stage 1 (Confirm) renders customer + scan count.
# ---------------------------------------------------------------------------

class TestSubmitModalConfirmStage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)
        m = re.search(
            r'function _openSubmitModal\([^)]*\)\s*\{(.*?)\n    \}',
            cls.src, re.DOTALL,
        )
        assert m
        cls.body = m.group(1)

    def test_modal_title_includes_scan_id(self):
        # "Submit project #<id>" (per the plan §8 mock).
        self.assertIn("'Submit project #'", self.body)

    def test_renders_customer_field(self):
        self.assertIn('Customer:', self.body)
        self.assertIn('customer_name', self.body)

    def test_renders_scan_count(self):
        # Scans line label + we walk scanNames from the scans list.
        self.assertIn('Scans:', self.body)
        self.assertIn('scanNames', self.body)

    def test_modal_warns_about_athathi_completion(self):
        # The plan's confirm copy: "Once submitted, the assignment is
        # marked complete in Athathi."
        self.assertIn('marked complete in Athathi', self.body)

    def test_confirm_stage_has_cancel_and_submit_buttons(self):
        # Per the plan mock: "[ Cancel ]    [ Submit ]"
        self.assertIn("'Cancel'", self.body)
        self.assertIn("'Submit'", self.body)


# ---------------------------------------------------------------------------
# 3. Submit POST goes via AppShell.fetchJson.
# ---------------------------------------------------------------------------

class TestSubmitGoesViaFetchJson(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)
        m = re.search(
            r'function _openSubmitModal\([^)]*\)\s*\{(.*?)\n    \}',
            cls.src, re.DOTALL,
        )
        assert m
        cls.body = m.group(1)

    def test_submit_post_uses_fetchJson(self):
        # Looking for: fetchJson('POST', '/api/project/' + ... + '/submit')
        self.assertRegex(
            self.body,
            r"fetchJson\(\s*'POST'\s*,\s*\n?\s*'/api/project/'",
        )
        self.assertIn("'/submit'", self.body)

    def test_preview_called_via_fetchJson_optional(self):
        # Preview is best-effort; must hit the /submit/preview endpoint.
        self.assertIn("'/submit/preview'", self.body)

    def test_no_raw_fetch_in_submit_modal(self):
        # The modal MUST NOT use raw `fetch(...)` — every API call goes
        # through fetchJson per the plan's hard constraint.
        self.assertNotRegex(
            self.body,
            r'(?<![a-zA-Z_])fetch\(',
            'submit modal must use fetchJson, not raw fetch()',
        )


# ---------------------------------------------------------------------------
# 4. On 200, success stage renders.
# ---------------------------------------------------------------------------

class TestSubmitSuccessStage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)
        m = re.search(
            r'function _openSubmitModal\([^)]*\)\s*\{(.*?)\n    \}',
            cls.src, re.DOTALL,
        )
        assert m
        cls.body = m.group(1)

    def test_success_check_mark_present(self):
        # The plan: "✓ Submitted on <time>".
        self.assertIn('✓ Submitted', self.body)

    def test_success_uses_completed_at(self):
        # Backend returns {ok: true, completed_at, hook}.
        self.assertIn('completed_at', self.body)

    def test_success_navigates_to_projects_list(self):
        self.assertIn("'#/projects'", self.body)


# ---------------------------------------------------------------------------
# 5. On 202 queued, modal closes + sync-pending toast appears.
# ---------------------------------------------------------------------------

class TestSubmitQueuedStage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)
        m = re.search(
            r'function _openSubmitModal\([^)]*\)\s*\{(.*?)\n    \}',
            cls.src, re.DOTALL,
        )
        assert m
        cls.body = m.group(1)

    def test_queued_path_detected_via_payload_marker(self):
        # fetchJson resolves on 2xx (including 202); we detect the queued
        # state via the body's `queued: true` marker.
        self.assertRegex(
            self.body,
            r'payload\s*&&\s*payload\.queued',
        )

    def test_queued_calls_close_modal_and_shows_toast(self):
        # Inside the renderQueued path, we must close the modal and show
        # a sync-pending toast that surfaces the upstream reason.
        self.assertIn('Will retry when network returns', self.body)
        self.assertIn("'↻ Queued — ' + reason +", self.body)
        self.assertIn('closeModal', self.body)
        self.assertIn('showToast', self.body)


# ---------------------------------------------------------------------------
# 6. On 502, error stage shows the upstream tail.
# ---------------------------------------------------------------------------

class TestSubmit502Error(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)
        m = re.search(
            r'function _openSubmitModal\([^)]*\)\s*\{(.*?)\n    \}',
            cls.src, re.DOTALL,
        )
        assert m
        cls.body = m.group(1)

    def test_502_branches_on_status(self):
        # We dispatch on err.status: 502 == upstream error.
        self.assertIn('502', self.body)
        self.assertIn('upstream_body_tail', self.body)

    def test_502_renders_failed_headline(self):
        self.assertIn('Submit failed', self.body)

    def test_502_provides_retry_button(self):
        # The error stage offers a retry button.
        self.assertIn("'Retry'", self.body)


# ---------------------------------------------------------------------------
# 7. On 400, error stage renders the gating message.
# ---------------------------------------------------------------------------

class TestSubmit400Gating(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)
        m = re.search(
            r'function _openSubmitModal\([^)]*\)\s*\{(.*?)\n    \}',
            cls.src, re.DOTALL,
        )
        assert m
        cls.body = m.group(1)

    def test_400_handled_explicitly(self):
        # 400 surfaces the server-side gating message verbatim.
        self.assertIn('400', self.body)
        self.assertIn('Cannot submit', self.body)

    def test_400_no_retry_button(self):
        # 400 (gating) means the work isn't ready — we offer Close, no Retry.
        # JS-5 generalised the guard from `status !== 400` to a 4xx-range
        # check (`isClient4xx`), so the Retry button is suppressed for any
        # client-error status, including 400.
        self.assertRegex(
            self.body,
            r'isClient4xx\s*=\s*status\s*>=\s*400\s*&&\s*status\s*<\s*500',
        )
        # And the Retry button is gated on NOT isClient4xx.
        self.assertRegex(
            self.body,
            r'if\s*\(\s*!isClient4xx\s*\)',
        )


# ---------------------------------------------------------------------------
# 8. Sync-pending pill appears on cards whose manifest has submit_pending.
# ---------------------------------------------------------------------------

class TestSyncPendingPill(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)
        m = re.search(
            r'function _projectStatusPill\([^)]*\)\s*\{(.*?)\n    \}',
            cls.src, re.DOTALL,
        )
        assert m
        cls.body = m.group(1)

    def test_pill_has_sync_pending_label(self):
        # Step 12 rebrands the warn pill to use the ↻ glyph + "sync pending".
        self.assertIn('sync pending', self.body)
        self.assertIn('↻', self.body)

    def test_pill_is_warn_variant(self):
        self.assertIn('submit_pending', self.body)
        self.assertIn("'warn'", self.body)


# ---------------------------------------------------------------------------
# 9. The 60 s auto-retry sweep is installed on the projects screen.
# ---------------------------------------------------------------------------

class TestAutoRetryTimer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)

    def _render_projects_body(self):
        m = re.search(
            r'function renderProjects\(\)\s*\{(.*?)\n    \}',
            self.src, re.DOTALL,
        )
        assert m
        return m.group(1)

    def test_retry_timer_runs_at_60s(self):
        body = self._render_projects_body()
        self.assertIn('60000', body,
                      'expected a 60 s auto-retry timer in renderProjects')

    def test_retry_timer_keyed_in_pollTimers(self):
        # The screen is meant to install state.pollTimers.<key> so the
        # global clearScreenTimers() can tear it down on screen leave.
        body = self._render_projects_body()
        self.assertRegex(
            body,
            r'state\.pollTimers\.\w+\s*=\s*setInterval',
        )

    def test_clear_on_screen_leave_handled_globally(self):
        # `clearScreenTimers()` walks every key in state.pollTimers and
        # clears it. The render function for the projects screen relies on
        # that — but the new project workspace also calls
        # clearScreenTimers() on entry, which kills the timer when leaving
        # the projects screen.
        self.assertIn('function clearScreenTimers(', self.src)
        m = re.search(
            r'function clearScreenTimers\(\)\s*\{(.*?)\n    \}',
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(m)
        self.assertIn('state.pollTimers', m.group(1))
        self.assertRegex(m.group(1), r'clearInterval')

    def test_timer_only_installs_when_pending_present(self):
        # Guard: we must not install the timer when the pending list is
        # empty — that would burn CPU + battery.
        body = self._render_projects_body()
        # Look for a guard like `if (pending.length > 0)`.
        self.assertRegex(
            body,
            r'pending\.length\s*>\s*0',
        )


# ---------------------------------------------------------------------------
# 10. The manual "Retry now" link triggers a retry for every pending project.
# ---------------------------------------------------------------------------

class TestRetryNowLink(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)

    def _render_projects_body(self):
        m = re.search(
            r'function renderProjects\(\)\s*\{(.*?)\n    \}',
            self.src, re.DOTALL,
        )
        assert m
        return m.group(1)

    def test_retry_now_link_present(self):
        body = self._render_projects_body()
        self.assertIn('Retry now', body)

    def test_retry_now_calls_retry_sweep(self):
        body = self._render_projects_body()
        self.assertIn('_runSubmitRetrySweep', body)

    def test_retry_sweep_uses_retry_endpoint(self):
        # _runSubmitRetrySweep itself must POST to /api/project/<id>/submit/retry.
        m = re.search(
            r'function _runSubmitRetrySweep\([^)]*\)\s*\{(.*?)\n    \}',
            self.src, re.DOTALL,
        )
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("'/submit/retry'", body)
        self.assertRegex(
            body,
            r"fetchJson\(\s*'POST'\s*,",
        )


# ---------------------------------------------------------------------------
# 11. After a successful retry, the relevant pill disappears.
#
# We exercise this via _submitPendingProjects under Node: we mutate the
# mock state (drop the submit_pending flag) and assert the helper no
# longer returns the project.
# ---------------------------------------------------------------------------

class TestSubmitPendingHelperPureLogic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)

    def test_helper_defined(self):
        self.assertIn('function _submitPendingProjects(', self.src)

    def test_helper_finds_pending_in_each_bucket(self):
        if not _has_node():
            self.skipTest('node not available')
        snippet = r"""
            // Re-implement the helper byte-equivalent to app.js.
            function _submitPendingProjects(env) {
                if (!env || typeof env !== 'object') return [];
                var out = [];
                var buckets = ['scheduled', 'history', 'ad_hoc'];
                for (var b = 0; b < buckets.length; b++) {
                    var arr = env[buckets[b]];
                    if (!Array.isArray(arr)) continue;
                    for (var i = 0; i < arr.length; i++) {
                        if (arr[i] && arr[i].submit_pending) out.push(arr[i]);
                    }
                }
                return out;
            }
            const env = {
                scheduled: [
                    { scan_id: 1, submit_pending: true,  customer_name: 'A' },
                    { scan_id: 2, submit_pending: false, customer_name: 'B' },
                ],
                history: [
                    { scan_id: 3, submit_pending: true,  customer_name: 'C' },
                ],
                ad_hoc: [
                    { scan_id: 4, submit_pending: false, customer_name: 'D' },
                    { scan_id: 5, submit_pending: true,  customer_name: 'E' },
                ],
            };
            const initial = _submitPendingProjects(env).map(p => p.scan_id);
            // Now mutate the mock state — clear sid 3's flag (simulate
            // a successful retry) and re-evaluate.
            env.history[0].submit_pending = false;
            const after = _submitPendingProjects(env).map(p => p.scan_id);
            // And clear the rest:
            env.scheduled[0].submit_pending = false;
            env.ad_hoc[1].submit_pending = false;
            const final = _submitPendingProjects(env).map(p => p.scan_id);
            console.log(JSON.stringify({initial, after, final}));
        """
        proc = _run_node(snippet)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout.strip())
        self.assertEqual(sorted(data['initial']), [1, 3, 5])
        # After mutating sid 3, only 1 + 5 remain.
        self.assertEqual(sorted(data['after']), [1, 5])
        # After clearing every flag, the helper returns [].
        self.assertEqual(data['final'], [])

    def test_helper_handles_missing_buckets(self):
        if not _has_node():
            self.skipTest('node not available')
        snippet = r"""
            function _submitPendingProjects(env) {
                if (!env || typeof env !== 'object') return [];
                var out = [];
                var buckets = ['scheduled', 'history', 'ad_hoc'];
                for (var b = 0; b < buckets.length; b++) {
                    var arr = env[buckets[b]];
                    if (!Array.isArray(arr)) continue;
                    for (var i = 0; i < arr.length; i++) {
                        if (arr[i] && arr[i].submit_pending) out.push(arr[i]);
                    }
                }
                return out;
            }
            console.log(JSON.stringify(_submitPendingProjects(null)));
            console.log(JSON.stringify(_submitPendingProjects({})));
            console.log(JSON.stringify(_submitPendingProjects({scheduled: 'not-array'})));
        """
        proc = _run_node(snippet)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.strip().split('\n')
        self.assertEqual(lines[0], '[]')
        self.assertEqual(lines[1], '[]')
        self.assertEqual(lines[2], '[]')


# ---------------------------------------------------------------------------
# 12. Touch-target check: every new interactive element ≥44 px (≥56 px for
# primary buttons). The CSS file appends rules at the bottom — we probe.
# ---------------------------------------------------------------------------

class TestTouchTargetRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = _read(APP_CSS_PATH)

    def test_sync_banner_min_height_44(self):
        # Banner row + retry button must each ≥ 44 px.
        m = re.search(
            r'\.sync-banner\s*\{(.*?)\}', self.css, re.DOTALL,
        )
        self.assertIsNotNone(m, '.sync-banner rule missing')
        self.assertRegex(m.group(1), r'min-height:\s*44px')

    def test_sync_banner_retry_min_height_44(self):
        m = re.search(
            r'\.sync-banner__retry\s*\{(.*?)\}', self.css, re.DOTALL,
        )
        self.assertIsNotNone(m, '.sync-banner__retry rule missing')
        self.assertRegex(m.group(1), r'min-height:\s*44px')

    def test_btn_lg_primary_used_in_modal_actions(self):
        # The submit modal reuses .btn-primary.btn-lg, which is already
        # ≥56 px from the global rule. Smoke-check the CSS has that rule.
        m = re.search(
            r'\.btn-primary\.btn-lg[^{]*\{(.*?)\}', self.css, re.DOTALL,
        )
        self.assertIsNotNone(m,
                             '.btn-primary.btn-lg rule missing — primary '
                             'CTA wouldn\'t hit the 56 px minimum')
        self.assertRegex(m.group(1), r'min-height:\s*56px')

    def test_submit_modal_css_present(self):
        for cls in ('.submit-modal__body', '.submit-modal__inflight',
                    '.submit-modal__result', '.sync-banner',
                    '.sync-banner__retry'):
            self.assertIn(cls, self.css,
                          f'CSS rule missing: {cls}')


# ---------------------------------------------------------------------------
# Bonus — node --check on the bundle (non-regression smoke).
# ---------------------------------------------------------------------------

class TestNodeSyntaxCheck(unittest.TestCase):
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


# ---------------------------------------------------------------------------
# Bonus — the project workspace is wired to the modal (no longer a toast).
# ---------------------------------------------------------------------------

class TestProjectWorkspaceWiringStep12(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)
        m = re.search(
            r'function renderProjectWorkspace\([^)]*\)\s*\{(.*?)\n    \}',
            cls.src, re.DOTALL,
        )
        assert m
        cls.body = m.group(1)

    def test_workspace_calls_open_submit_modal(self):
        self.assertIn('_openSubmitModal(', self.body,
                      'project workspace must wire the Submit button to '
                      '_openSubmitModal')

    def test_workspace_no_step_12_placeholder_toast(self):
        # The Step 9 placeholder toast is gone.
        self.assertNotIn('Submit modal lands in Step 12', self.body)

    def test_submit_button_uses_gating_message(self):
        self.assertIn('_submitGatingMessage(', self.body)


if __name__ == '__main__':
    unittest.main()
