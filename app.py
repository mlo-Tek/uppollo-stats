#!/usr/bin/env python3
import json
import os
import re
import sqlite3
import threading
import time
import hashlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', '/config'))
DB_PATH = Path(os.environ.get('DB_PATH', str(CONFIG_DIR / 'uploads.db')))
STATIC_DIR = Path(os.environ.get('STATIC_DIR', '/app/static'))
UPPOLLO_LOG_DIR = Path(os.environ.get('UPPOLLO_LOG_DIR', '/uppollo-logs'))
API_KEY = os.environ.get('STATS_API_KEY', '')
HOST = os.environ.get('HOST', '0.0.0.0')
PORT = int(os.environ.get('PORT', '8781'))
LOG_SCAN_SECONDS = float(os.environ.get('LOG_SCAN_SECONDS', '2'))
RETENTION_DAYS = int(os.environ.get('RETENTION_DAYS', '365'))
APP_VERSION = '0.4'

CONFIG_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

STATUS_ORDER = {
    'triggered': 10,
    'hardlinked': 20,
    'queued': 30,
    'processing': 40,
    'uploaded': 100,
    'skipped_tracker': 100,
    'skipped_group': 100,
    'skipped_category': 100,
    'skipped_tag': 100,
    'skipped_existing': 100,
    'filtered': 100,
    'dupe': 100,
    'failed': 100,
}
FINAL_STATUSES = {k for k, v in STATUS_ORDER.items() if v == 100}
DB_LOCK = threading.RLock()


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def parse_time(value):
    if not value:
        return utc_now_iso()
    s = str(value).strip()
    try:
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec='seconds')
    except Exception:
        return utc_now_iso()


@contextmanager
def db_conn(timeout=2.0):
    """Open a short-lived SQLite connection and always close it.

    journal_mode is intentionally NOT changed here. Running
    PRAGMA journal_mode=WAL for every request can require a schema/exclusive
    lock and caused /api/event to stall for SQLite's 30 second timeout while
    the log watcher was active.
    """
    conn = sqlite3.connect(DB_PATH, timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=2000')
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    # Set WAL once during startup, not on every request/scan connection.
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, timeout=5, check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA busy_timeout=5000')
            conn.executescript('''
        CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            torrent_hash TEXT,
            release_name TEXT NOT NULL,
            category TEXT,
            release_group TEXT,
            tracker TEXT,
            source_path TEXT,
            upload_path TEXT,
            status TEXT NOT NULL DEFAULT 'triggered',
            reason TEXT,
            source TEXT NOT NULL DEFAULT 'qbit',
            triggered_at TEXT NOT NULL,
            hardlinked_at TEXT,
            queued_at TEXT,
            processing_at TEXT,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            raw_last_message TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_uploads_triggered_at ON uploads(triggered_at);
        CREATE INDEX IF NOT EXISTS idx_uploads_status ON uploads(status);
        CREATE INDEX IF NOT EXISTS idx_uploads_category ON uploads(category);
        CREATE INDEX IF NOT EXISTS idx_uploads_upload_path ON uploads(upload_path);
        CREATE INDEX IF NOT EXISTS idx_uploads_hash ON uploads(torrent_hash);

        CREATE TABLE IF NOT EXISTS log_offsets (
            path TEXT PRIMARY KEY,
            inode INTEGER,
            offset INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
            ''')
            conn.commit()
        finally:
            conn.close()


def make_event_key(data):
    h = (data.get('torrent_hash') or data.get('hash') or '').strip().lower()
    if h:
        return 'hash:' + h
    basis = '|'.join([
        str(data.get('release_name') or data.get('torrent_name') or ''),
        str(data.get('source_path') or ''),
        str(data.get('upload_path') or ''),
    ])
    return 'fallback:' + hashlib.sha256(basis.encode('utf-8')).hexdigest()[:32]


def status_timestamp_column(status):
    return {
        'triggered': 'triggered_at',
        'hardlinked': 'hardlinked_at',
        'queued': 'queued_at',
        'processing': 'processing_at',
    }.get(status, 'finished_at' if status in FINAL_STATUSES else None)


def upsert_event(data):
    status = str(data.get('status') or 'triggered').strip().lower()
    if status not in STATUS_ORDER:
        status = 'triggered'
    now = parse_time(data.get('timestamp'))
    event_key = make_event_key(data)
    release_name = str(data.get('release_name') or data.get('torrent_name') or '').strip()
    if not release_name:
        release_name = Path(str(data.get('upload_path') or data.get('source_path') or 'unknown')).name or 'unknown'

    with DB_LOCK, db_conn() as conn:
        row = conn.execute('SELECT * FROM uploads WHERE event_key=?', (event_key,)).fetchone()
        if row:
            old_status = row['status']
            old_rank = STATUS_ORDER.get(old_status, 0)
            new_rank = STATUS_ORDER.get(status, 0)
            chosen_status = status if (new_rank >= old_rank or old_status in {'queued', 'processing'}) else old_status
            fields = {
                'torrent_hash': data.get('torrent_hash') or data.get('hash') or row['torrent_hash'],
                'release_name': release_name or row['release_name'],
                'category': data.get('category') if data.get('category') is not None else row['category'],
                'release_group': data.get('release_group') if data.get('release_group') is not None else row['release_group'],
                'tracker': data.get('tracker') if data.get('tracker') is not None else row['tracker'],
                'source_path': data.get('source_path') if data.get('source_path') is not None else row['source_path'],
                'upload_path': data.get('upload_path') if data.get('upload_path') is not None else row['upload_path'],
                'status': chosen_status,
                'reason': data.get('reason') if data.get('reason') is not None else row['reason'],
                'source': data.get('source') or row['source'],
                'updated_at': now,
                'raw_last_message': data.get('raw_last_message') or row['raw_last_message'],
            }
            ts_col = status_timestamp_column(status)
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
                'finished_at': now if status in FINAL_STATUSES else None,
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
    now = parse_time(timestamp)
    release = Path(upload_path.rstrip('/')).name

    with DB_LOCK, db_conn() as conn:
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

    old_status = row['status']
    old_rank = STATUS_ORDER.get(old_status, 0)
    new_rank = STATUS_ORDER.get(status, 0)
    chosen = status if (new_rank >= old_rank or old_status in {'queued', 'processing'}) else old_status
    fields = {'status': chosen, 'updated_at': now, 'raw_last_message': raw or row['raw_last_message']}
    if reason is not None:
        fields['reason'] = reason
    ts_col = status_timestamp_column(status)
    if ts_col and not row[ts_col]:
        fields[ts_col] = now
    sets = ', '.join(f'{k}=?' for k in fields)

    with DB_LOCK, db_conn() as conn:
        conn.execute(f'UPDATE uploads SET {sets} WHERE id=?', tuple(fields.values()) + (row['id'],))
        conn.commit()
    return True


def find_recent_processing_release(release_name=None):
    with DB_LOCK, db_conn() as conn:
        if release_name:
            row = conn.execute('''
                SELECT * FROM uploads WHERE release_name=?
                ORDER BY triggered_at DESC LIMIT 1
            ''', (release_name,)).fetchone()
            if row:
                return row
        return conn.execute('''
            SELECT * FROM uploads
            WHERE status IN ('queued','processing','hardlinked','triggered')
            ORDER BY updated_at DESC LIMIT 1
        ''').fetchone()


TIME_RE = re.compile(r'time=([^\s]+)')
MSG_RE = re.compile(r'msg="(.*)"$')
NEW_UPLOAD_RE = re.compile(r'^New Upload:\s+(.+)$', re.I)
PREPARE_RE = re.compile(r'^Preparing to upload\s+(.+)$', re.I)
SKIP_RE = re.compile(r'^Skipping\s*\((.*?)\):\s*(.+)$', re.I)
ERROR_RE = re.compile(r'^Error during upload processing:\s*(.+)$', re.I)

SUCCESS_PATTERNS = [
    re.compile(p, re.I) for p in [
        r'upload(?:ed)? successfully',
        r'successfully uploaded',
        r'upload successful',
        r'upload completed',
        r'torrent uploaded successfully',
    ]
]
DUPE_WORDS = ('dupe', 'duplicate', 'already exists', 'already uploaded', 'exists on tracker', 'torrent already')


def parse_log_line(line):
    line = line.strip()
    if not line:
        return
    tm = TIME_RE.search(line)
    timestamp = parse_time(tm.group(1) if tm else None)
    mm = MSG_RE.search(line)
    msg = mm.group(1).replace('\\"', '"') if mm else line

    m = NEW_UPLOAD_RE.match(msg)
    if m:
        update_by_upload_path(m.group(1).strip(), 'processing', timestamp=timestamp, raw=msg)
        return

    m = PREPARE_RE.match(msg)
    if m:
        update_by_upload_path(m.group(1).strip(), 'processing', timestamp=timestamp, raw=msg)
        return

    m = SKIP_RE.match(msg)
    if m:
        reason = m.group(1).strip()
        release = m.group(2).strip()
        status = 'dupe' if any(w in reason.lower() for w in DUPE_WORDS) else 'filtered'
        row = find_recent_processing_release(release)
        if row:
            update_by_upload_path(row['upload_path'], status, reason=reason, timestamp=timestamp, raw=msg)
        return

    m = ERROR_RE.match(msg)
    if m:
        reason = m.group(1).strip()
        low = reason.lower()
        if low.startswith('skipping'):
            return
        status = 'dupe' if any(w in low for w in DUPE_WORDS) else 'failed'
        row = find_recent_processing_release()
        if row:
            update_by_upload_path(row['upload_path'], status, reason=reason, timestamp=timestamp, raw=msg)
        return

    if any(p.search(msg) for p in SUCCESS_PATTERNS):
        row = find_recent_processing_release()
        if row:
            update_by_upload_path(row['upload_path'], 'uploaded', reason='Upload erfolgreich', timestamp=timestamp, raw=msg)


def get_offset(conn, path, inode):
    row = conn.execute('SELECT inode, offset FROM log_offsets WHERE path=?', (str(path),)).fetchone()
    if not row or row['inode'] != inode:
        return 0
    return int(row['offset'])


def set_offset(conn, path, inode, offset):
    conn.execute('''
        INSERT INTO log_offsets(path, inode, offset, updated_at) VALUES(?,?,?,?)
        ON CONFLICT(path) DO UPDATE SET inode=excluded.inode, offset=excluded.offset, updated_at=excluded.updated_at
    ''', (str(path), inode, offset, utc_now_iso()))


def scan_logs_once():
    if not UPPOLLO_LOG_DIR.exists():
        return
    files = sorted([p for p in UPPOLLO_LOG_DIR.rglob('*.log') if p.is_file()])
    if not files:
        return

    for path in files:
        try:
            st = path.stat()

            with DB_LOCK, db_conn() as conn:
                offset = get_offset(conn, path, st.st_ino)

            if offset > st.st_size:
                offset = 0

            with path.open('r', encoding='utf-8', errors='replace') as fh:
                fh.seek(offset)
                while True:
                    line = fh.readline()
                    if not line:
                        break
                    parse_log_line(line)
                new_offset = fh.tell()

            with DB_LOCK, db_conn() as conn:
                set_offset(conn, path, st.st_ino, new_offset)
                conn.commit()

        except Exception as exc:
            print(f'[log-watcher] {path}: {exc}', flush=True)


def log_watcher():
    while True:
        try:
            scan_logs_once()
        except Exception as exc:
            print(f'[log-watcher] scan failed: {exc}', flush=True)
        time.sleep(LOG_SCAN_SECONDS)


def cleanup_old_rows():
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat(timespec='seconds')
    with DB_LOCK, db_conn() as conn:
        conn.execute('DELETE FROM uploads WHERE triggered_at < ?', (cutoff,))
        conn.commit()


def get_days(params):
    try:
        days = int(params.get('days', ['30'])[0])
    except Exception:
        days = 30
    return max(1, min(days, 3650))


def query_stats(days):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec='seconds')
    with DB_LOCK, db_conn() as conn:
        rows = conn.execute('''
            SELECT status, COUNT(*) AS n
            FROM uploads WHERE triggered_at >= ?
            GROUP BY status
        ''', (cutoff,)).fetchall()
        counts = {r['status']: r['n'] for r in rows}
        total = sum(counts.values())
        uploaded = counts.get('uploaded', 0)
        filtered = counts.get('filtered', 0)
        dupe = counts.get('dupe', 0)
        failed = counts.get('failed', 0)
        skipped = sum(counts.get(s, 0) for s in ['skipped_tracker','skipped_group','skipped_category','skipped_tag','skipped_existing'])
        active = sum(counts.get(s, 0) for s in ['triggered','hardlinked','queued','processing'])
        success_rate = round(uploaded / total * 100, 1) if total else 0.0

        daily = conn.execute('''
            SELECT substr(triggered_at,1,10) AS day,
                   COUNT(*) AS triggered,
                   SUM(CASE WHEN status='uploaded' THEN 1 ELSE 0 END) AS uploaded,
                   SUM(CASE WHEN status IN ('filtered','dupe','failed') THEN 1 ELSE 0 END) AS rejected
            FROM uploads WHERE triggered_at >= ?
            GROUP BY substr(triggered_at,1,10)
            ORDER BY day ASC
        ''', (cutoff,)).fetchall()

        cats = conn.execute('''
            SELECT COALESCE(NULLIF(category,''),'unknown') AS category, COUNT(*) AS n
            FROM uploads WHERE triggered_at >= ?
            GROUP BY COALESCE(NULLIF(category,''),'unknown')
            ORDER BY n DESC
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
            'success_rate': success_rate,
        },
        'counts': counts,
        'daily': [dict(r) for r in daily],
        'categories': [dict(r) for r in cats],
    }


def query_uploads(params):
    days = get_days(params)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec='seconds')
    status = params.get('status', [''])[0].strip()
    category = params.get('category', [''])[0].strip()
    q = params.get('q', [''])[0].strip()
    try:
        limit = max(1, min(int(params.get('limit', ['250'])[0]), 1000))
    except Exception:
        limit = 250

    sql = 'SELECT * FROM uploads WHERE triggered_at >= ?'
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
    with DB_LOCK, db_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return {'days': days, 'items': [dict(r) for r in rows]}


class Handler(BaseHTTPRequestHandler):
    server_version = f'upPolloStats/{APP_VERSION}'

    def log_message(self, fmt, *args):
        print('[http] ' + fmt % args, flush=True)

    def send_json(self, obj, status=200):
        raw = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)

    def send_file(self, path, content_type):
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        if content_type.startswith('text/html'):
            self.send_header('Cache-Control', 'no-cache')
        else:
            self.send_header('Cache-Control', 'public, max-age=3600')
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self):
        if not API_KEY:
            return True
        x = self.headers.get('X-Stats-Key', '')
        auth = self.headers.get('Authorization', '')
        return x == API_KEY or auth == f'Bearer {API_KEY}'

    def do_GET(self):
        p = urlparse(self.path)
        params = parse_qs(p.query)
        if p.path == '/api/health':
            self.send_json({
                'ok': True,
                'version': APP_VERSION,
                'db': str(DB_PATH),
                'log_dir': str(UPPOLLO_LOG_DIR),
                'log_dir_exists': UPPOLLO_LOG_DIR.exists(),
            })
        elif p.path == '/api/stats':
            self.send_json(query_stats(get_days(params)))
        elif p.path == '/api/uploads':
            self.send_json(query_uploads(params))
        elif p.path == '/' or p.path == '/index.html':
            self.send_file(STATIC_DIR / 'index.html', 'text/html; charset=utf-8')
        elif p.path == '/app.js':
            self.send_file(STATIC_DIR / 'app.js', 'application/javascript; charset=utf-8')
        elif p.path == '/style.css':
            self.send_file(STATIC_DIR / 'style.css', 'text/css; charset=utf-8')
        else:
            self.send_error(404)

    def do_POST(self):
        p = urlparse(self.path)
        if p.path != '/api/event':
            self.send_error(404)
            return
        if not self.authorized():
            self.send_json({'ok': False, 'error': 'unauthorized'}, 401)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > 1024 * 1024:
                raise ValueError('invalid content length')
            data = json.loads(self.rfile.read(length).decode('utf-8'))
            if not isinstance(data, dict):
                raise ValueError('JSON object expected')
            key = upsert_event(data)
            self.send_json({'ok': True, 'event_key': key}, 200)
        except Exception as exc:
            self.send_json({'ok': False, 'error': str(exc)}, 400)


def main():
    init_db()
    cleanup_old_rows()
    t = threading.Thread(target=log_watcher, daemon=True, name='uppollo-log-watcher')
    t.start()
    print(f'upPollo Stats {APP_VERSION} listening on http://{HOST}:{PORT}', flush=True)
    print(f'Database: {DB_PATH}', flush=True)
    print(f'upPollo log dir: {UPPOLLO_LOG_DIR}', flush=True)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.serve_forever()


if __name__ == '__main__':
    main()
