"""Aggregate Docker labels by key/value and count distinct owning resources."""

from backend.docker.client import DockerError

RESOURCE_TYPES = ('stacks', 'containers', 'images', 'services', 'container_templates', 'networks', 'volumes', 'nodes', 'engines')


def inventory(client, stacks=()):
    groups, errors = {}, []

    def read(kind, function, fallback):
        try:
            return function()
        except (DockerError, OSError, ValueError) as exc:
            errors.append(f'{kind}: {exc}')
            return fallback

    def add(kind, identity, labels):
        for key, value in (labels or {}).items():
            group = groups.setdefault((str(key), str(value)), {name: set() for name in RESOURCE_TYPES})
            group[kind].add(identity)

    for stack in stacks:
        add('stacks', (stack.get('base_kind', stack['kind']), stack['name']), stack.get('labels'))

    for raw in read('Containers', lambda: client.request('/containers/json', query={'all': 'true'}), []):
        add('containers', raw['Id'], raw.get('Labels'))
    for raw in read('Images', lambda: client.request('/images/json', query={'all': 'true'}), []):
        add('images', raw['Id'], raw.get('Labels'))
    for raw in read('Networks', client.networks, []):
        add('networks', raw.get('Id') or raw.get('Name'), raw.get('Labels'))
    volumes = read('Volumes', lambda: client.request('/volumes'), {})
    for raw in volumes.get('Volumes') or []:
        add('volumes', raw['Name'], raw.get('Labels'))
    info = read('Engine', client.info, {})
    labels = {}
    for label in info.get('Labels') or []:
        key, _, value = label.partition('=')
        labels[key] = value
    add('engines', info.get('ID', 'endpoint'), labels)
    swarm = info.get('Swarm') or {}
    if swarm.get('ControlAvailable'):
        for kind, path in [('services', '/services'), ('nodes', '/nodes')]:
            for raw in read(kind.title(), lambda: client.request(path), []):
                spec = raw.get('Spec') or {}
                add(kind, raw['ID'], spec.get('Labels'))
                if kind == 'services':
                    add('container_templates', raw['ID'], spec.get('TaskTemplate', {}).get('ContainerSpec', {}).get('Labels'))
    elif swarm.get('LocalNodeState') == 'active':
        errors.append('Swarm service and node labels require a manager endpoint.')
    return {'labels': [{'key': key, 'value': value, **{kind: len(ids) for kind, ids in counts.items()}}
                       for (key, value), counts in sorted(groups.items())],
            'errors': errors,
            'note': 'One row per label key/value. Counts represent distinct owning resources on this endpoint; services, container templates and nodes are cluster-wide on a Swarm manager. Template labels and labels on actual containers are counted separately. Partial read failures make counts incomplete.'}
