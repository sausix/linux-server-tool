from common.identity import SESSION_COOKIE, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, MANAGED_HEADER
from session_helpers import session_cookie
import asyncio
import base64
import json
import socket
import os
import signal
import subprocess
import sys
import time

import pytest

from itsdangerous import TimestampSigner
from sqlalchemy import select
import uvicorn

from frontend.app import create_app
from frontend.config import Settings
from frontend.models import User
from frontend.server import FrontendServer


@pytest.mark.parametrize('reload', [False, True])
def test_cli_sigint_exits_without_traceback(tmp_path, reload):
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    environment = {key: value for key, value in os.environ.items() if not key.startswith('FRONTEND_')}
    environment.update(FRONTEND_SECRET_KEY='test-secret-key-that-is-long-enough-123456',
                       FRONTEND_DATABASE_URL=f'sqlite:///{tmp_path / "cli.db"}')
    log_path = tmp_path / 'frontend.log'
    with log_path.open('w+') as log:
        process = subprocess.Popen(
            [sys.executable, '-m', 'frontend.cli', '--port', str(port),
             '--reload' if reload else '--no-reload'],
            cwd=tmp_path, env=environment, stdout=log, stderr=log, start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 10
            while 'Application startup complete.' not in log_path.read_text():
                assert process.poll() is None, log_path.read_text()
                assert time.monotonic() < deadline, log_path.read_text()
                time.sleep(.05)
            os.killpg(process.pid, signal.SIGINT)
            assert process.wait(timeout=10) == 0, log_path.read_text()
            output = log_path.read_text()
            assert 'Application shutdown complete.' in output
            assert 'Traceback' not in output
            assert 'CancelledError' not in output
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)


def test_shutdown_closes_idle_event_stream(tmp_path):
    async def scenario():
        path = tmp_path / 'daemon.sock'

        async def daemon(reader, writer):
            try:
                await reader.readline()
                writer.write(b'{"jsonrpc":"2.0","result":{}}\n')
                await writer.drain()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()

        daemon_server = await asyncio.start_unix_server(daemon, str(path))
        settings = Settings(_env_file=None, environment='test',
            database_url=f'sqlite:///{tmp_path / "test.db"}',
            secret_key='test-secret-key-that-is-long-enough-123456',
            daemon_endpoint=f'unix://{path}')
        app = create_app(settings)
        app.state.daemon_client.ping = lambda: {'network_access': 'readonly', 'events': True}
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        server = FrontendServer(uvicorn.Config(app, log_level='error', timeout_graceful_shutdown=3))
        running = asyncio.create_task(server.serve(sockets=[listener]))
        writer = None
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(.01)
                with app.state.session_factory() as db:
                    user = db.scalar(select(User).where(User.username == 'admin'))
                    payload = base64.b64encode(json.dumps({'user_id': user.id}).encode())
                cookie = session_cookie(app, payload)
                reader, writer = await asyncio.open_connection('127.0.0.1', port)
                writer.write(f'GET /api/events HTTP/1.1\r\nHost: localhost\r\nCookie: {SESSION_COOKIE}={cookie}\r\n\r\n'.encode())
                await writer.drain()
                await reader.readuntil(b'event: ready')
                server.should_exit = True
                remaining = await reader.read()
                assert b'event: shutdown' in remaining
                await running
        finally:
            server.should_exit = True
            if writer:
                writer.close()
                await writer.wait_closed()
            await asyncio.wait_for(running, 5)
            listener.close()
            daemon_server.close()
            await daemon_server.wait_closed()

    asyncio.run(scenario())
