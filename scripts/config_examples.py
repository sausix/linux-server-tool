#!/usr/bin/env python3
"""Keep complete ENV references and Compose environment defaults aligned."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from frontend.config import Settings
from backend.daemon_config import DaemonSettings

ROOT = Path(__file__).resolve().parents[1]
MODELS = {'FRONTEND': Settings, 'BACKEND': DaemonSettings}
COMPOSE_DEFAULTS = {
    'FRONTEND': {'app_name': '', 'environment': 'production', 'bind_host': '0.0.0.0',
                 'database_url': 'sqlite:////frontend/data/frontend.db'},
    'BACKEND': {'docker_endpoint': 'unix:///run/docker.sock', 'system_host_root': '/host/root',
                'host_etc_directory': '/host/etc', 'networkd_directory': '/host/etc/systemd/network',
                'interfaces_file': '/host/etc/network/interfaces'},
}
HELPERS = {
    'APPLICATION_IMAGE': 'application:local',
    'FRONTEND_PUBLISHED_BIND': '127.0.0.1', 'FRONTEND_PUBLISHED_PORT': '8000',
    'FRONTEND_DATA_VOLUME': '', 'BACKEND_STATE_VOLUME': '',
    'BACKEND_SOCKET_GID': '2000', 'BACKEND_RUN_DIRECTORY': './lsdfm-run',
    'BACKEND_DOCKER_SOCKET_PATH': '/run/docker.sock', 'BACKEND_HOST_FILESYSTEM_ROOT': '/',
}
GROUPS = {
    'FRONTEND': {'app_name': 'Identity, runtime and database', 'session_max_age_seconds': 'Sessions',
                 'login_allowed_networks': 'Login policy and WebAuthn',
                 'request_max_bytes': 'Request and upload limits', 'password_max_age_days': 'Password policy',
                 'http_policy': 'HTTPS, cookies and reverse proxy',
                 'daemon_endpoint': 'Daemon connection', 'shutdown_timeout_seconds': 'Shutdown'},
    'BACKEND': {'transport': 'Listener, authentication and logging', 'docker_access': 'Component access',
                'system_host_root': 'Host mapping and per-feature access', 'docker_endpoint': 'Docker Engine',
                'docker_log_max_lines': 'Docker logs', 'docker_api_version': 'Docker API and TLS',
                'stack_database': 'Compose definitions, sources and lifecycle',
                'networkd_directory': 'Persistent Linux configuration and command limits',
                'events_enabled': 'Event subscriptions'},
}


def encode(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (list, dict)):
        return json.dumps(value, default=str, separators=(',', ':'))
    return str(value)


def defaults(component, compose=False):
    values = {name: field.default for name, field in MODELS[component].model_fields.items()}
    if compose:
        values.update(COMPOSE_DEFAULTS[component])
    return {name: encode(value) for name, value in values.items()}


def env_quote(value):
    # Compatible with dotenv, Compose and systemd EnvironmentFile syntax.
    return "'" + value.replace("'", "'\\''") + "'" if value and any(c in value for c in ' \t\"$#') else value


def example(components, *, compose=False):
    text = ['# Complete configuration reference; every value matches its context default.',
            '# A deployment .env only needs overrides. Blank optional values mean unset.',
            '# Missing files and unchanged examples produce the same effective settings.',
            '# Production still requires a unique FRONTEND_SECRET_KEY; no shared secret is supplied.',
            '# JSON lists use JSON syntax. Paths must be reachable in the process/container.', '']
    for component in components:
        text.append(f'{component}_ENV_PREFIX={component}_')
    text += ['', '# Shared component switches', 'DOCKER_SUPPORT=true', 'NETWORK_SUPPORT=true', 'SYSTEM_SUPPORT=true']
    for component in components:
        for field, value in defaults(component, compose).items():
            if field.endswith('_support'):
                continue
            if field in GROUPS[component]:
                text += ['', '# ' + component.title() + ': ' + GROUPS[component][field]]
            text.append(component + '_' + field.upper() + '=' + env_quote(value))
    if compose:
        text += ['', '# Compose-only inputs (not application Settings).',
                 '# Empty volume selectors use <Compose project>_frontend-data / _backend-state.',
                 '# Set COMPOSE_PROJECT_NAME explicitly only when choosing a project name.',
                 '# COMPOSE_PROJECT_NAME=your-deployment']
        text += [key + '=' + env_quote(value) for key, value in HELPERS.items()]
    return '\n'.join(text) + '\n'


def environment(component, *, minimal=False):
    values = defaults(component, compose=True)
    fixed = {}
    if minimal:
        fixed = {'docker_support': 'true', 'network_support': 'false', 'system_support': 'false',
                 'transport': 'unix', 'unix_socket': '/run/lsdfm/backend.sock',
                 'compose_host_root': '/unavailable/compose-sources', 'compose_host_read_only': 'true',
                 'compose_allowed_roots': '["${BACKEND_COMPOSE_ROOT:-/var/lib/lsdfm/compose}"]',
                 'compose_search_directories': '[]', 'host_etc_directory': '/unavailable/host-etc',
                 'system_host_root': '', 'networkd_directory': '/unavailable/networkd',
                 'interfaces_file': '/unavailable/interfaces', 'docker_endpoint': 'unix:///run/docker.sock'}
    lines = ['    environment:', '      # All component settings; defaults match compose.env.example.',
             f'      - {component}_ENV_PREFIX=${{{component}_ENV_PREFIX:-{component}_}}']
    for field, value in values.items():
        key = field.upper() if field.endswith('_support') else component + '_' + field.upper()
        target = key if field.endswith('_support') else '${' + component + '_ENV_PREFIX:-' + component + '_}' + field.upper()
        if field in fixed:
            expression = fixed[field]
        elif component == 'BACKEND' and field == 'access_token':
            expression = '${BACKEND_ACCESS_TOKEN:-${FRONTEND_DAEMON_TOKEN:-}}'
        elif component == 'FRONTEND' and field == 'secret_key':
            expression = '${FRONTEND_SECRET_KEY:?Set FRONTEND_SECRET_KEY to a unique secret of at least 32 characters}'
        else:
            expression = '${' + key + ':-' + value + '}'
        lines.append('      - ' + json.dumps(target + '=' + expression))
    return '\n'.join(lines) + '\n'


def outputs():
    result = {
        ROOT/'docs/deployment/compose.env.example': example(('FRONTEND', 'BACKEND'), compose=True),
        ROOT/'docs/frontend/frontend.env.example': example(('FRONTEND',)),
        ROOT/'backend/systemd/backend.env.example': example(('BACKEND',)),
    }
    for filename, component, minimal in [('compose.web.yaml', 'FRONTEND', False),
                                           ('compose.daemon.yaml', 'BACKEND', False),
                                           ('compose.docker.yaml', 'BACKEND', True)]:
        path = ROOT/'docs/deployment'/filename
        text = path.read_text()
        start = text.index('    environment:', text.index('  backend:') if minimal else 0)
        end = text.index('    volumes:', start)
        result[path] = text[:start] + environment(component, minimal=minimal) + text[end:]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    stale = []
    for path, content in outputs().items():
        if not path.exists() or path.read_text() != content:
            stale.append(str(path.relative_to(ROOT)))
            if not args.check:
                path.write_text(content)
    if args.check and stale:
        parser.exit(1, 'Configuration references need regeneration: ' + ', '.join(stale) + '\n')
    print('Configuration references are aligned.' if args.check else 'Configuration references updated.')


if __name__ == '__main__':
    main()
