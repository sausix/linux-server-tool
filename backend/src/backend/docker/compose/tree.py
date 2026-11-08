"""Immutable Compose snapshots and named overlays, independent of source permissions."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import uuid


def digest(document):
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def apply_patch(document, operations):
    result = deepcopy(document)
    for operation in operations:
        path = operation['path']
        if not path:
            result = deepcopy(operation['value'])
            continue
        keys = [key.replace('~1', '/').replace('~0', '~') for key in path[1:].split('/')]
        target = result
        for key in keys[:-1]:
            target = target[key]
        if operation['op'] == 'remove':
            del target[keys[-1]]
        else:
            target[keys[-1]] = deepcopy(operation['value'])
    return result


def document(tree, node_id):
    nodes = {node['id']: node for node in tree['nodes']}
    chain, seen = [], set()
    while node_id:
        if node_id in seen or node_id not in nodes:
            raise ValueError('Invalid definition hierarchy')
        seen.add(node_id)
        node = nodes[node_id]
        if 'definition' in node:
            result = deepcopy(node['definition'])
            break
        chain.append(node)
        node_id = node['parent']
    else:
        raise ValueError('Definition hierarchy has no source snapshot')
    for node in reversed(chain):
        result = apply_patch(result, node['patch'])
    return result


def node(label, kind, parent=None, **content):
    return {'id': uuid.uuid4().hex, 'label': label, 'kind': kind, 'parent': parent,
            'created': datetime.now(timezone.utc).isoformat(), **content}


def sync(tree, desired, origin, baseline=None, evidence='unknown'):
    tree = deepcopy(tree) if tree else {'revision': 0, 'nodes': [], 'active': None, 'running': None, 'source': None}
    source = next((item for item in tree['nodes'] if item['id'] == tree['source']), None)
    if not source or source['hash'] != digest(desired) or source.get('origin') != origin:
        source_label = ', '.join(origin.get('files', [])) or origin.get('label', 'Initial definition')
        fresh = node(source_label if not source else source_label + ' · ' + datetime.now(timezone.utc).isoformat(timespec='seconds'),
                     'source', tree['nodes'][0]['id'] if source else None,
                     definition=deepcopy(desired), hash=digest(desired), origin=deepcopy(origin))
        if not tree['active'] or tree['active'] == tree['source']:
            tree['active'] = fresh['id']
        tree['source'] = fresh['id']
        tree['nodes'].append(fresh)
    if baseline is not None:
        candidates = [item for item in tree['nodes'] if item['hash'] == digest(baseline)]
        current = next((item for item in candidates if item['id'] == tree['running']), None)
        if not current:
            current = candidates[-1] if candidates else None
        if current:
            tree['running'] = current['id']
        tree['running_evidence'] = evidence
    return tree


def summary(tree):
    if not tree:
        return None
    result = {key: deepcopy(value) for key, value in tree.items() if key != 'nodes'}
    result['nodes'] = [{key: deepcopy(value) for key, value in item.items() if key not in {'definition', 'patch'}} for item in tree['nodes']]
    return result


def editable(tree, selected):
    return (selected['kind'] not in {'source', 'baseline'} and selected['id'] != tree['source'] and selected['id'] != tree['running']
            and not any(item['parent'] == selected['id'] for item in tree['nodes']))


def deletable(tree, selected):
    return editable(tree, selected) and selected['id'] != tree['active']


def change(tree, action, node_id=None, label=None, definition=None, identity=None, context=None):
    from backend.docker.compose.models import json_patch
    tree = deepcopy(tree)
    selected = next((item for item in tree['nodes'] if item['id'] == node_id), None)
    if action != 'new' and not selected:
        raise ValueError('Definition not found; reload the hierarchy')
    if action in {'add', 'new', 'edit', 'duplicate'}:
        if action == 'duplicate' and not label:
            label = selected['label'][:153] + ' (copy)'
        if not isinstance(label, str) or not label.strip() or len(label) > 160 or any(ord(char) < 32 for char in label):
            raise ValueError('Use a nonempty definition name of at most 160 characters')
    if action == 'add':
        fresh = node(label.strip(), 'overlay', node_id, patch=json_patch(document(tree, node_id), definition), hash=digest(definition),
                     context=deepcopy(selected.get('context') or selected.get('origin') or {}))
        tree['nodes'].append(fresh)
    elif action in {'duplicate', 'new'}:
        value = (definition if definition is not None else document(tree, node_id)) if action == 'duplicate' else definition
        fresh = node(label.strip(), 'definition', selected['parent'] if action == 'duplicate' else None,
                     definition=deepcopy(value), hash=digest(value),
                     context=deepcopy((selected.get('context') or selected.get('origin') or {}) if selected else context or {}))
        if identity:
            if any(item['id'] == identity for item in tree['nodes']):
                raise ValueError('Definition already exists')
            fresh['id'] = identity
        tree['nodes'].append(fresh)
    elif action == 'edit':
        if not editable(tree, selected):
            raise ValueError('This definition is read-only; duplicate or derive it to make changes')
        if 'patch' in selected:
            selected['patch'] = json_patch(document(tree, selected['parent']), definition)
        else:
            selected['definition'] = deepcopy(definition)
        selected.update(label=label.strip(), hash=digest(definition), modified=datetime.now(timezone.utc).isoformat())
    elif action == 'activate':
        tree['active'] = node_id
    elif action == 'delete':
        if not deletable(tree, selected):
            raise ValueError('This definition cannot be deleted while it is a source, Active, Running or has children')
        tree['nodes'] = [item for item in tree['nodes'] if item['id'] != node_id]
    else:
        raise ValueError('Unknown definition hierarchy action')
    if len(tree['nodes']) > 128 or len(json.dumps(tree).encode()) > 16 * 1024 * 1024:
        raise ValueError('Definition history exceeds 128 nodes or 16 MiB; delete unused definitions')
    return tree
