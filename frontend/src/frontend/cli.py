"""Command-line entry point for the application frontend."""

from __future__ import annotations

import argparse
import os
import json

import uvicorn

from frontend.app import create_app
from frontend.config import Settings
from frontend.server import FrontendServer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="frontend", description="Run the application web service")
    for component in ('docker', 'network', 'system'):
        parser.add_argument('--' + component + '-support', action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--env-prefix", dest="_env_prefix", help="Override FRONTEND_ENV_PREFIX")
    parser.add_argument("--environment", choices=("development", "production", "test"))
    parser.add_argument("--host", dest="bind_host", help="Bind address")
    parser.add_argument("--port", type=int, help="TCP port")
    parser.add_argument("--public-url", help="Externally reachable base URL")
    parser.add_argument("--database-url", help="SQLAlchemy database URL")
    parser.add_argument("--secret-key", help="Session secret (prefer SECRET_KEY)")
    parser.add_argument("--session-max-age", dest="session_max_age_seconds", type=int)
    parser.add_argument('--session-persistent', action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument('--login-allowed-networks', type=json.loads, help='JSON array of allowed source IPs/CIDRs')
    parser.add_argument('--login-rules', type=json.loads, help='Ordered JSON login rules')
    parser.add_argument('--webauthn-mode', choices=('off', 'optional', 'required'))
    for option in ('session_absolute_max_age_seconds', 'login_failure_limit', 'login_ip_failure_limit',
                   'login_failure_window_seconds', 'login_block_seconds', 'login_max_concurrency', 'login_ipv6_prefix',
                   'request_max_bytes', 'post_max_bytes', 'login_max_bytes', 'upload_max_bytes', 'upload_max_files'):
        parser.add_argument('--' + option.replace('_', '-'), type=int)
    parser.add_argument("--password-max-age-days", type=int)
    parser.add_argument("--http-policy", choices=("allow", "redirect", "https-only"))
    parser.add_argument("--cookie-secure", choices=("auto", "always", "never"))
    parser.add_argument("--trusted-hosts", help="Comma-separated HTTP Host allowlist")
    parser.add_argument("--forwarded-allow-ips", help="Comma-separated trusted proxy IPs/CIDRs")
    parser.add_argument("--ssl-certfile", help="TLS certificate file")
    parser.add_argument("--ssl-keyfile", help="TLS private key file")
    parser.add_argument("--daemon-endpoint", help="backend unix:///path or tcp://host:port")
    parser.add_argument("--daemon-token", help="backend token (prefer FRONTEND_DAEMON_TOKEN)")
    parser.add_argument("--daemon-timeout-seconds", type=float)
    parser.add_argument('--daemon-inventory-timeout-seconds', type=float)
    parser.add_argument('--daemon-storage-timeout-seconds', type=float)
    parser.add_argument('--daemon-control-timeout-seconds', type=float)
    parser.add_argument("--shutdown-timeout-seconds", type=int)
    parser.add_argument("--reload", action=argparse.BooleanOptionalAction, default=None)
    return parser


def main() -> None:
    args = vars(build_parser().parse_args())
    overrides = {key: value for key, value in args.items() if value is not None}
    reload_enabled = overrides.pop("reload", False)
    settings = Settings(**overrides)
    application = create_app(settings)
    if reload_enabled:
        os.environ[settings.prefix_selector] = settings.env_prefix
        for name, value in settings.model_dump().items():
            if value is not None:
                os.environ[settings.env_name(name)] = json.dumps(value) if isinstance(value, (list, dict)) else str(value)
        application = "frontend.app:create_app"
    config = uvicorn.Config(
        application,
        factory=reload_enabled,
        host=settings.bind_host,
        port=settings.port,
        reload=reload_enabled,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        ssl_certfile=settings.ssl_certfile,
        ssl_keyfile=settings.ssl_keyfile,
        timeout_graceful_shutdown=settings.shutdown_timeout_seconds,
    )
    server = FrontendServer(config)
    if config.should_reload:
        from uvicorn.supervisors import ChangeReload
        sock = config.bind_socket()
        try:
            ChangeReload(config, target=server.run, sockets=[sock]).run()
        except KeyboardInterrupt:
            pass
        finally:
            sock.close()
    else:
        server.run()


if __name__ == "__main__":
    main()

