"""Read-only container inventory with explicitly selected, non-secret fields."""

import re
from backend.docker.networks import container_endpoints


def stack_identity(labels):
    for label, kind in [('com.docker.compose.project', 'compose'), ('com.docker.stack.namespace', 'swarm')]:
        if labels.get(label):
            return {'stack': labels[label], 'stack_kind': kind}
    return {'stack': '', 'stack_kind': ''}


def display_state(state, health=''):
    if state == 'dead':
        return 'failed'
    if health == 'unhealthy':
        return 'unhealthy'
    if state in {'created', 'restarting'} or health == 'starting':
        return 'starting'
    return 'stopped' if state == 'exited' else state


def inventory(client):
    items = []
    for raw in client.request('/containers/json', query={'all': 'true'}):
        labels = raw.get('Labels') or {}
        status = raw.get('Status', '')
        health = 'unhealthy' if '(unhealthy)' in status else 'starting' if 'health: starting' in status else 'healthy' if '(healthy)' in status else ''
        items.append({'id': raw.get('Id', ''), 'name': ', '.join(sorted(name.lstrip('/') for name in raw.get('Names', []))),
                      'image': raw.get('Image', ''), 'image_id': raw.get('ImageID', ''), 'state': raw.get('State', 'unknown'),
                      'display_state': display_state(raw.get('State', 'unknown'), health),
                      'health': health, 'status': status, 'created': raw.get('Created'),
                      'labels': labels,
                      'service': labels.get('com.docker.compose.service') or labels.get('com.docker.swarm.service.name', ''),
                      **stack_identity(labels)})
    items.sort(key=lambda item: (item['stack'].casefold(), item['stack_kind'], item['name'].casefold(), item['id']))
    return {'containers': items}


def details(client, container_id):
    if not isinstance(container_id, str) or not re.fullmatch(r'[a-fA-F0-9]{12,64}', container_id):
        raise ValueError('A container ID of 12–64 hexadecimal characters is required')
    raw = client.request(f'/containers/{container_id}/json')
    config, state = raw.get('Config') or {}, raw.get('State') or {}
    health = (state.get('Health') or {}).get('Status', '')
    return {'id': raw.get('Id', container_id), 'name': raw.get('Name', '').lstrip('/'),
            'labels': config.get('Labels') or {},
            'image': config.get('Image', ''), 'image_id': raw.get('Image', ''),
            'state': state.get('Status', 'unknown'), 'health': health,
            'display_state': display_state(state.get('Status', 'unknown'), health),
            'created': raw.get('Created'), 'started': state.get('StartedAt'), 'finished': state.get('FinishedAt'),
            'exit_code': state.get('ExitCode'), 'restart_count': raw.get('RestartCount'),
            'restart_policy': (raw.get('HostConfig') or {}).get('RestartPolicy', {}).get('Name', ''),
            'networks': container_endpoints(raw),
            'mounts': sorted([{'type': item.get('Type', ''), 'name': item.get('Name', ''),
                              'destination': item.get('Destination', ''), 'writable': bool(item.get('RW'))}
                             for item in raw.get('Mounts') or []], key=lambda item: item['destination']),
            **stack_identity(config.get('Labels') or {})}
