#!/usr/bin/env python3
"""End-to-end test against the real Modal API — gated, paid.

Targets the latest non-empty MCAP under RECORDINGS_DIR, submits it, polls to
done, asserts the on-disk artefacts are present, and re-checks the result-shape
the UI consumes.

Failure policy: on ANY exception or non-done terminal state, immediately fire
DELETE /api/session/<id> so the Modal H100 container is freed before this
script exits. Up to 3 attempts; after that the caller is expected to
investigate manually.

Run with `E2E_REAL=1 python3 tests/e2e_real_modal.py` so accidental invocations
never bill the user.
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if os.environ.get('E2E_REAL') != '1':
    print('Refusing to run: set E2E_REAL=1 to confirm real Modal call.')
    sys.exit(2)


def _latest_session():
    """Return (session_id, session_dict) for the newest stopped session
    that has a non-empty mcap on disk."""
    import app
    sessions = app._get_sessions()
    candidates = []
    for sid, s in sessions.items():
        if s.get('status') != 'stopped':
            continue
        mcap = app._find_mcap(s)
        if mcap and os.path.getsize(mcap) > 0:
            candidates.append((s.get('created', ''), sid, s, mcap))
    if not candidates:
        raise RuntimeError('No usable session with a non-empty mcap')
    candidates.sort(reverse=True)
    return candidates[0][1], candidates[0][2], candidates[0][3]


def _attempt(client, sid, session_name, mcap_path):
    print(f'\n=== Attempt: session={sid} name={session_name} '
          f'mcap={os.path.getsize(mcap_path) / 1048576:.1f} MB ===')

    r = client.post(f'/api/session/{sid}/process')
    print(f'POST /process -> {r.status_code} {r.get_json()}')
    if r.status_code != 200:
        raise RuntimeError(f'submit failed: {r.get_json()}')

    last_stage = None
    deadline = time.time() + 15 * 60   # 15 min hard cap
    while time.time() < deadline:
        time.sleep(5)
        r = client.get(f'/api/session/{sid}/result')
        if r.status_code != 200:
            print(f'  result -> {r.status_code} {r.get_data(as_text=True)[:200]}')
            time.sleep(5)
            continue
        d = r.get_json()
        status = d.get('status')
        stage = d.get('stage')
        elapsed = d.get('elapsed')
        if stage != last_stage:
            print(f'  [{int(elapsed or 0):4}s] status={status} stage={stage}')
            last_stage = stage
        if status == 'done':
            return d.get('result') or d
        if status in ('error', 'cancelled'):
            raise RuntimeError(f'terminal failure: {d}')

    raise TimeoutError('15-min deadline reached without done')


def _verify_on_disk(session_name, processed_root):
    """Assert the four required artefacts landed on disk."""
    pdir = os.path.join(processed_root, session_name)
    assert os.path.isdir(pdir), f'missing {pdir}'

    rj = os.path.join(pdir, 'result.json')
    assert os.path.isfile(rj), f'missing {rj}'
    with open(rj) as f:
        env = json.load(f)
    assert env.get('status') == 'done'
    assert 'floorplan' in env and 'walls' in env['floorplan']
    assert isinstance(env.get('furniture'), list)
    assert isinstance(env.get('best_images'), list)

    layout = os.path.join(pdir, 'layout_merged.txt')
    if os.path.isfile(layout):
        print(f'  layout_merged.txt: {os.path.getsize(layout)} bytes')
    else:
        print(f'  WARN: layout_merged.txt missing (non-fatal)')

    bv_dir = os.path.join(pdir, 'best_views')
    n_imgs = len(env['best_images'])
    if n_imgs == 0:
        print(f'  best_images empty — empty room is OK')
    else:
        assert os.path.isdir(bv_dir), f'missing {bv_dir}'
        files = sorted(os.listdir(bv_dir))
        print(f'  best_views: {len(files)} files for {n_imgs} bboxes')
        assert len(files) >= max(1, n_imgs - 2), \
            f'expected ~{n_imgs} JPEGs, got {len(files)}'
    return env


def _verify_route_shapes(client, sid, env):
    """Verify the routes the UI actually hits."""
    r = client.get(f'/api/sessions')
    assert r.status_code == 200
    rows = r.get_json()
    row = next(x for x in rows if x['id'] == sid)
    assert row['slam_status'] == 'done', row
    assert row.get('result_summary'), row
    print(f'  result_summary: {row["result_summary"]}')

    if env['best_images']:
        r = client.get(f'/api/session/{sid}/best_view/0.jpg')
        assert r.status_code == 200, r.get_data(as_text=True)[:200]
        assert r.data[:3] == b'\xff\xd8\xff', 'best_view/0.jpg not a JPEG'
        print(f'  best_view/0.jpg: {len(r.data)} bytes, JPEG magic OK')


def _safe_cancel(client, sid):
    """Best-effort cleanup after a verifier failure.

    NEVER DELETEs a job that has already reached terminal state `done` —
    cancelling a finished job wipes its server-side artefacts on Modal,
    which is destructive even though it costs no compute.
    Only fires `_modal_cancel(job_id)` for mid-flight jobs.
    """
    import app
    sess = app._get_session(sid) or {}
    job_id = sess.get('job_id')
    slam_status = sess.get('slam_status')

    if not job_id:
        print(f'  no job_id on session — nothing to cancel on Modal')
        return

    if slam_status == 'done':
        print(f'  job {job_id} already done — NOT calling DELETE '
              '(would wipe server-side artefacts)')
        return

    try:
        app._modal_cancel(job_id)
        print(f'  _modal_cancel({job_id}) issued (status was {slam_status})')
    except Exception as e:
        print(f'  cancel failed (non-fatal): {e}')

    # Reset session so the next attempt starts clean — only when we actually
    # cancelled something. A `done` job stays put.
    sess['slam_status'] = None
    sess['slam_stage'] = None
    sess.pop('slam_error', None)
    sess.pop('slam_result', None)
    sess.pop('job_id', None)
    sess.pop('idem_key', None)
    sess['status'] = 'stopped'
    app._put_session(sid, sess)
    with app._processing_lock:
        app._active_processing.pop(sid, None)


def main():
    import app
    client = app.app.test_client()

    sid, session, mcap = _latest_session()

    for attempt in range(1, 4):
        print(f'\n#### Attempt {attempt} of 3 ####')
        try:
            env = _attempt(client, sid, session['name'], mcap)
            _verify_on_disk(session['name'], app.PROCESSED_DIR)
            _verify_route_shapes(client, sid, env)
            print(f'\nE2E PASSED on attempt {attempt}.')
            return 0
        except Exception as e:
            print(f'\nE2E ATTEMPT {attempt} FAILED: {type(e).__name__}: {e}')
            _safe_cancel(client, sid)
            if attempt == 3:
                print('3 strikes — stopping. Manual review needed.')
                return 1
            print('Retrying after 30 s...')
            time.sleep(30)


if __name__ == '__main__':
    sys.exit(main())
