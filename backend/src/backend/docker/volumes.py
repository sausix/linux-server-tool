"""Engine volumes with observed references; never walk host mountpoints."""

import hashlib
import json
import re
from urllib.parse import quote

from backend.docker.client import DockerError
from backend.docker.containers import stack_identity


def fingerprint(item):
    fields = {key: item.get(key) for key in ('Name', 'Driver', 'CreatedAt', 'Mountpoint', 'Labels', 'Options', 'Scope')}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def inventory(client):
    raw_volumes = client.request('/volumes').get('Volumes') or []
    errors = []
    try:
        containers = client.request('/containers/json', query={'all': 'true'})
    except (DockerError, OSError, ValueError) as exc:
        errors.append(f'Container references: {exc}')
        containers = []
    services = []
    try:
        swarm = (client.info().get('Swarm') or {})
        if swarm.get('ControlAvailable'):
            services = client.request('/services')
        elif swarm.get('LocalNodeState') == 'active':
            errors.append('Swarm worker: service references require a manager endpoint.')
    except (DockerError, OSError, ValueError) as exc:
        errors.append(f'Service references: {exc}')
    volumes = []
    for raw in raw_volumes:
        name = raw['Name']
        refs, service_refs = [], []
        for container in containers:
            mounts = [mount for mount in container.get('Mounts') or [] if mount.get('Type') == 'volume' and mount.get('Name') == name]
            if mounts:
                refs.append({'id': container['Id'], 'name': ', '.join(sorted(n.lstrip('/') for n in container.get('Names') or [])),
                             'state': container.get('State', ''), 'destinations': sorted(m.get('Destination', '') for m in mounts),
                             'service': (container.get('Labels') or {}).get('com.docker.compose.service') or (container.get('Labels') or {}).get('com.docker.swarm.service.name', ''),
                             **stack_identity(container.get('Labels') or {})})
        for service in services:
            spec = service.get('Spec') or {}
            mounts = spec.get('TaskTemplate', {}).get('ContainerSpec', {}).get('Mounts') or []
            # Templated Swarm sources cannot be resolved reliably from the spec.
            if any(m.get('Type') == 'volume' and (m.get('Source') == name or '{{' in m.get('Source', '')) for m in mounts):
                service_refs.append({'id': service.get('ID', ''), 'name': spec.get('Name', ''), **stack_identity(spec.get('Labels') or {})})
        for ref in refs:
            if ref['service'] and not any(service['name'] == ref['service'] and service.get('stack') == ref.get('stack') for service in service_refs):
                service_refs.append({'id': '', 'name': ref['service'], 'stack': ref['stack'], 'stack_kind': ref['stack_kind']})
        service_refs.sort(key=lambda ref: (ref['name'].casefold(), ref.get('stack', ''), ref['id']))
        active = any(ref['state'] in {'running', 'paused', 'restarting'} for ref in refs)
        usage = 'active' if active else 'referenced' if refs or service_refs else 'unknown' if errors else 'unused'
        stacks = {(ref['stack_kind'], ref['stack']) for ref in refs + service_refs if ref.get('stack')}
        owner = stack_identity(raw.get('Labels') or {})
        if owner.get('stack'):
            stacks.add((owner['stack_kind'], owner['stack']))
        accounting = raw.get('UsageData') or {}
        size = accounting.get('Size')
        volumes.append({'id': name, 'name': name, 'driver': raw.get('Driver', ''), 'scope': raw.get('Scope', ''),
                        'created': raw.get('CreatedAt'), 'labels': raw.get('Labels') or {}, 'options': raw.get('Options') or {},
                        'anonymous': 'com.docker.volume.anonymous' in (raw.get('Labels') or {}),
                        'usage': usage, 'state': usage, 'size': size if isinstance(size, int) and size >= 0 else None,
                        'container': sorted({ref['name'] for ref in refs} | {ref['id'] for ref in refs}),
                        'stack': sorted({stack for _, stack in stacks}),
                        'service': sorted({ref['name'] for ref in service_refs}),
                        'container_items': sorted(refs, key=lambda ref: (ref['name'].casefold(), ref['id'])),
                        'service_items': service_refs,
                        'stack_items': [{'kind': kind, 'name': stack} for kind, stack in sorted(stacks)],
                        'fingerprint': fingerprint(raw)})
    return {'volumes': sorted(volumes, key=lambda item: (item['anonymous'], item['name'].casefold(), item['name'])), 'errors': errors,
            'note': 'Usage covers containers visible to this Engine and service specifications on a Swarm manager. Compose files alone do not reserve volumes. Unknown sizes are not scanned from host paths.'}


def details(client, name):
    if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', name):
        raise ValueError('A valid volume name is required')
    raw = client.request('/volumes/' + quote(name, safe=''))
    data = inventory(client)
    item = next((item for item in data['volumes'] if item['name'] == name), None)
    if item is None:
        raise ValueError('Volume disappeared while reading its references; refresh again')
    return {**item, 'inspect': raw, 'errors': data['errors'], 'note': data['note']}


def storage(client, timeout=None):
    disk = client.request('/system/df', **({'timeout': timeout} if timeout is not None else {}))
    return {'volumes': {raw['Name']: (raw.get('UsageData') or {}).get('Size') for raw in disk.get('Volumes') or []}}


def delete_volumes(client, ids, apply=False, expected=None):
    if not isinstance(ids, list) or not 1 <= len(ids) <= 1000 or any(
            not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', name) for name in ids):
        raise ValueError('Select 1–1000 valid volume names')
    if not isinstance(apply, bool):
        raise ValueError('apply must be a boolean')
    data = inventory(client)
    known = {item['name']: item for item in data['volumes']}
    items, errors, deleted = [], [], []
    for name in dict.fromkeys(ids):
        item = known.get(name)
        if item is None:
            errors.append(f'{name}: volume no longer exists')
        elif item['usage'] != 'unused':
            errors.append(f'{name}: volume is {item["usage"]}; only volumes without container or service references can be deleted')
        elif apply and (not isinstance(expected, dict) or expected.get(name) != item['fingerprint']):
            errors.append(f'{name}: volume changed or confirmation is missing; review again')
        else:
            items.append(item)
    if apply and not errors:
        for item in items:
            try:
                raw = client.request('/volumes/' + quote(item['name'], safe=''))
                if fingerprint(raw) != item['fingerprint']:
                    raise ValueError(f'{item["name"]}: volume changed; review again')
                client.request('/volumes/' + quote(item['name'], safe=''), method='DELETE', query={'force': 'false'})
                deleted.append(item['name'])
            except (DockerError, OSError, ValueError) as exc:
                errors.append(str(exc))
    return {'items': items, 'errors': errors, 'deleted': deleted, 'applied': apply}
