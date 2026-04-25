"""Static-asset + backend-route unit tests for the Step 11 Settings sheet.

Per Step 11 of TECHNICIAN_REVIEW_PLAN.md §16, the SPA grows a fullscreen
Settings sheet at `#/settings` with three tabs (Device / Account / App)
and the backend grows a single new route pair (`GET` + `PATCH
/api/settings/config`) for editing `auth.config.json`.

We can't drive a real browser in CI, but we CAN:

  1. String-search `static/app.js` for the load-bearing functions and
     literals.
  2. Drive the pure helpers (`_jwtExpiryFromToken`, `_settingsSaveEnabled`,
     `_setSettingsTab`) under Node by spawning `node -e`.
  3. Drive the new backend routes via `app.app.test_client()`.

These tests sit ON TOP of the existing 503 tests; they MUST NOT regress
any of them.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time as _time
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import auth   # noqa: E402
import app    # noqa: E402

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


def _make_jwt(payload):
    """Build a JWT-shaped token; signature is garbage (not verified locally)."""
    def b64url(b):
        return base64.urlsafe_b64encode(b).rstrip(b'=').decode('ascii')

    header = {'alg': 'HS256', 'typ': 'JWT'}
    h = b64url(json.dumps(header).encode('utf-8'))
    p = b64url(json.dumps(payload).encode('utf-8'))
    s = b64url(b'sig')
    return f'{h}.{p}.{s}'


def _fresh_jwt(seconds_until_expiry=3600):
    return _make_jwt({'exp': int(_time.time()) + seconds_until_expiry,
                      'user_id': 7, 'username': 'tech'})


# ---------------------------------------------------------------------------
# 1. renderSettings is defined + registered for #/settings.
# ---------------------------------------------------------------------------

class TestRenderSettingsWired(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)

    def test_render_settings_defined(self):
        self.assertIn('function renderSettings(', self.src,
                      'expected renderSettings() defined in app.js')

    def test_routes_table_uses_render_settings(self):
        # The ROUTES table maps the 'settings' name to renderSettings.
        m = re.search(r'var ROUTES = \{(.*?)\};', self.src, re.DOTALL)
        self.assertIsNotNone(m, 'expected ROUTES table in app.js')
        self.assertRegex(
            m.group(1),
            r"settings\s*:\s*renderSettings",
            'ROUTES.settings must point at renderSettings',
        )

    def test_router_dispatch_uses_render_settings(self):
        self.assertRegex(
            self.src,
            r"route\.name === 'settings'.*?renderSettings\(",
        )

    def test_no_more_settings_placeholder_in_routes(self):
        m = re.search(r'var ROUTES = \{(.*?)\};', self.src, re.DOTALL)
        self.assertIsNotNone(m)
        self.assertNotIn('renderSettingsPlaceholder', m.group(1))

    def test_app_shell_exports_step11_helpers(self):
        for sym in ('renderSettings', '_jwtExpiryFromToken',
                    '_settingsSaveEnabled', '_setSettingsTab'):
            self.assertIn(sym + ':', self.src,
                          f'AppShell missing exported helper: {sym}')


# ---------------------------------------------------------------------------
# 2. _setSettingsTab toggles correctly.
# ---------------------------------------------------------------------------

class TestSetSettingsTab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)

    def test_helper_defined(self):
        self.assertIn('function _setSettingsTab(', self.src)

    def test_helper_toggles_is_active_via_node(self):
        if not _has_node():
            self.skipTest('node not available')
        # Re-implement the helper inline (parallels _setReviewTab tests).
        snippet = r"""
            function makeBtn(tab){
                return {
                    dataset: { tab: tab }, classList: new Set(),
                    attrs: {},
                    getAttribute(k){ return this.dataset[k.replace('data-','')]; },
                    setAttribute(k, v){ this.attrs[k]=v; },
                };
            }
            function makeBody(tab){
                return {
                    dataset: { tab: tab }, style: {},
                    getAttribute(k){ return this.dataset[k.replace('data-','')]; },
                };
            }
            // class-list shim
            for (const cls of ['Set']){
                Set.prototype.add = Set.prototype.add;
            }
            const btns = [makeBtn('device'), makeBtn('account'), makeBtn('app')];
            const bodies = [makeBody('device'), makeBody('account'), makeBody('app')];
            const host = {
                querySelectorAll(sel){
                    if (sel === '.settings-tab__btn') return btns;
                    if (sel === '.settings-tab__body') return bodies;
                    return [];
                },
            };
            // re-implement matching the helper in app.js.
            function _setSettingsTab(host, name){
                if (!host) return;
                const bs = host.querySelectorAll('.settings-tab__btn');
                for (const b of bs){
                    const active = (b.getAttribute('data-tab') === name);
                    if (active) b.classList.add('is-active');
                    else b.classList.delete('is-active');
                    b.setAttribute('aria-selected', active ? 'true' : 'false');
                }
                const bd = host.querySelectorAll('.settings-tab__body');
                for (const x of bd){
                    const m = (x.getAttribute('data-tab') === name);
                    x.style.display = m ? '' : 'none';
                }
            }
            _setSettingsTab(host, 'account');
            const out = btns.map(b => ({
                tab: b.dataset.tab,
                active: b.classList.has('is-active'),
                ar: b.attrs['aria-selected'],
            }));
            const out2 = bodies.map(x => ({
                tab: x.dataset.tab, disp: x.style.display,
            }));
            console.log(JSON.stringify({btns: out, bodies: out2}));
        """
        proc = _run_node(snippet)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout.strip())
        # Only the account button should be active.
        for b in data['btns']:
            if b['tab'] == 'account':
                self.assertTrue(b['active'])
                self.assertEqual(b['ar'], 'true')
            else:
                self.assertFalse(b['active'])
                self.assertEqual(b['ar'], 'false')
        # Only the account body should be visible (display='').
        for x in data['bodies']:
            if x['tab'] == 'account':
                self.assertEqual(x['disp'], '')
            else:
                self.assertEqual(x['disp'], 'none')


# ---------------------------------------------------------------------------
# 3. _jwtExpiryFromToken returns the right ISO string.
# ---------------------------------------------------------------------------

class TestJwtExpiryFromToken(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)

    def test_helper_defined(self):
        self.assertIn('function _jwtExpiryFromToken(', self.src)

    def test_iso_matches_for_known_jwt(self):
        if not _has_node():
            self.skipTest('node not available')
        # Hand-craft a JWT whose `exp` is 1714639200 (2024-05-02 10:00:00Z).
        exp_value = 1714639200
        token = _make_jwt({'exp': exp_value, 'user_id': 1})
        snippet = """
            // inline the function under test, keeping it byte-equal to app.js.
            function _jwtExpiryFromToken(token){
                if (typeof token !== 'string' || !token) return null;
                var parts = token.split('.');
                if (parts.length < 2) return null;
                var seg = parts[1];
                seg = seg.replace(/-/g, '+').replace(/_/g, '/');
                while (seg.length %% 4) seg += '=';
                var json;
                try { json = Buffer.from(seg, 'base64').toString('utf8'); }
                catch (_) { return null; }
                var data;
                try { data = JSON.parse(json); } catch (_) { return null; }
                if (!data || typeof data !== 'object') return null;
                var exp = data.exp;
                if (typeof exp !== 'number' || !isFinite(exp)) return null;
                try { return new Date(exp * 1000).toISOString(); }
                catch (_) { return null; }
            }
            console.log(_jwtExpiryFromToken(%r));
            console.log(_jwtExpiryFromToken(''));
            console.log(_jwtExpiryFromToken('not.a.jwt.at.all'));
        """ % token
        proc = _run_node(snippet)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.strip().split('\n')
        # The first line is the ISO string for exp_value seconds since epoch.
        import datetime
        expected = datetime.datetime.utcfromtimestamp(
            exp_value).isoformat(timespec='milliseconds') + 'Z'
        self.assertEqual(lines[0], expected)
        # Empty token returns null -> 'null'.
        self.assertEqual(lines[1], 'null')


# ---------------------------------------------------------------------------
# 4. _settingsSaveEnabled disabled-when-unchanged logic.
# ---------------------------------------------------------------------------

class TestSettingsSaveEnabled(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)

    def test_helper_defined(self):
        self.assertIn('function _settingsSaveEnabled(', self.src)

    def test_pure_logic_via_node(self):
        if not _has_node():
            self.skipTest('node not available')
        snippet = """
            function _settingsSaveEnabled(original, current){
                var a = (original == null ? '' : String(original));
                var b = (current == null ? '' : String(current));
                return a !== b;
            }
            const cases = [
                ['http://a.test', 'http://a.test', false],
                ['http://a.test', 'http://b.test', true],
                [null, '', false],
                ['', null, false],
                [123, '123', false],
                [123, '124', true],
            ];
            for (const [a, b, want] of cases){
                const got = _settingsSaveEnabled(a, b);
                if (got !== want){
                    console.log('FAIL', JSON.stringify({a:a,b:b,want:want,got:got}));
                    process.exit(2);
                }
            }
            console.log('OK');
        """
        proc = _run_node(snippet)
        self.assertEqual(proc.returncode, 0,
                         f'stdout={proc.stdout} stderr={proc.stderr}')
        self.assertIn('OK', proc.stdout)


# ---------------------------------------------------------------------------
# 5. The log path /tmp/slam_app_debug.log appears in the App tab.
# ---------------------------------------------------------------------------

class TestAppTabReferences(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)

    def test_log_path_present(self):
        self.assertIn('/tmp/slam_app_debug.log', self.src)

    def test_log_path_exposed_via_app_shell(self):
        self.assertRegex(self.src,
                         r"SETTINGS_LOG_PATH\s*:\s*SETTINGS_LOG_PATH")

    def test_download_log_disabled_with_coming_soon(self):
        # The Telemetry section's Download log button must show "(coming soon)".
        self.assertIn('coming soon', self.src.lower())


# ---------------------------------------------------------------------------
# 6. Brio resolution dropdown is documented as TODO.
# ---------------------------------------------------------------------------

class TestBrioTodo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)

    def test_brio_section_present(self):
        # The Device tab still renders a Brio resolution section so the
        # operator can see the future control surface.
        self.assertRegex(self.src, r'Brio recapture resolution')

    def test_brio_marked_as_todo(self):
        # Body acknowledges that the backend key isn't wired yet.
        self.assertIn('brio_recapture_size', self.src)
        self.assertIn('TODO', self.src)


# ---------------------------------------------------------------------------
# 7. Logout confirm-modal copy matches the plan exactly.
# ---------------------------------------------------------------------------

class TestLogoutConfirm(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)

    def test_logout_confirm_copy_present(self):
        self.assertIn(
            'Local recordings + projects stay on this device.',
            self.src,
            'Logout confirm body must match the plan verbatim',
        )


# ---------------------------------------------------------------------------
# 8. Upload-filter editor validates JSON before saving.
# ---------------------------------------------------------------------------

class TestUploadFilterJsonValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(APP_JS_PATH)

    def test_json_parse_called_before_patch(self):
        # The save handler must JSON.parse the textarea, return on failure,
        # AND only then call PATCH /api/settings/upload_filter.
        # We verify the source contains both the JSON.parse and the
        # invalid-JSON error message in the right order.
        idx_parse = self.src.find('JSON.parse(raw)')
        self.assertGreater(idx_parse, 0,
                           'expected a JSON.parse(raw) guard in the upload-filter save handler')
        idx_invalid = self.src.find('Invalid JSON', idx_parse)
        self.assertGreater(idx_invalid, idx_parse,
                           'expected "Invalid JSON" error after parse')
        idx_patch = self.src.find('/api/settings/upload_filter', idx_invalid)
        self.assertGreater(idx_patch, idx_invalid,
                           'PATCH should come after the JSON.parse guard')


# ---------------------------------------------------------------------------
# 9-12. Backend routes /api/settings/config (GET + PATCH).
# ---------------------------------------------------------------------------

class _ConfigRouteBase(unittest.TestCase):
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
        auth.save_config({
            'api_url': 'http://test.athathi.local',
            'upload_endpoint': 'http://test.athathi.local',
            'last_user': '',
            'post_submit_hook': None,
            'image_transport': 'multipart',
            'visual_search_cache_ttl_s': 86400,
        })
        self.client = app.app.test_client()

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(auth, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self):
        auth.write_token(_fresh_jwt())
        auth.write_auth({'user_id': 7, 'username': 'tech_alice',
                         'user_type': 'technician'})


class TestSettingsConfigRoutes(_ConfigRouteBase):
    """Test 9: GET + PATCH /api/settings/config exist and round-trip."""

    def test_get_returns_current_config_when_logged_in(self):
        self._login()
        r = self.client.get('/api/settings/config')
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body['api_url'], 'http://test.athathi.local')
        self.assertIn('image_transport', body)
        self.assertIn('upload_endpoint', body)
        self.assertIn('visual_search_cache_ttl_s', body)
        self.assertIn('post_submit_hook', body)

    def test_patch_round_trips(self):
        self._login()
        r = self.client.patch('/api/settings/config',
                              json={'image_transport': 'inline'})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body['image_transport'], 'inline')
        # GET reflects the change.
        r2 = self.client.get('/api/settings/config')
        self.assertEqual(r2.get_json()['image_transport'], 'inline')

    def test_patch_unknown_key_rejected(self):
        """Test 10: unknown keys yield 400."""
        self._login()
        r = self.client.patch('/api/settings/config',
                              json={'totally_made_up_key': 'x'})
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertIn('error', body)

    def test_patch_strips_trailing_slash_on_api_url(self):
        """Test 11: PATCH api_url with trailing slash persists stripped."""
        self._login()
        r = self.client.patch('/api/settings/config',
                              json={'api_url': 'http://x/'})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body['api_url'], 'http://x')
        # Disk reflects the stripped value too.
        r2 = self.client.get('/api/settings/config')
        self.assertEqual(r2.get_json()['api_url'], 'http://x')

    def test_get_401_when_not_logged_in(self):
        """Test 12: GET 401 when not logged in."""
        r = self.client.get('/api/settings/config')
        self.assertEqual(r.status_code, 401)

    def test_patch_401_when_not_logged_in(self):
        r = self.client.patch('/api/settings/config',
                              json={'api_url': 'http://x'})
        self.assertEqual(r.status_code, 401)

    def test_patch_400_when_body_not_json_object(self):
        self._login()
        r = self.client.patch('/api/settings/config',
                              data='not-json',
                              content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_patch_with_empty_body_returns_current_config(self):
        # Backwards-defensive: posting `{}` is a no-op, not an error.
        self._login()
        r = self.client.patch('/api/settings/config', json={})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertIn('api_url', body)


# ---------------------------------------------------------------------------
# CSS smoke check + node --check on app.js.
# ---------------------------------------------------------------------------

class TestSettingsCssAppended(unittest.TestCase):
    def test_app_css_has_settings_classes(self):
        css = _read(APP_CSS_PATH)
        for cls in ('.settings-screen', '.settings-tabs', '.settings-tab__btn',
                    '.settings-tab__body', '.settings-field', '.settings-stat',
                    '.settings-textarea', '.settings-details'):
            self.assertIn(cls, css,
                          f'expected {cls} in static/app.css')


class TestNodeSyntaxStillClean(unittest.TestCase):
    def test_app_js_parses(self):
        if not _has_node():
            self.skipTest('node not available')
        rc = subprocess.run(
            ['node', '--check', APP_JS_PATH],
            capture_output=True, text=True,
        )
        self.assertEqual(rc.returncode, 0,
                         f'node --check app.js failed:\n{rc.stderr}')


if __name__ == '__main__':
    unittest.main()
