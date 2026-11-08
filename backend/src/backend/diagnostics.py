"""Non-mutating startup probes for enabled component roles."""

import logging
import os
import shutil
import subprocess

from backend.network.firewall import snapshot as firewall_snapshot
from backend.network.inventory import snapshot as network_snapshot
from backend.network.management import detect

logger = logging.getLogger('backend.checks')


def startup_checks(settings, docker):
    results = []

    def record(role, problem=None):
        results.append({'role': role, 'available': problem is None, 'detail': problem})
        if problem:
            logger.warning('%s: %s', role, problem)
        else:
            logger.info('%s: available', role)

    def tool(role, alternatives):
        if not any(shutil.which(name) for name in alternatives):
            record(role, 'No tool found; install one of: ' + ', '.join(alternatives))
        else:
            record(role)

    if settings.docker_access != 'disabled':
        try:
            if not docker.ping().get('reachable'):
                raise RuntimeError('Engine ping did not return OK')
        except Exception as exc:
            record('docker.read', str(exc))
        else:
            record('docker.read')
        if settings.docker_access == 'readwrite':
            command = settings.compose_command
            if not shutil.which(command[0]):
                record('docker.compose.control', 'Compose executable not found: ' + command[0])
            else:
                try:
                    result = subprocess.run([*command, 'version', '--short'], capture_output=True, timeout=5, check=False)
                    record('docker.compose.control', None if result.returncode == 0 else 'Compose plugin unavailable; install Docker Compose v2')
                except (OSError, subprocess.TimeoutExpired) as exc:
                    record('docker.compose.control', str(exc))

    if settings.network_access != 'disabled':
        if any(settings.feature_access(name) != 'disabled' for name in ('interfaces', 'routing')):
            try:
                if all(settings.feature_access(name) != 'disabled' for name in ('interfaces', 'routing')):
                    network_snapshot()
                else:
                    from backend.network.inventory import interfaces, addresses, routes
                    if settings.feature_access('interfaces') != 'disabled':
                        interfaces()
                        addresses()
                    if settings.feature_access('routing') != 'disabled':
                        routes()
            except Exception as exc:
                record('network.read', f'{exc}; check AF_NETLINK in RestrictAddressFamilies and the service syscall policy')
            else:
                record('network.read')
        if settings.feature_access('sysctl') != 'disabled':
            from backend.system.sysctl import snapshot
            for error in snapshot(include_routes=False, **settings.sysctl_sources)['errors']:
                record('network.sysctl.read', error)
        if settings.feature_access('firewall') != 'disabled':
            firewall = firewall_snapshot(settings.command_timeout_seconds, settings.firewall_backend)
            for error in firewall['errors']:
                logger.warning('network.firewall.read: %s', error)
            readable = firewall.get('ruleset') is not None or any(
                firewall.get(key) is not None for key in ('ipv4_rules', 'ipv6_rules')
            )
            record('network.firewall.read', None if readable else 'No usable firewall reader; check tools, CAP_NET_ADMIN, AF_NETLINK and tool stderr')
        if settings.events_enabled and settings.feature_access('firewall') != 'disabled' and settings.firewall_backend == 'nftables':
            tool('network.firewall.events', ['nft'])
        managers = detect(settings.networkd_directory, settings.interfaces_file)['detected']
        if settings.feature_access('interfaces') == 'rw':
            if not managers:
                record('network.persistence', 'No supported network manager detected (systemd-networkd or ifupdown)')
            for manager in managers:
                if manager['name'] == 'systemd-networkd':
                    tool('network.systemd-networkd.activate', ['networkctl'])
                    directory = settings.networkd_directory
                else:
                    if shutil.which('ifreload'):
                        tool('network.ifupdown.activate', ['ifreload'])
                    else:
                        tool('network.ifupdown.up', ['ifup'])
                        tool('network.ifupdown.down', ['ifdown'])
                    directory = settings.interfaces_file.parent / 'interfaces.d'
                parent = directory if directory.exists() else directory.parent
                record(f"network.{manager['name']}.write", None if os.access(parent, os.W_OK) else f'Configuration directory is not writable: {parent}')
            # Never exercise a mutation to test privileges.
            try:
                from pathlib import Path
                status = Path('/proc/self/status').read_text()
                capabilities = int(next(line.split()[1] for line in status.splitlines() if line.startswith('CapEff:')), 16)
                if not capabilities & (1 << 12):
                    record('network.write', 'CAP_NET_ADMIN is absent; kernel mutations will fail')
            except (OSError, StopIteration, ValueError):
                record('network.write', 'Unable to inspect effective capabilities')
    return results
