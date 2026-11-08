"""Local Engine images and their observed container/service references."""

from urllib.parse import quote
import re

from backend.docker.client import DockerError
from backend.docker.containers import stack_identity


def canonical_reference(value):
    value = value.removeprefix('docker.io/').removeprefix('index.docker.io/').removeprefix('library/')
    if '@' in value:
        repository, digest = value.split('@', 1)
        if ':' in repository.rsplit('/', 1)[-1]:
            repository = repository.rsplit(':', 1)[0]
        return repository + '@' + digest
    if ':' not in value.rsplit('/', 1)[-1]:
        value += ':latest'
    return value


def inventory(client, include_storage=False):
    errors = []
    def read(label, function, fallback):
        try:
            return function()
        except (DockerError, OSError, ValueError) as exc:
            errors.append(f'{label}: {exc}')
            return fallback

    raw_images = client.request('/images/json', query={'all': 'true'})
    images, by_id, by_reference = [], {}, {}
    for raw in raw_images:
        tags = sorted(value for value in raw.get('RepoTags') or [] if value != '<none>:<none>')
        digests = sorted(raw.get('RepoDigests') or [])
        shared = raw.get('SharedSize', -1)
        size = raw.get('Size')
        item = {'id': raw['Id'], 'name': tags[0] if tags else digests[0] if digests else raw['Id'],
                'labels': raw.get('Labels') or {},
                'tags': tags, 'digests': digests, 'created': raw.get('Created'), 'size': size,
                'shared_size': shared if shared is not None and shared >= 0 else None,
                'unique_size': size - shared if size is not None and shared is not None and 0 <= shared <= size else None,
                'parent_id': raw.get('ParentId'), 'child_items': [],
                'container_items': [], 'service_items': [], 'stack_items': []}
        images.append(item); by_id[item['id']] = item
        for ref in tags + digests:
            by_reference[canonical_reference(ref)] = item

    def resolve(ref):
        return by_id.get(ref) or by_reference.get(canonical_reference(ref))

    for image in images:
        if image['parent_id'] is None:
            inspected = read('Image dependencies', lambda: client.request('/images/' + quote(image['id'], safe='') + '/json'), {})
            image['parent_id'] = inspected.get('Parent')
            if image['parent_id'] is None:
                errors.append(f'{image["name"]}: Engine did not expose image dependency metadata')
        parent = by_id.get(image['parent_id'])
        if parent is not None:
            parent['child_items'].append({'id': image['id'], 'name': image['name']})

    containers = read('Container references', lambda: client.request('/containers/json', query={'all': 'true'}), [])
    for raw in containers:
        # A retagged name must never associate an existing container with the new image.
        image = by_id.get(raw.get('ImageID'))
        if image is not None:
            image['container_items'].append({'id': raw['Id'], 'name': ', '.join(sorted(name.lstrip('/') for name in raw.get('Names', []))),
                                             'state': raw.get('State', 'unknown'), **stack_identity(raw.get('Labels') or {})})
    info = read('Swarm status', client.info, {})
    swarm = info.get('Swarm') or {}
    if swarm.get('ControlAvailable'):
        for raw in read('Service references', lambda: client.request('/services', query={'status': 'true'}), []):
            spec = raw.get('Spec') or {}
            ref = spec.get('TaskTemplate', {}).get('ContainerSpec', {}).get('Image', '')
            image = resolve(ref)
            if image is not None:
                image['service_items'].append({'id': raw.get('ID', ''), 'name': spec.get('Name', ''),
                    'running': (raw.get('ServiceStatus') or {}).get('RunningTasks'), **stack_identity(spec.get('Labels') or {})})
    elif swarm.get('LocalNodeState') == 'active':
        errors.append('Swarm worker: cluster-wide service image references require a manager endpoint.')
    for image in images:
        containers, services = image['container_items'], image['service_items']
        image['containers'], image['services'] = len(containers), len(services)
        image['children'] = len(image['child_items'])
        image['child_items'].sort(key=lambda item: (item['name'].casefold(), item['id']))
        image['active'] = sum(item['state'] in {'running', 'paused', 'restarting'} for item in containers)
        image['usage'] = ('active' if image['active'] or any((item['running'] or 0) > 0 for item in services)
                          else 'referenced' if containers or services or image['child_items'] else 'unknown' if errors else 'unused')
        stacks = {(item['stack_kind'], item['stack']) for item in containers + services if item['stack']}
        image['stack_items'] = [{'kind': kind, 'name': name} for kind, name in sorted(stacks)]
        for field in ('container_items', 'service_items'):
            image[field].sort(key=lambda item: (item['name'].casefold(), item['id']))
    images.sort(key=lambda item: (item['name'].casefold(), item['id']))
    storage = {'images': len(images), 'active': sum(item['usage'] == 'active' for item in images),
               'unused_image_ids': [item['id'] for item in images if item['usage'] == 'unused'],
               'unused_size': sum(item['size'] for item in images if item['usage'] == 'unused') if all(item['size'] is not None for item in images if item['usage'] == 'unused') else None,
               'referenced': sum(item['usage'] == 'referenced' for item in images),
               'unused': sum(item['usage'] == 'unused' for item in images),
               'unknown': sum(item['usage'] == 'unknown' for item in images),
               'logical_size': sum(item['size'] for item in images) if all(item['size'] is not None for item in images) else None,
               'layers_size': None,
               'unique_size': sum(item['unique_size'] for item in images) if all(item['unique_size'] is not None for item in images) else None}
    if include_storage:
        disk = read('Image storage statistics', lambda: client.request('/system/df'), {})
        layers_size = disk.get('LayersSize')
        storage['layers_size'] = layers_size if isinstance(layers_size, int) and layers_size >= 0 else None
    return {'images': images, 'errors': errors, 'storage': storage,
            'note': 'Size is the image layer size, not additional disk usage. Layers can be shared; image sizes must not be summed. Shared/unique sizes are unknown when the Engine does not report them. References cover this endpoint and, on a Swarm manager, service specifications; compose files on disk are not scanned.'}


def storage_snapshot(client, timeout=None):
    """Read expensive layer accounting independently of image inventory."""
    disk = client.request('/system/df', **({'timeout': timeout} if timeout is not None else {}))
    total = disk.get('LayersSize')
    images = {}
    for raw in disk.get('Images') or []:
        size, shared = raw.get('Size'), raw.get('SharedSize')
        images[raw['Id']] = {'shared_size': shared if isinstance(shared, int) and shared >= 0 else None,
                            'unique_size': size - shared if isinstance(size, int) and isinstance(shared, int) and 0 <= shared <= size else None}
    return {'layers_size': total if isinstance(total, int) and total >= 0 else None, 'images': images}


def details(client, reference):
    if not isinstance(reference, str) or not reference or len(reference) > 1024 or any(ord(char) < 33 for char in reference):
        raise ValueError('A valid image reference is required')
    raw = client.request('/images/' + quote(reference, safe='') + '/json')
    data = inventory(client)
    item = next((item for item in data['images'] if item['id'] == raw.get('Id')), None)
    if item is None:
        item = {'id': raw.get('Id', reference), 'name': reference, 'tags': raw.get('RepoTags') or [],
                'digests': raw.get('RepoDigests') or [], 'usage': 'unknown', 'size': raw.get('Size'),
                'shared_size': None, 'unique_size': None, 'container_items': [], 'service_items': [], 'stack_items': []}
    return {**item, 'created': raw.get('Created'), 'os': raw.get('Os'), 'architecture': raw.get('Architecture'),
            'labels': (raw.get('Config') or {}).get('Labels') or {},
            'variant': raw.get('Variant'), 'layers': len((raw.get('RootFS') or {}).get('Layers') or []),
            'errors': data['errors'], 'note': data['note']}


def delete_images(client, ids, apply=False, expected=None):
    if not isinstance(ids, list) or not 1 <= len(ids) <= 1000 or any(
            not isinstance(value, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', value) for value in ids):
        raise ValueError('Select 1–1000 full immutable image IDs')
    if not isinstance(apply, bool):
        raise ValueError('apply must be a boolean')
    ids = list(dict.fromkeys(ids))
    data = inventory(client, include_storage=False)
    known = {item['id']: item for item in data['images']}
    items, errors, deleted = [], [], []
    for image_id in ids:
        item = known.get(image_id)
        if item is None:
            errors.append(f'{image_id}: image no longer exists')
        elif item['usage'] != 'unused':
            if item['child_items']:
                errors.append(f'{item["name"]}: cannot delete an image with dependent child images: ' + ', '.join(child['name'] for child in item['child_items']))
            else:
                errors.append(f'{item["name"]}: image is {item["usage"]}; only images without container, service or child-image references can be deleted')
        elif apply and (not isinstance(expected, dict) or expected.get(image_id) != item['tags']):
            errors.append(f'{item["name"]}: tags changed or confirmation is missing; review again')
        else:
            items.append({'id': image_id, 'name': item['name'], 'tags': item['tags'], 'size': item['size']})
    # Reject a stale/invalid selection before starting any mutation.
    if apply and not errors:
        for item in items:
            try:
                client.request('/images/' + quote(item['id'], safe=''), method='DELETE', query={'force': 'false', 'noprune': 'true'})
                deleted.append(item['id'])
            except (DockerError, OSError, ValueError) as exc:
                errors.append(str(exc))
    return {'items': items, 'errors': errors, 'deleted': deleted, 'applied': apply}
