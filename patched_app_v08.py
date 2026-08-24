#!/usr/bin/env python3
"""Short classification for SRRDB/NFO verification failures in upPollo Stats v0.8."""

import re

import app
import patched_app_v07 as v07

v06 = v07.base

app.APP_VERSION = '0.8'
app.Handler.server_version = 'upPolloStats/0.8'

NFO_MISMATCH_REASON = 'NFO/SRRDB-Mismatch'


def _short_reason(status, reason):
    text = str(reason or '').strip()
    low = text.lower()

    if status == 'dupe':
        return v06.DUPLICATE_REASON

    if status == 'failed':
        if low in {
            'all tracker uploads failed',
            'upload fehlgeschlagen – kein tracker hat den upload angenommen',
            'tracker-upload fehlgeschlagen',
        }:
            return v06.GENERIC_TRACKER_FAILURE_REASON

        # upPollo scene/season-pack verification errors: keep the useful
        # episode number when present, but hide the very long remediation text.
        if 'nfo' in low and 'srrdb' in low:
            m = re.search(r'\b(E\d{2})\b', text, re.I)
            if m:
                return f'{m.group(1).upper()} {NFO_MISMATCH_REASON}'
            return NFO_MISMATCH_REASON

    return reason


# v0.6's wrappers call _friendly_reason dynamically, so replacing it here
# automatically affects future parser updates without duplicating the parser.
v06._friendly_reason = _short_reason

_previous_cleanup = v07.cleanup_old_rows
MIGRATION_MARKER = app.CONFIG_DIR / '.migration-v0.8-short-nfo-reasons.done'


def cleanup_old_rows():
    _previous_cleanup()

    if MIGRATION_MARKER.exists():
        return

    with app.DB_LOCK, app.db_conn() as conn:
        rows = conn.execute('''
            SELECT id, reason
            FROM uploads
            WHERE status='failed'
              AND lower(COALESCE(reason,'')) LIKE '%nfo%'
              AND lower(COALESCE(reason,'')) LIKE '%srrdb%'
        ''').fetchall()

        for row in rows:
            short = _short_reason('failed', row['reason'])
            conn.execute('UPDATE uploads SET reason=? WHERE id=?', (short, row['id']))

        conn.commit()

    try:
        MIGRATION_MARKER.write_text('v0.8 migration completed\n', encoding='utf-8')
    except Exception as exc:
        print(f'[migration-v0.8] could not write marker: {exc}', flush=True)


app.cleanup_old_rows = cleanup_old_rows


if __name__ == '__main__':
    app.main()
