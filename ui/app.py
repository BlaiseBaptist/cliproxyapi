#!/usr/bin/env python3
import base64, json, os, sqlite3, time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit, quote

DB = os.environ.get('TRAFFIC_DB', '/data/requests.sqlite3')
HERE = os.path.dirname(os.path.abspath(__file__))
LIMIT = 30

def connection():
    c = sqlite3.connect('file:' + quote(DB, safe='/') + '?mode=ro', uri=True, timeout=3)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA query_only=ON')
    deadline = time.monotonic() + 2
    c.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    return c

def archive_exists(c):
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_records'").fetchone())

def integer(s, default=0, maximum=1000000):
    try: return max(0, min(maximum, int(s)))
    except (TypeError, ValueError): return default

def value(q, k): return q.get(k, [''])[0][:500]

def where(q, archive=False):
    clauses, args = [], []
    for k in (('host','source','path','record_id') if archive else ('method','status','path')):
        v = value(q,k)
        if v:
            if k in ('path','record_id'):
                clauses.append(k + ' LIKE ? ESCAPE \'\\\'')
                args.append('%' + v.replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%')
            else:
                clauses.append(k + ' = ?'); args.append(v)
    if archive and value(q,'search'):
        clauses.append("content_json LIKE ? ESCAPE '\\'")
        args.append('%' + value(q,'search').replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%')
    return (' WHERE ' + ' AND '.join(clauses) if clauses else ''), args

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def send(self, code, data, kind='application/json; charset=utf-8', extra=None):
        if not isinstance(data, bytes): data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code); self.send_header('Content-Type',kind); self.send_header('Content-Length',str(len(data)))
        self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Cache-Control','no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        for k,v in (extra or {}).items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        u=urlsplit(self.path); q=parse_qs(u.query); p=u.path
        try:
            if p in ('/','/app.js','/style.css'):
                name={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}[p]
                with open(os.path.join(HERE,name),'rb') as f: data=f.read()
                return self.send(200,data,{'/':'text/html; charset=utf-8','/app.js':'text/javascript; charset=utf-8','/style.css':'text/css; charset=utf-8'}[p])
            with closing(connection()) as c:
                if p=='/api/health': return self.send(200,{'ok': bool(c.execute('SELECT 1').fetchone())})
                if p=='/api/summary':
                    live=c.execute('SELECT max(id) FROM requests').fetchone()[0]
                    return self.send(200,{'latest_request_id':live,'archive_available':archive_exists(c)})
                if p=='/api/requests':
                    w,a=where(q); offset=integer(value(q,'offset'))
                    rows=c.execute('SELECT id,started,finished,method,path,status,outcome FROM requests'+w+' ORDER BY id DESC LIMIT ? OFFSET ?',a+[LIMIT+1,offset]).fetchall()
                    return self.send(200,{'rows':[dict(x) for x in rows[:LIMIT]],'more':len(rows)>LIMIT})
                if p=='/api/request':
                    row=c.execute('SELECT * FROM requests WHERE id=?',(integer(value(q,'id')),)).fetchone()
                    if not row: return self.send(404,{'error':'Not found'})
                    return self.send(200,dict(row))
                if p=='/api/body':
                    rid=integer(value(q,'id')); direction=value(q,'direction')
                    if direction not in ('request','response'): return self.send(400,{'error':'Invalid direction'})
                    # Stream each stored chunk without assembling the full body in memory.
                    if value(q,'download')=='1':
                        c.set_progress_handler(None, 0)
                        # Keep the length and bytes in one snapshot while the
                        # recorder appends chunks to an active response.
                        c.execute('BEGIN')
                        size=c.execute('SELECT coalesce(sum(length(data)),0) FROM body_chunks WHERE request_id=? AND direction=?',(rid,direction)).fetchone()[0]
                        self.send_response(200); self.send_header('Content-Type','application/octet-stream'); self.send_header('Content-Disposition',f'attachment; filename="request-{rid}-{direction}.bin"'); self.send_header('Content-Length',str(size)); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Cache-Control','no-store'); self.end_headers()
                        for row in c.execute('SELECT data FROM body_chunks WHERE request_id=? AND direction=? ORDER BY seq',(rid,direction)): self.wfile.write(row[0])
                        return
                    offset=integer(value(q,'offset'),maximum=2147483647); size=65536
                    # substr works on each blob; only the requested window crosses the HTTP boundary.
                    rows=c.execute('SELECT seq,length(data) AS n FROM body_chunks WHERE request_id=? AND direction=? ORDER BY seq',(rid,direction)).fetchall()
                    total=sum(r['n'] for r in rows); pos=0; parts=[]
                    for row in rows:
                        n=row['n']; start=max(0,offset-pos); end=min(n,offset+size-pos)
                        if end>start: parts.append(c.execute('SELECT substr(data,?,?) FROM body_chunks WHERE request_id=? AND direction=? AND seq=?',(start+1,end-start,rid,direction,row['seq'])).fetchone()[0])
                        pos+=n
                        if pos>=offset+size: break
                    return self.send(200,{'offset':offset,'total':total,'data':base64.b64encode(b''.join(parts)).decode()})
                if p in ('/api/archive','/api/archive-record'):
                    if not archive_exists(c): return self.send(200,{'available':False,'rows':[]})
                    if p=='/api/archive-record':
                        keys=[value(q,k) for k in ('host','source','path','record_id')]
                        row=c.execute('SELECT * FROM archive_records WHERE host=? AND source=? AND path=? AND record_id=?',keys).fetchone()
                        return self.send(200,dict(row)) if row else self.send(404,{'error':'Not found'})
                    w,a=where(q,True); offset=integer(value(q,'offset'))
                    rows=c.execute('SELECT host,source,path,record_id,timestamp FROM archive_records'+w+' ORDER BY host,source,path,record_id LIMIT ? OFFSET ?',a+[LIMIT+1,offset]).fetchall()
                    return self.send(200,{'available':True,'rows':[dict(x) for x in rows[:LIMIT]],'more':len(rows)>LIMIT})
                self.send(404,{'error':'Not found'})
        except sqlite3.OperationalError as e:
            self.send(503,{'error':'Database busy or search timed out. Narrow your filters and try again.'})
        except (BrokenPipeError,ConnectionResetError): pass
        except Exception:
            self.send(500,{'error':'Unable to read data'})
    def do_POST(self): self.send(405,{'error':'Read-only service'})
    do_PUT=do_DELETE=do_PATCH=do_POST

if __name__=='__main__': ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','8320'))),Handler).serve_forever()
