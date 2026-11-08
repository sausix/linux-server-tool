"""Exercise the shipped settings/login UI with Chromium's virtual FIDO2 token."""
import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.request

import pytest
from sqlalchemy import select
import uvicorn

from frontend.app import create_app
from frontend.config import Settings
from frontend.models import User
from frontend.security import set_password


def test_security_key_registration_and_login_in_browser(tmp_path):
    browser = shutil.which('chromium') or shutil.which('chromium-browser')
    if not browser:
        pytest.skip('Chromium is required for WebAuthn UI testing')
    websocket = pytest.importorskip('websockets.sync.client')
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    app = create_app(Settings(_env_file=None, environment='test', public_url=f'http://localhost:{port}',
        secret_key='test-secret-key-that-is-long-enough-123456', database_url=f'sqlite:///{tmp_path/"users.db"}'))
    app.state.daemon_client.ping = lambda: {'docker_access': 'disabled', 'network_access': 'disabled', 'system_access': 'disabled', 'events': False}
    server = uvicorn.Server(uvicorn.Config(app, loop='asyncio', log_level='error', lifespan='on'))
    worker = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    worker.start()
    process = None
    try:
        deadline = time.monotonic() + 8
        while not server.started:
            assert worker.is_alive() and time.monotonic() < deadline
            time.sleep(.02)
        password = 'browser-security-test-password-12345'
        with app.state.session_factory() as db:
            user = db.scalar(select(User).where(User.username == 'admin'))
            set_password(user, password, 90); db.commit()
        process = subprocess.Popen([browser, '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
            '--no-proxy-server', '--no-first-run', '--no-default-browser-check', '--user-data-dir='+str(tmp_path/'browser'),
            '--remote-debugging-port=0', 'about:blank'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        active = tmp_path/'browser/DevToolsActivePort'
        deadline = time.monotonic() + 8
        while not active.exists():
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(.02)
        debug_port = active.read_text().splitlines()[0]
        with urllib.request.urlopen(f'http://127.0.0.1:{debug_port}/json/list', timeout=3) as response:
            target = next(item for item in json.load(response) if item['type'] == 'page')
        with websocket.connect(target['webSocketDebuggerUrl'], open_timeout=3) as connection:
            identity = 0
            def command(method, params):
                nonlocal identity
                identity += 1
                connection.send(json.dumps({'id': identity, 'method': method, 'params': params}))
                while True:
                    result = json.loads(connection.recv(timeout=8))
                    if result.get('id') == identity:
                        assert 'error' not in result, result
                        return result.get('result', {})
            def evaluate(expression):
                result = command('Runtime.evaluate', {'expression': expression, 'returnByValue': True})
                assert 'exceptionDetails' not in result, result
                return result.get('result', {}).get('value')
            def until(expression):
                deadline = time.monotonic() + 8
                while not evaluate(expression):
                    assert time.monotonic() < deadline, evaluate('document.body.innerText')
                    time.sleep(.03)
            def click(selector):
                point = evaluate("(() => {const e=document.querySelector("+json.dumps(selector)+");e.scrollIntoView();const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()")
                for kind in ('mousePressed', 'mouseReleased'):
                    command('Input.dispatchMouseEvent', {'type': kind, **point, 'button': 'left', 'clickCount': 1})
            command('WebAuthn.enable', {})
            command('WebAuthn.addVirtualAuthenticator', {'options': {'protocol': 'ctap2', 'transport': 'usb',
                'hasResidentKey': False, 'hasUserVerification': True, 'isUserVerified': True, 'automaticPresenceSimulation': True}})
            command('Page.navigate', {'url': f'http://localhost:{port}/login'})
            until("!!document.querySelector('#username')")
            evaluate("document.querySelector('#username').value='admin';document.querySelector('#password').value="+json.dumps(password))
            click('button[type=submit]')
            until("location.pathname === '/'")
            command('Page.navigate', {'url': f'http://localhost:{port}/settings/user'})
            until("document.readyState === 'complete' && !!document.querySelector('[data-register-key]')")
            evaluate("document.querySelector('#key-password').value="+json.dumps(password)+";document.querySelector('#key-name').value='Virtual FIDO2 token'")
            click('[data-register-key] button[type=submit]')
            until("location.pathname === '/login' && !!document.querySelector('#username')")
            evaluate("document.querySelector('#username').value='admin';document.querySelector('#password').value="+json.dumps(password))
            click('button[type=submit]')
            until("document.readyState === 'complete' && !!document.querySelector('[data-webauthn-login]')")
            click('[data-webauthn-login] button')
            until("location.pathname === '/'")
            command('Page.navigate', {'url': f'http://localhost:{port}/settings/user'})
            until("!!document.body && document.body.innerText.includes('Virtual FIDO2 token') && document.body.innerText.includes('password+webauthn')")
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)
        server.should_exit = True
        worker.join(timeout=5)
        listener.close()
