#!/usr/bin/env python3
"""Runtime fixes for upPollo Stats v0.5.

Keeps the stable v0.4 application intact while correcting classification,
qBittorrent-only statistics and old log-import data.
"""
import re
from pathlib import Path

import app

app.APP_VERSION = '0.5'
app.Handler.server_version = 'upPolloStats/0.5'

# Final states intentionally have different priorities. A specific result such
# as "dupe" or "filtered" must not later be overwritten by upPollo's generic
# "all tracker uploads failed" wrapper error.
app.STATUS_ORDER = {
    'triggered': 10,
    'hardlinked': 20,
    'queued': 30,
    'processing': 40,
    'failed': 100,
    'skipped_tracker': 110,
    'skipped_group': 110,
    'skipped_category': 110,
    'skipped_tag': 110,
    'skipped_existing': 110,
    'filtered': 120,
    'dupe': 130,
    'uploaded': 140,
}
app.FINAL_STATUSES = {k for k, v in app.STATUS_ORDER.items() if v >= 100}


def _accept_status(old_status, new_status):
    return app.STATUS_ORDER.get(new_status, 0) >= app.STATUS_ORDER.get(old_status, 0)


def upsert_event(data):
    status = str(data.get('status') or 'triggered').strip().lower()
    if status not in app.STATUS_ORDER:
        status = 'triggered'
    now = app.parse_time(data.get('timestamp'))
    event_key = app.make_event_key(data)
    release_name = str(data.get('release_name') or data.get('torrent_name') or '').strip()
    if not release_name:
        release_name = Path(str(data.get('upload_path') or data.get('source_path') or 'unknown')).name or 'unknown'

    with app.DB_LOCK, app.db_conn() as conn:
        row = conn.execute('SELECT * FROM uploads WHERE event_key=?', (event_key,)).fetchone()
        if row:
            accepted = _accept_status(row['status'], status)
            fields = {
                'torrent_hash': data.get('torrent_hash') or data.get('hash') or row['torrent_hash'],
                'release_name': release_name or row['release_name'],
                'category': data.get('category') if data.get('category') is not None else row['category'],
                'release_group': data.get('release_group') if data.get('release_group') is not None else row['release_group'],
                'tracker': data.get('tracker') if data.get('tracker') is not None else row['tracker'],
                'source_path': data.get('source_path') if data.get('source_path') is not None else row['source_path'],
                'upload_path': data.get('upload_path') if data.get('upload_path') is not None else row['upload_path'],
                'status': status if accepted else row['status'],
                'source': data.get('source') or row['source'],
                'updated_at': now if accepted else row['updated_at'],
            }
            if accepted:
                fields['reason'] = data.get('reason') if data.get('reason') is not None else row['reason']
                fields['raw_last_message'] = data.get('raw_last_message') or row['raw_last_message']
                ts_col = app.status_timestamp_column(status)
                if ts_col and not row[ts_col]:
                    fields[ts_col] = now
            sets = ', '.join(f'{k}=?' for k in fields)
            conn.execute(f'UPDATE uploads SET {sets} WHERE event_key=?', tuple(fields.values()) + (event_key,))
        else:
            ts = {
                'triggered_at': now,
                'hardlinked_at': now if status == 'hardlinked' else None,
                'queued_at': now if status == 'queued' else None,
                'processing_at': now if status == 'processing' else None,
                'finished_at': now if status in app.FINAL_STATUSES else None,
            }
            conn.execute('''
                INSERT INTO uploads (
                    event_key, torrent_hash, release_name, category, release_group, tracker,
                    source_path, upload_path, status, reason, source,
                    triggered_at, hardlinked_at, queued_at, processing_at, finished_at,
                    updated_at, raw_last_message
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (
                event_key,
                data.get('torrent_hash') or data.get('hash'),
                release_name,
                data.get('category'),
                data.get('release_group'),
                data.get('tracker'),
                data.get('source_path'),
                data.get('upload_path'),
                status,
                data.get('reason'),
                data.get('source') or 'qbit',
                ts['triggered_at'], ts['hardlinked_at'], ts['queued_at'], ts['processing_at'], ts['finished_at'],
                now,
                data.get('raw_last_message'),
            ))
        conn.commit()
    return event_key


def update_by_upload_path(upload_path, status, reason=None, timestamp=None, raw=None):
    if not upload_path:
        return False
    now = app.parse_time(timestamp)
    release = Path(upload_path.rstrip('/')).name

    with app.DB_LOCK, app.db_conn() as conn:
        row = conn.execute('''
            SELECT * FROM uploads
            WHERE upload_path=? OR release_name=?
            ORDER BY triggered_at DESC LIMIT 1
        ''', (upload_path, release)).fetchone()

    if not row:
        upsert_event({
            'release_name': release,
            'upload_path': upload_path,
            'status': status,
            'reason': reason,
            'timestamp': now,
            'source': 'uppollo-log',
            'raw_last_message': raw,
        })
        return True

    if not _accept_status(row['status'], status):
        # Do not let a generic/lower-priority message destroy a more specific
        # final state or its reason.
        return True

    fields = {
        'status': status,
        'updated_at': now,
        'raw_last_message': raw or row['raw_last_message'],
    }
    if reason is not None:
        fields['reason'] = reason
    ts_col = app.status_timestamp_column(status)
    if ts_col and not row[ts_col]:
        fields[ts_col] = now
    sets = ', '.join(f'{k}=?' for k in fields)

    with app.DB_LOCK, app.db_conn() as conn:
        conn.execute(f'UPDATE uploads SET {sets} WHERE id=?', tuple(fields.values()) + (row['id'],))
        conn.commit()
    return True


# upPollo log parsing -------------------------------------------------------
FOUND_DUPE_RE = re.compile(r'^found dupe:\s*(.+)$', re.I)
TRACKER_DUPE_RE = re.compile(r'^([^:]+):\s*skipped\s*[·•:-]\s*dupe found\s*$', re.I)
CURRENT_UPLOAD_PATH = None


def parse_log_line(line):
    global CURRENT_UPLOAD_PATH
    line = line.strip()
    if not line:
        return

    tm = app.TIME_RE.search(line)
    timestamp = app.parse_time(tm.group(1) if tm else None)
    mm = app.MSG_RE.search(line)
    msg = mm.group(1).replace('\\"', '"') if mm else line

    m = app.NEW_UPLOAD_RE.match(msg)
    if m:
        CURRENT_UPLOAD_PATH = m.group(1).strip()
        update_by_upload_path(CURRENT_UPLOAD_PATH, 'processing', timestamp=timestamp, raw=msg)
        return

    m = app.PREPARE_RE.match(msg)
    if m:
        CURRENT_UPLOAD_PATH = m.group(1).strip()
        update_by_upload_path(CURRENT_UPLOAD_PATH, 'processing', timestamp=timestamp, raw=msg)
        return

    m = FOUND_DUPE_RE.match(msg)
    if m:
        if CURRENT_UPLOAD_PATH:
            update_by_upload_path(
                CURRENT_UPLOAD_PATH,
                'dupe',
                reason='Dupe auf Tracker gefunden',
                timestamp=timestamp,
                raw=msg,
            )
        return

    m = TRACKER_DUPE_RE.match(msg)
    if m:
        if CURRENT_UPLOAD_PATH:
            tracker = m.group(1).strip()
            update_by_upload_path(
                CURRENT_UPLOAD_PATH,
                'dupe',
                reason=f'Dupe auf {tracker} gefunden',
                timestamp=timestamp,
                raw=msg,
            )
        return

    m = app.SKIP_RE.match(msg)
    if m:
        reason = m.group(1).strip()
        status = 'dupe' if any(w in reason.lower() for w in app.DUPE_WORDS) else 'filtered'
        if CURRENT_UPLOAD_PATH:
            update_by_upload_path(CURRENT_UPLOAD_PATH, status, reason=reason, timestamp=timestamp, raw=msg)
        else:
            release = m.group(2).strip()
            row = app.find_recent_processing_release(release)
            if row:
                update_by_upload_path(row['upload_path'], status, reason=reason, timestamp=timestamp, raw=msg)
        return

    m = app.ERROR_RE.match(msg)
    if m:
        reason = m.group(1).strip()
        low = reason.lower()
        if low.startswith('skipping'):
            return
        status = 'dupe' if any(w in low for w in app.DUPE_WORDS) else 'failed'
        # Use the path of the current upload instead of an arbitrary recent
        # processing row. This also prevents "all tracker uploads failed" from
        # being attached to an unrelated concurrent job.
        if CURRENT_UPLOAD_PATH:
            update_by_upload_path(CURRENT_UPLOAD_PATH, status, reason=reason, timestamp=timestamp, raw=msg)
        return

    if any(p.search(msg) for p in app.SUCCESS_PATTERNS):
        if CURRENT_UPLOAD_PATH:
            update_by_upload_path(CURRENT_UPLOAD_PATH, 'uploaded', reason='Upload erfolgreich', timestamp=timestamp, raw=msg)
        return


# Data cleanup/migration ---------------------------------------------------
ORIGINAL_CLEANUP = app.cleanup_old_rows
MIGRATION_MARKER = app.CONFIG_DIR / '.migration-v0.5-stats-cleanup.done'


def _repair_old_generic_dupes():
    """Repair old rows that were misclassified as generic tracker failures.

    v0.4 did not understand upPollo's "found dupe" / "Tracker: skipped ·
    dupe found" lines and therefore stored the later generic wrapper error as
    the final result. Match the exact upload path in old logs and repair those
    rows once.
    """
    with app.DB_LOCK, app.db_conn() as conn:
        rows = conn.execute('''
            SELECT id, upload_path
            FROM uploads
            WHERE status='failed'
              AND lower(trim(COALESCE(reason,'')))='all tracker uploads failed'
              AND COALESCE(upload_path,'') <> ''
        ''').fetchall()

    if not rows or not app.UPPOLLO_LOG_DIR.exists():
        return

    log_files = sorted(p for p in app.UPPOLLO_LOG_DIR.rglob('*.log') if p.is_file())
    if not log_files:
        return

    for row in rows:
        target = f'New Upload: {row["upload_path"]}'
        repaired_reason = None
        repaired_raw = None
        for path in log_files:
            try:
                lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
            except Exception:
                continue
            for i, line in enumerate(lines):
                if target not in line:
                    continue
                # Inspect the following block. upPollo currently processes an
                # upload as one compact sequence; 120 lines is deliberately
                # generous without scanning the rest of the file.
                for candidate in lines[i + 1:i + 121]:
                    mm = app.MSG_RE.search(candidate)
                    msg = mm.group(1).replace('\\"', '"') if mm else candidate
                    tm = TRACKER_DUPE_RE.match(msg)
                    if tm:
                        repaired_reason = f'Dupe auf {tm.group(1).strip()} gefunden'
                        repaired_raw = msg
                        break
                    if FOUND_DUPE_RE.match(msg):
                        repaired_reason = 'Dupe auf Tracker gefunden'
                        repaired_raw = msg
                        # Keep looking for the tracker-specific line.
                if repaired_reason:
                    break
            if repaired_reason:
                break

        if repaired_reason:
            with app.DB_LOCK, app.db_conn() as conn:
                conn.execute('''
                    UPDATE uploads
                    SET status='dupe', reason=?, raw_last_message=?, updated_at=?
                    WHERE id=?
                ''', (repaired_reason, repaired_raw, app.utc_now_iso(), row['id']))
                conn.commit()


def cleanup_old_rows():
    ORIGINAL_CLEANUP()

    # Remove the deliberate API latency/connection fixtures. "test" is not a
    # valid production qBittorrent category in this setup.
    with app.DB_LOCK, app.db_conn() as conn:
        conn.execute("DELETE FROM uploads WHERE lower(COALESCE(category,''))='test'")
        conn.execute("DELETE FROM uploads WHERE release_name LIKE 'Stats.%'")
        conn.commit()

    if not MIGRATION_MARKER.exists():
        _repair_old_generic_dupes()
        try:
            MIGRATION_MARKER.write_text('v0.5 migration completed\n', encoding='utf-8')
        except Exception as exc:
            print(f'[migration] could not write marker: {exc}', flush=True)


# Statistics ---------------------------------------------------------------
REAL_QBIT_WHERE = """
    triggered_at >= ?
    AND source='qbit'
    AND COALESCE(category,'') <> ''
    AND lower(category) <> 'test'
    AND release_name NOT LIKE 'Stats.%'
"""


def query_stats(days):
    cutoff = (app.datetime.now(app.timezone.utc) - app.timedelta(days=days)).isoformat(timespec='seconds')
    with app.DB_LOCK, app.db_conn() as conn:
        rows = conn.execute(f'''
            SELECT status, COUNT(*) AS n
            FROM uploads
            WHERE {REAL_QBIT_WHERE}
            GROUP BY status
        ''', (cutoff,)).fetchall()
        counts = {r['status']: r['n'] for r in rows}

        total = sum(counts.values())
        uploaded = counts.get('uploaded', 0)
        filtered = counts.get('filtered', 0)
        dupe = counts.get('dupe', 0)
        failed = counts.get('failed', 0)
        skipped = sum(counts.get(s, 0) for s in [
            'skipped_tracker', 'skipped_group', 'skipped_category',
            'skipped_tag', 'skipped_existing'
        ])
        active = sum(counts.get(s, 0) for s in ['triggered', 'hardlinked', 'queued', 'processing'])

        # Success rate only considers jobs that actually reached a final
        # upPollo outcome. Active jobs and qBit-side skips do not distort it.
        completed_attempts = uploaded + filtered + dupe + failed
        success_rate = round(uploaded / completed_attempts * 100, 1) if completed_attempts else 0.0

        historical = conn.execute('''
            SELECT COUNT(*) AS n
            FROM uploads
            WHERE triggered_at >= ? AND source='uppollo-log'
        ''', (cutoff,)).fetchone()['n']

        daily = conn.execute(f'''
            SELECT substr(triggered_at,1,10) AS day,
                   COUNT(*) AS triggered,
                   SUM(CASE WHEN status='uploaded' THEN 1 ELSE 0 END) AS uploaded,
                   SUM(CASE WHEN status IN ('filtered','dupe','failed') THEN 1 ELSE 0 END) AS rejected
            FROM uploads
            WHERE {REAL_QBIT_WHERE}
            GROUP BY substr(triggered_at,1,10)
            ORDER BY day ASC
        ''', (cutoff,)).fetchall()

        cats = conn.execute(f'''
            SELECT category, COUNT(*) AS n
            FROM uploads
            WHERE {REAL_QBIT_WHERE}
            GROUP BY category
            ORDER BY n DESC, category ASC
        ''', (cutoff,)).fetchall()

    return {
        'days': days,
        'summary': {
            'triggered': total,
            'uploaded': uploaded,
            'filtered': filtered,
            'dupe': dupe,
            'skipped': skipped,
            'failed': failed,
            'active': active,
            'historical': historical,
            'success_rate': success_rate,
        },
        'counts': counts,
        'daily': [dict(r) for r in daily],
        'categories': [dict(r) for r in cats],
    }


def query_uploads(params):
    days = app.get_days(params)
    cutoff = (app.datetime.now(app.timezone.utc) - app.timedelta(days=days)).isoformat(timespec='seconds')
    status = params.get('status', [''])[0].strip()
    category = params.get('category', [''])[0].strip()
    q = params.get('q', [''])[0].strip()
    try:
        limit = max(1, min(int(params.get('limit', ['250'])[0]), 1000))
    except Exception:
        limit = 250

    sql = '''
        SELECT * FROM uploads
        WHERE triggered_at >= ?
          AND lower(COALESCE(category,'')) <> 'test'
          AND release_name NOT LIKE 'Stats.%'
    '''
    args = [cutoff]
    if status:
        sql += ' AND status=?'
        args.append(status)
    if category:
        sql += ' AND category=?'
        args.append(category)
    if q:
        sql += ' AND (release_name LIKE ? OR reason LIKE ? OR release_group LIKE ?)'
        like = f'%{q}%'
        args.extend([like, like, like])
    sql += ' ORDER BY triggered_at DESC LIMIT ?'
    args.append(limit)

    with app.DB_LOCK, app.db_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return {'days': days, 'items': [dict(r) for r in rows]}


# Install overrides before app.main starts its cleanup and watcher thread.
app.upsert_event = upsert_event
app.update_by_upload_path = update_by_upload_path
app.parse_log_line = parse_log_line
app.cleanup_old_rows = cleanup_old_rows
app.query_stats = query_stats
app.query_uploads = query_uploads


if __name__ == '__main__':
    app.main()
