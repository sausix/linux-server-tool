"""Local daemon configuration assessment; never reads secret file contents."""
import os
import stat
from urllib.parse import urlparse
from common.security_checks import finding, private_file


def assess(settings):
    checks = [finding('environment-prefix', 'Environment namespace', 'info',
        'Active prefix: ' + (settings.env_prefix or '(none)'),
        'Set the component ENV_PREFIX selector or use --env-prefix. Custom prefixes are isolated from compatibility names; shared support switches remain unprefixed.')]
    checks.append(finding('components', 'Component support switches', 'info',
        ', '.join(name + ': ' + ('enabled' if getattr(settings, name + '_support') else 'disabled') for name in ('docker', 'network', 'system')),
        'Use the same DOCKER_SUPPORT, NETWORK_SUPPORT and SYSTEM_SUPPORT values for web and daemon. Access modes independently restrict enabled components.'))
    def add(*args):
        checks.append(finding(*args))
    unix = settings.transport == 'unix'
    add('transport', 'Daemon transport', 'pass' if unix else 'critical',
        'A local Unix socket is configured.' if unix else 'The TCP daemon protocol does not encrypt traffic.',
        'Prefer a permission-restricted Unix socket. Keep TCP private behind an authenticated encrypted tunnel until verified mutual TLS is available.')
    if unix:
        try:
            mode = settings.unix_socket.stat().st_mode
            restricted = stat.S_ISSOCK(mode) and not mode & 0o007
            add('socket', 'Actual daemon socket permissions', 'pass' if restricted else 'critical',
                f'Observed mode: {stat.S_IMODE(mode):04o}.', 'Restrict the socket and its parent directory to the service account and trusted group; review group membership and ACLs.')
        except OSError:
            add('socket', 'Actual daemon socket permissions', 'unknown', 'The configured socket could not be inspected.',
                'Check the running listener, parent directory permissions and socket ownership.')
    add('token', 'Daemon authentication token', 'info' if unix and not settings.access_token else 'pass' if settings.access_token and len(settings.access_token) >= 32 else 'warning',
        'A token of at least 32 characters is configured.' if settings.access_token and len(settings.access_token) >= 32 else 'No token or a short token is configured.',
        'TCP requires authentication. Use an independently generated random token of at least 32 characters; length alone does not prove entropy. Unix socket permissions remain essential.')
    docker = settings.docker_access != 'disabled'
    if docker:
        local = settings.docker_endpoint.startswith('unix://')
        endpoint = urlparse(settings.docker_endpoint)
        use_tls = endpoint.scheme == 'https' or endpoint.scheme == 'tcp' and endpoint.port == 2376
        tls = use_tls and settings.docker_tls_verify and settings.docker_tls_cert and settings.docker_tls_key
        add('docker-transport', 'Docker endpoint transport', 'info' if local else 'pass' if tls else 'critical',
            'A Unix Docker socket is configured; access is root-equivalent.' if local else 'Verified mutual TLS settings are configured.' if tls else 'The remote Docker endpoint lacks complete verified mutual TLS settings.',
            'Use a restricted Unix socket or verified mutual TLS. Configuration does not prove the remote certificate or connection is valid.')
        if settings.docker_tls_key:
            checks.append(private_file('docker-key', 'Docker TLS private-key permissions', settings.docker_tls_key))
    writes = [name for name in ('docker', 'network', 'system') if getattr(settings, name + '_access') == 'readwrite']
    add('access', 'Component write permissions', 'warning' if writes else 'pass',
        'Write access enabled for: ' + ', '.join(writes) if writes else 'Top-level components are read-only or disabled.',
        'Enable write access only for required components. A read-only Docker API integration still exposes privileged information.')
    add('compose-sources', 'External Compose source protection', 'pass' if settings.compose_host_read_only else 'warning',
        'External Compose sources are read-only.' if settings.compose_host_read_only else 'External Compose source writes are enabled.',
        'Keep external sources read-only and use managed definitions/layers for changes.')
    add('compose-roots', 'Compose source allowlist', 'warning' if any(str(path) == '/' for path in settings.compose_allowed_roots) else 'pass',
        'The source allowlist includes the filesystem root.' if any(str(path) == '/' for path in settings.compose_allowed_roots) else 'The allowlist does not include the filesystem root.',
        'Limit source roots to required stack directories; this does not limit an independently mounted host root or OS-level privileges.')
    checks.append(private_file('state-file', 'Daemon state database permissions', settings.stack_database))
    add('debug', 'Daemon debug logging', 'warning' if settings.log_level == 'DEBUG' else 'pass',
        'DEBUG logging is configured.' if settings.log_level == 'DEBUG' else 'DEBUG logging is disabled.',
        'Use INFO for production and restrict log access and retention.')
    add('identity', 'Daemon operating-system identity', 'info',
        'The daemon runs as root.' if os.geteuid() == 0 else 'The daemon runs without UID 0.',
        'Grant only necessary capabilities and mounts. Docker socket access can grant root-equivalent control even without UID 0.')
    add('host-boundary', 'Host and container isolation', 'unknown',
        'These checks inspect the daemon process namespace, not all host ACLs, container privileges or firewall ingress.',
        'Review systemd hardening or container capabilities, host mounts, ingress rules and log retention separately.')
    return {'checks': checks}
