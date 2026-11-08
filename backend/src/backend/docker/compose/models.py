from common.identity import APP_NAME, STORAGE_ID, SESSION_COOKIE, SESSION_SALT, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, TEMP_PREFIX, MANAGED_HEADER
"""Complete Compose structures, template overlays and immutable deployment baselines."""

from copy import deepcopy
from dataclasses import dataclass
import difflib
import hashlib
import json
import re
import threading

from backend.docker.compose.definitions import MAX_DEFINITION_BYTES, validate_json_tree
from backend.docker.compose import tree as definition_tree


def encoded(value):
    validate_json_tree(value)
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(text.encode()) > MAX_DEFINITION_BYTES:
        raise ValueError('Resolved Compose definitions are limited to 1 MiB')
    return text


@dataclass(frozen=True)
class Definition:
    text: str

    @classmethod
    def capture(cls, document):
        return cls(encoded(document))

    @property
    def document(self):
        return json.loads(self.text)

    @property
    def digest(self):
        return hashlib.sha256(self.text.encode()).hexdigest()


def merge_patch(base, patch):
    """JSON Merge Patch: null deletes mapping members, arrays replace atomically."""
    if not isinstance(patch, dict):
        return deepcopy(patch)
    result = deepcopy(base) if isinstance(base, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def difference(base, desired):
    if not isinstance(base, dict) or not isinstance(desired, dict):
        return deepcopy(desired)
    patch = {key: None for key in base.keys() - desired.keys()}
    for key, value in desired.items():
        if key not in base:
            patch[key] = deepcopy(value)
        elif value != base[key]:
            patch[key] = difference(base[key], value)
    return patch


def json_patch(base, desired, path=''):
    if base == desired:
        return []
    if not isinstance(base, dict) or not isinstance(desired, dict):
        return [{'op': 'replace', 'path': path, 'value': deepcopy(desired)}]
    def pointer(key):
        return path + '/' + key.replace('~', '~0').replace('/', '~1')
    operations = [{'op': 'remove', 'path': pointer(key)} for key in sorted(base.keys() - desired.keys())]
    for key in sorted(desired):
        if key not in base:
            operations.append({'op': 'add', 'path': pointer(key), 'value': deepcopy(desired[key])})
        else:
            operations.extend(json_patch(base[key], desired[key], pointer(key)))
    return operations


def normalize_labels(value):
    if value is None:
        return {}
    if isinstance(value, list):
        pairs = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError('Label list entries must be strings: KEY=VALUE or KEY')
            key, _, label = item.partition('=')
            pairs.append((key, label))
    elif isinstance(value, dict):
        pairs = value.items()
    else:
        raise ValueError('Labels must be a mapping or a list of KEY=VALUE strings')
    result = {}
    for key, label in pairs:
        if not isinstance(key, str) or not key or any(ord(char) < 32 for char in key):
            raise ValueError('Label keys must be nonempty strings without control characters')
        if key.startswith('com.docker.compose.'):
            raise ValueError('The com.docker.compose. label prefix is reserved')
        if isinstance(label, bool) or label is not None and not isinstance(label, (str, int, float)):
            raise ValueError('Label values must be strings or numbers; quote boolean values')
        result[key] = '' if label is None else str(label)
    return dict(sorted(result.items()))


class ComposeModels:
    def __init__(self, settings, store=None):
        self.settings, self.store = settings, store
        self.lock = threading.RLock()
        self.inputs, self.records, self.baselines = {}, {}, {}
        self.import_overlays = {}
        self.trees = {}
        self.loaded = False

    def tree(self, name):
        if name not in self.trees and self.store:
            self.trees[name] = self.store.definition_tree(self.settings.docker_endpoint, name)
        return self.trees.get(name)

    def save_tree(self, name, tree):
        previous = self.tree(name)
        if previous == tree:
            return previous
        revision = previous['revision'] if previous else 0
        saved = self.store.save_definition_tree(self.settings.docker_endpoint, name, tree, revision) if self.store else {**tree, 'revision': revision + 1}
        self.trees[name] = saved
        return saved

    def _restore(self):
        if self.loaded:
            return
        if self.store:
            origins = {**self.store.running_origins(self.settings.docker_endpoint), **self.store.definition_tree_origins(self.settings.docker_endpoint)}
            for name, origin in origins.items():
                while origin.get('type') == 'template' and origin.get('source'):
                    origin = origin['source']
                self.inputs[name] = {'document': None, 'origin': origin, 'warnings': []}
        self.loaded = True

    def names(self):
        with self.lock:
            self._restore()
            return list(self.inputs)

    def update(self, inputs, stacks):
        with self.lock:
            self._restore()
            if self.store:
                self.trees.clear()
            self.inputs.update(deepcopy(inputs))
            resolved, failures = {}, {}

            def resolve(name, chain=()):
                if len(chain) > 32:
                    raise ValueError('Compose template chains are limited to 32 levels')
                if name in resolved:
                    return resolved[name].document
                if name in chain:
                    raise ValueError('Compose template cycle: ' + ' -> '.join((*chain, name)))
                source = self.inputs.get(name)
                if source is None or source.get('document') is None:
                    tree = self.tree(name)
                    active = next((node for node in tree['nodes'] if node['id'] == tree['active']), None) if tree else None
                    if source is not None and active and active['id'] != tree['source']:
                        resolved[name] = Definition.capture(definition_tree.document(tree, active['id']))
                        return resolved[name].document
                    raise ValueError('Template/source definition is unavailable: ' + name)
                document = deepcopy(source['document'])
                if source.get('stored_definition') is not None:
                    revision = source['stored_revision']
                    if self.import_overlays.get(name, (None,))[0] != revision:
                        stored = deepcopy(source['stored_definition'])
                        stored.pop(SOURCE_KEY, None)
                        self.import_overlays[name] = (revision, difference(document, stored))
                    document = merge_patch(document, self.import_overlays[name][1])
                meta = document.get('x-stackmeta') or {}
                template = meta.get('template') if isinstance(meta, dict) else None
                patch = meta.get('overlay', {}) if isinstance(meta, dict) else {}
                if isinstance(meta, dict) and 'labels' in meta:
                    meta['labels'] = normalize_labels(meta['labels'])
                if template:
                    if not isinstance(template, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,127}', template):
                        raise ValueError('x-stackmeta.template must name another Compose stack')
                    base = resolve(template, (*chain, name))
                    base.pop('name', None)
                    base.pop(SOURCE_KEY, None)
                    base_meta = base.get('x-stackmeta')
                    if isinstance(base_meta, dict):
                        for key in ('template', 'overlay'):
                            base_meta.pop(key, None)
                    document = merge_patch(base, document)
                if not isinstance(patch, dict):
                    raise ValueError('x-stackmeta.overlay must be an object')
                if isinstance(patch.get('x-stackmeta'), dict) and any(key in patch['x-stackmeta'] for key in ('template', 'overlay')):
                    raise ValueError('Set template/overlay on x-stackmeta directly, not inside its overlay')
                document = merge_patch(document, patch)
                if not isinstance(document.get('services', {}), dict):
                    raise ValueError('The resolved Compose services must be an object')
                document.setdefault('services', {})
                if isinstance(document.get('x-stackmeta'), dict):
                    document['x-stackmeta']['labels'] = normalize_labels(document['x-stackmeta'].get('labels'))
                Definition.capture(document)
                tree = self.tree(name)
                if name not in self.baselines:
                    saved = self.store.running_definition(self.settings.docker_endpoint, name) if self.store else None
                    if saved:
                        self.baselines[name] = (Definition.capture(saved), 'applied')
                    elif tree and tree.get('running'):
                        self.baselines[name] = (Definition.capture(definition_tree.document(tree, tree['running'])), tree.get('running_evidence', 'unknown'))
                baseline, evidence = self.baselines.get(name, (None, 'unknown'))
                if tree:
                    tree = self.save_tree(name, definition_tree.sync(tree, document, source['origin'], baseline.document if baseline else None, evidence))
                resolved[name] = Definition.capture(definition_tree.document(tree, tree['active']) if tree else document)
                return resolved[name].document

            for name in self.inputs:
                try:
                    resolve(name)
                except ValueError as exc:
                    failures[name] = str(exc)
            by_name = {stack['name']: stack for stack in stacks if stack.get('base_kind', stack['kind']) == 'compose'}
            for name, source in self.inputs.items():
                definition = resolved.get(name)
                stack = by_name.get(name, {})
                running = stack.get('running', 0) > 0
                if name not in self.baselines and self.store:
                    saved = self.store.running_definition(self.settings.docker_endpoint, name)
                    if saved:
                        self.baselines[name] = (Definition.capture(saved), 'applied')
                if definition and running and name not in self.baselines:
                    self.baselines[name] = (definition, 'observed-unverified')
                baseline, evidence = self.baselines.get(name, (None, 'unknown'))
                tree = self.tree(name)
                if tree and baseline:
                    self.save_tree(name, definition_tree.sync(tree, definition_tree.document(tree, tree['source']), source['origin'], baseline.document, evidence))
                origin = deepcopy(source['origin'])
                meta = source.get('document', {}).get('x-stackmeta') if source.get('document') else None
                if isinstance(meta, dict) and meta.get('template'):
                    origin = {'type': 'template', 'stack': meta['template'], 'source': origin}
                self.records[name] = {'origin': origin, 'desired': definition, 'baseline': baseline,
                    'baseline_evidence': evidence, 'running': running, 'error': failures.get(name),
                    'warnings': source.get('warnings', [])}
            return {name: definition.document for name, definition in resolved.items()}

    def refresh(self, topic=None, data=None):
        if topic and not topic.startswith(('docker.compose.', 'docker.stack.definition.')):
            return
        from backend.docker.compose.metadata import load_definition
        with self.lock:
            self._restore()
            inputs = deepcopy(self.inputs)
            for name, source in inputs.items():
                if source['origin']['type'] == 'file':
                    document, paths, warnings = load_definition({'name': name, 'config_files': source['origin'].get('files', []),
                        'working_directory': source['origin'].get('directory')}, self.settings)
                    source.update(document=document, warnings=warnings)
                    source['origin']['available'] = document is not None and not warnings
            if self.store:
                for saved in self.store.list():
                    if name := saved.get('name'):
                        if name not in inputs or inputs[name]['origin']['type'] == 'virtual':
                            inputs[name] = {'document': saved['definition'], 'origin': {'type': 'virtual'}, 'warnings': []}
                        elif saved['definition'].get(SOURCE_KEY):
                            inputs[name].update(stored_definition=saved['definition'], stored_revision=saved['revision'])
            running = [{'name': name, 'kind': 'compose', 'running': int(record['running'])} for name, record in self.records.items()]
            self.update(inputs, running)

    def mark_applied(self, name, document, node_id=None):
        frozen = Definition.capture(document)
        origin = self.records.get(name, {}).get('origin')
        if self.store:
            self.store.remember_running_definition(self.settings.docker_endpoint, name, document, origin)
        with self.lock:
            self.baselines[name] = (frozen, 'applied')
            if name in self.records:
                self.records[name].update(baseline=frozen, baseline_evidence='applied')
            tree = self.tree(name)
            if tree:
                source_origin = next(node['origin'] for node in tree['nodes'] if node['id'] == tree['source'])
                updated = definition_tree.sync(tree, definition_tree.document(tree, tree['source']), source_origin, document, 'applied')
                if any(node['id'] == node_id and node['hash'] == frozen.digest for node in updated['nodes']):
                    updated['running'] = node_id
                self.save_tree(name, updated)

    def node_info(self, name, node_id):
        with self.lock:
            tree = self.tree(name)
            selected_id = tree['active'] if tree and node_id == 'active' else node_id
            selected = next((node for node in tree['nodes'] if node['id'] == selected_id), None) if tree else None
            if not selected:
                raise ValueError('Definition not found; initialize stack management first')
            return {'definition': definition_tree.document(tree, selected_id), 'node': selected_id,
                    'label': selected['label'], 'revision': tree['revision'],
                    'writable': definition_tree.editable(tree, selected),
                    'file': (selected.get('origin', {}).get('files') or [None])[0] if selected_id == tree['source'] else None}

    def change_tree(self, name, params):
        from backend.docker.compose.definitions import validate_definition
        with self.lock:
            tree = self.tree(name)
            action = params.get('action')
            if action == 'initialize':
                record = self.records.get(name)
                desired = record.get('desired') if record else None
                if tree:
                    raise ValueError('Stack management is already initialized')
                if not desired or params.get('expected_hash') != desired.digest:
                    raise ValueError('Compose definition changed or is unavailable; reload and review again')
                origin = deepcopy(self.inputs[name]['origin'])
                tree = definition_tree.sync(None, desired.document, origin,
                    record['baseline'].document if record['baseline'] else None, record['baseline_evidence'])
                if not origin.get('files'):
                    tree['nodes'][0]['label'] = name
                preview = {'name': name, 'action': action, 'node_label': tree['nodes'][0]['label'], 'diff': '', 'applied': False}
                if params.get('apply') is True:
                    self.save_tree(name, tree)
                    preview['applied'] = True
                return preview
            if not tree or type(params.get('expected_revision')) is not int or params['expected_revision'] != tree['revision']:
                raise ValueError('Definition hierarchy changed; reload and review again')
            definition, context, assets, scoped = params.get('definition'), None, None, None
            if action == 'new':
                from backend.docker.compose import imports as definition_imports
                definition, context, assets, scoped = definition_imports.prepare(self.settings, name, params)
            if action == 'duplicate':
                definition = definition_tree.document(tree, params.get('node'))
                if 'name' in definition:
                    definition['name'] = name
            if action in {'add', 'edit', 'new'}:
                validate_definition(name, definition)
                encoded(definition)
            changed = definition_tree.change(tree, action, params.get('node'), params.get('label'), definition,
                                             params.get('identity'), context)
            selected = next((node for node in tree['nodes'] if node['id'] == params.get('node')), None)
            created = changed['nodes'][-1] if action in {'add', 'duplicate', 'new'} else None
            before = None if action == 'new' else definition_tree.document(tree, selected['id'] if selected else tree['active'])
            if action == 'activate':
                before = definition_tree.document(tree, tree['active'])
                after = definition_tree.document(changed, changed['active'])
            elif created:
                after = definition_tree.document(changed, created['id'])
            elif action == 'edit':
                after = definition_tree.document(changed, selected['id'])
            else:
                after = before
            preview = {'name': name, 'action': action, 'applied': False,
                       'node_label': selected['label'] if selected else params.get('label'),
                       'diff': '' if before is None else ''.join(difflib.unified_diff(json.dumps(before, indent=2, sort_keys=True).splitlines(True),
                                                         json.dumps(after, indent=2, sort_keys=True).splitlines(True), fromfile='current', tofile='proposed'))}
            if action == 'new':
                preview['definition'] = after
            if assets:
                preview['assets'] = assets
            if len(preview['diff'].encode()) > MAX_DEFINITION_BYTES:
                raise ValueError('Definition diff exceeds the preview limit')
            if params.get('apply') is True:
                published = False
                try:
                    if assets:
                        if params.get('expected_asset_hash') != assets['hash']:
                            raise ValueError('Imported files changed after preview; review again')
                        definition_imports.publish(scoped, params, assets['hash'])
                        published = True
                    self.save_tree(name, changed)
                except Exception:
                    if published:
                        definition_imports.discard(context, self.settings)
                    raise
                self.update({}, [{'name': key, 'kind': 'compose', 'running': int(value['running'])} for key, value in self.records.items()])
                preview['applied'] = True
            return preview

    def tree_summary(self, name):
        tree = self.tree(name)
        result = definition_tree.summary(tree)
        if result:
            for node in result['nodes']:
                original = next(item for item in tree['nodes'] if item['id'] == node['id'])
                node['can_edit'] = definition_tree.editable(tree, original)
                node['can_delete'] = definition_tree.deletable(tree, original)
                node['file'] = (original.get('origin', {}).get('files') or [None])[0] if node['id'] == tree['source'] else None
        return result

    def summary(self, name):
        with self.lock:
            record = self.records.get(name)
            if not record:
                return None
            desired, baseline = record['desired'], record['baseline']
            return {'origin': deepcopy(record['origin']), 'definition_hash': desired.digest if desired else None,
                    'baseline_available': baseline is not None,
                    'running_hash': baseline.digest if baseline else None, 'baseline_evidence': record['baseline_evidence'],
                    'pending_changes': bool(desired and baseline and desired.digest != baseline.digest), 'error': record['error'],
                    'warnings': list(record['warnings']), 'tree': self.tree_summary(name)}

    def details(self, name):
        with self.lock:
            if name not in self.records:
                raise ValueError('Compose stack model not found')
            record = self.records[name]
            desired, baseline = record['desired'], record['baseline']
            before = json.dumps(baseline.document, indent=2, ensure_ascii=False, sort_keys=True) if baseline else ''
            after = json.dumps(desired.document, indent=2, ensure_ascii=False, sort_keys=True) if desired else ''
            return {**self.summary(name), 'name': name, 'definition': desired.document if desired else None,
                    'running_definition': baseline.document if baseline else None,
                    'overlay': json_patch(baseline.document, desired.document) if baseline and desired else [],
                    'overlay_format': 'json-patch',
                    'diff': ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile='running/baseline', tofile='desired'))}
