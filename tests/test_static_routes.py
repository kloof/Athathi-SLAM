"""Flask test_client probes for the new static assets and the index route.

Per the Step 8 brief: NO Selenium / playwright. Just a Flask test_client
that confirms `/`, `/static/app.css`, `/static/app.js`, `/static/logo.svg`
all return the right MIME and that the index HTML carries the legacy DOM
IDs the new SPA shell will absorb in Steps 9-10.
"""

import os
import sys
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import app  # noqa: E402


class TestStaticRoutes(unittest.TestCase):
    """Smoke checks that the Flask app serves the new assets correctly."""

    @classmethod
    def setUpClass(cls):
        cls.client = app.app.test_client()

    # --- 1. The index route serves the new template. ---

    def test_index_returns_200(self):
        res = self.client.get('/')
        self.assertEqual(res.status_code, 200)

    def test_index_html_carries_brand(self):
        res = self.client.get('/')
        body = res.get_data(as_text=True).lower()
        self.assertTrue(
            'athathi' in body or 'technician' in body,
            'index template should include the new athathi/technician branding',
        )

    # --- 2. /static/app.css returns 200 + text/css. ---

    def test_static_app_css(self):
        res = self.client.get('/static/app.css')
        self.assertEqual(res.status_code, 200)
        ctype = (res.headers.get('Content-Type') or '').lower()
        self.assertIn('text/css', ctype)
        body = res.get_data(as_text=True)
        # Sanity: at least one Athathi token present.
        self.assertIn('--accent', body)

    # --- 3. /static/app.js returns 200 + JS MIME. ---

    def test_static_app_js(self):
        res = self.client.get('/static/app.js')
        self.assertEqual(res.status_code, 200)
        ctype = (res.headers.get('Content-Type') or '').lower()
        # Flask serves .js as application/javascript on Werkzeug; some envs use text/javascript.
        self.assertTrue(
            'javascript' in ctype,
            f'expected javascript content-type, got: {ctype!r}',
        )
        self.assertIn('AppShell', res.get_data(as_text=True))

    def test_static_legacy_app_js(self):
        res = self.client.get('/static/legacy_app.js')
        self.assertEqual(res.status_code, 200)
        ctype = (res.headers.get('Content-Type') or '').lower()
        self.assertTrue(
            'javascript' in ctype,
            f'expected javascript content-type, got: {ctype!r}',
        )
        self.assertIn('LegacyApp', res.get_data(as_text=True))

    # --- 4. /static/logo.svg returns 200 + SVG MIME. ---

    def test_static_logo_svg(self):
        res = self.client.get('/static/logo.svg')
        self.assertEqual(res.status_code, 200)
        ctype = (res.headers.get('Content-Type') or '').lower()
        self.assertIn('svg', ctype)
        body = res.get_data(as_text=True)
        self.assertIn('<svg', body)

    # --- 5. Legacy DOM IDs are still present in the index body. ---

    def test_legacy_dom_ids_present_in_index(self):
        res = self.client.get('/')
        body = res.get_data(as_text=True)
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

    def test_index_links_static_bundles(self):
        res = self.client.get('/')
        body = res.get_data(as_text=True)
        for href in ['/static/app.css', '/static/app.js', '/static/legacy_app.js', '/static/logo.svg']:
            self.assertIn(href, body, f'index HTML must reference {href}')

    def test_index_viewport_meta_locked(self):
        res = self.client.get('/')
        body = res.get_data(as_text=True)
        self.assertIn('width=640', body)
        self.assertIn('height=480', body)
        self.assertIn('user-scalable=no', body)


if __name__ == '__main__':
    unittest.main()
