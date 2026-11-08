"""Probe the configured local web listener without relying on fixed port settings."""
import ssl
from urllib.request import urlopen

from .config import Settings


def main():
    settings = Settings()
    host = settings.bind_host
    if host in ('0.0.0.0', '::'):
        host = '127.0.0.1' if host == '0.0.0.0' else '::1'
    if ':' in host:
        host = '[' + host + ']'
    tls = bool(settings.ssl_certfile)
    context = ssl.create_default_context(cafile=settings.ssl_certfile) if tls else None
    with urlopen(f'{"https" if tls else "http"}://{host}:{settings.port}/healthz', timeout=3, context=context) as response:
        if response.status != 200:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
