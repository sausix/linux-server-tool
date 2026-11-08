"""Docker presentation owns its theme and optional interface decorations."""
from urllib.parse import quote
from frontend.ui.models import badge

ACCENT = '#77bdff'
COLORS = {'category-docker': ACCENT, 'category-docker-tint': '#1b334c'}


def stack_link(stack, text=None):
    name = stack.get('name') or stack.get('stack', '')
    path = '/docker/stacks/' + quote(stack.get('kind', 'compose'), safe='') + '/' + quote(name, safe='')
    return badge(text or 'Stack: ' + name, ACCENT, path, 'stacks')


def extend_interfaces(data, ui):
    entries = {}
    def patch(name):
        return entries.setdefault(name, {'accentcolor': ACCENT, 'links': [], 'badges': [], 'columns': {}})
    for name, origin in data.get('interface_origins', {}).items():
        if origin == 'docker':
            patch(name)
    for name, stacks in data.get('interface_stacks', {}).items():
        patch(name)['links'] = [stack_link(stack) for stack in stacks]
    for link in data.get('interfaces', []):
        if link.get('docker_networks'):
            patch(link['name'])
        if link.get('docker_stacks'):
            patch(link['name'])['links'] = [stack_link(stack) for stack in link['docker_stacks']]
        for port in link.get('ports', []):
            if port.get('docker_services'):
                target = patch(port['name'])
                target['columns'] = {
                    'docker.service': {'label': 'Service', 'items': [stack_link(item, item['service']) for item in port['docker_services']]},
                    'docker.mac': {'label': 'Internal MAC', 'items': [badge(item['internal_mac'], ACCENT) for item in port['docker_services'] if item.get('internal_mac')]}}
    ui.replace('docker', entries, [{'subnet': item, 'accentcolor': ACCENT} for item in data.get('docker_subnets', [])])
