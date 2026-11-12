"""Exercise search-index backfill and ongoing importer writes using synthetic data."""
import json
import sqlite3
import tempfile
from pathlib import Path
from migrate import migrate

with tempfile.TemporaryDirectory() as tmp:
    db=Path(tmp)/'test.sqlite3'
    c=sqlite3.connect(db)
    c.executescript('''CREATE TABLE requests(id INTEGER PRIMARY KEY,started REAL,method TEXT,status INTEGER);
    CREATE TABLE archive_records(host TEXT,source TEXT,path TEXT,record_id TEXT,timestamp TEXT,content_json TEXT,PRIMARY KEY(host,source,path,record_id));''')
    def record(i,content):
        return ('fixture','codex','/session',str(i),'1700000000',json.dumps({'text':content},ensure_ascii=False))
    c.executemany('INSERT INTO archive_records VALUES(?,?,?,?,?,?)',[record(1,'snow 雪 café'),record(2,'literal 50%_ [brackets] "quotes"'),record(3,'obsolete token')]);c.commit()
    backup=Path(tmp)/'backup.sqlite3'
    migrate(str(db),str(backup),batch_size=2)
    assert sqlite3.connect(backup).execute('SELECT count(*) FROM archive_records').fetchone()[0]==3
    def matches(term):
        phrase='"'+term.replace('"','""')+'"'
        return [r[0] for r in c.execute('SELECT record_id FROM archive_records WHERE rowid IN (SELECT rowid FROM archive_search WHERE archive_search MATCH ?)',(phrase,))]
    for term in ('snow','雪 c','café','50%_','[brackets]','quotes'):
        assert matches(term),term
    c.execute("UPDATE archive_records SET content_json=? WHERE record_id='3'",(json.dumps({'text':'updated token'}),));c.commit()
    assert not matches('obsolete') and matches('updated')==['3']
    c.execute('INSERT OR REPLACE INTO archive_records VALUES(?,?,?,?,?,?)',record(3,'replacement token'));c.commit()
    assert not matches('updated') and matches('replacement')==['3']
    c.execute('DELETE FROM archive_records');c.commit()
    assert not matches('replacement')
    assert c.execute('SELECT count(*) FROM archive_sessions').fetchone()[0]==0
    c.execute('INSERT INTO archive_records VALUES(?,?,?,?,?,?)',record(4,'newly imported'));c.commit()
    assert matches('newly')==['4']
    assert c.execute('SELECT count(*) FROM archive_sessions').fetchone()[0]==1
    migrate(str(db),batch_size=2)
    assert matches('newly')==['4']
    c.close()
    print('Migration checks passed: backup, resumable/idempotent backfill, Unicode, punctuation, updates, REPLACE, deletes, new imports, sessions.')
