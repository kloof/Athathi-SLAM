"""Auto-grown class taxonomy aggregator + Athathi categories cache.

Plan §7d (auto-grow taxonomy from result.json class counts), §20 (visual
search → linked_product), §22d (helpers), §23a (Athathi `/api/categories/`
seeds 10 entries including BOTH "Chair" id 24 and "Chairs" id 26).

Pure helpers, framework-free. Lazy `import auth` / `import projects`
inside functions where needed — same pattern as `submit.py` / `review.py`,
so a cold `import taxonomy` never drags Flask in.

Public surface (verbatim from the Step 7 brief):

    TAXONOMY_CACHE_NAME, LEARNED_CLASSES_NAME

    aggregate_local_classes()                    -> {class_name: count}
    load_learned_classes()                       -> {class_name: count}
    save_learned_classes(d)                      -> None
    add_learned_class(name)                      -> None

    merged_taxonomy(athathi_categories=None)     -> [{name, count, source, ...}]

    cache_athathi_categories(categories)         -> None
    load_cached_athathi_categories(max_age_s=3600) -> [...] | None

This module performs zero network I/O. The Athathi `/api/categories/`
upstream is fetched by `athathi_proxy.get_categories` (Step 2); this
module only consumes its result and caches a snapshot.
"""

import glob
import json
import os
import sys
import tempfile
import time as _time


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Filename under <ATHATHI_DIR>. The full path is computed at call time so
# tests that monkeypatch `auth.ATHATHI_DIR` don't need to re-import this
# module.
TAXONOMY_CACHE_NAME = 'taxonomy_seed.json'

# auth.LEARNED_CLASSES_PATH already declares the absolute path, but the
# brief asks for the bare filename here too — we expose it for symmetry
# and so consumers can reason about the on-disk layout.
LEARNED_CLASSES_NAME = 'learned_classes.json'


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _athathi_dir():
    """Re-resolve `auth.ATHATHI_DIR` at call time.

    Tests monkeypatch the module attribute on `auth` directly (mirrors
    `review._athathi_dir`), so we MUST read it through the module each
    call rather than caching at import time.
    """
    import auth as _auth   # local import — keep cold-import cost zero
    return _auth.ATHATHI_DIR


def _learned_classes_path():
    """`<ATHATHI_DIR>/learned_classes.json`. Re-resolved at call time."""
    import auth as _auth
    return _auth.LEARNED_CLASSES_PATH


def _taxonomy_cache_path():
    """`<ATHATHI_DIR>/taxonomy_seed.json`. Re-resolved at call time."""
    return os.path.join(_athathi_dir(), TAXONOMY_CACHE_NAME)


def _ensure_athathi_dir():
    """Create `<ATHATHI_DIR>` (mode 0700) if missing. Best-effort."""
    d = _athathi_dir()
    try:
        os.makedirs(d, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    except OSError:
        pass


def _atomic_write_json(path, payload, mode=0o644):
    """Write JSON atomically via temp+rename in the same dir."""
    parent = os.path.dirname(path) or '.'
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix='.tmp.', dir=parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(payload, f, indent=2)
            f.write('\n')
        os.replace(tmp, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json_or_none(path):
    """Return parsed JSON, or None on missing/unreadable/unparseable.

    Errors are intentionally swallowed: a corrupt result.json on one scan
    must not poison the entire taxonomy aggregation.
    """
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Local class aggregation (plan §7d)
# ---------------------------------------------------------------------------

def _projects_root():
    """Re-resolve `projects.PROJECTS_ROOT` at call time so tests can patch it."""
    import projects as _projects
    return _projects.PROJECTS_ROOT


def _legacy_processed_root():
    """Re-resolve the legacy `<repo>/processed` (or `/mnt/.../processed`).

    Mirrors the `app.PROCESSED_DIR` resolution. We do NOT import `app`
    (would drag Flask in); instead we replicate the resolution inline.
    """
    if os.path.isdir('/mnt/slam_data'):
        return '/mnt/slam_data/processed'
    # Repo-relative fallback. `<this_module_dir>/processed`.
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, 'processed')


def aggregate_local_classes():
    """Walk every `result.json` and count `furniture[i].class` occurrences.

    Two trees:
      1. `<PROJECTS_ROOT>/*/scans/*/processed/runs/*/result.json` — current.
      2. `<PROCESSED_DIR>/*/result.json`                          — legacy.

    Returns:
        dict[str, int] mapping class name to occurrence count. Empty dict
        when no result.json files exist or when none have a `furniture`
        list.

    Tolerant by design: parse errors and unexpected shapes are skipped
    silently — a single bad file must NOT poison the aggregator. Class
    names are stripped of leading/trailing whitespace; empty / non-string
    class fields are ignored.
    """
    counts = {}

    pattern_new = os.path.join(
        _projects_root(), '*', 'scans', '*', 'processed', 'runs', '*',
        'result.json',
    )
    pattern_legacy = os.path.join(_legacy_processed_root(), '*', 'result.json')

    for pattern in (pattern_new, pattern_legacy):
        for path in glob.glob(pattern):
            data = _read_json_or_none(path)
            if not isinstance(data, dict):
                continue
            furniture = data.get('furniture')
            if not isinstance(furniture, list):
                continue
            for item in furniture:
                if not isinstance(item, dict):
                    continue
                cls = item.get('class')
                if not isinstance(cls, str):
                    continue
                cls = cls.strip()
                if not cls:
                    continue
                counts[cls] = counts.get(cls, 0) + 1

    return counts


# ---------------------------------------------------------------------------
# Learned (technician-introduced) classes
# ---------------------------------------------------------------------------

def load_learned_classes():
    """Return the parsed learned-classes dict, or `{}` on missing/corrupt.

    Schema: `{<class_name>: <count_seen>}`. Non-int counts are dropped to
    keep the aggregator predictable.
    """
    data = _read_json_or_none(_learned_classes_path())
    if not isinstance(data, dict):
        return {}
    out = {}
    for k, v in data.items():
        if not isinstance(k, str) or not k.strip():
            continue
        if not isinstance(v, int):
            # Decimal counts would be a future schema bump; coerce ints,
            # drop floats / strings / etc. silently.
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
        out[k] = v
    return out


def save_learned_classes(d):
    """Atomic write to `<ATHATHI_DIR>/learned_classes.json`. Creates the dir."""
    if not isinstance(d, dict):
        raise TypeError('d must be a dict')
    _ensure_athathi_dir()
    _atomic_write_json(_learned_classes_path(), d, mode=0o644)


def add_learned_class(name):
    """Bump the count for `name` in learned_classes.json.

    Trims leading/trailing whitespace; rejects empty / whitespace-only
    names silently (no exception — the route can call this even when the
    technician submits an empty value, and we just no-op).
    """
    if not isinstance(name, str):
        return
    name = name.strip()
    if not name:
        return
    cur = load_learned_classes()
    cur[name] = cur.get(name, 0) + 1
    save_learned_classes(cur)


# ---------------------------------------------------------------------------
# Athathi categories cache (offline-fallback seed)
# ---------------------------------------------------------------------------

def cache_athathi_categories(categories):
    """Persist a snapshot of the upstream `/api/categories/` body.

    Wraps the categories list with `{cached_at, blob}` so we can answer
    "how stale is this?" later. Atomic temp+rename. Best-effort: on disk
    error we re-raise (the caller logs).
    """
    if categories is None:
        return
    _ensure_athathi_dir()
    wrapper = {'cached_at': _time.time(), 'blob': categories}
    _atomic_write_json(_taxonomy_cache_path(), wrapper, mode=0o644)


def load_cached_athathi_categories(max_age_s=3600):
    """Return the cached snapshot if fresh; None when missing or stale.

    `max_age_s` is the staleness threshold in seconds. Negative / zero
    values disable the freshness gate (always return None — useful only
    in tests). The cache file format: `{cached_at: <ts>, blob: [...]}`.
    """
    path = _taxonomy_cache_path()
    data = _read_json_or_none(path)
    if not isinstance(data, dict):
        return None
    ts = data.get('cached_at')
    blob = data.get('blob')
    if not isinstance(ts, (int, float)):
        return None
    if max_age_s <= 0:
        return None
    if (_time.time() - ts) > max_age_s:
        return None
    return blob


# ---------------------------------------------------------------------------
# Three-source merger
# ---------------------------------------------------------------------------

def _normalize_categories_payload(athathi_categories):
    """Coerce the upstream payload to a list of {id?, name, description?} dicts.

    Tolerant of two shapes:
      - bare list:       `[{id, name, description}, ...]`
      - wrapped dict:    `{categories: [{...}]}`

    Anything else returns an empty list (no exception). Items missing a
    string `name` field are skipped.
    """
    if athathi_categories is None:
        return []
    cats = athathi_categories
    if isinstance(cats, dict):
        cats = cats.get('categories')
    if not isinstance(cats, list):
        return []
    out = []
    for it in cats:
        if not isinstance(it, dict):
            continue
        name = it.get('name')
        if not isinstance(name, str) or not name.strip():
            continue
        entry = {'name': name.strip()}
        if 'id' in it:
            entry['athathi_id'] = it['id']
        if 'description' in it and isinstance(it.get('description'), str):
            entry['description'] = it['description']
        out.append(entry)
    return out


def merged_taxonomy(athathi_categories=None):
    """Merge local + Athathi-categories + learned-classes into one list.

    Sources, in priority order for the canonical-name preservation:
      1. Local model counts (from `result.json` walk).
      2. Athathi categories (from `/api/categories/` — `athathi_id`,
         `description` fields preserved).
      3. Technician-introduced learned classes.

    Returns:
        list of dicts shaped `{name, count, source, athathi_id?, description?}`
        sorted by `count` descending. Names are deduplicated case
        INSENSITIVELY but the canonical name is the most-common spelling
        (highest count wins; ties broken by insertion order — local
        first, then Athathi, then technician).

    Special case (plan §23a): Athathi returns BOTH "Chair" (id 24) AND
    "Chairs" (id 26). The case-insensitive merge collapses different
    casings of the SAME word ("chair" / "Chair"), but "Chair" and
    "Chairs" remain separate entries (different lemmas, separate ids).
    """
    # Per-canonical-key bucket: { lower_name: {best_name, count, source,
    # athathi_id?, description?, _name_counts: {spelling: count}} }
    buckets = {}

    def _bump(key, spelling, count, source, **extra):
        b = buckets.get(key)
        if b is None:
            b = {
                'name': spelling,
                'count': 0,
                'source': source,
                '_name_counts': {},
            }
            for ek, ev in extra.items():
                if ev is not None:
                    b[ek] = ev
            buckets[key] = b
        else:
            # Merge richer fields without clobbering existing ones.
            for ek, ev in extra.items():
                if ev is not None and ek not in b:
                    b[ek] = ev
            # Source priority: model > technician > athathi for display
            # ordering — keep whatever we saw FIRST unless a higher
            # priority arrives. Implemented via the source priority map.
            if _source_priority(source) > _source_priority(b['source']):
                b['source'] = source
        b['count'] += count
        nc = b['_name_counts']
        nc[spelling] = nc.get(spelling, 0) + count

    # 1. Local model counts.
    for name, cnt in aggregate_local_classes().items():
        if not isinstance(name, str) or not name.strip():
            continue
        if cnt <= 0:
            continue
        _bump(name.strip().lower(), name.strip(), cnt, 'model')

    # 2. Athathi categories. Each contributes count=0 unless local already
    #    saw it (the count is added to the existing bucket via _bump's
    #    accumulator). We pass count=0 so the bucket appears for unseen
    #    upstream classes.
    for it in _normalize_categories_payload(athathi_categories):
        name = it['name']
        key = name.lower()
        _bump(
            key, name, 0, 'athathi',
            athathi_id=it.get('athathi_id'),
            description=it.get('description'),
        )

    # 3. Technician-introduced learned classes.
    for name, cnt in load_learned_classes().items():
        if not isinstance(name, str) or not name.strip():
            continue
        spelling = name.strip()
        key = spelling.lower()
        # learned counts contribute to the bucket count too.
        _bump(key, spelling, max(int(cnt), 1), 'technician')

    # Pick the canonical spelling per bucket: the most-common spelling
    # wins. Ties broken by lex order so the output is deterministic.
    out = []
    for key, b in buckets.items():
        nc = b.pop('_name_counts')
        if nc:
            best = max(nc.items(), key=lambda kv: (kv[1], -_lex_rank(kv[0])))
            b['name'] = best[0]
        out.append(b)

    # Sort by count desc; tie-break by name asc (deterministic UI).
    out.sort(key=lambda d: (-int(d.get('count') or 0), d['name'].lower()))
    return out


# Source priority for "what color does this class get in the UI?". Higher
# wins when two sources contribute the same canonical name. Model-derived
# counts are the most authoritative (they reflect what the device has
# actually seen); technician-introduced names are the next most useful
# (the technician explicitly added them); Athathi categories are the
# weakest (just a seed list). The exact ordering only matters for the
# `source` field on the merged dict.
_SOURCE_PRIORITY = {'model': 3, 'technician': 2, 'athathi': 1}


def _source_priority(s):
    return _SOURCE_PRIORITY.get(s, 0)


def _lex_rank(s):
    """Negative because we want larger lex values to lose tie-breaks."""
    # Avoid surrogate-pair issues: just compare string codepoint sums.
    # The exact mapping doesn't matter as long as it's stable and total.
    if not isinstance(s, str):
        return 0
    return sum(ord(c) for c in s)
