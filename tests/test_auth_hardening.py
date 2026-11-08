from common.identity import SESSION_COOKIE, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, MANAGED_HEADER
import hashlib
import json
import re
import secrets
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import Mock

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from webauthn.helpers import bytes_to_base64url as b64, base64url_to_bytes as unb64

from frontend.app import create_app
from frontend.config import Settings
from frontend.models import User, SecurityKey, AuthSession, AuditOutbox, utcnow
from frontend.security import set_password

PASSWORD = 'test-current-password-12345'


def csrf(client, path='/login'):
    response = client.get(path)
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


@pytest.fixture
def application(tmp_path):
    @contextmanager
    def start(**overrides):
        settings = Settings(_env_file=None, environment='test', public_url='https://testserver', trusted_hosts='testserver',
            secret_key='test-secret-key-that-is-long-enough-123456', database_url=f'sqlite:///{tmp_path / (secrets.token_hex(8)+".db")}', **overrides)
        app = create_app(settings)
        app.state.daemon_client.ping = lambda: {'docker_access': 'readwrite', 'system_access': 'readonly', 'network_access': 'readonly', 'events': False}
        with TestClient(app, base_url='https://testserver', client=('198.51.100.10', 5000)) as client:
            with app.state.session_factory() as db:
                user = db.scalar(select(User).where(User.username == 'admin'))
                set_password(user, PASSWORD, 90); db.commit()
            yield app, client
    return start


def login(client, password=PASSWORD, token=None):
    return client.post('/login', data={'username': 'admin', 'password': password, 'csrf_token': token or csrf(client)}, follow_redirects=False)


def set_cookie(client, value):
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, value, domain='testserver.local', path='/')


def test_logout_and_password_change_revoke_sessions_server_side(application):
    with application() as (app, client):
        assert login(client).status_code == 303
        cookie = client.cookies.get(SESSION_COOKIE)
        token = csrf(client, '/settings/user')
        client.post('/logout', data={'csrf_token': token})
        set_cookie(client, cookie)
        assert client.get('/api/session').status_code == 401
        assert login(client).status_code == 303
        first = client.cookies.get(SESSION_COOKIE)
        uid = app.state.sessions.load(first)[1]['user_id']
        second, _, second_row = app.state.sessions.create(uid, {'user_id': uid, 'csrf_token': 'secondary-test-csrf'}, login_method='password')
        response = client.post('/settings/user/password', data={'current_password': PASSWORD, 'new_password': 'new-test-password-98765',
            'confirm_password': 'new-test-password-98765', 'csrf_token': csrf(client, '/settings/user')}, follow_redirects=False)
        assert response.headers['location'] == '/login?reason=password-changed'
        for stolen in (first, second):
            set_cookie(client, stolen)
            assert client.get('/api/session').status_code == 401
        assert not app.state.sessions.persist(second_row['id'], {'user_id': uid}, renew=True)
        client.cookies.clear()
        assert login(client, 'new-test-password-98765').status_code == 303


def test_password_change_required_only_profile_and_no_sensitive_reads(application):
    with application() as (app, client):
        login(client)
        with app.state.session_factory() as db:
            user = db.scalar(select(User).where(User.username == 'admin'))
            user.password_is_temporary = True; db.commit()
        assert client.get('/').url.path == '/settings/user'
        assert client.get('/docker/logs?scope=container&id=' + 'a'*64).url.path == '/settings/user'
        assert client.get('/api/docker/containers').status_code == 403
        assert client.get('/docs', follow_redirects=False).headers['location'] == '/settings/user'
        page = client.get('/settings/user')
        assert 'Settings · User' in page.text and 'data-security-keys' not in page.text
        assert 'data-stack-menu' not in page.text and 'Network control' not in page.text


def test_failed_logins_are_persistent_and_audited(application, monkeypatch):
    with application(login_failure_limit=3, login_block_seconds=30) as (app, client):
        token = csrf(client)
        for _ in range(3):
            assert login(client, 'incorrect-password', token).status_code == 200
        blocked = login(client, PASSWORD, token)
        assert blocked.status_code == 429 and int(blocked.headers['retry-after']) > 0
        from frontend.login_policy import LoginThrottle
        fresh = LoginThrottle(app.state.session_factory, app.state.settings, app.state.settings.secret_key)
        assert fresh.blocked('admin', '203.0.113.55') > 0  # Account bucket also covers other IPs.
        with app.state.session_factory() as db:
            events = [json.loads(item.payload) for item in db.scalars(select(AuditOutbox))]
        failures = [event for event in events if event['event'] == 'login.failed']
        assert len(failures) == 3 and failures[0]['source_ip'] == '198.51.100.10'
        assert failures[0]['user_agent'] and failures[0]['login_method'] == 'password'
        assert PASSWORD not in json.dumps(events) and 'incorrect-password' not in json.dumps(events)


def test_source_network_policy_and_spoofed_forwarded_header(application):
    with application(login_allowed_networks=['203.0.113.0/24']) as (_, client):
        token = csrf(client)
        response = client.post('/login', headers={'X-Forwarded-For': '203.0.113.10'},
            data={'username': 'admin', 'password': PASSWORD, 'csrf_token': token})
        assert response.status_code == 403
    with application(login_rules=[{'users': ['admin'], 'networks': ['198.51.100.0/24'], 'webauthn': 'required'}, {'action': 'deny'}]) as (_, client):
        assert login(client).headers['location'] == '/settings/user'
        assert client.get('/api/docker/containers').status_code == 403
        assert 'Register a FIDO2 security key' in client.get('/settings/user').text


def test_limits_and_no_store_before_form_parsing(application):
    with application(login_max_bytes=1024) as (_, client):
        assert client.post('/login', content=b'x'*1025, headers={'Content-Type': 'application/x-www-form-urlencoded'}).status_code == 413
        assert client.post('/login', files={'unused': ('test.bin', b'x'*2000)}).status_code == 415
        for path in ('/login', '/password', '/healthz', '/api/session'):
            assert client.get(path).headers['cache-control'] == 'no-store'


class Token:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.id = secrets.token_bytes(32)
        self.count = 0

    def client_data(self, kind, challenge, origin='https://testserver'):
        return json.dumps({'type': kind, 'challenge': challenge, 'origin': origin, 'crossOrigin': False}, separators=(',', ':')).encode()

    def register(self, challenge, *, origin='https://testserver', verified=True):
        numbers = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, 'big'), -3: numbers.y.to_bytes(32, 'big')})
        flags = 0x41 | (4 if verified else 0)
        data = hashlib.sha256(b'testserver').digest() + bytes([flags]) + bytes(4) + bytes(16) + len(self.id).to_bytes(2, 'big') + self.id + cose
        attestation = cbor2.dumps({'fmt': 'none', 'attStmt': {}, 'authData': data})
        return {'id': b64(self.id), 'rawId': b64(self.id), 'type': 'public-key', 'response': {
            'clientDataJSON': b64(self.client_data('webauthn.create', challenge, origin)), 'attestationObject': b64(attestation), 'transports': ['usb']}}

    def authenticate(self, challenge, *, origin='https://testserver', verified=True):
        self.count += 1
        data = hashlib.sha256(b'testserver').digest() + bytes([1 | (4 if verified else 0)]) + self.count.to_bytes(4, 'big')
        client = self.client_data('webauthn.get', challenge, origin)
        signature = self.key.sign(data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {'id': b64(self.id), 'rawId': b64(self.id), 'type': 'public-key', 'response': {
            'clientDataJSON': b64(client), 'authenticatorData': b64(data), 'signature': b64(signature)}}


def post(client, url, token, **body):
    return client.post(url, json={'csrf_token': token, **body})


def enroll(client, key):
    token = csrf(client, '/settings/user')
    challenge = post(client, '/settings/user/security-keys/register/options', token, current_password=PASSWORD, name='Primary security key')
    assert challenge.status_code == 200, challenge.text
    value = challenge.json()
    return post(client, '/settings/user/security-keys/register/verify', token, challenge_id=value['challenge_id'], credential=key.register(value['publicKey']['challenge']))


def login_with_key(client, key):
    response = login(client)
    assert response.headers['location'] == '/login?step=security-key'
    token = csrf(client)
    value = post(client, '/login/webauthn/options', token).json()
    return post(client, '/login/webauthn/verify', token, challenge_id=value['challenge_id'], credential=key.authenticate(value['publicKey']['challenge']))


def test_real_webauthn_registration_login_and_one_use_challenge(application):
    with application() as (app, client):
        login(client); old = client.cookies.get(SESSION_COOKIE); key = Token()
        assert enroll(client, key).status_code == 200
        set_cookie(client, old)
        assert client.get('/api/session').status_code == 401
        client.cookies.clear()
        assert login_with_key(client, key).status_code == 200
        assert client.get('/api/session').status_code == 200
        with app.state.session_factory() as db:
            stored = db.scalar(select(SecurityKey))
            assert stored.sign_count == 1 and stored.user_id
        assert client.get('/settings/user').headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('wrong', ['origin', 'verification', 'challenge'])
def test_webauthn_rejects_invalid_registration_and_consumes_challenge(application, wrong):
    with application() as (app, client):
        login(client); token = csrf(client, '/settings/user'); key = Token()
        value = post(client, '/settings/user/security-keys/register/options', token, current_password=PASSWORD, name='Test key').json()
        credential = key.register('wrong' if wrong == 'challenge' else value['publicKey']['challenge'],
                                  origin='https://foreign.example' if wrong == 'origin' else 'https://testserver', verified=wrong != 'verification')
        assert post(client, '/settings/user/security-keys/register/verify', token, challenge_id=value['challenge_id'], credential=credential).status_code == 403
        assert post(client, '/settings/user/security-keys/register/verify', token, challenge_id=value['challenge_id'], credential=key.register(value['publicKey']['challenge'])).status_code == 403
        with app.state.session_factory() as db:
            assert db.scalar(select(func.count()).select_from(SecurityKey)) == 0


@pytest.mark.parametrize('wrong', ['origin', 'verification', 'signature'])
def test_webauthn_rejects_invalid_assertions(application, wrong):
    with application() as (_, client):
        login(client); key = Token(); assert enroll(client, key).status_code == 200
        login(client); token = csrf(client)
        value = post(client, '/login/webauthn/options', token).json()
        credential = key.authenticate(value['publicKey']['challenge'],
            origin='https://foreign.example' if wrong == 'origin' else 'https://testserver', verified=wrong != 'verification')
        if wrong == 'signature':
            credential['response']['signature'] = b64(b'invalid signature')
        assert post(client, '/login/webauthn/verify', token, challenge_id=value['challenge_id'], credential=credential).status_code == 403
        assert client.get('/api/session').status_code == 401
        assert post(client, '/login/webauthn/verify', token, challenge_id=value['challenge_id'],
                    credential=key.authenticate(value['publicKey']['challenge'])).status_code == 403


def test_key_changes_require_existing_factor_and_purpose_bound_grant(application):
    with application() as (app, client):
        login(client); key = Token(); enroll(client, key); login_with_key(client, key)
        token = csrf(client, '/settings/user')
        path = '/settings/user/security-keys/'
        assert post(client, path+'register/options', token, current_password=PASSWORD, name='Second').status_code == 403
        with app.state.session_factory() as db:
            key_id = db.scalar(select(SecurityKey.id))
        assert post(client, path+'remove', token, current_password=PASSWORD, key_id=key_id).status_code == 403
        value = post(client, path+'authorize/options', token, current_password=PASSWORD, operation='register').json()
        assertion = key.authenticate(value['publicKey']['challenge'])
        grant = post(client, path+'authorize/verify', token, operation='register', challenge_id=value['challenge_id'], credential=assertion).json()['grant']
        # A registration grant cannot remove a credential.
        assert post(client, path+'remove', token, current_password=PASSWORD, key_id=key_id, grant=grant).status_code == 403
        second = Token()
        value = post(client, path+'register/options', token, current_password=PASSWORD, name='Second', grant=grant).json()
        assert post(client, path+'register/options', token, current_password=PASSWORD, name='Replay', grant=grant).status_code == 403
        assert post(client, path+'register/verify', token, challenge_id=value['challenge_id'], credential=second.register(value['publicKey']['challenge'])).status_code == 200
        assert client.get('/api/session').status_code == 401
        assert login_with_key(client, second).status_code == 200


def test_required_mode_prevents_last_key_removal_and_off_mode_disables_enrollment(application):
    with application(webauthn_mode='required') as (_, client):
        login(client); key = Token(); enroll(client, key); login_with_key(client, key)
        token = csrf(client, '/settings/user')
        assert post(client, '/settings/user/security-keys/remove', token, current_password=PASSWORD, key_id=1).status_code == 409
    with application(webauthn_mode='off') as (_, client):
        login(client)
        assert post(client, '/settings/user/security-keys/register/options', csrf(client, '/settings/user'), current_password=PASSWORD, name='Disabled').status_code == 403


def test_challenge_is_bound_to_session(application):
    with application() as (app, client):
        login(client); key = Token(); enroll(client, key); login(client)
        first = client.cookies.get(SESSION_COOKIE); token = csrf(client)
        value = post(client, '/login/webauthn/options', token).json()
        client.cookies.clear(); login(client); second_token = csrf(client)
        credential = key.authenticate(value['publicKey']['challenge'])
        assert post(client, '/login/webauthn/verify', second_token, challenge_id=value['challenge_id'], credential=credential).status_code == 403
        set_cookie(client, first)
        assert post(client, '/login/webauthn/verify', token, challenge_id=value['challenge_id'], credential=credential).status_code == 200


@pytest.mark.parametrize('url', ['/api/docker/logs/live?scope=container&id='+'a'*64, '/api/events?component=docker'])
def test_live_stream_closes_when_http_logout_revokes_session(application, monkeypatch, url):
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import AsyncMock
    ready = threading.Event()
    writer = Mock(); writer.drain = AsyncMock(); writer.wait_closed = AsyncMock()
    class Reader:
        first = True
        async def readline(self):
            if self.first:
                self.first = False
                outgoing = json.loads(writer.write.call_args.args[0])
                return json.dumps({'jsonrpc': '2.0', 'id': outgoing['id'], 'result': {}}).encode()
            ready.set()
            await asyncio.Event().wait()
    monkeypatch.setattr('frontend.docker.logs.routes.asyncio.open_unix_connection', AsyncMock(return_value=(Reader(), writer)))
    with application() as (app, client):
        app.state.daemon_client.ping = lambda: {'docker_access': 'readonly', 'events': True, 'network_access': 'disabled'}
        login(client); token = csrf(client, '/settings/user')
        with ThreadPoolExecutor() as executor:
            stream = executor.submit(client.get, url)
            assert ready.wait(3)
            assert client.post('/logout', data={'csrf_token': token}, follow_redirects=False).status_code == 303
            response = stream.result(timeout=3)
        assert 'event: session-expired' in response.text
        writer.close.assert_called_once()


def test_shared_database_revocation_interrupts_another_worker(application):
    import asyncio
    from frontend.sessions import SessionStore
    with application() as (app, client):
        login(client)
        record = app.state.sessions.load(client.cookies.get(SESSION_COOKIE))[1]
        other = SessionStore(app.state.session_factory, app.state.settings, app.state.settings.secret_key, app.state.security_audit)
        async def check():
            async with app.state.sessions.monitor(record['id'], record['user_id']) as ended:
                await asyncio.to_thread(other.revoke, record['id'])
                await asyncio.wait_for(ended.wait(), 2)
        asyncio.run(check())


def test_security_audit_retries_and_daemon_validates_schema(application, caplog):
    from backend.daemon import DaemonServer
    from backend.daemon_config import DaemonSettings
    from common.protocol import ProtocolError
    with application() as (app, client):
        audit = app.state.security_audit
        audit.close()  # Exercise outbox delivery deterministically.
        login(client, password='wrong-password')
        with app.state.session_factory() as db:
            assert db.scalar(select(func.count()).select_from(AuditOutbox)) > 0
        audit.stop_event.clear()
        audit.client.call = Mock(side_effect=OSError('unavailable'))
        audit.flush()
        daemon = DaemonServer(DaemonSettings(_env_file=None, docker_access='disabled', network_access='disabled', system_access='disabled', stack_database=app.state.settings.database_url.removeprefix('sqlite:///') + '.stacks'))
        audit.client.call = daemon.dispatch
        audit.flush()
        with app.state.session_factory() as db:
            assert db.scalar(select(func.count()).select_from(AuditOutbox)) == 0
        assert any(item.name == 'backend.security' and 'login.failed' in item.message for item in caplog.records)
        with pytest.raises(ProtocolError) as exc:
            daemon.dispatch('security.event', {'event': {'event': 'login.failed', 'password': 'secret'}})
        assert exc.value.code == -32602


def test_security_configuration_validates_networks_and_cli_precedence(monkeypatch):
    from pydantic import ValidationError
    from frontend.cli import build_parser
    monkeypatch.setenv('FRONTEND_WEBAUTHN_MODE', 'required')
    monkeypatch.setenv('FRONTEND_LOGIN_ALLOWED_NETWORKS', '["192.0.2.0/24"]')
    args = {key: value for key, value in vars(build_parser().parse_args(['--webauthn-mode', 'off', '--login-allowed-networks', '["2001:db8::/32"]'])).items() if value is not None}
    args.pop('reload', None)
    value = Settings(_env_file=None, **args)
    assert value.webauthn_mode == 'off' and value.login_allowed_networks == ['2001:db8::/32']
    for options in ({'login_allowed_networks': ['bad']}, {'login_rules': [{'networks': ['bad']}]}, {'post_max_bytes': 64*1024*1024}):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **options)


def test_chunked_request_is_bounded_and_validation_errors_do_not_echo_secrets(application):
    with application(login_max_bytes=1024) as (_, client):
        assert client.post('/login', content=iter([b'a'*600, b'b'*600]), headers={'Content-Type': 'application/x-www-form-urlencoded'}).status_code == 413
        response = client.post('/login', data={'password': 'never-echo-this-password'})
        assert response.status_code == 422 and 'never-echo' not in response.text


def test_absolute_session_expiry_cannot_be_renewed(application, monkeypatch):
    import frontend.sessions as session_module
    with application(session_absolute_max_age_seconds=60) as (app, client):
        stamp = session_module.now()
        monkeypatch.setattr(session_module, 'now', lambda: stamp)
        login(client)
        stolen = client.cookies.get(SESSION_COOKIE)
        absolute = app.state.sessions.load(stolen)[1]['absolute_expires_at']
        monkeypatch.setattr(session_module, 'now', lambda: stamp + 59)
        assert client.get('/api/session').status_code == 200
        assert app.state.sessions.load(client.cookies.get(SESSION_COOKIE))[1]['expires_at'] == absolute
        monkeypatch.setattr(session_module, 'now', lambda: stamp + 61)
        assert client.get('/api/session').status_code == 401
        assert app.state.sessions.load(stolen) is None


def test_upload_budgets_apply_to_expanded_archives_and_file_counts():
    import io
    import zipfile
    from common.stack_files import upload_bundle
    def archive(files):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as value:
            for name, data in files.items():
                value.writestr(name, data)
        return stream.getvalue()
    packed = archive({'compose.yaml': b'#'*800, 'extra.txt': b'#'*800})
    assert len(packed) < 1024
    with pytest.raises(ValueError, match='1024-byte'):
        upload_bundle('stack.zip', packed, max_bytes=1024)
    with pytest.raises(ValueError, match='too many files'):
        upload_bundle('stack.zip', archive({'compose.yaml': b'services: {}', 'extra.txt': b'text'}), max_files=1)


def test_non_ascii_csrf_is_rejected_without_server_error(application):
    with application() as (_, client):
        csrf(client)
        assert login(client, token='not-ascii-ä').status_code == 403
