"""Normalize stored provider exchanges into displayable conversation blocks."""

import json


_PARAMETERS = {
    'temperature', 'top_p', 'top_k', 'max_tokens', 'max_completion_tokens',
    'max_output_tokens', 'stop', 'stop_sequences', 'seed', 'frequency_penalty',
    'presence_penalty', 'response_format', 'tool_choice', 'tools', 'thinking',
    'reasoning', 'generationConfig', 'generation_config', 'safetySettings',
    'service_tier', 'parallel_tool_calls', 'modalities', 'audio', 'n',
}


def _readable(value):
    if isinstance(value, str):
        return value
    if value is None:
        return ''
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return '\n'.join(filter(None, (_readable(v) for v in value)))
    if isinstance(value, dict):
        return '; '.join(f'{k}: {_readable(v)}' for k, v in value.items()
                         if v is not None and _readable(v))
    return ''


def _args(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if value is not None else ''


def _block(blocks, role, kind, text, name=None):
    text = _readable(text)
    if not text and kind not in ('image', 'audio', 'file'):
        return
    block = {'role': role or 'assistant', 'kind': kind, 'text': text}
    if name:
        block['name'] = str(name)
    blocks.append(block)


def _parts(blocks, role, content, name=None):
    if isinstance(content, str):
        _block(blocks, role, 'text', content, name)
        return
    if isinstance(content, list):
        for part in content:
            _parts(blocks, role, part, name)
        return
    if not isinstance(content, dict):
        _block(blocks, role, 'unknown', content, name)
        return
    typ = content.get('type', '')
    if 'functionCall' in content or 'function_call' in content:
        f = content.get('functionCall', content.get('function_call')) or {}
        _block(blocks, role, 'tool_call', _args(f.get('args', f.get('arguments'))), f.get('name'))
    elif 'functionResponse' in content or 'function_response' in content:
        f = content.get('functionResponse', content.get('function_response')) or {}
        _block(blocks, role, 'tool_result', _readable(f.get('response')), f.get('name'))
    elif (typ in ('text', 'input_text', 'output_text') or ('text' in content and not typ)) and not content.get('thought'):
        _block(blocks, role, 'text', content.get('text', ''), name)
        for annotation in content.get('annotations') or []:
            _block(blocks, role, 'citation', _readable(annotation))
    elif typ in ('thinking', 'reasoning', 'reasoning_text', 'summary_text') or content.get('thought'):
        summary=content.get('summary', '')
        if isinstance(summary,list):
            summary='\n'.join(x.get('text','') for x in summary if isinstance(x,dict))
        _block(blocks, role, 'reasoning', content.get('thinking', content.get('text', summary)), name)
    elif typ in ('tool_use', 'function_call', 'tool_call', 'function'):
        f = content.get('function', content)
        _block(blocks, role, 'tool_call', _args(f.get('input', f.get('arguments', f.get('args')))), f.get('name'))
    elif typ in ('tool_result', 'function_call_output'):
        _block(blocks, role, 'tool_result', content.get('content', content.get('output', '')), content.get('name') or content.get('tool_use_id') or content.get('call_id'))
    elif typ in ('refusal', 'refusal_delta'):
        _block(blocks, role, 'refusal', content.get('refusal', content.get('text', '')), name)
    elif ('inlineData' in content or 'inline_data' in content) and str((content.get('inlineData') or content.get('inline_data') or {}).get('mimeType', (content.get('inlineData') or content.get('inline_data') or {}).get('mime_type', ''))).startswith('audio/'):
        _block(blocks, role, 'audio', 'Audio supplied', name)
    elif 'image' in typ or 'inlineData' in content or 'inline_data' in content:
        info = content.get('image_url', content.get('source', content.get('inlineData', content.get('inline_data', {}))))
        if isinstance(info, dict):
            info = info.get('url') or info.get('media_type') or info.get('mimeType') or info.get('mime_type') or 'Image supplied'
        _block(blocks, role, 'image', info or 'Image supplied', name)
    elif 'audio' in typ:
        _block(blocks, role, 'audio', content.get('transcript') or content.get('text') or 'Audio supplied', name)
    elif 'file' in typ or 'fileData' in content or 'file_data' in content:
        info = content.get('fileData', content.get('file_data', content))
        _block(blocks, role, 'file', info.get('file_uri', info.get('fileUri', info.get('filename', info.get('file_id', 'File supplied')))) if isinstance(info, dict) else info, name)
    elif typ == 'message' or 'parts' in content or 'content' in content:
        message_role = content.get('role', role)
        _parts(blocks, 'assistant' if message_role == 'model' else message_role, content.get('parts', content.get('content')), content.get('name', name))
        if content.get('reasoning_content') or content.get('reasoning'):
            _block(blocks, role, 'reasoning', content.get('reasoning_content') or content.get('reasoning'), name)
        if content.get('refusal'):
            _block(blocks, role, 'refusal', content['refusal'], name)
        for call in content.get('tool_calls') or []:
            _parts(blocks, role, call)
        if content.get('function_call'):
            _parts(blocks, role, {'type': 'function_call', **content['function_call']})
    else:
        # Exclude opaque binary and transport payloads while retaining descriptive fields.
        useful = {k: v for k, v in content.items() if k not in ('type', 'data', 'signature', 'encrypted_content')}
        _block(blocks, role, 'unknown', _readable(useful) or typ, name)


def _prompt(req):
    blocks = []
    for key in ('instructions', 'system', 'systemInstruction', 'system_instruction'):
        if key in req:
            _parts(blocks, 'system', req[key])
    source = req.get('input', req.get('messages', req.get('contents', req.get('prompt', []))))
    if isinstance(source, str):
        _block(blocks, 'user', 'text', source)
    else:
        for item in source if isinstance(source, list) else [source]:
            if isinstance(item, dict):
                role = item.get('role', 'user')
                role = 'assistant' if role == 'model' else role
                if role == 'tool':
                    _block(blocks, role, 'tool_result', item.get('content', ''), item.get('name') or item.get('tool_call_id'))
                else:
                    _parts(blocks, role, item)
            else:
                _parts(blocks, 'user', item)
    return blocks


def _snapshot(obj):
    blocks, usage, completion = [], {}, {}
    if not isinstance(obj, dict):
        return blocks, usage, completion, None
    usage = obj.get('usage') or obj.get('usageMetadata') or obj.get('usage_metadata') or {}
    if not isinstance(usage, dict):
        usage = {'value': usage}
    excluded = {'output', 'choices', 'content', 'candidates', 'usage', 'usageMetadata', 'usage_metadata', 'error', 'model', 'object', 'type', 'input', 'instructions', 'tools'}
    for k, v in obj.items():
        if k not in excluded:
            completion[k] = v
    if obj.get('error'):
        _block(blocks, 'error', 'unknown', _readable(obj['error']))
    if 'output' in obj:
        _parts(blocks, 'assistant', obj['output'])
    if 'content' in obj:
        _parts(blocks, obj.get('role', 'assistant'), obj['content'])
    for index, choice in enumerate(obj.get('choices') or []):
        # Name identifies alternatives without changing the block contract.
        label = f"Choice {choice.get('index', index) + 1}" if len(obj['choices']) > 1 else None
        message = choice.get('message', choice.get('delta', {}))
        _parts(blocks, 'assistant', message, label)
        if 'text' in choice:
            _block(blocks, 'assistant', 'text', choice['text'], label)
        if choice.get('finish_reason') is not None:
            completion.setdefault('choices', []).append({'index': choice.get('index', index), 'finish_reason': choice['finish_reason']})
    for index, candidate in enumerate(obj.get('candidates') or []):
        label = f"Choice {candidate.get('index', index) + 1}" if len(obj['candidates']) > 1 else None
        _parts(blocks, 'assistant', candidate.get('content', {}), label)
        meta = {k: v for k, v in candidate.items() if k != 'content'}
        if meta:
            completion.setdefault('candidates', []).append(meta)
    return blocks, usage, completion, obj.get('model') or obj.get('modelVersion')


def _events(raw, notices):
    events = []
    for event in raw.replace('\r\n', '\n').replace('\r', '\n').split('\n\n'):
        data = '\n'.join(line[5:].lstrip(' ') for line in event.split('\n') if line.startswith('data:'))
        if not data or data == '[DONE]':
            continue
        try:
            events.append(json.loads(data))
        except (ValueError, TypeError):
            notices.append('A streaming event could not be decoded.')
    return events


def _stream(events):
    # Ordered slots aggregate deltas without merging separate choices or content blocks.
    slots, usage, completion, model, final = {}, {}, {}, None, None
    def append(key, role, kind, text, name=None):
        if key not in slots:
            slots[key] = {'role': role, 'kind': kind, 'text': '', **({'name': str(name)} if name else {})}
        slots[key]['text'] += str(text or '')
    for event in events:
        if not isinstance(event, dict):
            continue
        typ = event.get('type', '')
        response = event.get('response')
        if typ in ('response.completed', 'response.incomplete', 'response.failed') and isinstance(response, dict):
            final = response
        if typ == 'message_start':
            msg = event.get('message') or {}
            _, u, c, m = _snapshot(msg)
            usage.update(u); completion.update(c); model = m or model
        if event.get('usage') or event.get('usageMetadata'):
            usage.update(event.get('usage') or event.get('usageMetadata'))
        model = event.get('model') or event.get('modelVersion') or model
        idx = event.get('output_index', 0)
        ci = event.get('content_index', event.get('index', 0))
        if typ in ('response.output_text.delta', 'response.reasoning_summary_text.delta', 'response.reasoning_text.delta', 'response.function_call_arguments.delta', 'response.refusal.delta'):
            kind = 'reasoning' if 'reasoning' in typ else 'tool_call' if 'function_call' in typ else 'refusal' if 'refusal' in typ else 'text'
            append(('response', idx, ci, kind), 'assistant', kind, event.get('delta'))
        if typ == 'response.output_item.added':
            item = event.get('item') or {}
            if item.get('type') == 'function_call':
                append(('response', idx, ci, 'tool_call'), 'assistant', 'tool_call', item.get('arguments', ''), item.get('name'))
        if typ == 'content_block_start':
            item = event.get('content_block') or {}
            kind = {'text': 'text', 'thinking': 'reasoning', 'tool_use': 'tool_call'}.get(item.get('type'), 'unknown')
            value = item.get('text', item.get('thinking', ''))
            if kind == 'tool_call' and item.get('input'):
                value = _args(item['input'])
            append(('anthropic', ci), 'assistant', kind, value, item.get('name'))
        if typ == 'content_block_delta':
            delta = event.get('delta') or {}
            kind = {'text_delta': 'text', 'thinking_delta': 'reasoning', 'input_json_delta': 'tool_call'}.get(delta.get('type'), 'unknown')
            append(('anthropic', ci), 'assistant', kind, delta.get('text', delta.get('thinking', delta.get('partial_json', ''))))
        if typ == 'message_delta':
            completion.update(event.get('delta') or {})
        for index, choice in enumerate(event.get('choices') or []):
            i = choice.get('index', index); delta = choice.get('delta') or {}
            label = f'Choice {i + 1}'
            for field, kind in (('content', 'text'), ('reasoning_content', 'reasoning'), ('reasoning', 'reasoning'), ('refusal', 'refusal')):
                if delta.get(field):
                    append(('chat', i, kind), 'assistant', kind, delta[field], label)
            for call in delta.get('tool_calls') or []:
                f = call.get('function') or {}; key = ('chat', i, 'tool', call.get('index', 0))
                append(key, 'assistant', 'tool_call', f.get('arguments'), f.get('name') or label)
                if f.get('name'):
                    slots[key]['name'] = f['name']
            if choice.get('finish_reason') is not None:
                completion.setdefault('choices', []).append({'index': i, 'finish_reason': choice['finish_reason']})
        for index, candidate in enumerate(event.get('candidates') or []):
            i = candidate.get('index', index)
            for part_index, part in enumerate((candidate.get('content') or {}).get('parts') or []):
                parsed = []; _parts(parsed, 'assistant', part, f'Choice {i + 1}')
                for b in parsed:
                    append(('gemini', i, part_index, b['kind']), b['role'], b['kind'], b['text'], b.get('name'))
            meta = {k: v for k, v in candidate.items() if k != 'content'}
            if meta:
                completion.setdefault('candidates', []).append(meta)
        if event.get('error'):
            append(('error',), 'error', 'unknown', _readable(event['error']))
    streamed = [b for b in slots.values() if b['text'] or b['kind'] in ('image', 'audio', 'file')]
    if final is not None:
        blocks, u, c, m = _snapshot(final)
        usage.update(u); completion.update(c); model = m or model
        # Final snapshots are authoritative; retain streamed kinds omitted entirely.
        kinds = {b['kind'] for b in blocks}
        blocks.extend(b for b in streamed if b['kind'] not in kinds)
    else:
        blocks = streamed
    return blocks, usage, completion, model


def parse_exchange(request_bytes: bytes, response_bytes: bytes) -> dict:
    notices = []
    def decode(raw, label):
        text = raw.decode('utf-8', errors='replace')
        if '\ufffd' in text:
            notices.append(f'{label} contained invalid UTF-8; replacement characters were used.')
        return text
    request = decode(request_bytes, 'Request')
    response = decode(response_bytes, 'Response')
    try:
        req = json.loads(request) if request.strip() else {}
        if not isinstance(req, dict):
            raise ValueError()
    except ValueError:
        req = {}
        notices.append('Request could not be decoded as a provider object.')
    if any(line.startswith('data:') for line in response.splitlines()):
        output, usage, completion, model = _stream(_events(response, notices))
    else:
        try:
            obj = json.loads(response) if response.strip() else {}
            output, usage, completion, model = _snapshot(obj)
        except ValueError:
            output, usage, completion, model = [], {}, {}, None
            notices.append('Response could not be decoded as a provider object.')
    parameters = {k: v for k, v in req.items() if k in _PARAMETERS}
    if parameters:
        completion['parameters'] = parameters
    return {'model': model or req.get('model'), 'prompt': _prompt(req), 'output': output,
            'usage': usage, 'completion': completion, 'notices': notices}
