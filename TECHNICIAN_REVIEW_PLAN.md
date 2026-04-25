# Technician Review & Athathi Integration Plan

This is the second major plan on the `feature/processing` branch. The first plan (`PROCESSING_PLAN.md`) replaced the old Cloud Run SLAM with the async Modal `cloud-slam-icp` pipeline — that work is shipped. This plan layers a **technician-facing app** on top of it: Athathi login, scheduled scans, per-project workspaces, **human review of Modal output**, recapture via the Brio camera, bbox merge/edit/delete, and a final Submit step that produces a clean `result_reviewed.json` and notifies Athathi.

Default decisions (confirmed in the brainstorm — change anything later by editing this file):

- **Reviewed JSON destination**: full envelope stays on the Pi forever. Submit fires `POST /api/technician/scans/{scan_id}/complete/` (no body) **plus** an optional outbound POST of `result_for_upload.json` (the filtered subset — see §20) to a configurable endpoint. An optional post-submit shell hook also runs.
- **Class taxonomy**: auto-grow from "every class seen across all stored result.json files" plus free-text input. No dependency on Athathi `/api/categories/` for v1.
- **Per-room review gating**: explicit "Mark reviewed" tap per room. Submit-Project is disabled until every room is marked reviewed.
- **Sub-processing**: every Modal run for a room is versioned at `processed/runs/<run_id>/`. Technicians can re-process with different params, switch the active run, and review state is preserved per run (see §19).
- **Visual product search**: each bbox card has a "Find product" action that calls `/api/visual-search/search-full/` with the bbox's image and lets the technician attach the chosen product to the review (see §20).
- **Upload filter**: a declarative JSON whitelist (`.athathi/upload_filter.json`) controls which fields of the reviewed envelope are transmitted. The full envelope stays local; the filtered subset is what ships (see §21).

---

## -1. Hard constraints — what we MUST NOT break

This is a **UI rewrite + backend addition**. The recording, LiDAR, camera, calibration, and Modal-processing code paths must keep working byte-for-byte. The technician's livelihood depends on these — if the recording flow regresses, the plan has failed regardless of how good the review tool looks.

### -1a. Off-limits (zero changes — read-only references)

| File / area | Why it is load-bearing |
|---|---|
| `record_scan.sh` | Bash entry that wires ROS + the bag recorder + the camera node |
| `camera_node.py` | Custom v4l2 → ROS publisher with constant-fps recovery; replacing usb_cam was non-trivial |
| `camera_config.py` | Brio control settings (exposure, gain, white balance, focus) — must match what calibration assumed |
| `set_brio_fov.py`, `run_calibration.sh` | Operator scripts that currently work |
| `calibrate_camera.py`, `calibrate_extrinsics.py`, `calibration_tool.py` | Intrinsics + extrinsics math + visual tooling |
| `bag_qos_override.yaml` | ROS QoS tuning that solved a real recording bug |
| `setup_eth0.sh` | LiDAR network bring-up |
| `app.py` recording subsystem (lines ≈700–950): `_start_recording_thread`, `_is_recording`, `_kill_process_group`, `_setup_network`, `_wait_for_topic`, `_active_recording` dict, `/api/record/start`, `/api/record/stop`, `/api/camera/preview` | Hardened over many commits against orphan ffmpeg, dying drivers, race conditions on /dev/video0 |
| `app.py` Modal subsystem: `_zstd_compress`, `_modal_submit`, `_modal_poll`, `_modal_cancel`, `_modal_fetch`, `_process_thread`, `_recover_stuck_sessions`, `_active_processing`, `_processing_lock`, the `/api/session/<id>/process|result|best_view|layout|artifact` routes, the SSE `/api/events` | Just-shipped (commit `7bb9d3b`); 33 unit tests passing; one paid E2E proven |
| `floorplan.py`, `level.py` | Orphaned by the Modal swap but kept on disk for reference; do not delete |
| `sessions.json` (legacy) | Existing session records; the new flow reads it for ad-hoc display only |
| `RECORDINGS_DIR` selection logic (mount fallback) | Recording will write here regardless of project — keep as-is |
| `calibration/` directory | Intrinsics + extrinsics YAML files; all new code must read these in-place |

The new code may **wrap** the above (call them, scope their outputs into a project-scoped directory afterward) but never modify their internals or signatures.

### -1b. Allowed-to-touch

- `templates/index.html` — full rewrite OK; this is the bulk of the UI work.
- `app.py` — additive only: new helpers, new routes, new threads. Existing functions get **wrappers**, not edits. Constants at the top may be reorganised. The recording / Modal blocks above are off-limits for in-place edits even when neighbouring code is being changed.
- `static/` — new directory, OK to fill (logo, fonts, CSS if extracted).
- New backend modules (`auth.py`, `athathi_proxy.py`, `review.py`, `runs.py`, `upload_filter.py`) — encouraged. Keeps the diff in `app.py` small and the recording/Modal blocks visually untouched.
- `tests/` — additive only; existing `tests/test_processing.py` must keep passing.
- `config.json`, `upload_filter.json`, `learned_classes.json` — new config files under `/mnt/slam_data/.athathi/`.

### -1c. Regression gates (CI / pre-merge)

Before any technician-UI commit lands:

1. `python3 -m pytest tests/test_processing.py -q` → all 33 tests must pass.
2. A boot smoke: `python3 -c "import app; print('imports ok')"` — must succeed without `MODAL_API` or network.
3. A Flask test_client smoke: GET `/api/sessions`, GET `/api/status`, GET `/api/camera/preview` (image bytes) must all return non-error.
4. The legacy session list must still render (a stored `sessions.json` from before the redesign can still produce a working `/api/sessions` page through the Settings → "Show legacy sessions" toggle).
5. Manual: `record_scan.sh` invoked end-to-end on the Pi must still produce a usable mcap, with no Python traceback in `/tmp/slam_app_debug.log`.

### -1d. Logo

The brand mark is at `static/logo.svg` (copied from `/home/talal/Desktop/logov2.svg`). Used:

- Login screen: centred, ~120 px wide.
- Top bar: 28 px tall, left-aligned. White-fills (CSS `filter: brightness(0) invert(1)`) for the dark `--bg-sidebar` background.
- Favicon and `apple-touch-icon` reference the same file at smaller sizes.

The logo SVG paths use `fill: #231f20` natively; we override with CSS for placement on dark backgrounds.

---

## 0. Terminology (locked)

| Term | Meaning |
|---|---|
| **Project** | One customer assignment, identified by the `scan_id` returned from `/api/technician/scans/schedule/`. The project page shows the customer info from the API. |
| **Scan** | One room recording inside a project. A project has 1+ scans (e.g. `living_room`, `bedroom`). Folder names are technician-chosen, snake_case. |
| **Run** | One Modal processing pass on a scan. A scan has 1+ runs (sub-processing). Each run has its own `result.json` + review state. |
| **bbox_id** | Modal's per-detection identifier inside a run's `result.json` (e.g. `bbox_0`, `bbox_4`). Reviews are keyed on these. May renumber across runs — the cross-run review carry-over uses spatial proximity matching, not strict id equality. |

Everywhere this plan previously said "room", read "scan". Filesystem paths use `scans/`; URLs use `/scan/<scan_name>/`. The `room_name` field name persists in some JSON files only because it's already on disk in legacy data.

---

## 1. Goal

Replace the current single-page recording app with a technician workflow:

```
Login  →  Projects (scheduled scans)  →  Project workspace  →  Per-room
                                                                 │
                                                                 ├─ Record
                                                                 ├─ Process (Modal)
                                                                 └─ Review (HUMAN-IN-LOOP)
                                                                          │
                                                                          ▼
                                                            Submit Project  →  Athathi
```

The technician walks into a customer's space with the Pi + LiDAR + Brio. They log in once. They tap one of their scheduled assignments. Inside the assignment, they record one or more rooms. After Modal returns, they review every detected piece of furniture — confirming the photo, fixing class labels, merging duplicate bboxes, recapturing bad photos with the Brio, deleting spurious detections. When all rooms in the assignment are reviewed, they tap Submit. The reviewed JSON is rendered locally; `/complete/` is called against Athathi; any optional post-submit hook fires.

---

## 2. Filesystem layout

```
/mnt/slam_data/
├── .athathi/                                          # auth + global config
│   ├── token                                          # JWT, chmod 600
│   └── config.json                                    # {api_url, last_user, post_submit_hook?}
│
├── projects/                                          # NEW canonical workspace
│   └── 42/                                            # = scan_id (integer from API; identifies the project)
│       ├── manifest.json                              # see §6 — caches API customer info
│       ├── settings.json                              # per-project stub (empty for now)
│       └── scans/
│           └── living_room/                           # technician-named scan (= one room recording)
│               ├── rosbag/rosbag_0.mcap
│               └── processed/
│                   ├── active_run.json                # {"active_run_id": "..."} — see §19
│                   └── runs/
│                       └── 20260425_142103/           # one Modal run; many runs per scan
│                           ├── result.json            # raw Modal envelope (immutable)
│                           ├── review.json            # technician's workbook for this run
│                           ├── result_reviewed.json   # rendered: result + review
│                           ├── result_for_upload.json # filtered subset; what ships
│                           ├── meta.json              # {job_id, params, started_at, finished_at, status}
│                           ├── layout_merged.txt
│                           ├── scan.mcap.zst          # transient; deleted post-submit
│                           └── best_views/
│                               ├── 0.jpg              # original Modal frame
│                               ├── 0_recapture.jpg    # technician's Brio shot (optional)
│                               └── ...
│
├── recordings/                                        # legacy ad-hoc (read-only after migration)
└── processed/                                         # legacy ad-hoc (read-only after migration)
```

Helpers: `_project_dir(scan_id)`, `_room_dir(scan_id, name)`, `_room_processed_dir(scan_id, name)`. The existing `_processed_dir_for_session` keeps working for legacy sessions; new flow goes through the project-scoped helpers.

Path naming for ad-hoc / non-scheduled work: a virtual project `0` (or `local`) catches scans that aren't on the technician's roster.

---

## 3. Auth + config

### 3a. `.athathi/config.json` schema (single editable file, also exposed in Settings)

```json
{
  "api_url":          "http://116.203.199.113:8002",
  "upload_endpoint":  "http://116.203.199.113:8002",
  "last_user":        "test2",
  "post_submit_hook": null,
  "image_transport":  "multipart",
  "visual_search_cache_ttl_s": 86400
}
```

- `api_url` — base URL the Athathi proxy uses (auth, schedule, complete, visual-search). Trailing slash stripped on save.
- `upload_endpoint` — base URL the multipart `result_for_upload.json + best_view_*.jpg` bundle is POSTed to at submit time. Defaults to `http://116.203.199.113:8002`. The exact path on that host is wired up in §21d once we curl the right route — until then, the bundle is rendered to disk and the post-submit hook can pick it up.
- `last_user` — pre-fills the username on login.
- `post_submit_hook` — optional shell command. Receives the reviewed run directory as `$1` and the scan_id (project) as `$2`. Runs after the `/complete/` call. Failures logged, don't block.
- `image_transport` — `"multipart"` or `"inline"` (base64 in the JSON). Controls how the bundle is built (§21c).
- `visual_search_cache_ttl_s` — how long the local visual-search cache survives (§20g). Set to 0 to disable caching.

**This file is the single source of truth and is editable in two places:**

1. Settings → App tab → individual fields with Save / Test buttons.
2. Direct edit of `/mnt/slam_data/.athathi/config.json` on disk (textfile-friendly, no special tooling). The app reloads it on every Save in Settings AND on every restart.

A defaults file `config.defaults.json` lives in the repo (read-only); on first boot, if the user file doesn't exist, the defaults get copied to `/mnt/slam_data/.athathi/config.json` so the technician always has something to edit.

### 3b. Token lifecycle

- Login form posts `{username, password}` to Athathi `/api/users/login/`. Live probe confirmed (Agent A, §23): the JWT comes back **both** in the response body (`token` field) **and** as `Set-Cookie: user_token=...; HttpOnly; Max-Age=604800; SameSite=Lax; Secure`. The server enforces a **7-day expiry** via the JWT's `exp` claim. We use the body field (simpler than cookie-jar handling) and pass it as `Authorization: Bearer <token>` on every authenticated call.
- The token plus a parsed snapshot of the login response (`{user_id, username, user_type}`) are written to `/mnt/slam_data/.athathi/token` (chmod 600) and `/mnt/slam_data/.athathi/auth.json` respectively. The user info comes from the login body — **there is no `GET /api/users/me/` endpoint** (it's PATCH-only; YAML was misleading).
- Boot path:
  1. Read the token. If absent → show Login.
  2. Decode the JWT locally; check the `exp` claim. If now ≥ exp → drop the token, show Login.
  3. Hit `GET /api/technician/scans/schedule/` with the bearer token as the live validity probe (this is also useful work — we want the schedule for the home screen). On 200 → logged in. On 401 → drop the token, show Login. On network error → show the "no network" banner but stay on the projects screen with cached schedule (recording / processing / review still work offline).
- No proactive refresh. A 401 mid-session bumps the user back to Login.
- Logout: `POST /api/users/logout/`, then unlink `token` and `auth.json`. Local recordings, processed/, projects/, and reviews are preserved (no per-user partitioning, per the user's call).

### 3c. Athathi proxy (Pi-side Flask, server-to-server)

The Pi's frontend never speaks to Athathi directly. Reasons:
- Browser CORS would otherwise force Athathi to whitelist every Pi.
- We can attach `Authorization: Bearer <token>` server-side without exposing it to the page.
- We can add caching, retry, and a "no network" fallback transparently.

| Local route | Forwards to |
|---|---|
| `POST   /api/auth/login` | `POST /api/users/login/` |
| `POST   /api/auth/logout` | `POST /api/users/logout/` |
| `GET    /api/auth/me` | **(local only)** — returns the cached `auth.json` plus a JWT-exp check. No upstream call (no GET /me/ exists on Athathi). |
| `GET    /api/athathi/schedule` | `GET  /api/technician/scans/schedule/` — returns a **bare JSON array**, not `{results:[...]}` |
| `GET    /api/athathi/history` | `GET  /api/technician/scans/history/` — bare array, same shape rules |
| `POST   /api/athathi/scans/<id>/complete` | `POST /api/technician/scans/{scan_id}/complete/` |
| `POST   /api/athathi/scans/<id>/cancel` | `POST /api/technician/scans/{scan_id}/cancel/` |
| `POST   /api/athathi/visual-search/search-full` | `POST /api/visual-search/search-full/` — multipart, field name `file` |
| `GET    /api/athathi/categories` | `GET  /api/categories/` — pre-seed taxonomy ({id, name, description}, ~10 items, has duplicates "Chair" 24 + "Chairs" 26) |

Each proxy route also caches the last successful response in `/mnt/slam_data/.athathi/cache/<route>.json` so the UI stays useful when the Pi is offline.

---

## 4. Backend route surface (full enumeration)

Existing routes from prior work that **stay** (recording / processing / SSE):

```
POST   /api/record/start, /api/record/stop
GET    /api/status, /api/camera/preview, /api/sessions, /api/events
POST   /api/calibration/intrinsics/start|stop, /api/calibration/extrinsics
GET    /api/calibration/intrinsics/status
POST   /api/session/<id>/process                     ← gets a project-scoped sibling
GET    /api/session/<id>/result                      ← retained for legacy sessions
GET    /api/session/<id>/best_view/<idx>.jpg
GET    /api/session/<id>/layout.txt
GET    /api/session/<id>/artifact/<name>
DELETE /api/session/<id>
```

**New routes** layered on top:

| Method | Route | Purpose |
|---|---|---|
| GET    | `/api/projects` | List local projects + Athathi schedule, merged |
| GET    | `/api/project/<scan_id>` | Manifest + room status summary |
| POST   | `/api/project/<scan_id>/sync` | Re-pull manifest fields from `/api/athathi/schedule` |
| POST   | `/api/project/<scan_id>/room` | Body `{name}` — create a new room subfolder |
| DELETE | `/api/project/<scan_id>/room/<name>` | Delete a room (removes rosbag + processed) |
| POST   | `/api/project/<scan_id>/room/<name>/start_recording` | Scoped wrapper around the existing recording flow |
| POST   | `/api/project/<scan_id>/room/<name>/process` | Scoped wrapper around the Modal processing flow |
| GET    | `/api/project/<scan_id>/room/<name>/result` | The full envelope + review state |
| GET    | `/api/project/<scan_id>/room/<name>/review` | review.json + computed counts (kept/deleted/merged) |
| PATCH  | `/api/project/<scan_id>/room/<name>/review` | Partial update (one bbox, or notes) |
| POST   | `/api/project/<scan_id>/room/<name>/review/recapture/<idx>` | Grab one Brio frame, persist to `<idx>_recapture.jpg`, set `image_override` |
| POST   | `/api/project/<scan_id>/room/<name>/review/merge` | Atomic merge of N bboxes into one primary |
| POST   | `/api/project/<scan_id>/room/<name>/review/mark_reviewed` | Sets `review.json.reviewed_at` |
| GET    | `/api/project/<scan_id>/room/<name>/best_view/<idx>.jpg` | Recapture if present, else original |
| GET    | `/api/project/<scan_id>/room/<name>/preview_reviewed.json` | The rendered `result_reviewed.json` for sanity-check |
| POST   | `/api/project/<scan_id>/submit` | Render every room's reviewed JSON, fire `/complete/`, run hook |
| GET    | `/api/categories` | Distinct classes seen across all of this device's stored result.json files (auto-grown taxonomy) |
| GET    | `/api/settings` | Current `config.json` |
| PATCH  | `/api/settings` | Update `api_url` / `post_submit_hook` |

Removed routes after the redesign: the legacy `/api/session/<id>/*` set is preserved for historical sessions but no new sessions go through it.

---

## 5. Frontend information architecture

Single SPA. Three top-level views, swapped via JS (no full page reloads, no flicker on small screen).

```
┌────────── 1. Login ──────────┐
│  Athathi mark                 │
│  Username: ________           │
│  Password: ________           │
│  [ Sign in ]                  │
│  network status banner        │
└───────────────────────────────┘
                ↓
┌──────── 2. Projects ─────────┐
│ ⌂thathi  Technician     [⚙] [⏻]│   ← top bar (--bg-sidebar)
├───────────────────────────────┤
│ MY SCHEDULE                   │
│ • #42  Smith Apartment        │
│   14:00–16:00 today  2 rooms ✓│
│ • #43  Jones Office           │
│   16:00–18:00 today  pending  │
│                               │
│ TODAY: 4 slots, 2 completed   │
│                               │
│ HISTORY (Show 0 →)            │
└───────────────────────────────┘
                ↓
┌────── 3. Project workspace ──┐
│ ←  Project #42  Smith        │  
├───────────────────────────────┤
│ ROOMS                         │
│ • living_room  ✓ reviewed     │  
│ • bedroom      processing     │  
│                               │
│ [+ New Room]                  │
│ [Submit Project] (disabled)   │
└───────────────────────────────┘
                ↓
┌──── 4. Room workspace ───────┐
│ ←  living_room                │
├───────────────────────────────┤
│ Status: ready to review       │
│ [Review] [Re-process] [Delete]│
└───────────────────────────────┘
                ↓
┌─── 5. Review tool ───────────┐
│ ←  living_room  · Review      │
│ tabs:  Floorplan │ Furniture │ Notes │
│  ... (see §7)                 │
│ [Mark reviewed]               │
└───────────────────────────────┘
```

A floating gear opens **6. Settings** as a fullscreen sheet over whatever's underneath — calibration, API URL, account info, future per-project settings.

A persistent **toast / status bar** at the bottom shows: recording timer, processing stage, network state, sync-pending count.

---

## 6. Data model

### 6a. `manifest.json` (per project)

```json
{
  "scan_id": 42,
  "customer_name": "Smith Family Apartment",
  "slot_start": "2026-04-25T14:00:00Z",
  "slot_end":   "2026-04-25T16:00:00Z",
  "address":    "...",
  "athathi_meta": { ... },
  "created_at": "...",
  "completed_at": null,
  "submitted_at": null,
  "post_submit_hook_status": null
}
```

Fields beyond `scan_id`/dates come from whatever shape `/api/athathi/schedule` returns — discovered live, mirrored verbatim into `athathi_meta` so we don't lose anything we don't yet know about. The named top-level fields are best-effort extracted for the UI.

### 6b. `review.json` (per room)

```json
{
  "scan_id": 42,
  "room_name": "living_room",
  "result_job_id": "j_2026-04-25_66a459b9",
  "started_at": "...",
  "reviewed_at": null,
  "submitted_at": null,
  "version": 1,

  "bboxes": {
    "bbox_0": { "status": "kept" },
    "bbox_3": { "status": "deleted", "reason": "duplicate" },
    "bbox_4": {
      "status": "kept",
      "class_override": "armchair",
      "image_override": "best_views/4_recapture.jpg",
      "merged_from": ["bbox_5"]
    },
    "bbox_5": { "status": "merged_into", "target": "bbox_4" }
  },

  "notes": ""
}
```

- A bbox absent from `bboxes` is treated as `{status: "untouched"}` for UI purposes (shown but greyed). Saving the room as "reviewed" requires every bbox to have an explicit status.
- `result_job_id` enables stale detection: when the technician opens the room, we compare it against `result.json.job_id`. Mismatch → modal warning ("results were re-processed; review state may not apply").

### 6c. `result_reviewed.json` (rendered, per room)

Pure function of `result.json + review.json`. Rules:

1. **Drop** `furniture[i]` and `best_images[i]` whose `bbox_id` has `status` ∈ {`deleted`, `merged_into`, `untouched`}.
2. **Apply class override**: replace `furniture[i].class` and `best_images[i].class` with `class_override` when present.
3. **Apply image override**: the on-disk envelope's `best_images[i]` has a remote `url` field (Modal CDN), not a local path. The render function adds a new field `local_path = "best_views/<idx>.jpg"` to each kept entry, replaced by the technician's `image_override` ("best_views/<idx>_recapture.jpg") when present. Original `url` is retained in the local copy for forensics; dropped from the upload subset (see §21a `exclude_paths`). Drop `pixel_aabb` for any image that's been recaptured (no longer meaningful for the new frame).
4. **Merge**: for any kept bbox with `merged_from: [...]`, expand `furniture[i].size` to the AABB union of its members + their `size/2` extents centered at their `center`. `yaw` keeps the **primary's value verbatim** — members' yaws are NOT consulted, even if they differ by >90°. Deterministic by design; may produce visually odd geometry if members strongly disagree, but the technician chose to merge them. `center` recomputed as the centroid of all members' centers.
5. **Add provenance**: top-level `review_meta = { reviewed_at, technician, notes, bbox_count_original, bbox_count_reviewed, merged_count, deleted_count, recaptured_count }`.
6. **Preserve everything else** from `result.json` verbatim (floorplan, walls, doors, windows, metrics, artifacts).

The render function is idempotent and pure. Tested unit-level by feeding hand-built fixtures and comparing the output.

---

## 7. Review tool — deep dive

### 7a. Furniture tab (the main interaction surface)

Vertical scroll list; one card per non-merged-into bbox. ~150 px tall on the 480 px screen → 2.5 cards visible at once, fast scroll.

Card layout:

```
┌────┬──────────────────────────────────────────────────────────┐
│img │  bbox_4   class:[dining_chair ▼]   1.6 m   ☐ select     │
│120 │  Recapture │ Delete │ View on plan                       │
│×120│  ⓘ taken from frame 615 ms after start                   │
└────┴──────────────────────────────────────────────────────────┘
```

- **Image**: tap → fullscreen preview. Long-press → side-by-side "Original | Recapture" toggle (only when override exists).
- **Class dropdown**: built from auto-grown taxonomy; opening the dropdown shows the most-used classes first, then the rest, then a "✏ Type custom" option.
- **Distance**: Modal-reported camera distance.
- **Select**: multi-select checkbox. When 2+ checkboxes are on, a bottom toolbar appears: `Merge (N) │ Delete (N) │ Cancel`.
- **Recapture button**: opens fullscreen camera overlay (see §7c).
- **Delete button**: marks `status: "deleted"`. Card visually struck through; floorplan dims that rect. Tap again to undo.
- **View on plan**: scrolls the Floorplan tab to that rect and pulses it.

Empty state: "No furniture detected — model returned 0 bboxes for this room. [Re-process]". This is also a valid Submit case (a fully empty room).

### 7b. Multi-select & merge

Selection enters by tapping any card's checkbox. While in selection mode, taps elsewhere on cards toggle that card's selection (rather than opening the image), and a bottom toolbar pins to the screen.

**Merge UX** (one-step modal):

```
┌─────────────────────────────────────────────────────────────┐
│ Merge 3 bboxes into one                                     │
│                                                             │
│ Primary (largest) ──────────  bbox_4 dining_chair (1.05 m³) │
│ Members  ──────────────────── bbox_5, bbox_6                │
│                                                             │
│ Class for the merged item:                                  │
│ ( ) dining_chair  (most common, 2/3)                        │
│ ( ) office_chair                                            │
│ ( ) armchair                                                │
│ ( ) ✏ Type custom: __________                               │
│                                                             │
│  [ Cancel ]                       [ Merge ]                 │
└─────────────────────────────────────────────────────────────┘
```

On Merge:
- Backend computes AABB union of all members' (center ± size/2). New `size` = union extents. New `center` = midpoint of union. `yaw` = primary's yaw.
- Primary's `review.bboxes[primary]`: `{ status: kept, class_override: <chosen>, merged_from: [<members>] }`.
- Each member's `review.bboxes[id] = { status: "merged_into", target: <primary> }`.
- Floorplan re-renders with the merged primary visible and a small `↻3` badge in the corner.

Undo: tap the primary's `↻N` badge → "Unmerge?" → splits back into the original individual reviews (members revert to `{status: "kept"}`, primary loses `merged_from` and reverts class).

### 7c. Recapture flow (Brio)

Tap **Recapture** on a card. Frontend:
1. Pause the recording-time camera preview if running (recording is implicit-paused during review anyway).
2. Open a fullscreen overlay containing `<img src="/api/camera/preview">` (the existing MJPEG endpoint) plus two big buttons: **Capture** / **Cancel**. Touch-friendly: 80 px button height, full width.
3. Optional: a thin red rectangle hint overlay showing the original `pixel_aabb` so the technician can roughly frame the same view (only meaningful if they're at the same vantage; usually they walk up to the object).

Backend `POST /api/project/<scan_id>/scan/<name>/review/recapture/<idx>`:
1. **Hard guard against recording**: refuse with 409 if `_is_recording() OR _active_recording['starting']` is true. (`_active_recording` has no single boolean; both fields must be checked.) `camera_node.py` holds `/dev/video0` exclusively while recording is active, so a snapshot ffmpeg call would always fail anyway. Recapture is only allowed post-recording — i.e. during review.
2. Grab one frame from `/dev/video0` at native resolution. Strategy:
   - If the preview ffmpeg is running, send it `SIGUSR1` (or read from its output pipe — easiest is to just snapshot one JPEG from the MJPEG byte stream we already serve).
   - Otherwise: spawn `ffmpeg -hide_banner -loglevel error -f v4l2 -video_size 1280x720 -i /dev/video0 -frames:v 1 -q:v 2 -y <dst>` (settings derived from `camera_config.py`).
3. Save to `<room>/processed/best_views/<idx>_recapture.jpg`.
4. PATCH `review.json.bboxes[bbox_id].image_override` to the new path.
5. Return `{ok: true, path: "best_views/<idx>_recapture.jpg", size_bytes: …}`.

Resolution choice: native Brio (4K) is overkill for a single human-review snapshot. Default to 1920×1080 to match the Athathi Dashboard's image columns. Quality `-q:v 2` (≈ 90% JPEG).

Failure cases:
- `/dev/video0` busy: return 503 "camera in use"; show a toast "wait for current preview to settle".
- `/dev/video0` missing: return 503 "no camera detected".
- Disk full: return 507 "no space".

### 7d. Class taxonomy (auto-grown)

The `GET /api/categories` endpoint walks every `result.json` under `/mnt/slam_data/projects/*/scans/*/processed/runs/*/result.json` plus the legacy `/mnt/slam_data/processed/*/result.json` and counts class occurrences. Pre-seeded with the 10 entries from `/api/athathi/categories` on first boot (Agent A confirmed live: ids 4-26, classes Sofa/Bed/Wardrobe/Cabinet/Table/Drawer/Chair/Carpet/Chairs/Uncategorized — note "Chair" id 24 and "Chairs" id 26 are both present in the upstream taxonomy; we do NOT silently dedupe). Returns:

```json
{
  "classes": [
    {"name": "dining_chair", "count": 86, "source": "model"},
    {"name": "armchair",     "count":  4, "source": "technician"},
    ...
  ]
}
```

`source: "technician"` flags classes the technician introduced via free-text — so the back-office can see new label suggestions over time. The frontend dropdown shows the top 12 by count first, then the rest, then a "Type custom" affordance that adds the new class to the local list and persists it (a single line in `.athathi/learned_classes.json` so it survives a wipe of all result.json files).

### 7e. Floorplan tab

Same SVG render from the prior plan (PROCESSING_PLAN §3b), with review-state overlays:

- Kept bboxes: solid `--accent` stroke, `--accent` fill at 15% alpha.
- Deleted bboxes: `--text-on-dark` stroke, `2px` dashed pattern, no fill, struck-through label.
- Merged primary: solid stroke; small `↻N` badge near top-right of the rect.
- Selected (multi-select): heavy 0.08 m stroke in `--accent`, full-opacity fill at 30% alpha.
- Tap a rect → scrolls Furniture tab to that bbox card and highlights for 1 s.

Coordinate system is consistent with the prior render: top-down, `transform="scale(1, -1)"` so +Y is up.

### 7f. Notes tab

A `<textarea>` saved to `review.notes` on blur. No autocomplete, no formatting. ~10 lines visible; scrolls within the tab.

---

## 8. Submit pipeline

The technician taps **Submit Project** on the project workspace. Conditions (button is disabled unless all hold):

1. Every room in the project has `review.reviewed_at` set.
2. Every room has a successful `result.json` with `status="done"`.
3. Project's `manifest.submitted_at` is null.
4. Network is up (cached schedule may be stale; `/complete/` requires live network).

On tap:

1. Confirm modal: "Submit project #42 — N rooms. Once submitted, the assignment is marked complete in Athathi. Continue?"
2. For each room: render `result_reviewed.json` from `result.json + review.json`. Atomic temp-write + rename.
3. `POST /api/athathi/scans/{scan_id}/complete` (the local proxy → the Athathi `/complete/` endpoint). On 5xx: 3 retries with 1/2/4 s backoff. On final failure: keep the rendered files, mark `manifest.submit_pending: true`, show a "Pending sync" pill on the project card; the next manual Submit retry or boot-time retry sweep will re-attempt.
4. If `config.post_submit_hook` is set: spawn it as `<hook> <project_dir> <scan_id>`. Capture stdout/stderr to `manifest.post_submit_hook_log`. Failures are logged but don't block.
5. Stamp `manifest.submitted_at = now`. Stamp every room's `review.json.submitted_at = now`.
6. Toast: "Project #42 submitted ✓". Navigate back to the projects list with #42 visually moved to History.

**Idempotency**: even though `/complete/` is path-only POST, double-submit can come from a network retry. The local `manifest.submitted_at` is checked first; once set, Submit is a no-op (just re-runs the hook if it failed before).

**Network-down at submit**: render reviewed JSONs locally regardless; mark `submit_pending: true`; project card shows a `↻ Sync pending` pill. A periodic background task (every 60 s while the projects view is open) retries the `/complete/` call and clears the flag on success.

---

## 9. Login + projects screen specifics

### 9a. Login

```
┌─────────────────────────────────────────────────┐
│              ⌂THATHI  TECHNICIAN                 │
│                                                  │
│   Sign in to continue                            │
│                                                  │
│   USERNAME  ___________________________          │
│   PASSWORD  ___________________________          │
│                                                  │
│   [        Sign in        ]                      │
│                                                  │
│   ⚠ no network                                   │  
└─────────────────────────────────────────────────┘
```

- Pre-fills username from `config.last_user`.
- `Enter` in either field submits.
- On error (401): "Wrong username or password" inline under the form. Shake animation.
- On network error: "Cannot reach Athathi server. Check the API URL in Settings (gear icon below)."
- A small gear icon at the bottom-right of the login screen takes the technician to Settings (so they can fix the API URL even when not logged in).

### 9b. Projects list

`GET /api/projects` returns:

```json
{
  "now": "2026-04-25T13:00:00Z",
  "scheduled": [
    { "scan_id": 42, "customer_name": "...", "slot_start": "...", "slot_end": "...",
      "rooms_local": 2, "rooms_reviewed": 1, "submitted": false },
    ...
  ],
  "history": [
    { "scan_id": 38, "customer_name": "...", "completed_at": "..." }
  ],
  "ad_hoc": [
    { "name": "scan_20260423_230731", "rooms_local": 1, "submitted": false }
  ]
}
```

Three sections, each collapsible:

1. **My schedule** — assigned scans not yet submitted. Default-expanded.
2. **Recent history** — last 10 submitted scans (collapsible, default-collapsed; tap "Show ▾").
3. **Ad-hoc** — local recordings not tied to an Athathi scan. Useful during development; in production this section is hidden unless the user has `--allow-ad-hoc` flag set.

Pull-to-refresh re-runs the schedule fetch. A small "Last updated 2 min ago" line at the bottom of the My-schedule section.

Tap a project card → enters Project workspace.

---

## 10. Project workspace

The project workspace shows **all customer info pulled from the API** at the top, then the list of scans (room recordings) under it. Tapping a scan opens its workspace.

```
┌──────────────────────────────────────────────────┐
│ ←  Project #42                            [⚙] [⏻] │
├──────────────────────────────────────────────────┤
│ Customer       Smith Family Apartment             │
│ Slot           14:00–16:00 today                  │
│ Address        1234 Lexington Ave, …              │
│ Phone / notes  +1 555 …  / VIP                    │
│ (every key the schedule API returned, rendered    │
│  generically so we don't lose unknown fields)     │
│                                                   │
│ SCANS (2)                                         │
│ • living_room   ✓ reviewed                        │
│ • bedroom       processing… stage_5_infer 4:21    │
│                                                   │
│ [ + New Scan ]                                    │
│                                                   │
│ ─────────────────────────────────────────────    │
│ [    Submit Project    ]    (disabled if any     │
│                              scan not reviewed)   │
└──────────────────────────────────────────────────┘
```

The customer block is rendered in two layers:
- **Known fields** (Customer, Slot, Address, Phone, Notes) get pretty labels with fallback "—" if missing.
- **Unknown fields** from `manifest.athathi_meta` are appended as a key-value list so a new column added on the Athathi side shows up on the Pi without a code change.

Per-scan status pill values: `idle`, `recording 0:32`, `compressing`, `uploading`, `stage_5_infer 4:21`, `done — review pending`, `done — reviewing N/M`, `done — reviewed`, `error: …`, `submit_pending`.

Tapping a scan pill enters the Scan workspace.

`+ New Scan` opens a modal: text input for the scan name (lowercase, snake_case; default suggestion: `scan_<n>` where n is the next index, technician usually changes to a room name like `living_room`). On confirm, the scan subfolder is created and the technician lands on the Scan workspace ready to record.

---

## 11. Room workspace

```
┌──────────────────────────────────────────────────┐
│ ←  living_room                              [⚙]  │
├──────────────────────────────────────────────────┤
│ status   done — reviewed                         │
│ frames   391  ·  trajectory  19.8 m              │
│ furniture 12 (10 kept · 2 merged)                │
│                                                  │
│ [   View / Edit Review   ]                       │
│ [   Re-record   ]   [   Re-process   ]            │
│ [   Delete room   ]                              │
└──────────────────────────────────────────────────┘
```

States and which buttons are enabled:

| State | Primary action shown |
|---|---|
| `idle` (no rosbag yet) | `Start recording` |
| `recording` | `Stop recording` |
| `recorded` (rosbag, no Modal output) | `Process` |
| `processing` | (read-only progress; `Cancel` available — fires Modal DELETE) |
| `done`, no review | `Review` (auto-opens Review tool) |
| `done`, review in progress | `Continue review` |
| `done — reviewed` | `View review` |
| `error` | `Retry` |

`Re-process` is destructive — wipes review.json + result.json + best_views, prompts before doing it.
`Re-record` wipes the rosbag, prompts.
`Delete room` removes the entire room subfolder, prompts.

---

## 12. Settings

A modal sheet, full screen, slides up from the bottom. Tabs at the top: **Device · Account · App**.

### 12a. Device tab

- Network: `192.168.1.2 / eth0` ok / err
- LiDAR: connected / not reachable (live)
- Camera: streaming / not detected (live)
- Calibration:
  - Intrinsics: ✓ / not calibrated
  - Extrinsics: ✓ / not set
  - Buttons: `Calibrate Camera`, `Set Extrinsics (manual)` — same flows as today
- Storage: `/mnt/slam_data` total / free
- Brio resolution dropdown (1280×720 / 1920×1080) for recapture quality
- Keep-alive toggle for the camera preview during review (off by default to free GPU; on for debugging)

### 12b. Account tab

- Logged in as: `test2 (Technician)`
- Athathi API URL: `<input type=url>` with **Save** and **Test** buttons (Test pings `/health` or `/api/users/me/`).
- Logout: prompts confirmation; clears token + cache. Local data preserved.

### 12c. App tab

- Version: branch + commit
- Submit hook command: text input. Saved to `config.json`.
- Show ad-hoc / legacy sessions: toggle.
- Telemetry: log path (`/tmp/slam_app_debug.log`), download log button.

---

## 13. Visual / style spec

### Color tokens

```css
:root {
  --bg-app:        #FFFFFF;
  --bg-surface:    #F8FAFC;   /* cards on white */
  --bg-sidebar:    #0F172A;   /* top bar; we don't use a sidebar on 640×480 */
  --accent:        #22C7A9;   /* teal — buttons, active state, links, stats */
  --accent-soft:   #CCF2EA;   /* tonal background for selected items */
  --text-primary:  #0F172A;   /* headings, big numbers */
  --text-body:     #475569;   /* body, sub-labels */
  --text-on-dark:  #94A3B8;   /* muted text on the dark top bar */
  --border:        #E2E8F0;   /* dividers, dashed empty states */
  --warn:          #F59E0B;   /* network slow, sync pending */
  --danger:        #DC2626;   /* errors, no network, delete buttons */
  --success:       #16A34A;   /* "done", "reviewed" */
}
```

### Typography

- Sans: `-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif`.
- Base: 14 px (1 rem). Headings 1.4× / 1.2×.
- Tabular numerals (`font-variant-numeric: tabular-nums`) for timers, counts, distances.
- Letter-spacing: 0.04 em on uppercase eyebrow labels (e.g. `MY SCHEDULE`, `ROOMS`).

### Layout rules for 640×480 landscape

- Viewport: `<meta name="viewport" content="width=640, initial-scale=1, user-scalable=no">`.
- Top bar fixed, 48 px tall, `--bg-sidebar` background.
- Status bar fixed bottom, 32 px tall, white; only shows when there's something to say.
- Content area scrolls vertically. Horizontal scroll forbidden everywhere.
- All primary buttons ≥ 44 px tall; 56 px when standalone (Sign in, Submit Project).
- All form inputs ≥ 44 px tall, 16 px font (avoids iOS auto-zoom; same defensive default for the Pi browser).
- All interactive surfaces have `cursor: pointer` and `:active` opacity 0.7.
- No hover-only affordances. Long-press detection (~600 ms) for advanced actions.
- Focus rings on tab navigation: 2 px `--accent` outline.

### Iconography

- Inline SVG only (no font icons). Stroke-based 24 px viewBox icons. Colors: `currentColor`.
- A small icon set: gear, log-out, refresh, back chevron, plus, trash, camera, undo, check, warning. About 10 icons.

### Empty / error states

Always with a one-line description and (when applicable) a single clear action. Pattern:

```
[icon]
"No scans scheduled."
"Pull down to refresh, or wait for a new assignment."
[ Refresh ]
```

---

## 14. Edge cases (catalogued)

| Case | Behavior |
|---|---|
| `/mnt/slam_data` not mounted at boot | Settings shows red banner; recording disabled; processing fall-back already in place (writes to repo `processed/`); login still works |
| Token file present but corrupted | Treat as missing; show login |
| `config.json` missing | Generate from defaults; `api_url=http://116.203.199.113:8002`, no hook |
| `api_url` unreachable | "No network" pill; cached schedule shown; recording/processing/review still work; submit blocked |
| Login network OK but 401 | Inline error; clear field; do not log out an existing valid session |
| Modal returns non-zero `error.stage` | Room status `error: <stage>: <stderr_tail>`; Review button hidden; Retry shown |
| Re-process while review.json exists | Prompt: "Re-process will discard your review of this room. Continue?" — only on confirm |
| New Modal job for same room (job_id changes) | Compare bbox_ids; if exact match, keep review; else show stale warning |
| Two browser tabs open the same review | Last-write-wins on `review.json` PATCH; both tabs poll every 2 s for outside changes |
| Recapture while recording is in progress | 409 with toast "stop recording first" |
| Submit while one room is still processing | Submit disabled; tooltip: "bedroom is still processing" |
| Submit network down | Render reviewed JSONs; mark `submit_pending`; queue retry; pill shown |
| Hook command times out (60 s default) | Hook killed; status `hook_timeout`; submit otherwise considered successful (since `/complete/` already returned 200) |
| Hook command exits non-zero | Status `hook_failed`; submit considered successful but with a warning toast |
| Disk full mid-recapture | 507 toast; review.json untouched |
| Brio unplugged mid-recapture | 503 toast |
| Empty room (model returned 0 furniture) | Review screen shows "No furniture detected"; `Mark reviewed` button still works |
| All bboxes deleted | Allowed; `result_reviewed.json` has empty `furniture[]` |
| All bboxes merged into one | Allowed; one furniture entry remains |
| Class override is empty string | Reject; UI keeps the previous override or original |
| Logout while a job is processing | Prompt: "you have 1 job processing — abort and logout?". On confirm: cancel Modal job (DELETE), wipe token, return to login |
| Pi reboot during recording | Existing `_recover_stuck_sessions` already handles |
| Pi reboot during review | review.json is on disk; resume on next open |
| Pi reboot during submit | If `/complete/` was already called: manifest stays as is. Otherwise: `submit_pending` flag is on; retry sweep on next boot |

---

## 15. Migration / backward compatibility

- Legacy `recordings/<name>` and `processed/<name>` paths keep working through the existing `/api/session/<id>/*` routes. The Settings → "Show ad-hoc / legacy sessions" toggle controls visibility.
- Old `sessions.json` is not deleted; the projects page reads it for ad-hoc rendering only.
- One-shot migration script `scripts/migrate_legacy_to_projects.py` (out of scope for v1; provided as a stub) will move legacy sessions into a synthesized "ad-hoc" project with `scan_id=0` once we're confident.
- The new `_process_thread` reuses the existing Modal helpers verbatim. Only the destination paths change.

---

## 16. Implementation order (subagent assignments)

| Step | Owner | Description | Blocked by |
|---|---|---|---|
| 1 | backend | `.athathi/config.json` loader + writer. Token read/write. Boot validation. | — |
| 2 | backend | Athathi proxy routes (login/logout/me/schedule/history/complete/cancel) with `Authorization: Bearer`. Cache layer. | 1 |
| 3 | backend | New project + room helpers (`_project_dir`, `_room_dir`, manifest read/write). `GET /api/projects`. | 1 |
| 4 | backend | New scoped recording + processing routes; refactor `_process_thread` to take a project+room target. Keep legacy routes intact. | 3 |
| 5 | backend | review.json schema, render function, all `/review/*` routes (incl. recapture endpoint). Brio snapshot logic. | 4 |
| 6 | backend | Submit pipeline (render reviewed JSONs, `/complete/` retry, hook execution, `submit_pending` retry sweep, recovery). | 5 |
| 7 | backend | `GET /api/categories` (auto-grown taxonomy). | 5 |
| 8 | frontend | Color tokens, typography, layout primitives. Login screen. | 1, 2 |
| 9 | frontend | Top bar with `[⚙] [⏻]`, Projects list, Project workspace, Room workspace. | 3, 4 |
| 10 | frontend | **Review tool** (Furniture tab, Floorplan tab, Notes tab, multi-select, merge modal, recapture overlay, class dropdown). | 5, 7, 9 |
| 11 | frontend | Settings sheet (Device / Account / App). | 8, 9 |
| 12 | frontend | Submit modal + sync-pending toast. | 6, 9 |
| 13 | tests | Unit tests: render-reviewed, merge math, .env / config parsers, taxonomy aggregator, submit retry. | per step |
| 14 | tests | E2E: login → schedule fetch → record (ad-hoc) → process → review (delete one, merge two, recapture one, override class) → submit → assert reviewed JSON contents + that `/complete/` was called. Fully mocked Athathi + mocked Modal for CI. One real-Modal smoke variant gated by env flag (already established pattern). | 12 |
| 15 | tests | Visual regression: render the projects screen, project workspace, review tool with a fixture envelope; compare DOM snapshots for the four key states (idle, processing, ready-to-review, reviewed). | 9, 10 |
| 16 | review | Independent code review pass after each backend chunk and after the review tool is wired. | per step |

Each subagent is briefed with this file plus the latest contract for the routes it owns. No subagent runs paid Modal tests without an explicit gate (`E2E_REAL=1`).

---

## 17. Out of scope (explicitly)

- Multi-technician on the same Pi: per the user's call, **no per-user data partitioning**. All technicians on the device share `projects/`, `recordings/`, `processed/`. Logout/login swaps the JWT but local data persists and is visible to whoever logs in next. A small banner on login warns "Logged in as <new_user>; previous local work by <last_user> is still on this device."
- Offline-first edits to scheduled scans (we'll only refresh; no mutating offline ops other than recording/review).
- Pushing reviewed JSON to a server endpoint is not implemented — only the optional shell hook fires post-submit. Wire a real upload endpoint in a follow-up.
- USDZ export / SpatialLM re-run / 3D model regeneration. Those are server-side concerns.
- Multi-room SLAM stitching (each room is its own Modal job).
- Editing wall geometry. Walls are read-only in v1; only furniture is editable.
- Editing the floorplan (room shape, doors, windows). Read-only in v1.
- Per-bbox 6-DoF pose editing (yaw nudge, position fine-tune). v1 supports only delete / merge / class / image. A future v2 may add a "Adjust pose" handle on the floorplan rect.
- Account creation. Technician accounts are provisioned by Athathi.
- Push notifications when a new schedule item arrives. Pull-to-refresh only in v1.

---

## 18. Reference: the existing pieces we're building on top of

- Modal pipeline + processing routes: `PROCESSING_PLAN.md` + `app.py` (lines ~1300–2100).
- Existing recording flow: `app.py` (lines ~700–950).
- Existing camera preview / Brio plumbing: `camera_node.py`, `camera_config.py`.
- Existing calibration: `calibrate_camera.py`, `calibrate_extrinsics.py`, `calibration_tool.py`.
- Existing template (will be rewritten): `templates/index.html`.
- Color/style reference image: `https://media.discordapp.net/attachments/916378354700144700/1497528726496215161/image.png` (Athathi Technician Dashboard mock — desktop, will collapse to 640×480 landscape).
- Sample reviewed-able envelope: `/home/talal/Desktop/result_LIVE_scan_20260423_230731.json` (from the live Modal job `j_2026-04-25_66a459b9`, 28 furniture items, 4 walls, 0 windows — perfect dev fixture).

---

## 19. Sub-processing per scan (versioned Modal runs)

A "scan" in this app is a single recording for one room. The technician can process the same recording multiple times — for instance, after recapturing photos, after tweaking parameters, or after a Modal upgrade. Each run is its own folder; old runs aren't destroyed.

### 19a. Filesystem

```
projects/42/rooms/living_room/
├── rosbag/rosbag_0.mcap
└── processed/
    ├── active_run.json                  # {"active_run_id": "20260425_142103"}
    └── runs/
        ├── 20260425_142103/             # ISO-ish timestamp
        │   ├── result.json
        │   ├── review.json
        │   ├── result_reviewed.json
        │   ├── result_for_upload.json   # rendered at submit time
        │   ├── layout_merged.txt
        │   ├── best_views/
        │   ├── meta.json                # {job_id, params, started_at, finished_at, duration_s, status}
        │   └── scan.mcap.zst            # transient
        └── 20260425_153842/
            └── ...
```

Only the **active** run is what the room workspace shows by default. Older runs are listed in a "Run history" expander and can be made active again with one tap.

### 19b. Run lifecycle

- **Start a new run**: `POST /api/project/<id>/room/<name>/process` with optional body `{params: {voxel_size?: 0.01, ...}}`. Always creates a brand-new `runs/<run_id>/` directory and makes it active.
- **Run identifiers**: `<UTC YYYYMMDD>_<HHMMSS>` of the launch moment. Collisions resolved with a `_2`/`_3` suffix.
- **Active-run pointer**: `active_run.json` (single-key file). Atomic temp+rename writes.
- **Switch active run**: `POST /api/project/<id>/room/<name>/active_run` body `{run_id: "..."}`. The room workspace immediately reflects the switch; review state is per-run so nothing is lost.
- **Delete a run**: `DELETE /api/project/<id>/room/<name>/runs/<run_id>`. Refuses to delete the active run unless `force=true` is set; refuses if `submitted_at` is non-null in that run's review.json (audit trail preserved).
- **Re-process from review** (preserve edits): a "Re-process keeping review" button copies the active run's review.json into the new run, then maps each old bbox to a new bbox by **spatial proximity** — for each new bbox, find the old one whose `center` is within 0.3 m AND has the same `class` (or class_override) and same furniture footprint scale within 25%. If a single old bbox matches a single new bbox, carry the review (class_override / image_override / linked_product / merged_from / status) verbatim. If multiple olds match one new, the closest wins **with deterministic alphabetical tie-break on `bbox_id` if two are equidistant** (e.g. `bbox_0` beats `bbox_1`); the losers are recorded in `merge_carryover[]` for the technician to confirm. Unmatched new bboxes start as `untouched`. Unmatched old bboxes are surfaced in a "Carry-over warnings" pane on the run header so the technician can decide whether to manually re-link. **Bbox_ids regenerate every Modal run** (confirmed by user) — never trust id equality, only spatial proximity.

### 19c. Run params

The Modal `cloud-slam-icp` API itself takes a single body (the mcap) plus a filename query. Per-run params live on the Pi — they tune the *Pi-side* preprocessing and the optional re-runs we may want for things like:

- `voxel_size` — passed to a future Modal-side knob if/when it's exposed (placeholder for now).
- `compress_level` — `zstd -1` is default; technician can crank to `-3` to save bandwidth at the cost of CPU time.
- `note` — free-text reason for this run ("post-recapture", "different angle").

Stored in `runs/<id>/meta.json` so we can later inspect why a run was kicked off.

### 19d. UI

- Room workspace gets a small "**Run #3**" pill at the top with a dropdown showing all runs (active highlighted, others tappable). The active run's status drives the room workspace's primary action button.
- Review tool's tab bar shows "Review · run #3" so the technician always knows which version they're editing.
- A `[ Re-process keeping review ]` and `[ Re-process from scratch ]` pair on the Room workspace.

### 19e. Routes

| Method | Route | Purpose |
|---|---|---|
| `GET`    | `/api/project/<id>/room/<name>/runs` | List all runs with status, durations, job_ids |
| `POST`   | `/api/project/<id>/room/<name>/process` | Body `{params?, copy_review_from?}` — start new run, become active |
| `POST`   | `/api/project/<id>/room/<name>/active_run` | Body `{run_id}` — switch the active run pointer |
| `DELETE` | `/api/project/<id>/room/<name>/runs/<run_id>` | Delete a non-active, non-submitted run |

### 19f. Edge cases

- Cancel during a sub-run: `_active_processing[sid].cancel.set()` + Modal DELETE; the new run's directory is left with a `meta.json` of status `"cancelled"`. The previous active run remains the active one.
- Two browser tabs both kick off a re-process: the second one returns 409 (the scan is already processing). Only one Modal job at a time per scan.
- Deleting the only run: refused; would orphan the scan. The technician must `Delete scan` instead.
- **Run retention**: nothing is auto-deleted. Every run stays on disk until the technician manually deletes it (and the active + any submitted run are always protected). Per the user's call: "keep everything for now."
- **bbox_ids regenerate every run**: confirmed with the user — the model produces a fresh numbering each pass. The spatial-proximity carry-over (above) is therefore the only viable strategy; strict id equality is never relied upon.

---

## 20. Visual product search (Athathi `/api/visual-search/search-full/`)

A bbox card may include a per-detection product link. The technician taps **Find product** on a card; the Pi sends that bbox's image to `POST /api/visual-search/search-full/`; results come back as a ranked candidate list; the technician picks one (or "no match"); the choice is recorded in the review.

### 20a. Endpoint contract (Athathi)

```
POST /api/visual-search/search-full/
Content-Type: multipart/form-data
Body: file=<jpeg bytes>
Auth: Authorization: Bearer <token>
Returns: { results: [...], total_time }
```

**Locked from live probe (Agent A, §23)**. Top-level: `{results: [...], total_time: float}`. Each `results[]` item has these exact 12 keys:

| Key | Type | Notes |
|---|---|---|
| `id` | int | Athathi product PK — only stable handle (no `sku`) |
| `name` | str | Product name |
| `price` | str (decimal) | Cast to float on the Pi if doing math |
| `thumbnail_url` | str | Object-storage URL — same domain as `model_url` |
| `model_url` | str | `.glb` 3D asset URL |
| `model_usdz` | str | `.usdz` 3D asset URL (iOS AR) |
| `width`, `height`, `depth` | str (decimal cm) | All strings — Django DecimalField serialization |
| `category` | str | Free-form string, matches `/api/categories/` `name` field |
| `store_name` | str | Vendor display name |
| `similarity` | float | Cosine similarity 0..1, results sorted descending |

Server returns ~6 results (no `top_k` parameter exposed). No pagination. Typical latency 2 s.

### 20b. Pi-side proxy

| Method | Route | Purpose |
|---|---|---|
| `POST` | `/api/athathi/visual-search/search-full` | Multipart proxy; attaches token; returns the upstream JSON verbatim |

Image source: by default we send the bbox's **active best_view** (recapture if present, else the original Modal frame). A "Use full room photo" alternative button on the same card will send the full-room photo (a tagged frame that's the closest in time to the bbox — fall back to any best_view in the room if needed).

### 20c. Review state extension

Add to `review.json.bboxes[id]` (using the actual upstream field names):

```json
{
  "status": "kept",
  "linked_product": {
    "id": 574,
    "name": "Pearla 3 Seater Sofa",
    "price": "279.00",
    "thumbnail_url": "https://hel1.your-objectstorage.com/athathi/4a56b3a3-...",
    "model_url":     "https://hel1.your-objectstorage.com/athathi/3d_models/b16395dc-....glb",
    "model_usdz":    "https://hel1.your-objectstorage.com/athathi/3d_models/b16395dc-....usdz",
    "width":  "250.00",
    "height": "85.00",
    "depth":  "103.00",
    "category": "Sofa",
    "store_name": "Abyat",
    "similarity": 0.7078,
    "linked_at": "2026-04-25T15:02:11Z",
    "linked_by": "technician",
    "_raw": { ... }    // full upstream candidate verbatim
  }
}
```

If the technician picks "no match", we store `{"linked_product": null, "search_attempted": true, "search_attempted_at": "..."}` so we don't pester them on every load.

### 20d. UI flow

1. Technician taps **Find product** on a bbox card.
2. Modal opens with a header "Searching for: <class>" and a spinner.
3. Pi posts the image to the proxy. Spinner runs while we wait (typical: 1-2 s).
4. Results modal renders a 2-column grid (≤4 items visible without scroll on 480 px tall):
   ```
   ┌──────────────────────────────────────────────┐
   │ Match suggestions for bbox_4                 │
   ├──────────────────────────────────────────────┤
   │ [thumb]  Eames Lounge Chair                  │
   │          SKU: EM-LC-BLK-01     score 0.91    │
   │ ─────────────────────────────────────────    │
   │ [thumb]  Mid-Century Lounge Chair            │
   │          SKU: MC-LCB-02        score 0.87    │
   │ ─────────────────────────────────────────    │
   │ ...                                          │
   │                                              │
   │ [ No match ]  [ Use full room photo ]        │
   └──────────────────────────────────────────────┘
   ```
5. Tap a row → `linked_product` is set; modal closes; the bbox card now shows the linked product as a small pill below the class. Tap the pill → re-opens the search modal so the technician can change the choice.

### 20e. When the search button is hidden

- No best_view image present yet (Modal hasn't run).
- Network down (button disabled with tooltip "no network").
- Bbox is `deleted` or `merged_into` (no point linking those).

### 20f. Re-search on recapture

If the technician recaptures a bbox's image AFTER linking a product, the link stays but a small badge **"image changed since last search"** appears on the linked-product pill, prompting them to re-search.

### 20g. Caching

The Pi keeps `/mnt/slam_data/.athathi/cache/visual_search/<sha1-of-image>.json` for 24 h. Sending the same image twice (e.g. accidentally double-tapping) is a free hit. The cache is small JSON, not the image itself. Disabled in Settings → App if storage is a concern.

### 20h. Edge cases

- Athathi returns `[]` (no matches): show "No matches above threshold" with a "Search anyway with lower threshold" link → a follow-up call to `/api/visual-search/search/` (or just text-search via `/text/`) — we'll wire only `search-full` in v1, leaving the others as future fallbacks.
- Image upload fails (5xx, network): retry once; show toast "search failed — try again".
- Search returns a product that's since been deleted on Athathi: the cached snapshot still works for review purposes; on submit, the back-office will reject and we'll surface that as a sync warning.
- Multiple bboxes get linked to the same product: allowed (e.g. matching dining chairs). Each bbox keeps its own `linked_product` block.

---

## 21. Upload filter (what ships vs what stays local)

The full reviewed envelope is **rich** — Modal stats, internal job_id, artifact URLs, raw frames, pixel_aabb, voxel counts, etc. The Athathi back-office only needs a curated subset. The filter is **declarative** so we can tune the contract without code changes.

### 21a. Filter spec — `.athathi/upload_filter.json`

```json
{
  "version": 1,
  "include_paths": [
    "schema_version",
    "scan_id",
    "room_name",
    "submitted_at",
    "submitted_by",

    "floorplan.walls[*].id",
    "floorplan.walls[*].start",
    "floorplan.walls[*].end",
    "floorplan.walls[*].height",
    "floorplan.walls[*].thickness",

    "floorplan.doors[*].id",
    "floorplan.doors[*].wall",
    "floorplan.doors[*].center",
    "floorplan.doors[*].width",
    "floorplan.doors[*].height",

    "floorplan.windows[*]",

    "furniture[*].id",
    "furniture[*].class",
    "furniture[*].center",
    "furniture[*].size",
    "furniture[*].yaw",
    "furniture[*].linked_product.id",
    "furniture[*].linked_product.name",
    "furniture[*].linked_product.price",
    "furniture[*].linked_product.thumbnail_url",
    "furniture[*].linked_product.model_url",
    "furniture[*].linked_product.model_usdz",
    "furniture[*].linked_product.width",
    "furniture[*].linked_product.height",
    "furniture[*].linked_product.depth",
    "furniture[*].linked_product.category",
    "furniture[*].linked_product.store_name",
    "furniture[*].linked_product.similarity",

    "best_images[*].bbox_id",
    "best_images[*].class",
    "best_images[*].camera_distance_m",
    "best_images[*].local_path",
    "best_images[*].image_source",

    "review_meta.bbox_count_original",
    "review_meta.bbox_count_reviewed",
    "review_meta.merged_count",
    "review_meta.deleted_count",
    "review_meta.recaptured_count",
    "review_meta.notes"
  ],
  "exclude_paths": [
    "metrics",
    "artifacts",
    "best_images[*].pixel_aabb",
    "best_images[*].frame_timestamp_ns",
    "best_images[*].url",
    "furniture[*].linked_product._raw"
  ]
}
```

Semantics:

- `include_paths` is the ALLOW-list. Anything not matched by an include path is dropped.
- `exclude_paths` is applied AFTER include — used to prune sub-fields that an include like `furniture[*]` would otherwise let through.
- Path syntax: dot for nesting; `[*]` for "every array element"; trailing `*` not supported (paths must be specific).
- The filter is loaded from `.athathi/upload_filter.json`. If the file doesn't exist, a sane default (the JSON above) is generated on first run.
- The filter is also exposed in Settings → App so a technician with admin rights can tweak it on the device. Changes are versioned (`version` bumps) and the UI warns if a previously submitted run was filtered with an older version.

### 21b. Render pipeline

```
result.json + review.json
        │
        ▼
 result_reviewed.json    (full local copy, lossless; merges/deletes/overrides applied)
        │
        ▼
 upload_filter.json
        │
        ▼
 result_for_upload.json  (filtered subset — what we transmit)
```

Both files are written to `runs/<run_id>/` so we always know exactly what was sent on a given submit.

### 21c. Bundling images

For each bbox kept after review, the bbox's image (recapture if present, else original) is included in the upload bundle. Two transmission strategies:

- **Inline base64** in `result_for_upload.json` (simplest; works for ≤30 small JPEGs at ~50 KB each → ≈ 1.5 MB JSON; that's fine over local Wi-Fi but starts to hurt over LTE).
- **Multipart**: send `result_for_upload.json` as one form field plus each `best_view_<idx>.jpg` as a separate form field. Server reassembles. Smaller payload, but requires a server endpoint that supports it.

v1 ships with **multipart** (assumed to be the desired path; configurable in `upload_filter.json` via `"image_transport": "multipart" | "inline"`).

### 21d. Outbound endpoint

`config.json` adds `upload_endpoint: null` by default. When set, Submit POSTs the bundle there as multipart:

```
POST <upload_endpoint>
Content-Type: multipart/form-data
Body:
  envelope    = <bytes of result_for_upload.json>
  image_<idx> = <bytes of best_view JPEG>
  ...
Auth: Authorization: Bearer <token>
```

If the endpoint is null, the bundle is rendered to disk only; the post-submit shell hook can pick it up. This keeps us forward-compatible: when Athathi adds a real upload endpoint, we just set the URL in Settings.

### 21e. Editability later

The filter being a JSON file means:

- A field that the back-office decides it doesn't want any more → remove its line from `include_paths` and re-deploy the file. No Pi-app code change.
- A field added later (e.g. per-bbox confidence score) → add it to include_paths, no code change.
- A schema-level migration → bump `version` in the filter; the renderer stamps the version into `result_for_upload.json.schema_version`, and the back-office can dispatch by that.

### 21f. Local "everything" copy stays forever

`result_reviewed.json` is the lossless local copy. It's never trimmed, never overwritten by the filter step. If we ever need to re-render `result_for_upload.json` with a new filter version (e.g. to re-submit with extra fields), we do it from `result_reviewed.json`. No re-processing of the raw mcap is needed.

A small `[Re-render upload bundle]` button under the Run history surface lets the technician (or a support engineer) regenerate `result_for_upload.json` for an old, already-submitted run — useful when the back-office contract changes and old data needs reshipping.

### 21g. Edge cases

- **Filter file corrupted**: fall back to the embedded default; show a warning banner in Settings → App ("upload_filter.json is invalid; using defaults").
- **Path doesn't match anything**: silently dropped. No-op. (Logged at debug level so we can spot stale entries.)
- **Excluded sub-field of an excluded parent**: redundant but harmless.
- **Recapture image missing on disk** (e.g. wiped): the include for that path silently drops it; review.json still has the override pointer for forensic purposes.
- **Multipart upload partial failure**: the request is treated as atomic; on 5xx, retry per the existing submit retry policy; on 4xx, surface the error and keep `submit_pending`.

---

## 22b. SSE contract additions

Existing `/api/events` SSE entries under `processing[sid]` carry `{status, stage, progress, elapsed, job_id}`. The technician UI needs two more fields per entry — these are additive, no break to existing tabs:

- **`run_id`** (str) — the active sub-run id from §19 (e.g. `"20260425_142103"`). Lets the UI render "Run #3" without an extra fetch.
- **`error`** (str|null) — populated when `status="error"`; otherwise null. The technician sees the failure inline without round-tripping `/result`.

Both are populated by `_set_active_stage` / `_set_session_slam_status` under `_processing_lock`, which is already the existing discipline. No race risk.

## 22c. Submit-button gating table

Submit is a single button, but four conditions can disable it. When multiple apply, show the highest-priority message:

| Priority | Condition | Disabled-state message |
|---|---|---|
| 1 | `manifest.submitted_at` already set | `Already submitted on <date>` |
| 2 | Any scan has `slam_status` ∈ {`compressing`, `uploading`, `queued`, `decoding`, or `stage_*`} | `<scan_name> is still processing` |
| 3 | Any scan has `slam_status` = `error` | `<scan_name> failed — re-process or delete` |
| 4 | Any scan has `review.reviewed_at` = null | `<scan_name> not reviewed yet` |
| 5 | No network (last `/api/auth/me` probe failed) | `No network — Submit requires Athathi connection` |

Otherwise enabled. The disabled tooltip is on the Submit button; tapping a scan row jumps to the offending scan.

## 22d. New helpers introduced (additive, do NOT replace existing helpers)

- `_project_dir(scan_id)` → `<PROJECTS_ROOT>/<scan_id>/`
- `_scan_dir(scan_id, name)` → `<PROJECTS_ROOT>/<scan_id>/scans/<name>/`
- `_scan_processed_root(scan_id, name)` → `<scan_dir>/processed/`
- `_processed_dir_for_run(scan_id, name, run_id)` → `<scan_dir>/processed/runs/<run_id>/`
- `_active_run_id(scan_id, name)` → reads `active_run.json`
- `_set_active_run(scan_id, name, run_id)` → atomic temp+rename write
- `_render_reviewed_envelope(result_path, review_path) -> dict`
- `_apply_upload_filter(envelope, filter) -> dict`

The existing `_processed_dir_for_session(name)` is preserved verbatim for legacy ad-hoc sessions. Tests in `tests/test_processing.py` continue to mock the legacy helper; new helpers get their own test file `tests/test_review.py`.

---

## 22. Updated implementation order (delta from §16)

Insert these steps in §16:

- After step 5 (review schema): **5b — Sub-processing (run versioning, active-run pointer, run history endpoints).**
- After step 6 (submit pipeline): **6b — Upload filter renderer + multipart bundler + outbound POST + re-render-from-reviewed action.**
- After step 7 (auto-grown taxonomy): **7b — Visual search proxy + bbox-card "Find product" button + results modal + linked_product persistence.**

The end of §16 step 14 (E2E test) is updated to also exercise: pick a product via visual search, switch active runs, submit, assert that `result_for_upload.json` is filtered correctly and the multipart payload was POSTed to the configured endpoint (mocked in CI).

---



- **Why a JSON workbook (`review.json`) and a separate rendered file?**
  Audit trail. Modal's `result.json` is the model's truth; the technician's edits are layered on top, preserved separately so we can always show "what was added, what was removed". Reproducibility: re-rendering from inputs is deterministic.

- **Why per-room review state but per-project submit?**
  The Athathi assignment (`scan_id`) is the unit of work. A customer pays for one project; many rooms make up that project. We don't want a half-reviewed assignment to land in the back-office.

- **Why a shell hook instead of building the upload now?**
  We don't have an upload endpoint documented. A hook lets ops wire any one-line script (curl, scp, gcloud cp) without us having to ship a code change.

- **Why auto-grow the taxonomy instead of a fixed list?**
  The Athathi taxonomy isn't documented in this YAML. Auto-grow defers the dependency without locking the technician out. We can swap in `/api/categories/` later.

- **Why no per-bbox geometry editing?**
  A 640×480 touchscreen with a fingertip can't hit a 1 cm handle on a 4 m wall. Building a usable pose-editing UI is a v2 job. v1 keeps the floor at delete/merge/class/image — high-leverage edits that don't need fine motor control.

---

## 23. Validation passes — what we learned (Apr 25, 2026)

Four agents ran in parallel before any implementation began. Every claim in the plan has been cross-checked against the live API, the existing codebase, and the on-disk envelope from a real Modal run. Findings folded into the plan above. Raw notes preserved here for traceability.

### 23a. Live API probe (Athathi `http://116.203.199.113:8002`)

Probed: `/api/users/login/`, `/api/users/me/`, `/api/technician/scans/schedule/`, `/api/technician/scans/history/`, `/api/visual-search/search-full/`, `/api/categories/`, `/api/visual-search/health/`. All probes used `test2:talal2003` (technician role). One real visual-search POST against `processed/scan_20260423_230731/best_views/0.jpg` (71 KB sofa thumb) confirmed end-to-end.

Discovered:
- **JWT comes back in body AND `Set-Cookie: user_token` (HttpOnly, 7-day Max-Age=604800).** Plan now uses the body token; cookie is ignored.
- **`GET /api/users/me/` returns 405** (PATCH-only). Plan now uses local JWT-exp + schedule call as the validity probe.
- **`schedule` and `history` are bare arrays**, not `{results:[...]}`. Plan adjusted.
- **schedule was empty for test2** — per-item shape unobservable. Plan stays defensive: render unknown `manifest.athathi_meta` keys generically.
- **`linked_product` schema locked to 12 fields**: `id, name, price, thumbnail_url, model_url, model_usdz, width, height, depth, category, store_name, similarity`. No `sku`. All decimals are strings.
- **Visual search has no `top_k`** parameter; server returns ~6. Latency ~2 s.
- **`/api/categories/`** returns `{categories: [{id, name, description}]}` — 10 items, includes both "Chair" (id 24) and "Chairs" (id 26). Pre-seeded into the auto-grown taxonomy.

### 23b. Codebase load-bearing audit

Confirmed §-1 with file:line precision. Highlights — full report archived in agent transcript:

- **Recording subsystem** (load-bearing): `app.py:183-242` (`_record_lock`, `_active_recording`, `_is_recording`), `app.py:566-588` (`_start_bag_record`), `app.py:606-646` (camera monitor), `app.py:789-826` (`POST /api/record/start`), `app.py:975-1087` (`POST /api/record/stop`), `app.py:1089-1160` (`/api/camera/preview`), `app.py:2036-2044` (`_cleanup_on_exit`).
- **Modal subsystem** (load-bearing): `app.py:206-208` (`_processing_lock`, `_active_processing`), `app.py:1452-1467` (`_zstd_compress`), `app.py:1470-1602` (Modal helpers), `app.py:1607-1745` (`_process_thread`), `app.py:1828-1945` (process/result routes), `app.py:1974-2029` (artifact proxy with `X-API-Key`), `app.py:1305-1394` (DELETE + SSE), `app.py:2047-2087` (boot recovery).
- **15 existing routes inventoried.** New technician routes must NOT use `/api/session/` (legacy scope) — use `/api/projects/`, `/api/athathi/`, `/api/auth/`.
- **19-item invariants checklist** captured in agent output. Notable: `_record_lock` is RLock not Lock; bag SIGTERM grace = 30 s, camera = 10 s; cancel is `threading.Event` not bool; idempotency key fresh per `/process`; best_views wiped before re-process; zst always cleaned in `finally`; `Retry-After` header respected.
- `floorplan.py` (2611 lines) and `level.py` (267 lines) confirmed orphaned but **DO NOT DELETE** without explicit user approval.

### 23c. Plan correctness cross-check

- BLOCKER (resolved): `GET /api/users/me/` doesn't exist — plan §3b updated.
- BLOCKER (resolved): recapture endpoint must guard against `_active_recording['starting']` plus `_is_recording()` — plan §7c updated.
- BLOCKER (resolved): on-disk envelope's `best_images[].url` is a remote CDN URL, not a local path. Render function now adds a `local_path` field; original `url` retained locally, dropped from upload — plan §6c, §21a updated.
- IMPORTANT (resolved): glob paths in §7d and §19 said `rooms/`; corrected to `scans/`.
- IMPORTANT (resolved): `_processed_dir_for_session` left untouched for legacy; new `_processed_dir_for_run(scan_id, name, run_id)` introduced — plan §22d.
- NIT (resolved): merge yaw is primary's verbatim, members ignored — plan §6c updated.
- NIT (resolved): spatial-proximity tie-break uses alphabetical `bbox_id` — plan §19b updated.
- NIT (resolved): submit-button gating table with priorities — plan §22c.

### 23d. UI / asset readiness

- **Logo SVG** is monochrome `#231f20`, viewBox 206.88 × 47.66 (4.34:1). Filter `brightness(0) invert(1)` works for the dark top bar.
- **Existing JS to wrap in `LegacyApp` namespace** rather than rewriting: `connectSSE`, `updateProcessingUI`, `processSession`, `renderFloorplanSvg`, `renderGallery`, `renderResultBlock`, `loadSessions`, `checkStatus`, `startRecording`/`stopRecording`, calibration trio, `setExtrinsicsManual`.
- **DOM IDs to keep stable** across view swaps: `#btn-start`, `#btn-stop`, `#session-name`, `#rec-timer`, `#recording-banner`, `#preview-img`, `#s-network|lidar|camera|calib|state`, `#calib-progress`, `#calib-status`, `#extr-status`, `#sessions-list`.
- **First commit** of the rewrite: extract the 850-line `<style>` block into `/static/app.css` and lock viewport: `<meta name="viewport" content="width=640, height=480, user-scalable=no">`.
- **SSE contract gap** (resolved): `processing[sid]` entries needed `run_id` and `error` for the new sub-run UI — plan §22b adds them.
- **Touch ergonomics**: existing template passes ≥44 px tap-target rule. Watch new icons (Find product / Merge / Recapture / Delete) — must be ≥44 px.
- Flask serves `/static/` by default; no config change needed.

---

End of plan. Implementation starts with step 1 in §16, with the deltas in §22a–§22d folded in.
