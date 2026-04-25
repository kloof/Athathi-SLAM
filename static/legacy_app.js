/* ===========================================================
 * legacy_app.js — namespaced legacy recording / processing UI.
 *
 * Step 8 of TECHNICIAN_REVIEW_PLAN.md §16 / §23d:
 *   "Existing JS to wrap in `LegacyApp` namespace rather than
 *    rewriting: connectSSE, updateProcessingUI, processSession,
 *    renderFloorplanSvg, renderGallery, renderResultBlock,
 *    loadSessions, checkStatus, startRecording / stopRecording,
 *    calibration trio, setExtrinsicsManual."
 *
 * The function bodies are lifted verbatim from the previous
 * inline <script> in templates/index.html so behaviour is
 * preserved 1:1. Each function only touches DOM nodes that
 * still live under #legacy-root, and every getElementById
 * lookup is checked for truthiness before use (the original
 * already did this for the optional ones; we add guards on
 * the rest so the legacy block can safely be hidden).
 *
 * Exposed:
 *   window.LegacyApp = {
 *     // state
 *     previewErrors, isRecording, processingStates,
 *     resultCache, resultInFlight, evtSource,
 *     // SSE
 *     connectSSE, updateProcessingUI, updateRecordingUI,
 *     // status / session list
 *     checkStatus, loadSessions, fetchResult,
 *     // recording
 *     startRecording, stopRecording,
 *     // processing / sessions
 *     processSession, deleteSession,
 *     // result rendering
 *     renderFloorplanSvg, renderGallery, renderResultBlock,
 *     // calibration
 *     startIntrinsicCalib, stopIntrinsicCalib, pollCalibStatus,
 *     setExtrinsicsManual,
 *     // lifecycle
 *     init,
 *   };
 *
 * Steps 9-10 will absorb these into the new screens; this
 * step just keeps them functional in a hidden state.
 * =========================================================== */

(function () {
    'use strict';

    var LegacyApp = {
        // --- mutable state ---
        evtSource: null,
        isRecording: false,
        processingStates: {},
        previewErrors: 0,
        resultCache: {},     // session_id -> envelope
        resultInFlight: {},  // session_id -> bool
        _statusTimer: null,
        _initialized: false,
    };

    // expose `previewErrors` as a callable lvalue for legacy inline handlers
    // (the original onerror/onload did `previewErrors++`). The DOM handlers
    // were rewritten below to call LegacyApp methods directly.

    // --- SSE -----------------------------------------------------------

    LegacyApp.connectSSE = function () {
        if (LegacyApp.evtSource) {
            try { LegacyApp.evtSource.close(); } catch (_) { /* ignore */ }
        }
        var src = new EventSource('/api/events');
        LegacyApp.evtSource = src;
        src.onmessage = function (e) {
            var d;
            try { d = JSON.parse(e.data); } catch (_) { return; }
            LegacyApp.updateRecordingUI(d);
            LegacyApp.updateProcessingUI(d.processing || {});
        };
        src.onerror = function () {
            try { src.close(); } catch (_) { /* ignore */ }
            setTimeout(LegacyApp.connectSSE, 3000);
        };
    };

    LegacyApp.updateProcessingUI = function (processing) {
        var wasProcessing = Object.keys(LegacyApp.processingStates);
        var nowProcessing = Object.keys(processing);
        var needRefresh = false;

        // Refresh if something finished or stage changed.
        for (var i = 0; i < wasProcessing.length; i++) {
            var sid = wasProcessing[i];
            if (nowProcessing.indexOf(sid) === -1) {
                needRefresh = true;
            } else {
                var before = LegacyApp.processingStates[sid];
                var now = processing[sid];
                if (before.status !== now.status || before.stage !== now.stage) {
                    needRefresh = true;
                }
            }
        }

        LegacyApp.processingStates = processing;
        if (needRefresh) LegacyApp.loadSessions();

        // Update inline progress lines.
        for (var sid2 in processing) {
            if (!Object.prototype.hasOwnProperty.call(processing, sid2)) continue;
            var info = processing[sid2];
            var el = document.getElementById('slam-progress-' + sid2);
            if (el) {
                var mins = Math.floor(info.elapsed / 60);
                var secs = Math.floor(info.elapsed % 60);
                var stage = info.stage || info.progress || info.status || 'working';
                el.textContent = 'Processing — ' + stage + ' (' + mins + ':' + secs.toString().padStart(2, '0') + ')';
            }
        }
    };

    LegacyApp.updateRecordingUI = function (d) {
        var banner = document.getElementById('recording-banner');
        var btnStart = document.getElementById('btn-start');
        var btnStop = document.getElementById('btn-stop');
        var nameInput = document.getElementById('session-name');
        var timer = document.getElementById('rec-timer');
        var stateEl = document.getElementById('s-state');

        if (d.recording) {
            LegacyApp.isRecording = true;
            if (banner) banner.style.display = 'block';
            if (btnStart) btnStart.style.display = 'none';
            if (btnStop) btnStop.style.display = 'block';
            if (nameInput) nameInput.disabled = true;
            var mins = Math.floor(d.elapsed / 60);
            var secs = Math.floor(d.elapsed % 60);
            if (timer) timer.textContent = mins + ':' + secs.toString().padStart(2, '0');
            if (stateEl) {
                stateEl.textContent = d.status || 'recording';
                stateEl.className = 'status-value warn';
            }

            var camStatus = document.getElementById('rec-camera-status');
            if (camStatus) {
                if (d.camera_recording === null || d.camera_recording === undefined) {
                    camStatus.textContent = '';
                } else if (d.camera_recording) {
                    camStatus.textContent = 'Camera: streaming (' + d.camera_frames + ' frames)';
                    camStatus.style.color = '#4ade80';
                } else {
                    camStatus.textContent = 'Camera: NOT STREAMING — stop and check the device';
                    camStatus.style.color = '#f87171';
                }
            }
        } else {
            if (LegacyApp.isRecording) {
                LegacyApp.isRecording = false;
                LegacyApp.loadSessions();
            }
            if (banner) banner.style.display = 'none';
            if (btnStart) {
                btnStart.style.display = 'block';
                btnStart.disabled = false;
            }
            if (btnStop) btnStop.style.display = 'none';
            if (nameInput) nameInput.disabled = false;
            if (stateEl) {
                stateEl.textContent = 'idle';
                stateEl.className = 'status-value ok';
            }
        }
    };

    // --- API calls -----------------------------------------------------

    LegacyApp.checkStatus = function () {
        return fetch('/api/status').then(function (res) { return res.json(); }).then(function (d) {
            var netEl = document.getElementById('s-network');
            if (netEl) {
                netEl.textContent = d.network.ok ? 'OK' : 'Error';
                netEl.className = 'status-value ' + (d.network.ok ? 'ok' : 'err');
            }

            var lidarEl = document.getElementById('s-lidar');
            if (lidarEl) {
                lidarEl.textContent = d.lidar_reachable ? 'Connected' : 'Not reachable';
                lidarEl.className = 'status-value ' + (d.lidar_reachable ? 'ok' : 'warn');
            }

            var camEl = document.getElementById('s-camera');
            if (camEl) {
                if (d.camera) {
                    camEl.textContent = d.camera.ok ? d.camera.message : 'Not connected';
                    camEl.className = 'status-value ' + (d.camera.ok ? 'ok' : 'warn');
                    if (d.camera.ok) LegacyApp.previewErrors = 0;
                }
            }

            var calibEl = document.getElementById('s-calib');
            if (calibEl) {
                var intrOk = d.calibrated && d.calibrated.intrinsics;
                var extOk = d.calibrated && d.calibrated.extrinsics;
                calibEl.textContent = intrOk && extOk ? 'Ready' : intrOk ? 'Intrinsics only' : 'Not calibrated';
                calibEl.className = 'status-value ' + (intrOk && extOk ? 'ok' : 'warn');
            }

            // Hide preview during recording
            var previewImg = document.getElementById('preview-img');
            if (previewImg) {
                if (d.recording) {
                    previewImg.style.display = 'none';
                    previewImg.src = '';
                } else if (!previewImg.src.includes('/api/camera/preview') && LegacyApp.previewErrors < 3) {
                    previewImg.src = '/api/camera/preview';
                }
            }

            var calibCard = document.getElementById('calib-card');
            if (calibCard) calibCard.style.display = (d.camera && d.camera.ok) ? 'block' : 'none';
        }).catch(function () {
            var netEl = document.getElementById('s-network');
            if (netEl) netEl.textContent = 'Fetch error';
        });
    };

    LegacyApp.startRecording = function () {
        var nameInput = document.getElementById('session-name');
        var name = (nameInput && nameInput.value.trim()) || undefined;
        var btnStart = document.getElementById('btn-start');
        if (btnStart) btnStart.disabled = true;

        return fetch('/api/record/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name }),
        }).then(function (res) {
            return res.json().then(function (d) { return { ok: res.ok, d: d }; });
        }).then(function (r) {
            if (!r.ok) {
                alert(r.d.error || 'Failed to start');
                if (btnStart) btnStart.disabled = false;
            }
        }).catch(function (e) {
            alert('Network error: ' + e.message);
            if (btnStart) btnStart.disabled = false;
        });
    };

    LegacyApp.stopRecording = function () {
        var btnStop = document.getElementById('btn-stop');
        if (btnStop) btnStop.disabled = true;

        return fetch('/api/record/stop', { method: 'POST' }).then(function (res) {
            return res.json().then(function (d) { return { ok: res.ok, d: d }; });
        }).then(function (r) {
            if (!r.ok) alert(r.d.error || 'Failed to stop');
            LegacyApp.loadSessions();
            LegacyApp.checkStatus();
        }).catch(function (e) {
            alert('Network error: ' + e.message);
        }).then(function () {
            if (btnStop) btnStop.disabled = false;
        });
    };

    LegacyApp.processSession = function (id) {
        var btn = document.getElementById('btn-process-' + id);
        if (btn) btn.disabled = true;

        // Wipe any cached result so we refetch after the new run finishes.
        delete LegacyApp.resultCache[id];

        return fetch('/api/session/' + id + '/process', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        }).then(function (res) {
            return res.json().then(function (d) { return { ok: res.ok, d: d }; });
        }).then(function (r) {
            if (!r.ok) {
                alert(r.d.error || 'Failed to start processing');
                if (btn) btn.disabled = false;
            }
            LegacyApp.loadSessions();
        }).catch(function (e) {
            alert('Network error: ' + e.message);
            if (btn) btn.disabled = false;
        });
    };

    LegacyApp.deleteSession = function (id) {
        if (!confirm('Delete this recording?')) return Promise.resolve();
        return fetch('/api/session/' + id, { method: 'DELETE' }).then(function () {
            delete LegacyApp.resultCache[id];
            LegacyApp.loadSessions();
        }).catch(function () {
            alert('Failed to delete');
        });
    };

    // ---------- Result envelope rendering ----------

    LegacyApp.fetchResult = function (sessionId) {
        if (LegacyApp.resultCache[sessionId] || LegacyApp.resultInFlight[sessionId]) {
            return Promise.resolve();
        }
        LegacyApp.resultInFlight[sessionId] = true;
        return fetch('/api/session/' + sessionId + '/result').then(function (res) {
            if (!res.ok) {
                console.warn('result fetch failed for ' + sessionId + ': ' + res.status);
                return null;
            }
            return res.json();
        }).then(function (data) {
            if (data && data.result) {
                LegacyApp.resultCache[sessionId] = data.result;
                LegacyApp.loadSessions();
            }
        }).catch(function (e) {
            console.warn('result fetch error for ' + sessionId + ':', e);
        }).then(function () {
            delete LegacyApp.resultInFlight[sessionId];
        });
    };

    LegacyApp.renderFloorplanSvg = function (sessionId, envelope) {
        var fp = envelope.floorplan || {};
        var walls = fp.walls || [];
        var doors = fp.doors || [];
        var windows = fp.windows || [];
        var furniture = envelope.furniture || [];

        if (!walls.length) {
            var empty = document.createElement('div');
            empty.className = 'gallery-empty';
            empty.textContent = 'No walls in result';
            return empty;
        }

        // Compute extents over walls.
        var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
        for (var i = 0; i < walls.length; i++) {
            var w = walls[i];
            var pts = [w.start, w.end];
            for (var j = 0; j < pts.length; j++) {
                var p = pts[j];
                if (p[0] < minX) minX = p[0];
                if (p[0] > maxX) maxX = p[0];
                if (p[1] < minY) minY = p[1];
                if (p[1] > maxY) maxY = p[1];
            }
        }
        var pad = 0.5;
        minX -= pad; minY -= pad; maxX += pad; maxY += pad;
        var ww = maxX - minX;
        var hh = maxY - minY;

        var svgNs = 'http://www.w3.org/2000/svg';
        var svg = document.createElementNS(svgNs, 'svg');
        svg.setAttribute('class', 'floorplan-svg');
        svg.setAttribute('viewBox', minX + ' ' + (-maxY) + ' ' + ww + ' ' + hh);
        svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');

        var flipG = document.createElementNS(svgNs, 'g');
        flipG.setAttribute('transform', 'scale(1,-1)');
        svg.appendChild(flipG);

        var wallById = {};
        for (var k = 0; k < walls.length; k++) wallById[walls[k].id] = walls[k];

        function wallDir(wl) {
            var dx = wl.end[0] - wl.start[0];
            var dy = wl.end[1] - wl.start[1];
            var len = Math.hypot(dx, dy) || 1;
            return { dx: dx / len, dy: dy / len, len: len };
        }

        // Walls.
        for (var w0 = 0; w0 < walls.length; w0++) {
            var wl = walls[w0];
            var line = document.createElementNS(svgNs, 'line');
            line.setAttribute('x1', wl.start[0]);
            line.setAttribute('y1', wl.start[1]);
            line.setAttribute('x2', wl.end[0]);
            line.setAttribute('y2', wl.end[1]);
            line.setAttribute('class', 'wall');
            line.setAttribute('stroke-width', '0.05');
            flipG.appendChild(line);
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
            gap.setAttribute('stroke', '#0f1525');
            gap.setAttribute('stroke-width', '0.07');
            flipG.appendChild(gap);

            var stop = document.createElementNS(svgNs, 'line');
            stop.setAttribute('x1', cx - dirA.dx * halfW);
            stop.setAttribute('y1', cy - dirA.dy * halfW);
            stop.setAttribute('x2', cx - dirA.dx * halfW + (-dirA.dy) * 0.05);
            stop.setAttribute('y2', cy - dirA.dy * halfW + (dirA.dx) * 0.05);
            stop.setAttribute('class', 'door-stop');
            flipG.appendChild(stop);

            var sx = cx - dirA.dx * halfW;
            var sy = cy - dirA.dy * halfW;
            var r = dr.width || 0.8;
            var ex = sx + (-dirA.dy) * r;
            var ey = sy + (dirA.dx) * r;
            var arc = document.createElementNS(svgNs, 'path');
            arc.setAttribute('d', 'M ' + sx + ' ' + sy + ' A ' + r + ' ' + r + ' 0 0 1 ' + ex + ' ' + ey);
            arc.setAttribute('class', 'door-arc');
            flipG.appendChild(arc);
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
            inner.setAttribute('class', 'window');
            inner.setAttribute('stroke-width', '0.12');
            flipG.appendChild(inner);
            var inner2 = document.createElementNS(svgNs, 'line');
            inner2.setAttribute('x1', cxn - dirB.dx * halfWn);
            inner2.setAttribute('y1', cyn - dirB.dy * halfWn);
            inner2.setAttribute('x2', cxn + dirB.dx * halfWn);
            inner2.setAttribute('y2', cyn + dirB.dy * halfWn);
            inner2.setAttribute('stroke', '#4ecca3');
            inner2.setAttribute('stroke-width', '0.04');
            flipG.appendChild(inner2);
        }

        // Furniture.
        for (var f0 = 0; f0 < furniture.length; f0++) {
            (function (f) {
                var cx = f.center[0], cy = f.center[1];
                var w0Sz = (f.size && f.size[0]) || 0.3;
                var d0Sz = (f.size && f.size[1]) || 0.3;
                var yawRad = f.yaw || 0;
                var yawDeg = yawRad * 180 / Math.PI;
                var rect = document.createElementNS(svgNs, 'rect');
                rect.setAttribute('x', -w0Sz / 2);
                rect.setAttribute('y', -d0Sz / 2);
                rect.setAttribute('width', w0Sz);
                rect.setAttribute('height', d0Sz);
                rect.setAttribute('class', 'furn');
                rect.setAttribute('stroke-width', '0.03');
                rect.setAttribute('transform', 'translate(' + cx + ' ' + cy + ') rotate(' + yawDeg + ')');
                rect.addEventListener('click', function () {
                    var targetId = 'bbox-' + f.id + '-' + sessionId;
                    var el = document.getElementById(targetId);
                    if (el) {
                        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        el.classList.add('flash');
                        setTimeout(function () { el.classList.remove('flash'); }, 1200);
                    }
                });
                flipG.appendChild(rect);
            })(furniture[f0]);
        }

        return svg;
    };

    LegacyApp.renderGallery = function (sessionId, envelope) {
        var wrap = document.createElement('div');
        var items = envelope.best_images || [];

        var title = document.createElement('div');
        title.className = 'gallery-title';
        title.textContent = 'Furniture (' + items.length + ')';
        wrap.appendChild(title);

        if (items.length === 0) {
            var empty = document.createElement('div');
            empty.className = 'gallery-empty';
            empty.textContent = 'No objects detected';
            wrap.appendChild(empty);
            return wrap;
        }

        var groups = {};
        items.forEach(function (it, idx) {
            var cls = it.class || 'unknown';
            (groups[cls] = groups[cls] || []).push({ it: it, idx: idx });
        });

        var keys = Object.keys(groups).sort();
        for (var ki = 0; ki < keys.length; ki++) {
            var cls = keys[ki];
            var group = document.createElement('div');
            group.className = 'gallery-class-group';

            var header = document.createElement('div');
            header.className = 'gallery-class-header';
            header.textContent = cls + ' (' + groups[cls].length + ')';
            group.appendChild(header);

            var grid = document.createElement('div');
            grid.className = 'gallery-grid';

            for (var gi = 0; gi < groups[cls].length; gi++) {
                var entry = groups[cls][gi];
                var card = document.createElement('div');
                card.className = 'gallery-card';
                card.id = 'bbox-' + entry.it.bbox_id + '-' + sessionId;

                var img = document.createElement('img');
                img.src = '/api/session/' + sessionId + '/best_view/' + entry.idx + '.jpg';
                img.alt = cls + ' ' + entry.idx;
                img.loading = 'lazy';
                img.onerror = function () { this.style.display = 'none'; };
                card.appendChild(img);

                var label = document.createElement('div');
                label.className = 'label';
                label.textContent = cls;
                card.appendChild(label);

                var dist = document.createElement('div');
                dist.className = 'dist';
                var dm = (entry.it.camera_distance_m != null) ? entry.it.camera_distance_m.toFixed(2) : '?';
                dist.textContent = dm + ' m';
                card.appendChild(dist);

                grid.appendChild(card);
            }

            group.appendChild(grid);
            wrap.appendChild(group);
        }

        return wrap;
    };

    LegacyApp.renderResultBlock = function (sessionId, envelope) {
        var block = document.createElement('div');
        block.className = 'result-block';

        var slamMx = (envelope.metrics && envelope.metrics.slam) || {};
        var totalDur = envelope.metrics && envelope.metrics.total_duration_s;
        var bbox = slamMx.bounding_box_m || [];
        var bboxStr = (bbox.length === 3)
            ? bbox[0].toFixed(1) + ' x ' + bbox[1].toFixed(1) + ' x ' + bbox[2].toFixed(1) + ' m'
            : '';
        var traj = (typeof slamMx.trajectory_length_m === 'number')
            ? slamMx.trajectory_length_m.toFixed(1) : '?';
        var frames = (slamMx.num_frames != null) ? slamMx.num_frames : '?';

        var metricsRow = document.createElement('div');
        metricsRow.className = 'metrics-row';
        metricsRow.innerHTML = ''
            + '<span>' + frames + ' frames</span>'
            + (bboxStr ? ('<span>' + bboxStr + '</span>') : '')
            + '<span>' + traj + ' m walked</span>'
            + (totalDur != null ? ('<span>' + totalDur + ' s total</span>') : '');
        block.appendChild(metricsRow);

        var svg = LegacyApp.renderFloorplanSvg(sessionId, envelope);
        block.appendChild(svg);

        var gallery = LegacyApp.renderGallery(sessionId, envelope);
        block.appendChild(gallery);

        var artRow = document.createElement('div');
        artRow.className = 'artifact-row';
        var artifacts = [
            ['colored_map.ply', 'Open colored map (PLY)'],
            ['scene_with_boxes.ply', 'Open scene+boxes (PLY)'],
            ['result.json', 'Open result.json'],
        ];
        for (var ai = 0; ai < artifacts.length; ai++) {
            var nameLbl = artifacts[ai][0];
            var lblTxt = artifacts[ai][1];
            var a = document.createElement('a');
            a.className = 'artifact-link';
            a.href = '/api/session/' + sessionId + '/artifact/' + nameLbl;
            a.textContent = lblTxt;
            a.target = '_blank';
            a.rel = 'noopener';
            artRow.appendChild(a);
        }
        block.appendChild(artRow);

        return block;
    };

    // ---------- Sessions list ----------

    LegacyApp.loadSessions = function () {
        return fetch('/api/sessions').then(function (res) { return res.json(); }).then(function (sessions) {
            var container = document.getElementById('sessions-list');
            if (!container) return;

            if (!sessions.length) {
                container.innerHTML = '<div class="empty">No recordings yet</div>';
                return;
            }

            container.innerHTML = '';
            sessions.forEach(function (s) {
                var item = document.createElement('div');
                item.className = 'session-item';

                var header = document.createElement('div');
                header.className = 'session-header';
                header.style.gap = '8px';
                var nameSpan = document.createElement('span');
                nameSpan.className = 'session-name-text';
                nameSpan.textContent = s.name;

                if (s.slam_status === 'cancelled') {
                    var badge = document.createElement('span');
                    badge.className = 'badge badge-cancelled';
                    badge.textContent = 'Cancelled';
                    nameSpan.appendChild(badge);
                }

                var btnGroup = document.createElement('div');
                btnGroup.style.display = 'flex';
                btnGroup.style.gap = '6px';
                btnGroup.style.alignItems = 'center';

                var procInfo = LegacyApp.processingStates[s.id];
                var isProcessing = !!procInfo;
                var terminal = ['done', 'error', 'cancelled'];
                var canStart = (s.status === 'stopped' || s.status === 'processed') && !isProcessing && s.bag_size;
                var canReprocess = canStart && terminal.indexOf(s.slam_status) !== -1;
                var canFirstProcess = canStart && !s.slam_status;

                if (canFirstProcess) {
                    var procBtn = document.createElement('button');
                    procBtn.className = 'btn-process';
                    procBtn.id = 'btn-process-' + s.id;
                    procBtn.textContent = 'Process';
                    procBtn.onclick = (function (id) {
                        return function () { LegacyApp.processSession(id); };
                    })(s.id);
                    btnGroup.appendChild(procBtn);
                } else if (canReprocess) {
                    var reBtn = document.createElement('button');
                    reBtn.className = 'btn-process';
                    reBtn.id = 'btn-process-' + s.id;
                    reBtn.textContent = 'Re-process';
                    reBtn.onclick = (function (id) {
                        return function () { LegacyApp.processSession(id); };
                    })(s.id);
                    btnGroup.appendChild(reBtn);
                }

                var delBtn = document.createElement('button');
                delBtn.className = 'btn btn-delete';
                delBtn.textContent = 'Delete';
                delBtn.onclick = (function (id) {
                    return function () { LegacyApp.deleteSession(id); };
                })(s.id);
                btnGroup.appendChild(delBtn);

                header.appendChild(nameSpan);
                header.appendChild(btnGroup);

                var meta = document.createElement('div');
                meta.className = 'session-meta';
                var created = s.created ? new Date(s.created).toLocaleString() : '';
                var dur = s.duration ? s.duration + 's' : '';
                var size = s.bag_size || 'no data';
                var summary = s.result_summary;
                var summaryHtml = '';
                if (summary) {
                    summaryHtml = ' | <span class="ok">'
                        + (summary.num_walls || 0) + ' walls, '
                        + (summary.num_doors || 0) + ' doors, '
                        + (summary.num_windows || 0) + ' windows, '
                        + (summary.num_furniture || 0) + ' objects'
                        + '</span>';
                }
                meta.innerHTML = created + ' | ' + dur + ' | ' + size + ' | <span class="' + (s.status === 'error' ? 'err' : 'ok') + '">' + s.status + '</span>' + summaryHtml;

                var scp = document.createElement('div');
                scp.className = 'session-scp';
                scp.textContent = 'scp -r talal@' + location.hostname + ':' + s.scp_path + ' .';

                item.appendChild(header);
                item.appendChild(meta);
                item.appendChild(scp);

                if (isProcessing) {
                    var prog = document.createElement('div');
                    prog.className = 'slam-progress';
                    prog.id = 'slam-progress-' + s.id;
                    var mins = Math.floor((procInfo.elapsed || 0) / 60);
                    var secs = Math.floor((procInfo.elapsed || 0) % 60);
                    var stage = procInfo.stage || procInfo.progress || procInfo.status || 'working';
                    prog.textContent = 'Processing — ' + stage + ' (' + mins + ':' + secs.toString().padStart(2, '0') + ')';
                    item.appendChild(prog);
                }

                if (s.slam_status === 'error' && s.slam_error) {
                    var errDiv = document.createElement('div');
                    errDiv.className = 'slam-error';
                    errDiv.textContent = 'Processing error: ' + s.slam_error;
                    item.appendChild(errDiv);
                }

                if (s.slam_status === 'done') {
                    var envelope = LegacyApp.resultCache[s.id];
                    if (envelope) {
                        item.appendChild(LegacyApp.renderResultBlock(s.id, envelope));
                    } else {
                        var placeholder = document.createElement('div');
                        placeholder.className = 'slam-progress';
                        placeholder.textContent = 'Loading result...';
                        item.appendChild(placeholder);
                        LegacyApp.fetchResult(s.id);
                    }
                }

                container.appendChild(item);
            });
        }).catch(function (e) {
            console.error('Failed to load sessions', e);
        });
    };

    // --- Calibration ----------------------------------------------------

    LegacyApp.startIntrinsicCalib = function () {
        return fetch('/api/calibration/intrinsics/start', { method: 'POST' }).then(function (res) {
            return res.json().then(function (d) { return { ok: res.ok, d: d }; });
        }).then(function (r) {
            if (!r.ok) { alert(r.d.error || 'Failed'); return; }
            var i = document.getElementById('btn-calib-intr');
            var s = document.getElementById('btn-calib-stop');
            var p = document.getElementById('calib-progress');
            if (i) i.style.display = 'none';
            if (s) s.style.display = 'inline-block';
            if (p) {
                p.style.display = 'block';
                p.textContent = 'Hold checkerboard in front of camera...';
            }
            LegacyApp.pollCalibStatus();
        }).catch(function (e) { alert('Error: ' + e.message); });
    };

    LegacyApp.stopIntrinsicCalib = function () {
        return fetch('/api/calibration/intrinsics/stop', { method: 'POST' }).then(function () {
            var i = document.getElementById('btn-calib-intr');
            var s = document.getElementById('btn-calib-stop');
            var p = document.getElementById('calib-progress');
            if (i) i.style.display = 'inline-block';
            if (s) s.style.display = 'none';
            if (p) p.style.display = 'none';
        });
    };

    LegacyApp.pollCalibStatus = function () {
        return fetch('/api/calibration/intrinsics/status').then(function (res) { return res.json(); }).then(function (d) {
            if (d.running) {
                if (d.frames != null) {
                    var p = document.getElementById('calib-progress');
                    if (p) p.textContent = d.frames + '/' + (d.target || 15) + ' frames captured';
                }
                setTimeout(LegacyApp.pollCalibStatus, 2000);
            } else {
                var i = document.getElementById('btn-calib-intr');
                var s = document.getElementById('btn-calib-stop');
                var p2 = document.getElementById('calib-progress');
                var st = document.getElementById('calib-status');
                if (i) i.style.display = 'inline-block';
                if (s) s.style.display = 'none';
                if (p2) p2.style.display = 'none';
                if (st) {
                    if (d.calibrated) {
                        st.textContent = 'Intrinsics calibrated successfully';
                        st.style.color = '#4ecca3';
                    } else {
                        st.textContent = 'Calibration failed - try again';
                        st.style.color = '#e94560';
                    }
                }
                LegacyApp.checkStatus();
            }
        }).catch(function () { setTimeout(LegacyApp.pollCalibStatus, 3000); });
    };

    LegacyApp.setExtrinsicsManual = function () {
        var input = prompt(
            'Enter camera-to-LiDAR offset as: tx ty tz roll pitch yaw\n' +
            'Translation in meters, rotation in degrees\n' +
            'Example: 0.0 0.05 0.02 0 -90 0');
        if (!input) return Promise.resolve();
        var parts = input.trim().split(/\s+/).map(Number);
        if (parts.length !== 6 || parts.some(isNaN)) {
            alert('Need 6 numbers: tx ty tz roll pitch yaw');
            return Promise.resolve();
        }
        return fetch('/api/calibration/extrinsics', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                translation: { x: parts[0], y: parts[1], z: parts[2] },
                rotation: { x: 0, y: 0, z: 0, w: 1 },
                rpy_degrees: { roll: parts[3], pitch: parts[4], yaw: parts[5] }
            }),
        }).then(function (res) {
            return res.json().then(function (d) { return { ok: res.ok, d: d }; });
        }).then(function (r) {
            var st = document.getElementById('extr-status');
            if (r.ok) {
                if (st) {
                    st.textContent = 'Extrinsics saved';
                    st.style.color = '#4ecca3';
                }
                LegacyApp.checkStatus();
            } else {
                alert(r.d.error || 'Failed to save');
            }
        }).catch(function (e) { alert('Error: ' + e.message); });
    };

    // --- Inline DOM hooks -----------------------------------------------

    // The legacy <img id="preview-img"> used inline onload/onerror that
    // mutated `previewErrors`. Since we moved into a namespace, the SPA
    // shell wires the legacy DOM explicitly via this helper so we don't
    // need to keep window-level globals.
    LegacyApp._wirePreviewHandlers = function () {
        var img = document.getElementById('preview-img');
        if (!img) return;
        img.onerror = function () {
            this.style.display = 'none';
            LegacyApp.previewErrors++;
        };
        img.onload = function () {
            this.style.display = 'block';
            LegacyApp.previewErrors = 0;
        };
    };

    // The legacy buttons used inline onclick="startRecording()" — those
    // calls now resolve at window scope, so we expose backwards-compatible
    // shim aliases. Steps 9-10 move to addEventListener-based wiring.
    LegacyApp._installGlobals = function () {
        var aliased = [
            'connectSSE', 'updateProcessingUI', 'processSession',
            'renderFloorplanSvg', 'renderGallery', 'renderResultBlock',
            'loadSessions', 'checkStatus',
            'startRecording', 'stopRecording',
            'startIntrinsicCalib', 'stopIntrinsicCalib', 'pollCalibStatus',
            'setExtrinsicsManual', 'deleteSession',
        ];
        for (var i = 0; i < aliased.length; i++) {
            var name = aliased[i];
            if (typeof window[name] === 'undefined' && typeof LegacyApp[name] === 'function') {
                window[name] = LegacyApp[name];
            }
        }
    };

    // --- Lifecycle ------------------------------------------------------

    LegacyApp.init = function () {
        if (LegacyApp._initialized) return;
        LegacyApp._initialized = true;
        LegacyApp._wirePreviewHandlers();
        LegacyApp._installGlobals();
        LegacyApp.checkStatus();
        LegacyApp.loadSessions();
        LegacyApp.connectSSE();
        if (!LegacyApp._statusTimer) {
            LegacyApp._statusTimer = setInterval(LegacyApp.checkStatus, 10000);
        }
    };

    window.LegacyApp = LegacyApp;
})();
