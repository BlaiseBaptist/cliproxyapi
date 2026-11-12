#!/usr/bin/env python3
"""Add derived browsing/search indexes; original records and body bytes are untouched.

Requires SQLite >=3.43 with FTS5 and the trigram tokenizer. Safe to resume after
interruption. Run outside the read-only viewer, using the database owner's UID.
"""
import argparse
import os
import sqlite3
from contextlib import closing


def migrate(path, backup=None, batch_size=500):
    with closing(sqlite3.connect(path, timeout=30)) as c:
        c.execute('PRAGMA busy_timeout=30000')
        # Probe capabilities before making any schema changes.
        c.execute("CREATE VIRTUAL TABLE temp.fts_probe USING fts5(text, tokenize='trigram', content='', contentless_delete=1)")
        c.execute('DROP TABLE temp.fts_probe')
        if backup:
            if os.path.exists(backup):
                raise FileExistsError('Backup already exists; choose a new path')
            fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            # Pin a WAL snapshot so continuous body-chunk writes cannot keep
            # restarting the backup from its first page.
            c.execute('BEGIN')
            c.execute('SELECT count(*) FROM sqlite_master').fetchone()
            try:
                with closing(sqlite3.connect(backup)) as target:
                    c.backup(target, pages=4096)
            finally:
                c.rollback()
            print('Online backup complete', flush=True)
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='archive_records'").fetchone():
            raise RuntimeError('Import archive_records before running this migration')
        with c:
            c.execute('CREATE INDEX IF NOT EXISTS requests_status_id ON requests(status,id)')
            c.execute('CREATE INDEX IF NOT EXISTS requests_method_id ON requests(method,id)')
            c.execute('CREATE INDEX IF NOT EXISTS requests_started ON requests(started)')
            c.execute('CREATE TABLE IF NOT EXISTS viewer_migrations(name TEXT PRIMARY KEY, last_rowid INTEGER NOT NULL DEFAULT 0, ready INTEGER NOT NULL DEFAULT 0)')
            c.execute("INSERT OR IGNORE INTO viewer_migrations(name) VALUES('archive_search_v1')")
            c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS archive_search USING fts5(content_json, tokenize='trigram', content='', contentless_delete=1)")
            c.execute('CREATE TABLE IF NOT EXISTS archive_sessions(host TEXT NOT NULL, source TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY(host,source,path)) WITHOUT ROWID')
            c.execute('CREATE INDEX IF NOT EXISTS archive_session_order ON archive_records(host,source,path)')
            # Session keys form a small directory; records remain the source of truth.
            c.execute('INSERT OR IGNORE INTO archive_sessions SELECT host,source,path FROM archive_records GROUP BY host,source,path')
            for name in ('archive_search_insert','archive_search_delete','archive_search_update'):
                c.execute('DROP TRIGGER IF EXISTS '+name)
            c.execute('''CREATE TRIGGER archive_search_insert AFTER INSERT ON archive_records BEGIN
                INSERT OR REPLACE INTO archive_search(rowid,content_json) VALUES(new.rowid,new.content_json);
                INSERT OR IGNORE INTO archive_sessions VALUES(new.host,new.source,new.path);
            END''')
            c.execute('''CREATE TRIGGER archive_search_delete AFTER DELETE ON archive_records BEGIN
                DELETE FROM archive_search WHERE rowid=old.rowid;
                DELETE FROM archive_sessions WHERE host=old.host AND source=old.source AND path=old.path
                  AND NOT EXISTS(SELECT 1 FROM archive_records WHERE host=old.host AND source=old.source AND path=old.path);
            END''')
            c.execute('''CREATE TRIGGER archive_search_update AFTER UPDATE ON archive_records BEGIN
                DELETE FROM archive_search WHERE rowid=old.rowid;
                INSERT OR REPLACE INTO archive_search(rowid,content_json) VALUES(new.rowid,new.content_json);
                INSERT OR IGNORE INTO archive_sessions VALUES(new.host,new.source,new.path);
                DELETE FROM archive_sessions WHERE host=old.host AND source=old.source AND path=old.path
                  AND NOT EXISTS(SELECT 1 FROM archive_records WHERE host=old.host AND source=old.source AND path=old.path);
            END''')
        last, ready = c.execute("SELECT last_rowid,ready FROM viewer_migrations WHERE name='archive_search_v1'").fetchone()
        if ready:
            print('Browsing and search indexes already current', flush=True)
            return
        while True:
            # Short write transactions keep backfill consistent with concurrent imports.
            c.execute('BEGIN IMMEDIATE')
            try:
                rows = c.execute('SELECT rowid,content_json FROM archive_records WHERE rowid>? ORDER BY rowid LIMIT ?', (last,batch_size)).fetchall()
                if not rows:
                    c.execute("UPDATE viewer_migrations SET ready=1 WHERE name='archive_search_v1'")
                    c.commit()
                    break
                c.executemany('INSERT OR REPLACE INTO archive_search(rowid,content_json) VALUES(?,?)',rows)
                last=rows[-1][0]
                c.execute("UPDATE viewer_migrations SET last_rowid=? WHERE name='archive_search_v1'",(last,))
                c.commit()
            except BaseException:
                c.rollback()
                raise
            if last % (batch_size*20)==0:
                print(f'Indexed through row {last}',flush=True)
        print('Browsing and search indexes ready',flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database')
    parser.add_argument('--backup',help='Create an online SQLite backup at this new path before migrating')
    args=parser.parse_args()
    migrate(args.database,args.backup)
