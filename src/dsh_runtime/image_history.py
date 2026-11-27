"""Image-only Responses projection and a task-local, read-only MCP archive.

The native DSH transcript stays complete. Only old tool image blocks in the
provider request become exact references; text, calls, and opaque items survive.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import hashlib
import json
from pathlib import Path
import time

from src.agent_runtime.adapter import decode_image_url
from src.agent_runtime.context import ContextPolicy, context_image_tool_spec

NAME = 'read_context_image'
PREFIX = 'mcp__sim_bim_context__'
DIRECT_READS = {'view_image', 'mcp__sim_bim__view_image'}
COMPARISON_READS = DIRECT_READS | {NAME, PREFIX + NAME}
# Persisted archive/index protocol, including old recoverable references and resume manifests.
REVISION = 'dsh-image-history-v1'
PROJECTION_REVISION = 'dsh-image-history-v3'


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_bytes(encoded(value))
    temp.replace(path)


def chat_tool():
    spec = context_image_tool_spec()
    return {'type': 'function', 'function': {
        'name': NAME, 'description': spec['description'], 'parameters': spec['inputSchema'],
    }}


def native_tool():
    spec = context_image_tool_spec()
    return {'type': 'function', 'name': PREFIX + NAME,
            'description': spec['description'], 'parameters': spec['inputSchema']}


class ImageHistory:
    def __init__(self, directory: Path, batches: int = 3, *, max_images: int = 16):
        self.directory = Path(directory)
        policy = ContextPolicy(image_history_batches=batches, max_images=max_images)
        self.batches = policy.image_history_batches
        self.max_images = policy.max_images
        self.max_image_bytes = policy.max_image_bytes

    def records(self):
        path = self.directory / 'index.json'
        return json.loads(path.read_bytes()) if path.exists() else []

    @staticmethod
    def reference(record):
        return {'type': 'input_text', 'text': encoded({
            'archived_image': {
                'projection': REVISION,
                'view_id': record['view_id'], 'sha256': record['sha256'],
                'source_tool': record['source_tool'], 'source_call_id': record['call_id'],
                'read': {'tool': NAME, 'arguments': {
                    'view_id': record['view_id'], 'sha256': record['sha256'],
                }},
                'note': 'Exact earlier image saved. Reopen it when needed; this reference does not establish current artifact validity.',
            },
        }).decode()}

    def _register(self, records, known, block, call, block_index):
        data, mime = decode_image_url(block['image_url'])
        image_sha, block_sha = sha(data), sha(encoded(block))
        view_id = 'dsh-image-' + sha(encoded([call['call_id'], block_index]))[:24]
        record = {
            'view_id': view_id, 'sha256': image_sha, 'bytes': len(data), 'mime_type': mime,
            'block_sha256': block_sha, 'call_id': call['call_id'], 'block_index': block_index,
            'source_tool': call['name'], 'source_arguments': call['arguments'],
        }
        if view_id in known:
            prior = known[view_id]
            if {k: v for k, v in prior.items() if k != 'index'} != record:
                raise ValueError('An archived image identity changed')
            return prior
        record['index'] = len(records)
        for subdir, key, content in (
            ('media', image_sha, data), ('blocks', block_sha, encoded(block)),
        ):
            path = self.directory / subdir / key
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                if sha(path.read_bytes()) != key:
                    raise ValueError('Archived image blob hash mismatch')
            else:
                path.write_bytes(content)
        records.append(record)
        known[view_id] = record
        return record

    def _comparison(self, body, recent, reads):
        """Keep the newest occurrence of each exact read across a consecutive comparison."""
        if not reads:
            return recent
        # User images and recent non-comparison results keep their original contracts.
        # Allocate only the remaining image slots/bytes to comparisons, including the latest batch.
        kept = dict(recent)
        latest = {}
        for record in reversed(reads):
            kept.pop(record['view_id'], None)
            latest.setdefault(record['sha256'], record)
        count, size = len(kept), sum(r['bytes'] for r in kept.values())
        for item in body['input']:
            content = item.get('content')
            for block in content if isinstance(content, list) else ():
                if block.get('type') != 'input_image':
                    continue
                count += 1
                url = block.get('image_url', '')
                if not isinstance(url, str) or not url.startswith('data:') or ';base64,' not in url:
                    return kept  # The protected image has unknown size; reserve no comparison bytes.
                length = len(url) - url.index(',') - 1
                size += length // 4 * 3 - int(url.endswith('=')) - int(url.endswith('=='))
        for record in latest.values():
            if count >= self.max_images:
                break
            if size + record['bytes'] > self.max_image_bytes:
                continue
            kept[record['view_id']] = record
            count += 1
            size += record['bytes']
        return kept

    @staticmethod
    def _record_read(call, returned, known, reads):
        """Append verified pixels, or accept a directory lookup without pixels."""
        if call['name'] in DIRECT_READS:
            if len(returned) == 1:
                # A crop or normalized preview is identified by its actual returned bytes.
                reads.append(returned[0])
                return True
            return False
        arguments = call['arguments']
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = None
        if isinstance(arguments, dict):
            action = arguments.get('action', 'read')
            if action == 'list' and not returned:
                return True
            view_id = arguments.get('view_id')
            source = known.get(view_id) if isinstance(view_id, str) else None
            if (action == 'read' and len(returned) == 1 and source
                    and arguments.get('sha256') == source['sha256'] == returned[0]['sha256']):
                reads.append(returned[0])
                return True
        return False

    def project(self, body):
        """Keep recent batches and bounded, consecutive exact reads presented for comparison."""
        projected = copy.deepcopy(body)
        records = self.records()
        known = {r['view_id']: r for r in records}
        calls, images, reads = {}, [], []
        pending = set()
        read_batch = False
        batch = -1
        outputs_seen = True
        for item_index, item in enumerate(body['input']):
            if item.get('type') == 'function_call':
                if outputs_seen:
                    if pending:  # A new batch started before every prior reply arrived.
                        reads.clear()
                    pending.clear()
                    batch += 1
                    outputs_seen = False
                    read_batch = True
                pending.add(item['call_id'])
                if item['name'] not in COMPARISON_READS:
                    read_batch = False
                    reads.clear()
                calls[item['call_id']] = (batch, item)
            elif item.get('type') == 'function_call_output':
                outputs_seen = True
                output = item.get('output')
                returned = []
                for block_index, block in enumerate(output if isinstance(output, list) else ()):
                    if block.get('type') != 'input_image':
                        continue
                    if item['call_id'] not in calls:
                        raise ValueError('Cannot archive an image without its producer call')
                    group, call = calls[item['call_id']]
                    record = self._register(records, known, block, call, block_index)
                    images.append((group, item_index, block_index, record))
                    returned.append(record)
                if item['call_id'] in pending:
                    pending.remove(item['call_id'])
                    if read_batch and not self._record_read(calls[item['call_id']][1], returned, known, reads):
                        read_batch = False
                        reads.clear()
            elif item.get('role') in ('user', 'assistant', 'developer', 'system'):
                reads.clear()
                pending.clear()
                read_batch = False
                outputs_seen = True
        if pending:
            reads.clear()
        write_json(self.directory / 'index.json', records)
        groups = sorted({group for group, *_ in images})
        recent_groups = set(groups[-self.batches:])
        recent = {r['view_id']: r for group, _, _, r in images if group in recent_groups}
        kept = self._comparison(body, recent, reads)
        removed = []
        for group, item_index, block_index, record in images:
            if record['view_id'] not in kept:
                projected['input'][item_index]['output'][block_index] = self.reference(record)
                removed.append(record['view_id'])
        evidence = {
            'revision': PROJECTION_REVISION, 'archive_revision': REVISION, 'image_history_batches': self.batches,
            'full_body_sha256': sha(encoded(body)), 'full_body_bytes': len(encoded(body)),
            'projected_body_sha256': sha(encoded(projected)),
            'projected_body_bytes': len(encoded(projected)),
            'tool_images_before': len(images), 'tool_images_after': len(images) - len(removed),
            'retained_image_batches': sorted({g for g, _, _, r in images if r['view_id'] in kept}),
            'comparison_sha256': sorted({r['sha256'] for r in reads if r['view_id'] in kept}),
            'archived_view_ids': removed,
            'initial_user_images': 'unchanged', 'non_image_content': 'unchanged',
        }
        return projected, evidence

    def restore_full(self, body):
        """Undo only exact generated references at their registered call/block locations."""
        full = copy.deepcopy(body)
        known = {(r['call_id'], r['block_index']): r for r in self.records()}
        for item in full['input']:
            if item.get('type') != 'function_call_output' or not isinstance(item.get('output'), list):
                continue
            for index, block in enumerate(item['output']):
                record = known.get((item['call_id'], index))
                if record is None or block != self.reference(record):
                    continue
                data = (self.directory / 'blocks' / record['block_sha256']).read_bytes()
                if sha(data) != record['block_sha256']:
                    raise ValueError('Archived original block hash mismatch')
                item['output'][index] = json.loads(data)
        return full

    def call(self, arguments):
        """Read registered identities only; never accepts a path or fetches a URL."""
        import jsonschema
        from mcp import types
        jsonschema.validate(arguments, context_image_tool_spec()['inputSchema'])
        records = self.records()
        action = arguments.get('action', 'read')
        if action == 'list':
            through = arguments.get('through_index', max(0, len(records) - 1))
            before = arguments.get('before_index', through + 1)
            selected = [r for r in reversed(records) if r['index'] <= through and r['index'] < before
                        and (not arguments.get('source_tool') or r['source_tool'] == arguments['source_tool'])]
            limit = arguments.get('limit', 16)
            page = selected[:limit]
            public = [{**r, 'read_arguments': {'view_id': r['view_id'], 'sha256': r['sha256']}} for r in page]
            next_args = None
            if len(selected) > limit:
                next_args = {**arguments, 'action': 'list', 'through_index': through,
                             'before_index': page[-1]['index'], 'limit': limit}
            result = [types.TextContent(type='text', text=encoded({
                'images': public, 'next_arguments': next_args,
                'note': 'Saved provenance is not current artifact validation.',
            }).decode())]
            receipt = {'action': 'list', 'returned': len(page)}
        else:
            record = next((r for r in records if r['view_id'] == arguments.get('view_id')), None)
            if record is None:
                raise ValueError('Exact registered view_id and sha256 are required')
            if arguments.get('sha256') != record['sha256']:
                source = {'tool': record['source_tool'], 'call_id': record['call_id']}
                if record['source_tool'] in DIRECT_READS:
                    try:
                        producer = record['source_arguments']
                        producer = json.loads(producer) if isinstance(producer, str) else producer
                    except ValueError:
                        producer = None
                    if isinstance(producer, dict):
                        if isinstance(producer.get('name'), str):
                            source['name'] = producer['name'][:160]
                        box = producer.get('box')
                        if isinstance(box, list) and len(box) == 4 and all(type(v) is int for v in box):
                            source['box'] = box
                hint = {'source': source, 'read': {'tool': NAME, 'arguments': {
                    'view_id': record['view_id'], 'sha256': record['sha256']}}}
                raise ValueError('Exact registered view_id and sha256 are required. '
                                 'Check the source before requesting this saved image: ' + encoded(hint).decode())
            data = (self.directory / 'media' / record['sha256']).read_bytes()
            if sha(data) != record['sha256']:
                raise ValueError('Archived image bytes do not match the registered digest')
            result = [
                types.TextContent(type='text', text=encoded({'saved_image': record,
                    'note': 'Exact saved image; no new source evidence or current artifact validation.'}).decode()),
                types.ImageContent(type='image', data=base64.b64encode(data).decode(), mimeType=record['mime_type']),
            ]
            receipt = {'action': 'read', 'view_id': record['view_id'], 'sha256': record['sha256'], 'bytes': len(data)}
        with (self.directory / 'reads.jsonl').open('ab') as log:
            log.write(encoded({'time': time.time(), **receipt}) + b'\n')
        return result


async def serve(directory):
    from mcp import types
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    server = Server('sim-bim-context-images')
    archive = ImageHistory(directory)

    @server.list_tools()
    async def list_tools():
        return [types.Tool(**context_image_tool_spec())]

    @server.call_tool()
    async def call_tool(name, arguments):
        if name != NAME:
            raise ValueError('Unknown runtime tool')
        return archive.call(arguments)

    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(serve(args.directory.resolve()))
