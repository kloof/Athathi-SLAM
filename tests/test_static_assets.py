"""Static asset presence + content checks for Step 8.

These tests look at the files on disk (no Flask client). They lock in:
  * `/static/app.css` exists and contains every Athathi color token.
  * `/static/app.js` defines `window.AppShell`.
  * `/static/legacy_app.js` defines `window.LegacyApp` and exposes the
    function names from plan §23d.
  * `/static/logo.svg` exists.
  * `templates/index.html` references all of the above + locks the viewport
    to width=640, height=480.

No Selenium / playwright. Pure file-presence + string checks per the
Step 8 brief.
"""

import os
import re
import sys
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

STATIC_DIR = os.path.join(ROOT, 'static')
TEMPLATES_DIR = os.path.join(ROOT, 'templates')


# Tokens locked in plan §13.
EXPECTED_CSS_TOKENS = [
    '--bg-app',
    '--bg-surface',
    '--bg-sidebar',
    '--accent',
    '--accent-soft',
    '--text-primary',
    '--text-body',
    '--text-on-dark',
    '--border',
    '--warn',
    '--danger',
    '--success',
]


# Function names listed in plan §23d ("UI/asset readiness").
EXPECTED_LEGACY_FUNCS = [
    'connectSSE',
    'updateProcessingUI',
    'processSession',
    'renderFloorplanSvg',
    'renderGallery',
    'renderResultBlock',
    'loadSessions',
    'checkStatus',
    'startRecording',
    'stopRecording',
    'startIntrinsicCalib',
    'stopIntrinsicCalib',
    'pollCalibStatus',
    'setExtrinsicsManual',
    'deleteSession',
]


def _read(path):
    with open(path, 'rb') as f:
        return f.read().decode('utf-8')


class TestStaticAssetsPresence(unittest.TestCase):
    """Files exist on disk."""

    def test_app_css_exists(self):
        path = os.path.join(STATIC_DIR, 'app.css')
        self.assertTrue(os.path.isfile(path), f'missing: {path}')
        self.assertGreater(os.path.getsize(path), 100, 'app.css looks empty')

    def test_app_js_exists(self):
        path = os.path.join(STATIC_DIR, 'app.js')
        self.assertTrue(os.path.isfile(path), f'missing: {path}')
        self.assertGreater(os.path.getsize(path), 100, 'app.js looks empty')

    def test_legacy_app_js_exists(self):
        path = os.path.join(STATIC_DIR, 'legacy_app.js')
        self.assertTrue(os.path.isfile(path), f'missing: {path}')
        self.assertGreater(os.path.getsize(path), 100, 'legacy_app.js looks empty')

    def test_logo_svg_exists(self):
        path = os.path.join(STATIC_DIR, 'logo.svg')
        self.assertTrue(os.path.isfile(path), f'missing: {path}')
        self.assertGreater(os.path.getsize(path), 100, 'logo.svg looks empty')


class TestAppCssTokens(unittest.TestCase):
    """app.css contains every Athathi color token from plan §13."""

    def test_all_color_tokens_present(self):
        css = _read(os.path.join(STATIC_DIR, 'app.css'))
        for token in EXPECTED_CSS_TOKENS:
            self.assertIn(token, css, f'app.css missing token: {token}')

    def test_token_root_block_declared(self):
        css = _read(os.path.join(STATIC_DIR, 'app.css'))
        self.assertIn(':root', css, 'app.css must declare a :root block for tokens')

    def test_layout_primitive_classes_present(self):
        css = _read(os.path.join(STATIC_DIR, 'app.css'))
        for selector in [
            '.topbar',
            '.screen',
            '.btn-primary',
            '.btn-secondary',
            '.btn-danger',
            '.input',
            '.empty-state',
            '.toast',
            '.spinner',
        ]:
            self.assertIn(selector, css, f'app.css missing primitive: {selector}')


class TestOskAndLayoutFixes(unittest.TestCase):
    """Locks the OSK / layout / contrast fixes from the technician-UI audit.

    Each test is a CSS-substring check — pure file content, no DOM.
    """

    @classmethod
    def setUpClass(cls):
        cls.css = _read(os.path.join(STATIC_DIR, 'app.css'))

    def test_css1_dvh_fallbacks_present(self):
        # CSS-1: 100vh paired with 100dvh on the three full-height blocks.
        self.assertIn('min-height: 100dvh;', self.css)
        self.assertIn('min-height: calc(100dvh - var(--topbar-h));', self.css)

    def test_css2_screen_padding_bottom_260(self):
        # CSS-2: bottom-anchored buttons reachable when OSK opens.
        self.assertIn('padding: 16px 16px 260px;', self.css)

    def test_css2_review_screen_padding(self):
        # CSS-2: .review-screen padding-bottom raised to 260px.
        self.assertRegex(
            self.css,
            r'\.review-screen\s*\{[^}]*padding-bottom:\s*260px',
        )

    def test_css3_modal_top_anchored(self):
        # CSS-3: modal host uses align-items: flex-start (was center).
        self.assertRegex(
            self.css,
            r'\.modal-host\s*\{[^}]*align-items:\s*flex-start',
        )
        self.assertIn('max-height: calc(100dvh - 24px);', self.css)

    def test_css4_textarea_max_heights(self):
        # CSS-4: review-notes + settings-textarea capped at 160px.
        self.assertRegex(
            self.css,
            r'\.review-notes__input\s*\{[^}]*max-height:\s*160px',
        )
        self.assertRegex(
            self.css,
            r'\.settings-textarea\s*\{[^}]*max-height:\s*160px',
        )

    def test_css5_accent_text_token(self):
        # CSS-5: WCAG-compliant text-only accent token.
        self.assertIn('--accent-text:', self.css)
        # And used at least one place as text color.
        self.assertIn('color: var(--accent-text);', self.css)

    def test_css6_class_chip_ellipsis(self):
        # CSS-6: review-card class chip clips long labels with ellipsis.
        self.assertRegex(
            self.css,
            r'\.review-card__class\s*\{[^}]*text-overflow:\s*ellipsis',
        )

    def test_css7_topbar_user_clipped(self):
        # CSS-7: topbar username max-width: 140px.
        self.assertRegex(
            self.css,
            r'\.topbar__user\s*\{[^}]*max-width:\s*140px',
        )

    def test_css8_review_header_below_topbar(self):
        # CSS-8: sticky review header offset by topbar height.
        self.assertRegex(
            self.css,
            r'\.review-header\s*\{[^}]*top:\s*var\(--topbar-h\)',
        )

    def test_css9_focus_visible_block_present(self):
        # CSS-9: combined :focus-visible selector covering project-card et al.
        self.assertIn('.project-card:focus-visible', self.css)
        self.assertIn('.scan-row:focus-visible', self.css)
        self.assertIn('.product-card:focus-visible', self.css)
        self.assertIn('.runs-list__row:focus-visible', self.css)
        self.assertIn('.review-tab__btn:focus-visible', self.css)
        self.assertIn('.section__head:focus-visible', self.css)

    def test_css10_merge_modal_radio_44px(self):
        # CSS-10: merge-modal radio min-height 44 (was 36).
        self.assertRegex(
            self.css,
            r'\.merge-modal__radio\s*\{[^}]*min-height:\s*44px',
        )
        # And no leftover 36 in the same rule.
        m = re.search(
            r'\.merge-modal__radio\s*\{[^}]*\}', self.css,
        )
        assert m is not None
        self.assertNotIn('36px', m.group(0))

    def test_css11_osk_open_repositions_toast_and_sticky(self):
        # CSS-11: body.osk-open lifts the toast and hides the sticky bar.
        self.assertRegex(
            self.css,
            r'body\.osk-open\s+\.toast\s*\{[^}]*bottom:\s*260px',
        )
        self.assertRegex(
            self.css,
            r'body\.osk-open\s+\.review-sticky\s*\{[^}]*display:\s*none',
        )


class TestAppJsShape(unittest.TestCase):
    """app.js exports an identifiable AppShell global."""

    def test_app_shell_global_assigned(self):
        js = _read(os.path.join(STATIC_DIR, 'app.js'))
        # Look for the actual assignment, not just a string mention.
        self.assertIn('window.AppShell', js)
        self.assertTrue(
            'window.AppShell =' in js or 'window.AppShell=' in js,
            'app.js must explicitly assign window.AppShell',
        )

    def test_app_js_has_router(self):
        js = _read(os.path.join(STATIC_DIR, 'app.js'))
        # The router walks the hash; both the parser and a hashchange listener
        # should be present.
        self.assertIn('parseRoute', js)
        self.assertIn('hashchange', js)

    def test_app_js_has_login_handler(self):
        js = _read(os.path.join(STATIC_DIR, 'app.js'))
        self.assertIn('/api/auth/login', js)
        self.assertIn('/api/auth/logout', js)
        self.assertIn('/api/auth/me', js)

    def test_app_js_has_fetch_helper(self):
        js = _read(os.path.join(STATIC_DIR, 'app.js'))
        self.assertIn('fetchJson', js)
        self.assertIn("credentials: 'same-origin'", js)


class TestLegacyAppJsShape(unittest.TestCase):
    """legacy_app.js defines window.LegacyApp + every named function."""

    def test_legacy_app_global_assigned(self):
        js = _read(os.path.join(STATIC_DIR, 'legacy_app.js'))
        self.assertIn('window.LegacyApp', js)
        self.assertTrue(
            'window.LegacyApp =' in js or 'window.LegacyApp=' in js,
            'legacy_app.js must explicitly assign window.LegacyApp',
        )

    def test_legacy_app_has_named_functions(self):
        js = _read(os.path.join(STATIC_DIR, 'legacy_app.js'))
        for fn in EXPECTED_LEGACY_FUNCS:
            # Each function is attached as `LegacyApp.<name> = function ...`
            needle_a = f'LegacyApp.{fn} ='
            needle_b = f'LegacyApp.{fn}='
            self.assertTrue(
                needle_a in js or needle_b in js,
                f'legacy_app.js missing: LegacyApp.{fn}',
            )

    def test_legacy_app_has_init(self):
        js = _read(os.path.join(STATIC_DIR, 'legacy_app.js'))
        self.assertTrue(
            'LegacyApp.init =' in js or 'LegacyApp.init=' in js,
            'legacy_app.js must define LegacyApp.init',
        )


class TestIndexTemplate(unittest.TestCase):
    """templates/index.html links all the new assets and locks the viewport."""

    def setUp(self):
        self.html = _read(os.path.join(TEMPLATES_DIR, 'index.html'))

    def test_links_app_css(self):
        self.assertIn('/static/app.css', self.html)

    def test_links_app_js(self):
        self.assertIn('/static/app.js', self.html)

    def test_links_legacy_app_js(self):
        self.assertIn('/static/legacy_app.js', self.html)

    def test_references_logo(self):
        self.assertIn('/static/logo.svg', self.html)

    def test_viewport_locked_to_640x480(self):
        # Hard-coded per plan §13 + §23d.
        self.assertIn('width=640', self.html)
        self.assertIn('height=480', self.html)
        self.assertIn('user-scalable=no', self.html)

    def test_legacy_dom_ids_preserved(self):
        # Plan §23d: keep these IDs stable across view swaps.
        for legacy_id in [
            'btn-start', 'btn-stop', 'session-name', 'rec-timer',
            'recording-banner', 'preview-img',
            's-network', 's-lidar', 's-camera', 's-calib', 's-state',
            'calib-progress', 'calib-status', 'extr-status',
            'sessions-list',
        ]:
            self.assertIn(f'id="{legacy_id}"', self.html,
                          f'legacy id missing from template: {legacy_id}')

    def test_legacy_block_present_under_legacy_root(self):
        self.assertIn('id="legacy-root"', self.html)
        self.assertIn('data-legacy="true"', self.html)

    def test_app_root_present(self):
        self.assertIn('id="app-root"', self.html)


if __name__ == '__main__':
    unittest.main()
