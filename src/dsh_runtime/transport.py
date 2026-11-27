"""Verified pre-write reconnection and pooled official Responses transport."""
from contextlib import contextmanager
import hashlib
import re
import time
import httpx


_MESSAGE_CODES = (
    ('peer closed connection without sending complete message body', 'peer_closed_incomplete_body'),
    ('server disconnected without sending a response', 'peer_closed_before_headers'),
    ('incomplete chunked read', 'incomplete_chunked_read'),
    ('incomplete message body', 'incomplete_message_body'),
    ('illegal chunk header', 'invalid_chunk_header'),
    ('illegal status line', 'invalid_status_line'),
    ('illegal header line', 'invalid_header_line'),
    ('received server connection termination', 'remote_connection_terminated'),
    ('stream ended before terminal response', 'upstream_stream_missing_terminal'),
    ('connection reset', 'connection_reset'),
    ('forcibly closed by the remote host', 'connection_reset'),
    ('connection aborted', 'connection_aborted'),
    ('all connection attempts failed', 'connection_attempts_failed'),
    ('getaddrinfo failed', 'dns_resolution_failed'),
    ('name or service not known', 'dns_resolution_failed'),
    ('eof occurred in violation of protocol', 'tls_unexpected_eof'),
    ('certificate verify failed', 'certificate_verification_failed'),
    ('timed out', 'timed_out'),
)


def classify_failure(exc):
    """Classify the observed boundary, without assigning fault to the origin server."""
    remote = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError,
              httpx.ReadTimeout, httpx.WriteError, httpx.WriteTimeout,
              httpx.CloseError, httpx.RemoteProtocolError)
    local = (httpx.LocalProtocolError, httpx.UnsupportedProtocol, httpx.PoolTimeout)
    category = ('upstream_transport' if isinstance(exc, remote) else
                'local_transport' if isinstance(exc, local) else 'unclassified')
    return {'category': category, 'reason_code': type(exc).__name__,
            'origin': 'unresolved' if category == 'upstream_transport' else 'local_or_unknown'}


def response_ids(response):
    """Only bounded public correlation identifiers, never arbitrary headers."""
    return {name: value for name in ('x-request-id', 'request-id')
            if (value := response.headers.get(name)) is not None
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value)}


def public_error(exc):
    """Preserve known protocol diagnoses without retaining raw exception payloads."""
    causes = []
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        message = str(exc)
        code = next((code for pattern, code in _MESSAGE_CODES if pattern in message.lower()), None)
        node = {'type': type(exc).__name__}
        for key in ('errno', 'winerror'):
            value = getattr(exc, key, None)
            if type(value) is int:
                node[key] = value
        if message:
            data = message.encode('utf-8', errors='replace')
            node.update(message_code=code or 'unclassified',
                        message_bytes=len(data), message_sha256=hashlib.sha256(data).hexdigest())
        causes.append(node)
        exc = exc.__cause__ or exc.__context__
    return causes

class PooledTransport:
    """No retry after request headers/body begin, server HTTP status, or SSE reception."""

    def __init__(self, endpoint, audit, *, client=None, wait=time.sleep):
        self.audit = audit
        self.safe_to_repeat = False
        self.endpoint = endpoint
        self.client = (
            client
            or httpx.Client(
                trust_env=False,
                follow_redirects=False,
                http2=False,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1, keepalive_expiry=300),
            )
        )
        self.wait = wait
        self.turn = None
        self.last_failure = None

    def record_failure(self, exc, *, phase, attempt, trace, response=None):
        """Public metadata for the coordinator; this does not authorize replay."""
        self.last_failure = {
            **classify_failure(exc), 'phase': phase, 'attempt': attempt,
            'new_connection': any(ev.endswith('connect_tcp.started') for ev in trace),
            'trace': list(trace[-32:]), 'causes': public_error(exc),
            'response_headers_seen': response is not None,
            'request_ids': response_ids(response) if response is not None else {},
        }
        return self.last_failure

    @contextmanager
    def stream(self, method, url, *, content, headers, timeout):
        if method != 'POST' or url != self.endpoint:
            raise ValueError('route not admitted')
        self.safe_to_repeat = False
        self.last_failure = None
        delays = (1, 2, 4, 8)
        response = None
        for attempt in range(1, len(delays) + 2):
            self.safe_to_repeat = False
            trace = []

            def observe(event, info):
                if re.fullmatch(r'(connection|http11|http2)\.[a-z_]+\.(started|failed)', event):
                    trace.append(event)
            req = self.client.build_request(
                method,
                url,
                content=content,
                headers=headers,
                timeout=timeout,
                extensions={'trace': observe},
            )
            try:
                response = self.client.send(req, stream=True, follow_redirects=False)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                started = any(('send_request_' in ev for ev in trace))
                establishing = any((ev.endswith(('connect_tcp.started', 'start_tls.started')) for ev in trace))
                verified = establishing and (not started)
                self.safe_to_repeat = verified
                causes = public_error(exc)
                diagnostic = self.record_failure(exc, phase='connection', attempt=attempt, trace=trace)
                cert_error = any((node['type'] == 'SSLCertVerificationError' for node in causes))
                repeat = verified and (not cert_error) and (attempt <= len(delays))
                wait = delays[attempt - 1] if repeat else None
                self.audit(
                    'transport_connect_failure',
                    turn=self.turn,
                    attempt=attempt,
                    trace=trace,
                    causes=causes,
                    proven_pre_write=verified,
                    retry=repeat,
                    backoff_seconds=wait,
                    diagnostic=diagnostic,
                )
                if not repeat:
                    raise
                self.wait(wait)
            except httpx.HTTPError as exc:
                diagnostic = self.record_failure(exc, phase='request_or_headers', attempt=attempt, trace=trace)
                self.audit(
                    'transport_nonretryable_error',
                    turn=self.turn,
                    attempt=attempt,
                    trace=trace,
                    causes=public_error(exc),
                    retry=False,
                    maybe_submitted=any(('send_request_' in ev for ev in trace)),
                    diagnostic=diagnostic,
                )
                raise
            else:
                self.last_failure = None
                self.audit(
                    'upstream_http_headers',
                    turn=self.turn,
                    attempt=attempt,
                    status=response.status_code,
                    new_connection=any((ev.endswith('connect_tcp.started') for ev in trace)),
                    request_ids=response_ids(response),
                )
                break
        body_finished = False
        try:
            yield response
            body_finished = True
        except httpx.HTTPError as exc:
            diagnostic = self.record_failure(exc, phase='response_body', attempt=attempt,
                                             trace=trace, response=response)
            self.audit(
                'transport_stream_interrupted',
                turn=self.turn,
                causes=public_error(exc),
                response_headers_seen=True,
                retry=False,
                diagnostic=diagnostic,
            )
            raise
        finally:
            try:
                response.close()
            except httpx.TransportError as cleanup_error:
                if body_finished:
                    raise
                # Preserve the active failure, including an upstream Retry-After.
                self.audit('response_cleanup_after_failure', turn=self.turn, causes=public_error(cleanup_error))
