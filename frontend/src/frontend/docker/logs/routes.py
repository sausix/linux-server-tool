"""Authenticated log popups and JSON-RPC-to-SSE streaming without persistence."""

import asyncio
import json
import time
import secrets
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from common.client import DaemonUnavailable
from common.protocol import ProtocolError, MAX_MESSAGE_BYTES, encode_message, request as rpc_request
from frontend.server import read_stream_line
from frontend.security import csrf_token, csrf_is_valid


def install_log_routes(app, daemon, settings, render, get_db, require_docker):
    checkpoints = {}
    def target(request, status):
        values = {key: request.query_params.get(key, '') for key in ('scope', 'id', 'kind', 'name', 'service', 'service_id')}
        if values['scope'] not in {'stack', 'service', 'container'}:
            raise HTTPException(422, 'Invalid log scope')
        try:
            limit = int(request.query_params.get('limit', str(status.get('logs', {}).get('default_lines', 200))))
        except ValueError:
            raise HTTPException(422, 'Invalid line limit') from None
        if not 1 <= limit <= status.get('logs', {}).get('max_lines', 5000):
            raise HTTPException(422, 'Line limit is outside the supported range')
        return {**values, 'limit': limit}

    @app.get('/docker/logs')
    def log_window(request: Request, db=Depends(get_db)):
        user, status = require_docker(request, db)
        params = target(request, status)
        label = params['id'][:12] if params['scope'] == 'container' else ' / '.join(value for value in [params['name'], params['service'] if params['scope'] == 'service' else ''] if value)
        return render(request, 'logs.html', user=user, daemon_status=status, hide_search=True,
                      log_target=params, target_label=label, max_lines=status.get('logs', {}).get('max_lines', 5000))

    @app.get('/api/docker/logs')
    def recent_logs(request: Request, db=Depends(get_db)):
        user, status = require_docker(request, db)
        params = target(request, status)
        db.close()
        try:
            data = dict(daemon.call('docker.logs.tail', params))
            cursors = data.pop('cursors', {})
            now = time.monotonic()
            for key, checkpoint in list(checkpoints.items()):
                if checkpoint['expires'] < now:
                    checkpoints.pop(key, None)
            while len(checkpoints) >= 128:
                checkpoints.pop(next(iter(checkpoints)))
            token = secrets.token_urlsafe(32)
            checkpoints[token] = {'user': user.id, 'csrf': csrf_token(request.session), 'target': params,
                                  'cursors': cursors, 'expires': now + 600}
            return JSONResponse({**data, 'checkpoint': token}, headers={'Cache-Control': 'no-store'})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get('/api/docker/logs/live')
    def log_events(request: Request, db=Depends(get_db)):
        user, status = require_docker(request, db)
        params = target(request, status)
        token = request.query_params.get('checkpoint')
        if token:
            checkpoint = checkpoints.pop(token, None)
            if (not checkpoint or checkpoint['user'] != user.id or checkpoint['expires'] < time.monotonic()
                    or checkpoint['target'] != params or not csrf_is_valid(request.session, checkpoint['csrf'])):
                raise HTTPException(409, 'Log checkpoint expired or changed; fetch recent lines again')
            params['cursors'] = checkpoint['cursors']
        db.close()  # Do not retain a database connection for the lifetime of SSE.
        async def stream():
            writer = None
            try:
                endpoint = urlparse(settings.daemon_endpoint)
                if endpoint.scheme == 'unix':
                    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(endpoint.path, limit=MAX_MESSAGE_BYTES + 1), settings.daemon_timeout_seconds)
                else:
                    reader, writer = await asyncio.wait_for(asyncio.open_connection(endpoint.hostname, endpoint.port, limit=MAX_MESSAGE_BYTES + 1), settings.daemon_timeout_seconds)
                outgoing = rpc_request('docker.logs.subscribe', params, settings.daemon_token)
                writer.write(encode_message(outgoing)); await writer.drain()
                raw = await read_stream_line(reader, app.state.shutdown_event, settings.daemon_inventory_timeout_seconds, cancel_event=request.scope['stream_session_end'])
                if raw is None:
                    yield ('event: session-expired\ndata: {}\n\n' if request.scope['stream_session_end'].is_set() else 'event: shutdown\ndata: {}\n\n'); return
                acknowledgement = json.loads(raw)
                if acknowledgement.get('id') != outgoing['id'] or 'result' not in acknowledgement:
                    raise ValueError(acknowledgement.get('error', {}).get('message', 'Log subscription rejected'))
                yield 'event: ready\ndata: ' + json.dumps(acknowledgement['result']) + '\n\n'
                while True:
                    try:
                        raw = await read_stream_line(reader, app.state.shutdown_event, 15, cancel_event=request.scope['stream_session_end'])
                    except TimeoutError:
                        yield ': heartbeat\n\n'; continue
                    if raw is None:
                        yield ('event: session-expired\ndata: {}\n\n' if request.scope['stream_session_end'].is_set() else 'event: shutdown\ndata: {}\n\n'); return
                    if not raw:
                        raise ValueError('Daemon log connection closed')
                    message = json.loads(raw)
                    if message.get('method') == 'docker.log':
                        record = message.get('params', {})
                        kind = 'log' if record.get('type') == 'line' else 'log-warning'
                        yield 'event: ' + kind + '\ndata: ' + json.dumps(record.get('line') if kind == 'log' else record) + '\n\n'
            except (OSError, ValueError, TimeoutError) as exc:
                yield 'event: log-error\ndata: ' + json.dumps({'message': str(exc)}) + '\n\n'
            finally:
                if writer:
                    writer.close()
                    try:
                        await asyncio.wait_for(writer.wait_closed(), settings.daemon_timeout_seconds)
                    except (OSError, TimeoutError):
                        pass
        async def bound_stream():
            row = request.scope['session_record']
            async with app.state.sessions.monitor(row['id'], user.id) as ended:
                request.scope['stream_session_end'] = ended
                async for chunk in stream():
                    yield chunk
        return StreamingResponse(bound_stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
