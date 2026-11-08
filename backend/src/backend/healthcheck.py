"""Probe the configured daemon listener using its active namespace and token."""
from common.client import DaemonClient
from .daemon_config import DaemonSettings


def main():
    settings = DaemonSettings()
    host = settings.listen_host
    if host in ('0.0.0.0', '::'):
        host = '127.0.0.1' if host == '0.0.0.0' else '::1'
    if ':' in host:
        host = '[' + host + ']'
    endpoint = (f'unix://{settings.unix_socket}' if settings.transport == 'unix'
                else f'tcp://{host}:{settings.listen_port}')
    DaemonClient(endpoint, token=settings.access_token, timeout=2).ping()


if __name__ == '__main__':
    main()
