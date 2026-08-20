#!/usr/bin/env python3
"""User-facing result wording fixes for upPollo Stats v0.6.

Builds on the v0.5 parser/statistics fixes and makes duplicate outcomes explicit:
a duplicate is not a technical failure; the release was not uploaded because the
same version already exists on RocketHD.
"""
from pathlib import Path

import app
import patched_app as base

app.APP_VERSION = '0.6'
app.Handler.server_version = 'upPolloStats/0.6'

DUPLICATE_REASON = 'Nicht hochgeladen – diese Version ist bereits auf RocketHD vorhanden'
GENERIC_TRACKER_FAILURE_REASON = 'Upload fehlgeschlagen – kein Tracker hat den Upload angenommen'

_original_upsert_event = base.upsert_event
_original_update_by_upload_path = base.update_by_upload_path
_original_cleanup_old_rows = base.cleanup_old_rows


def _friendly_reason(status, reason):
    if status == 'dupe':
        return DUPLICATE_REASON
    if status == 'failed' and str(reason or '').strip().lower() == 'all tracker uploads failed':
        return GENERIC_TRACKER_FAILURE_REASON
    return reason


def upsert_event(data):
    payload = dict(data)
    status = str(payload.get('status') or 'triggered').strip().lower()
    payload['reason'] = _friendly_reason(status, payload.get('reason'))
    return _original_upsert_event(payload)


def update_by_upload_path(upload_path, status, reason=None, timestamp=None, raw=None):
    return _original_update_by_upload_path(
        upload_path,
        status,
        reason=_friendly_reason(status, reason),
        timestamp=timestamp,
        raw=raw,
    )


MIGRATION_MARKER = app.CONFIG_DIR / '.migration-v0.6-friendly-reasons.done'


def cleanup_old_rows():
    _original_cleanup_old_rows()

    if MIGRATION_MARKER.exists():
        return

    # v0.5 can identify old generic failures as dupes by inspecting the
    # historical upPollo log block. Run it again for installations where the
    # original v0.5 migration marker already existed before this wording fix.
    try:
        base._repair_old_generic_dupes()
    except Exception as exc:
        print(f'[migration-v0.6] dupe repair failed: {exc}', flush=True)

    with app.DB_LOCK, app.db_conn() as conn:
        # Every confirmed duplicate gets one unambiguous user-facing reason.
        conn.execute('''
            UPDATE uploads
            SET reason=?
            WHERE status='dupe'
        ''', (DUPLICATE_REASON,))

        # Never expose upPollo's internal wrapper text in the dashboard. If no
        # duplicate evidence was found, keep it as a real technical failure but
        # translate it into a useful explanation.
        conn.execute('''
            UPDATE uploads
            SET reason=?
            WHERE status='failed'
              AND lower(trim(COALESCE(reason,'')))='all tracker uploads failed'
        ''', (GENERIC_TRACKER_FAILURE_REASON,))
        conn.commit()

    try:
        MIGRATION_MARKER.write_text('v0.6 migration completed\n', encoding='utf-8')
    except Exception as exc:
        print(f'[migration-v0.6] could not write marker: {exc}', flush=True)


# Replace the v0.5 hooks. base.parse_log_line looks up these module globals at
# runtime, so future dupe/failure messages are normalized before reaching DB.
base.upsert_event = upsert_event
base.update_by_upload_path = update_by_upload_path
app.upsert_event = upsert_event
app.update_by_upload_path = update_by_upload_path
app.cleanup_old_rows = cleanup_old_rows


if __name__ == '__main__':
    app.main()
