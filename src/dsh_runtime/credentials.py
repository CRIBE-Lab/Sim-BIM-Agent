"""Bounded credential acquisition before any model request is submitted."""
from __future__ import annotations

import asyncio

import httpx

from src.agent_runtime.openai_subscription import (
    SubscriptionAuthError,
    _OAuthHttpError,
    _UNUSABLE_REFRESH_ERRORS,
)

from .transport import classify_failure


def _failure(exc):
    """Inspect typed evidence, including the auth client's suppressed context."""
    chain = []
    seen = set()
    current = exc
    while current is not None and id(current) not in seen and len(chain) < 12:
        seen.add(id(current))
        chain.append({'type': type(current).__name__})
        if not isinstance(current, SubscriptionAuthError):
            break
        current = current.__cause__ or current.__context__

    diagnostic = {'phase': 'authentication', 'causes': chain}
    failure = {'category': 'internal_error', 'code': 'authentication_local_error',
               'message': 'Credential acquisition failed before a model request was submitted.',
               'external': False, 'retryable': False, 'diagnostic': diagnostic}
    status = None
    oauth_code = None
    if isinstance(current, _OAuthHttpError):
        status = current.status
        # OAuth error strings are untrusted server data, so keep only known codes.
        if isinstance(current.code, str) and current.code in _UNUSABLE_REFRESH_ERRORS:
            oauth_code = current.code
            diagnostic['oauth_code'] = oauth_code
    elif isinstance(current, httpx.HTTPStatusError):
        status = current.response.status_code

    if type(status) is int and 400 <= status <= 599:
        diagnostic['http_status'] = status
        external = status in {401, 403, 408, 429} or status >= 500 or oauth_code is not None
        failure.update(category='external_auth' if external else 'internal_error',
                       code=oauth_code or f'authentication_http_{status}', external=external)
        return failure

    if isinstance(current, httpx.TransportError):
        boundary = classify_failure(current)
        diagnostic.update(boundary)
        if boundary['category'] == 'upstream_transport':
            failure.update(category='external_auth', code='authentication_network_error', external=True)
    return failure


async def acquire_token(credential, timeout):
    """Classify credential failures without replaying a rotating refresh token.

    The relay has not submitted or counted a Responses request at this point.
    A failure can happen after token rotation, while validating an ID token or
    fetching JWKS, so even ConnectError cannot authorize repeating this method.
    The credential owner must retain authority over operation-level recovery.
    """
    from . import relay

    scope = asyncio.timeout(timeout)
    try:
        async with scope:
            return await credential.get_access_token()
    except Exception as exc:
        if isinstance(exc, TimeoutError) and scope.expired():
            # A timeout during local credential work is not proof of network loss.
            failure = {'category': 'internal_error', 'code': 'authentication_deadline_exceeded',
                       'message': 'Credential acquisition exceeded its total time limit before model submission.',
                       'external': False, 'retryable': False, 'deadline_exceeded': True,
                       'diagnostic': {'phase': 'authentication', 'causes': [{'type': 'TimeoutError'}]}}
        else:
            failure = _failure(exc)
        failure['attempts'] = 1
        relay.audit('auth_failure', phase='authentication', attempt=1,
                    request_not_submitted=True, diagnostic=failure['diagnostic'],
                    failure=failure, retry=False)
        raise relay.ProviderInterruption(failure) from None
