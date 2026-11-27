"""Launch the installed DSH against one admitted BIM case and the project subscription."""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from src.agent.runtime_entry import prepare_inputs
from src.agent.runtime_tools import frozen_bim_client
from src.agent_runtime.openai_subscription import SubscriptionCredentials
from src.agent_runtime.providers import is_custom_provider
from . import relay
from .profile import make_patch
ROOT = Path(__file__).resolve().parents[2]
PACKAGE = Path(__file__).resolve().parent
OVERLAY = PACKAGE / 'deepseek_harness.yml'
if not OVERLAY.is_file():
    OVERLAY = ROOT / 'AI_agent/workflow/configs/deepseek_harness.yml'

def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def host_identity(config):
    exe = Path(config['dsh_executable'])
    return {'executable': digest(exe), 'app_asar': digest(exe.parent / 'resources/app.asar'),
            'overlay': digest(OVERLAY)}

def prepare(case, *, material_paths=None):
    """Freeze current domain guidance/schemas after the normal input admission."""
    config = read(case / 'config.json')
    task = (case / 'shared/task.txt').read_text(encoding='utf-8-sig').strip()
    work = case / 'private/dsh/work'
    work.mkdir(parents=True, exist_ok=True)
    if (work / 'bim').exists():
        raise ValueError('This case already contains admitted inputs')
    limits = config['limits']
    inputs = material_paths if material_paths is not None else {
        'images': case / 'inputs/auxiliary', 'mesh': case / 'inputs/building.glb',
        'building_input': case / 'inputs/building.json'}
    run, guide, effective_task = prepare_inputs(
        work,
        **inputs,
        scope=task,
        image_kind=config.get('image_kind', 'drawings'),
        max_candidates=limits['candidates'],
        floor_plan_images=config.get('floor_images'),
        started_epoch=time.time(),
        seconds=limits['wall_seconds'],
        **({'orchestrator': True} if material_paths is not None else {}),
    )
    content = [{'type': 'text', 'text': effective_task}]
    manifest = read(run / 'inputs.json')
    admitted = manifest.get('building_input')
    if admitted:
        content.append(
            {
                'type': 'text',
                'text': 'Additional user-supplied building declaration; not independently verified: ' + json.dumps({k: admitted[k] for k in ('raw_sha256', 'declaration', 'field_semantics', 'conflict_policy')}, ensure_ascii=False),
            },
        )

    async def catalog():
        async with frozen_bim_client(run, readonly=False, repository_root=ROOT) as client:
            return await client.list_tools()
    specs = asyncio.run(catalog())
    if config.get('worker'):
        from src.agent.runtime_mesh_workers import observer_tool_specs
        worker = config["worker"]
        specs += observer_tool_specs(dsh=True, background=worker.get("background", False),
                                     routes=worker.get("routes"))
    tools = [{
        'type': 'function',
        'function': {'name': t['name'], 'description': t.get('description', ''), 'parameters': t['inputSchema']},
    } for t in specs]
    shared = case / 'shared'
    (shared / 'guide.txt').write_text(guide, encoding='utf-8')
    relay.save(shared / 'limits.json', limits)
    relay.save(shared / 'tools.json', tools)
    frozen_files = [case / 'config.json', *shared.glob('*.txt'), shared / 'limits.json',
                    shared / 'tools.json', shared / 'initial-messages.json', *sorted((case / 'inputs').rglob('*'))]
    if material_paths is not None:
        frozen_files += [run / 'inputs.json', *sorted((run / 'images').glob('*.png')),
                         *sorted((run / 'assets').rglob('*')), run / 'building_input.json']
        if material_paths.get('revision') is not None:
            # Freeze the original snapshot index; editable working copies are separately
            # admitted by digest and may receive new child-side evidence during the session.
            revision_index = material_paths['revision_directory'] / 'snapshot.json'
            if digest(revision_index) != material_paths['revision']['snapshotSha256']:
                raise ValueError("Revision snapshot changed during admission")
    relay.save(
        shared / 'initial-messages.json',
        [{'role': 'system', 'content': guide}, {'role': 'user', 'content': content}],
    )
    from src.agent_runtime.agent_registry import release_records, source_commit
    records = release_records(ROOT)
    relay.save(
        shared / 'freeze.json',
        {
            'created': time.time(),
            'domain': records['domain'],
            **({'runtime': records['runtime'], 'source_commit': source_commit(ROOT),
                'installed_host': host_identity(config)}
               if material_paths is not None else {}),
            'files': {str(p.relative_to(case)): digest(p) for p in frozen_files if p.is_file()},
        },
    )
    return run

def host_environment(case, session, nonce, url):
    """Keep only OS startup variables and the loopback capability in the DSH process."""
    cwd = session / 'workspace'
    cwd.mkdir(exist_ok=True, parents=True)
    fields = {
        'PATH',
        'SYSTEMROOT',
        'WINDIR',
        'COMSPEC',
        'PATHEXT',
        'TEMP',
        'TMP',
        'SYSTEMDRIVE',
        'NUMBER_OF_PROCESSORS',
        'PROCESSOR_ARCHITECTURE',
    }
    env = {k: v for k, v in os.environ.items() if k.upper() in fields}
    env.update(
        DSH_HOME=str(session / 'dsh-home'),
        HOME=str(cwd),
        USERPROFILE=str(cwd),
        APPDATA=str(session / 'appdata'),
        LOCALAPPDATA=str(session / 'localappdata'),
        DSH_TELEMETRY_MODE='OFF',
        SIM_BIM_RELAY_TOKEN=nonce,
        SIM_BIM_REPO=str(ROOT),
        SIM_BIM_RUN=str(case / 'private/dsh/work/bim'),
        SIM_BIM_WRITER='1',
        SIM_BIM_LOCAL_ENDPOINT=url + '/responses',
        SIM_BIM_LOCAL_TRANSPORT_RECEIPT=str(session / 'local-fetch.json'),
        PYTHONDONTWRITEBYTECODE='1',
        PYTHONUTF8='1',
        ELECTRON_RUN_AS_NODE='1',
        NODE_OPTIONS='--require="' + (PACKAGE / 'host-network.cjs').as_posix() + '"',
        HTTP_PROXY='http://127.0.0.1:1',
        HTTPS_PROXY='http://127.0.0.1:1',
        NO_PROXY='127.0.0.1,localhost',
    )
    return (env, cwd)

def session_id(session):
    files = list((session / 'dsh-home/sessions').rglob('session.v4.jsonl.zstd'))
    if len(files) != 1:
        raise ValueError('Expected exactly one native DSH session')
    return files[0].parent.name

def next_attempt(session):
    used = [1] if (session / 'events.jsonl').exists() else []
    used.extend(
        (int(path.name.removeprefix('attempt-')) for path in session.glob('attempt-*') if path.is_dir() and path.name.removeprefix('attempt-').isdigit()),
    )
    return max(used, default=0) + 1

def execute_host(case, session, config, url, nonce, attempt, resume):
    env, cwd = host_environment(case, session, nonce, url)
    out = session if attempt == 1 else session / f'attempt-{attempt:03}'
    out.mkdir(exist_ok=True, parents=True)
    patch = out / 'dsh.yml'
    policy = case / 'control/runtime-policy.json'
    images = case / 'control/image-history' if policy.exists() else None
    worker_args = None
    if config.get('worker'):
        worker = config['worker']
        routes = worker.get('routes', [])
        worker_provider = routes[0]['provider'] if routes else config.get('provider', 'chatgpt-subscription')
        worker_args = ['--run', str(case / 'private/dsh/work/bim'),
                       '--store', str(case / 'private/workers'), '--provider', worker_provider,
                       '--model', worker['model'], '--chatgpt-auth-dir', config['chatgpt_auth_dir'],
                       '--output-tokens', str(worker['output_tokens']), '--reasoning-effort', worker['reasoning_effort'],
                       '--max-concurrent', str(worker['max_concurrent']),
                       '--model-calls', str(worker['model_calls']), '--tool-calls', str(worker['tool_calls'])]
        if routes:
            worker_args += ['--routes', json.dumps(routes)]
        if config.get('connection_revisions'):
            worker_args += ['--connection-revisions', json.dumps(config['connection_revisions']),
                            '--custom-directory', config['custom_directory']]
        if worker_provider == 'deepseek' or any(r['provider'] == 'deepseek' for r in routes):
            if config.get('credentials_file'):
                worker_args += ['--credentials-file', config['credentials_file']]
            else:
                from src.workbench.connections import protected_deepseek_key_path
                worker_args += ['--protected-key', str(protected_deepseek_key_path())]
        if config['limits']['wall_seconds'] is not None:
            worker_args += ['--seconds', str(config['limits']['wall_seconds'])]
        if worker.get('background'):
            worker_args.append('--background')
    patch.write_text(make_patch(url, config['model'], images, worker_args=worker_args,
                                context_window=config.get('context_window', 1050000),
                                **({'display_name': 'Custom model API', 'max_tokens': config['output_tokens']}
                                   if is_custom_provider(config.get('provider')) else {}),
                                **({'display_name': 'DeepSeek official API'} if config.get('provider') == 'deepseek' else {}),
                                worker_wall_seconds=config['limits']['wall_seconds'] if worker_args else None), encoding='utf-8')
    exe = Path(config['dsh_executable'])
    cli = exe.parent / 'resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js'
    command = [
        str(exe),
        '--expose-internals',
        str(cli),
        '--profile',
        'headless',
        '--patch',
        str(OVERLAY),
        '--patch',
        str(patch),
    ]
    if resume:
        command += ['--session-id', session_id(session)]
    command += ['--json', '-']
    from .completion import DELIVERY_MARKER
    prompt = (DELIVERY_MARKER if resume == 'delivery' else relay.RESUME_MARKER if resume else
              (case / 'shared/task.txt').read_text(encoding='utf-8'))
    if not prompt.strip():
        # DSH needs a nonempty initial turn even when the optional kickoff is blank.
        prompt = read(case / 'shared/initial-messages.json')[1]['content'][0]['text']
    env['SIM_BIM_LOCAL_TRANSPORT_RECEIPT'] = str(out / 'local-fetch.json')
    started = time.time()
    proc = None
    error = None
    with (out / 'events.jsonl').open('xb') as log, (out / 'stderr.log').open('xb') as stderr:
        try:
            proc = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=stderr,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            wall = config['limits']['wall_seconds']
            epoch = read(case / 'private/start.json')['epoch'] if wall is not None else None
            timeout = max(0, epoch + wall - time.time()) if wall is not None else None
            proc.communicate(prompt.encode('utf-8'), timeout=timeout)
        except subprocess.TimeoutExpired:
            error = {'type': 'wall_deadline_exceeded'}
        except OSError as exc:
            error = {'type': type(exc).__name__, 'errno': exc.errno}
        finally:
            if proc is not None and proc.poll() is None:
                subprocess.run(
                    ['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                    capture_output=True,
                    check=False,
                )
                proc.wait(timeout=30)
    journals = list((session / 'dsh-home/sessions').rglob('session.v4.jsonl.zstd'))
    exit_code = proc.returncode if proc is not None else 1
    relay.save(
        out / 'execution.json',
        {
            'runtime': 'DeepSeek Harness',
            'started': started,
            'ended': time.time(),
            'exit_code': exit_code,
            'model': config['model'],
            'reasoning_effort': 'high',
            'resume': bool(resume),
            'continuation_kind': 'delivery' if resume == 'delivery' else 'request_retry' if resume else None,
            'startup_error': error,
            'native_session_id': journals[0].parent.name if len(journals) == 1 else None,
            'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
            'patch_sha256': digest(patch),
            'overlay_sha256': digest(OVERLAY),
        },
    )
    return exit_code

def run(case, resume=False, *, credential=None, host=execute_host, image_history_batches=None, print_terminal=True):
    config = read(case / 'config.json')
    freeze = read(case / 'shared/freeze.json')
    if any((digest(case / name) != value for name, value in freeze['files'].items())):
        raise ValueError('Frozen case inputs or settings changed')
    from src.agent_runtime.agent_registry import release_records
    records = release_records(ROOT)
    if records['domain'] != freeze['domain']:
        raise ValueError('Domain changed after this case was prepared')
    if 'runtime' in freeze and (records['runtime'] != freeze['runtime'] or host_identity(config) != freeze['installed_host']):
        raise ValueError('Runtime or installed DSH changed after this case was prepared')
    control = case / 'control'
    control.mkdir(exist_ok=True)
    from .image_history import ImageHistory, REVISION, native_tool
    policy_path = control / 'runtime-policy.json'
    policy = read(policy_path) if policy_path.exists() else None
    if image_history_batches is not None:
        proposed = {'revision': REVISION, 'image_history_batches': image_history_batches,
                    'runtime_tool': native_tool(), 'domain_tool_catalog': 'shared/tools.json'}
        # Validate before persisting a controlled runtime overlay; the frozen
        # domain catalog, user inputs, and original request records stay intact.
        ImageHistory(control / 'image-history', image_history_batches)
        if policy is not None and policy != proposed:
            raise ValueError('An existing image-history policy cannot change on resume')
        policy = proposed
        if not policy_path.exists():
            relay.save(policy_path, policy)
    images = None
    if policy is not None:
        if policy['revision'] != REVISION or policy['runtime_tool'] != native_tool():
            raise ValueError('Unsupported runtime image-history overlay')
        images = ImageHistory(control / 'image-history', policy['image_history_batches'])
    session = case / 'private/dsh/session'
    run = case / 'private/dsh/work/bim'
    if resume:
        if not any((control / name).exists() for name in ('pending.json', 'continuation.json')):
            raise ValueError('No recorded interrupted request or delivery checkpoint to resume')
    elif (control / 'relay.jsonl').exists():
        raise ValueError('Existing run requires explicit --resume')
    from .lifecycle import drive, REVISION, EXTERNAL_RETRIES
    delivery_policy = {'revision': REVISION, 'external_retries_per_streak': EXTERNAL_RETRIES,
                       'completion': 'attested_model_finish_only',
                       'uncertain_tool_write': 'inspect_before_continue'}
    policy_file = control / 'delivery-policy.json'
    if policy_file.exists() and read(policy_file) != delivery_policy:
        raise ValueError('Recorded delivery recovery policy differs from this runtime')
    if not policy_file.exists():
        relay.save(policy_file, delivery_policy)
    if credential is None:
        if config.get('provider') == 'deepseek' or is_custom_provider(config.get('provider')):
            from .model_routes import ApiCredential, model_connection
            provider = config['provider']
            credential = ApiCredential(model_connection(provider, config['model'], credentials_file=config.get('credentials_file'),
                                       custom_directory=config.get('custom_directory'),
                                       revision=config.get('connection_revisions', {}).get(provider)))
        else:
            credential = (SubscriptionCredentials.legacy_project_registration() if config.get('subscription_store') == 'legacy-project'
                          else SubscriptionCredentials(Path(config['chatgpt_auth_dir'])) if config.get('chatgpt_auth_dir')
                          else SubscriptionCredentials())
    server = relay.create_server(
        case,
        control,
        config['model'],
        credential,
        config.get('minimum_floor_elevations', 1),
        image_history=images,
        **({'provider': 'deepseek', 'output_tokens': config['output_tokens']} if config.get('provider') == 'deepseek' else {}),
        **({'provider': config['provider'], 'output_tokens': config['output_tokens'],
            'output_parameter': config.get('output_parameter', 'max_tokens')}
           if is_custom_provider(config.get('provider')) else {}),
    )
    serving = False
    try:
        server.background_workers = config.get('worker', {}).get('background', False)
        if resume:
            from .continuation import restore_delivery, restore_pending
            if (control / 'pending.json').exists():
                restore_pending(server.state, control)
            else:
                restore_delivery(server.state, control)
                resume = 'delivery'
        nonce = secrets.token_urlsafe(40)
        server.auth = 'Bearer ' + nonce
        url = f'http://127.0.0.1:{server.server_port}/dsh/v1'
        epoch = read(run / 'inputs.json')['started_epoch']
        if not (case / 'private/start.json').exists():
            relay.save(case / 'private/start.json', {'epoch': epoch})
        first = next_attempt(session)
        relay.save(
            control / f'launch-{first:03}.json',
            {
                'started': time.time(),
                'model': config['model'],
                'limits': config['limits'],
                'runtime_files': {p.name: digest(p) for p in PACKAGE.iterdir() if p.suffix in {'.py', '.cjs'}},
                'dsh_executable_sha256': digest(Path(config['dsh_executable'])),
                'resumed': bool(resume),
                'delivery_policy': delivery_policy,
                'runtime_policy': policy,
                'runtime_policy_sha256': digest(policy_path) if policy else None,
            },
        )
        relay.save(
            control / 'relay-ready.json',
            {'pid': os.getpid(), 'port': server.server_port, 'model': config['model']},
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        serving = True
        terminal = drive(case, session, config, server, url, nonce, first, resume, host)
        if print_terminal:
            print(json.dumps(terminal, ensure_ascii=False))
        if not terminal['delivered']:
            failure = terminal['failure']
            print(f"Delivery failed [{failure['category']} / {failure['code']}]: {failure['message']}", file=sys.stderr)
        return terminal['exit_code']
    except BaseException as exc:
        relay.save(
            control / 'terminal.json',
            {
                'ended': time.time(),
                'delivered': False,
                'status': 'cancelled' if isinstance(exc, KeyboardInterrupt) else 'internal_error',
                'exit_code': 2,
                'allowed_external_failure': False,
                'error_type': type(exc).__name__,
                'model_requests': server.state.requests,
                'tool_calls_reserved': server.state.tool_calls,
                'pending': bool(server.state.pending),
            },
        )
        raise
    finally:
        if serving:
            server.shutdown()
        server.server_close()
        server.transport.client.close()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', type=Path, required=True)
    parser.add_argument('--image-history-batches', type=int,
                        help='Retain complete recent image-bearing tool batches; register exact image retrieval')
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        '--prepare',
        action='store_true',
        help='Admit inputs and freeze the current domain without a model call',
    )
    group.add_argument(
        '--resume',
        action='store_true',
        help='Resume an interrupted request or a verified unfinished-delivery checkpoint',
    )
    args = parser.parse_args()
    case = args.case.resolve()
    if args.prepare:
        print(prepare(case))
        return 0
    try:
        return run(case, args.resume, image_history_batches=args.image_history_batches)
    except Exception as exc:
        print(json.dumps({'status': 'admission_or_internal_error', 'delivered': False,
                          'error_type': type(exc).__name__, 'allowed_external_failure': False}), file=sys.stderr)
        return 2
