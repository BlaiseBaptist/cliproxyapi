#!/usr/bin/env python3
import base64, json, os, sqlite3, time
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit, quote
from presentation import exchange, preview, archive_messages
from index_usage import _model_request

DB = os.environ.get('TRAFFIC_DB', '/data/requests.sqlite3')
HERE = os.path.dirname(os.path.abspath(__file__))
LIMIT = 30
SUMMARY_LOCK = threading.Lock()
SUMMARY_CACHE = (0, None)
# Session/control events stay stored; the library browses conversation content.
ARCHIVE_CONTENT = """source NOT IN ('proxy_error_log','sqlite_requests') AND
 (source NOT IN ('claude_jsonl','codex_jsonl') OR
 json_extract(CASE WHEN json_valid(content_json) THEN content_json ELSE '{}' END,'$.role') IS NOT NULL OR
 json_extract(CASE WHEN json_valid(content_json) THEN content_json ELSE '{}' END,'$.type') IN ('response_item','assistant','user'))"""

def connection():
    c = sqlite3.connect('file:' + quote(DB, safe='/') + '?mode=ro', uri=True, timeout=3)
    c.row_factory = sqlite3.Row
    c.create_function('is_model_request',2,_model_request,deterministic=True)
    c.execute('PRAGMA query_only=ON')
    deadline = time.monotonic() + 2
    c.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    return c

def archive_exists(c):
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_records'").fetchone())

def indexed(c):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='viewer_migrations'").fetchone():
        return False
    row=c.execute("SELECT ready FROM viewer_migrations WHERE name='archive_search_v1'").fetchone()
    return bool(row and row[0])

def summary(c):
    global SUMMARY_CACHE
    with SUMMARY_LOCK:
        now=time.monotonic()
        if SUMMARY_CACHE[1] is not None and now-SUMMARY_CACHE[0]<60:
            return SUMMARY_CACHE[1]
        available=archive_exists(c)
        data={'latest_request_id':c.execute('SELECT max(id) FROM requests').fetchone()[0],
              'live_count':c.execute('SELECT count(*) FROM requests').fetchone()[0],
              'archive_available':available, 'archive_count':0, 'archive_sessions':0,
              'archive_hosts':[], 'archive_sources':[], 'search_index':indexed(c)}
        data['model_count']=c.execute('SELECT count(*) FROM requests WHERE is_model_request(method,path)').fetchone()[0]
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='model_token_usage'").fetchone():
            data['tokens']=dict(c.execute('SELECT coalesce(sum(input_tokens),0) AS input,coalesce(sum(output_tokens),0) AS output,coalesce(sum(total_tokens),0) AS total,count(*) AS responses,coalesce(sum(has_usage),0) AS with_usage FROM model_token_usage').fetchone())
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='archived_token_usage'").fetchone():
            data['archive_tokens']=dict(c.execute('SELECT coalesce(sum(input_tokens),0) AS input,coalesce(sum(output_tokens),0) AS output,coalesce(sum(total_tokens),0) AS total,count(*) AS responses,coalesce(sum(has_usage),0) AS with_usage FROM archived_token_usage').fetchone())
        if available:
            data['archive_count']=c.execute('SELECT count(*) FROM archive_records').fetchone()[0]
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='archive_sessions'").fetchone():
                data['archive_sessions']=c.execute('SELECT count(*) FROM archive_sessions').fetchone()[0]
                data['archive_hosts']=[r[0] for r in c.execute('SELECT DISTINCT host FROM archive_sessions ORDER BY host LIMIT 200')]
                data['archive_sources']=[r[0] for r in c.execute('SELECT DISTINCT source FROM archive_sessions ORDER BY source LIMIT 200')]
        SUMMARY_CACHE=(now,data)
        return data

def integer(s, default=0, maximum=1000000):
    try: return max(0, min(maximum, int(s)))
    except (TypeError, ValueError): return default

def value(q, k): return q.get(k, [''])[0][:500]

def where(q, archive=False, search_index=False):
    clauses, args = [], []
    for k in (('host','source','path','record_id') if archive else ('method','status','path','outcome')):
        v = value(q,k)
        if v:
            if k in ('path','record_id'):
                clauses.append(k + ' LIKE ? ESCAPE \'\\\'')
                args.append('%' + v.replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%')
            else:
                clauses.append(k + ' = ?'); args.append(v)
    if archive and value(q,'search'):
        if search_index and len(value(q,'search'))>=3:
            # Quote as one literal trigram phrase: user input is never FTS syntax.
            clauses.append('rowid IN (SELECT rowid FROM archive_search WHERE archive_search MATCH ?)')
            args.append('"'+value(q,'search').replace('"','""')+'"')
        clauses.append("content_json LIKE ? ESCAPE '\\'")
        args.append('%' + value(q,'search').replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%')
    if not archive and value(q,'since'):
        try:
            since=float(value(q,'since'))
            if not 0<=since<1e12: raise ValueError()
        except ValueError:
            raise ValueError('Since must be a Unix timestamp in seconds')
        clauses.append('started >= ?'); args.append(since)
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
                    return self.send(200,summary(c))
                if p in ('/api/requests','/api/outputs'):
                    w,a=where(q); offset=integer(value(q,'offset'))
                    if p=='/api/outputs':
                        w+=(' AND ' if w else ' WHERE ')+'is_model_request(method,path)'
                    order='ASC' if value(q,'sort')=='oldest' else 'DESC'
                    rows=c.execute('SELECT id,started,finished,method,path,status,outcome,round((finished-started)*1000) AS duration_ms FROM requests'+w+' ORDER BY id '+order+' LIMIT ? OFFSET ?',a+[LIMIT+1,offset]).fetchall()
                    result=[dict(x) for x in rows[:LIMIT]]
                    if p=='/api/outputs':
                        for row in result: row.update(preview(c,row))
                    return self.send(200,{'rows':result,'more':len(rows)>LIMIT})
                if p=='/api/exchange':
                    rid=integer(value(q,'id'))
                    row=c.execute('SELECT id,started,finished,status,outcome,round((finished-started)*1000) AS duration_ms FROM requests WHERE id=?',(rid,)).fetchone()
                    if not row:return self.send(404,{'error':'Not found'})
                    c.execute('BEGIN')
                    return self.send(200,{**dict(row),**exchange(c,rid)})
                if p=='/api/request':
                    row=c.execute('SELECT *,round((finished-started)*1000) AS duration_ms FROM requests WHERE id=?',(integer(value(q,'id')),)).fetchone()
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
                if p in ('/api/archive','/api/archive-record','/api/archive-session'):
                    if not archive_exists(c): return self.send(200,{'available':False,'rows':[]})
                    if p=='/api/archive-record':
                        keys=[value(q,k) for k in ('host','source','path','record_id')]
                        row=c.execute('SELECT * FROM archive_records WHERE host=? AND source=? AND path=? AND record_id=?',keys).fetchone()
                        return self.send(200,{**dict(row),**archive_messages(row['content_json'])}) if row else self.send(404,{'error':'Not found'})
                    if p=='/api/archive-session':
                        keys=[value(q,k) for k in ('host','source','path')]
                        after=integer(value(q,'after'),maximum=9223372036854775807)
                        rows=c.execute('SELECT record_id,timestamp,content_json,rowid AS row_cursor FROM archive_records WHERE host=? AND source=? AND path=? AND rowid>? AND '+ARCHIVE_CONTENT+' ORDER BY rowid LIMIT ?',keys+[after,LIMIT+1]).fetchall()
                        page=rows[:LIMIT]
                        return self.send(200,{'available':True,'rows':[{**dict(x),**archive_messages(x['content_json'])} for x in page],'more':len(rows)>LIMIT,'next_cursor':page[-1]['row_cursor'] if page else after})
                    w,a=where(q,True,indexed(c)); offset=integer(value(q,'offset'))
                    w+=(' AND ' if w else ' WHERE ')+ARCHIVE_CONTENT
                    order='ASC' if value(q,'sort')=='oldest' else 'DESC'
                    rows=c.execute('SELECT host,source,path,record_id,timestamp,content_json FROM archive_records'+w+' ORDER BY rowid '+order+' LIMIT ? OFFSET ?',a+[LIMIT+1,offset]).fetchall()
                    result=[]
                    for row in rows[:LIMIT]:
                        shown={k:row[k] for k in ('host','source','path','record_id','timestamp')}
                        parsed=archive_messages(row['content_json'])
                        shown['model']=parsed['model']
                        shown['preview']=' '.join(x['text'] for x in parsed['messages'])[:240]
                        result.append(shown)
                    return self.send(200,{'available':True,'rows':result,'more':len(rows)>LIMIT})
                self.send(404,{'error':'Not found'})
        except ValueError as e:
            self.send(400,{'error':str(e)})
        except sqlite3.OperationalError as e:
            self.send(503,{'error':'Database busy or search timed out. Narrow your filters and try again.'})
        except (BrokenPipeError,ConnectionResetError): pass
        except Exception:
            self.send(500,{'error':'Unable to read data'})
    def do_POST(self): self.send(405,{'error':'Read-only service'})
    do_PUT=do_DELETE=do_PATCH=do_POST

if __name__=='__main__': ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','8320'))),Handler).serve_forever()
