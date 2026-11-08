"""Small read-only overviews for resources linked from the label inventory."""

KINDS = {'services', 'container_templates', 'networks', 'volumes', 'nodes', 'engines'}


def inventory(client, kind):
    if kind not in KINDS:
        raise ValueError('Unknown Docker resource kind')
    items = []
    def add(identity, name, labels, state='', driver=''):
        items.append({'id': identity, 'name': name, 'labels': labels or {}, 'state': state, 'driver': driver})
    if kind == 'networks':
        for raw in client.networks():
            add(raw.get('Id', raw['Name']), raw['Name'], raw.get('Labels'), raw.get('Scope', ''), raw.get('Driver', ''))
    elif kind == 'volumes':
        for raw in client.request('/volumes').get('Volumes') or []:
            add(raw['Name'], raw['Name'], raw.get('Labels'), raw.get('Scope', ''), raw.get('Driver', ''))
    else:
        info = client.info()
        if kind == 'engines':
            labels = {key: value for label in info.get('Labels') or [] for key, _, value in [label.partition('=')]}
            add(info.get('ID', 'endpoint'), info.get('Name', 'Engine'), labels)
        else:
            if not (info.get('Swarm') or {}).get('ControlAvailable'):
                raise ValueError('A Swarm manager endpoint is required')
            for raw in client.request('/nodes' if kind == 'nodes' else '/services'):
                spec = raw.get('Spec') or {}
                labels = spec.get('Labels')
                if kind == 'container_templates':
                    labels = spec.get('TaskTemplate', {}).get('ContainerSpec', {}).get('Labels')
                name = (raw.get('Description') or {}).get('Hostname', raw['ID']) if kind == 'nodes' else spec.get('Name', raw['ID'])
                add(raw['ID'], name, labels, (raw.get('Status') or {}).get('State', ''))
    return {'resources': sorted(items, key=lambda item: (item['name'].casefold(), item['id']))}
