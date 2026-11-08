"""Read-only local Docker binding provenance and daemon.json inspection."""

import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def sanitize(value, key=''):
    if re.search(r'password|secret|token|authorization|^auth$', key, re.I):
        return '[redacted]'
    if isinstance(value, dict):
        return {name: sanitize(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item, key) for item in value]
    if isinstance(value, str) and '://' in value:
        try:
            parsed = urlsplit(value)
            if parsed.scheme in {'unix', 'fd'}:
                return parsed.scheme + '://' + parsed.netloc + parsed.path
            host = parsed.hostname or ''
            if ':' in host:
                host = '[' + host + ']'
            if parsed.port:
                host += ':' + str(parsed.port)
            return urlunsplit((parsed.scheme, host, parsed.path, '', ''))
        except ValueError:
            return '[invalid URL]'
    return value


def binding_arguments(arguments):
    hosts, config = [], None
    iterator = iter(arguments)
    for arg in iterator:
        if arg in ('-H', '--host'):
            hosts.append(next(iterator, ''))
        elif arg.startswith('--host='):
            hosts.append(arg.split('=', 1)[1])
        elif arg.startswith('-H') and len(arg) > 2:
            hosts.append(arg[2:].lstrip('='))
        elif arg == '--config-file':
            config = next(iterator, None)
        elif arg.startswith('--config-file='):
            config = arg.split('=', 1)[1]
    return {'hosts': sanitize(hosts), 'config_file': config}


def read_config(path):
    result = {'path': str(path), 'exists': None, 'settings': {}, 'error': None}
    try:
        text = path.read_text()
        result['exists'] = True
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError('Expected a JSON object')
        result['settings'] = sanitize(raw)
    except FileNotFoundError:
        result['exists'] = False
    except (OSError, ValueError) as exc:
        # JSON errors include location, not the file contents.
        result['error'] = str(exc)
    return result


def inspect_local(endpoint, config_path=Path('/etc/docker/daemon.json'), proc=Path('/proc'), timeout=5):
    result = {'local': urlsplit(endpoint).scheme == 'unix', 'systemd': {},
              'processes': [], 'daemon_json': None, 'additional_configs': [],
              'warnings': []}
    if not result['local']:
        result['warnings'].append('Remote/TCP endpoint: local process and configuration evidence cannot establish its binding. Local files are not inspected.')
        return result
    result['daemon_json'] = read_config(config_path)
    systemctl = shutil.which('systemctl')
    if systemctl:
        try:
            command = [systemctl, 'show', 'docker.service', '--no-pager',
                       '--property=MainPID,FragmentPath,DropInPaths,ExecStart']
            output = subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
            properties = dict(line.split('=', 1) for line in output.stdout.splitlines() if '=' in line)
            result['systemd'] = {key: properties.get(key, '') for key in ('MainPID', 'FragmentPath', 'DropInPaths')}
            exec_start = properties.get('ExecStart', '')
            argv = re.search(r'argv\[\]=(.*?)(?:\s;|$)', exec_start)
            if argv:
                result['systemd']['configured_binding'] = binding_arguments(shlex.split(argv.group(1)))
            socket_info = subprocess.run([systemctl, 'show', 'docker.socket', '--no-pager', '--property=Listen'],
                                         check=True, capture_output=True, text=True, timeout=timeout)
            result['systemd']['socket_listen'] = socket_info.stdout.partition('=')[2].strip()
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            result['warnings'].append(f'Unable to inspect systemd Docker binding: {exc}')
    else:
        result['warnings'].append('systemctl unavailable; systemd binding source cannot be inspected')
    try:
        processes = list(proc.iterdir())
    except OSError as exc:
        result['warnings'].append(f'Unable to enumerate dockerd processes: {exc}')
        processes = []
    for directory in processes:
        if not directory.name.isdigit():
            continue
        try:
            if (directory / 'comm').read_text().strip() != 'dockerd':
                continue
            arguments = (directory / 'cmdline').read_bytes().decode(errors='replace').rstrip('\0').split('\0')
            result['processes'].append({'pid': int(directory.name), **binding_arguments(arguments)})
        except FileNotFoundError:
            continue
        except OSError as exc:
            result['warnings'].append(f'Cannot inspect process {directory.name}: {exc}')
    configs = [result['daemon_json']]
    for process in result['processes']:
        custom = process['config_file']
        if custom and Path(custom).is_absolute() and Path(custom) != config_path:
            if not any(item['path'] == custom for item in configs):
                item = read_config(Path(custom))
                configs.append(item)
                result['additional_configs'].append(item)
        elif custom and not Path(custom).is_absolute():
            result['warnings'].append('A relative --config-file path was found; its file is not inspected')
        selected = next((item for item in configs if item['path'] == (custom or str(config_path))), None)
        if process['hosts'] and selected and 'hosts' in selected['settings']:
            result['warnings'].append(f"PID {process['pid']}: hosts is specified both on the command line and in {selected['path']}. Docker normally rejects duplicate flag/file settings; the file may have changed since startup.")
    configured = result['systemd'].get('configured_binding', {})
    if configured.get('hosts') and result['daemon_json'] and 'hosts' in result['daemon_json']['settings']:
        result['warnings'].append('systemd ExecStart specifies -H/--host and daemon.json specifies hosts. This may prevent the next Docker start; neither setting overrides the other.')
    result['warnings'].append('Local evidence is not proof that this socket belongs to a particular dockerd. Current file/unit contents may differ from startup configuration.')
    return result
