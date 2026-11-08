from __future__ import annotations

import argparse
import asyncio
import json
import hmac
import logging
import os
import platform
import sqlite3
from pathlib import Path
from typing import Any

from backend import __version__
from backend.daemon_config import DaemonSettings
from backend.network.firewall import FirewallError, snapshot as firewall_snapshot
from backend.events import EventBroker, EventRuntime
from backend.diagnostics import startup_checks
from backend.system.sysctl import snapshot as routing_snapshot
from backend.system.kernel_defaults import local_defaults
from backend.network.inventory import interfaces, addresses, routes
from urllib.parse import urlsplit, urlunsplit
from backend.network.configuration import declarations
from backend.network.address_configuration import change as change_address_configuration
from backend.network.inventory import NetlinkError, change_address, change_route, set_link
from backend.network.management import NetworkManagementError, configure as configure_network, detect as detect_network_management
from common.protocol import (
    MAX_MESSAGE_BYTES,
    ProtocolError,
    decode_message,
    encode_message,
    failure,
    success,
    validate_request,
)

from common.errors import BackendError

logger = logging.getLogger("backend")


from backend.system.inventory import SystemInventory
from backend.system.configuration import snapshot as configuration_review


class DaemonServer:
    def __init__(self, settings: DaemonSettings):
        self.settings = settings
        self.system_inventory = SystemInventory(host_root=settings.system_host_root)
        self._connections = asyncio.Semaphore(settings.max_clients)
        self.events = EventBroker()
        self.components = {}
        self.docker = None
        self.stack_control = None
        if settings.docker_support:
            from backend.docker.component import DockerComponent
            self.components['docker'] = DockerComponent(self)

    def enrich(self, model, result):
        for component in self.components.values():
            component.enrich(model, result)
        return result

    def annotate_configuration_sources(self, items):
        for component in self.components.values():
            component.annotate_configuration_sources(items)

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == 'security.checks':
            from backend.security_checks import assess
            return assess(self.settings)
        if method == 'security.event':
            from common.security_events import validate_event
            from datetime import datetime, timezone
            try:
                event = validate_event(params.get('event'))
            except ValueError:
                raise ProtocolError(-32602, 'Invalid security event') from None
            security_logger = logging.getLogger('backend.security')
            security_logger.setLevel(logging.INFO)
            security_logger.info('%s', json.dumps({**event, 'received_at': datetime.now(timezone.utc).isoformat()}, ensure_ascii=True))
            return {'accepted': True, 'event_id': event['event_id']}
        if method == "ping":
            return {
                "service": "backend",
                "version": __version__,
                "docker_access": self.settings.docker_access,
                "network_access": self.settings.network_access,
                "events": self.settings.events_enabled,
                'system_access': self.settings.system_access,
                'host_paths': {**({str(self.settings.system_host_root): '/'} if self.settings.system_host_root else {}),
                               **({str(self.settings.compose_host_root): '/'} if self.settings.compose_host_root != Path('/') else {}),
                               **({str(self.settings.host_etc_directory): '/etc'} if self.settings.host_etc_directory != Path('/etc') else {})},
                'logs': {'default_lines': self.settings.docker_log_default_lines, 'max_lines': self.settings.docker_log_max_lines, 'max_streams': self.settings.docker_log_max_streams},
                'features': self.settings.features,
                'connections': {
                    **({'docker': self.safe_endpoint(self.settings.docker_endpoint)}
                       if self.settings.docker_access != 'disabled' else {}),
                    **({'network': 'netlink://NETLINK_ROUTE'}
                       if any(self.settings.feature_access(name) != 'disabled' for name in ('interfaces', 'routing')) else {}),
                    **({'sysctl': 'procfs:///proc/sys/net'}
                       if self.settings.feature_access('sysctl') != 'disabled' else {}),
                },
            }
        if method == "system.info":
            return {
                "hostname": platform.node(),
                "platform": platform.system(),
                "release": platform.release(),
            }
        if method.startswith("docker.") and (not self.settings.docker_support or self.settings.docker_access == "disabled"):
            raise ProtocolError(-32020, "Docker access is disabled")
        if method in {'docker.stack.model.change', 'docker.volumes.delete', 'docker.images.delete', 'docker.stack.definition.save', 'docker.stack.file.save',
                      'docker.stack.convert', 'docker.stack.action', 'docker.stack.job', 'docker.stack.create'} and self.settings.docker_access != 'readwrite':
            raise ProtocolError(-32022, 'Docker access is read-only')
        if method in {'system.configuration', 'system.snapshot', 'system.sysctl', 'network.sysctl'} and self.settings.system_access == 'disabled':
            raise ProtocolError(-32021, 'System access is disabled')
        if method == 'system.sysctl':
            method = 'network.sysctl'
        if (
            method.startswith("network.") or method.startswith("firewall.")
        ) and method != 'network.sysctl' and self.settings.network_access == "disabled":
            raise ProtocolError(-32021, "Network access is disabled")
        network_write = (
            method == "network.configure" and (params.get("commit") or params.get("activate"))
        ) or (method in {"network.link.set", "network.address.change", "network.address.configuration", "network.route.change"} and params.get("apply"))
        if network_write and self.settings.network_access != "readwrite":
            raise ProtocolError(-32022, "Network access is read-only")
        feature = {
            'network.snapshot': 'interfaces', 'network.manager': 'interfaces',
            'network.configure': 'interfaces', 'network.link.set': 'interfaces',
            'network.address.change': 'interfaces', 'network.address.configuration': 'interfaces', 'network.route.change': 'routing',
            'network.routing': 'routing', 'network.sysctl': 'sysctl',
            'firewall.snapshot': 'firewall',
        }.get(method)
        if feature:
            access = self.settings.feature_access(feature)
            if access == 'disabled':
                raise ProtocolError(-32021, f'{feature} feature is disabled')
            if network_write and access != 'rw':
                raise ProtocolError(-32022, f'{feature} feature is read-only')
        try:
            if method in {'network.configuration', 'system.configuration'}:
                result = configuration_review(method.split('.')[0], self.settings)
                if method == 'network.configuration':
                    self.annotate_configuration_sources(result['items'])
                return result
            if method == 'system.snapshot':
                return self.system_inventory.snapshot()
            if method.startswith('docker.'):
                return self.components['docker'].dispatch(method, params)
            if method == "network.snapshot":
                inventory = {'interfaces': interfaces(), 'addresses': addresses(),
                             'routes': routes() if self.settings.feature_access('routing') != 'disabled' else []}
                inventory['configuration'] = declarations(
                    inventory['interfaces'], self.settings.networkd_directory,
                    self.settings.interfaces_file,
                )
                return self.enrich('interfaces', inventory)
            if method == "network.manager":
                return detect_network_management(
                    self.settings.networkd_directory, self.settings.interfaces_file
                )
            if method == 'network.routing':
                return self.enrich('routes', {'routes': routes(), 'options': []})
            if method == 'network.sysctl':
                result = routing_snapshot(include_routes=False, **self.settings.sysctl_sources)
                self.annotate_configuration_sources(result['options'])
                self.enrich('sysctl', result)
                if self.settings.host_etc_directory != Path('/etc') and self.settings.system_host_root is None:
                    result['source_note'] += ' Container mode: only host /etc sysctl sources are mounted; vendor and runtime declarations may be incomplete.'
                return result
            if method == "firewall.snapshot":
                result = firewall_snapshot(self.settings.command_timeout_seconds, self.settings.firewall_backend)
                self.enrich('firewall', result)
                for error in result['errors']:
                    logger.warning('firewall.snapshot: %s', error)
                return result
            if method == "network.configure":
                configuration = params.get("configuration")
                if not isinstance(configuration, dict):
                    raise ProtocolError(-32602, "configuration must be an object")
                return configure_network(
                    str(params.get("manager", "")),
                    configuration,
                    self.settings.networkd_directory,
                    self.settings.interfaces_file,
                    commit=params.get("commit") is True,
                    activate=params.get("activate") is True,
                    timeout=self.settings.command_timeout_seconds,
                )
            if method == "network.link.set":
                planned = {
                    "interface": params.get("interface"),
                    "up": params.get("up"),
                    "mtu": params.get("mtu"),
                }
                if params.get("apply") is not True:
                    return {"planned": planned, "applied": False}
                return {"result": set_link(**planned), "applied": True}
            if method == 'network.address.configuration':
                return change_address_configuration(
                    params.get('interface'), params.get('address'), params.get('action'),
                    self.settings.networkd_directory, self.settings.interfaces_file,
                    apply=params.get('apply') is True, expected_hash=params.get('expected_hash'))
            if method == "network.address.change":
                planned = {
                    "action": params.get("action"),
                    "interface": params.get("interface"),
                    "address": params.get("address"),
                }
                if params.get("apply") is not True:
                    return {"planned": planned, "applied": False}
                return {"result": change_address(**planned), "applied": True}
            if method == "network.route.change":
                planned = {
                    key: params.get(key)
                    for key in ("action", "destination", "gateway", "interface", "metric", "family")
                }
                if params.get("apply") is not True:
                    return {"planned": planned, "applied": False}
                return {"result": change_route(**planned), "applied": True}
        except (
            BackendError,
            FirewallError,
            NetlinkError,
            NetworkManagementError,
            OSError,
            sqlite3.Error,
            TypeError,
            ValueError,
        ) as exc:
            logger.warning('%s failed: %s', method, exc)
            raise ProtocolError(-32010, str(exc)) from exc
        raise ProtocolError(-32601, "Method not found")

    @staticmethod
    def safe_endpoint(endpoint):
        parsed = urlsplit(endpoint)
        host = parsed.hostname or ''
        if ':' in host:
            host = '[' + host + ']'
        if parsed.port:
            host += ':' + str(parsed.port)
        return urlunsplit((parsed.scheme, host, parsed.path, '', ''))

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        async with self._connections:
            peer = writer.get_extra_info("peername") or "unix-client"
            logger.debug("Client connected: %s", peer)
            subscription_id: str | None = None
            notification_task: asyncio.Task | None = None
            log_task: asyncio.Task | None = None
            live_logs = None
            write_lock = asyncio.Lock()

            async def send(message: dict) -> None:
                async with write_lock:
                    writer.write(encode_message(message))
                    await writer.drain()

            async def notifications(queue: asyncio.Queue) -> None:
                try:
                    while True:
                        event = await queue.get()
                        await send({"jsonrpc": "2.0", "method": "event", "params": event})
                except ConnectionError:
                    logger.debug('Subscription client disconnected: %s', peer)
                    writer.close()
                except Exception:
                    logger.exception('Subscription delivery failed: %s', peer)
                    writer.close()

            async def stop_notifications() -> None:
                nonlocal notification_task
                if notification_task:
                    notification_task.cancel()
                    await asyncio.gather(notification_task, return_exceptions=True)
                    notification_task = None

            async def stop_logs():
                nonlocal log_task, live_logs
                if live_logs:
                    live_logs.close()
                if log_task:
                    log_task.cancel()
                    await asyncio.gather(log_task, return_exceptions=True)
                log_task, live_logs = None, None

            async def log_notifications(subscription):
                try:
                    async for record in subscription.records():
                        await send({'jsonrpc': '2.0', 'method': 'docker.log', 'params': record})
                except ConnectionError:
                    writer.close()
                except Exception:
                    logger.exception('Log subscription delivery failed')
                    writer.close()
                finally:
                    subscription.close()

            try:
                while payload := await reader.readline():
                    request_id = None
                    try:
                        message = decode_message(payload.rstrip(b"\r\n"))
                        request_id, method, params = validate_request(message)
                        configured_token = self.settings.access_token
                        if configured_token and not hmac.compare_digest(
                            str(message.get("auth", "")), configured_token
                        ):
                            raise ProtocolError(-32001, "Unauthorized")
                        if method == 'docker.logs.subscribe':
                            if self.settings.docker_access == 'disabled':
                                raise ProtocolError(-32020, 'Docker access is disabled')
                            await stop_logs()
                            try:
                                live_logs = self.components['docker'].live_logs(params)
                                selected = await live_logs.reconcile()
                            except (BackendError, OSError, ValueError) as exc:
                                if live_logs:
                                    live_logs.close()
                                raise ProtocolError(-32010, str(exc)) from exc
                            await send(success(request_id, {'sources': selected}))
                            log_task = asyncio.create_task(log_notifications(live_logs))
                            continue
                        if method == 'docker.logs.unsubscribe':
                            await stop_logs()
                            await send(success(request_id, {'unsubscribed': True}))
                            continue
                        if method == "events.subscribe":
                            if not self.settings.events_enabled:
                                raise ProtocolError(-32030, "Event subscriptions are disabled")
                            topics = params.get("topics", ["*"])
                            if not isinstance(topics, list) or not all(
                                isinstance(topic, str) and topic for topic in topics
                            ):
                                raise ProtocolError(-32602, "topics must be a list of strings")
                            if subscription_id:
                                self.events.unsubscribe(subscription_id)
                            await stop_notifications()
                            subscription_id, queue = self.events.subscribe(topics)
                            await send(
                                success(
                                    request_id,
                                    {"subscription": subscription_id, "topics": topics},
                                )
                            )
                            notification_task = asyncio.create_task(notifications(queue))
                            continue
                        if method == "events.unsubscribe":
                            if subscription_id:
                                self.events.unsubscribe(subscription_id)
                                subscription_id = None
                            await stop_notifications()
                            await send(success(request_id, {"unsubscribed": True}))
                            continue
                        response = success(
                            request_id, await asyncio.to_thread(self.dispatch, method, params)
                        )
                    except ProtocolError as exc:
                        response = failure(request_id, exc.code, exc.message, exc.data)
                    except ConnectionError:
                        raise
                    except Exception:
                        logger.exception("Unhandled request error")
                        response = failure(request_id, -32603, "Internal error")
                    await send(response)
            except (ConnectionError, asyncio.LimitOverrunError):
                logger.debug("Client disconnected during I/O: %s", peer)
            finally:
                if subscription_id:
                    self.events.unsubscribe(subscription_id)
                await stop_notifications()
                await stop_logs()
                try:
                    writer.close()
                    # A failed write can be raised again by wait_closed().
                    await asyncio.wait_for(writer.wait_closed(), timeout=2)
                except (ConnectionError, TimeoutError):
                    logger.debug('Client connection already closed: %s', peer)

    async def serve(self) -> None:
        self.events.bind(asyncio.get_running_loop())
        if self.settings.feature_access('sysctl') != 'disabled':
            await asyncio.to_thread(local_defaults)
        await asyncio.to_thread(startup_checks, self.settings, self.docker)
        if self.settings.transport == "unix":
            socket_path = self.settings.unix_socket
            socket_path.parent.mkdir(parents=True, exist_ok=True)
            if socket_path.exists():
                if not socket_path.is_socket():
                    raise RuntimeError(f"Refusing to replace non-socket path: {socket_path}")
                socket_path.unlink()
            server = await asyncio.start_unix_server(
                self.handle_client, path=socket_path, limit=MAX_MESSAGE_BYTES + 1
            )
            os.chmod(socket_path, self.settings.socket_mode_int)
            logger.info("backend listening on unix://%s", socket_path)
        else:
            server = await asyncio.start_server(
                self.handle_client,
                host=self.settings.listen_host,
                port=self.settings.listen_port,
                limit=MAX_MESSAGE_BYTES + 1,
            )
            logger.info(
                "backend listening on tcp://%s:%s",
                self.settings.listen_host,
                self.settings.listen_port,
            )
        runtime = EventRuntime(self.events, self.docker, self.settings,
            docker_source=self.components['docker'].event_source if 'docker' in self.components else None)
        self.event_runtime = runtime
        if self.settings.events_enabled:
            runtime.start()
        try:
            async with server:
                await server.serve_forever()
        finally:
            if self.stack_control:
                self.stack_control.close()
            runtime.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the backend system service")
    for component in ('docker', 'network', 'system'):
        parser.add_argument('--' + component + '-support', action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--env-prefix", dest="_env_prefix", help="Override BACKEND_ENV_PREFIX")
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument("--unix-socket", type=Path, help="Listen on a Unix socket")
    transport.add_argument("--listen-host", help="Listen on a TCP interface")
    parser.add_argument("--listen-port", type=int, help="TCP port")
    parser.add_argument("--socket-mode", help="Unix socket mode, for example 0660")
    parser.add_argument("--access-token", help="Client token (prefer ACCESS_TOKEN)")
    parser.add_argument("--max-clients", type=int)
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    parser.add_argument("--docker-endpoint", help="Docker unix:// socket or tcp:// endpoint")
    parser.add_argument("--docker-access", choices=("disabled", "ro", "rw", "readonly", "readwrite"))
    parser.add_argument("--network-access", choices=("disabled", "ro", "rw", "readonly", "readwrite"))
    parser.add_argument('--system-access', choices=('disabled', 'ro', 'rw', 'readonly', 'readwrite'))
    parser.add_argument('--system-host-root', type=Path)
    for feature in ('interfaces', 'routing', 'firewall', 'sysctl'):
        parser.add_argument(f'--{feature}-access', choices=('disabled', 'ro', 'rw'))
    parser.add_argument("--events-enabled", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--docker-api-version")
    parser.add_argument("--docker-timeout-seconds", type=float)
    parser.add_argument('--docker-storage-timeout-seconds', type=float)
    parser.add_argument('--docker-log-max-lines', type=int)
    parser.add_argument('--docker-log-default-lines', type=int)
    parser.add_argument('--docker-log-max-streams', type=int)
    parser.add_argument('--docker-log-discovery-interval-seconds', type=float)
    parser.add_argument("--docker-tls-ca", type=Path)
    parser.add_argument("--docker-tls-cert", type=Path)
    parser.add_argument("--docker-tls-key", type=Path)
    parser.add_argument('--stack-database', type=Path)
    parser.add_argument('--compose-host-root', type=Path)
    parser.add_argument('--compose-root', type=Path)
    parser.add_argument('--compose-host-read-only', action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument('--compose-allowed-roots', nargs='+', type=Path)
    parser.add_argument('--compose-search-directories', nargs='+', type=Path)
    parser.add_argument('--compose-command', nargs='+')
    parser.add_argument('--stack-control-timeout-seconds', type=float)
    parser.add_argument('--stack-max-concurrent-jobs', type=int)
    parser.add_argument('--stack-stop-timeout-seconds', type=int)
    parser.add_argument(
        "--docker-tls-verify", action=argparse.BooleanOptionalAction, default=None
    )
    parser.add_argument("--networkd-directory", type=Path)
    parser.add_argument("--interfaces-file", type=Path)
    parser.add_argument('--host-etc-directory', type=Path)
    parser.add_argument('--firewall-backend', choices=('auto', 'nftables', 'iptables', 'iptables-legacy', 'iptables-nft'))
    parser.add_argument("--command-timeout-seconds", type=float)
    return parser


def settings_from_cli() -> DaemonSettings:
    values = {key: value for key, value in vars(build_parser().parse_args()).items() if value is not None}
    if "unix_socket" in values:
        values["transport"] = "unix"
    elif "listen_host" in values:
        values["transport"] = "tcp"
    return DaemonSettings(**values)


def main() -> None:
    settings = settings_from_cli()
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(DaemonServer(settings).serve())
    except KeyboardInterrupt:
        logger.info("backend stopped")


if __name__ == "__main__":
    main()
