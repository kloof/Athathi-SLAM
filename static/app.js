/* ===========================================================
 * app.js — Athathi technician SPA shell.
 *
 * Step 8 laid the foundations (router, login, top bar, fetchJson).
 * Step 9 (this file) fills in three real screens:
 *   - #/projects                       — project list (My Schedule / History / Ad-hoc)
 *   - #/project/<id>                   — project workspace (customer info, scans)
 *   - #/project/<id>/scan/<name>       — scan workspace (recording + processing controls)
 *
 * No backend edits. Every fetch goes through `AppShell.fetchJson` (auth-gated).
 *
 * SSE bridge:
 *   The Step 9 scan workspace needs the live `/api/events` feed for the
 *   recording timer + processing stage. We open our OWN EventSource here
 *   (independent of LegacyApp.connectSSE) so the scan screen works without
 *   booting the legacy DOM. State is mirrored on `state.lastSse` and a
 *   `'sse-tick'` CustomEvent is dispatched on `window` for screens to
 *   subscribe to. Reconnects with a 3-second back-off on error.
 *
 * Globals exposed:
 *   window.AppShell — for tests + console debugging.
 * =========================================================== */

(function () {
    'use strict';

    // -----------------------------------------------------------------
    // 1. fetchJson helper
    // -----------------------------------------------------------------

    /**
     * Thin fetch wrapper.
     *  - method:  'GET' / 'POST' / 'PATCH' / 'DELETE'
     *  - path:    '/api/...'
     *  - body:    optional JSON-serialisable body
     *  - opts.skipAuthRedirect: don't auto-route to #/login on 401
     *
     * Resolves with the parsed JSON body (or null on 204).
     * Rejects with an Error whose `.status` and `.body` are set.
     */
    function fetchJson(method, path, body, opts) {
        opts = opts || {};
        var headers = { 'Accept': 'application/json' };
        var init = {
            method: method,
            credentials: 'same-origin',
            headers: headers,
        };
        if (body !== undefined && body !== null) {
            headers['Content-Type'] = 'application/json';
            init.body = JSON.stringify(body);
        }
        return fetch(path, init).then(function (res) {
            var ct = res.headers.get('content-type') || '';
            var parser = ct.indexOf('application/json') !== -1
                ? res.json().catch(function () { return null; })
                : res.text().then(function (t) { return t || null; });
            return parser.then(function (data) {
                if (res.ok) return data;

                if (res.status === 401 && !opts.skipAuthRedirect) {
                    // Drop to login on auth failure (ignored when caller
                    // handles 401 itself, e.g. the login form).
                    location.hash = '#/login';
                }
                var err = new Error(
                    (data && (data.error || data.message)) || ('HTTP ' + res.status)
                );
                err.status = res.status;
                err.body = data;
                throw err;
            });
        });
    }

    // -----------------------------------------------------------------
    // 2. Inline SVG icon set (plan §13: gear, log-out, etc.)
    // -----------------------------------------------------------------

    var ICONS = {
        gear: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="20" height="20"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
        logout: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="20" height="20"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>',
        warning: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
        back: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="22" height="22"><polyline points="15 18 9 12 15 6"/></svg>',
        refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="18" height="18"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>',
        plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="18" height="18"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>',
        chevron: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14"><polyline points="6 9 12 15 18 9"/></svg>',
    };

    // -----------------------------------------------------------------
    // 3. State + helpers
    // -----------------------------------------------------------------

    var state = {
        user: null,        // last `/api/auth/me` envelope
        currentRoute: null,
        legacyBooted: false,
        lastSse: null,     // last SSE payload {recording, elapsed, status, session_id, processing}
        // {scan_id_str: {result, fetched_at}} cache for done — reviewed scans
        resultCache: {},
        // Top bar override set per screen via setTopBar.
        topBar: null,
        // Per-screen poll timers keyed by purpose (so we can clear on nav).
        pollTimers: {},
    };

    function el(tag, attrs, children) {
        var node = document.createElement(tag);
        if (attrs) {
            for (var k in attrs) {
                if (!Object.prototype.hasOwnProperty.call(attrs, k)) continue;
                if (k === 'class') node.className = attrs[k];
                else if (k === 'html') node.innerHTML = attrs[k];
                else if (k === 'on' && attrs[k]) {
                    for (var ev in attrs[k]) {
                        if (Object.prototype.hasOwnProperty.call(attrs[k], ev)) {
                            node.addEventListener(ev, attrs[k][ev]);
                        }
                    }
                } else if (k === 'style') {
                    node.setAttribute('style', attrs[k]);
                } else if (k === 'disabled') {
                    if (attrs[k]) node.setAttribute('disabled', 'disabled');
                } else if (k.indexOf('data-') === 0 || k === 'role' || k === 'aria-label' || k === 'aria-disabled' || k === 'aria-expanded' || k === 'type' || k === 'placeholder' || k === 'value' || k === 'autocomplete' || k === 'name' || k === 'id' || k === 'href' || k === 'title' || k === 'for' || k === 'autocapitalize' || k === 'spellcheck' || k === 'inputmode' || k === 'maxlength' || k === 'pattern') {
                    node.setAttribute(k, attrs[k]);
                } else {
                    node[k] = attrs[k];
                }
            }
        }
        if (children) {
            if (!Array.isArray(children)) children = [children];
            for (var i = 0; i < children.length; i++) {
                var c = children[i];
                if (c == null) continue;
                if (typeof c === 'string') node.appendChild(document.createTextNode(c));
                else node.appendChild(c);
            }
        }
        return node;
    }

    var TOAST_TIMEOUT = null;
    function showToast(msg, variant) {
        var existing = document.querySelector('.toast');
        if (existing && existing.parentNode) existing.parentNode.removeChild(existing);
        if (TOAST_TIMEOUT) clearTimeout(TOAST_TIMEOUT);
        var t = el('div', { class: 'toast' + (variant ? ' toast--' + variant : '') }, msg);
        document.body.appendChild(t);
        // Force reflow for the entrance transition.
        // eslint-disable-next-line no-unused-expressions
        t.offsetWidth;
        t.classList.add('is-visible');
        TOAST_TIMEOUT = setTimeout(function () {
            t.classList.remove('is-visible');
            setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 250);
        }, 3000);
    }

    function clearScreenTimers() {
        for (var k in state.pollTimers) {
            if (Object.prototype.hasOwnProperty.call(state.pollTimers, k)) {
                clearInterval(state.pollTimers[k]);
                clearTimeout(state.pollTimers[k]);
            }
        }
        state.pollTimers = {};
    }

    // -----------------------------------------------------------------
    // 3b. Time + display helpers (plan §9 / §10)
    // -----------------------------------------------------------------

    function fmtMmSs(seconds) {
        if (typeof seconds !== 'number' || !isFinite(seconds) || seconds < 0) seconds = 0;
        var mins = Math.floor(seconds / 60);
        var secs = Math.floor(seconds % 60);
        return mins + ':' + (secs < 10 ? '0' + secs : '' + secs);
    }

    function _isoToDate(iso) {
        if (!iso || typeof iso !== 'string') return null;
        var d = new Date(iso);
        if (isNaN(d.getTime())) return null;
        return d;
    }

    function _hhmm(date) {
        var h = date.getHours();
        var m = date.getMinutes();
        return (h < 10 ? '0' + h : h) + ':' + (m < 10 ? '0' + m : m);
    }

    function fmtSlotRange(start, end) {
        var ds = _isoToDate(start);
        var de = _isoToDate(end);
        if (!ds && !de) {
            // Render whatever's there raw; preserves the empty case.
            var out = (start || '') + (start && end ? '–' : '') + (end || '');
            return out || '';
        }
        var label = '';
        if (ds) label += _hhmm(ds);
        if (ds && de) label += '–';
        if (de) label += _hhmm(de);
        // Day suffix: today / tomorrow / weekday.
        var ref = ds || de;
        if (ref) {
            var now = new Date();
            var refStart = new Date(ref.getFullYear(), ref.getMonth(), ref.getDate()).getTime();
            var todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
            var dayDiff = Math.round((refStart - todayStart) / (24 * 3600 * 1000));
            if (dayDiff === 0) label += ' today';
            else if (dayDiff === 1) label += ' tomorrow';
            else if (dayDiff === -1) label += ' yesterday';
            else if (dayDiff > 1 && dayDiff < 7) {
                var days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
                label += ' ' + days[ref.getDay()];
            } else {
                label += ' ' + ref.toLocaleDateString();
            }
        }
        return label;
    }

    function fmtRelative(iso) {
        var d = _isoToDate(iso);
        if (!d) return iso || '';
        var diffSec = (Date.now() - d.getTime()) / 1000;
        if (diffSec < 60) return 'just now';
        if (diffSec < 3600) return Math.floor(diffSec / 60) + ' min ago';
        if (diffSec < 86400) return Math.floor(diffSec / 3600) + ' h ago';
        var days = Math.floor(diffSec / 86400);
        if (days < 30) return days + (days === 1 ? ' day ago' : ' days ago');
        return d.toLocaleDateString();
    }

    // -----------------------------------------------------------------
    // 4. Top bar (configurable per screen)
    // -----------------------------------------------------------------

    /**
     * Per-screen top bar config. Called at the START of each render fn.
     *
     * shape: { title?: string, backHref?: string, backLabel?: string,
     *          showGear?: bool (default true), showLogout?: bool (default true) }
     */
    function setTopBar(cfg) {
        state.topBar = cfg || {};
    }

    function renderTopbar() {
        var cfg = state.topBar || {};
        var username = (state.user && state.user.username) || '';
        var children = [];

        if (cfg.backHref) {
            children.push(el('button', {
                class: 'topbar__btn topbar__btn--back',
                'aria-label': cfg.backLabel || 'Back',
                title: cfg.backLabel || 'Back',
                html: ICONS.back,
                on: { click: function () { location.hash = cfg.backHref; } },
            }));
        } else {
            children.push(el('img', {
                class: 'topbar__logo', src: '/static/logo.svg', 'aria-label': 'Athathi',
            }));
        }

        if (cfg.title) {
            children.push(el('span', { class: 'topbar__title topbar__title--screen' }, cfg.title));
        } else {
            children.push(el('span', { class: 'topbar__title' }, 'TECHNICIAN'));
        }

        children.push(el('span', { class: 'topbar__spacer' }));
        if (username) children.push(el('span', { class: 'topbar__user' }, username));

        if (cfg.showGear !== false) {
            children.push(el('button', {
                class: 'topbar__btn',
                'aria-label': 'Settings', title: 'Settings',
                html: ICONS.gear,
                on: { click: function () { location.hash = '#/settings'; } },
            }));
        }
        if (cfg.showLogout !== false) {
            children.push(el('button', {
                class: 'topbar__btn',
                'aria-label': 'Log out', title: 'Log out',
                html: ICONS.logout,
                on: { click: function () { handleLogout(); } },
            }));
        }

        return el('div', { class: 'topbar', role: 'banner' }, children);
    }

    function handleLogout() {
        fetchJson('POST', '/api/auth/logout', {}, { skipAuthRedirect: true })
            .catch(function () { /* logout is best-effort server-side */ })
            .then(function () {
                state.user = null;
                location.hash = '#/login';
            });
    }

    // -----------------------------------------------------------------
    // 5. Modal + confirm primitives
    // -----------------------------------------------------------------

    /**
     * Generic modal. Returns the host node; caller appends children.
     * `onClose` fires on backdrop click + Escape + close button.
     */
    function openModal(title, bodyChildren, onClose) {
        // Remove any existing modal (single-modal model).
        var existing = document.querySelector('.modal-host');
        if (existing && existing.parentNode) existing.parentNode.removeChild(existing);

        var content = el('div', { class: 'modal__content', role: 'document' });
        var header = el('div', { class: 'modal__header' }, [
            el('div', { class: 'modal__title' }, title || ''),
            el('button', {
                class: 'modal__close', 'aria-label': 'Close',
                on: { click: function () { closeModal(); } },
            }, '✕'),
        ]);
        content.appendChild(header);
        var body = el('div', { class: 'modal__body' });
        if (Array.isArray(bodyChildren)) {
            for (var i = 0; i < bodyChildren.length; i++) {
                if (bodyChildren[i]) body.appendChild(bodyChildren[i]);
            }
        } else if (bodyChildren) {
            body.appendChild(bodyChildren);
        }
        content.appendChild(body);

        var host = el('div', {
            class: 'modal-host', role: 'dialog', 'aria-label': title || 'Dialog',
            on: {
                click: function (e) {
                    if (e.target === host) {
                        if (onClose) onClose();
                        closeModal();
                    }
                },
            },
        }, content);

        function escHandler(e) {
            if (e.key === 'Escape') {
                if (onClose) onClose();
                closeModal();
            }
        }
        host._escHandler = escHandler;
        document.addEventListener('keydown', escHandler);

        document.body.appendChild(host);
        return { host: host, content: content, body: body, header: header };
    }

    function closeModal() {
        var host = document.querySelector('.modal-host');
        if (!host) return;
        if (host._escHandler) {
            document.removeEventListener('keydown', host._escHandler);
        }
        if (host.parentNode) host.parentNode.removeChild(host);
    }

    /**
     * Promise-based confirm. Returns Promise<bool>.
     *   confirm({title, body, confirmText?, cancelText?, danger?: bool})
     */
    function confirmDialog(opts) {
        opts = opts || {};
        return new Promise(function (resolve) {
            var resolved = false;
            function done(v) { if (resolved) return; resolved = true; closeModal(); resolve(v); }

            var bodyEl = el('div', { class: 'modal__text' }, opts.body || '');
            var confirmBtn = el('button', {
                class: opts.danger ? 'btn-danger btn-lg' : 'btn-primary btn-lg',
                on: { click: function () { done(true); } },
            }, opts.confirmText || 'Confirm');
            var cancelBtn = el('button', {
                class: 'btn-secondary btn-lg',
                on: { click: function () { done(false); } },
            }, opts.cancelText || 'Cancel');

            openModal(opts.title || 'Confirm', [
                bodyEl,
                el('div', { class: 'modal__actions' }, [cancelBtn, confirmBtn]),
            ], function () { done(false); });
        });
    }

    // -----------------------------------------------------------------
    // 6. Validators (snake_case scan name, etc.)
    // -----------------------------------------------------------------

    var _SCAN_RESERVED = ['runs', 'processed', 'rosbag', 'review', 'meta', '.', '..'];
    /**
     * Validate a scan name against the backend rules in
     * `projects.py::_validate_scan_name`. Returns null on OK, else a
     * human-readable error string.
     */
    function validateScanName(name) {
        if (typeof name !== 'string') return 'scan name must be a string';
        var s = name.trim();
        if (!s) return 'scan name must not be empty';
        if (s.length > 40) return 'scan name must be 1..40 chars';
        if (!/^[a-z0-9_]+$/.test(s)) {
            return 'scan name must be lowercase snake_case (a-z, 0-9, _)';
        }
        if (s.charAt(0) === '_') return 'scan name must not start with an underscore';
        if (_SCAN_RESERVED.indexOf(s) !== -1) return 'scan name "' + s + '" is reserved';
        return null;
    }

    // -----------------------------------------------------------------
    // 7. Login screen (unchanged from Step 8)
    // -----------------------------------------------------------------

    function renderLogin(opts) {
        opts = opts || {};
        var prefill = (state.user && state.user.last_user)
            || (state.user && state.user.username)
            || opts.prefillUsername
            || '';

        var errorBox;
        var formCard;
        var usernameInput, passwordInput, submitBtn;

        function setError(msg, shake) {
            if (errorBox) errorBox.textContent = msg || '';
            if (shake && formCard) {
                formCard.classList.remove('shake');
                // Force reflow to restart the animation.
                // eslint-disable-next-line no-unused-expressions
                formCard.offsetWidth;
                formCard.classList.add('shake');
            }
        }

        function onSubmit(e) {
            e.preventDefault();
            var u = (usernameInput.value || '').trim();
            var p = passwordInput.value || '';
            if (!u) { setError('Username required'); usernameInput.focus(); return; }
            if (!p) { setError('Password required'); passwordInput.focus(); return; }
            setError('');
            submitBtn.disabled = true;
            submitBtn.innerHTML = '<span class="spinner"></span>';

            fetchJson('POST', '/api/auth/login',
                { username: u, password: p },
                { skipAuthRedirect: true }
            ).then(function (data) {
                state.user = (data && data.user) || { username: u };
                location.hash = '#/projects';
            }).catch(function (err) {
                if (err.status === 401) {
                    setError('Wrong username or password', true);
                    passwordInput.value = '';
                    passwordInput.focus();
                } else if (err.status === 503 || !err.status) {
                    setError('Cannot reach Athathi server. Check API URL in Settings (gear icon).');
                } else {
                    setError(err.message || ('Login failed (' + err.status + ')'));
                }
            }).then(function () {
                submitBtn.disabled = false;
                submitBtn.textContent = 'Sign in';
            });
        }

        usernameInput = el('input', {
            class: 'input', id: 'login-username', name: 'username', type: 'text',
            autocomplete: 'username', autocapitalize: 'off', spellcheck: 'false',
            value: prefill, placeholder: 'Username',
        });
        passwordInput = el('input', {
            class: 'input', id: 'login-password', name: 'password', type: 'password',
            autocomplete: 'current-password', placeholder: 'Password',
        });
        submitBtn = el('button', {
            class: 'btn-primary btn-lg', type: 'submit',
        }, 'Sign in');

        errorBox = el('div', { class: 'login__error', role: 'alert' });

        formCard = el('form', {
            class: 'login__card', on: { submit: onSubmit },
        }, [
            el('img', { class: 'login__logo', src: '/static/logo.svg', alt: 'Athathi' }),
            el('div', { class: 'login__title' }, 'Sign in to continue'),
            el('div', { class: 'login__field' }, [
                el('label', { class: 'login__label', for: 'login-username' }, 'Username'),
                usernameInput,
            ]),
            el('div', { class: 'login__field' }, [
                el('label', { class: 'login__label', for: 'login-password' }, 'Password'),
                passwordInput,
            ]),
            errorBox,
            submitBtn,
            el('div', { class: 'login__footer' }, [
                el('span', null, ''),  // left spacer for symmetric layout
                el('button', {
                    class: 'login__settings-btn', type: 'button',
                    'aria-label': 'Settings', title: 'Settings',
                    html: ICONS.gear,
                    on: { click: function () { location.hash = '#/settings'; } },
                }),
            ]),
        ]);

        var screen = el('div', { class: 'login', id: 'login-screen' }, formCard);

        // Auto-focus the empty field once mounted.
        setTimeout(function () {
            (prefill ? passwordInput : usernameInput).focus();
        }, 0);

        return screen;
    }

    // -----------------------------------------------------------------
    // 8. Projects screen (#/projects)  (Step 9a)
    // -----------------------------------------------------------------

    function _projectStatusPill(p) {
        // §9: submit_pending -> warn; fully reviewed -> success; partial -> body.
        var roomsLocal = parseInt(p.rooms_local || 0, 10) || 0;
        var roomsReviewed = parseInt(p.rooms_reviewed || 0, 10) || 0;
        if (p.submit_pending) {
            // Step 12 §8: "↻ Sync pending" with the warn color so the
            // technician can spot the queued projects at a glance.
            return { label: '↻ sync pending', variant: 'warn' };
        }
        if (p.submitted_at) {
            return { label: 'submitted', variant: 'success' };
        }
        if (roomsLocal > 0 && roomsReviewed >= roomsLocal) {
            return { label: 'reviewed', variant: 'success' };
        }
        if (roomsReviewed > 0) {
            return {
                label: roomsReviewed + '/' + roomsLocal + ' reviewed',
                variant: 'body',
            };
        }
        if (roomsLocal > 0) {
            return { label: roomsLocal + ' scans', variant: 'body' };
        }
        return { label: '0 scans', variant: 'body' };
    }

    function _projectCard(p) {
        var sid = p.scan_id;
        var customer = p.customer_name || '(unnamed customer)';
        var slot = fmtSlotRange(p.slot_start, p.slot_end);
        var pill = _projectStatusPill(p);

        var headRow = el('div', { class: 'project-card__head' }, [
            el('span', { class: 'project-card__id' }, '#' + sid),
            el('span', { class: 'project-card__name' }, customer),
        ]);

        var subRow = el('div', { class: 'project-card__sub' }, [
            slot ? el('span', { class: 'project-card__slot' }, slot) : null,
            el('span', { class: 'project-card__spacer' }),
            el('span', {
                class: 'pill pill--' + pill.variant,
            }, pill.label),
        ]);

        var metaParts = [];
        if (typeof p.rooms_local === 'number') {
            metaParts.push(p.rooms_local + (p.rooms_local === 1 ? ' scan' : ' scans'));
        }
        if (p.address) metaParts.push(p.address);
        var metaRow = metaParts.length
            ? el('div', { class: 'project-card__meta' }, metaParts.join(' · '))
            : null;

        return el('button', {
            class: 'project-card', type: 'button',
            on: {
                click: function () {
                    location.hash = '#/project/' + encodeURIComponent(sid);
                },
            },
        }, [headRow, subRow, metaRow]);
    }

    function _projectHistoryCard(p) {
        var sid = p.scan_id;
        var customer = p.customer_name || '(unnamed customer)';
        var sub = '';
        if (p.submitted_at) sub = 'submitted ' + fmtRelative(p.submitted_at);
        else if (p.completed_at) sub = 'completed ' + fmtRelative(p.completed_at);

        return el('button', {
            class: 'project-card project-card--compact', type: 'button',
            on: {
                click: function () {
                    location.hash = '#/project/' + encodeURIComponent(sid);
                },
            },
        }, [
            el('div', { class: 'project-card__head' }, [
                el('span', { class: 'project-card__id' }, '#' + sid),
                el('span', { class: 'project-card__name' }, customer),
            ]),
            sub ? el('div', { class: 'project-card__sub' }, sub) : null,
        ]);
    }

    function _section(title, count, body, opts) {
        opts = opts || {};
        var collapsed = !!opts.collapsedDefault;
        var bodyWrap = el('div',
            { class: 'section__body' + (collapsed ? ' is-collapsed' : '') }, body);

        var head = el('button', {
            class: 'section__head', type: 'button', 'aria-expanded': collapsed ? 'false' : 'true',
            on: {
                click: function () {
                    var expanded = !bodyWrap.classList.contains('is-collapsed');
                    if (expanded) {
                        bodyWrap.classList.add('is-collapsed');
                        head.setAttribute('aria-expanded', 'false');
                    } else {
                        bodyWrap.classList.remove('is-collapsed');
                        head.setAttribute('aria-expanded', 'true');
                    }
                },
            },
        }, [
            el('span', { class: 'section__title' }, title),
            count != null ? el('span', { class: 'section__count' }, '(' + count + ')') : null,
            el('span', { class: 'section__chevron', html: ICONS.chevron }),
            opts.right || null,
        ]);

        return el('div', { class: 'section' }, [head, bodyWrap]);
    }

    /**
     * Step 12: walk a /api/projects envelope and return the list of
     * projects whose `submit_pending` flag is truthy. Used to drive the
     * sync-pending banner + retry sweep on the projects screen.
     */
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

    /**
     * Step 12b: per-project retry sweep. POSTs `/api/project/<id>/submit/retry`
     * for every pending project. The backend route is global (it walks every
     * pending manifest regardless of the URL scan_id) — but we still call it
     * per-id so we can show a per-project toast on success and so the next
     * /api/projects refresh reflects the new state.
     *
     * Returns Promise<{cleared: [scan_id...]}> after all retries complete.
     * Always resolves; per-project errors are swallowed (the next sweep
     * tries again).
     */
    function _runSubmitRetrySweep(pending) {
        if (!Array.isArray(pending) || !pending.length) {
            return Promise.resolve({ cleared: [], failed: [] });
        }
        var cleared = [];
        var failed = [];
        var any401 = false;
        var work = pending.map(function (p) {
            var sid = p.scan_id;
            return fetchJson(
                'POST',
                '/api/project/' + encodeURIComponent(sid) + '/submit/retry'
            ).then(function (res) {
                // Per submit_pending_retry: res.results = [{scan_id, status,
                // error?}, ...]. status is 'submitted' / 'already_submitted'
                // / 'failed' / 'skipped_no_token'. We treat the first two as
                // "cleared" — the project no longer needs the technician's
                // attention.
                var ok = false;
                var results = (res && res.results) || [];
                var done = { 'submitted': 1, 'already_submitted': 1, 'ok': 1 };
                for (var i = 0; i < results.length; i++) {
                    if (parseInt(results[i].scan_id, 10) === parseInt(sid, 10)
                            && (done[results[i].status] || results[i].ok)) {
                        ok = true;
                        break;
                    }
                }
                if (ok) {
                    cleared.push(sid);
                    showToast('Project #' + sid + ' synced ✓', 'success');
                } else {
                    failed.push(sid);
                }
            }).catch(function (err) {
                // JS-6: track failure so we can surface it after the loop.
                failed.push(sid);
                if (err && err.status === 401) any401 = true;
            });
        });
        return Promise.all(work).then(function () {
            // JS-6: if any project still failed after the sweep, surface a
            // single aggregated toast and direct the user to Settings.
            if (failed.length) {
                var nWord = failed.length === 1 ? 'project' : 'projects';
                showToast(failed.length + ' ' + nWord + ' failed to sync — check Settings', 'warn');
                if (any401) {
                    try { location.hash = '#/login'; } catch (_) { /* ignore */ }
                }
            }
            return { cleared: cleared, failed: failed };
        });
    }

    function renderProjects() {
        setTopBar({});  // default top bar (logo + TECHNICIAN)
        var screen = el('div', { class: 'screen', id: 'projects-screen' });

        var bannerHost = el('div', { class: 'banner-host' });
        var contentHost = el('div', { class: 'projects-content' });
        var loading = el('div', { class: 'loading-row' }, [
            el('span', { class: 'spinner' }), el('span', null, 'Loading projects...'),
        ]);
        contentHost.appendChild(loading);

        screen.appendChild(bannerHost);
        screen.appendChild(contentHost);

        function renderEnvelope(env) {
            bannerHost.innerHTML = '';
            contentHost.innerHTML = '';

            if (env && env.cached) {
                // Prefer `fetched_at` (the actual cache-fetch time from the
                // backend); fall back to `now` if the backend hasn't been
                // upgraded yet (forward-compat).
                var bannerTs = env.fetched_at || env.now;
                var ts = fmtRelative(bannerTs) || '';
                bannerHost.appendChild(el('div', { class: 'cache-banner' },
                    'Showing cached data — last refreshed ' + ts));
            }

            // Step 12b: sync-pending banner. Surfaces queued submits and
            // gives the technician a manual "Retry now" affordance.
            var pending = _submitPendingProjects(env);
            if (pending.length > 0) {
                var nWord = pending.length === 1 ? 'project' : 'projects';
                var verb = pending.length === 1 ? 'is' : 'are';
                var bannerText = pending.length + ' ' + nWord + ' waiting to sync — '
                    + 'Athathi unreachable. ';
                // unused-variable suppression
                void verb;
                var retryLink = el('button', {
                    class: 'sync-banner__retry', type: 'button',
                    on: {
                        click: function (e) {
                            e.stopPropagation();
                            retryLink.disabled = true;
                            _runSubmitRetrySweep(pending).then(function () {
                                retryLink.disabled = false;
                                loadProjects();
                            });
                        },
                    },
                }, 'Retry now');
                bannerHost.appendChild(el('div', {
                    class: 'sync-banner', role: 'status',
                }, [
                    el('span', { class: 'sync-banner__icon' }, '↻'),
                    el('span', { class: 'sync-banner__text' }, bannerText),
                    retryLink,
                ]));
            }

            // Manage the auto-retry timer: install if there's pending work
            // AND the screen is still mounted, tear down otherwise.
            if (pending.length > 0) {
                if (!state.pollTimers.submitRetry) {
                    state.pollTimers.submitRetry = setInterval(function () {
                        // Re-derive pending each tick so a successful sweep
                        // shrinks the work list naturally.
                        fetchJson('GET', '/api/projects').then(function (env2) {
                            var pend2 = _submitPendingProjects(env2 || {});
                            if (!pend2.length) {
                                if (state.pollTimers.submitRetry) {
                                    clearInterval(state.pollTimers.submitRetry);
                                    delete state.pollTimers.submitRetry;
                                }
                                renderEnvelope(env2 || {});
                                return;
                            }
                            _runSubmitRetrySweep(pend2).then(function (out) {
                                if (out.cleared.length) loadProjects();
                            });
                        }).catch(function () { /* next tick */ });
                    }, 60000);
                }
            } else if (state.pollTimers.submitRetry) {
                clearInterval(state.pollTimers.submitRetry);
                delete state.pollTimers.submitRetry;
            }

            var scheduled = (env && Array.isArray(env.scheduled)) ? env.scheduled.slice() : [];
            var history = (env && Array.isArray(env.history)) ? env.history.slice() : [];
            var adHoc = (env && Array.isArray(env.ad_hoc)) ? env.ad_hoc.slice() : [];

            // History sort: submitted_at desc.
            history.sort(function (a, b) {
                var aa = a.submitted_at || a.completed_at || '';
                var bb = b.submitted_at || b.completed_at || '';
                if (aa === bb) return 0;
                return aa > bb ? -1 : 1;
            });

            // ---- My Schedule ----
            var refreshBtn = el('button', {
                class: 'section__action', type: 'button',
                'aria-label': 'Refresh',
                on: {
                    click: function (e) {
                        e.stopPropagation();
                        loadProjects();
                    },
                },
            }, [el('span', { class: 'section__action-icon', html: ICONS.refresh }),
                el('span', null, 'Refresh')]);

            var scheduleBody;
            if (!scheduled.length) {
                scheduleBody = el('div', { class: 'empty-state' }, [
                    el('div', { class: 'empty-state__label' }, 'No scans scheduled.'),
                    el('div', { class: 'empty-state__hint' },
                        'Pull down to refresh, or wait for a new assignment.'),
                    el('button', {
                        class: 'btn-primary', type: 'button',
                        on: { click: function () { loadProjects(); } },
                    }, 'Refresh'),
                ]);
            } else {
                scheduleBody = el('div', { class: 'project-list' },
                    scheduled.map(_projectCard));
            }

            contentHost.appendChild(_section(
                'My schedule', scheduled.length || null, scheduleBody,
                { right: refreshBtn }
            ));

            // ---- History ----
            contentHost.appendChild(_section(
                'History', history.length, history.length
                    ? el('div', { class: 'project-list' }, history.map(_projectHistoryCard))
                    : el('div', { class: 'empty-state' }, [
                        el('div', { class: 'empty-state__label' }, 'No history yet.'),
                    ]),
                { collapsedDefault: true }
            ));

            // ---- Ad-hoc ----
            contentHost.appendChild(_section(
                'Ad-hoc', adHoc.length, adHoc.length
                    ? el('div', { class: 'project-list' }, adHoc.map(_projectCard))
                    : el('div', { class: 'empty-state' }, [
                        el('div', { class: 'empty-state__label' }, 'No ad-hoc projects.'),
                    ]),
                { collapsedDefault: true }
            ));
        }

        function loadProjects() {
            contentHost.innerHTML = '';
            contentHost.appendChild(loading.cloneNode(true));
            fetchJson('GET', '/api/projects').then(function (env) {
                renderEnvelope(env || {});
            }).catch(function (err) {
                contentHost.innerHTML = '';
                contentHost.appendChild(el('div', { class: 'empty-state' }, [
                    el('div', { class: 'empty-state__label' },
                        'Could not load projects'),
                    el('div', { class: 'empty-state__hint' },
                        err.message || 'Network error'),
                    el('button', {
                        class: 'btn-primary', type: 'button',
                        on: { click: function () { loadProjects(); } },
                    }, 'Retry'),
                ]));
            });
        }

        loadProjects();
        return screen;
    }

    // -----------------------------------------------------------------
    // 9. Project workspace (#/project/<scan_id>)  (Step 9b)
    // -----------------------------------------------------------------

    /**
     * Plan §22c gating priorities. Returns null if Submit is allowed,
     * else a short reason string. Mirrors `submit.gating_message` shape
     * but driven from projects/scan listings (no result.json access).
     *
     * Inputs:
     *   project = manifest dict (with rooms_local, rooms_reviewed, submitted_at, submit_pending)
     *   scans   = list_scans(project.scan_id) result, or null if not yet fetched
     *
     * Priority order:
     *   1. already submitted        → Already submitted on <date>
     *   2. still processing         → <scan> is still processing
     *   3. error                    → <scan> failed — re-process or delete
     *   4. not reviewed yet         → <scan> not reviewed yet
     *   5. no network               → No network — Submit requires Athathi connection
     */
    function _submitGatingMessage(project, scans, processingMap) {
        if (!project) return 'Project not loaded yet';
        if (project.submitted_at) {
            return 'Already submitted on ' + project.submitted_at;
        }
        var sList = Array.isArray(scans) ? scans : [];

        // Priority 2: still-processing.
        var procMap = processingMap || {};
        for (var i = 0; i < sList.length; i++) {
            var sName = sList[i].name;
            for (var sid in procMap) {
                if (!Object.prototype.hasOwnProperty.call(procMap, sid)) continue;
                var p = procMap[sid] || {};
                // The SSE feed doesn't currently expose scan_name on each
                // entry, but session names use `<scan_id>__<scan_name>`. We
                // match by suffix when present.
                if (p.scan_name === sName) {
                    return sName + ' is still processing';
                }
            }
            if (sList[i].status === 'processing') {
                return sName + ' is still processing';
            }
        }

        // Priority 3: error.
        for (var k = 0; k < sList.length; k++) {
            if (sList[k].status === 'error') {
                return sList[k].name + ' failed — re-process or delete';
            }
        }

        // Priority 4: not reviewed yet.
        for (var j = 0; j < sList.length; j++) {
            if (!sList[j].reviewed) {
                return sList[j].name + ' not reviewed yet';
            }
        }

        // Defensive: empty project.
        if (!sList.length) return 'no scans in project';

        // Priority 5: offline. Browser-side hint via navigator.onLine — the
        // backend route still re-checks before actually submitting, but
        // surfacing the message here keeps the disabled-button tooltip
        // honest.
        if (typeof navigator !== 'undefined' && navigator.onLine === false) {
            return 'No network — Submit requires Athathi connection';
        }

        return null;
    }

    function _scanRowStatusLabel(scan, sse) {
        // Plan §10/§11: idle / recording / compressing / uploading /
        // stage_5_infer / done — review pending / reviewing / reviewed /
        // error / submit_pending.
        if (!scan) return 'idle';
        var name = scan.name;

        // Live recording match: SSE recording session + project_scoped + same scan.
        if (sse && sse.recording && sse.session_id && sse.scanName === name) {
            return 'recording ' + fmtMmSs(sse.elapsed || 0);
        }

        var procMap = (sse && sse.processing) || {};
        for (var sid in procMap) {
            if (!Object.prototype.hasOwnProperty.call(procMap, sid)) continue;
            var p = procMap[sid] || {};
            if (p.scan_name === name) {
                var stage = p.stage || p.status || 'processing';
                var elapsed = p.elapsed ? ' ' + fmtMmSs(p.elapsed) : '';
                return stage + elapsed;
            }
        }

        if (scan.status === 'error') return 'error';
        if (scan.reviewed) return 'done — reviewed';
        if (scan.has_rosbag && scan.active_run_id) return 'done — review pending';
        if (scan.has_rosbag) return 'recorded';
        return 'idle';
    }

    /**
     * Step 12a: Submit-project modal.
     *
     *   _openSubmitModal(scanId, project, scans, onAfterSuccess?)
     *
     * Walks three stages inside a single modal:
     *   1. Confirm   — show customer + scan count + an upload-size preview
     *                  (best-effort; preview failure does NOT block).
     *   2. In-flight — spinner + multi-phase status walker (rendering →
     *                  uploading → completing → hook). The backend route
     *                  is synchronous from the client's perspective, so the
     *                  walker is a cosmetic timeline that advances on a
     *                  small interval until the POST resolves.
     *   3. Result    — terminal state: ✓ submitted, ↻ queued (closes +
     *                  shows a sync-pending toast), or ✕ error (with retry
     *                  button + upstream tail when available).
     *
     * `onAfterSuccess` fires after the modal closes on a successful submit
     * so the caller can refresh its state.
     */
    function _openSubmitModal(scanId, project, scans, onAfterSuccess) {
        scans = Array.isArray(scans) ? scans : [];
        var scanNames = scans.map(function (s) { return s.name; });
        var customer = (project && project.customer_name) || '(unnamed customer)';

        var bodyHost = el('div', { class: 'submit-modal__body' });
        var modal = openModal(
            'Submit project #' + scanId,
            [bodyHost],
            function () { clearPhaseTimer(); }
        );

        // ------- Stage 1: Confirm -------
        function renderConfirm() {
            bodyHost.innerHTML = '';

            var scanLine = scans.length
                ? scans.length + ' — ' + scanNames.join(', ')
                : '0 scans';

            var previewLine = el('div', {
                class: 'submit-modal__preview', 'data-role': 'preview',
            }, 'Calculating upload size...');

            bodyHost.appendChild(el('div', { class: 'submit-modal__kv' }, [
                el('div', { class: 'submit-modal__kv-row' }, [
                    el('div', { class: 'submit-modal__kv-label' }, 'Customer:'),
                    el('div', { class: 'submit-modal__kv-value' }, customer),
                ]),
                el('div', { class: 'submit-modal__kv-row' }, [
                    el('div', { class: 'submit-modal__kv-label' }, 'Scans:'),
                    el('div', { class: 'submit-modal__kv-value' }, scanLine),
                ]),
            ]));
            bodyHost.appendChild(previewLine);
            bodyHost.appendChild(el('div', { class: 'modal__text' },
                'Once submitted, the assignment is marked complete in Athathi. Continue?'));

            var cancelBtn = el('button', {
                class: 'btn-secondary btn-lg',
                on: { click: function () { closeModal(); } },
            }, 'Cancel');
            var submitBtn = el('button', {
                class: 'btn-primary btn-lg',
                on: { click: function () { runSubmit(); } },
            }, 'Submit');
            bodyHost.appendChild(el('div', { class: 'modal__actions' },
                [cancelBtn, submitBtn]));

            // Best-effort preview. Failure → just remove the line.
            fetchJson('GET',
                '/api/project/' + encodeURIComponent(scanId) + '/submit/preview'
            ).then(function (env) {
                var sList = (env && Array.isArray(env.scans)) ? env.scans : [];
                var totalBytes = 0, totalImages = 0;
                for (var i = 0; i < sList.length; i++) {
                    var s = sList[i] || {};
                    if (typeof s.upload_size === 'number') totalBytes += s.upload_size;
                    if (typeof s.n_images === 'number') totalImages += s.n_images;
                }
                var mb = (totalBytes / (1024 * 1024)).toFixed(1);
                previewLine.textContent = sList.length + ' scans, '
                    + mb + ' MB to upload (' + totalImages + ' images)';
            }).catch(function () {
                if (previewLine.parentNode) {
                    previewLine.parentNode.removeChild(previewLine);
                }
            });
        }

        // ------- Stage 2: In-flight -------
        function renderInflight() {
            bodyHost.innerHTML = '';

            var phases = [
                'rendering reviewed envelopes...',
                'uploading scan bundles...',
                'completing scan with Athathi...',
                'running post-submit hook...',
            ];
            var stepEl = el('div', { class: 'submit-modal__phase' }, phases[0]);
            bodyHost.appendChild(el('div', { class: 'submit-modal__inflight' }, [
                el('span', { class: 'spinner spinner--lg' }),
                stepEl,
            ]));

            // Backend is synchronous — we walk the phases on a cosmetic
            // timer. A real implementation could subscribe to SSE, but the
            // submit pipeline doesn't currently emit per-phase events.
            var idx = 0;
            var timer = setInterval(function () {
                if (idx < phases.length - 1) {
                    idx += 1;
                    stepEl.textContent = phases[idx];
                }
            }, 1500);
            // JS-7: track via state.pollTimers so clearScreenTimers() cleans
            // it up on hash navigation.
            state.pollTimers.submitPhase = timer;
        }

        function clearPhaseTimer() {
            if (state.pollTimers.submitPhase) {
                clearInterval(state.pollTimers.submitPhase);
                delete state.pollTimers.submitPhase;
            }
        }

        // ------- Stage 3: Result -------
        function renderSuccess(payload) {
            clearPhaseTimer();
            bodyHost.innerHTML = '';
            var when = (payload && payload.completed_at)
                || (payload && payload.already_submitted_at)
                || '';
            var line = when
                ? '✓ Submitted on ' + when
                : '✓ Submitted';
            bodyHost.appendChild(el('div', {
                class: 'submit-modal__result submit-modal__result--ok',
            }, line));
            var hookStatus = payload && payload.post_submit_hook_status;
            if (hookStatus === 'failed') {
                bodyHost.appendChild(el('div', { class: 'submit-modal__hint' },
                    'Note: post-submit hook failed; resubmit re-runs the hook.'));
            }
            var doneBtn = el('button', {
                class: 'btn-primary btn-lg',
                on: {
                    click: function () {
                        closeModal();
                        if (typeof onAfterSuccess === 'function') {
                            try { onAfterSuccess(); } catch (_) { /* ignore */ }
                        }
                        location.hash = '#/projects';
                    },
                },
            }, 'Done');
            bodyHost.appendChild(el('div', { class: 'modal__actions' }, [doneBtn]));
        }

        function renderQueued(payload) {
            clearPhaseTimer();
            // Per the plan: modal closes; sync-pending toast appears with the
            // upstream queue reason (e.g. "no network", "server error").
            closeModal();
            var reason = (payload && payload.reason) || 'no network';
            showToast('↻ Queued — ' + reason + '. Will retry when network returns', 'warn');
            if (typeof onAfterSuccess === 'function') {
                try { onAfterSuccess(); } catch (_) { /* ignore */ }
            }
        }

        function renderError(err) {
            clearPhaseTimer();
            bodyHost.innerHTML = '';
            var status = (err && err.status) || 0;
            var body = (err && err.body) || {};
            var headline;
            var detail = '';
            // JS-5: 4xx status codes (except plain 400) are not retriable —
            // the cause must be fixed first. 413 in particular signals the
            // payload exceeds upstream limits.
            var isClient4xx = status >= 400 && status < 500;
            if (status === 413) {
                headline = '✕ Project too large to upload — contact ops.';
            } else if (status === 400) {
                headline = '✕ Cannot submit — ' + ((body && body.error) || 'gating failed');
            } else if (status === 502) {
                headline = '✕ Submit failed — try again';
                if (body && body.upstream_body_tail) {
                    detail = String(body.upstream_body_tail).slice(0, 400);
                } else if (body && body.error) {
                    detail = String(body.error);
                }
            } else if (isClient4xx) {
                headline = '✕ Cannot submit — ' + ((body && body.error) || ('HTTP ' + status));
            } else {
                headline = '✕ Submit failed — ' + ((err && err.message) || 'try again');
            }
            bodyHost.appendChild(el('div', {
                class: 'submit-modal__result submit-modal__result--err',
            }, headline));
            if (detail) {
                bodyHost.appendChild(el('pre', { class: 'submit-modal__upstream' },
                    detail));
            }
            var cancelBtn = el('button', {
                class: 'btn-secondary btn-lg',
                on: { click: function () { closeModal(); } },
            }, 'Close');
            // 4xx (client / gating) = no retry; 5xx + others = retry.
            var children = [cancelBtn];
            if (!isClient4xx) {
                var retryBtn = el('button', {
                    class: 'btn-primary btn-lg',
                    on: { click: function () { runSubmit(); } },
                }, 'Retry');
                children.push(retryBtn);
            } else {
                showToast('Cannot retry — fix the cause and try again.', 'warn');
            }
            bodyHost.appendChild(el('div', { class: 'modal__actions' }, children));
        }

        function runSubmit() {
            renderInflight();
            // Note: fetchJson resolves on any 2xx (including 202). We detect
            // the queued path via the body's `queued: true` marker, which
            // the backend always sets when returning 202.
            fetchJson('POST',
                '/api/project/' + encodeURIComponent(scanId) + '/submit'
            ).then(function (payload) {
                if (payload && payload.queued) {
                    renderQueued(payload);
                    return;
                }
                renderSuccess(payload || {});
            }).catch(function (err) {
                renderError(err);
            });
        }

        renderConfirm();
        return modal;
    }

    function renderProjectWorkspace(scanId) {
        clearScreenTimers();
        setTopBar({
            title: 'Project #' + scanId,
            backHref: '#/projects', backLabel: 'Back to projects',
        });

        var screen = el('div', { class: 'screen', id: 'project-screen' });
        var headerHost = el('div', { class: 'project-header' });
        var fieldsHost = el('div', { class: 'kv-list' });
        var scansHost = el('div', { class: 'project-scans' });
        var submitHost = el('div', { class: 'submit-host' });

        screen.appendChild(headerHost);
        screen.appendChild(fieldsHost);
        screen.appendChild(scansHost);
        screen.appendChild(submitHost);

        var lastProject = null;
        var lastScans = [];

        function refreshSubmitArea() {
            submitHost.innerHTML = '';
            var sse = state.lastSse || {};
            var procMap = sse.processing || {};
            var msg = _submitGatingMessage(lastProject, lastScans, procMap);
            // Disable the button whenever the gating message is non-empty.
            // Step 12 wires the click handler that opens the Submit modal.
            var isGated = !!msg;
            var btn = el('button', {
                class: 'btn-primary btn-lg', type: 'button',
                disabled: isGated,
                title: msg || 'Submit Project',
                on: {
                    click: function () {
                        if (msg) {
                            // Defensive: button shouldn't fire when gated, but
                            // if it does, surface the reason as a toast.
                            showToast(msg, 'warn');
                            return;
                        }
                        _openSubmitModal(scanId, lastProject, lastScans,
                            function onAfterSubmit() { loadProject(true); });
                    },
                },
            }, 'Submit Project');
            submitHost.appendChild(btn);
            if (msg) {
                submitHost.appendChild(el('div', { class: 'submit-host__hint' }, msg));
            }
        }

        function renderScansRow() {
            scansHost.innerHTML = '';
            scansHost.appendChild(el('div', { class: 'project-scans__head' }, [
                el('span', { class: 'project-scans__title' },
                    'Scans (' + lastScans.length + ')'),
                el('button', {
                    class: 'btn-secondary project-scans__add', type: 'button',
                    on: { click: function () { openNewScanModal(); } },
                }, [el('span', { html: ICONS.plus, class: 'icon-inline' }),
                    el('span', null, 'New')]),
            ]));

            if (!lastScans.length) {
                scansHost.appendChild(el('div', { class: 'empty-state' }, [
                    el('div', { class: 'empty-state__label' }, 'No scans yet'),
                    el('div', { class: 'empty-state__hint' },
                        'Tap "New" to record the first room.'),
                ]));
                return;
            }

            var sse = state.lastSse || {};
            var list = el('div', { class: 'scan-rows' });
            for (var i = 0; i < lastScans.length; i++) {
                (function (scan) {
                    var label = _scanRowStatusLabel(scan, sse);
                    var row = el('button', {
                        class: 'scan-row', type: 'button',
                        on: {
                            click: function () {
                                location.hash = '#/project/' + encodeURIComponent(scanId)
                                    + '/scan/' + encodeURIComponent(scan.name);
                            },
                        },
                    }, [
                        el('span', { class: 'scan-row__name' }, scan.name),
                        el('span', { class: 'scan-row__spacer' }),
                        el('span', { class: 'scan-row__status' }, label),
                    ]);
                    list.appendChild(row);
                })(lastScans[i]);
            }
            scansHost.appendChild(list);
        }

        function renderProjectFields() {
            headerHost.innerHTML = '';
            fieldsHost.innerHTML = '';

            if (!lastProject) {
                headerHost.appendChild(el('div', { class: 'loading-row' }, [
                    el('span', { class: 'spinner' }),
                    el('span', null, 'Loading project...'),
                ]));
                return;
            }

            // Header: customer name big, status pill, tag.
            var pill = _projectStatusPill(lastProject);
            headerHost.appendChild(el('div', { class: 'project-header__row' }, [
                el('div', { class: 'project-header__title' },
                    lastProject.customer_name || '(unnamed customer)'),
                el('span', { class: 'pill pill--' + pill.variant }, pill.label),
            ]));

            // Known KV rows.
            var rows = [];
            function addKv(label, value) {
                if (value == null || value === '') return;
                rows.push(el('div', { class: 'kv-row' }, [
                    el('div', { class: 'kv-row__label' }, label),
                    el('div', { class: 'kv-row__value' }, String(value)),
                ]));
            }
            addKv('Customer', lastProject.customer_name);
            var slot = fmtSlotRange(lastProject.slot_start, lastProject.slot_end);
            if (slot) addKv('Slot', slot);
            addKv('Address', lastProject.address);

            // Surface any other manifest.athathi_meta keys as generic KV.
            var meta = lastProject.athathi_meta;
            if (meta && typeof meta === 'object') {
                var skip = {
                    customer_name: 1, customerName: 1, customer: 1, name: 1,
                    slot_start: 1, slotStart: 1, start: 1, starts_at: 1,
                    slot_end: 1, slotEnd: 1, end: 1, ends_at: 1,
                    address: 1, location: 1, addr: 1,
                    scan_id: 1, id: 1, scanId: 1,
                };
                for (var k in meta) {
                    if (!Object.prototype.hasOwnProperty.call(meta, k)) continue;
                    if (skip[k]) continue;
                    var v = meta[k];
                    if (v == null) continue;
                    if (typeof v === 'object') {
                        try { v = JSON.stringify(v); } catch (_) { v = '(complex)'; }
                    }
                    if (v === '' || v === null || v === undefined) continue;
                    addKv(k.replace(/_/g, ' '), v);
                }
            }

            for (var i = 0; i < rows.length; i++) fieldsHost.appendChild(rows[i]);
        }

        function loadProject(silent) {
            var pPromise = fetchJson('GET', '/api/projects').then(function (env) {
                var arr = []
                    .concat((env && env.scheduled) || [])
                    .concat((env && env.history) || [])
                    .concat((env && env.ad_hoc) || []);
                for (var i = 0; i < arr.length; i++) {
                    if (parseInt(arr[i].scan_id, 10) === parseInt(scanId, 10)) {
                        return arr[i];
                    }
                }
                return null;
            });
            var sPromise = fetchJson('GET',
                '/api/project/' + encodeURIComponent(scanId) + '/scans')
                .then(function (data) { return (data && data.scans) || []; })
                .catch(function () { return []; });

            return Promise.all([pPromise, sPromise]).then(function (vals) {
                lastProject = vals[0];
                lastScans = vals[1] || [];
                renderProjectFields();
                renderScansRow();
                refreshSubmitArea();
            }).catch(function (err) {
                if (!silent) {
                    headerHost.innerHTML = '';
                    headerHost.appendChild(el('div', { class: 'empty-state' }, [
                        el('div', { class: 'empty-state__label' },
                            'Could not load project'),
                        el('div', { class: 'empty-state__hint' },
                            err.message || 'Network error'),
                    ]));
                }
            });
        }

        function openNewScanModal() {
            var n = (lastScans.length || 0) + 1;
            var defaultName = 'scan_' + n;
            var nameInput = el('input', {
                class: 'input', type: 'text', value: defaultName,
                autocapitalize: 'off', spellcheck: 'false', maxlength: '40',
            });
            var errBox = el('div', { class: 'modal__error' });
            var saveBtn = el('button', {
                class: 'btn-primary btn-lg',
                on: {
                    click: function () {
                        var raw = (nameInput.value || '').trim();
                        var err = validateScanName(raw);
                        if (err) { errBox.textContent = err; return; }
                        if (lastScans.some(function (s) { return s.name === raw; })) {
                            errBox.textContent = 'A scan with that name already exists';
                            return;
                        }
                        saveBtn.disabled = true;
                        errBox.textContent = '';
                        fetchJson('POST',
                            '/api/project/' + encodeURIComponent(scanId) + '/scan',
                            { name: raw }
                        ).then(function () {
                            closeModal();
                            location.hash = '#/project/' + encodeURIComponent(scanId)
                                + '/scan/' + encodeURIComponent(raw);
                        }).catch(function (e) {
                            saveBtn.disabled = false;
                            errBox.textContent = e.message || 'Could not create scan';
                        });
                    },
                },
            }, 'Create scan');
            var cancelBtn = el('button', {
                class: 'btn-secondary btn-lg',
                on: { click: function () { closeModal(); } },
            }, 'Cancel');

            openModal('New scan', [
                el('label', { class: 'modal__label' }, 'Room name'),
                nameInput,
                el('div', { class: 'modal__hint' },
                    'Lowercase, snake_case (a-z, 0-9, _), 1–40 chars.'),
                errBox,
                el('div', { class: 'modal__actions' }, [cancelBtn, saveBtn]),
            ]);
            setTimeout(function () {
                nameInput.focus();
                nameInput.select();
            }, 0);
        }

        // Initial paint, then poll every 60 s while on the screen.
        loadProject();
        state.pollTimers.project = setInterval(function () { loadProject(true); }, 60000);

        // Re-render scan rows on every SSE tick to update timers + stages.
        function onSseTick() {
            renderScansRow();
            refreshSubmitArea();
        }
        window.addEventListener('sse-tick', onSseTick);
        // Tear down listener when the screen is replaced. The `clearScreenTimers`
        // approach handles intervals; we use a separate WeakRef-style cleanup
        // by storing the handler on the screen.
        screen._sseHandler = onSseTick;

        return screen;
    }

    // -----------------------------------------------------------------
    // 10. Scan workspace (#/project/<id>/scan/<name>)  (Step 9c)
    // -----------------------------------------------------------------

    /**
     * Pure mapping from a scan-state string to the primary action label.
     * Plan §11.
     */
    function _scanPrimaryAction(stateName) {
        switch (stateName) {
            case 'idle': return 'Start recording';
            case 'recording': return 'Stop recording';
            case 'recorded': return 'Process';
            case 'processing': return null;  // read-only, with Cancel
            case 'done_unreviewed': return 'Review';
            case 'done_reviewing': return 'Continue review';
            case 'done_reviewed': return 'View review';
            case 'error': return 'Retry';
            default: return null;
        }
    }

    /**
     * Compute a normalized scan state from (scan summary, sse, result).
     * Returns one of: 'idle', 'recording', 'recorded', 'processing',
     * 'done_unreviewed', 'done_reviewing', 'done_reviewed', 'error'.
     */
    function _scanState(scan, sse, result) {
        // Single-tenant Pi: ANY in-flight recording belongs to whatever scan
        // the technician is looking at. Mirror the legacy `updateRecordingUI`
        // logic: `d.recording` true → recording; intermediate session
        // statuses (launching_driver / waiting_for_topics / starting) also
        // count as "recording-in-flight" so the Stop button appears
        // immediately, not after a 30+ s topic-wait.
        var INFLIGHT_REC_STATUSES = {
            recording: 1,
            starting: 1,
            launching_driver: 1,
            waiting_for_topics: 1,
        };
        // `in_flight` is the canonical "any recording in progress" flag from
        // the SSE feed (covers pre-bag launching_driver / waiting_for_topics
        // window). `recording` alone is only true once the bag spawns.
        if (sse && (sse.in_flight || sse.recording)) return 'recording';
        if (sse && sse.status && INFLIGHT_REC_STATUSES[sse.status]) return 'recording';
        var procMap = (sse && sse.processing) || {};
        for (var sid in procMap) {
            if (!Object.prototype.hasOwnProperty.call(procMap, sid)) continue;
            // Single-tenant: any active processing job belongs to this scan
            // when we're on the scan workspace. (No multi-tenant routing yet.)
            return 'processing';
        }
        if (result && result.status === 'processing') return 'processing';
        if (result && result.status === 'error') return 'error';
        if (scan.reviewed) return 'done_reviewed';
        if (result && result.status === 'done') return 'done_unreviewed';
        if (scan.has_rosbag && scan.active_run_id) return 'done_unreviewed';
        if (scan.has_rosbag) return 'recorded';
        return 'idle';
    }

    function renderScanWorkspace(scanId, scanName) {
        clearScreenTimers();
        setTopBar({
            title: scanName,
            backHref: '#/project/' + encodeURIComponent(scanId),
            backLabel: 'Back to project',
        });

        var screen = el('div', { class: 'screen', id: 'scan-screen' });
        var statusHost = el('div', { class: 'scan-status' });
        var metricsHost = el('div', { class: 'scan-metrics' });
        var actionsHost = el('div', { class: 'scan-actions' });
        var dangerHost = el('div', { class: 'scan-danger' });

        screen.appendChild(statusHost);
        screen.appendChild(metricsHost);
        screen.appendChild(actionsHost);
        screen.appendChild(dangerHost);

        var lastScan = null;     // {name, has_rosbag, active_run_id, reviewed}
        var lastResult = null;   // /result envelope

        function scanLabel(stateName) {
            switch (stateName) {
                case 'idle': return 'idle';
                case 'recording':
                    var sse = state.lastSse || {};
                    // Show the live session status (launching_driver,
                    // waiting_for_topics, recording, …) so the user sees
                    // progress during the 30-50 s pre-bag window, matching
                    // the legacy `updateRecordingUI` `stateEl.textContent =
                    // d.status` behaviour.
                    var label = sse.status || 'recording';
                    if (sse.recording) {
                        return label + ' ' + fmtMmSs(sse.elapsed || 0);
                    }
                    return label + '…';
                case 'recorded': return 'recorded — ready to process';
                case 'processing':
                    var sse2 = state.lastSse || {};
                    var procMap = sse2.processing || {};
                    var stage = '';
                    var elapsed = 0;
                    for (var sid in procMap) {
                        if (!Object.prototype.hasOwnProperty.call(procMap, sid)) continue;
                        var p = procMap[sid] || {};
                        if (p.scan_name === scanName) {
                            stage = p.stage || p.status || 'processing';
                            elapsed = p.elapsed || 0;
                            break;
                        }
                    }
                    return (stage || 'processing') + ' ' + fmtMmSs(elapsed);
                case 'done_unreviewed': return 'done — review pending';
                case 'done_reviewing': return 'done — reviewing';
                case 'done_reviewed': return 'done — reviewed';
                case 'error': return 'error';
                default: return stateName;
            }
        }

        function renderAll() {
            var sse = state.lastSse || {};
            // Annotate sse with the resolved scan name when the SSE session
            // matches our scan. The scoped recording uses synthetic session
            // names of `<scan_id>__<scan_name>`.
            if (sse.session_id) {
                // Best-effort match: we don't have the session record here.
                // The SSE feed itself doesn't include scan_id/scan_name on
                // the recording row. We compare against the active scan
                // optimistically — if the user is on this scan AND a
                // recording is in progress, we assume it's this scan.
                sse.scanName = scanName;
            }
            // Annotate processing entries with scan_name when the synthetic
            // session-name shape can be parsed (we don't have session info
            // either, so fallback to assuming a single in-flight job
            // belongs to the scan currently in view).
            if (sse.processing) {
                for (var sid in sse.processing) {
                    if (!Object.prototype.hasOwnProperty.call(sse.processing, sid)) continue;
                    var pe = sse.processing[sid];
                    if (pe && !pe.scan_name) pe.scan_name = scanName;
                }
            }

            var stateName = _scanState(lastScan || { name: scanName }, sse, lastResult);
            statusHost.innerHTML = '';
            statusHost.appendChild(el('div', { class: 'kv-row' }, [
                el('div', { class: 'kv-row__label' }, 'Status'),
                el('div', { class: 'kv-row__value' }, scanLabel(stateName)),
            ]));

            // Metrics row from cached result if reviewed.
            metricsHost.innerHTML = '';
            var res = lastResult && lastResult.result;
            if (stateName === 'done_reviewed' && res) {
                var frames = (res.frames != null) ? res.frames : null;
                var traj = (res.trajectory_length_m != null) ? res.trajectory_length_m
                    : (res.trajectory != null ? res.trajectory : null);
                var furn = Array.isArray(res.furniture) ? res.furniture.length : null;
                var parts = [];
                if (frames != null) parts.push(frames + ' frames');
                if (traj != null) parts.push(
                    'trajectory ' + (typeof traj === 'number' ? traj.toFixed(1) : traj) + ' m');
                if (furn != null) parts.push(furn + ' furniture');
                if (parts.length) {
                    metricsHost.appendChild(el('div', { class: 'metrics-row' },
                        parts.join('  ·  ')));
                }
            }

            // Primary action(s).
            actionsHost.innerHTML = '';
            var primary = _scanPrimaryAction(stateName);
            if (stateName === 'idle') {
                actionsHost.appendChild(el('button', {
                    class: 'btn-primary btn-lg', type: 'button',
                    on: { click: function () { startRecording(); } },
                }, primary));
            } else if (stateName === 'recording') {
                actionsHost.appendChild(el('button', {
                    class: 'btn-danger btn-lg', type: 'button',
                    on: { click: function () { stopRecording(); } },
                }, primary));
            } else if (stateName === 'recorded') {
                actionsHost.appendChild(el('button', {
                    class: 'btn-primary btn-lg', type: 'button',
                    on: { click: function () { startProcess(); } },
                }, primary));
            } else if (stateName === 'processing') {
                actionsHost.appendChild(el('div',
                    { class: 'inline-progress' },
                    'Processing in progress — this can take several minutes.'));
                // Cancel intentionally absent for Step 9: backend cancel
                // route is in the scoped processing block but the UI for
                // it lands with the review tool in Step 10. The button
                // here would need a /cancel endpoint that's not yet wired.
            } else if (stateName === 'done_unreviewed' || stateName === 'done_reviewing') {
                actionsHost.appendChild(el('button', {
                    class: 'btn-primary btn-lg', type: 'button',
                    on: {
                        click: function () {
                            location.hash = '#/project/' + encodeURIComponent(scanId)
                                + '/scan/' + encodeURIComponent(scanName) + '/review';
                        },
                    },
                }, primary));
            } else if (stateName === 'done_reviewed') {
                actionsHost.appendChild(el('button', {
                    class: 'btn-primary btn-lg', type: 'button',
                    on: {
                        click: function () {
                            location.hash = '#/project/' + encodeURIComponent(scanId)
                                + '/scan/' + encodeURIComponent(scanName) + '/review';
                        },
                    },
                }, primary));
            } else if (stateName === 'error') {
                actionsHost.appendChild(el('button', {
                    class: 'btn-primary btn-lg', type: 'button',
                    on: { click: function () { startProcess(); } },
                }, primary));
            }

            // Secondary / danger actions per state.
            dangerHost.innerHTML = '';
            // Re-record + Re-process available once a rosbag exists and the
            // state isn't actively recording / processing.
            if (lastScan && lastScan.has_rosbag &&
                stateName !== 'recording' && stateName !== 'processing') {
                dangerHost.appendChild(el('button', {
                    class: 'btn-secondary',
                    on: { click: function () { confirmReRecord(); } },
                }, 'Re-record'));
                if (lastScan.active_run_id) {
                    dangerHost.appendChild(el('button', {
                        class: 'btn-secondary',
                        on: { click: function () { confirmReProcess(); } },
                    }, 'Re-process'));
                }
            }
            // Delete scan — always available, except while in flight.
            if (stateName !== 'recording' && stateName !== 'processing') {
                dangerHost.appendChild(el('button', {
                    class: 'btn-danger',
                    on: { click: function () { confirmDelete(); } },
                }, 'Delete scan'));
            }
        }

        function startRecording() {
            // The POST returns 200 immediately while the recording thread
            // does the actual setup (network, driver, /dev/video0, ROS bag).
            // Optimistic UI lies: poll status briefly and surface real errors.
            showToast('Starting recording...');
            fetchJson('POST',
                '/api/project/' + encodeURIComponent(scanId) + '/scan/'
                + encodeURIComponent(scanName) + '/start_recording'
            ).then(function () {
                _verifyRecordingStarted();
            }).catch(function (e) {
                showToast(e.message || 'Could not start recording', 'error');
            });
        }

        function _verifyRecordingStarted() {
            // Poll /api/status and the scan list for up to ~35s — that covers
            // the LiDAR topic-wait timeout in _wait_for_topics. If the
            // backend's `recording` flag flips on, we're good. If the
            // matching session record shows status='error', surface it.
            var attempts = 0;
            var maxAttempts = 35;
            var iv = setInterval(function () {
                // JS-3: increment FIRST, regardless of fetch outcome — if the
                // network drops the timer would otherwise tick forever.
                attempts++;
                if (attempts >= maxAttempts) {
                    clearInterval(iv);
                    showToast('Recording is still starting — check the scan workspace.', 'warn');
                    loadAll();
                    return;
                }
                fetchJson('GET', '/api/status').then(function (s) {
                    if (s && s.recording) {
                        clearInterval(iv);
                        showToast('✓ Recording', 'success');
                        loadAll();
                        return;
                    }
                    // Not yet recording — check if a recent session errored.
                    fetchJson('GET', '/api/sessions').then(function (sessions) {
                        var match = (sessions || []).find(function (x) {
                            return x.name === scanId + '__' + scanName;
                        });
                        if (match && match.status === 'error') {
                            clearInterval(iv);
                            showToast('Recording failed: ' + (match.error || 'unknown error'), 'error');
                            loadAll();
                            return;
                        }
                    }).catch(function () { /* keep polling */ });
                }).catch(function () { /* keep polling */ });
            }, 1000);
            // Track so screen-leave clears it.
            state.pollTimers.recVerify = iv;
        }

        function stopRecording() {
            fetchJson('POST',
                '/api/project/' + encodeURIComponent(scanId) + '/scan/'
                + encodeURIComponent(scanName) + '/stop_recording'
            ).then(function () {
                showToast('Recording stopped');
                loadAll();
            }).catch(function (e) {
                showToast(e.message || 'Could not stop recording', 'error');
            });
        }

        function startProcess() {
            fetchJson('POST',
                '/api/project/' + encodeURIComponent(scanId) + '/scan/'
                + encodeURIComponent(scanName) + '/process'
            ).then(function () {
                showToast('Processing started');
                loadAll();
                _verifyProcessStarted(scanId, scanName);
            }).catch(function (e) {
                showToast(e.message || 'Could not start processing', 'error');
            });
        }

        function _verifyProcessStarted(_scanId, _scanName) {
            // JS-4: mirror of _verifyRecordingStarted — poll the result
            // endpoint for ~30s. If status==='error', toast it. If done,
            // toast success. Else warn after timeout.
            var attempts = 0;
            var maxAttempts = 30;
            var iv = setInterval(function () {
                attempts++;
                if (attempts >= maxAttempts) {
                    clearInterval(iv);
                    showToast('Processing is still starting — check the scan workspace.', 'warn');
                    loadAll();
                    return;
                }
                fetchJson('GET',
                    '/api/project/' + encodeURIComponent(_scanId) + '/scan/'
                    + encodeURIComponent(_scanName) + '/result'
                ).then(function (r) {
                    if (!r) return;
                    if (r.status === 'error') {
                        clearInterval(iv);
                        showToast('Processing failed: ' + (r.error || 'unknown error'), 'error');
                        loadAll();
                        return;
                    }
                    if (r.status === 'done' || r.done === true) {
                        clearInterval(iv);
                        showToast('✓ Processing complete', 'success');
                        loadAll();
                    }
                }).catch(function () { /* keep polling */ });
            }, 1000);
            state.pollTimers.procVerify = iv;
        }

        function confirmReRecord() {
            confirmDialog({
                title: 'Re-record this scan?',
                body: 'The existing recording for "' + scanName + '" will be deleted. '
                    + 'You can start a fresh recording afterwards.',
                confirmText: 'Re-record', danger: true,
            }).then(function (ok) {
                if (!ok) return;
                // Re-record = delete then recreate. The backend doesn't
                // expose a single "reset rosbag" endpoint, so we delete
                // and recreate the scan dir.
                fetchJson('DELETE',
                    '/api/project/' + encodeURIComponent(scanId) + '/scan/'
                    + encodeURIComponent(scanName)
                ).then(function () {
                    return fetchJson('POST',
                        '/api/project/' + encodeURIComponent(scanId) + '/scan',
                        { name: scanName });
                }).then(function () {
                    showToast('Scan reset — ready to record');
                    loadAll();
                }).catch(function (e) {
                    showToast(e.message || 'Could not reset scan', 'error');
                });
            });
        }

        function confirmReProcess() {
            confirmDialog({
                title: 'Re-process this scan?',
                body: 'A new processing run will be started. The previous '
                    + 'reviewed run stays on disk.',
                confirmText: 'Re-process',
            }).then(function (ok) { if (ok) startProcess(); });
        }

        function confirmDelete() {
            confirmDialog({
                title: 'Delete this scan?',
                body: 'This permanently removes the recording and all '
                    + 'processed runs for "' + scanName + '".',
                confirmText: 'Delete', danger: true,
            }).then(function (ok) {
                if (!ok) return;
                fetchJson('DELETE',
                    '/api/project/' + encodeURIComponent(scanId) + '/scan/'
                    + encodeURIComponent(scanName)
                ).then(function () {
                    showToast('Scan deleted');
                    location.hash = '#/project/' + encodeURIComponent(scanId);
                }).catch(function (e) {
                    showToast(e.message || 'Could not delete scan', 'error');
                });
            });
        }

        function loadAll() {
            return fetchJson('GET',
                '/api/project/' + encodeURIComponent(scanId) + '/scans'
            ).then(function (data) {
                var scans = (data && data.scans) || [];
                lastScan = null;
                for (var i = 0; i < scans.length; i++) {
                    if (scans[i].name === scanName) { lastScan = scans[i]; break; }
                }
                if (!lastScan) {
                    statusHost.innerHTML = '';
                    statusHost.appendChild(el('div', { class: 'empty-state' }, [
                        el('div', { class: 'empty-state__label' }, 'Scan not found'),
                        el('div', { class: 'empty-state__hint' },
                            'It may have been deleted from another tab.'),
                        el('button', {
                            class: 'btn-secondary', type: 'button',
                            on: {
                                click: function () {
                                    location.hash = '#/project/' + encodeURIComponent(scanId);
                                },
                            },
                        }, 'Back to project'),
                    ]));
                    return;
                }
                // Result (cached when reviewed; refetched on each loadAll
                // otherwise so processing state stays current).
                var cacheKey = scanId + '/' + scanName;
                if (lastScan.reviewed && state.resultCache[cacheKey]) {
                    lastResult = state.resultCache[cacheKey];
                    renderAll();
                    return;
                }
                fetchJson('GET',
                    '/api/project/' + encodeURIComponent(scanId) + '/scan/'
                    + encodeURIComponent(scanName) + '/result'
                ).then(function (rj) {
                    lastResult = rj || null;
                    if (lastScan.reviewed && rj && rj.status === 'done') {
                        state.resultCache[cacheKey] = rj;
                    }
                    renderAll();
                }).catch(function () {
                    lastResult = null;
                    renderAll();
                });
            }).catch(function (e) {
                statusHost.innerHTML = '';
                statusHost.appendChild(el('div', { class: 'empty-state' }, [
                    el('div', { class: 'empty-state__label' }, 'Could not load scan'),
                    el('div', { class: 'empty-state__hint' },
                        e.message || 'Network error'),
                ]));
            });
        }

        loadAll();

        // Track processing-to-done transitions so we can refresh /result
        // and auto-navigate to the review screen the moment Modal finishes.
        // Without this, the workspace stays on "processing — querying..."
        // until the user manually reloads, even though the bag is done.
        var sawProcessing = false;
        var navigatedToReview = false;

        function onSseTick() {
            renderAll();
            // Heuristic: was a processing entry visible last tick?
            var sse = state.lastSse || {};
            var procMap = sse.processing || {};
            var stillProcessing = false;
            for (var sid in procMap) {
                if (Object.prototype.hasOwnProperty.call(procMap, sid)) {
                    stillProcessing = true;
                    break;
                }
            }
            if (sawProcessing && !stillProcessing && !navigatedToReview) {
                // Transition processing → done. Re-fetch /result so we know
                // the actual outcome (could be done OR error).
                fetchJson('GET',
                    '/api/project/' + encodeURIComponent(scanId) + '/scan/'
                    + encodeURIComponent(scanName) + '/result'
                ).then(function (rj) {
                    lastResult = rj || null;
                    renderAll();
                    if (rj && rj.status === 'done' && !navigatedToReview) {
                        navigatedToReview = true;  // single-shot
                        showToast('Processing complete — opening review',
                            'success');
                        setTimeout(function () {
                            location.hash = '#/project/'
                                + encodeURIComponent(scanId)
                                + '/scan/' + encodeURIComponent(scanName)
                                + '/review';
                        }, 600);
                    }
                }).catch(function () { /* keep current view */ });
            }
            sawProcessing = stillProcessing;
        }
        window.addEventListener('sse-tick', onSseTick);
        screen._sseHandler = onSseTick;

        // Polling fallback: SSE can stall on Chromium kiosk (EventSource
        // sometimes silently drops, especially over the lifecycle of a
        // single tab). Poll /api/status every 1 s and synthesise an
        // SSE-shaped payload into `state.lastSse`, then re-render. The
        // SSE listener still wins when it ticks (its updates are richer —
        // includes `processing[]`); this is a strict safety net so the
        // Stop button + status label can never get stuck on stale data.
        state.pollTimers.scanStatus = setInterval(function () {
            fetchJson('GET', '/api/status', null, { skipAuthRedirect: true })
                .then(function (s) {
                    if (!s) return;
                    var prev = state.lastSse || {};
                    state.lastSse = {
                        recording: !!s.recording,
                        in_flight: !!(s.in_flight || s.recording),
                        elapsed: s.elapsed || 0,
                        status: s.inflight_status
                            || (s.recording ? 'recording' : (prev.status || 'idle')),
                        session_id: s.active_session || prev.session_id || null,
                        // /api/status now mirrors the SSE processing snapshot
                        // so `elapsed` keeps ticking + `stage` updates even
                        // when EventSource drops on Chromium kiosk.
                        processing: s.processing || prev.processing || {},
                    };
                    renderAll();
                })
                .catch(function () { /* keep polling */ });
        }, 1000);

        return screen;
    }

    // -----------------------------------------------------------------
    // 11. Settings sheet (#/settings)  (Step 11)
    // -----------------------------------------------------------------

    /**
     * Decode a JWT's middle segment and return an ISO-8601 string for the
     * `exp` claim, or null. Pure helper — exposed for tests via AppShell.
     *
     * Mirrors `auth.decode_jwt_payload` server-side, but stays vanilla JS
     * (no signature verification — that stays on the Athathi server).
     */
    function _jwtExpiryFromToken(token) {
        if (typeof token !== 'string' || !token) return null;
        var parts = token.split('.');
        if (parts.length < 2) return null;
        var seg = parts[1];
        // base64url -> base64 + padding fix.
        seg = seg.replace(/-/g, '+').replace(/_/g, '/');
        while (seg.length % 4) seg += '=';
        var json;
        try {
            json = atob(seg);
        } catch (_) { return null; }
        var data;
        try { data = JSON.parse(json); } catch (_) { return null; }
        if (!data || typeof data !== 'object') return null;
        var exp = data.exp;
        if (typeof exp !== 'number' || !isFinite(exp)) return null;
        try {
            return new Date(exp * 1000).toISOString();
        } catch (_) { return null; }
    }

    /**
     * Pure helper: should the Save button be enabled?
     *
     * Returns true iff the trimmed `current` string differs from the
     * trimmed `original`. Used by per-field Save buttons so that a
     * round-trip of the same value never triggers a PATCH.
     */
    function _settingsSaveEnabled(original, current) {
        var a = (original == null ? '' : String(original));
        var b = (current == null ? '' : String(current));
        return a !== b;
    }

    /**
     * Build a row with `<label>`, `<input>`, [Save] [Test?] buttons.
     * Wires the disabled-when-unchanged logic and a save-spinner.
     */
    function _settingsField(opts) {
        // opts: { label, value, key, type?, hint?, withTest?, onSave(value)->Promise,
        //         onTest?(value)->Promise, choices?: [{value,label}] (=> select) }
        var input;
        var initial = opts.value == null ? '' : String(opts.value);
        if (Array.isArray(opts.choices) && opts.choices.length) {
            input = el('select', { class: 'input settings-select' });
            for (var i = 0; i < opts.choices.length; i++) {
                var c = opts.choices[i];
                var o = el('option', { value: c.value }, c.label || c.value);
                if (String(c.value) === initial) o.selected = true;
                input.appendChild(o);
            }
        } else {
            input = el('input', {
                class: 'input',
                type: opts.type || 'text',
                value: initial,
                autocapitalize: 'off', spellcheck: 'false',
            });
        }

        var saveBtn = el('button', {
            class: 'btn-primary settings-field__save',
            type: 'button',
            disabled: true,
        }, 'Save');

        var testBtn = null;
        if (opts.withTest) {
            testBtn = el('button', {
                class: 'btn-secondary settings-field__test',
                type: 'button',
            }, 'Test');
        }

        function refreshSaveBtn() {
            var cur = input.value || '';
            saveBtn.disabled = !_settingsSaveEnabled(initial, cur);
        }
        input.addEventListener('input', refreshSaveBtn);
        input.addEventListener('change', refreshSaveBtn);

        saveBtn.addEventListener('click', function () {
            var cur = input.value || '';
            if (!_settingsSaveEnabled(initial, cur)) return;
            saveBtn.disabled = true;
            var prevHtml = saveBtn.innerHTML;
            saveBtn.innerHTML = '<span class="spinner"></span>';
            Promise.resolve()
                .then(function () { return opts.onSave(cur); })
                .then(function (effective) {
                    // Backend may strip trailing slash; reflect what's actually saved.
                    if (typeof effective === 'string') {
                        initial = effective;
                        input.value = effective;
                    } else {
                        initial = cur;
                    }
                    saveBtn.innerHTML = prevHtml;
                    refreshSaveBtn();
                    showToast((opts.label || 'Setting') + ' saved');
                })
                .catch(function (err) {
                    saveBtn.innerHTML = prevHtml;
                    refreshSaveBtn();
                    showToast(err.message || 'Save failed', 'error');
                });
        });

        if (testBtn) {
            testBtn.addEventListener('click', function () {
                var cur = input.value || '';
                var prev = testBtn.innerHTML;
                testBtn.disabled = true;
                testBtn.innerHTML = '<span class="spinner"></span>';
                Promise.resolve()
                    .then(function () { return opts.onTest(cur); })
                    .then(function (msg) {
                        testBtn.disabled = false;
                        testBtn.innerHTML = prev;
                        showToast(msg || 'Reachable');
                    })
                    .catch(function (err) {
                        testBtn.disabled = false;
                        testBtn.innerHTML = prev;
                        showToast(err.message || 'Unreachable', 'error');
                    });
            });
        }

        var actions = [saveBtn];
        if (testBtn) actions.unshift(testBtn);

        var children = [
            el('label', { class: 'settings-field__label' }, opts.label || ''),
            el('div', { class: 'settings-field__row' },
                [input].concat(actions)),
        ];
        if (opts.hint) {
            children.push(el('div', { class: 'settings-field__hint' }, opts.hint));
        }

        return el('div', { class: 'settings-field' }, children);
    }

    /**
     * Probe a remote API URL via the server-side proxy route. Any HTTP
     * response (2xx-5xx) from the upstream proves the server is alive;
     * network failure / DNS failure / timeout rejects with an error.
     *
     * FRONTEND-B1: the prior implementation did a raw `fetch(...)` direct
     * from the browser, which both bypassed `fetchJson` (plan §-1 says
     * everything goes through it) and assumed the remote was CORS-open.
     * `POST /api/settings/probe_api_url` runs `curl` on the Pi and
     * returns `{ok, status_code, error?}`.
     */
    function _settingsProbeUrl(rawUrl) {
        if (!rawUrl) return Promise.reject(new Error('URL is empty'));
        var url = String(rawUrl).replace(/\/+$/, '');
        return fetchJson('POST', '/api/settings/probe_api_url', { url: url })
            .then(function (res) {
                res = res || {};
                if (res.ok) {
                    return 'Reachable (HTTP ' + (res.status_code || '?') + ')';
                }
                throw new Error(res.error || ('Cannot reach ' + url));
            });
    }

    function _setSettingsTab(host, name) {
        if (!host) return;
        var btns = host.querySelectorAll('.settings-tab__btn');
        for (var i = 0; i < btns.length; i++) {
            var b = btns[i];
            var active = (b.getAttribute('data-tab') === name);
            if (active) b.classList.add('is-active');
            else b.classList.remove('is-active');
            b.setAttribute('aria-selected', active ? 'true' : 'false');
        }
        var bodies = host.querySelectorAll('.settings-tab__body');
        for (var j = 0; j < bodies.length; j++) {
            var bd = bodies[j];
            var match = (bd.getAttribute('data-tab') === name);
            bd.style.display = match ? '' : 'none';
        }
    }

    var SETTINGS_LOG_PATH = '/tmp/slam_app_debug.log';

    // ---- Device tab body -------------------------------------------------
    function _renderDeviceTab(opts) {
        // opts: { isLoggedIn, status }
        var status = opts.status || {};
        var net = status.network || {};
        var cam = status.camera || {};
        var calib = status.calibrated || {};

        function statusLine(label, ok, msg) {
            return el('div', { class: 'settings-stat' }, [
                el('span', { class: 'settings-stat__label' }, label),
                el('span',
                    { class: 'settings-stat__value' + (ok ? ' is-ok' : ' is-bad') },
                    msg || (ok ? 'ok' : 'unreachable')),
            ]);
        }

        var calibProgress = el('div',
            { class: 'settings-calib__progress', style: 'display:none' });
        var calibStatusMsg = el('div', { class: 'settings-calib__status' });
        var startBtn, stopBtn;

        function refreshCalibButtons(isRunning) {
            if (startBtn) startBtn.style.display = isRunning ? 'none' : '';
            if (stopBtn) stopBtn.style.display = isRunning ? '' : 'none';
            calibProgress.style.display = isRunning ? '' : 'none';
        }

        function pollCalib() {
            fetchJson('GET', '/api/calibration/intrinsics/status')
                .then(function (d) {
                    d = d || {};
                    if (d.running) {
                        calibProgress.textContent = (d.frames || 0) + '/'
                            + (d.target || 15) + ' frames captured';
                        refreshCalibButtons(true);
                        state.pollTimers.calib = setTimeout(pollCalib, 2000);
                    } else {
                        refreshCalibButtons(false);
                        if (d.calibrated) {
                            calibStatusMsg.textContent = 'Intrinsics calibrated successfully';
                            calibStatusMsg.className = 'settings-calib__status is-ok';
                        } else {
                            calibStatusMsg.textContent = 'Calibration not running';
                            calibStatusMsg.className = 'settings-calib__status';
                        }
                    }
                })
                .catch(function () {
                    state.pollTimers.calib = setTimeout(pollCalib, 3000);
                });
        }

        startBtn = el('button', {
            class: 'btn-primary',
            on: {
                click: function () {
                    fetchJson('POST', '/api/calibration/intrinsics/start')
                        .then(function () {
                            calibStatusMsg.textContent = 'Hold checkerboard in front of camera...';
                            calibStatusMsg.className = 'settings-calib__status';
                            refreshCalibButtons(true);
                            pollCalib();
                        })
                        .catch(function (e) {
                            showToast(e.message || 'Failed to start calibration', 'error');
                        });
                },
            },
        }, 'Calibrate Camera');

        stopBtn = el('button', {
            class: 'btn-secondary', style: 'display:none',
            on: {
                click: function () {
                    fetchJson('POST', '/api/calibration/intrinsics/stop')
                        .then(function () {
                            refreshCalibButtons(false);
                        })
                        .catch(function (e) {
                            showToast(e.message || 'Failed to stop', 'error');
                        });
                },
            },
        }, 'Stop');

        var extrStatusMsg = el('div', { class: 'settings-calib__status' });
        var extrBtn = el('button', {
            class: 'btn-primary',
            on: {
                click: function () {
                    var input = window.prompt(
                        'Enter camera-to-LiDAR offset as: tx ty tz roll pitch yaw\n'
                        + 'Translation in meters, rotation in degrees\n'
                        + 'Example: 0.0 0.05 0.02 0 -90 0');
                    if (!input) return;
                    var parts = input.trim().split(/\s+/).map(Number);
                    if (parts.length !== 6 || parts.some(isNaN)) {
                        showToast('Need 6 numbers: tx ty tz roll pitch yaw', 'error');
                        return;
                    }
                    fetchJson('POST', '/api/calibration/extrinsics', {
                        translation: { x: parts[0], y: parts[1], z: parts[2] },
                        rotation: { x: 0, y: 0, z: 0, w: 1 },
                        rpy_degrees: { roll: parts[3], pitch: parts[4], yaw: parts[5] },
                    }).then(function () {
                        extrStatusMsg.textContent = 'Extrinsics saved';
                        extrStatusMsg.className = 'settings-calib__status is-ok';
                        showToast('Extrinsics saved');
                    }).catch(function (e) {
                        showToast(e.message || 'Failed to save extrinsics', 'error');
                    });
                },
            },
        }, 'Set Extrinsics (manual)');

        var children = [
            el('h3', { class: 'settings-section__title' }, 'Network'),
            statusLine('Network', !!net.ok, net.message || (net.ok ? 'ok' : 'err')),
            el('h3', { class: 'settings-section__title' }, 'Hardware'),
            statusLine('LiDAR', !!status.lidar_reachable,
                status.lidar_reachable ? 'connected' : 'not reachable'),
            statusLine('Camera', !!cam.ok,
                cam.ok ? 'streaming' : (cam.message || 'not detected')),

            el('h3', { class: 'settings-section__title' }, 'Calibration'),
            statusLine('Intrinsics', !!calib.intrinsics,
                calib.intrinsics ? 'calibrated' : 'not calibrated'),
            statusLine('Extrinsics', !!calib.extrinsics,
                calib.extrinsics ? 'set' : 'not set'),
            el('div', { class: 'settings-calib__actions' }, [startBtn, stopBtn]),
            calibProgress,
            calibStatusMsg,
            el('div', { class: 'settings-calib__actions' }, [extrBtn]),
            extrStatusMsg,

            // Storage: /api/status doesn't expose disk metrics today, so
            // we just show the mount path. The plan calls this out as
            // skip-if-not-exposed.
            el('h3', { class: 'settings-section__title' }, 'Storage'),
            el('div', { class: 'settings-stat' }, [
                el('span', { class: 'settings-stat__label' }, 'Path'),
                el('span', { class: 'settings-stat__value' }, '/mnt/slam_data'),
            ]),

            // Brio recapture resolution. No backend route accepts this key
            // yet (`brio_recapture_size` isn't in `auth.DEFAULT_CONFIG`), so
            // we render it as disabled with a clear TODO note. When the
            // backend grows the key, swap the disabled flag for a real
            // _settingsField wiring.
            el('h3', { class: 'settings-section__title' }, 'Brio recapture resolution'),
            el('div', { class: 'settings-field' }, [
                el('div', { class: 'settings-field__row' }, [
                    el('select', {
                        class: 'input settings-select', disabled: true,
                    }, [
                        el('option', { value: '1280x720' }, '1280×720'),
                        el('option', { value: '1920x1080' }, '1920×1080'),
                    ]),
                ]),
                el('div', { class: 'settings-field__hint' },
                    'TODO: backend key brio_recapture_size not yet wired.'),
            ]),
        ];

        return el('div', {
            class: 'settings-tab__body', 'data-tab': 'device',
        }, children);
    }

    // ---- Account tab body ------------------------------------------------
    function _renderAccountTab(opts) {
        var me = opts.me || {};
        var cfg = opts.config || {};
        var loggedIn = !!me.is_logged_in;

        var userBlock;
        if (loggedIn) {
            var who = (me.username || '?')
                + (me.user_type ? ' (' + me.user_type + ')' : '');
            userBlock = el('div', { class: 'settings-stat' }, [
                el('span', { class: 'settings-stat__label' }, 'Logged in as'),
                el('span', { class: 'settings-stat__value' }, who),
            ]);
        } else {
            userBlock = el('div', { class: 'settings-stat' }, [
                el('span', { class: 'settings-stat__label' }, 'Account'),
                el('span', { class: 'settings-stat__value is-bad' }, 'not logged in'),
            ]);
        }

        var apiUrlField = _settingsField({
            label: 'Athathi API URL',
            value: cfg.api_url || '',
            type: 'url',
            withTest: true,
            onSave: function (v) {
                return fetchJson('PATCH', '/api/settings/config',
                    { api_url: v }).then(function (newCfg) {
                    return (newCfg && newCfg.api_url) || v;
                });
            },
            onTest: _settingsProbeUrl,
        });

        var uploadField = _settingsField({
            label: 'Upload endpoint',
            value: cfg.upload_endpoint || '',
            type: 'url',
            withTest: true,
            onSave: function (v) {
                return fetchJson('PATCH', '/api/settings/config',
                    { upload_endpoint: v }).then(function (newCfg) {
                    return (newCfg && newCfg.upload_endpoint) || v;
                });
            },
            onTest: _settingsProbeUrl,
        });

        // Token expiry: from server-provided exp_at first; fall back to
        // attempting decode if a token surface ever ships client-side.
        var tokenLine = null;
        if (loggedIn && me.exp_at) {
            tokenLine = el('div', { class: 'settings-stat' }, [
                el('span', { class: 'settings-stat__label' }, 'Token valid until'),
                el('span', { class: 'settings-stat__value' }, me.exp_at),
            ]);
        }

        var logoutBtn = el('button', {
            class: 'btn-danger btn-lg settings-logout-btn',
            type: 'button',
            disabled: !loggedIn,
            on: {
                click: function () {
                    confirmDialog({
                        title: 'Logout?',
                        body: 'Local recordings + projects stay on this device.',
                        confirmText: 'Logout',
                        danger: true,
                    }).then(function (ok) {
                        if (!ok) return;
                        fetchJson('POST', '/api/auth/logout', {},
                            { skipAuthRedirect: true })
                            .catch(function () { /* best-effort */ })
                            .then(function () {
                                state.user = null;
                                showToast('Logged out');
                                location.hash = '#/login';
                            });
                    });
                },
            },
        }, 'Logout');

        var children = [
            userBlock,
            tokenLine,
            el('h3', { class: 'settings-section__title' }, 'Athathi server'),
            apiUrlField,
            uploadField,
            el('div', { class: 'settings-section__hint' },
                'The Test button pings <api_url>/api/users/me/. Any HTTP '
                + 'response means the server is reachable.'),
            el('div', { class: 'settings-logout-host' }, [logoutBtn]),
        ];

        return el('div', {
            class: 'settings-tab__body', 'data-tab': 'account',
        }, children);
    }

    // ---- App tab body ----------------------------------------------------
    function _renderAppTab(opts) {
        var cfg = opts.config || {};
        var loggedIn = !!opts.loggedIn;
        var version = opts.version || 'feature/processing';

        var hookField = _settingsField({
            label: 'Submit hook command',
            value: cfg.post_submit_hook || '',
            hint: 'Receives the project dir as $1 and scan_id as $2 after a '
                + 'successful submit. Hook failures are logged but don\'t block.',
            onSave: function (v) {
                var body = { post_submit_hook: v ? v : null };
                return fetchJson('PATCH', '/api/settings/config', body)
                    .then(function (newCfg) {
                        return (newCfg && newCfg.post_submit_hook) || '';
                    });
            },
        });

        var transportField = _settingsField({
            label: 'Image transport',
            value: cfg.image_transport || 'multipart',
            choices: [
                { value: 'multipart', label: 'multipart' },
                { value: 'inline', label: 'inline' },
            ],
            onSave: function (v) {
                return fetchJson('PATCH', '/api/settings/config',
                    { image_transport: v }).then(function (newCfg) {
                    return (newCfg && newCfg.image_transport) || v;
                });
            },
        });

        var ttlField = _settingsField({
            label: 'Visual-search cache TTL (seconds)',
            value: cfg.visual_search_cache_ttl_s == null
                ? '86400' : String(cfg.visual_search_cache_ttl_s),
            type: 'number',
            hint: '0 disables the cache.',
            onSave: function (v) {
                var n = parseInt(v, 10);
                if (!isFinite(n) || n < 0) {
                    return Promise.reject(new Error('TTL must be a non-negative integer'));
                }
                return fetchJson('PATCH', '/api/settings/config',
                    { visual_search_cache_ttl_s: n })
                    .then(function (newCfg) {
                        return String((newCfg
                            && newCfg.visual_search_cache_ttl_s != null)
                            ? newCfg.visual_search_cache_ttl_s : n);
                    });
            },
        });

        // Ad-hoc / legacy projects toggle (localStorage only).
        var ADHOC_KEY = 'athathi.show_ad_hoc';
        var adHocOn = (function () {
            try {
                var v = window.localStorage.getItem(ADHOC_KEY);
                return v == null ? true : (v === 'true');
            } catch (_) { return true; }
        })();
        var adHocCheckbox = el('input', {
            type: 'checkbox', class: 'settings-toggle',
            id: 'settings-adhoc-toggle',
        });
        if (adHocOn) adHocCheckbox.checked = true;
        adHocCheckbox.addEventListener('change', function () {
            try {
                window.localStorage.setItem(ADHOC_KEY,
                    adHocCheckbox.checked ? 'true' : 'false');
                showToast('Ad-hoc setting saved');
            } catch (_) {
                showToast('localStorage unavailable', 'error');
            }
        });
        var adHocRow = el('div', { class: 'settings-field' }, [
            el('div', { class: 'settings-toggle-row' }, [
                adHocCheckbox,
                el('label', {
                    class: 'settings-toggle__label',
                    for: 'settings-adhoc-toggle',
                }, 'Show legacy / ad-hoc sessions'),
            ]),
            el('div', { class: 'settings-field__hint' },
                'When on, the Projects screen shows the ad-hoc section.'),
        ]);

        // Telemetry: log path + (disabled) download.
        var telemetry = el('div', { class: 'settings-field' }, [
            el('div', { class: 'settings-field__label' }, 'Telemetry / debug log'),
            el('div', { class: 'settings-field__row' }, [
                el('code', { class: 'settings-code' }, SETTINGS_LOG_PATH),
                el('button', {
                    class: 'btn-secondary',
                    type: 'button',
                    disabled: true,
                    title: 'coming soon',
                }, 'Download log (coming soon)'),
            ]),
        ]);

        // Upload-filter editor (collapsible, lazy-load).
        var ufTextarea = el('textarea', {
            class: 'settings-textarea', spellcheck: 'false',
            placeholder: '{}',
        });
        var ufError = el('div', { class: 'settings-field__error' });
        var ufSaveBtn = el('button', {
            class: 'btn-primary', type: 'button',
        }, 'Save filter');
        ufSaveBtn.addEventListener('click', function () {
            var raw = ufTextarea.value || '';
            var parsed;
            try { parsed = JSON.parse(raw); }
            catch (e) {
                ufError.textContent = 'Invalid JSON: ' + e.message;
                return;
            }
            if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
                ufError.textContent = 'Filter must be a JSON object';
                return;
            }
            ufError.textContent = '';
            ufSaveBtn.disabled = true;
            var prev = ufSaveBtn.innerHTML;
            ufSaveBtn.innerHTML = '<span class="spinner"></span>';
            fetchJson('PATCH', '/api/settings/upload_filter', parsed)
                .then(function () {
                    ufSaveBtn.innerHTML = prev;
                    ufSaveBtn.disabled = false;
                    showToast('Upload filter saved');
                })
                .catch(function (err) {
                    ufSaveBtn.innerHTML = prev;
                    ufSaveBtn.disabled = false;
                    showToast(err.message || 'Save failed', 'error');
                });
        });

        var ufDetails = el('details', { class: 'settings-details' }, [
            el('summary', { class: 'settings-details__summary' },
                'Upload-filter editor (advanced)'),
            el('div', { class: 'settings-details__body' }, [
                ufTextarea,
                ufError,
                el('div', { class: 'settings-field__row' }, [ufSaveBtn]),
            ]),
        ]);
        ufDetails.addEventListener('toggle', function () {
            if (!ufDetails.open || ufTextarea.value) return;
            if (!loggedIn) {
                ufError.textContent = 'Login to load the upload filter.';
                return;
            }
            fetchJson('GET', '/api/settings/upload_filter')
                .then(function (data) {
                    ufTextarea.value = JSON.stringify(data, null, 2);
                })
                .catch(function (err) {
                    ufError.textContent = err.message || 'Could not load filter';
                });
        });

        var children = [
            el('div', { class: 'settings-stat' }, [
                el('span', { class: 'settings-stat__label' }, 'Version'),
                el('span', { class: 'settings-stat__value' }, version),
            ]),
            el('h3', { class: 'settings-section__title' }, 'Submit hook'),
            hookField,
            el('h3', { class: 'settings-section__title' }, 'Image transport'),
            transportField,
            el('h3', { class: 'settings-section__title' }, 'Visual-search cache'),
            ttlField,
            el('h3', { class: 'settings-section__title' }, 'Projects view'),
            adHocRow,
            el('h3', { class: 'settings-section__title' }, 'Telemetry'),
            telemetry,
            el('h3', { class: 'settings-section__title' }, 'Advanced'),
            ufDetails,
        ];

        return el('div', {
            class: 'settings-tab__body', 'data-tab': 'app',
        }, children);
    }

    function renderSettings() {
        clearScreenTimers();
        // Settings has its own header (with ← Back). No top bar gear /
        // logout (we'd just bounce back to the Settings sheet anyway).
        setTopBar(null);

        var screen = el('div', { class: 'screen settings-screen', id: 'settings-screen' });

        // Custom header (replaces the top bar): ← Back · Settings · tabs.
        var tabHostId = 'settings-tabs';
        var tabBtns = ['device', 'account', 'app'].map(function (name) {
            return el('button', {
                class: 'settings-tab__btn',
                'data-tab': name,
                'aria-selected': 'false',
                type: 'button',
                on: {
                    click: function () {
                        _setSettingsTab(document.getElementById(tabHostId), name);
                    },
                },
            }, name.charAt(0).toUpperCase() + name.slice(1));
        });

        var backBtn = el('button', {
            class: 'topbar__btn topbar__btn--back settings-back',
            'aria-label': 'Back', title: 'Back',
            html: ICONS.back,
            on: {
                click: function () {
                    // Prefer history.back when there's an in-app referrer;
                    // otherwise route by auth state.
                    var loggedIn = !!(state.user && state.user.is_logged_in);
                    if (window.history && window.history.length > 1
                        && document.referrer
                        && document.referrer.indexOf(location.origin) === 0) {
                        window.history.back();
                    } else {
                        location.hash = loggedIn ? '#/projects' : '#/login';
                    }
                },
            },
        });

        var header = el('div', { class: 'settings-header' }, [
            backBtn,
            el('div', { class: 'settings-header__title' }, 'Settings'),
            el('div', { class: 'settings-header__spacer' }),
            el('div', { class: 'settings-tabs', id: tabHostId, role: 'tablist' }, tabBtns),
        ]);

        var bodyHost = el('div', { class: 'settings-body' });
        screen.appendChild(header);
        screen.appendChild(bodyHost);

        // Loading row while we fetch /api/status, /api/auth/me, /api/settings/config.
        var loading = el('div', { class: 'loading-row' }, [
            el('span', { class: 'spinner' }),
            el('span', null, 'Loading settings...'),
        ]);
        bodyHost.appendChild(loading);

        function loadAll() {
            var statusP = fetchJson('GET', '/api/status', null,
                { skipAuthRedirect: true })
                .catch(function () { return {}; });
            var meP = fetchJson('GET', '/api/auth/me', null,
                { skipAuthRedirect: true })
                .catch(function () { return null; });
            var cfgP = fetchJson('GET', '/api/settings/config', null,
                { skipAuthRedirect: true })
                .catch(function () { return {}; });

            return Promise.all([statusP, meP, cfgP]).then(function (vals) {
                var status = vals[0] || {};
                var me = vals[1] || {};
                var config = vals[2] || {};
                var loggedIn = !!(me && me.is_logged_in);

                bodyHost.innerHTML = '';

                bodyHost.appendChild(_renderDeviceTab({
                    isLoggedIn: loggedIn, status: status,
                }));
                bodyHost.appendChild(_renderAccountTab({
                    me: me, config: config,
                }));
                bodyHost.appendChild(_renderAppTab({
                    config: config, loggedIn: loggedIn, version: status.version,
                }));

                _setSettingsTab(document.getElementById(tabHostId), 'device');
            });
        }

        loadAll();

        return screen;
    }

    // -----------------------------------------------------------------
    // 11b. Review tool (#/project/<id>/scan/<name>/review)  (Step 10)
    // -----------------------------------------------------------------

    /**
     * Pure helper: pick the largest-by-volume bbox out of a list of
     * (id, size[3]) tuples. Returns the id of the winner. Tie-break is
     * input order (stable).
     *
     * Plan §10e: "Largest" = max(volume) where volume = sx * sy * sz.
     */
    function _pickPrimary(boxes) {
        if (!Array.isArray(boxes) || !boxes.length) return null;
        var bestId = null, bestVol = -Infinity;
        for (var i = 0; i < boxes.length; i++) {
            var b = boxes[i] || {};
            var s = b.size || [];
            var v = (parseFloat(s[0]) || 0)
                  * (parseFloat(s[1]) || 0)
                  * (parseFloat(s[2]) || 0);
            if (v > bestVol) { bestVol = v; bestId = b.id; }
        }
        return bestId;
    }

    /**
     * Pure helper: most-common class string from a list. Ties broken by
     * input order (first one wins).
     *
     * Plan §10e: defaults the merge-class radio to the most common.
     */
    function _pickMostCommonClass(classes) {
        if (!Array.isArray(classes) || !classes.length) return null;
        var counts = {};
        var firstAt = {};
        for (var i = 0; i < classes.length; i++) {
            var c = classes[i];
            if (typeof c !== 'string' || !c) continue;
            if (!(c in counts)) { counts[c] = 0; firstAt[c] = i; }
            counts[c]++;
        }
        var best = null, bestCount = -1, bestFirst = Infinity;
        for (var k in counts) {
            if (!Object.prototype.hasOwnProperty.call(counts, k)) continue;
            if (counts[k] > bestCount
                || (counts[k] === bestCount && firstAt[k] < bestFirst)) {
                best = k; bestCount = counts[k]; bestFirst = firstAt[k];
            }
        }
        return best;
    }

    /**
     * Append a `?v=<ts>` cache-buster to a URL. If `?` already appears,
     * use `&`. Empty input returns ''.
     */
    function _recaptureBust(url, ts) {
        if (!url) return '';
        var sep = url.indexOf('?') === -1 ? '?' : '&';
        return url + sep + 'v=' + (ts || Date.now());
    }

    /**
     * Bbox-id ↔ best-image idx mapping. Plan §10j.9: must look up by
     * `result.best_images[idx].bbox_id`, NOT a naive position match.
     */
    function _bboxIdxByBboxId(result, bboxId) {
        if (!result || !Array.isArray(result.best_images)) return -1;
        for (var i = 0; i < result.best_images.length; i++) {
            var bi = result.best_images[i] || {};
            if (bi.bbox_id === bboxId) return i;
        }
        return -1;
    }

    /**
     * Switch the Review tool's active tab. Pure DOM toggling — no fetches.
     * `name` ∈ {floorplan, furniture, notes}.
     */
    function _setReviewTab(host, name) {
        if (!host) return;
        var btns = host.querySelectorAll('.review-tab__btn');
        for (var i = 0; i < btns.length; i++) {
            var b = btns[i];
            var isActive = (b.getAttribute('data-tab') === name);
            if (isActive) b.classList.add('is-active');
            else b.classList.remove('is-active');
            b.setAttribute('aria-selected', isActive ? 'true' : 'false');
        }
        var bodies = host.querySelectorAll('.review-tab__body');
        for (var j = 0; j < bodies.length; j++) {
            var bd = bodies[j];
            var match = (bd.getAttribute('data-tab') === name);
            bd.style.display = match ? '' : 'none';
        }
    }

    /**
     * Render an SVG for the Floorplan tab with review-state overlays.
     * Pure: takes the result envelope + review map; returns an SVG node.
     *
     * Each <rect> gets:
     *   - data-bbox-id attribute (so callers can match clicks).
     *   - state-derived class: rect.review-rect, .is-deleted,
     *     .is-merged, .is-selected, .is-untouched.
     *   - When merged primary, a <text class="review-merge-badge"> with `↻N`.
     */
    function _renderReviewFloorplanSvg(result, reviewMap, opts) {
        opts = opts || {};
        var fp = (result && result.floorplan) || {};
        var rawWalls = fp.walls || [];
        var doors = fp.doors || [];
        var windows = fp.windows || [];
        var furniture = (result && result.furniture) || [];
        reviewMap = reviewMap || {};

        var svgNs = 'http://www.w3.org/2000/svg';

        // Hygiene filter: SpatialLM occasionally emits zero-length wall
        // duplicates (109 walls but 108 collapsed to a single point — see
        // scan_2 / project 48). Drop those so the renderer doesn't stack
        // labels on top of each other and the auto-scale doesn't collapse
        // the room to nothing. Also de-duplicate walls whose endpoints
        // round to the same (cm-precision) line segment.
        var walls = [];
        var seen = {};
        for (var wi = 0; wi < rawWalls.length; wi++) {
            var rw = rawWalls[wi];
            if (!rw || !rw.start || !rw.end) continue;
            var sx = rw.start[0], sy = rw.start[1];
            var ex = rw.end[0], ey = rw.end[1];
            var rlen = Math.hypot(ex - sx, ey - sy);
            if (rlen < 0.1) continue;  // stub / degenerate
            // Canonical key: sorted endpoints rounded to 1 cm.
            var ka = [sx.toFixed(2), sy.toFixed(2)].join(',');
            var kb = [ex.toFixed(2), ey.toFixed(2)].join(',');
            var key = ka < kb ? (ka + '|' + kb) : (kb + '|' + ka);
            if (seen[key]) continue;
            seen[key] = true;
            walls.push(rw);
        }

        if (!walls.length) {
            var hint = (rawWalls.length > 0)
                ? 'The model returned ' + rawWalls.length
                  + ' wall entries but none had valid geometry. '
                  + 'Re-record this scan with a longer walk-around.'
                : 'No walls returned. Re-record with a longer walk-around.';
            var empty = el('div', { class: 'empty-state' }, [
                el('div', { class: 'empty-state__label' },
                    'No usable walls in this run'),
                el('div', { class: 'empty-state__hint' }, hint),
            ]);
            return empty;
        }

        // Compute extents over walls.
        var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
        for (var i = 0; i < walls.length; i++) {
            var w = walls[i];
            var pts = [w.start, w.end];
            for (var j = 0; j < pts.length; j++) {
                var p = pts[j];
                if (!p) continue;
                if (p[0] < minX) minX = p[0];
                if (p[0] > maxX) maxX = p[0];
                if (p[1] < minY) minY = p[1];
                if (p[1] > maxY) maxY = p[1];
            }
        }
        // Padding scales with room size — 4% of the longer dimension, clamped
        // to [0.25 m, 0.8 m]. A small 3 m room gets ~0.25 m breathing room
        // (8% of room) instead of a fixed 0.5 m which would dominate it.
        var roomLongest = Math.max(maxX - minX, maxY - minY);
        var pad = Math.min(0.8, Math.max(0.25, roomLongest * 0.04));
        minX -= pad; minY -= pad; maxX += pad; maxY += pad;
        var ww = maxX - minX;
        var hh = maxY - minY;

        var svg = document.createElementNS(svgNs, 'svg');
        svg.setAttribute('class', 'review-floorplan-svg');
        svg.setAttribute('viewBox', minX + ' ' + (-maxY) + ' ' + ww + ' ' + hh);
        svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');

        // Variable pixel size: bound the floorplan so a small room renders
        // small but always readable, a typical room renders at natural
        // scale, and a huge room is scaled down to fit. On the 640×480
        // touchscreen with the review-section column ~608 px wide and
        // ~320 px vertical budget:
        //
        //   PX_PER_METRE   natural pixel scale (a 5 m wall ≈ 200 px)
        //   MIN_W / MIN_H  always at least this big — small rooms / partial
        //                  detections still produce a usable diagram.
        //   COL_W_CAP / COL_H_CAP   never bigger than the column.
        //
        // Aspect is preserved on both up- and down-scale.
        var PX_PER_METRE = 40;
        var COL_W_CAP = 608;
        var COL_H_CAP = 320;
        var MIN_W = 280;
        var MIN_H = 200;
        var natW = ww * PX_PER_METRE;
        var natH = hh * PX_PER_METRE;
        // Scale up if both dimensions are below the floor.
        if (natW < MIN_W && natH < MIN_H) {
            var kup = Math.min(MIN_W / natW, MIN_H / natH);
            natW *= kup; natH *= kup;
        }
        // Scale down if either dimension exceeds the cap.
        if (natW > COL_W_CAP || natH > COL_H_CAP) {
            var kdn = Math.min(COL_W_CAP / natW, COL_H_CAP / natH);
            natW *= kdn; natH *= kdn;
        }
        svg.setAttribute('width', String(Math.round(natW)));
        svg.setAttribute('height', String(Math.round(natH)));

        // Background tap = clear all selections. Rect click handlers call
        // `stopPropagation()`, so any click that bubbles up to the SVG
        // came from a non-furniture surface (canvas background, walls,
        // doors, windows) — fire the deselect.
        if (typeof opts.onCanvasTap === 'function') {
            svg.addEventListener('click', function (_e) {
                opts.onCanvasTap();
            });
        }

        var flipG = document.createElementNS(svgNs, 'g');
        flipG.setAttribute('transform', 'scale(1,-1)');
        svg.appendChild(flipG);

        var wallById = {};
        for (var k = 0; k < walls.length; k++) {
            wallById[walls[k].id] = walls[k];
        }
        function wallDir(wl) {
            var dx = wl.end[0] - wl.start[0];
            var dy = wl.end[1] - wl.start[1];
            var len = Math.hypot(dx, dy) || 1;
            return { dx: dx / len, dy: dy / len, len: len };
        }

        // Walls + length labels.
        // Font size is in world units (metres). 0.16 m world ≈ 18-22 px on
        // a 480-tall screen with a 6-9 m room — readable but not shouty.
        var WALL_FONT = 0.16;
        for (var w0 = 0; w0 < walls.length; w0++) {
            var wl = walls[w0];
            var line = document.createElementNS(svgNs, 'line');
            line.setAttribute('x1', wl.start[0]);
            line.setAttribute('y1', wl.start[1]);
            line.setAttribute('x2', wl.end[0]);
            line.setAttribute('y2', wl.end[1]);
            line.setAttribute('class', 'review-wall');
            line.setAttribute('stroke-width', '0.05');
            flipG.appendChild(line);

            // Length label: midpoint of wall, offset perpendicular outward.
            var dirW = wallDir(wl);
            if (dirW.len < 0.3) continue;  // skip stub walls (door jambs etc.)
            var mxw = (wl.start[0] + wl.end[0]) / 2;
            var myw = (wl.start[1] + wl.end[1]) / 2;
            // Perpendicular: rotate dir 90°. Push label 0.18 m away from wall.
            var nx = -dirW.dy * 0.18;
            var ny = dirW.dx * 0.18;
            var lblText = document.createElementNS(svgNs, 'text');
            lblText.setAttribute('x', mxw + nx);
            // Outer SVG group is NOT flipped (we render labels on the
            // post-flip surface). Negate y to compensate.
            lblText.setAttribute('y', -(myw + ny));
            lblText.setAttribute('text-anchor', 'middle');
            lblText.setAttribute('dominant-baseline', 'central');
            lblText.setAttribute('class', 'review-wall-len');
            lblText.setAttribute('font-size', WALL_FONT);
            lblText.textContent = dirW.len.toFixed(1) + ' m';
            svg.appendChild(lblText);
        }

        // Doors.
        for (var d0 = 0; d0 < doors.length; d0++) {
            var dr = doors[d0];
            var wl2 = wallById[dr.wall];
            if (!wl2) continue;
            var dirA = wallDir(wl2);
            var halfW = (dr.width || 0.8) / 2;
            var cx = dr.center[0], cy = dr.center[1];
            var gap = document.createElementNS(svgNs, 'line');
            gap.setAttribute('x1', cx - dirA.dx * halfW);
            gap.setAttribute('y1', cy - dirA.dy * halfW);
            gap.setAttribute('x2', cx + dirA.dx * halfW);
            gap.setAttribute('y2', cy + dirA.dy * halfW);
            gap.setAttribute('class', 'review-door');
            gap.setAttribute('stroke-width', '0.07');
            flipG.appendChild(gap);
        }

        // Windows.
        for (var n0 = 0; n0 < windows.length; n0++) {
            var wn = windows[n0];
            var wl3 = wallById[wn.wall];
            if (!wl3) continue;
            var dirB = wallDir(wl3);
            var halfWn = (wn.width || 1.0) / 2;
            var cxn = wn.center[0], cyn = wn.center[1];
            var inner = document.createElementNS(svgNs, 'line');
            inner.setAttribute('x1', cxn - dirB.dx * halfWn);
            inner.setAttribute('y1', cyn - dirB.dy * halfWn);
            inner.setAttribute('x2', cxn + dirB.dx * halfWn);
            inner.setAttribute('y2', cyn + dirB.dy * halfWn);
            inner.setAttribute('class', 'review-window');
            inner.setAttribute('stroke-width', '0.12');
            flipG.appendChild(inner);
        }

        // Furniture rects with review-state styling.
        var selectedSet = opts.selectedSet || {};
        // Index furniture by id so a merged primary can pull its members'
        // geometries on-the-fly to render the AABB union live (without
        // waiting for `render_reviewed`).
        var furnIndex = {};
        for (var fi0 = 0; fi0 < furniture.length; fi0++) {
            var ff = furniture[fi0];
            if (ff && ff.id) furnIndex[ff.id] = ff;
        }
        for (var f0 = 0; f0 < furniture.length; f0++) {
            (function (f) {
                if (!f || !f.id) return;
                var entry = reviewMap[f.id] || {};
                var status = entry.status || 'untouched';
                if (status === 'merged_into') return;  // hidden — collapsed

                // For a merged primary, compute the AABB union of
                // primary + all members so the rect actually grows to
                // cover all merged sofas / chairs / etc. Yaw stays
                // verbatim from the primary (plan §6c step 4).
                var members = entry.merged_from;
                var isMergedPrimary = (status === 'kept'
                    && Array.isArray(members) && members.length > 0);

                var fcx, fcy, sx, sy;
                var yawRad = f.yaw || 0;
                if (isMergedPrimary) {
                    // World-axis-aligned union — same math as
                    // review._merge_geometry server-side.
                    var lo = [Infinity, Infinity];
                    var hi = [-Infinity, -Infinity];
                    var allBoxes = [f];
                    for (var mi = 0; mi < members.length; mi++) {
                        var m = furnIndex[members[mi]];
                        if (m) allBoxes.push(m);
                    }
                    for (var bi = 0; bi < allBoxes.length; bi++) {
                        var b = allBoxes[bi];
                        var bcx = (b.center && b.center[0]) || 0;
                        var bcy = (b.center && b.center[1]) || 0;
                        var bsx = (b.size && b.size[0]) || 0.3;
                        var bsy = (b.size && b.size[1]) || 0.3;
                        if (bcx - bsx / 2 < lo[0]) lo[0] = bcx - bsx / 2;
                        if (bcy - bsy / 2 < lo[1]) lo[1] = bcy - bsy / 2;
                        if (bcx + bsx / 2 > hi[0]) hi[0] = bcx + bsx / 2;
                        if (bcy + bsy / 2 > hi[1]) hi[1] = bcy + bsy / 2;
                    }
                    fcx = (lo[0] + hi[0]) / 2;
                    fcy = (lo[1] + hi[1]) / 2;
                    sx = hi[0] - lo[0];
                    sy = hi[1] - lo[1];
                    // Render the union AXIS-ALIGNED (yaw = 0) so the
                    // visible rect actually contains all members. Server
                    // keeps the primary's yaw on submit for forward-compat,
                    // but the visual representation is clearer this way.
                    yawRad = 0;
                } else {
                    fcx = (f.center && f.center[0]) || 0;
                    fcy = (f.center && f.center[1]) || 0;
                    sx = (f.size && f.size[0]) || 0.3;
                    sy = (f.size && f.size[1]) || 0.3;
                }
                var yawDeg = yawRad * 180 / Math.PI;

                var rect = document.createElementNS(svgNs, 'rect');
                rect.setAttribute('x', -sx / 2);
                rect.setAttribute('y', -sy / 2);
                rect.setAttribute('width', sx);
                rect.setAttribute('height', sy);
                rect.setAttribute('data-bbox-id', f.id);

                var cls = 'review-rect';
                if (status === 'deleted') cls += ' is-deleted';
                else if (status === 'kept') cls += ' is-kept';
                else cls += ' is-untouched';

                // members + isMergedPrimary already computed above for
                // the AABB-union geometry calculation. Don't redeclare.
                if (isMergedPrimary) cls += ' is-merged';
                if (selectedSet[f.id]) cls += ' is-selected';

                rect.setAttribute('class', cls);
                if (status === 'deleted') {
                    rect.setAttribute('stroke-dasharray', '0.04 0.04');
                }
                rect.setAttribute('stroke-width',
                    selectedSet[f.id] ? '0.08' : '0.04');
                rect.setAttribute('transform',
                    'translate(' + fcx + ' ' + fcy + ') rotate(' + yawDeg + ')');

                if (typeof opts.onTap === 'function') {
                    rect.addEventListener('click', function (e) {
                        // Don't bubble to the SVG — that handler clears
                        // the selection when the user taps blank canvas.
                        e.stopPropagation();
                        opts.onTap(f.id);
                    });
                }
                flipG.appendChild(rect);

                // Class label on top of the rect, sized + rotated to fit.
                if (status !== 'deleted') {
                    var cname = (entry.class_override
                        || (f.class || '')).toString();
                    // Read along the LONGER side: if depth > width, rotate
                    // the label 90° so it lays along the longer dimension
                    // and we can fit more characters before truncating.
                    var alongY = sy > sx;
                    var longSide = alongY ? sy : sx;
                    var shortSide = alongY ? sx : sy;
                    // Font size: scale to short side (height) — this is the
                    // SVG line-height bound. Floor at 0.10 m (≈ 12 px) so
                    // tiny bboxes still get readable text.
                    var fontSz = Math.max(0.10, shortSide * 0.36);
                    // Char budget on the long side — assume ~0.55 m per
                    // 1 m of fontSize for our font weight.
                    var charBudget = Math.max(4,
                        Math.floor(longSide / (fontSz * 0.55)));
                    var displayName = cname.length > charBudget
                        ? cname.slice(0, Math.max(1, charBudget - 1)) + '…'
                        : cname;
                    if (displayName) {
                        var lbl = document.createElementNS(svgNs, 'text');
                        lbl.setAttribute('x', '0');
                        lbl.setAttribute('y', '0');
                        lbl.setAttribute('text-anchor', 'middle');
                        lbl.setAttribute('dominant-baseline', 'central');
                        lbl.setAttribute('class', 'review-furn-label');
                        lbl.setAttribute('font-size', fontSz);
                        lbl.textContent = displayName;
                        lbl.setAttribute('pointer-events', 'none');

                        // Wrap in a <g> so we can: (1) translate to the
                        // rect's centre in the un-flipped outer SVG space
                        // (negate y), and (2) rotate -90° when the rect
                        // is taller than it is wide.
                        var lblG = document.createElementNS(svgNs, 'g');
                        var tx = fcx, ty = -fcy;
                        var transform = 'translate(' + tx + ' ' + ty + ')';
                        if (alongY) transform += ' rotate(-90)';
                        lblG.setAttribute('transform', transform);
                        lblG.setAttribute('pointer-events', 'none');
                        lblG.appendChild(lbl);
                        svg.appendChild(lblG);
                    }
                }

                // Merged-primary badge: `↻N` (member count + 1).
                if (isMergedPrimary) {
                    var badgeG = document.createElementNS(svgNs, 'g');
                    var bx = fcx + sx / 2 - 0.05;
                    var by = -(fcy + sy / 2 - 0.05);  // flipped Y for outer SVG
                    badgeG.setAttribute('class', 'review-merge-badge');
                    badgeG.setAttribute('transform',
                        'translate(' + bx + ' ' + by + ')');
                    var badgeBg = document.createElementNS(svgNs, 'circle');
                    badgeBg.setAttribute('r', '0.12');
                    badgeBg.setAttribute('cx', '0');
                    badgeBg.setAttribute('cy', '0');
                    badgeBg.setAttribute('class', 'review-merge-badge__bg');
                    badgeG.appendChild(badgeBg);
                    var badgeText = document.createElementNS(svgNs, 'text');
                    badgeText.setAttribute('x', '0');
                    badgeText.setAttribute('y', '0.05');
                    badgeText.setAttribute('text-anchor', 'middle');
                    badgeText.setAttribute('class', 'review-merge-badge__text');
                    badgeText.setAttribute('font-size', '0.16');
                    badgeText.textContent = '↻' + (members.length + 1);
                    badgeG.appendChild(badgeText);
                    svg.appendChild(badgeG);  // outside flipped group
                }
            })(furniture[f0]);
        }

        return svg;
    }

    /**
     * Render the linked-product pill: thumbnail + name + dismiss.
     * 12-key product schema (plan §20a). Empty product → null.
     */
    function _renderProductPill(product, opts) {
        opts = opts || {};
        if (!product || typeof product !== 'object') return null;
        var imgUrl = product.thumbnail_url || '';
        var name = product.name || '(unnamed product)';

        var thumb = imgUrl
            ? el('img', {
                class: 'product-pill__thumb', src: imgUrl,
                alt: '', loading: 'lazy',
            })
            : el('span', { class: 'product-pill__thumb' });

        var children = [thumb, el('span', { class: 'product-pill__name' }, name)];
        if (opts.imageChanged) {
            children.push(el('span', {
                class: 'product-pill__changed',
                title: 'image changed since last search',
            }, '!'));
        }
        if (typeof opts.onClear === 'function') {
            children.push(el('button', {
                class: 'product-pill__clear', type: 'button',
                'aria-label': 'Remove linked product',
                on: {
                    click: function (e) {
                        e.stopPropagation();
                        opts.onClear();
                    },
                },
            }, '✕'));
        }
        return el('button', {
            class: 'product-pill', type: 'button',
            on: {
                click: function () {
                    if (typeof opts.onClick === 'function') opts.onClick();
                },
            },
        }, children);
    }

    function renderReviewScreen(scanId, scanName) {
        clearScreenTimers();
        setTopBar({
            title: 'Review — ' + scanName,
            backHref: '#/project/' + encodeURIComponent(scanId)
                + '/scan/' + encodeURIComponent(scanName),
            backLabel: 'Back to scan',
        });

        var screen = el('div', { class: 'screen review-screen', id: 'review-screen' });

        var headerHost = el('div', { class: 'review-header' });
        var staleBannerHost = el('div', { class: 'review-stale-banner-host' });
        var bodyHost = el('div', { class: 'review-body' });
        var stickyHost = el('div', { class: 'review-sticky-host' });

        screen.appendChild(headerHost);
        screen.appendChild(staleBannerHost);
        screen.appendChild(bodyHost);
        screen.appendChild(stickyHost);

        // Per-screen state.
        var ctx = {
            review: null,         // /api/.../review { run_id, review }
            result: null,         // /api/.../result envelope
            runId: null,
            runs: [],
            taxonomy: null,       // /api/taxonomy/classes
            activeTab: 'floorplan',
            selected: {},         // { bbox_id: true }
            cacheBust: {},        // { bbox_id: ts }
            // pulse target id when cross-tab navigating
            pulseFurnitureBboxId: null,
            pulseFloorplanBboxId: null,
        };

        // ---- helpers ----
        function reviewBaseUrl() {
            return '/api/project/' + encodeURIComponent(scanId)
                + '/scan/' + encodeURIComponent(scanName);
        }

        function bboxImgUrl(bboxId) {
            var idx = _bboxIdxByBboxId(ctx.result, bboxId);
            if (idx < 0) return '';
            var url = reviewBaseUrl() + '/best_view/' + idx + '.jpg';
            var ts = ctx.cacheBust[bboxId];
            return ts ? _recaptureBust(url, ts) : url;
        }

        function bboxesArr() {
            if (!ctx.result || !Array.isArray(ctx.result.furniture)) return [];
            return ctx.result.furniture;
        }

        function reviewMap() {
            return (ctx.review && ctx.review.bboxes) || {};
        }

        function isHiddenInList(bboxId) {
            var entry = reviewMap()[bboxId] || {};
            return entry.status === 'merged_into';
        }

        function bboxClass(bboxId) {
            var entry = reviewMap()[bboxId] || {};
            if (entry.class_override) return entry.class_override;
            var arr = bboxesArr();
            for (var i = 0; i < arr.length; i++) {
                if (arr[i].id === bboxId) return arr[i].class || '';
            }
            return '';
        }

        function distanceMeters(bboxId) {
            if (!ctx.result || !Array.isArray(ctx.result.best_images)) return null;
            for (var i = 0; i < ctx.result.best_images.length; i++) {
                var bi = ctx.result.best_images[i];
                if (bi && bi.bbox_id === bboxId) {
                    var d = bi.camera_distance_m;
                    if (typeof d === 'number') return d;
                }
            }
            return null;
        }

        // ---- header (tabs + run pill) ----
        function renderHeader() {
            headerHost.innerHTML = '';
            var furnCount = 0;
            var bxs = bboxesArr();
            var rmap = reviewMap();
            for (var i = 0; i < bxs.length; i++) {
                var bid = bxs[i].id;
                if (!bid) continue;
                if ((rmap[bid] || {}).status === 'merged_into') continue;
                furnCount++;
            }

            var reviewedAt = ctx.review && ctx.review.reviewed_at;

            // Single-page review (no tabs): floorplan, then furniture
            // cards, then notes — all stacked vertically in one scroll.
            // The ctx.activeTab value persists for legacy code paths
            // (sticky bar / floorplan-tap pulse) but every "switch tab"
            // call now is a no-op visual.
            var tabs = el('div',
                { class: 'review-section-summary' },
                'Furniture: ' + furnCount);

            var runPill = el('button', {
                class: 'review-run-pill', type: 'button',
                'aria-label': 'Switch run',
                on: { click: openRunsMenu },
            }, [
                el('span', null, 'Run ' + (ctx.runId ? '#' + _shortRun(ctx.runId) : '?')),
                el('span', { class: 'review-run-pill__chevron', html: ICONS.chevron }),
            ]);

            var reviewedPill = reviewedAt
                ? el('span', { class: 'pill pill--success review-reviewed-pill' },
                    '✓ Reviewed')
                : null;

            headerHost.appendChild(el('div', { class: 'review-header__row' }, [
                tabs, el('span', { class: 'review-header__spacer' }),
                reviewedPill, runPill,
            ]));
        }

        function _shortRun(runId) {
            // The run ids look like 20260425_142103. Show "1421" or last
            // 6 chars depending on length.
            if (!runId) return '';
            var parts = String(runId).split('_');
            if (parts.length === 2 && parts[1].length >= 4) {
                return parts[1].substring(0, 4);
            }
            return runId.length > 6 ? runId.slice(-6) : runId;
        }

        // ---- run-switching menu ----
        function openRunsMenu() {
            fetchJson('GET', reviewBaseUrl() + '/review/runs')
                .then(function (data) {
                    var runs = (data && data.runs) || [];
                    var active = data && data.active_run_id;
                    var rows = runs.map(function (r) {
                        var btn = el('button', {
                            class: 'runs-list__row' + (r.is_active ? ' is-active' : ''),
                            type: 'button',
                            on: {
                                click: function () {
                                    if (r.is_active) { closeModal(); return; }
                                    fetchJson('POST', reviewBaseUrl()
                                        + '/review/active_run',
                                        { run_id: r.run_id }
                                    ).then(function () {
                                        closeModal();
                                        showToast('Switched to ' + r.run_id);
                                        loadAll();
                                    }).catch(function (e) {
                                        showToast(e.message || 'Could not switch run',
                                            'error');
                                    });
                                },
                            },
                        }, [
                            el('span', { class: 'runs-list__id' }, r.run_id),
                            el('span', { class: 'runs-list__sub' }, [
                                r.is_active ? 'active' : '',
                                r.reviewed_at ? '· reviewed' : '',
                                r.submitted_at ? '· submitted' : '',
                            ].filter(Boolean).join(' ')),
                        ]);
                        return btn;
                    });
                    if (!rows.length) {
                        rows.push(el('div', { class: 'empty-state' }, [
                            el('div', { class: 'empty-state__label' }, 'No runs'),
                        ]));
                    }
                    openModal('Runs (active = #' + (active || '?') + ')',
                        [el('div', { class: 'runs-list' }, rows)]);
                }).catch(function (e) {
                    showToast(e.message || 'Could not load runs', 'error');
                });
        }

        // ---- stale banner ----
        function renderStaleBanner() {
            staleBannerHost.innerHTML = '';
            if (!ctx.review || !ctx.result) return;
            var resJobId = ctx.result.job_id;
            var revJobId = ctx.review.result_job_id;
            if (!resJobId || !revJobId) return;
            if (resJobId === revJobId) return;

            var banner = el('div', { class: 'review-stale-banner' }, [
                el('div', { class: 'review-stale-banner__msg' },
                    'Results were re-processed; review state may not apply.'),
                el('div', { class: 'review-stale-banner__actions' }, [
                    el('button', {
                        class: 'btn-secondary',
                        on: { click: discardReview },
                    }, 'Discard review and start fresh'),
                ]),
            ]);
            staleBannerHost.appendChild(banner);
        }

        function discardReview() {
            confirmDialog({
                title: 'Discard review?',
                body: 'This resets every bbox to "untouched" and clears your '
                    + 'notes. The original Modal output is unchanged.',
                confirmText: 'Discard', danger: true,
            }).then(function (ok) {
                if (!ok) return;
                // Per plan §10i: serial PATCHes. Clear notes first, then
                // every bbox status that's not already untouched.
                var rmap = reviewMap();
                var bids = Object.keys(rmap);
                var ops = [];
                ops.push(['notes', null]);
                for (var i = 0; i < bids.length; i++) {
                    var s = (rmap[bids[i]] || {}).status;
                    if (s && s !== 'untouched') {
                        ops.push(['status', bids[i]]);
                    }
                }
                var chain = Promise.resolve();
                ops.forEach(function (op) {
                    chain = chain.then(function () {
                        var body;
                        if (op[0] === 'notes') {
                            body = { notes: '' };
                        } else {
                            body = { bbox_id: op[1], status: 'untouched' };
                        }
                        return fetchJson('PATCH',
                            reviewBaseUrl() + '/review', body);
                    });
                });
                chain.then(function () {
                    showToast('Review discarded');
                    loadAll();
                }).catch(function (e) {
                    showToast(e.message || 'Could not discard review',
                        'error');
                });
            });
        }

        // ---- furniture cards ----
        function renderFurnitureTab(host) {
            host.innerHTML = '';
            var bxs = bboxesArr();
            var visible = bxs.filter(function (f) {
                return f && f.id && !isHiddenInList(f.id);
            });

            if (!visible.length) {
                host.appendChild(el('div', { class: 'empty-state' }, [
                    el('div', { class: 'empty-state__label' },
                        'No furniture detected'),
                    el('div', { class: 'empty-state__hint' },
                        'Model returned 0 bboxes for this run.'),
                    el('button', {
                        class: 'btn-secondary',
                        on: {
                            click: function () {
                                location.hash = '#/project/'
                                    + encodeURIComponent(scanId)
                                    + '/scan/' + encodeURIComponent(scanName);
                            },
                        },
                    }, 'Re-process'),
                ]));
                return;
            }

            var list = el('div', { class: 'review-furniture-list' });
            for (var i = 0; i < visible.length; i++) {
                list.appendChild(buildFurnitureCard(visible[i]));
            }
            host.appendChild(list);
        }

        function buildFurnitureCard(f) {
            var bboxId = f.id;
            var entry = reviewMap()[bboxId] || {};
            var status = entry.status || 'untouched';
            var isDeleted = (status === 'deleted');
            var members = entry.merged_from;
            var isMergedPrimary = (status === 'kept'
                && Array.isArray(members) && members.length > 0);
            var distance = distanceMeters(bboxId);
            var classNow = bboxClass(bboxId);

            // Image.
            var imgEl = el('img', {
                class: 'review-card__img', alt: '',
                src: bboxImgUrl(bboxId), loading: 'lazy',
                'data-bbox-id': bboxId,
                on: {
                    click: function () {
                        // Lightbox.
                        openModal('', [
                            el('img', {
                                class: 'review-lightbox-img',
                                src: bboxImgUrl(bboxId), alt: '',
                            }),
                        ]);
                    },
                },
            });

            // Class dropdown.
            var classSelect = el('select', {
                class: 'review-card__class',
                'aria-label': 'Class',
                'data-bbox-id': bboxId,
                on: {
                    change: function () {
                        var v = classSelect.value;
                        if (v === '__custom__') {
                            classSelect.value = classNow;  // revert until input
                            openCustomClassModal(bboxId);
                            return;
                        }
                        if (!v || v === classNow) return;
                        fetchJson('PATCH', reviewBaseUrl() + '/review',
                            { bbox_id: bboxId, class_override: v }
                        ).then(function () {
                            ensureBbox(bboxId).class_override = v;
                            renderHeader(); renderBody();
                        }).catch(function (e) {
                            showToast(e.message || 'Could not update class',
                                'error');
                        });
                    },
                },
            });
            populateClassOptions(classSelect, classNow);

            // Distance label.
            var distLabel = (distance != null)
                ? distance.toFixed(2) + ' m' : '';

            // Select checkbox.
            var checkbox = el('input', {
                class: 'review-card__check', type: 'checkbox',
                'aria-label': 'Select bbox ' + bboxId,
                'data-bbox-id': bboxId,
                on: {
                    change: function () {
                        if (checkbox.checked) ctx.selected[bboxId] = true;
                        else delete ctx.selected[bboxId];
                        renderStickyBar();
                        // Restyle this card.
                        if (Object.keys(ctx.selected).length) {
                            cardEl.classList.add('is-selectable');
                        } else {
                            cardEl.classList.remove('is-selectable');
                        }
                    },
                },
            });
            if (ctx.selected[bboxId]) checkbox.checked = true;

            var titleRow = el('div', { class: 'review-card__title-row' }, [
                el('span', { class: 'review-card__id' }, bboxId),
                classSelect,
                distLabel ? el('span', { class: 'review-card__dist' },
                    distLabel) : null,
                el('label', { class: 'review-card__check-wrap' }, [
                    checkbox,
                    el('span', null, 'select'),
                ]),
            ]);

            // Action row.
            var recapBtn = el('button', {
                class: 'btn-secondary review-card__action', type: 'button',
                on: { click: function () { openRecaptureOverlay(bboxId); } },
            }, 'Recapture');
            var findBtn = el('button', {
                class: 'btn-secondary review-card__action', type: 'button',
                on: { click: function () { openFindProductModal(bboxId); } },
            }, 'Find product');
            var delBtn = el('button', {
                class: (isDeleted ? 'btn-secondary' : 'btn-danger')
                    + ' review-card__action', type: 'button',
                on: { click: function () { toggleDelete(bboxId); } },
            }, isDeleted ? 'Undelete' : 'Delete');
            var planBtn = el('button', {
                class: 'btn-secondary review-card__action', type: 'button',
                on: { click: function () {
                    ctx.pulseFloorplanBboxId = bboxId;
                    ctx.activeTab = 'floorplan';
                    _setReviewTab(screen, 'floorplan');
                    renderBody();
                } },
            }, 'View on plan');

            var actionRow = el('div', { class: 'review-card__actions' }, [
                recapBtn, findBtn, delBtn, planBtn,
            ]);

            // Linked product pill (if any). The "image changed since last search"
            // badge is intentionally NOT computed here: without a server-side
            // ordering between image_override and search_attempted_at, the
            // heuristic produced false positives (badge fired even when the
            // recapture happened BEFORE the link). Re-enable when review.json
            // grows an `image_override_set_at` timestamp.
            var lp = entry.linked_product;
            var pill = lp ? _renderProductPill(lp, {
                imageChanged: false,
                onClick: function () { openFindProductModal(bboxId); },
                onClear: function () { linkProduct(bboxId, null); },
            }) : null;

            // Optional caption.
            var caption = null;
            if (isMergedPrimary) {
                caption = el('div', { class: 'review-card__caption' },
                    '↻ merged from ' + members.length + ' bbox'
                    + (members.length > 1 ? 'es' : '')
                    + ' (' + members.join(', ') + ')');
            }

            var rightCol = el('div', { class: 'review-card__col' }, [
                titleRow, actionRow, pill, caption,
            ]);

            var cardEl = el('div', {
                class: 'review-card'
                    + (isDeleted ? ' is-deleted' : '')
                    + (isMergedPrimary ? ' is-merged' : '')
                    + (Object.keys(ctx.selected).length
                        ? ' is-selectable' : ''),
                'data-bbox-id': bboxId,
                id: 'review-card-' + bboxId,
            }, [imgEl, rightCol]);

            // Pulse if requested from a floorplan tap.
            if (ctx.pulseFurnitureBboxId === bboxId) {
                setTimeout(function () {
                    cardEl.classList.add('pulse');
                    setTimeout(function () {
                        cardEl.classList.remove('pulse');
                    }, 1000);
                }, 50);
                ctx.pulseFurnitureBboxId = null;
            }

            return cardEl;
        }

        function ensureBbox(bboxId) {
            if (!ctx.review) ctx.review = { bboxes: {} };
            if (!ctx.review.bboxes) ctx.review.bboxes = {};
            if (!ctx.review.bboxes[bboxId]) {
                ctx.review.bboxes[bboxId] = { status: 'kept' };
            }
            return ctx.review.bboxes[bboxId];
        }

        function populateClassOptions(selectNode, current) {
            selectNode.innerHTML = '';
            var classes = (ctx.taxonomy && ctx.taxonomy.classes) || [];
            // Top 12 by count.
            var sorted = classes.slice().sort(function (a, b) {
                return (b.count || 0) - (a.count || 0);
            });
            var top = sorted.slice(0, 12);
            var rest = sorted.slice(12);

            function appendOpt(name) {
                var opt = document.createElement('option');
                opt.value = name; opt.textContent = name;
                if (name === current) opt.selected = true;
                selectNode.appendChild(opt);
            }
            // If `current` is missing from taxonomy entirely, add it first.
            var present = {};
            for (var i = 0; i < classes.length; i++) {
                present[classes[i].name] = true;
            }
            if (current && !present[current]) appendOpt(current);

            for (var j = 0; j < top.length; j++) appendOpt(top[j].name);
            if (rest.length) {
                var sep = document.createElement('option');
                sep.disabled = true; sep.textContent = '──────';
                selectNode.appendChild(sep);
                for (var k = 0; k < rest.length; k++) appendOpt(rest[k].name);
            }
            var customOpt = document.createElement('option');
            customOpt.value = '__custom__';
            customOpt.textContent = '✏ Type custom…';
            selectNode.appendChild(customOpt);
        }

        function openCustomClassModal(bboxId) {
            var input = el('input', {
                class: 'input', type: 'text',
                placeholder: 'New class name',
                autocapitalize: 'off', spellcheck: 'false',
            });
            var errBox = el('div', { class: 'modal__error' });
            var saveBtn = el('button', {
                class: 'btn-primary btn-lg',
                on: {
                    click: function () {
                        var v = (input.value || '').trim();
                        if (!v) {
                            errBox.textContent = 'name required';
                            return;
                        }
                        saveBtn.disabled = true;
                        fetchJson('POST', '/api/taxonomy/learned',
                            { name: v }
                        ).then(function () {
                            return fetchJson('PATCH',
                                reviewBaseUrl() + '/review',
                                { bbox_id: bboxId, class_override: v });
                        }).then(function () {
                            ensureBbox(bboxId).class_override = v;
                            // Refresh taxonomy in the background.
                            fetchJson('GET', '/api/taxonomy/classes')
                                .then(function (tx) { ctx.taxonomy = tx; })
                                .catch(function () { /* ignore */ });
                            closeModal();
                            renderBody();
                        }).catch(function (e) {
                            saveBtn.disabled = false;
                            errBox.textContent = e.message
                                || 'Could not save';
                        });
                    },
                },
            }, 'Save');
            var cancelBtn = el('button', {
                class: 'btn-secondary btn-lg',
                on: { click: function () { closeModal(); } },
            }, 'Cancel');
            openModal('Custom class', [
                el('label', { class: 'modal__label' }, 'Class name'),
                input, errBox,
                el('div', { class: 'modal__actions' }, [cancelBtn, saveBtn]),
            ]);
            setTimeout(function () { input.focus(); }, 0);
        }

        function toggleDelete(bboxId) {
            var entry = reviewMap()[bboxId] || {};
            var nextStatus = (entry.status === 'deleted') ? 'kept' : 'deleted';
            fetchJson('PATCH', reviewBaseUrl() + '/review',
                { bbox_id: bboxId, status: nextStatus }
            ).then(function () {
                ensureBbox(bboxId).status = nextStatus;
                renderBody();
            }).catch(function (e) {
                showToast(e.message || 'Could not toggle delete', 'error');
            });
        }

        function linkProduct(bboxId, product) {
            fetchJson('POST', reviewBaseUrl() + '/review/link_product',
                { bbox_id: bboxId, product: product }
            ).then(function (out) {
                if (out && out.bbox_state) {
                    ctx.review.bboxes[bboxId] = out.bbox_state;
                }
                renderBody();
            }).catch(function (e) {
                showToast(e.message || 'Could not link product', 'error');
            });
        }

        // ---- recapture overlay ----
        function openRecaptureOverlay(bboxId) {
            var overlay = el('div', { class: 'recapture-overlay' });
            var img = el('img', {
                class: 'recapture-overlay__feed',
                src: '/api/camera/preview', alt: '',
            });
            var captureBtn = el('button', {
                class: 'btn-primary btn-lg recapture-overlay__btn',
                type: 'button',
                on: {
                    click: function () {
                        captureBtn.disabled = true;
                        captureBtn.textContent = 'Capturing…';
                        var idx = _bboxIdxByBboxId(ctx.result, bboxId);
                        fetchJson('POST',
                            reviewBaseUrl() + '/review/recapture/' + idx
                        ).then(function (data) {
                            if (overlay.parentNode) {
                                overlay.parentNode.removeChild(overlay);
                            }
                            ctx.cacheBust[bboxId] = Date.now();
                            var bb = ensureBbox(bboxId);
                            if (data && data.path) bb.image_override = data.path;
                            showToast('Captured');
                            renderBody();
                        }).catch(function (e) {
                            captureBtn.disabled = false;
                            captureBtn.textContent = 'Capture';
                            if (e.status === 409) {
                                showToast(
                                    'Recording in progress — stop recording first.',
                                    'warn');
                            } else if (e.status === 503) {
                                var tail = e.body && e.body.stderr_tail;
                                showToast('Camera error'
                                    + (tail ? ': ' + tail : ''), 'error');
                            } else {
                                showToast(e.message || 'Capture failed',
                                    'error');
                            }
                        });
                    },
                },
            }, 'Capture');
            var cancelBtn = el('button', {
                class: 'btn-secondary btn-lg recapture-overlay__btn',
                type: 'button',
                on: {
                    click: function () {
                        if (overlay.parentNode) {
                            overlay.parentNode.removeChild(overlay);
                        }
                    },
                },
            }, 'Cancel');
            overlay.appendChild(img);
            overlay.appendChild(el('div',
                { class: 'recapture-overlay__row' },
                [captureBtn, cancelBtn]));
            document.body.appendChild(overlay);
        }

        // ---- find-product: prefetch + cache + modal -----------------
        //
        // Strategy: when the review screen first loads `result.json`, we
        // queue every bbox for a background visual-search prefetch (with
        // small concurrency so we don't melt the Athathi server). Each
        // result lands in `ctx.findProductCache[bboxId]` AND the backend's
        // sha1-keyed disk cache. When the user taps "Find product", the
        // modal reads from `ctx.findProductCache` first — instant render.
        //
        // The prefetch is governed by `config.visual_search_prefetch`
        // (default true). Set to false in Settings to disable (for slow
        // links / metered connections).

        ctx.findProductCache = ctx.findProductCache || {};
        ctx.findProductPrefetchQueue = [];
        ctx.findProductPrefetchActive = 0;
        var FP_PREFETCH_CONCURRENCY = 2;

        function _fpPrefetchNext() {
            if (ctx.findProductPrefetchActive >= FP_PREFETCH_CONCURRENCY) return;
            var job = ctx.findProductPrefetchQueue.shift();
            if (!job) return;
            ctx.findProductPrefetchActive++;
            fetchJson('POST', reviewBaseUrl() + '/review/find_product/' + job.idx,
                null, { skipAuthRedirect: true })
                .then(function (data) {
                    ctx.findProductCache[job.bboxId] = data || { results: [] };
                })
                .catch(function () {
                    // Quiet — modal will retry on demand.
                })
                .then(function () {
                    ctx.findProductPrefetchActive--;
                    _fpPrefetchNext();
                });
            _fpPrefetchNext();  // fill the second concurrency slot
        }

        function _fpKickoffPrefetch() {
            // Default: prefetch on. Disable only when state.config exists
            // AND explicitly says false. This means the review screen
            // doesn't have to wait for /api/settings/config to load.
            if (state.config
                    && state.config.visual_search_prefetch === false) {
                return;
            }
            var bbis = (ctx.result && ctx.result.best_images) || [];
            var seenInQueue = {};
            for (var i = 0; i < bbis.length; i++) {
                var b = bbis[i];
                if (!b || !b.bbox_id) continue;
                if (ctx.findProductCache[b.bbox_id]) continue;
                if (seenInQueue[b.bbox_id]) continue;
                seenInQueue[b.bbox_id] = true;
                ctx.findProductPrefetchQueue.push({
                    bboxId: b.bbox_id, idx: i,
                });
            }
            for (var c = 0; c < FP_PREFETCH_CONCURRENCY; c++) _fpPrefetchNext();
        }
        // Kick off once result data is on hand AND state.config has been
        // loaded. Called from the Promise.all in loadAll() AFTER ctx.result
        // is populated (registered via `ctx.onResultLoaded` so we don't
        // need to thread it through closures).
        function _fpEnsureConfigLoaded() {
            if (state.config) return Promise.resolve();
            return fetchJson('GET', '/api/settings/config', null,
                { skipAuthRedirect: true })
                .then(function (cfg) { state.config = cfg || {}; })
                .catch(function () { /* leave undefined → defaults kick in */ });
        }
        ctx.onResultLoaded = function () {
            if (!ctx.result) return;
            _fpEnsureConfigLoaded().then(_fpKickoffPrefetch);
        };

        function _fpTopK() {
            var k = state.config && state.config.visual_search_top_k;
            return (typeof k === 'number' && k > 0) ? k : 6;
        }

        function openFindProductModal(bboxId) {
            var bodyHostL = el('div', { class: 'find-product__body' });

            var noMatchBtn = el('button', {
                class: 'btn-secondary btn-lg', type: 'button',
                on: {
                    click: function () {
                        linkProduct(bboxId, null);
                        closeModal();
                    },
                },
            }, 'No match');
            var closeBtn = el('button', {
                class: 'btn-secondary btn-lg', type: 'button',
                on: { click: function () { closeModal(); } },
            }, 'Close');

            openModal('Find product for ' + bboxId,
                [bodyHostL,
                 el('div', { class: 'modal__actions' },
                    [noMatchBtn, closeBtn])]);

            var idx = _bboxIdxByBboxId(ctx.result, bboxId);
            if (idx < 0) {
                bodyHostL.innerHTML = '';
                bodyHostL.appendChild(el('div', { class: 'empty-state' }, [
                    el('div', { class: 'empty-state__label' },
                        'No image on disk for this bbox'),
                ]));
                return;
            }

            function renderResults(data) {
                bodyHostL.innerHTML = '';
                var results = (data && data.results) || [];
                var topK = _fpTopK();
                results = results.slice(0, topK);
                if (!results.length) {
                    bodyHostL.appendChild(el('div', { class: 'empty-state' }, [
                        el('div', { class: 'empty-state__label' },
                            'No matches'),
                    ]));
                    return;
                }
                var grid = el('div', { class: 'find-product__grid' });
                for (var i = 0; i < results.length; i++) {
                    grid.appendChild(buildProductCandidate(bboxId, results[i]));
                }
                bodyHostL.appendChild(grid);
            }

            // Cache hit — render synchronously, no spinner.
            if (ctx.findProductCache[bboxId]) {
                renderResults(ctx.findProductCache[bboxId]);
                return;
            }

            // Cache miss — show spinner, fetch, cache, render.
            bodyHostL.appendChild(el('div', { class: 'loading-row' }, [
                el('span', { class: 'spinner' }),
                el('span', null, 'Searching…'),
            ]));
            fetchJson('POST', reviewBaseUrl() + '/review/find_product/' + idx)
                .then(function (data) {
                    ctx.findProductCache[bboxId] = data || { results: [] };
                    renderResults(data);
                })
                .catch(function (e) {
                    bodyHostL.innerHTML = '';
                    bodyHostL.appendChild(el('div', { class: 'empty-state' }, [
                        el('div', { class: 'empty-state__label' },
                            'Search failed'),
                        el('div', { class: 'empty-state__hint' },
                            e.message || 'Network error'),
                    ]));
                });
        }

        function buildProductCandidate(bboxId, p) {
            var thumb = p.thumbnail_url
                ? el('img', { class: 'product-card__thumb', src: p.thumbnail_url, alt: '' })
                : el('span', { class: 'product-card__thumb' });
            var meta = [];
            if (p.category) meta.push(p.category);
            if (p.store_name) meta.push(p.store_name);
            if (p.width && p.height) {
                meta.push(p.width + ' cm × ' + p.height + ' cm');
            }
            return el('button', {
                class: 'product-card', type: 'button',
                on: {
                    click: function () {
                        linkProduct(bboxId, p);
                        closeModal();
                    },
                },
            }, [
                thumb,
                el('div', { class: 'product-card__col' }, [
                    el('div', { class: 'product-card__name' },
                        p.name || '(unnamed)'),
                    el('div', { class: 'product-card__meta' },
                        meta.join(' · ')),
                    el('div', { class: 'product-card__sim' },
                        'similarity ' + (typeof p.similarity === 'number'
                            ? p.similarity.toFixed(2) : '?')),
                ]),
            ]);
        }

        // ---- multi-select sticky bar + merge modal ----
        function renderStickyBar() {
            stickyHost.innerHTML = '';
            var ids = Object.keys(ctx.selected);
            if (!ids.length) return;
            var n = ids.length;
            var mergeBtn = el('button', {
                class: 'btn-primary btn-lg', type: 'button',
                disabled: n < 2,
                on: { click: openMergeModal },
            }, 'Merge into one (' + n + ')');
            var delBtn = el('button', {
                class: 'btn-danger btn-lg', type: 'button',
                on: {
                    click: function () {
                        // Serialize PATCH calls — backend's read-modify-write
                        // on review.json races under concurrent calls.
                        var chain = Promise.resolve();
                        ids.forEach(function (id) {
                            chain = chain.then(function () {
                                return fetchJson('PATCH',
                                    reviewBaseUrl() + '/review',
                                    { bbox_id: id, status: 'deleted' }
                                ).then(function () {
                                    ensureBbox(id).status = 'deleted';
                                }).catch(function () { /* per-id swallow */ });
                            });
                        });
                        ctx.selected = {};
                        chain.then(function () { loadAll(); });
                    },
                },
            }, 'Delete (' + n + ')');
            var cancelBtn = el('button', {
                class: 'btn-secondary btn-lg', type: 'button',
                on: {
                    click: function () {
                        ctx.selected = {};
                        renderBody();
                    },
                },
            }, 'Cancel');
            stickyHost.appendChild(el('div', { class: 'review-sticky' },
                [mergeBtn, delBtn, cancelBtn]));
        }

        function openMergeModal() {
            var selectedIds = Object.keys(ctx.selected);
            if (selectedIds.length < 2) return;
            var bxs = bboxesArr();
            var byId = {};
            for (var i = 0; i < bxs.length; i++) byId[bxs[i].id] = bxs[i];
            var infos = selectedIds.map(function (id) {
                var f = byId[id] || {};
                return {
                    id: id,
                    size: f.size || [0, 0, 0],
                    cls: bboxClass(id) || (f.class || ''),
                };
            });
            var primaryId = _pickPrimary(infos);
            var classes = infos.map(function (x) { return x.cls; });
            var defaultClass = _pickMostCommonClass(classes);
            var memberIds = infos
                .filter(function (x) { return x.id !== primaryId; })
                .map(function (x) { return x.id; });

            var primaryInfo = infos.filter(
                function (x) { return x.id === primaryId; })[0];
            var primaryVol = primaryInfo
                ? (primaryInfo.size[0] * primaryInfo.size[1]
                   * primaryInfo.size[2])
                : 0;

            var radioName = 'merge-class-' + Date.now();
            var classOptions = [];
            // Most common first.
            if (defaultClass) classOptions.push(defaultClass);
            // Then unique others.
            for (var c = 0; c < classes.length; c++) {
                if (classes[c] && classOptions.indexOf(classes[c]) === -1) {
                    classOptions.push(classes[c]);
                }
            }

            var customInput = el('input', {
                class: 'input', type: 'text',
                placeholder: 'New class', autocapitalize: 'off',
            });

            var radios = [];
            classOptions.forEach(function (cl, ix) {
                var rb = el('input', {
                    type: 'radio', name: radioName, value: cl,
                });
                if (ix === 0) rb.checked = true;
                radios.push(rb);
                var label = el('label', { class: 'merge-modal__radio' }, [
                    rb,
                    el('span', null, cl + (cl === defaultClass
                        ? ' (most common)' : '')),
                ]);
                classOptions[ix] = { value: cl, node: label, radio: rb };
            });
            var customRadio = el('input', {
                type: 'radio', name: radioName, value: '__custom__',
            });
            var customLabel = el('label', { class: 'merge-modal__radio' }, [
                customRadio,
                el('span', null, '✏ Type custom: '),
                customInput,
            ]);

            var errBox = el('div', { class: 'modal__error' });
            var mergeBtn = el('button', {
                class: 'btn-primary btn-lg', type: 'button',
                on: {
                    click: function () {
                        var chosen = null;
                        for (var k = 0; k < classOptions.length; k++) {
                            if (classOptions[k].radio.checked) {
                                chosen = classOptions[k].value; break;
                            }
                        }
                        if (customRadio.checked) {
                            chosen = (customInput.value || '').trim();
                            if (!chosen) {
                                errBox.textContent = 'custom class required';
                                return;
                            }
                        }
                        mergeBtn.disabled = true;
                        var doMerge = function () {
                            return fetchJson('POST',
                                reviewBaseUrl() + '/review/merge',
                                {
                                    primary_id: primaryId,
                                    member_ids: memberIds,
                                    chosen_class: chosen,
                                });
                        };
                        var p;
                        if (customRadio.checked && chosen) {
                            p = fetchJson('POST', '/api/taxonomy/learned',
                                { name: chosen }).catch(function () {})
                                .then(doMerge);
                        } else {
                            p = doMerge();
                        }
                        p.then(function () {
                            ctx.selected = {};
                            closeModal();
                            showToast('Merged');
                            loadAll();
                        }).catch(function (e) {
                            mergeBtn.disabled = false;
                            if (e.status === 409) {
                                errBox.textContent = e.message
                                    || 'already merged into a different primary';
                            } else {
                                errBox.textContent = e.message
                                    || 'merge failed';
                            }
                        });
                    },
                },
            }, 'Merge');
            var cancelBtn = el('button', {
                class: 'btn-secondary btn-lg', type: 'button',
                on: { click: function () { closeModal(); } },
            }, 'Cancel');

            openModal('Merge ' + selectedIds.length + ' bboxes into one', [
                el('div', { class: 'merge-modal__row' }, [
                    el('span', { class: 'merge-modal__label' }, 'Primary (largest):'),
                    el('span', null, primaryId
                        + (primaryInfo ? ' ' + primaryInfo.cls : '')
                        + ' (' + primaryVol.toFixed(2) + ' m³)'),
                ]),
                el('div', { class: 'merge-modal__row' }, [
                    el('span', { class: 'merge-modal__label' }, 'Members:'),
                    el('span', null, memberIds.join(', ')),
                ]),
                el('div', { class: 'merge-modal__label' },
                    'Class for the merged item:'),
                el('div', { class: 'merge-modal__radios' }, classOptions
                    .map(function (o) { return o.node; })
                    .concat([customLabel])),
                errBox,
                el('div', { class: 'modal__actions' }, [cancelBtn, mergeBtn]),
            ]);
        }

        // ---- notes tab ----
        function renderNotesTab(host) {
            host.innerHTML = '';
            var notes = (ctx.review && ctx.review.notes) || '';
            var counter = el('div', { class: 'review-notes__counter' },
                notes.length + ' chars');
            var debounceTimer = null;
            var ta = el('textarea', {
                class: 'review-notes__input',
                placeholder: 'Free-text notes for this scan…',
                spellcheck: 'true',
                'aria-label': 'Notes',
                on: {
                    input: function () {
                        counter.textContent = ta.value.length + ' chars';
                        if (debounceTimer) clearTimeout(debounceTimer);
                        debounceTimer = setTimeout(saveNotes, 600);
                    },
                    blur: function () {
                        if (debounceTimer) {
                            clearTimeout(debounceTimer);
                            debounceTimer = null;
                        }
                        saveNotes();
                    },
                },
            });
            ta.value = notes;

            function saveNotes() {
                var v = ta.value || '';
                if (v === ((ctx.review && ctx.review.notes) || '')) return;
                fetchJson('PATCH', reviewBaseUrl() + '/review',
                    { notes: v }
                ).then(function () {
                    if (ctx.review) ctx.review.notes = v;
                }).catch(function (e) {
                    showToast(e.message || 'Could not save notes', 'error');
                });
            }

            host.appendChild(el('div', { class: 'review-notes' }, [ta, counter]));
        }

        // ---- floorplan tab ----
        function renderFloorplanTab(host) {
            host.innerHTML = '';
            var svg = _renderReviewFloorplanSvg(ctx.result, reviewMap(), {
                selectedSet: ctx.selected,
                onTap: function (bboxId) {
                    if (ctx.selected[bboxId]) {
                        delete ctx.selected[bboxId];
                    } else {
                        ctx.selected[bboxId] = true;
                    }
                    ctx.pulseFurnitureBboxId = bboxId;
                    renderBody();
                },
                onCanvasTap: function () {
                    // Tap on blank canvas (no rect under finger) — clear
                    // any selection so the technician can start fresh.
                    if (Object.keys(ctx.selected).length === 0) return;
                    ctx.selected = {};
                    renderBody();
                },
            });
            host.appendChild(el('div', { class: 'review-floorplan-wrap' }, svg));

            // Pulse the requested rect (cross-tab nav).
            if (ctx.pulseFloorplanBboxId) {
                var bid = ctx.pulseFloorplanBboxId;
                ctx.pulseFloorplanBboxId = null;
                setTimeout(function () {
                    var rect = host.querySelector(
                        'rect[data-bbox-id="' + bid + '"]');
                    if (rect) {
                        rect.classList.add('pulse');
                        setTimeout(function () {
                            rect.classList.remove('pulse');
                        }, 1000);
                    }
                }, 60);
            }
        }

        // ---- mark reviewed action ----
        function renderMarkReviewed(host) {
            var reviewedAt = ctx.review && ctx.review.reviewed_at;
            if (reviewedAt) {
                host.appendChild(el('div',
                    { class: 'review-mark-host' }, [
                    el('span', { class: 'pill pill--success review-mark-pill' },
                        '✓ Reviewed'),
                ]));
                return;
            }
            var btn = el('button', {
                class: 'btn-primary btn-lg review-mark-btn', type: 'button',
                on: {
                    click: function () {
                        btn.disabled = true;
                        fetchJson('POST',
                            reviewBaseUrl() + '/review/mark_reviewed'
                        ).then(function () {
                            if (!ctx.review) ctx.review = {};
                            ctx.review.reviewed_at =
                                new Date().toISOString();
                            renderHeader(); renderBody();
                            showToast('Marked reviewed');
                        }).catch(function (e) {
                            btn.disabled = false;
                            showToast(e.message || 'Could not mark reviewed',
                                'error');
                        });
                    },
                },
            }, 'Mark this scan reviewed');
            host.appendChild(el('div', { class: 'review-mark-host' }, [btn]));
        }

        // ---- body: single-scroll layout (floorplan → furniture → notes) ----
        function renderBody() {
            bodyHost.innerHTML = '';
            var floorpHost = el('div', { class: 'review-section' });
            var furnHost = el('div', { class: 'review-section' });
            var notesHost = el('div', { class: 'review-section' });
            renderFloorplanTab(floorpHost);
            renderFurnitureTab(furnHost);
            renderNotesTab(notesHost);
            bodyHost.appendChild(floorpHost);
            bodyHost.appendChild(furnHost);
            bodyHost.appendChild(notesHost);

            renderMarkReviewed(bodyHost);
            renderStickyBar();
        }

        // ---- data load ----
        function loadAll() {
            bodyHost.innerHTML = '';
            bodyHost.appendChild(el('div', { class: 'loading-row' }, [
                el('span', { class: 'spinner' }),
                el('span', null, 'Loading review…'),
            ]));

            var p1 = fetchJson('GET', reviewBaseUrl() + '/review')
                .then(function (data) {
                    ctx.runId = data && data.run_id;
                    ctx.review = (data && data.review) || null;
                });
            var p2 = fetchJson('GET', reviewBaseUrl() + '/result')
                .then(function (data) {
                    ctx.result = (data && data.result) || null;
                });
            var p3 = fetchJson('GET', '/api/taxonomy/classes')
                .then(function (tx) { ctx.taxonomy = tx; })
                .catch(function () { ctx.taxonomy = { classes: [] }; });

            Promise.all([p1, p2, p3]).then(function () {
                renderHeader();
                renderStaleBanner();
                renderBody();
                // Fire the visual-search prefetch the moment result data
                // is on hand — see `_fpKickoffPrefetch` for the queue.
                if (typeof ctx.onResultLoaded === 'function') {
                    ctx.onResultLoaded();
                }
            }).catch(function (e) {
                bodyHost.innerHTML = '';
                bodyHost.appendChild(el('div', { class: 'empty-state' }, [
                    el('div', { class: 'empty-state__label' },
                        'Could not load review'),
                    el('div', { class: 'empty-state__hint' },
                        e.message || 'Network error'),
                ]));
            });
        }

        loadAll();
        return screen;
    }

    // -----------------------------------------------------------------
    // 12. Router
    // -----------------------------------------------------------------

    function parseRoute(hash) {
        // Normalize: drop leading '#', leading '/'.
        var path = (hash || '').replace(/^#\/?/, '');
        if (path === '') return { name: 'projects' };  // default once logged in
        var parts = path.split('/');
        if (parts[0] === 'login') return { name: 'login' };
        if (parts[0] === 'projects') return { name: 'projects' };
        if (parts[0] === 'settings') return { name: 'settings' };
        if (parts[0] === 'project' && parts.length === 2) {
            return { name: 'project', projectId: decodeURIComponent(parts[1]) };
        }
        if (parts[0] === 'project' && parts.length === 4 && parts[2] === 'scan') {
            return {
                name: 'scan',
                projectId: decodeURIComponent(parts[1]),
                scanName: decodeURIComponent(parts[3]),
            };
        }
        if (parts[0] === 'project' && parts.length === 5
                && parts[2] === 'scan' && parts[4] === 'review') {
            return {
                name: 'review',
                projectId: decodeURIComponent(parts[1]),
                scanName: decodeURIComponent(parts[3]),
            };
        }
        return { name: 'unknown', raw: path };
    }

    /**
     * Route table — used by tests + the dispatcher below. Keys are route
     * names returned from parseRoute, values are the render functions.
     */
    var ROUTES = {
        login: renderLogin,
        projects: renderProjects,
        project: renderProjectWorkspace,
        scan: renderScanWorkspace,
        review: renderReviewScreen,
        settings: renderSettings,
    };

    function render(route) {
        var root = document.getElementById('app-root');
        if (!root) return;

        // Close any open modal so hash-route navigation can't strand UI on
        // top of the swapped screen. The modal's onClose runs and clears
        // any per-modal timers (e.g. submit-modal phase walker).
        if (typeof closeModal === 'function') {
            try { closeModal(); } catch (_) { /* no modal open */ }
        }

        // Tear down any per-screen SSE listener before swapping the DOM.
        var prevScreen = root.querySelector('.screen');
        if (prevScreen && prevScreen._sseHandler) {
            window.removeEventListener('sse-tick', prevScreen._sseHandler);
        }
        clearScreenTimers();

        // Wipe everything except the legacy block (kept hidden, never destroyed).
        var legacy = document.getElementById('legacy-root');
        var keep = legacy;
        var children = Array.prototype.slice.call(root.childNodes);
        for (var i = 0; i < children.length; i++) {
            if (children[i] !== keep) root.removeChild(children[i]);
        }

        if (route.name === 'login') {
            // Login owns its own chrome — no top bar.
            setTopBar(null);
            root.appendChild(renderLogin());
            return;
        }

        // Render the screen first so each render fn can populate setTopBar.
        var screenNode;
        if (route.name === 'projects') screenNode = renderProjects();
        else if (route.name === 'project') screenNode = renderProjectWorkspace(route.projectId);
        else if (route.name === 'scan') screenNode = renderScanWorkspace(route.projectId, route.scanName);
        else if (route.name === 'review') screenNode = renderReviewScreen(route.projectId, route.scanName);
        else if (route.name === 'settings') screenNode = renderSettings();
        else {
            location.hash = '#/projects';
            return;
        }

        // Now mount: top bar (with current state.topBar config) + screen.
        // Screens that own their own header (e.g. Settings) can call
        // `setTopBar(null)` to suppress the global top bar.
        if (state.topBar !== null) {
            root.appendChild(renderTopbar());
        }
        root.appendChild(screenNode);
    }

    function navigate() {
        var route = parseRoute(location.hash);
        state.currentRoute = route;

        // Settings is reachable both pre- and post-login (per the plan: the
        // operator must be able to fix the API URL even when logged out).
        if (route.name === 'login' || route.name === 'settings') {
            render(route);
            return;
        }

        // For everything else, require auth.
        if (state.user && state.user.is_logged_in) {
            render(route);
            return;
        }

        // Probe /api/auth/me lazily; cache result in state.user.
        fetchJson('GET', '/api/auth/me', null, { skipAuthRedirect: true }).then(function (me) {
            state.user = me || null;
            if (me && me.is_logged_in) {
                render(route);
            } else {
                location.hash = '#/login';
            }
        }).catch(function () {
            // No network / backend unreachable — push to login regardless.
            state.user = null;
            location.hash = '#/login';
        });
    }

    // -----------------------------------------------------------------
    // 13. SSE bridge — own EventSource + window 'sse-tick' event.
    // -----------------------------------------------------------------

    var _sseSource = null;
    var _sseRetryTimer = null;

    function _connectSse() {
        if (_sseSource) {
            try { _sseSource.close(); } catch (_) { /* ignore */ }
        }
        try {
            _sseSource = new EventSource('/api/events');
        } catch (e) {
            _sseSource = null;
            return;
        }
        _sseSource.onmessage = function (e) {
            var payload;
            try { payload = JSON.parse(e.data); } catch (_) { return; }
            state.lastSse = payload;
            try {
                window.dispatchEvent(new CustomEvent('sse-tick', { detail: payload }));
            } catch (_) {
                // IE fallback (defensive — we don't ship to IE but keep it tolerant).
                var ev = document.createEvent('Event');
                ev.initEvent('sse-tick', true, true);
                ev.detail = payload;
                window.dispatchEvent(ev);
            }
        };
        _sseSource.onerror = function () {
            try { _sseSource.close(); } catch (_) { /* ignore */ }
            _sseSource = null;
            if (_sseRetryTimer) clearTimeout(_sseRetryTimer);
            _sseRetryTimer = setTimeout(_connectSse, 3000);
        };
    }

    // -----------------------------------------------------------------
    // 14. Boot
    // -----------------------------------------------------------------

    function boot() {
        // Make sure #app-root exists; the template provides it but be safe.
        if (!document.getElementById('app-root')) {
            var root = el('div', { id: 'app-root' });
            document.body.insertBefore(root, document.body.firstChild);
        }

        // JS-1: OSK awareness — when an editable element gains focus, mark
        // body.osk-open so CSS can lift the toast above the keyboard and
        // hide the sticky multi-select bar. Scroll the field into view.
        document.addEventListener('focusin', function (e) {
            var t = e.target;
            if (t && t.matches && t.matches('input,textarea,select')) {
                document.body.classList.add('osk-open');
                try { t.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch (_) { /* ignore */ }
            }
        });
        document.addEventListener('focusout', function () {
            setTimeout(function () {
                var a = document.activeElement;
                if (!a || !a.matches || !a.matches('input,textarea,select')) {
                    document.body.classList.remove('osk-open');
                }
            }, 200);
        });

        window.addEventListener('hashchange', navigate);

        // Initial probe.
        fetchJson('GET', '/api/auth/me', null, { skipAuthRedirect: true })
            .then(function (me) {
                state.user = me || null;
                if (!location.hash) {
                    location.hash = (me && me.is_logged_in) ? '#/projects' : '#/login';
                    return;  // hashchange triggers render
                }
                navigate();
                if (me && me.is_logged_in) _connectSse();
            })
            .catch(function () {
                state.user = null;
                if (!location.hash || location.hash === '#/' || location.hash === '#') {
                    location.hash = '#/login';
                } else {
                    navigate();
                }
            });
    }

    // -----------------------------------------------------------------
    // 15. Public surface
    // -----------------------------------------------------------------

    var AppShell = {
        boot: boot,
        navigate: navigate,
        parseRoute: parseRoute,
        fetchJson: fetchJson,
        showToast: showToast,
        ICONS: ICONS,
        state: state,
        ROUTES: ROUTES,
        setTopBar: setTopBar,
        confirm: confirmDialog,
        openModal: openModal,
        closeModal: closeModal,
        validateScanName: validateScanName,
        bootLegacyOnce: function () {
            if (state.legacyBooted) return;
            if (window.LegacyApp && typeof window.LegacyApp.init === 'function') {
                window.LegacyApp.init();
                state.legacyBooted = true;
            }
        },
        // Exposed for tests. Pure functions, no DOM side-effects.
        _scanPrimaryAction: _scanPrimaryAction,
        _scanState: _scanState,
        _submitGatingMessage: _submitGatingMessage,
        _projectStatusPill: _projectStatusPill,
        _scanRowStatusLabel: _scanRowStatusLabel,
        // Step 12 submit-modal + retry-sweep helpers (DOM + IO; tests
        // probe the source rather than driving the live DOM).
        _openSubmitModal: _openSubmitModal,
        _submitPendingProjects: _submitPendingProjects,
        _runSubmitRetrySweep: _runSubmitRetrySweep,
        fmtMmSs: fmtMmSs,
        fmtSlotRange: fmtSlotRange,
        fmtRelative: fmtRelative,
        // Step 10 review-tool helpers (pure / DOM utilities, exposed for tests).
        _pickPrimary: _pickPrimary,
        _pickMostCommonClass: _pickMostCommonClass,
        _recaptureBust: _recaptureBust,
        _bboxIdxByBboxId: _bboxIdxByBboxId,
        _setReviewTab: _setReviewTab,
        _renderReviewFloorplanSvg: _renderReviewFloorplanSvg,
        _renderProductPill: _renderProductPill,
        renderReviewScreen: renderReviewScreen,
        // Step 11 settings helpers (pure utilities, exposed for tests).
        renderSettings: renderSettings,
        _jwtExpiryFromToken: _jwtExpiryFromToken,
        _settingsSaveEnabled: _settingsSaveEnabled,
        _setSettingsTab: _setSettingsTab,
        SETTINGS_LOG_PATH: SETTINGS_LOG_PATH,
    };

    window.AppShell = AppShell;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
})();
