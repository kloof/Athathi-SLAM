# Athathi Scan Importer — Server-Side Contract

Reference for the back-office service that receives reviewed scans from the Pi technician device.

This document describes:

1. **Wire format** — what arrives at the upload endpoint.
2. **Envelope schema** — every field the Pi sends, types, units, optionality.
3. **Image bundle** — how per-bbox JPEGs are paired with furniture entries.
4. **Edge cases** — empty rooms, deleted-everything, recaptures, missing products.
5. **Reference Django importer** — copy-paste-able starting point.
6. **Versioning + forward-compat** — how the Pi tells the server what schema it spoke.

The wire is **multipart/form-data**. The Pi's submit pipeline (`submit.py` + `app.py` Step 6 banner block) handles all the rendering before POST; on the server side you receive a clean, filtered payload that's already been:

- Stripped of Modal-internal fields (job_ids, internal URLs, raw upstream blobs).
- Walked through the technician's review (deleted bboxes dropped, merged bboxes consolidated, class overrides applied, recaptured images swapped in).
- Authenticated — every request carries `Authorization: Bearer <jwt>` from the technician's login.

---

## 1. Wire format

```
POST <upload_endpoint>
Content-Type: multipart/form-data
Authorization: Bearer <jwt>      (Athathi's user JWT — same one /api/users/login/ returns)

Form fields:
  envelope        (1×, application/json)  — result_for_upload.json bytes
  image_<bbox_id> (N×, image/jpeg)         — one JPEG per kept bbox
                                            e.g. image_bbox_4=@4_recapture.jpg
                                                 image_bbox_7=@7.jpg
```

- `envelope` is **always present**, exactly one file.
- `image_<bbox_id>` is one form field per kept furniture entry. The field name is literally `image_` followed by the `id` from `furniture[].id` (e.g. `image_bbox_0`, `image_bbox_27`).
- Image count equals `len(envelope.furniture)` for non-empty rooms, or zero for an "empty room" scan.
- Total payload size: typically 1–5 MB (envelope ~10–30 KB JSON + 10–30 JPEGs at 50–150 KB each).

The Pi retries 3 times on 5xx (1/2/4 s exponential backoff) before queuing for offline retry. **Your endpoint is expected to be idempotent on `(scan_id, run_id)`** — see §6.

---

## 2. Envelope schema (`result_for_upload.json`)

A minimised representative example (real envelope from a reviewed scan):

```json
{
  "submitted_at": "2026-04-25T20:42:11Z",

  "floorplan": {
    "walls":   [{"id": "wall_0", "start": [x,y,z], "end": [x,y,z],
                 "height": 2.86, "thickness": 0.0}, ...],
    "doors":   [{"id": "door_0", "wall": "wall_0", "center": [x,y,z],
                 "width": 1.06, "height": 2.54}, ...],
    "windows": [...]
  },

  "furniture": [
    {
      "id": "bbox_4",
      "class": "armchair",
      "center": [4.00, -2.73, 0.83],
      "size":   [1.05, 1.42, 1.40],
      "yaw":    -1.5708,
      "linked_product": {              // optional — only when technician picked a match
        "id": 574,
        "name": "Pearla 3 Seater Sofa",
        "price": "279.00",
        "thumbnail_url": "https://...",
        "model_url":     "https://...glb",
        "model_usdz":    "https://...usdz",
        "width":  "250.00",
        "height": "85.00",
        "depth":  "103.00",
        "category": "Sofa",
        "store_name": "Abyat",
        "similarity": 0.7078
      }
    },
    ...
  ],

  "best_images": [
    {
      "bbox_id": "bbox_4",
      "class":   "armchair",
      "camera_distance_m": 2.34,
      "local_path": "best_views/4_recapture.jpg",
      "image_source": "recapture"
    },
    ...
  ],

  "review_meta": {
    "bbox_count_original": 28,
    "bbox_count_reviewed": 12,
    "merged_count":  1,
    "deleted_count": 2,
    "recaptured_count": 3,
    "notes": "TV cabinet trim was off — cropped manually."
  }
}
```

### 2a. Top-level keys

| Path | Type | Required | Notes |
|---|---|---|---|
| `submitted_at` | ISO 8601 UTC | yes | When the technician tapped Submit. Source of truth for "when this scan was finalised." |
| `floorplan` | dict | yes | Room geometry (walls + openings). |
| `furniture` | list | yes (may be empty) | Final detected items after review. |
| `best_images` | list | yes (may be empty) | One per furniture entry, by `bbox_id`. Same length as `furniture[]`. |
| `review_meta` | dict | yes | Audit + telemetry. |

### 2b. `floorplan.walls[]`

| Field | Type | Units | Notes |
|---|---|---|---|
| `id` | str | — | Stable within a single envelope (`wall_0`, `wall_1`, …). |
| `start` | `[float, float, float]` | metres | World-frame XYZ. Z is the floor height. |
| `end` | `[float, float, float]` | metres | World-frame XYZ. Same Z as `start`. |
| `height` | float | metres | Wall height (floor → ceiling). |
| `thickness` | float | metres | Often `0.0` (thin-wall model). Treat as informational. |

Walls are line segments in the XY plane. To draw the floor outline, sort/walk wall endpoints; closed loops form rooms (the model emits non-loop walls in the wild — your importer must tolerate them).

### 2c. `floorplan.doors[]` and `floorplan.windows[]`

| Field | Type | Units | Notes |
|---|---|---|---|
| `id` | str | — | `door_0`, `window_0`, … |
| `wall` | str | — | References `walls[].id` (the wall the opening sits in). |
| `center` | `[float, float, float]` | metres | World-frame midpoint of the opening. |
| `width` | float | metres | Along the wall direction. |
| `height` | float | metres | Floor-to-top of the opening. |

`windows` shares the exact same schema. The list is empty when no windows were detected — common in interior rooms.

### 2d. `furniture[]`

| Field | Type | Units | Notes |
|---|---|---|---|
| `id` | str | — | `bbox_<n>`. **This is the key for matching against `image_<id>` form field and against `best_images[].bbox_id`.** |
| `class` | str | — | Free-form lower-case-with-underscores label. May be a Modal-output class (`sofa`, `dining_chair`, `floor-standing_lamp`, …) OR a technician override (anything they typed in). Don't expect a closed enum. |
| `center` | `[float, float, float]` | metres | AABB centre, world-frame. |
| `size` | `[float, float, float]` | metres | Extents `[width X, depth Y, height Z]`. |
| `yaw` | float | **radians** | Rotation around the Z (vertical) axis. |
| `linked_product` | dict or absent | — | Set when the technician matched the bbox to an Athathi product via visual search. |

### 2e. `furniture[].linked_product` (when present)

The 12-key schema mirrors `/api/visual-search/search-full/` results:

| Field | Type | Notes |
|---|---|---|
| `id` | int | **Athathi product PK — primary handle.** Only stable identifier; product names + URLs may change over time. |
| `name` | str | Product display name |
| `price` | str | Decimal as string (Django `DecimalField` serialization). Cast to `Decimal` server-side; don't parse as float. |
| `thumbnail_url` | str | Absolute URL to product thumbnail (object storage). |
| `model_url` | str | Absolute URL to `.glb` 3D asset. |
| `model_usdz` | str | Absolute URL to `.usdz` (iOS AR). |
| `width` / `height` / `depth` | str | Decimals as strings, **centimetres**. |
| `category` | str | Free-form (matches Athathi `/api/categories/`). |
| `store_name` | str | Vendor display name. |
| `similarity` | float | Cosine similarity [0, 1] of the visual-search match. Higher = closer. |

The product `id` is the only field your importer should use for joins back into the Athathi product table. Everything else is a snapshot at the moment of linking — the upstream catalogue may have changed since.

### 2f. `best_images[]`

| Field | Type | Notes |
|---|---|---|
| `bbox_id` | str | Matches a `furniture[].id`. |
| `class` | str | Mirror of `furniture[].class` for convenience. |
| `camera_distance_m` | float | Distance from the camera to the bbox centre at capture time. |
| `local_path` | str | Hint about the original Pi-side path (e.g. `best_views/4_recapture.jpg`). **Use the multipart `image_<bbox_id>` field for the actual JPEG bytes — `local_path` is informational only.** |
| `image_source` | `"original"` \| `"recapture"` | `"original"` = Modal-selected best frame from the recording. `"recapture"` = technician took a fresh photo with the Brio post-recording. |

`best_images[]` length matches `furniture[]` length **exactly**. The order does NOT necessarily match — pair by `bbox_id`, not by index.

### 2g. `review_meta`

Audit + telemetry. Useful for back-office QA dashboards.

| Field | Type | Notes |
|---|---|---|
| `bbox_count_original` | int | Modal-detected count before review. |
| `bbox_count_reviewed` | int | Final count after delete + merge. |
| `merged_count` | int | Number of merge operations performed. |
| `deleted_count` | int | Bboxes the technician marked deleted. |
| `recaptured_count` | int | Bboxes whose JPEG was replaced via Brio recapture. |
| `notes` | str | Free-text technician notes. May be empty. |

---

## 3. Image bundle pairing

The simplest correct pairing logic:

```python
furniture = envelope["furniture"]
best_imgs = {b["bbox_id"]: b for b in envelope["best_images"]}

for f in furniture:
    bbox_id = f["id"]
    img_meta = best_imgs.get(bbox_id)        # may be None for legacy envelopes
    img_field = f"image_{bbox_id}"
    img_file = request.FILES.get(img_field)  # may be None if Pi couldn't find on disk

    if img_file is None:
        # Skip image association OR fail the import — your call.
        # Pi-side guarantee: if a furniture entry is in the envelope,
        # its image_<id> form field SHOULD be present. But disk failures
        # on the Pi can drop one without dropping the others.
        continue

    save_jpeg(img_file.read(), bbox_id, img_meta)
```

`request.FILES` is Django's MultipartParser dict; substitute the equivalent in your framework.

---

## 4. Edge cases — what your importer must handle

### 4a. Empty room (no furniture detected)

```json
"furniture": [],
"best_images": [],
"review_meta": { "bbox_count_original": 0, "bbox_count_reviewed": 0, ... }
```

The room is real (walls present), but the model found nothing. **Persist the floor plan, skip furniture creation.** Don't reject as malformed.

### 4b. All furniture deleted by technician

```json
"furniture": [],
"best_images": [],
"review_meta": { "bbox_count_original": 28, "bbox_count_reviewed": 0, "deleted_count": 28, ... }
```

Same outcome as 4a from your importer's perspective — persist room geometry, no furniture. Use `review_meta.bbox_count_original` to spot this if you want to flag for QA ("technician deleted all 28 bboxes — rescan?").

### 4c. Furniture with no `linked_product`

The technician chose "No match" or didn't run visual search. The `linked_product` key is **absent** (not `null`). Your import logic should default to "no product association" — store the geometry, leave the product FK null.

### 4d. Recaptured image

`best_images[i].image_source == "recapture"` → the technician took a fresh photo with the Brio post-Modal. Treat the JPEG identically to an original — same dimensions, same MIME, just a different camera moment. The Pi already swapped the bytes.

### 4e. Wall without a closed loop

The model occasionally emits stub walls (door jambs, partial loops). Don't enforce "every wall is part of a closed polygon" — render whatever you receive. The Pi UI handles this by drawing each wall as an independent line segment.

### 4f. Same `class` / `bbox_id` across submissions

`bbox_id` is stable WITHIN a single envelope. Across re-submissions of the same scan, ids are regenerated by Modal — never assume `bbox_4` of submission A is the same physical chair as `bbox_4` of submission B.

### 4g. Product changes upstream

A linked product's `id` is the only stable handle. By the time the import lands, the snapshot fields (`name`, `price`, dimensions, URLs) may differ from the current Athathi product row. The Pi captures these for forensic snapshot only — your importer should join on `id` and re-fetch authoritative product data.

### 4h. Idempotent re-submission

The Pi's submit pipeline retries on 5xx. Your endpoint MUST be idempotent on `(scan_id, run_id)`. Recommended: include them in the envelope (currently they're not — see §6 for the upgrade path) OR derive them from the JWT subject + `submitted_at` window.

---

## 5. Reference Django importer

```python
import json
from decimal import Decimal
from datetime import datetime
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from django.db import transaction

from .models import Project, Wall, Door, Window, FurniturePlacement, Product


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def receive_scan(request):
    """Receive a reviewed scan from the Pi technician device.

    Auth: Authorization: Bearer <jwt>  (the technician's user JWT).
    Body: multipart/form-data with `envelope` (JSON) + `image_<bbox_id>` files.
    Idempotent on the (scan_id, submitted_at) pair.
    """
    if not request.user.user_type == "technician":
        return Response({"error": "technician only"}, status=403)

    envelope_file = request.FILES.get("envelope")
    if envelope_file is None:
        return Response({"error": "envelope missing"}, status=400)
    try:
        envelope = json.load(envelope_file)
    except (ValueError, json.JSONDecodeError) as e:
        return Response({"error": f"envelope is not valid JSON: {e}"}, status=400)

    # Locate the project. The Pi's URL embeds the scan_id; surface it
    # via a query param or path component on YOUR side.
    scan_id = request.query_params.get("scan_id")
    if not scan_id:
        return Response({"error": "scan_id query param required"}, status=400)
    project = Project.objects.filter(scan_id=scan_id).first()
    if project is None:
        return Response({"error": f"unknown scan_id {scan_id}"}, status=404)

    submitted_at = parse_iso(envelope.get("submitted_at"))
    if submitted_at is None:
        return Response({"error": "submitted_at missing or unparseable"}, status=400)

    # Idempotency: if we already imported this exact submitted_at, return 200.
    if project.last_submitted_at and project.last_submitted_at >= submitted_at:
        return Response({"ok": True, "already_imported": True}, status=200)

    fp = envelope.get("floorplan") or {}

    with transaction.atomic():
        # Wipe + rewrite — submission is the new authoritative state.
        project.walls.all().delete()
        project.doors.all().delete()
        project.windows.all().delete()
        project.furniture.all().delete()

        wall_lookup = {}
        for w in fp.get("walls") or []:
            row = Wall.objects.create(
                project=project,
                external_id=w["id"],
                start_x=w["start"][0], start_y=w["start"][1], start_z=w["start"][2],
                end_x=w["end"][0],     end_y=w["end"][1],     end_z=w["end"][2],
                height=w["height"], thickness=w.get("thickness", 0.0),
            )
            wall_lookup[w["id"]] = row

        for d in fp.get("doors") or []:
            Door.objects.create(
                project=project,
                wall=wall_lookup.get(d.get("wall")),
                cx=d["center"][0], cy=d["center"][1], cz=d["center"][2],
                width=d["width"], height=d["height"],
            )

        for wn in fp.get("windows") or []:
            Window.objects.create(
                project=project,
                wall=wall_lookup.get(wn.get("wall")),
                cx=wn["center"][0], cy=wn["center"][1], cz=wn["center"][2],
                width=wn["width"], height=wn["height"],
            )

        best_imgs = {b["bbox_id"]: b for b in (envelope.get("best_images") or [])}
        for f in envelope.get("furniture") or []:
            bbox_id = f["id"]
            lp = f.get("linked_product") or {}

            placement = FurniturePlacement.objects.create(
                project=project,
                external_id=bbox_id,
                klass=f["class"],
                cx=f["center"][0], cy=f["center"][1], cz=f["center"][2],
                size_x=f["size"][0], size_y=f["size"][1], size_z=f["size"][2],
                yaw_rad=f["yaw"],
                product_id=lp.get("id"),                 # int FK, may be None
                similarity=lp.get("similarity"),
                image_source=(best_imgs.get(bbox_id) or {}).get("image_source"),
                camera_distance_m=(best_imgs.get(bbox_id) or {}).get("camera_distance_m"),
            )

            # Save the per-bbox JPEG bundled in the multipart.
            img_file = request.FILES.get(f"image_{bbox_id}")
            if img_file is not None:
                placement.image.save(f"{bbox_id}.jpg", img_file, save=True)

        project.last_submitted_at = submitted_at
        project.review_notes      = (envelope.get("review_meta") or {}).get("notes", "")
        project.review_meta       = envelope.get("review_meta") or {}
        project.save(update_fields=["last_submitted_at", "review_notes", "review_meta"])

    return Response({
        "ok": True,
        "project_id":          project.id,
        "walls_imported":      len(fp.get("walls") or []),
        "doors_imported":      len(fp.get("doors") or []),
        "windows_imported":    len(fp.get("windows") or []),
        "furniture_imported":  len(envelope.get("furniture") or []),
    }, status=200)


def parse_iso(s):
    if not isinstance(s, str): return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
```

This is intentionally simple — read it for shape, not perfection. Production-grade additions you'll likely want:

- **Versioning**: read `envelope.schema_version` (see §6) and dispatch.
- **Background processing**: queue heavy product joins to Celery instead of blocking the request.
- **Validation library**: pydantic / DRF serializers per top-level section. The schemas in §2 are stable.
- **Image storage**: route uploads to S3 / object storage instead of Django's default FileField.
- **Conflict log**: when `product_id` references a deleted product, log it for QA review and persist the snapshot fields anyway.

---

## 6. Versioning + forward-compat

### Today

Envelopes do **not** include an explicit `schema_version`. The Pi's `apply_upload_filter` is configured by `/mnt/slam_data/.athathi/upload_filter.json` (versioned `version: 1`) but that version isn't currently stamped into the envelope.

### Recommended near-term server contract

Treat the absence of `schema_version` as `"v1"`. Add the field to the upload filter's `include_paths` so future envelopes carry it:

```json
{
  "include_paths": [
    "schema_version",
    "submitted_at",
    "scan_id",
    "run_id",
    "...",
  ]
}
```

When the Pi's upload filter is updated, the next submit auto-includes `schema_version: 2` (or whatever) and your importer dispatches.

### Proposed envelope additions for v2 (not yet shipped)

These would make idempotency + telemetry simpler. Mention them to the Pi maintainer to pull in:

| Field | Reason |
|---|---|
| `schema_version` | Explicit version tag for dispatch. |
| `scan_id` | The Athathi scan_id (currently inferred from URL or query param). |
| `run_id` | The Pi's local run identifier (`<UTC ts>_<seq>`); makes idempotency a `(scan_id, run_id)` lookup. |
| `technician.user_id` | Who reviewed it. The JWT carries `user_id` already, so this is mainly for forensics. |
| `pi_version` | Branch/commit of the Pi software for back-office "which version produced this?". |

When v2 lands, the only change in your importer is:

```python
schema = envelope.get("schema_version", "v1")
if schema == "v1":
    return import_v1(envelope, ...)
elif schema == "v2":
    return import_v2(envelope, ...)
return Response({"error": f"unknown schema_version {schema}"}, status=415)
```

---

## 7. Status codes the Pi expects

| Server returns | Pi behavior |
|---|---|
| 2xx | ✓ Submitted. Pi stamps `manifest.submitted_at`. |
| 4xx | Permanent failure. Pi stops retrying, surfaces error to technician with the upstream body's `error` / `detail` field. **Don't return 4xx for transient issues.** |
| 5xx | Transient. Pi retries up to 3× with 1/2/4 s backoff, then queues for manual retry. The technician sees a sync-pending banner and can tap "Retry now". |
| Network failure / DNS / refused | Same as 5xx — retry then queue. |

A clean error JSON body the Pi already parses:

```json
{ "error": "human-readable summary", "detail": "optional longer explanation" }
```

---

## 8. Manual end-to-end test from a Pi

If you want to drive the importer without a real Pi setup:

```bash
# 1. Get a JWT
TOKEN=$(curl -s -X POST http://116.203.199.113:8002/api/users/login/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"<technician_user>","password":"<...>"}' | jq -r .token)

# 2. Pull a real reviewed envelope from a Pi (or generate one with the
#    snippet at the bottom of this file).
ENV=/path/to/result_for_upload.json

# 3. Build the multipart bundle.
curl -X POST <upload_endpoint> \
  -H "Authorization: Bearer $TOKEN" \
  -F "envelope=@${ENV};type=application/json" \
  -F "image_bbox_4=@/path/to/4_recapture.jpg;type=image/jpeg" \
  -F "image_bbox_7=@/path/to/7.jpg;type=image/jpeg" \
  -v
```

You should see a 2xx with `{ok: true, ...}` from your importer.

---

## 9. Generating a sample envelope locally (no Pi needed)

The Pi ships a deterministic renderer in `review.py`. Given a Modal `result.json` and a synthesised `review.json`, you can produce a representative `result_for_upload.json` for end-to-end testing:

```python
# At the test_slam repo root.
import json, sys, review

with open("/path/to/result.json") as f:
    result = json.load(f)

review_doc = {
    "scan_id": 88,
    "room_name": "test_room",
    "result_job_id": result.get("job_id"),
    "started_at": "2026-04-25T20:00:00Z",
    "reviewed_at": "2026-04-25T20:42:11Z",
    "version": 1,
    "notes": "test envelope",
    "bboxes": {
        "bbox_0": {"status": "kept"},
        "bbox_1": {"status": "deleted", "reason": "duplicate"},
    },
}

reviewed = review.render_reviewed(result, review_doc)
upload = review.apply_upload_filter(reviewed, review.DEFAULT_UPLOAD_FILTER)

with open("sample_for_upload.json", "w") as f:
    json.dump(upload, f, indent=2)
```

Use this artefact to exercise your importer in CI without standing up a Pi or the Modal pipeline.

---

## 10. Quick reference card

```
ENDPOINT
  POST <upload_endpoint>           multipart/form-data, Authorization: Bearer <jwt>

FIELDS
  envelope         result_for_upload.json (application/json)
  image_<bbox_id>  one JPEG per kept bbox

REQUIRED ENVELOPE KEYS
  submitted_at, floorplan{walls,doors,windows}, furniture, best_images, review_meta

PRIMARY KEYS
  walls/doors/windows[].id   stable within envelope
  furniture[].id             matches image_<id> form field + best_images[].bbox_id
  linked_product.id          Athathi product PK (int)

UNITS
  positions: metres (world frame)
  yaw:       radians (around Z)
  prices/dimensions in linked_product: STRINGS (Decimals)

IDEMPOTENCY
  Dedupe on (scan_id, submitted_at). Treat re-submissions as full replace
  per project. Bbox ids are NOT stable across re-submissions.
```

---

End of guide. Update this file alongside any change to `submit.py`'s rendering or `review.DEFAULT_UPLOAD_FILTER`.
