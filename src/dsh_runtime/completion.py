"""Bind a native text-only exit to an explicit continuation of the same task."""
from __future__ import annotations

import copy

from .continuation import reinstate


DELIVERY_MARKER = (
    'Continue the current Sim-BIM task within its original scope. '
    'The required delivery has not yet been verified. Inspect the saved state, '
    'repair outstanding check errors, inspect the required actual overlays against the supplied inputs, '
    'and complete the whole-building handoff with finish_bim. '
    'Do not repeat completed writes or change the task scope. '
    'A text-only answer does not complete this task.'
)
_SCHEMA = 'dsh_delivery_checkpoint_v1'


def _short_hash(value):
    """pi-ai shortHash, including JavaScript's UTF-16 and 32-bit arithmetic."""
    units = value.encode('utf-16-le', errors='surrogatepass')
    h1, h2 = 0xDEADBEEF, 0x41C6CE57
    for offset in range(0, len(units), 2):
        char = int.from_bytes(units[offset:offset + 2], 'little')
        h1 = ((h1 ^ char) * 2654435761) & 0xFFFFFFFF
        h2 = ((h2 ^ char) * 1597334677) & 0xFFFFFFFF
    h1 = (((h1 ^ (h1 >> 16)) * 2246822507)
          ^ ((h2 ^ (h2 >> 13)) * 3266489909)) & 0xFFFFFFFF
    h2 = (((h2 ^ (h2 >> 16)) * 2246822507)
          ^ ((h1 ^ (h1 >> 13)) * 3266489909)) & 0xFFFFFFFF

    def base36(number):
        result = ''
        while number:
            number, digit = divmod(number, 36)
            result = '0123456789abcdefghijklmnopqrstuvwxyz'[digit] + result
        return result or '0'

    return base36(h2) + base36(h1)


def _native_output(response):
    """Project successful output as the installed pi-ai Responses adapter does.

    openai-responses-shared.js stores each output_item.done message as one text
    block, then replays its id/recognized phase with an empty annotations list.
    Reasoning signatures replay the complete original item without modification.
    """
    if response.get('status') != 'completed' or not isinstance(response.get('output'), list):
        raise ValueError('Delivery continuation requires a completed response')
    projected = []
    messages = 0
    for item in response['output']:
        if not isinstance(item, dict):
            raise ValueError('Invalid completed response item')
        kind = item.get('type')
        if kind == 'function_call':
            raise ValueError('Tool-call responses must use the ordinary tool continuation')
        if kind == 'reasoning':
            if not isinstance(item.get('id'), str) or not item['id']:
                raise ValueError('Reasoning item requires its native identity')
            projected.append(copy.deepcopy(item))
            continue
        if kind != 'message' or item.get('role') != 'assistant':
            raise ValueError('Unsupported completed response item')
        if not isinstance(item.get('id'), str) or not item['id']:
            raise ValueError('Assistant message requires its native identity')
        content = item.get('content')
        if not isinstance(content, list):
            raise ValueError('Invalid assistant message content')
        pieces = []
        for part in content:
            if not isinstance(part, dict) or part.get('type') not in {'output_text', 'refusal'}:
                raise ValueError('Unsupported assistant message content')
            text = part.get('text' if part['type'] == 'output_text' else 'refusal')
            if not isinstance(text, str):
                raise ValueError('Invalid assistant text or refusal')
            pieces.append(text)
        # pi-ai removes unpaired surrogates while preserving valid pairs.
        text = ''.join(pieces).encode('utf-16-le', errors='surrogatepass').decode('utf-16-le', errors='ignore')
        identity = item['id']
        if len(identity.encode('utf-16-le', errors='surrogatepass')) // 2 > 64:
            identity = 'msg_' + _short_hash(identity)
        message = {
            'type': 'message', 'role': 'assistant',
            'content': [{'type': 'output_text', 'text': text, 'annotations': []}],
            'status': 'completed', 'id': identity,
        }
        if item.get('phase') in {'commentary', 'final_answer'}:
            message['phase'] = item['phase']
        projected.append(message)
        messages += 1
    if not messages:
        raise ValueError('Delivery continuation requires an assistant text response')
    return projected


def delivery_checkpoint(raw, body, response):
    """Capture trusted request/response evidence; relay.save may seal its opaque data."""
    from .relay import digest, encoded

    _native_output(response)
    if not isinstance(raw.get('input'), list) or not isinstance(body.get('input'), list):
        raise ValueError('Delivery checkpoint requires stateless request input lists')
    return {
        'schema': _SCHEMA,
        **{name: copy.deepcopy(value) for name, value in
           (('raw', raw), ('body', body), ('response', response))},
        **{name + '_sha256': digest(encoded(value)) for name, value in
           (('raw', raw), ('body', body), ('response', response))},
    }


def verified_delivery_continuation(checkpoint, raw, marker=DELIVERY_MARKER):
    """Accept only one recorded runtime instruction after the exact native history.

    The outgoing request is reconstructed entirely from captured evidence. The
    incoming transcript supplies only authenticated opaque replay items, never
    new scope, assistant claims, or fabricated tool receipts.
    """
    from .relay import clean, digest, encoded

    if checkpoint.get('schema') != _SCHEMA:
        raise ValueError('Invalid delivery checkpoint schema')
    items = raw.get('input')
    marker_item = {'role': 'user', 'content': [{'type': 'input_text', 'text': marker}]}
    if not isinstance(items, list) or items[-1:] != [marker_item]:
        raise ValueError('Missing exact native delivery continuation marker')
    reasoning = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('Invalid native transcript item')
        if item.get('type') != 'reasoning':
            continue
        identity = item.get('id')
        if not isinstance(identity, str) or not identity:
            raise ValueError('Reasoning item requires its native identity')
        if identity in reasoning and reasoning[identity] != item:
            raise ValueError('Conflicting native reasoning identity')
        reasoning[identity] = item
    restored = {}
    for name in ('raw', 'body', 'response'):
        restored[name] = reinstate(clean(checkpoint[name]), reasoning)
        if digest(encoded(restored[name])) != checkpoint[name + '_sha256']:
            raise ValueError('Restored delivery checkpoint differs from its recorded digest')
    prior, body, response = (restored[name] for name in ('raw', 'body', 'response'))
    expected = {**prior, 'input': prior['input'] + _native_output(response) + [marker_item]}
    if raw != expected:
        raise ValueError('Native delivery continuation differs from the captured transcript or parameters')
    return {**body, 'input': body['input'] + response['output'] + [marker_item],
            'tool_choice': 'required'}
