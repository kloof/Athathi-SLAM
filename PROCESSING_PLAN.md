# Modal `cloud-slam-icp` Integration Plan

Branch: `feature/processing`
Target API: `https://tiktokredditkw--cloud-slam-icp-web.modal.run`
API key source: `.env` → `MODAL_API`

## Goal

Replace the existing Cloud Run + local floorplan pipeline with the async Modal API. Keep raw `.mcap` recordings in `recordings/`; write all Modal-derived results to a separate `processed/` tree on the flash drive. The user gets live stage updates while a job runs, a rendered floor plan, and a per-object best-view gallery once it finishes.

---

## 1. New filesystem layout

| Path | What |
|------|------|
| `/mnt/slam_data/recordings/<session>/rosbag/` | Raw `.mcap` (unchanged) |
| `/mnt/slam_data/processed/<session>/result.json` | Full Modal result envelope |
| `/mnt/slam_data/processed/<session>/best_views/<idx>_<class>.jpg` | One JPEG per furniture bbox |
| `/mnt/slam_data/processed/<session>/layout_merged.txt` | SpatialLM raw layout (small, cheap to keep) |

- Local fallback when `/mnt/slam_data` is not mounted: `<repo>/processed/`.
- PLY artifacts (`colored_map.ply`, `scene_with_boxes.ply`) are **not** downloaded — surfaced as remote URLs in the UI for click-through download only.
- Old per-session files (`result.ply`, `result_leveled.ply`, `floorplan.png`, `walls_preview.png`, `candidates/`) are left untouched on disk.

---

## 2. Backend changes (`app.py`)

### 2a. Constants & config (top of file)
- **Remove**: `SLAM_API_URL`, `DIRECT_UPLOAD_LIMIT`, `COMPRESS_THRESHOLD`, `gzip` import.
- **Add**:
  ```python
  MODAL_API_URL = 'https://tiktokredditkw--cloud-slam-icp-web.modal.run'
  PROCESSED_DIR = '/mnt/slam_data/processed' if os.path.isdir('/mnt/slam_data') \
                  else os.path.join(SCRIPT_DIR, 'processed')
  POLL_INTERVAL_S = 3
  ```
- **`.env` loader**: tiny inline parser (no extra dep) that reads `MODAL_API` from `.env` at startup. Grammar:
  - Lines starting with `#` are ignored.
  - Blank lines are ignored.
  - `KEY=value` and `KEY="value"` and `KEY='value'` all supported (surrounding quotes stripped).
  - CRLF tolerated (rstrip the line).
  - No shell expansion; no nested quotes.

  Fail-soft at boot if `MODAL_API` is missing — log a clear error but let the app keep running so non-processing features (recording, calibration) still work.

### 2b. New processing pipeline
Replace `_process_thread`, `_upload_direct`, `_upload_via_gcs`, `_floorplan_thread`, `_detect_walls_thread`, `_generate_from_selection_thread`, `_download_ply` with:

- `_zstd_compress(src_mcap, dst_path) -> path` — runs `zstd -1 -q -o <dst> <src>`. Writes the `.zst` next to the source under `recordings/<name>/rosbag/scan.mcap.zst` (same volume as the mcap, so 4 GiB inputs don't blow `/tmp`). Cleaned up in `finally`. At boot, `shutil.which('zstd')` is checked; clear error if missing.
- `_modal_submit(zst_path, idem_key) -> job_id` — POSTs `--data-binary` to `/jobs?filename=scan.mcap.zst` with `X-API-Key` and `X-Idempotency-Key`.
- `_modal_poll(job_id) -> (envelope, retry_after_s)` — GETs `/jobs/{id}` with `X-API-Key`. Returns parsed JSON and the `Retry-After` header value (fallback `POLL_INTERVAL_S`).
- `_modal_cancel(job_id)` — DELETEs `/jobs/{id}` with `X-API-Key`. Best-effort (logged on failure).
- `_modal_fetch(job_id, path, dest)` — GETs an artifact or image to `dest` via `curl`, **always with `X-API-Key`**. `path` is e.g. `image/3` or `artifact/result.json`.
- **All `curl` calls include `-H "X-API-Key: $MODAL_API"` except `/health`.**
- **Synchronization**: every read/write of `_active_processing[sid]` and its sub-fields (`stage`, `status`, `cancel`, `job_id`) happens under `_processing_lock`. Cancellation uses a `threading.Event` stored in the entry rather than a plain bool, so the poller can break out of `time.sleep` promptly via `event.wait(retry_after_s)`.
- **No race on submit**: the POST route inserts the full `_active_processing[sid]` entry under `_processing_lock` *before* spawning the thread (matches existing pattern at app.py:1458). The thread only mutates fields; it never inserts.
- `_process_thread(session_id, mcap_path)`:
  1. Read entry under lock; if `cancel.is_set()` → bail without submitting.
  2. zstd-compress to `recordings/<name>/rosbag/scan.mcap.zst`.
  3. Generate fresh `idem_key = uuid.uuid4()`. Persist on session as `idem_key`. Re-check cancel. Submit; under lock, store `job_id` on both `_active_processing[sid]` and the session record. Save `submitted_at`.
  4. Loop:
     - `event.wait(retry_after_s)`. If event fired → `_modal_cancel(job_id)`, set `slam_status='cancelled'`, break.
     - Poll. On exception, increment failure counter; abort after 5 consecutive. On HTTP 5xx, same.
     - Under lock, mirror `envelope.status` into `_active_processing[sid]['stage']` and `session.slam_status` (so SSE shows `stage_5_infer`).
     - On `done`: `shutil.rmtree(processed/<name>/best_views, ignore_errors=True)` first to avoid stale-file leak on re-process. Download in this order so a disk-full failure leaves the most useful files: `result.json` → `layout_merged.txt` → each `best_views/<idx>.jpg` via `GET /jobs/{id}/image/{idx}` (the API exposes images by index, not URL — file the local copy as `<idx>.jpg`; the UI keys off `best_images[idx]`). Per-image IOError is caught and logged so a partial gallery still ships. Save `slam_status='done'`, `slam_result=<envelope>`. Break.
     - On `failed`: `slam_error = f"{envelope.error.stage}: {envelope.error.stderr_tail}"`, `slam_status='error'`. Break.
  5. `finally`: remove the `.zst`, pop from `_active_processing` under lock, clear `idem_key` on session.

**Mid-job mount loss**: before each write to `processed/<name>/`, check `os.path.ismount('/mnt/slam_data')` if that was the boot-time `PROCESSED_DIR`. On failure, fall back to `<repo>/processed/<name>/` and log a warning rather than aborting the whole run.

### 2c. API surface

| Method+route | Action |
|---|---|
| `POST /api/session/<id>/process` | Inserts `_active_processing[sid]` under lock with a `threading.Event` for cancel, then spawns the thread. Returns `{job_id_pending: true, status:'started'}` (job_id arrives later). |
| `GET  /api/session/<id>/result` | Always returns the same shape: `{status, stage, elapsed, error?, result?}`. `result` (full envelope) is populated only when `slam_status='done'`. |
| `GET  /api/session/<id>/best_view/<idx>.jpg` | Serves the local `processed/<name>/best_views/<idx>.jpg`. |
| `GET  /api/session/<id>/layout.txt` | Serves the local `processed/<name>/layout_merged.txt`. |
| `GET  /api/session/<id>/artifact/<name>` | **Server-side proxy** — streams the Modal artifact through Flask, attaching `X-API-Key`. Browser "Open" links target this Flask route, not Modal directly (Modal would 401 on bare-browser requests). Whitelist `colored_map.ply`, `scene_with_boxes.ply`, `result.json`, `layout_merged.txt`. |
| `DELETE /api/session/<id>` | If actively processing, fire the cancel `Event`. Poll loop wakes, calls Modal `DELETE /jobs/{job_id}`, sets `slam_status='cancelled'`, exits. Route waits up to 5 s for `_active_processing[sid]` to clear, then deletes local files. If `job_id` was never set yet (cancel before submit), the thread sees the event pre-submit and bails without contacting Modal. |

**Removed routes**: `/floorplan`, `/floorplan/detect`, `/floorplan/candidate/<id>.png`, `/floorplan/pick`, `/floorplan.png`, `/download` (PLY).

### 2d. Session list shape (`/api/sessions`)
Adjust the projected fields to:
```
slam_status, slam_stage, slam_error, job_id,
result_summary: { num_walls, num_doors, num_windows, num_furniture, total_duration_s }
```
Drop `floorplan_*` and `floorplan_candidates`. The UI computes everything else from the full envelope returned by `/api/session/<id>/result`.

### 2e. SSE (`/api/events`)
Each `processing[sid]` entry carries: `status` (high-level: `compressing|uploading|polling|done|error|cancelled`), `stage` (verbatim Modal status, e.g. `stage_5_infer`), `progress` (human string — populated as `f"{stage}"` for backward compat with old browser tabs that read `procInfo.progress`), and `elapsed`.

### 2f. Crash recovery (`_recover_stuck_sessions`)
- New stuck-state check works against `slam_status`, NOT `status` (the existing code checks `s['status']` which is for recording state). Recover when `slam_status` ∈ `{compressing, uploading, queued, decoding, cancelled}` OR starts with `stage_`. All become `slam_status='error'`, `slam_error='Interrupted by shutdown'`.
- **Modal-side cleanup on boot**: for each recovered session that has a persisted `job_id`, fire-and-forget `DELETE /jobs/{job_id}` so a long-running container doesn't keep burning credits after an app crash. Errors are logged but do not block boot.
- Drop floorplan recovery branch entirely.

---

## 3. Frontend changes (`templates/index.html`)

### 3a. Strip (enumerated so an implementer can grep-and-delete)

**Markup / templates/index.html lines to remove:**
- Voxel `<select>` (`voxel-${s.id}`) at ~448–452.
- "Download PLY" `<a>` referencing `s.slam_result.download_url` at 463–468.
- "Re-process" path that depends on `download_url` at 470–476 (keep re-process via the new flow).
- SLAM result line consuming `num_points`, `bounding_box`, `slam_time`, `total_time` at 514–520.
- Whole floor-plan section: 532–595 (`fpStatus`, `floorplan_meta`, `floorplan_error`, `/floorplan.png`, retry button).
- Whole candidates grid: 598–632.
- "Generate Floor Plan" button + ortho toggle: 635–655.

**JS functions to remove entirely:**
- `detectWalls(id)` (~372).
- `pickCandidate(sessionId, candidateId)` (~389).
- The branching inside `updateProcessingUI` that splits `info.status === 'floorplan'` vs SLAM (~207–217). Replace with a single stage-based progress line.
- The `voxelSize` plumbing inside `processSession` (~347–354). Body becomes empty `{}`.

**CSS to remove:**
- `.candidate-card`, `.candidate-dims`, `.candidate-detail`, `.candidates-title`, `.candidates-grid`, `.ortho-toggle`, `.btn-floorplan`, `.floorplan-container`, `.floorplan-meta`, `.floorplan-status`, `.voxel-select`.

**Backend fields the UI no longer reads** (and `/api/sessions` no longer projects):
`download_url`, `floorplan_status`, `floorplan_meta`, `floorplan_error`, `floorplan_candidates`, `slam_result.num_points`, `slam_result.bounding_box`, `slam_result.slam_time`, `slam_result.total_time`.

### 3b. Add
- **Stage line** during processing: `"Processing — stage_5_infer (3:42)"`.
- **Floorplan SVG render** (client-side, when `slam_status=done`):
  - Project walls to top-down (drop Z), draw line segments.
  - Doors: gap on the parent wall + arc.
  - Windows: thicker double-line on the parent wall.
  - Furniture: rotated rectangles per `center+size+yaw`, labelled by class. Tap a rect → scrolls to that bbox in the gallery below.
  - Auto-fit viewBox to the wall bounds with padding.
- **Furniture gallery** below the floorplan: thumbnail (`/api/session/<id>/best_view/<idx>.jpg` where `idx` is the array index into `result.best_images`) + class + `camera_distance_m`, grouped by class with a count.
- **Artifacts row**: three small "Open" links pointing at the Flask proxy `/api/session/<id>/artifact/<name>` for `colored_map.ply`, `scene_with_boxes.ply`, `result.json`. (Direct Modal URLs would 401 without the API key.)
- **Cancelled state**: when `slam_status='cancelled'`, render a "Cancelled" badge plus a "Re-process" button (same handler as the normal Process button).

### 3c. Adjust
- `loadSessions()`: when a session has `slam_status='done'`, fetch `/api/session/<id>/result` once and cache; render floorplan + gallery into the card.
- `updateProcessingUI()`: drop the floorplan/SLAM split; show one progress line that reads from `info.stage`.

---

## 4. Cancellation flow
1. User clicks Delete on a session that's mid-job.
2. `DELETE /api/session/<id>`:
   - Under `_processing_lock`: if `_active_processing[sid]` exists, fire its cancel `Event`.
   - The thread can be in three states when cancel fires:
     - **Pre-submit (compressing/about-to-submit)**: thread checks the Event before/after compress and bails without contacting Modal. No `job_id` to cancel.
     - **Post-submit, in `event.wait()`**: wakes immediately, calls `_modal_cancel(job_id)`, sets `slam_status='cancelled'`, exits.
     - **Mid-curl** (poll or fetch): finishes the in-flight HTTP, then sees Event on next iteration.
   - Meanwhile the route waits up to ~5 s for `_active_processing[sid]` to clear, then proceeds to delete local files regardless. (If the poller is truly wedged, local files are still removed; the Modal-side container winds itself down via its own cancel — and the boot-time recovery sweep is a safety net.)
3. Returns `{deleted: id}`.

---

## 5. Edge cases handled
- **Missing `MODAL_API`**: `/api/session/<id>/process` returns 503 with a clear message; recording still works.
- **`zstd` not installed**: at app boot, `shutil.which('zstd')` is checked; if missing, the Process button is disabled in the UI and the route returns 503.
- **`zstd` returncode != 0**: surface `slam_error='compression failed: <stderr tail>'`, abort.
- **Submit 5xx / network error**: 3 retries with exponential backoff (1, 2, 4 s) before giving up; uses the same `X-Idempotency-Key` so we don't double-submit.
- **Poll 5xx / network blip**: log and keep polling; only abort after 5 consecutive failures.
- **`status='failed'`** envelope: `slam_error = f"{error.stage}: {error.stderr_tail}"`.
- **`done` but `best_images=[]`** (empty room): still saves `result.json`; gallery shows "No objects detected".
- **Disk full while saving best views**: catch IOError per-image, continue — the floorplan is still useful. Order of writes (`result.json` → `layout_merged.txt` → images) ensures the most useful files survive a partial run.
- **Re-processing same session**: a fresh `idem_key` is generated on each `/process` invocation (not reused across user-driven re-runs), and stale `processed/<name>/best_views/` is wiped before writing new images so old objects don't leak into the new gallery.
- **App restart mid-job**: `_recover_stuck_sessions` marks the session errored and fires `DELETE /jobs/{job_id}` against Modal so the H100 container is freed.
- **Flash drive unmounts mid-job**: each write to `processed/<name>/` falls back to `<repo>/processed/<name>/` with a logged warning rather than aborting the run.
- **4 GiB mcap**: `.zst` is written next to the source under `recordings/<name>/rosbag/`, not `/tmp` (tmpfs would OOM).
- **`Retry-After` header**: poll cadence reads the header from each `GET /jobs/{id}` response; falls back to `POLL_INTERVAL_S=3` when absent.
- **Two browser tabs hitting `/process`**: route inserts `_active_processing[sid]` under lock before spawning the thread; the second request hits the existing 409.

---

## 6. Out of scope (explicitly leaving alone)
- `floorplan.py` and `level.py` — orphaned but not deleted in this PR. Easy to remove once the UI confirms the Modal output is sufficient.
- The Cloud Run service itself — unchanged; we just stop calling it.
- Old session records on disk — left as-is. Re-processing one writes the new outputs into `processed/<name>/` alongside the stale local files.

---

## 7. Order of work

- [ ] **Step 1** — `.env` loader + new constants in `app.py`.
- [ ] **Step 2** — New `_modal_*` helpers and `_process_thread`.
- [ ] **Step 3** — Wire the new `/process`, `/result`, `/best_view`, `/layout.txt`, `/artifacts`, and DELETE-with-cancel routes; rip out the dead routes/threads.
- [ ] **Step 4** — Update `_recover_stuck_sessions`, `api_sessions`, and `/api/events` payload.
- [ ] **Step 5** — Rewrite the session card in `templates/index.html`: stage line, SVG floorplan render, furniture gallery, artifact links.
- [ ] **Step 6** — Manual smoke test: record a 30 s scan → process → verify stage updates flow → confirm `processed/<name>/` populated → confirm Delete-during-job calls Modal `DELETE`.

---

## Reference: expected result shape

See `/home/talal/Desktop/result.json` for a full sample. Key fields the UI consumes:

- `status` — `done`, `failed`, or one of `queued | decoding | stage_0_slam .. stage_8_best_views`.
- `floorplan.walls[]` — `{id, start:[x,y,z], end:[x,y,z], height, thickness}`.
- `floorplan.doors[]` / `windows[]` — `{id, wall, center, width, height}`.
- `furniture[]` — `{id, class, center:[x,y,z], size:[w,d,h], yaw}`.
- `best_images[]` — `{bbox_id, class, camera_distance_m, pixel_aabb, frame_timestamp_ns, relative_image_path}`. **The actual envelope does NOT include a per-image `url` field** (despite earlier API doc wording). Images are fetched via `GET /jobs/{id}/image/{idx}` where `idx` is the array index. Local storage uses that same index as the filename.
- `artifacts` — **relative paths** in the envelope (e.g. `"artifacts/slam/colored_map.ply"`), not URLs. Downloads go through `GET /jobs/{id}/artifact/{name}` (which requires the API key, hence the Flask proxy).
- `metrics.slam` — useful headline numbers (`num_frames`, `bounding_box_m`, `trajectory_length_m`).
- `metrics.total_duration_s` — total wall time.
