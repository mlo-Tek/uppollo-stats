#!/usr/bin/env python3
"""Short user-facing result wording for upPollo Stats v0.7."""

import app
import patched_app_v06 as base

app.APP_VERSION = '0.7'
app.Handler.server_version = 'upPolloStats/0.7'

# Short, precise dashboard reasons.
base.DUPLICATE_REASON = 'Bereits auf RocketHD'
base.GENERIC_TRACKER_FAILURE_REASON = 'Tracker-Upload fehlgeschlagen'

_previous_cleanup = base.cleanup_old_rows
MIGRATION_MARKER = app.CONFIG_DIR / '.migration-v0.7-short-reasons.done'


def cleanup_old_rows():
    _previous_cleanup()

    if MIGRATION_MARKER.exists():
        return

    with app.DB_LOCK, app.db_conn() as conn:
        conn.execute("UPDATE uploads SET reason=? WHERE status='dupe'", (base.DUPLICATE_REASON,))
        conn.execute('''
            UPDATE uploads
            SET reason=?
            WHERE status='failed'
              AND lower(trim(COALESCE(reason,''))) IN (
                  'all tracker uploads failed',
                  'upload fehlgeschlagen – kein tracker hat den upload angenommen'
              )
        ''', (base.GENERIC_TRACKER_FAILURE_REASON,))
        conn.commit()

    try:
        MIGRATION_MARKER.write_text('v0.7 migration completed\n', encoding='utf-8')
    except Exception as exc:
        print(f'[migration-v0.7] could not write marker: {exc}', flush=True)


app.cleanup_old_rows = cleanup_old_rows


if __name__ == '__main__':
    app.main()
