"""Read routing sysctls and trace candidate persistent assignments without sysctl subprocesses."""

import fnmatch
from pathlib import Path

from backend.network.inventory import routes
from backend.system.kernel_defaults import local_defaults

# Higher-priority directories come first; selected basenames are then sorted.
SYSCTL_DIRECTORIES = (
    Path('/etc/sysctl.d'), Path('/run/sysctl.d'), Path('/usr/local/lib/sysctl.d'),
    Path('/usr/lib/sysctl.d'), Path('/lib/sysctl.d'),
)

CORE_OPTIONS = {'default_qdisc', 'rmem_max', 'rmem_default', 'wmem_max', 'wmem_default',
                'optmem_max', 'netdev_max_backlog', 'somaxconn', 'netdev_budget', 'netdev_budget_usecs'}
BUFFER_OPTIONS = {'rmem_max', 'rmem_default', 'wmem_max', 'wmem_default', 'optmem_max',
                  'tcp_rmem', 'tcp_wmem', 'tcp_mem', 'udp_mem', 'udp_rmem_min', 'udp_wmem_min'}
CORE_KEYS = {'net/core/' + name for name in CORE_OPTIONS}


def excluded_key(key):
    parts = key.split('/')
    # flush is a write-only action, not a readable configuration value.
    # stable_secret is sensitive and may return EIO when uninitialized.
    return key in {'net/ipv4/route/flush', 'net/ipv6/route/flush',
                   'net/ipv4/tcp_fastopen_key'} or (
        len(parts) == 5 and parts[:3] == ['net', 'ipv6', 'conf']
        and parts[-1] == 'stable_secret'
    )


def normalize_value(value):
    return ' '.join(value.split())


def normalize_key(key):
    # The first separator determines notation; slashes preserve dots in interface names.
    dot, slash = key.find('.'), key.find('/')
    if dot >= 0 and (slash < 0 or dot < slash):
        return key.translate(str.maketrans('./', '/.'))
    return key


def assignments(directories=SYSCTL_DIRECTORIES, legacy=Path('/etc/sysctl.conf'), include_kernel=False):
    selected = {}
    errors = []
    for directory in directories:
        try:
            for path in directory.glob('*.conf'):
                selected.setdefault(path.name, path)
        except OSError as exc:
            errors.append(f'{directory}: {exc}')
    files = [selected[name] for name in sorted(selected)]
    # Keep legacy assignments separate: systemd-sysctl does not read sysctl.conf directly.
    if legacy.is_file() and legacy not in files:
        files.append(legacy)
    declarations = []
    for path in files:
        try:
            lines = path.read_text().splitlines()
        except (OSError, UnicodeError) as exc:
            errors.append(f'{path}: {exc}')
            continue
        for number, line in enumerate(lines, 1):
            line = line.strip()
            if not line or line.startswith(('#', ';')):
                continue
            if '=' in line:
                key, value = (part.strip() for part in line.split('=', 1))
                value = value.split('#', 1)[0].split(';', 1)[0].strip()
            elif line.startswith('-'):
                key, value = line, None
            else:
                continue
            key = normalize_key(key.lstrip('-'))
            if (key.startswith(('net/ipv4/', 'net/ipv6/')) or key in CORE_KEYS or (include_kernel and key.startswith('kernel/'))) and not excluded_key(key):
                declarations.append({'key': key, 'value': value, 'file': str(path),
                                     'line': number, 'legacy': path == legacy})
    return declarations, errors


def snapshot(proc_root=Path('/proc/sys'), directories=SYSCTL_DIRECTORIES,
             legacy=Path('/etc/sysctl.conf'), include_routes=True, kernel_defaults=None):
    kernel_defaults = local_defaults() if kernel_defaults is None else kernel_defaults
    declarations, errors = assignments(directories, legacy)
    options = []

    def children(directory):
        try:
            return list(directory.glob('*'))
        except OSError as exc:
            errors.append(f'{directory}: {exc}')
            return []

    for family in ('ipv4', 'ipv6', 'core'):
        base = proc_root / 'net' / family
        paths = children(base) + children(base / 'route')
        if family == 'core':
            paths = [base / name for name in sorted(CORE_OPTIONS)]
        for interface in children(base / 'conf'):
            paths.extend(children(interface))
        for path in sorted(paths):
            key = path.relative_to(proc_root).as_posix()
            if excluded_key(key):
                continue
            try:
                if not path.is_file():
                    continue
                value = path.read_text().strip()
            except OSError as exc:
                errors.append(f'{path}: {exc}')
                continue
            exact = [item for item in declarations if item['key'] == key]
            matching = exact or [item for item in declarations
                if len(key.split('/')) == len(item['key'].split('/')) and all(
                    fnmatch.fnmatchcase(part, pattern)
                    for part, pattern in zip(key.split('/'), item['key'].split('/')))]
            candidates = [item for item in matching if item['value'] is not None]
            systemd = [item for item in candidates if not item['legacy']]
            effective = [group[-1] for group in (systemd, candidates) if group]
            kernel_default = kernel_defaults.get(key)
            comparisons = [normalize_value(item['value']) == normalize_value(value) for item in effective]
            state = 'default' if not effective else 'configured' if all(comparisons) else 'changed'
            if not effective and kernel_default and normalize_value(kernel_default['value']) != normalize_value(value):
                state = 'changed'
            ambiguous = bool(comparisons) and any(comparisons) and not all(comparisons)
            parts = key.split('/')
            prefix = '.'.join(parts[:-1])
            classes = []
            classes.append(state)
            if parts[-1] in BUFFER_OPTIONS:
                classes.append('socket-buffer')
            if parts[2] == 'route' or parts[-1] in {'ip_forward', 'forwarding', 'mc_forwarding'}:
                classes.append('routing')
            if parts[2] == 'conf':
                classes.append('interface' if parts[3] not in {'all', 'default'} else 'interface-defaults')
            if parts[-1].startswith(('accept_ra', 'router_')):
                classes.append('router-advertisement')
            if parts[-1] == 'rp_filter':
                classes.append('reverse-path-filter')
            options.append({
                'key': key.replace('/', '.') if not any('.' in part for part in parts) else key,
                'path': str(path), 'family': family,
                'interface': parts[3] if len(parts) > 4 and parts[2] == 'conf' else None,
                'value': value, 'sources': matching,
                'unit': ('pages' if parts[-1] in {'tcp_mem', 'udp_mem'} else 'bytes') if parts[-1] in BUFFER_OPTIONS else None,
                'classes': classes, 'prefix': prefix,
                'state': state, 'ambiguous': ambiguous,
                'kernel_default': kernel_default['value'] if kernel_default else None,
                'kernel_default_source': kernel_default,
                'default_note': 'Build default from local kernel configuration; runtime initialization can depend on context.' if kernel_default else 'No stable default found in local kernel configuration. default means no persistent candidate found, not verified kernel default.',
                'systemd_candidate': systemd[-1] if systemd else None,
                'procps_candidate': candidates[-1] if candidates else None,
            })
    if not options:
        errors.append(f'No readable sysctl values below {proc_root}/net; check procfs availability, network namespace and container security policy.')
    return {'options': options, 'routes': routes() if include_routes else [], 'errors': errors,
            'source_note': 'Configuration candidates are inferred from files, not proof of what was applied. systemd-sysctl and sysctl --system differ in legacy sysctl.conf handling. Runtime changes by network managers may override these values.'}
