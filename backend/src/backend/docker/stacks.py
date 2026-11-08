"""Discover runtime stacks and enrich Compose projects with local/virtual metadata."""

from backend.docker.client import DockerError
from backend.docker.networks import local_interface_names, network_interface, annotate_interfaces, container_endpoints, associate_endpoints
from backend.docker.containers import display_state


def snapshot(client, interface_names=None, settings=None, virtual_definitions=(), interface_links=None, peer_links=None, model_registry=None):
    groups, errors = {}, []
    interface_names = local_interface_names() if interface_names is None else interface_names

    def read(label, function, fallback):
        try:
            return function()
        except (DockerError, OSError, ValueError) as exc:
            errors.append(f'{label}: {exc}')
            return fallback

    def group(labels):
        if labels.get('com.docker.compose.project'):
            kind, name = 'compose', labels['com.docker.compose.project']
        elif labels.get('com.docker.stack.namespace'):
            kind, name = 'swarm', labels['com.docker.stack.namespace']
        else:
            return None
        stack = groups.setdefault((kind, name), {'kind': kind, 'base_kind': kind, 'name': name,
            'container_items': [], 'service_items': [], 'network_items': [], 'volume_items': [],
            'config_files': [], 'working_directory': None})
        files = list(filter(None, labels.get('com.docker.compose.project.config_files', '').split(',')))
        if files and not stack['config_files']:
            stack['config_files'] = files
        stack['working_directory'] = labels.get('com.docker.compose.project.working_dir') or stack['working_directory']
        return stack

    containers = read('Containers', lambda: client.request('/containers/json', query={'all': 'true'}), [])
    for container in containers:
        labels = container.get('Labels') or {}
        stack = group(labels)
        if stack is None:
            continue
        stack['container_items'].append({
            'id': container.get('Id', ''), 'name': ', '.join(name.lstrip('/') for name in container.get('Names', [])),
            'image': container.get('Image', ''), 'image_id': container.get('ImageID', ''), 'state': container.get('State', 'unknown'),
            'status': container.get('Status', ''),
            'service': labels.get('com.docker.compose.service') or labels.get('com.docker.swarm.service.name', ''),
            'service_id': labels.get('com.docker.swarm.service.id', ''),
            'labels': labels,
            'created': container.get('Created', 0),
            'display_state': display_state(container.get('State', 'unknown'),
                'unhealthy' if '(unhealthy)' in container.get('Status', '') else 'starting' if 'health: starting' in container.get('Status', '') else ''),
        })
        stack['working_directory'] = labels.get('com.docker.compose.project.working_dir') or stack['working_directory']
    networks = read('Networks', client.networks, [])
    endpoints = [endpoint for container in containers for endpoint in container_endpoints(container)]
    associate_endpoints(endpoints, networks, interface_links or [], peer_links)
    if interface_links is not None:
        annotate_interfaces(interface_links, networks, containers, peer_links)
    local_links = {link['name']: link for link in interface_links or []}
    for network in networks:
        stack = group(network.get('Labels') or {})
        if stack is not None:
            stack['network_items'].append({'name': network.get('Name', ''), 'driver': network.get('Driver', ''), 'scope': network.get('Scope', ''),
                                          'labels': network.get('Labels') or {},
                                          **network_interface(network, interface_names),
                                          'endpoints': sorted([endpoint for endpoint in endpoints if
                                              (endpoint['network_id'] == network['Id'] if endpoint['network_id'] and network.get('Id') else endpoint['name'] == network.get('Name'))],
                                              key=lambda endpoint: (endpoint['service'].casefold(), endpoint['container'].casefold(), endpoint['container_id'])),
                                          'bridge_ports': sorted(local_links.get(network_interface(network, interface_names)['interface'], {}).get('ports', []), key=lambda port: port['name'])})
    volumes = read('Volumes', lambda: client.request('/volumes'), {})
    for volume in volumes.get('Volumes') or []:
        stack = group(volume.get('Labels') or {})
        if stack is not None:
            stack['volume_items'].append({'name': volume.get('Name', ''), 'driver': volume.get('Driver', ''), 'labels': volume.get('Labels') or {}})
    info = read('Swarm status', client.info, {})
    swarm = info.get('Swarm') or {}
    if swarm.get('ControlAvailable'):
        services = read('Swarm services', lambda: client.request('/services', query={'status': 'true'}), [])
        for service in services:
            spec = service.get('Spec') or {}
            stack = group(spec.get('Labels') or {})
            if stack is None:
                continue
            status = service.get('ServiceStatus') or {}
            mode = spec.get('Mode') or {}
            stack['service_items'].append({'name': spec.get('Name', ''), 'id': service.get('ID', ''),
                'labels': spec.get('Labels') or {},
                'container_labels': spec.get('TaskTemplate', {}).get('ContainerSpec', {}).get('Labels') or {},
                'image': spec.get('TaskTemplate', {}).get('ContainerSpec', {}).get('Image', ''),
                'mode': next(iter(mode), 'unknown'), 'running': status.get('RunningTasks'),
                'desired': status.get('DesiredTasks')})
    elif swarm.get('LocalNodeState') == 'active':
        errors.append('Swarm worker: stack services and cluster-wide replica counts require a manager endpoint.')
    for virtual in virtual_definitions:
        group({'com.docker.compose.project': virtual['name']})
    if model_registry is not None:
        for name in model_registry.names():
            stack = group({'com.docker.compose.project': name})
            origin = model_registry.inputs[name]['origin']
            if origin['type'] == 'file' and (not stack['config_files'] or stack['config_files'] == ['-']):
                stack.update(config_files=origin.get('files', []), working_directory=origin.get('directory'))
    if settings is not None:
        from backend.docker.compose.managed import projects
        for project in projects(settings):
            stack = group({'com.docker.compose.project': project['name']})
            if not stack['config_files']:
                stack.update(config_files=project['config_files'], working_directory=project['working_directory'])
    for stack in groups.values():
        items = stack['container_items']
        if stack['kind'] == 'compose':
            names = sorted({item['service'] for item in items if item['service']})
            stack['service_items'] = [{'name': name, 'mode': 'compose',
                'running': sum(item['state'] == 'running' for item in items if item['service'] == name),
                'desired': None, 'image': ', '.join(sorted({item['image'] for item in items if item['service'] == name}))}
                for name in names]
        known_services = {service['name'] for service in stack['service_items']}
        for name in sorted({item['service'] for item in items if item['service']} - known_services):
            stack['service_items'].append({'name': name, 'mode': 'unknown', 'image': '',
                                           'running': None, 'desired': None})
        for service in stack['service_items']:
            children = [item for item in items if (service.get('id') and item['service_id'] == service['id']) or item['service'] == service['name']]
            if stack['kind'] == 'compose':
                service['image_items'] = [{'name': name, 'id': image_id} for name, image_id in
                                          sorted({(item['image'], item['image_id']) for item in children if item['image']})]
            states = {item['display_state'] for item in children}
            if service['desired'] is not None and service['running'] is not None:
                service['state'] = ('stopped' if service['desired'] == 0 else
                                    'running' if service['running'] == service['desired'] else 'degraded')
            elif stack['kind'] == 'compose':
                service['state'] = ('running' if states == {'running'} else 'partial' if 'running' in states
                                    else 'stopped' if states == {'stopped'} else 'unknown')
            else:
                service['state'] = 'unknown'
            for state in ('starting', 'unhealthy', 'failed'):
                if state in states:
                    service['state'] = state
            if errors:
                service['state'] = 'unknown'
        stack['containers'] = len(items)
        stack['running'] = sum(item['state'] == 'running' for item in items)
        stack['stopped'] = sum(item['state'] not in {'running', 'paused', 'restarting'} for item in items)
        stack['unhealthy'] = sum('(unhealthy)' in item['status'] for item in items)
        stack['services'] = len(stack['service_items'])
        stack['networks'] = len(stack['network_items'])
        stack['volumes'] = len(stack['volume_items'])
        stack['desired_replicas'] = None
        stack['running_replicas'] = None
        if stack['kind'] == 'swarm':
            services = stack['service_items']
            known = bool(services) and all(item['desired'] is not None and item['running'] is not None for item in services)
            if known:
                stack['desired_replicas'] = sum(item['desired'] for item in services)
                stack['running_replicas'] = sum(item['running'] for item in services)
                stack['state'] = 'scaled-down' if not stack['desired_replicas'] else 'running' if stack['desired_replicas'] == stack['running_replicas'] else 'degraded'
            else:
                stack['state'] = 'unknown'
        else:
            stack['state'] = ('failed' if any(item['state'] == 'dead' for item in items)
                              else 'unhealthy' if stack['unhealthy']
                              else 'starting' if any(item['state'] in {'created', 'restarting'} or 'health: starting' in item['status'] for item in items)
                              else 'running' if items and stack['running'] == len(items)
                              else 'partial' if stack['running'] else 'stopped' if items else 'empty')
        stack['incomplete'] = bool(errors)
        for field in ('container_items', 'service_items', 'network_items', 'volume_items'):
            stack[field].sort(key=lambda item: (item['name'].casefold(), item['name'], item.get('id', '')))
        if errors:
            stack['state'] = 'unknown'
    stacks = list(groups.values())
    if settings is not None:
        from backend.docker.compose.metadata import enrich_stacks
        enrich_stacks(stacks, client, settings, virtual_definitions, model_registry)
    return {'stacks': sorted(stacks, key=lambda item: (item['kind'], item['name'])),
            'errors': errors, 'note': 'Inventory covers discovered resources and saved definitions. Use explicit stack controls to apply saved configuration. Swarm status is cluster-wide only on a manager.'}


def runtime_info(client, stack):
    """Only called for a detail view; unknown start times remain unknown."""
    from datetime import datetime, timezone
    import re
    created, started = [], []
    tree = (stack.get('compose_model') or {}).get('tree') or {}
    selected = next((node for node in tree.get('nodes', []) if node['id'] == tree.get('running')), {})
    context = selected.get('context') or selected.get('origin') or {}
    directory = context.get('directory') or stack.get('working_directory')
    for container in stack.get('container_items', []):
        candidate = None
        stamp = container.get('created')
        if type(stamp) in {int, float} and stamp > 0:
            try:
                candidate = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
            except (ValueError, OverflowError, OSError):
                pass
        if container.get('state') == 'running' and re.fullmatch(r'[a-fA-F0-9]{12,64}', container.get('id', '')):
            try:
                raw = client.request('/containers/' + container['id'] + '/json')
                if isinstance(raw, dict):
                    value = (raw.get('State') or {}).get('StartedAt')
                    if isinstance(value, str) and value and not value.startswith('0001-'):
                        started.append(value)
                    if isinstance(raw.get('Created'), str) and raw['Created']:
                        candidate = raw['Created']
            except (DockerError, OSError, ValueError, KeyError):
                pass
        if candidate:
            created.append(candidate)
    return {'directory': directory, 'created': min(created) if created else None,
            'started': max(started) if started else None}
