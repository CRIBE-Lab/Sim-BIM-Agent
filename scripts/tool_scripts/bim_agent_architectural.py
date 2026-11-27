"""Bind architectural adoption to frozen run policy and original declarations.

Observations and parametric drafts remain untouched.  Only the adopted copy is
expanded; its policy, audit and proposal are independently hash-bound.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _path(toolkit, reference):
    root = Path(toolkit.run).resolve()
    path = (root / reference).resolve()
    if not path.is_relative_to(root):
        raise ValueError("architectural regularization reference escapes this run")
    return path


def _save(toolkit, path, value):
    path = _path(toolkit, path)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True,
            indent=2, allow_nan=False) + "\n")
    return {"file": path.relative_to(Path(toolkit.run).resolve()).as_posix(),
            "sha256": _sha(path)}


def _read(toolkit, reference):
    path = _path(toolkit, reference["file"])
    try:
        data = path.read_bytes()
    except FileNotFoundError as error:
        raise ValueError(f"architectural regularization artifact unavailable: {reference['file']}") from error
    if hashlib.sha256(data).hexdigest() != reference["sha256"]:
        raise ValueError("architectural regularization artifact changed")
    return json.loads(data)


def _policy(toolkit):
    return getattr(toolkit, "manifest", {}).get("architectural_modelling_policy")


def effective_parametric_plan(plan, manifest):
    """Resolve explicit input shorthands without changing the editable draft."""
    if not manifest.get("orchestrator") or not isinstance(plan, dict):
        return plan
    effective = plan.copy()
    frame = plan.get("mesh_frame")
    frozen_sha = (manifest.get("mesh_input") or {}).get("sha256")
    if isinstance(frame, dict) and frozen_sha:
        if frame.get("mesh_sha256", frozen_sha) != frozen_sha:
            raise ValueError("mesh_frame.mesh_sha256 differs from the frozen original GLB")
        effective["mesh_frame"] = {**frame, "mesh_sha256": frozen_sha}
    connections = plan.get("connections")
    if isinstance(connections, list):
        effective["connections"] = connections.copy()
        for i, opening in enumerate(connections):
            if not isinstance(opening, dict):
                continue
            endpoints = {"z_min", "z_max"} & opening.keys()
            if not endpoints and "z" in opening:
                continue
            if len(endpoints) != 2 or "z" in opening:
                raise ValueError(f"/connections/{i}: declare absolute z=[bottom,top] in metres "
                                 "or both z_min and z_max, using only one form")
            row = opening.copy()
            row["z"] = [row.pop("z_min"), row.pop("z_max")]
            effective["connections"][i] = row
    return effective


def prepare_parametric_plan(toolkit, plan, *, draft_path):
    effective = effective_parametric_plan(plan, getattr(toolkit, "manifest", {}))
    policy = _policy(toolkit)
    if policy is None:
        return effective, None
    from src.agent.geometry.architectural_regularization import regularize_architectural_plan
    from src.agent.geometry.parametric_proposal import expand_parametric_proposal
    try:
        adopted, audit = regularize_architectural_plan(effective, policy=policy)
    except ValueError as error:
        if hasattr(error, "audit"):
            error.audit_reference = _save(toolkit,
                draft_path.with_suffix(".regularization.json"), error.audit)
        raise
    modelling = copy.deepcopy(plan.get('modelling', {}))
    if audit.get('policy', {}).get('frames'):
        modelling['frames'] = copy.deepcopy(audit['policy']['frames'])
    reference = {"schema_version": 1, "kind": "parametric", "policy": copy.deepcopy(policy),
        "modelling": modelling,
        "original_sha256": _hash(plan), "adopted_sha256": _hash(adopted),
        "proposal_sha256": _hash(expand_parametric_proposal(adopted)),
        "adopted": _save(toolkit, draft_path.with_suffix(".adopted.json"), adopted),
        "audit": _save(toolkit, draft_path.with_suffix(".regularization.json"), audit)}
    from scripts.tool_scripts.bim_agent_source_precision import reference_line_policy
    if reference_line_policy(toolkit.manifest):
        original = {"file": _path(toolkit, draft_path).relative_to(Path(toolkit.run).resolve()).as_posix(),
                    "sha256": _sha(_path(toolkit, draft_path))}
        reference.update(original=original, effective=original if effective == plan else
            _save(toolkit, draft_path.with_suffix(".effective.json"), effective))
    return adopted, reference


def validate_build_regularization(toolkit, proposal, reference):
    if reference.get("policy") != _policy(toolkit):
        raise ValueError("architectural regularization policy changed")
    adopted = _read(toolkit, reference["adopted"])
    audit = _read(toolkit, reference["audit"])
    if _hash(adopted) != reference["adopted_sha256"]:
        raise ValueError("architectural adopted declaration binding changed")
    if _hash(proposal) != reference["proposal_sha256"]:
        raise ValueError("proposal does not match architectural adoption")
    inherited = audit.get('retained_parent_rings')
    if inherited:
        parent = _parent_adoption(toolkit, inherited['candidate'])
        source, original, parent_ref, _, _ = parent
        if (not parent_ref or inherited['source_model_sha256'] != source['source_model_sha256']
                or inherited['proposal_sha256'] != parent_ref['proposal_sha256']
                or not _same_parent_structure(original, proposal)):
            raise ValueError("architectural retained parent geometry binding changed")
        _verify_retained_declaration(toolkit, inherited['candidate'], parent)
    partition = audit.get('retained_partition_apertures')
    if partition:
        parent = _parent_adoption(toolkit, partition['candidate'])
        source, previous, parent_ref, _, _ = parent
        submitted = _read(toolkit, reference['original'])
        if (not parent_ref or partition['source_model_sha256'] != source['source_model_sha256']
                or partition['proposal_sha256'] != parent_ref['proposal_sha256']
                or not _only_translated_partition(previous, submitted)
                or any(proposal['geometry'].get(k) != submitted['geometry'].get(k)
                       for k in ('floors', 'openings', 'windows'))):
            raise ValueError("architectural retained partition aperture binding changed")
        _verify_retained_declaration(toolkit, partition['candidate'], parent)
    return adopted, audit


def validate_parametric_regularization(toolkit, state):
    reference = state["architectural_regularization"]
    folder = toolkit.candidate_path(state["candidate"])
    plan = json.loads((folder / "parametric_plan.json").read_bytes())
    if reference.get("kind") != "parametric" or _hash(plan) != reference["original_sha256"]:
        raise ValueError("architectural original declaration binding changed")
    # A normalized declaration can now have its own artifact. Exact patching
    # must still refer to the admitted input and the same frozen mesh frame.
    effective = reference.get("effective")
    if effective is not None and effective != reference.get("original"):
        if _read(toolkit, effective) != effective_parametric_plan(plan, toolkit.manifest):
            raise ValueError("architectural effective declaration binding changed")
    proposal = json.loads((folder / "proposal.json").read_bytes())
    validate_build_regularization(toolkit, proposal, reference)


def _parent_adoption(toolkit, parent):
    if parent is None:
        return None, None, None, None, None
    from src.agent.geometry.source_image_overlay import _source_hash
    folder = toolkit.candidate_path(parent)
    source = json.loads((folder/'source_model.json').read_bytes())
    _source_hash(source)
    reference = source.get('generation', {}).get('provenance', {}).get('architectural_regularization')
    if not reference:
        return source, None, None, None, None
    original = json.loads((folder/'proposal.json').read_bytes())
    adopted, audit = validate_build_regularization(toolkit, original, reference)
    return source, original, reference, adopted, audit


def _same_parent_structure(original, proposal):
    """Compare everything except editable apertures, notes and their correction log."""
    def structure(value, geometry):
        return ({k: v for k, v in value.items() if k not in {'geometry', 'assumptions', 'unresolved'}},
                {k: v for k, v in geometry.items() if k not in {'openings', 'windows', 'corrections'}})

    before, after = original['geometry'], proposal['geometry']
    # Analytic curves have a separate adoption contract and keep their ordinary route.
    if before.get('boundary_curves') or after.get('boundary_curves'):
        return False
    submitted = _hash(structure(proposal, after))
    if _hash(structure(original, before)) == submitted:
        return True
    # Local edits hydrate polygon defaults in the already accepted proposal.
    if before.get('schema_version') == 'source_polygon_v1':
        from src.agent.geometry.source_curves import expand_source_polygon_geometry
        return _hash(structure(original, expand_source_polygon_geometry(before))) == submitted
    return False


def _only_translated_partition(original, proposal):
    """Prove a single translated common edge, independent of the caller's edit log."""
    from src.agent.geometry.proposal_edits import _cell_shape, _translated_partition

    def excluding(row, keys):
        return {key: value for key, value in row.items() if key not in keys}

    before, after = original['geometry'], proposal['geometry']
    if (before.get('boundary_curves') or excluding(original, {'geometry'}) != excluding(proposal, {'geometry'})
            or excluding(before, {'floors', 'corrections'}) != excluding(after, {'floors', 'corrections'})
            or len(before['floors']) != len(after['floors'])):
        return False
    old_shapes = new_shapes = None
    ring_keys = {'x', 'y', 'polygon'}
    for old, new in zip(before['floors'], after['floors']):
        if old == new:
            continue
        if (old_shapes is not None or excluding(old, {'cells'}) != excluding(new, {'cells'})
                or len(old['cells']) != len(new['cells'])):
            return False
        changes = [(a, b) for a, b in zip(old['cells'], new['cells']) if a != b]
        if len(changes) != 2 or any(a.get('boundary_path') or excluding(a, ring_keys) != excluding(b, ring_keys)
                                    for a, b in changes):
            return False
        try:
            old_shapes, new_shapes = [
                [_cell_shape(cell, operation='partition adoption',
                             require_orthogonal=before.get('schema_version') != 'source_polygon_v1')
                 for cell in side] for side in zip(*changes)]
        except ValueError:
            return False
    if old_shapes is None:
        return False
    return (old_shapes[0].union(old_shapes[1]).equals(new_shapes[0].union(new_shapes[1]))
            and _translated_partition(old_shapes, new_shapes) is not None)


def _verify_retained_declaration(toolkit, parent, admission):
    from src.agent.geometry.architectural_regularization import _hash as audit_hash
    _, original, reference, adopted, audit = admission
    if (audit.get('status') != 'adopted' or audit.get('rejections')
            or audit.get('adopted_sha256') != audit_hash(adopted)):
        raise ValueError("architectural retained parent adoption changed")
    if reference['kind'] == 'parametric':
        raw_plan = toolkit.candidate_path(parent) / 'parametric_plan.json'
        if not raw_plan.is_file() and 'original' not in reference:
            # Older archives bind the adopted plan and its expansion. They can
            # verify the formal geometry even without a separate editable draft.
            from src.agent.geometry.parametric_proposal import expand_parametric_proposal
            if _hash(expand_parametric_proposal(adopted)) != _hash(original):
                raise ValueError("architectural retained parent expansion changed")
            return
        declaration = json.loads(raw_plan.read_bytes()) if raw_plan.is_file() else _read(toolkit, reference['original'])
        effective = effective_parametric_plan(declaration, toolkit.manifest)
    else:
        declaration = _read(toolkit, reference['original'])
        effective = declaration.copy()
        if reference['modelling']:
            effective['modelling'] = reference['modelling']
    if (_hash(declaration) != reference['original_sha256']
            or (reference['kind'] == 'parametric' and 'original' in reference and _read(toolkit, reference['original']) != declaration)
            or ('effective' in reference and _read(toolkit, reference['effective']) != effective)
            or audit.get('input_sha256') != audit_hash(effective)):
        raise ValueError("architectural retained parent declaration or adoption changed")


def _inherited_modelling(toolkit, parent, proposal, *, parent_adoption=None):
    """Carry adopted local frames through source edits without replaying old plans."""
    _, _, reference, adopted, _ = parent_adoption or _parent_adoption(toolkit, parent)
    if not reference:
        return {}
    context = copy.deepcopy(reference.get('modelling', {}))
    # Merge groups describe the adoption already performed. They do not grant
    # permission to keep re-merging levels after a later explicit source edit.
    context.pop('vertical', None)
    if reference.get('modelling', {}).get('vertical', {}).get('datum_m') is not None:
        context['vertical'] = {'datum_m': reference['modelling']['vertical']['datum_m']}
    names = {row['name'] for row in proposal['geometry']['floors']}
    if reference['kind'] == 'parametric':
        from src.agent.geometry.parametric_proposal import expand_parametric_instances
        instances = expand_parametric_instances(adopted)
        # Continuous groups compile into separate source floors. They must
        # inherit the same ruler as their members, or a later legal source edit
        # re-snaps one side of a core door in the identity frame.
        from src.agent.geometry.parametric_continuity import declared_continuity_members
        instance_by_id = {row['id']: row for row in instances}
        frames = context.get('frames', [])
        frame_by_template = {name: frame for frame in frames for name in frame['templates']}
        derived_frames = {}
        for group in declared_continuity_members(adopted):
            if group['id'] not in names:
                continue
            # Expansion copies the lowest member's adopted polygon. Retain
            # that member's frame; the usual cross-host gate still checks every
            # aperture against the other hosts after a subsequent source edit.
            reference_member = min(
                (instance_by_id[sid.split(':', 1)[0]] for sid in group['member_space_ids']),
                key=lambda row: row['z'])
            chosen = frame_by_template.get(reference_member['template'])
            if chosen:
                derived_frames[group['id']] = chosen
        for frame in context.get('frames', []):
            templates = set(frame['templates'])
            frame['templates'] = [row['id'] for row in instances
                                  if row['template'] in templates and row['id'] in names]
            frame['templates'].extend(name for name, inherited in derived_frames.items()
                                      if inherited is frame)
        intents = context.get('space_intents', {})
        context['space_intents'] = {
            f"{row['id']}:{row['id']}:{space['id']}": intents[f"{row['template']}:{space['id']}"]
            for row in instances if row['id'] in names
            for space in adopted['templates'][row['template']]['spaces']
            if f"{row['template']}:{space['id']}" in intents}
    else:
        for frame in context.get('frames', []):
            frame['templates'] = [name for name in frame['templates'] if name in names]
    if 'frames' in context:
        context['frames'] = [row for row in context['frames'] if row['templates']]
    if context.get('space_intents'):
        present = {f"{floor['name']}:{cell['id']}" for floor in proposal['geometry']['floors']
                   for cell in floor['cells']}
        context['space_intents'] = {key: value for key,value in context['space_intents'].items() if key in present}
    return context


def prepare_source_proposal(toolkit, proposal, *, parent=None, revise=False):
    policy = _policy(toolkit)
    if policy is None:
        return proposal, None
    from src.agent.geometry.architectural_regularization import regularize_architectural_proposal
    folder = Path(toolkit.run) / "architectural_drafts"
    folder.mkdir(exist_ok=True)
    number = max((int(p.stem.split("_")[-1]) for p in folder.glob("draft_*.json")
                  if p.stem.split("_")[-1].isdigit()), default=0) + 1
    path = folder / f"draft_{number:03d}.json"
    original_ref = _save(toolkit, path, proposal)
    effective = copy.deepcopy(proposal)
    parent_adoption = _parent_adoption(toolkit, parent)
    source, previous, parent_ref, _, _ = parent_adoption
    context = _inherited_modelling(toolkit, parent, proposal, parent_adoption=parent_adoption)
    context.update(copy.deepcopy(proposal.get('modelling', {})))
    if context:
        effective['modelling'] = context
    try:
        verified = revise and parent_ref is not None and source['validation']['status'] in {'pass', 'warning'}
        retained = verified and _same_parent_structure(previous, proposal)
        partition = verified and not retained and _only_translated_partition(previous, proposal)
        if retained or partition:
            _verify_retained_declaration(toolkit, parent, parent_adoption)
        adopted, audit = regularize_architectural_proposal(effective, policy=policy,
            _parent_geometry=previous['geometry'] if retained else None, _partition_edit=bool(partition))
        retention = 'retained_parent_rings' if retained else None
        if audit.get('retained_partition_apertures'):
            retention = 'retained_partition_apertures'
        if retention:
            audit[retention] = {'candidate': parent,
                'source_model_sha256': source['source_model_sha256'], 'proposal_sha256': parent_ref['proposal_sha256']}
    except ValueError as error:
        if hasattr(error, "audit"):
            error.audit['submitted_proposal'] = original_ref
            error.audit_reference = _save(toolkit, path.with_suffix(".regularization.json"), error.audit)
        raise
    reference = {"schema_version": 1, "kind": "proposal", "policy": copy.deepcopy(policy),
        "modelling": context, "frame_context_parent": parent,
        "original": original_ref, "original_sha256": _hash(proposal),
        "adopted_sha256": _hash(adopted), "proposal_sha256": _hash(adopted),
        "adopted": _save(toolkit, path.with_suffix(".adopted.json"), adopted),
        "audit": _save(toolkit, path.with_suffix(".regularization.json"), audit)}
    from scripts.tool_scripts.bim_agent_source_precision import reference_line_policy
    if reference_line_policy(toolkit.manifest):
        reference["effective"] = _save(toolkit, path.with_suffix(".effective.json"), effective)
    return adopted, reference
