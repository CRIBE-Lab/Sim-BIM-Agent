"""Read-only domain attestation of the actual final tool batch."""
import hashlib
import json
import math
from pathlib import Path
import time
from src.agent.runtime_delivery import attest_finished_handoff

def attest_local_finish(run, candidate, result, minimum_floors=1):
    proof = attest_finished_handoff(run, result, candidate)
    if not proof:
        return None
    basis = proof.get('scope_basis')
    field = {'mesh_model_reported_review': 'mesh_review',
             'drawing_model_reported_review': 'drawing_review'}.get(basis)
    review = proof.get(field, {})
    if review.get('status') != 'current' or review.get('coverage') != 'whole_building':
        return None
    source_name = f'{candidate}/source_model.json'
    try:
        raw = (Path(run) / source_name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != proof['artifact_sha256'][source_name]:
            return None
        source = json.loads(raw)
        elevations = {floor['z_floor'] for floor in source['floors'] if type(floor.get('z_floor')) in (float, int) and math.isfinite(floor['z_floor'])}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if len(elevations) < minimum_floors:
        return None
    return {
        **proof,
        'scope': 'whole_building',
        'distinct_floor_elevations': len(elevations),
        'architectural_quality': 'unrated',
    }

def attest_response_input(run, items, *, minimum_floors=1):
    """Verify only the final proposed action, its observed result, and files still on disk."""
    if not isinstance(items, list):
        return None
    calls = [(i, item) for i, item in enumerate(items) if isinstance(item, dict) and item.get('type') == 'function_call']
    if not calls:
        return None
    index, call = calls[-1]
    cid = call.get('call_id')
    if not cid or call.get('name') not in {'mcp__sim_bim__finish_bim', 'finish_bim'}:
        return None
    receipts = [b for b in items[index + 1:] if isinstance(b, dict) and b.get('type') == 'function_call_output' and (b.get('call_id') == cid)]
    if len(receipts) != 1:
        return None
    output = receipts[0].get('output')
    if isinstance(output, list):
        output = '\n'.join(
            (b['text'] for b in output if isinstance(b, dict) and isinstance(b.get('text'), str)),
        )
    try:
        args = call['arguments']
        candidate = (json.loads(args) if isinstance(args, str) else args)['candidate']
        result, _ = json.JSONDecoder().raw_decode(output.lstrip())
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if not isinstance(result, dict):
        return None
    proof = attest_local_finish(run, candidate, result, minimum_floors)
    if proof:
        return {
            **proof,
            'call_id': cid,
            'observed_in': 'DSH Responses request replay',
            'full_result_sha256': hashlib.sha256(output.encode()).hexdigest(),
            'verified_epoch': time.time(),
        }
    return None
