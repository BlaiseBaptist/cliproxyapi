"""Read-only presentation of stored provider exchanges and conversation events."""
import json
from collections import OrderedDict
from threading import Lock
from provider import parse_exchange

MAX_BODY = 32 * 1024 * 1024
PREVIEWS = OrderedDict()
PREVIEW_LOCK = Lock()


def body(c, rid, direction):
    parts=[]; size=0; truncated=False
    for row in c.execute('SELECT data FROM body_chunks WHERE request_id=? AND direction=? ORDER BY seq',(rid,direction)):
        data=row[0]
        if size+len(data)>MAX_BODY:
            parts.append(data[:MAX_BODY-size]); truncated=True; break
        parts.append(data); size+=len(data)
    return b''.join(parts),truncated


def exchange(c, rid):
    request,req_cut=body(c,rid,'request')
    response,res_cut=body(c,rid,'response')
    result=parse_exchange(request,response)
    if req_cut or res_cut:
        result.setdefault('notices',[]).append('This exchange exceeds the 32 MiB display limit per direction. All original bytes remain stored.')
    return result


def preview(c, row):
    key=(row['id'],row['finished'])
    if row['finished'] is not None:
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='model_output_previews'").fetchone():
            stored=c.execute('SELECT model,prompt_preview,output_preview FROM model_output_previews WHERE request_id=?',(row['id'],)).fetchone()
            if stored:return dict(stored)
        with PREVIEW_LOCK:
            if key in PREVIEWS:
                PREVIEWS.move_to_end(key)
                return PREVIEWS[key]
    result=exchange(c,row['id'])
    prompts=result.get('prompt',[])
    prompt=next((x.get('text','') for x in reversed(prompts) if x.get('role')=='user' and x.get('kind')=='text'),'')
    output=' '.join(x.get('text','') for x in result.get('output',[]) if x.get('kind') in ('text','refusal','error') or x.get('role')=='error')
    data={'model':result.get('model'),'prompt_preview':prompt[:240],'output_preview':output[:360]}
    if row['finished'] is not None:
        with PREVIEW_LOCK:
            PREVIEWS[key]=data
            while len(PREVIEWS)>256: PREVIEWS.popitem(last=False)
    return data


def archive_messages(raw):
    """Unwrap imported session formats, exposing conversation content only."""
    try: obj=json.loads(raw) if isinstance(raw,str) else raw
    except (ValueError,TypeError): return {'model':None,'messages':[],'usage':{}}
    messages=[]; model=None; usage={}; visited=0
    def walk(v,role=None,depth=0):
        nonlocal model,usage,visited
        visited+=1
        if depth>16 or visited>10000:return
        if isinstance(v,str):
            try: walk(json.loads(v),role,depth+1)
            except (ValueError,TypeError):
                if role and v.strip():messages.append({'role':role,'kind':'text','text':v})
            return
        if isinstance(v,list):
            for item in v:walk(item,role,depth+1)
            return
        if not isinstance(v,dict):return
        model=v.get('model') or model
        if isinstance(v.get('usage'),dict):usage.update(v['usage'])
        kind=v.get('type','')
        r=v.get('role') or {'user_message':'user','assistant_message':'assistant','user':'user','assistant':'assistant','developer':'developer','system':'system','tool_result':'tool','function_call_output':'tool','function_call':'assistant','reasoning':'assistant'}.get(kind) or role
        if kind in ('function_call','tool_use'):
            args=v.get('arguments',v.get('input',''))
            messages.append({'role':r or 'assistant','kind':'tool_call','name':v.get('name','Tool call'),'text':args if isinstance(args,str) else json.dumps(args,ensure_ascii=False,indent=2)})
            return
        if kind in ('function_call_output','tool_result'):
            content=v.get('output',v.get('content',''))
            messages.append({'role':'tool','kind':'tool_result','text':content if isinstance(content,str) else json.dumps(content,ensure_ascii=False,indent=2)})
            return
        if kind in ('reasoning','thinking'):
            text=v.get('text',v.get('thinking',''))
            if text: messages.append({'role':'assistant','kind':'reasoning','text':text})
            for item in v.get('summary',[]):
                if isinstance(item,dict) and item.get('text'):messages.append({'role':'assistant','kind':'reasoning','text':item['text']})
            return
        if isinstance(v.get('text'),str) and (r or kind in ('text','input_text','output_text')):
            messages.append({'role':r or 'assistant','kind':'text','text':v['text']})
            return
        if isinstance(v.get('content'),(str,list)) and r:
            walk(v['content'],r,depth+1);return
        # Only unwrap known message envelopes; session metadata is not a message.
        for key in ('message','payload','payload_json','data'):
            if key in v:walk(v[key],r,depth+1)
    walk(obj)
    return {'model':model,'messages':messages,'usage':usage}
