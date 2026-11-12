"""Incrementally index token usage from captured responses and imported requests."""
import argparse
import json
import re
import sqlite3
import time

from provider import parse_exchange

MAX_RESPONSE_BYTES = 32 * 1024 * 1024
BATCH_SIZE = 1000
MODEL_PATH = re.compile(r'^/v1/(?:responses|chat/completions|messages|completions)/?$')
GEMINI_PATH = re.compile(r'^/(?:v1|v1beta)/(?:[^/]+/)*models/[^/]+:(?:generateContent|streamGenerateContent)/?$')


def _number(usage, *names):
    for name in names:
        value = usage.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def normalize_usage(usage):
    """Return input, output, total and presence; absent counts remain NULL."""
    if not isinstance(usage, dict):
        return None, None, None, 0
    inp = _number(usage, 'input_tokens', 'prompt_tokens', 'promptTokenCount')
    out = _number(usage, 'output_tokens', 'completion_tokens', 'candidatesTokenCount')
    total = _number(usage, 'total_tokens', 'totalTokenCount')
    present = any(value is not None for value in (inp, out, total))
    if total is None and inp is not None and ('cache_creation_input_tokens' in usage or 'cache_read_input_tokens' in usage):
        inp += (_number(usage, 'cache_creation_input_tokens') or 0) + (_number(usage, 'cache_read_input_tokens') or 0)
    thoughts = _number(usage, 'thoughtsTokenCount')
    if out is not None and thoughts is not None and 'candidatesTokenCount' in usage:
        out += thoughts
    if total is None and inp is not None and out is not None:
        total = inp + out
    return inp, out, total, int(present)


def _table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _model_request(method, path):
    path = (path or '').split('?', 1)[0]
    return (method or '').upper() == 'POST' and bool(MODEL_PATH.fullmatch(path) or GEMINI_PATH.fullmatch(path))


def _archive_usage(content):
    try:
        obj = json.loads(content)
    except (ValueError, TypeError):
        obj = {}
    if not isinstance(obj, dict):
        obj = {}
    inp = _number(obj, 'input_tokens')
    out = _number(obj, 'output_tokens')
    return inp, out, inp + out if inp is not None and out is not None else None, int(inp is not None or out is not None)


def index_once(db_path):
    """Index one batch; return counts for live rows and archive rows examined.

    `archived_indexed` counts newly inserted unique archive entries.
    `archived_missing` counts unique archive ledger entries with no usage.
    `archive_more` indicates another immediate batch may be available.
    """
    db = sqlite3.connect(db_path, timeout=30)
    try:
        db.execute('PRAGMA busy_timeout=30000')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS model_token_usage(
                request_id INTEGER PRIMARY KEY, input_tokens INTEGER, output_tokens INTEGER,
                total_tokens INTEGER, model TEXT, has_usage INTEGER NOT NULL, finished REAL);
            CREATE TABLE IF NOT EXISTS archived_token_usage(
                content_sha256 TEXT PRIMARY KEY, input_tokens INTEGER, output_tokens INTEGER,
                total_tokens INTEGER, has_usage INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS usage_index_progress(name TEXT PRIMARY KEY, last_rowid INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS model_output_previews(
                request_id INTEGER PRIMARY KEY,model TEXT,prompt_preview TEXT,output_preview TEXT);
        ''')
        live = 0
        pending_usage=[]
        pending_previews=[]
        def flush():
            nonlocal live
            if not pending_usage:return
            before=db.total_changes
            db.executemany('INSERT OR IGNORE INTO model_token_usage VALUES(?,?,?,?,?,?,?)',pending_usage)
            live+=db.total_changes-before
            db.executemany('INSERT OR REPLACE INTO model_output_previews VALUES(?,?,?,?)',pending_previews)
            db.commit()
            pending_usage.clear();pending_previews.clear()
        if _table_exists(db, 'requests') and _table_exists(db, 'body_chunks'):
            # Read candidates without retaining a transaction while parsing response bodies.
            candidates = db.execute('''SELECT r.id,r.method,r.path,r.finished FROM requests r
                LEFT JOIN model_token_usage u ON u.request_id=r.id
                LEFT JOIN model_output_previews p ON p.request_id=r.id
                WHERE r.finished IS NOT NULL AND (u.request_id IS NULL OR p.request_id IS NULL)
                  AND upper(r.method)='POST' ORDER BY r.id''').fetchall()
            for request_id, method, path, finished in candidates:
                if not _model_request(method, path):
                    continue
                size = db.execute("SELECT coalesce(sum(length(data)),0) FROM body_chunks WHERE request_id=? AND direction='response'", (request_id,)).fetchone()[0]
                model = None
                prompt_preview=output_preview=''
                values = (None, None, None, 0)
                if size > MAX_RESPONSE_BYTES:
                    print(f'Usage index: response for request {request_id} exceeds 32 MiB; usage unavailable.', flush=True)
                else:
                    chunks = db.execute("SELECT data FROM body_chunks WHERE request_id=? AND direction='response' ORDER BY seq", (request_id,)).fetchall()
                    response = b''.join(row[0] for row in chunks)
                    if len(response) > MAX_RESPONSE_BYTES:
                        print(f'Usage index: response for request {request_id} exceeds 32 MiB; usage unavailable.', flush=True)
                    else:
                        from presentation import body
                        request,_ = body(db,request_id,'request')
                        parsed = parse_exchange(request, response)
                        values = normalize_usage(parsed['usage'])
                        model = parsed['model']
                        prompt_preview=next((x.get('text','') for x in reversed(parsed['prompt']) if x.get('role')=='user' and x.get('kind')=='text'),'')[:240]
                        output_preview=' '.join(x.get('text','') for x in parsed['output'] if x.get('kind') in ('text','refusal') or x.get('role')=='error')[:360]
                pending_usage.append((request_id,*values[:3],model,values[3],finished))
                pending_previews.append((request_id,model,prompt_preview,output_preview))
                if len(pending_usage)>=32:flush()
                if live >= BATCH_SIZE:
                    break
            flush()
        examined = 0
        archived_indexed = 0
        if _table_exists(db, 'archive_records'):
            columns = {row[1] for row in db.execute('PRAGMA table_info(archive_records)')}
            if {'source', 'content_json', 'content_sha256'} <= columns:
                progress = db.execute("SELECT last_rowid FROM usage_index_progress WHERE name='archive_records'").fetchone()
                last = progress[0] if progress else 0
                rows = db.execute('SELECT rowid,source,content_sha256,content_json FROM archive_records WHERE rowid>? ORDER BY rowid LIMIT ?', (last, BATCH_SIZE)).fetchall()
                records = [(sha, *_archive_usage(content)) for _, source, sha, content in rows if source == 'sqlite_requests' and sha is not None]
                if rows:
                    before = db.total_changes
                    db.executemany('INSERT OR IGNORE INTO archived_token_usage VALUES(?,?,?,?,?)', records)
                    archived_indexed = db.total_changes - before
                    db.execute("INSERT INTO usage_index_progress VALUES('archive_records',?) ON CONFLICT(name) DO UPDATE SET last_rowid=max(last_rowid,excluded.last_rowid)", (rows[-1][0],))
                    db.commit()
                examined = len(rows)
        missing = db.execute('SELECT count(*) FROM archived_token_usage WHERE has_usage=0').fetchone()[0]
        return {'indexed': live, 'archived_indexed': archived_indexed, 'archive_examined': examined, 'archived_missing': missing, 'archive_more': examined == BATCH_SIZE}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    while True:
        try:
            result = index_once(args.database)
            if args.once:
                print(json.dumps(result))
                return
            if result['archive_more'] or result['indexed'] == BATCH_SIZE:
                continue
        except sqlite3.OperationalError as exc:
            if args.once or not any(s in str(exc).lower() for s in ('locked', 'busy')):
                raise
            print(f'Usage index: database busy; retrying: {exc}', flush=True)
        time.sleep(10)


if __name__ == '__main__':
    main()
