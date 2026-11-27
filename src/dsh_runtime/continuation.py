"""Recover only a recorded native request using its original opaque item digests."""
import gzip
import json

def reinstate(value, reasoning):
    from .relay import clean
    if isinstance(value, list):
        return [reinstate(item, reasoning) for item in value]
    if not isinstance(value, dict):
        return value
    if any((key.endswith('_omitted') for key in value)):
        item = reasoning.get(value.get('id'))
        if value.get('type') != 'reasoning' or not item or clean(item) != value:
            raise ValueError('native reasoning item does not match the captured digest or metadata')
        return {k.removesuffix('_omitted'): item[k.removesuffix('_omitted')] for k in value}
    return {key: reinstate(child, reasoning) for key, child in value.items()}

def restore_pending(state, control):
    from .relay import read_json
    pending = read_json(control / 'pending.json')
    turn = pending['turn']
    wire = control / 'wire/dsh'
    raw = json.loads(gzip.decompress((wire / f'{turn:02}-in.json.gz').read_bytes()))
    body = json.loads(gzip.decompress((wire / f'{turn:02}-out.json.gz').read_bytes()))
    events = [json.loads(line) for line in (control / 'relay.jsonl').read_bytes().splitlines()]
    requests = [r for r in events if r['event'] == 'request']
    if (
        not requests
        or requests[-1]['turn'] != turn
        or requests[-1]['body_sha256'] != pending['body_sha256']
    ):
        raise ValueError('Pending turn does not match the last provider request')
    state.requests = turn
    state.tool_calls = sum((len(r.get('tool_calls', [])) for r in events
                           if r['event'] == 'response' and r.get('finish_reason', 'completed') == 'completed'))
    state.pending = {**pending, 'raw': raw, 'body': body, 'redacted': True}
    state.failure = pending.get('failure')
    state.request_snapshot = pending.get('work_snapshot')


def restore_delivery(state, control):
    """Restore a completed text checkpoint, without fabricating a tool receipt."""
    from .relay import read_json
    state.completion_checkpoint = read_json(control / 'continuation.json')
    events = [json.loads(line) for line in (control / 'relay.jsonl').read_bytes().splitlines()]
    requests = [row for row in events if row['event'] == 'request']
    if not requests:
        raise ValueError('Delivery continuation has no recorded provider request')
    state.requests = requests[-1]['turn']
    state.tool_calls = sum(len(row.get('tool_calls', [])) for row in events
                           if row['event'] == 'response' and row.get('finish_reason', 'completed') == 'completed')
    state.delivery_requested = True
    checkpoint = control / 'checkpoints' / f'{state.requests:03}.json'
    state.request_snapshot = read_json(checkpoint)['work_snapshot'] if checkpoint.is_file() else None

def verified_resume(pending, raw, marker, *, runtime_tool=None):
    from .relay import clean, digest, encoded
    prior = {**raw, 'input': raw.get('input', [])[:-1]}
    latest = raw.get('input', [])[-1:]
    if latest != [{'role': 'user', 'content': [{'type': 'input_text', 'text': marker}]}]:
        raise ValueError('Missing exact native session continuation marker')
    if runtime_tool is not None:
        name = runtime_tool['name']
        actual = [t for t in prior.get('tools', []) if t.get('name') == name]
        expected = [t for t in pending['raw'].get('tools', []) if t.get('name') == name]
        if actual != [runtime_tool]:
            raise ValueError('Runtime image reader differs from the registered overlay')
        if not expected:
            prior = {**prior, 'tools': [t for t in prior['tools'] if t.get('name') != name]}
    if pending.get('redacted'):
        if clean(prior) != pending['raw']:
            raise ValueError('Restored DSH transcript differs from the captured request')
        signatures = {item['id']: item for item in prior['input'] if item.get('type') == 'reasoning'}
        body = reinstate(pending['body'], signatures)
        if digest(encoded(body)) != pending['body_sha256']:
            raise ValueError('Restored upstream bytes differ from the recorded submission')
        return body
    if prior != pending['raw']:
        raise ValueError('Native DSH history changed on resume')
    return pending['body']
