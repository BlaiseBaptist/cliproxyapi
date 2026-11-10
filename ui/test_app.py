import json, os, socket, sqlite3, subprocess, tempfile, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

root=Path(__file__).resolve().parent
with tempfile.TemporaryDirectory() as tmp:
    db=Path(tmp)/'fixture.sqlite3'; c=sqlite3.connect(db)
    c.executescript('''CREATE TABLE requests(id INTEGER PRIMARY KEY,started REAL,finished REAL,method TEXT,path TEXT,request_headers TEXT,status INTEGER,response_headers TEXT,outcome TEXT,error TEXT); CREATE TABLE body_chunks(request_id INTEGER,direction TEXT,seq INTEGER,data BLOB,PRIMARY KEY(request_id,direction,seq));''')
    c.execute('INSERT INTO requests VALUES(1,1,2,?,?,?,?,?,?,?)',('POST','/fixture','{}',200,'{}','ok',None))
    expected=('hello 雪 <script>alert(1)</script> '+ 'x'*70000).encode()+b'\x00\xff'
    c.executemany('INSERT INTO body_chunks VALUES(?,?,?,?)',[(1,'request',1,expected[40000:]),(1,'request',0,expected[:40000]),(1,'response',0,b'data: one\n\n'),(1,'response',1,b'data: two\n\n')]); c.commit()
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    proc=subprocess.Popen(['python3',str(root/'app.py')],env={**os.environ,'TRAFFIC_DB':str(db),'PORT':str(port)},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    base=f'http://127.0.0.1:{port}'
    def get(path):
        with urllib.request.urlopen(base+path,timeout=5) as r:return r.read()
    def js(path):return json.loads(get(path))
    try:
        for _ in range(50):
            try: js('/api/health');break
            except OSError: time.sleep(.05)
        assert js('/api/archive')['available'] is False
        assert js('/api/requests?method=POST&status=200')['rows'][0]['id']==1
        assert get('/api/body?id=1&direction=request&download=1')==expected
        assert get('/api/body?id=1&direction=response&download=1')==b'data: one\n\ndata: two\n\n'
        assert js('/api/body?id=1&direction=request&offset=65536')['total']==len(expected)
        c.execute('CREATE TABLE archive_records(host TEXT,source TEXT,path TEXT,record_id TEXT,timestamp TEXT,content_json TEXT,content_sha256 TEXT,imported_at TEXT,PRIMARY KEY(host,source,path,record_id))')
        content=json.dumps({'role':'user','content':'snow 雪 <img src=x onerror=alert(1)>','tool_result':'done'},ensure_ascii=False)
        c.execute('INSERT INTO archive_records VALUES(?,?,?,?,?,?,?,?)',('fixture','codex_jsonl','/session/a','abc','1700000000',content,'hash','today'));c.commit()
        assert js('/api/archive?host=fixture&source=codex_jsonl&search=snow')['rows'][0]['record_id']=='abc'
        q=urllib.parse.urlencode({'host':'fixture','source':'codex_jsonl','path':'/session/a','record_id':'abc'})
        assert json.loads(js('/api/archive-record?'+q)['content_json'])['content'].startswith('snow 雪')
        try: urllib.request.urlopen(urllib.request.Request(base+'/api/requests',data=b'{}',method='POST'))
        except urllib.error.HTTPError as e: assert e.code==405
        else: raise AssertionError('write accepted')
        assert b'textContent' in get('/app.js') and b'innerHTML' not in get('/app.js')
        assert c.execute('SELECT count(*) FROM requests').fetchone()[0]==1
        print('Fixture checks passed: missing archive, filters, streamed/binary bodies, Unicode, HTML, archive content, read-only methods.')
    finally:
        proc.terminate();proc.wait();c.close()
