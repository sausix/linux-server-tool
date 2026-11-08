"""Admin-only, secret-free security configuration assessments."""
import json
import socket
from pathlib import Path
from unittest.mock import Mock

from sqlalchemy import select

from test_auth_hardening import application, login
from frontend.models import User
from backend.daemon import DaemonServer
from backend.daemon_config import DaemonSettings
from backend.security_checks import assess as daemon_assess


def by_id(checks):
    return {item['id']: item for item in checks}


def test_admin_security_page_and_navigation(application):
    with application() as (app, client):
        daemon = Mock(return_value={'checks': [{'id': 'sample', 'title': 'Daemon check', 'status': 'warning',
            'observation': '<script>untrusted</script>', 'recommendation': 'Review configuration'}]})
        app.state.daemon_client.call = daemon
        assert client.get('/settings/security', follow_redirects=False).status_code == 303
        daemon.assert_not_called()
        login(client)
        page = client.get('/settings/security')
        assert page.status_code == 200 and page.headers['cache-control'] == 'no-store'
        assert 'Security · Settings' in page.text and 'Run checks again' in page.text
        assert 'Frontend' in page.text and 'Daemon check' in page.text
        assert '&lt;script&gt;untrusted&lt;/script&gt;' in page.text
        assert '<script>untrusted</script>' not in page.text
        assert 'data-inventory-search' not in page.text
        assert 'href="/settings/security" aria-current="page"' in page.text
        sidebar = page.text.split('<aside', 1)[1].split('</aside>', 1)[0]
        assert 'href="/settings/user" aria-current="page"' not in sidebar
        assert app.state.settings.secret_key not in page.text
        daemon.assert_called_once_with('security.checks', {})


def test_security_page_denies_regular_users_and_restricted_accounts(application):
    with application() as (app, client):
        login(client)
        app.state.daemon_client.call = Mock()
        with app.state.session_factory() as db:
            user = db.scalar(select(User)); user.is_admin = False; db.commit()
        assert client.get('/settings/security').status_code == 403
        assert 'href="/settings/security"' not in client.get('/settings/user').text
        app.state.daemon_client.call.assert_not_called()
        with app.state.session_factory() as db:
            user = db.scalar(select(User)); user.is_admin = True; user.password_is_temporary = True; db.commit()
        response = client.get('/settings/security', follow_redirects=False)
        assert response.status_code == 303 and response.headers['location'] == '/settings/user'
        assert 'href="/settings/security"' not in client.get('/settings/user').text
        app.state.daemon_client.call.assert_not_called()


def test_daemon_outage_or_incompatible_report_keeps_frontend_checks(application):
    with application() as (app, client):
        login(client)
        from common.client import DaemonUnavailable
        for response in (DaemonUnavailable('secret-from-remote-error'), {}, {'checks': [{'status': 'invalid'}]}):
            app.state.daemon_client.call = Mock(side_effect=response) if isinstance(response, Exception) else Mock(return_value=response)
            page = client.get('/settings/security')
            assert page.status_code == 200
            assert 'Secure session cookie' in page.text and 'Daemon assessment unavailable' in page.text
            assert 'secret-from-remote-error' not in page.text


def test_frontend_risky_settings_are_assessed_without_secrets(application):
    from frontend.security_checks import assess
    from starlette.requests import Request
    with application(cookie_secure='never', forwarded_allow_ips='*',
                     daemon_endpoint='tcp://localhost:9876', daemon_token='private-daemon-token') as (app, client):
        app.state.settings.trusted_hosts = '*'
        request = Request({'type': 'http', 'scheme': 'https', 'server': ('testserver', 443), 'path': '/', 'headers': []})
        with app.state.session_factory() as db:
            checks = by_id(assess(app.state.settings, request, db))
        assert checks['cookie']['status'] == 'critical'
        assert checks['proxy']['status'] == 'critical'
        assert checks['hosts']['status'] == 'warning'
        assert checks['daemon-channel']['status'] == 'critical'
        assert checks['external']['status'] == 'unknown'
        serialized = json.dumps(checks)
        assert 'private-daemon-token' not in serialized and app.state.settings.secret_key not in serialized


def test_daemon_assessment_is_available_with_components_disabled_and_no_secrets(tmp_path):
    settings = DaemonSettings(_env_file=None, docker_access='disabled', network_access='disabled', system_access='disabled',
        transport='tcp', access_token='secret-daemon-token-12345678901234567890', stack_database=tmp_path/'state.db')
    daemon = DaemonServer(settings)
    result = daemon.dispatch('security.checks', {})
    checks = by_id(result['checks'])
    assert checks['transport']['status'] == 'critical'
    assert checks['access']['status'] == 'pass'
    assert checks['token']['status'] == 'pass'
    assert settings.access_token not in json.dumps(result)
    assert 'docker-transport' not in checks


def test_daemon_observes_actual_socket_and_database_modes(tmp_path):
    path = tmp_path/'daemon.sock'
    database = tmp_path/'state.db'; database.touch(); database.chmod(0o644)
    settings = DaemonSettings(_env_file=None, unix_socket=path, stack_database=database)
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(path)); path.chmod(0o666)
        checks = by_id(daemon_assess(settings)['checks'])
        assert checks['socket']['status'] == 'critical'
        assert checks['state-file']['status'] == 'warning'
        path.chmod(0o660); database.chmod(0o600)
        checks = by_id(daemon_assess(settings)['checks'])
        assert checks['socket']['status'] == 'pass' and checks['state-file']['status'] == 'pass'


def test_docker_tls_assessment_matches_actual_transport_selection(tmp_path):
    for endpoint, expected in [('http://docker:2375', 'critical'), ('tcp://docker:2375', 'critical'),
                               ('tcp://docker:2376', 'pass'), ('https://docker:2376', 'pass')]:
        settings = DaemonSettings(_env_file=None, docker_endpoint=endpoint,
            docker_tls_cert=tmp_path/'cert.pem', docker_tls_key=tmp_path/'key.pem', stack_database=tmp_path/'state.db')
        check = by_id(daemon_assess(settings)['checks'])['docker-transport']
        assert check['status'] == expected


def test_default_deny_rules_can_require_mfa_without_global_required_mode(application):
    from frontend.security_checks import assess
    from starlette.requests import Request
    with application(login_rules=[{'users': ['admin'], 'webauthn': 'required'}, {'action': 'deny'}]) as (app, _):
        request = Request({'type': 'http', 'scheme': 'https', 'server': ('testserver', 443), 'path': '/', 'headers': []})
        with app.state.session_factory() as db:
            assert by_id(assess(app.state.settings, request, db))['mfa-policy']['status'] == 'pass'
