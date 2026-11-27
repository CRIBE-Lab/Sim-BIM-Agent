"""Drive an admitted native session to an attested delivery or a classified error."""
from __future__ import annotations

import time

from . import relay
from .lifecycle_evidence import attest_host_finish, verify_recovery_state

REVISION = 'dsh-delivery-loop-v1'
EXTERNAL_RETRIES = 3


def attempt_directory(session, attempt):
    return session if attempt == 1 else session / f'attempt-{attempt:03}'


def _logs(session):
    paths = [session / 'events.jsonl']
    paths.extend(p / 'events.jsonl' for p in sorted(session.glob('attempt-*')) if p.is_dir())
    return [p for p in paths if p.is_file()]


def _budget_failure(state, case):
    if state.requests >= state.limits['model_calls'] or state.tool_calls >= state.limits['tool_calls']:
        return {'category': 'budget_exhausted', 'code': 'run_budget_reached',
                'message': 'The configured run budget was exhausted before an attested delivery.',
                'external': False, 'retryable': False}
    wall = state.limits['wall_seconds']
    epoch = relay.read_json(case / 'private/start.json')['epoch']
    if wall is not None and time.time() >= epoch + wall:
        return {'category': 'budget_exhausted', 'code': 'wall_budget_reached',
                'message': 'The configured wall-time budget was exhausted before delivery.',
                'external': False, 'retryable': False}
    return None


def drive(case, session, config, server, url, nonce, first, resume, host, *, wait=time.sleep):
    """Never infer success from process exit or the presence of a saved candidate."""
    state = server.state
    run = case / 'private/dsh/work/bim'
    control = case / 'control'
    attempt, mode = first, resume
    recoveries = continuations = consecutive_external = 0
    last_failed_turn = None
    proof = failure = None
    host_exit = None
    host_attempts = 0
    retry_backoff = 0
    while True:
        if attempt == first and mode:
            previous = _logs(session)
            snapshot = (state.pending or {}).get('work_snapshot', state.request_snapshot)
            inspection = verify_recovery_state(run, snapshot,
                                               previous[-1] if previous else session / 'events.jsonl')
            relay.audit('resume_admission_inspected', attempt=attempt, **inspection)
            if not inspection['safe_to_resume']:
                failure = {'category': 'internal_error', 'code': 'recovery_state_unverified',
                           'message': 'The saved session must be reconciled before resuming.',
                           'external': False, 'retryable': False, 'inspection': inspection}
                break
        if mode is True and state.pending:
            service_failure = state.pending.get('failure') or {}
            failure = _budget_failure(state, case)
            if failure:
                break
            delay = max(retry_backoff, service_failure.get('retry_not_before', 0) - time.time())
            wall = state.limits['wall_seconds']
            if wall is not None and time.time() + delay >= relay.read_json(case / 'private/start.json')['epoch'] + wall:
                failure = {**service_failure, 'retry_exceeds_wall_budget': True}
                break
            if attempt != first:
                relay.audit('automatic_request_recovery', attempt=attempt - 1, prior_turn=state.pending['turn'],
                            failure=service_failure, backoff_seconds=delay, repeated_tool_calls=0)
            if delay > 0:
                wait(delay)
                failure = _budget_failure(state, case)
                if failure:
                    break
            if attempt != first:
                recoveries += 1
            retry_backoff = 0
        out = attempt_directory(session, attempt)
        events = out / 'events.jsonl'
        state.host_events = events
        before_requests = state.requests
        host_exit = host(case, session, config, url, nonce, attempt, mode)
        host_attempts += 1
        # The native process can exit on a streaming error while the relay is
        # still recording that request. Inspect only the settled attempt.
        with server.mutex:
            finish_file = control / 'dsh-relay-finish.json'
            recorded = relay.read_json(finish_file) if finish_file.is_file() else None
            proof = attest_host_finish(run, _logs(session), server.minimum_floors, recorded_proof=recorded)
        if proof:
            relay.save(control / 'dsh-relay-finish.json', proof)
            state.pending = None
            (control / 'pending.json').unlink(missing_ok=True)
            relay.audit('verified_host_finish', attempt=attempt, candidate=proof['candidate'])
            break
        failure = _budget_failure(state, case)
        if failure:
            break
        if state.failure and not state.failure.get('retryable'):
            # A new contract/auth failure must not be overwritten by an old
            # pending request whose continuation was never accepted.
            failure = state.failure
            break
        if mode and state.requests == before_requests:
            failure = {'category': 'internal_error',
                       'code': 'host_recovery_made_no_request' if mode is True else 'host_continuation_made_no_request',
                       'message': 'The native host did not consume its verified continuation.',
                       'external': False, 'retryable': False}
            break
        pending = state.pending
        if pending:
            failure = pending.get('failure') or {
                'category': 'unclassified', 'code': pending.get('reason', 'unknown_pending'),
                'message': 'Interrupted request lacks a classified recovery record.',
                'external': False, 'retryable': False}
            if not failure.get('retryable'):
                break
            inspection = verify_recovery_state(run, pending.get('work_snapshot'), events)
            relay.audit('recovery_state_inspected', attempt=attempt, prior_turn=pending['turn'], **inspection)
            if not inspection['safe_to_resume']:
                failure = {'category': 'internal_error', 'code': 'recovery_state_unverified',
                           'message': 'Recovery cannot replay an unverified tool or changed saved state.',
                           'external': False, 'retryable': False, 'inspection': inspection}
                break
            external = bool(failure.get('external'))
            # A successful intervening model response starts a fresh failure streak.
            if last_failed_turn is None or pending['turn'] != last_failed_turn + 1:
                consecutive_external = 0
            consecutive_external = consecutive_external + 1 if external else 0
            last_failed_turn = pending['turn']
            if external and consecutive_external > EXTERNAL_RETRIES:
                failure = {**failure, 'retries_exhausted': True,
                           'consecutive_failures': consecutive_external}
                break
            retry_backoff = min(2 ** max(consecutive_external - 1, 0), 8) if external else 0
            mode = True
        elif state.failure:
            failure = state.failure
            break
        elif state.completion_checkpoint is not None or state.last_calls:
            inspection = verify_recovery_state(run, state.request_snapshot, events)
            # Completed tools may legitimately have changed the previous request's
            # snapshot. For a finished tool batch require a fresh closed-journal
            # inspection, never replay that batch; the next input contains receipts.
            if state.last_calls and inspection.get('reason') == 'work_snapshot_changed':
                from .lifecycle_evidence import work_snapshot
                inspection = verify_recovery_state(run, work_snapshot(run), events)
            relay.audit('delivery_continuation_inspected', attempt=attempt, **inspection)
            if not inspection['safe_to_resume']:
                failure = {'category': 'internal_error', 'code': 'unfinished_tool_batch',
                           'message': 'A tool result or persistent effect must be reconciled before continuing.',
                           'external': False, 'retryable': False, 'inspection': inspection}
                break
            state.delivery_requested = True
            relay.audit('automatic_delivery_continuation', attempt=attempt,
                        after_turn=state.requests, reason='no_verified_finish',
                        continuation_kind='completed_text' if state.completion_checkpoint is not None else 'completed_tools')
            continuations += 1
            mode = 'delivery'
        else:
            failure = {'category': 'internal_error', 'code': 'host_exit_without_recoverable_state',
                       'message': 'The native host exited without delivery or a recoverable model checkpoint.',
                       'external': False, 'retryable': False, 'host_exit_code': host_exit}
            break
        attempt += 1
    external = bool(failure and failure.get('external'))
    terminal = {'ended': time.time(), 'exit_code': 0 if proof else 2,
                'host_exit_code': host_exit, 'status': 'delivered' if proof else
                ('external_error' if external else (failure or {}).get('category', 'internal_error')),
                'delivered': bool(proof), 'proof': proof, 'failure': None if proof else failure,
                'allowed_external_failure': external if not proof else False,
                'model_requests': state.requests, 'tool_calls_reserved': state.tool_calls,
                'pending': bool(state.pending), 'automatic_recoveries': recoveries,
                'delivery_continuations': continuations, 'host_attempts_this_launch': host_attempts,
                'delivery_loop_revision': REVISION}
    if config.get("worker", {}).get("background"):
        from .observer_closeout import observer_closeout
        wall = state.limits["wall_seconds"]
        deadline = relay.read_json(case / "private/start.json")["epoch"] + wall if wall is not None else None
        terminal["background_workers"] = observer_closeout(case, deadline)
        terminal["ended"] = time.time()
    relay.save(control / 'terminal.json', terminal)
    return terminal
