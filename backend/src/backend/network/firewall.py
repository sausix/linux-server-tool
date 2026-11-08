"""Read Linux firewall state using the native firewall front-end available on the host."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class FirewallError(Exception):
    pass


def _run(command: list[str], timeout: float) -> str:
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or '').strip()[:4000]
        raise FirewallError(
            f"{command[0]} exited with status {exc.returncode}: {detail or 'no diagnostic output'}"
        ) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FirewallError(f"Unable to read firewall state with {command[0]}: {exc}") from exc
    return result.stdout


def _table_names(path: str) -> list[str]:
    try:
        return [line for line in Path(path).read_text(encoding="ascii").splitlines() if line]
    except OSError:
        return []


def _nft_snapshot(timeout):
    nft = shutil.which("nft")
    errors: list[str] = []
    if nft:
        try:
            output = _run([nft, "--json", "list", "ruleset"], timeout)
            ruleset = json.loads(output)
        except (FirewallError, json.JSONDecodeError) as exc:
            errors.append(str(exc))
        else:
            return {
                "backend": "nftables",
                "tool": nft,
                "ipv4_tables": _table_names("/proc/net/ip_tables_names"),
                "ipv6_tables": _table_names("/proc/net/ip6_tables_names"),
                "ruleset": ruleset,
                "errors": errors,
            }
    else:
        errors.append('nftables reader unavailable: nft not found')
    return {'backend': 'nftables', 'ruleset': None, 'errors': errors}


def _iptables_snapshot(timeout, backend):
    suffix = {'iptables': '', 'iptables-legacy': '-legacy', 'iptables-nft': '-nft'}[backend]
    result = {
        "backend": backend,
        "ipv4_tables": _table_names("/proc/net/ip_tables_names"),
        "ipv6_tables": _table_names("/proc/net/ip6_tables_names"),
        "ipv4_rules": None,
        "ipv6_rules": None,
        "errors": [], 'tools': {},
    }
    for family, name in [('ipv4', 'iptables'), ('ipv6', 'ip6tables')]:
        command = name + suffix + '-save'
        tool = shutil.which(command)
        if not tool:
            result['errors'].append(f'{backend} {family} reader unavailable: {command} not found')
            continue
        result['tools'][family] = tool
        try:
            result[family + '_rules'] = _run([tool, '-c'], timeout)
        except FirewallError as exc:
            result['errors'].append(str(exc))
    return result


def has_inventory(result):
    return any(any(kind in entry for kind in ('table', 'chain', 'rule'))
               for entry in (result.get('ruleset') or {}).get('nftables', [])) or any(
                   line.startswith('*') for field in ('ipv4_rules', 'ipv6_rules')
                   for line in (result.get(field) or '').splitlines())


def snapshot(timeout: float = 15.0, backend: str = 'auto') -> dict:
    if backend == 'nftables':
        return _nft_snapshot(timeout)
    if backend in {'iptables', 'iptables-legacy', 'iptables-nft'}:
        return _iptables_snapshot(timeout, backend)
    if backend != 'auto':
        raise ValueError('Unsupported firewall backend')
    probes = []
    if shutil.which('nft'):
        probes.append(_nft_snapshot(timeout))
    if shutil.which('iptables-legacy-save') or shutil.which('ip6tables-legacy-save'):
        probes.append(_iptables_snapshot(timeout, 'iptables-legacy'))
    selected = [probe for probe in probes if has_inventory(probe)]
    if not selected and (shutil.which('iptables-save') or shutil.which('ip6tables-save')):
        probe = _iptables_snapshot(timeout, 'iptables')
        probes.append(probe)
        if has_inventory(probe):
            selected.append(probe)
    if not selected:
        selected = probes[:1]
    if not selected:
        return {'backend': 'unavailable', 'backends': [], 'errors': ['No firewall reader found: install nftables or iptables']}
    result = {'backend': ' + '.join(probe['backend'] for probe in selected),
              'backends': [probe['backend'] for probe in selected],
              'errors': [error for probe in probes for error in probe['errors']]}
    for probe in selected:
        result.update({key: value for key, value in probe.items() if key not in {'backend', 'errors'}})
    result['selection'] = 'auto: non-empty rulesets preferred; nftables and legacy can coexist'
    return result
