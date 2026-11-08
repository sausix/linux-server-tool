from common.identity import APP_NAME, STORAGE_ID, SESSION_COOKIE, SESSION_SALT, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, TEMP_PREFIX, MANAGED_HEADER
"""Read Compose extension metadata without executing or deploying Compose files."""

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import stat

import yaml

from backend.docker.compose.definitions import MAX_DEFINITION_BYTES, validate_json_tree

STATES = {'started', 'healthy', 'completed'}
COMPOSE_NAMES = ('compose.yaml', 'compose.yml', 'compose.json', 'docker-compose.yaml', 'docker-compose.yml', 'docker-compose.json')


class ComposeLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        keys = set()
        for key_node, _ in node.value:
            if key_node.tag == 'tag:yaml.org,2002:merge':
                continue
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in keys:
                raise yaml.constructor.ConstructorError(None, None, 'Compose keys must be unique strings', key_node.start_mark)
            keys.add(key)
        return super().construct_mapping(node, deep=deep)


# Compose uses true/false booleans; YAML 1.1 must not turn restart: no into False.
ComposeLoader.yaml_implicit_resolvers = deepcopy(yaml.SafeLoader.yaml_implicit_resolvers)
for first, resolvers in ComposeLoader.yaml_implicit_resolvers.items():
    ComposeLoader.yaml_implicit_resolvers[first] = [(tag, regex) for tag, regex in resolvers if tag not in {'tag:yaml.org,2002:bool', 'tag:yaml.org,2002:timestamp'}]
ComposeLoader.add_implicit_resolver('tag:yaml.org,2002:bool', re.compile(r'^(?:true|false|True|False|TRUE|FALSE)$'), list('tTfF'))


def parse_document(text, suffix):
    if len(text.encode()) > MAX_DEFINITION_BYTES:
        raise ValueError('Compose files are limited to 1 MiB')
    document = json.loads(text) if suffix.lower() == '.json' else yaml.load(text, Loader=ComposeLoader)
    if not isinstance(document, dict):
        raise ValueError('The Compose document must be an object')
    validate_json_tree(document)
    return document


def merge_metadata_documents(documents):
    # Retain complete parsed documents. Deployment resolution still belongs to
    # Docker Compose; this merge does not interpolate values or execute includes.
    def merge(left, right, depth=0):
        if depth > 32:
            raise ValueError('Compose configuration nesting is too deep')
        result = dict(left)
        for key, value in right.items():
            result[key] = merge(result[key], value, depth + 1) if isinstance(result.get(key), dict) and isinstance(value, dict) else value
        return result
    result = {}
    for document in documents:
        result = merge(result, document)
    return result


def validate_metadata(document):
    present = 'x-stackmeta' in document
    result = {'present': present, 'autostart': False, 'description': '', 'requires': [],
              'labels': {}, 'warnings': [], 'invalid_fields': [], 'restart_conflicts': [], 'eligible': False}
    if not present:
        return result
    def invalid(field, reason):
        result['invalid_fields'].append({'field': field, 'reason': reason})
    meta = document['x-stackmeta']
    if not isinstance(meta, dict):
        invalid('x-stackmeta', 'Expected an object')
        return result
    for key in meta.keys() - {'autostart', 'description', 'dependencies', 'labels', 'template', 'overlay'}:
        invalid(str(key), 'Unsupported x-stackmeta property')
    from backend.docker.compose.models import normalize_labels
    try:
        result['labels'] = normalize_labels(meta.get('labels'))
    except ValueError as exc:
        invalid('labels', str(exc))
    if 'template' in meta and (not isinstance(meta['template'], str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,127}', meta['template'])):
        invalid('template', 'Expected a Compose stack name')
    if 'overlay' in meta and not isinstance(meta['overlay'], dict):
        invalid('overlay', 'Expected a JSON Merge Patch object')
    if 'autostart' in meta:
        if type(meta['autostart']) is not bool:
            invalid('autostart', 'Expected true or false')
        else:
            result['autostart'] = meta['autostart']
    if 'description' in meta:
        if not isinstance(meta['description'], str):
            invalid('description', 'Expected a string')
        else:
            result['description'] = meta['description']
    dependencies = meta.get('dependencies', {})
    if not isinstance(dependencies, dict) or dependencies.keys() - {'requires'}:
        invalid('dependencies', 'Expected an object containing only requires')
    else:
        requires = dependencies.get('requires', [])
        if isinstance(requires, str):
            requires = [requires]
        if not isinstance(requires, list):
            invalid('dependencies.requires', 'Expected a stack name or a list of stack names/objects')
        else:
            seen = set()
            for item in requires:
                item = {'stack': item} if isinstance(item, str) else item
                if (not isinstance(item, dict) or item.keys() - {'stack', 'state'} or
                        not isinstance(item.get('stack'), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', item['stack']) or
                        not isinstance(item.get('state', 'healthy'), str) or item.get('state', 'healthy') not in STATES):
                    invalid('dependencies.requires', 'Each entry needs a stack name and state started, healthy or completed')
                    continue
                if item['stack'] in seen:
                    invalid('dependencies.requires', f'Duplicate dependency: {item["stack"]}')
                seen.add(item['stack'])
                result['requires'].append({'stack': item['stack'], 'state': item.get('state', 'healthy')})
    services = document.get('services') or {}
    if document.get('include'):
        invalid('include', 'Includes must be resolved before restart policies can be assessed')
    if not isinstance(services, dict):
        invalid('services', 'Cannot assess restart policies without a services object')
        services = {}
    for name, service in services.items():
        if not isinstance(service, dict):
            invalid('services', f'{name}: expected an object')
            continue
        if service.get('extends'):
            invalid('services', f'{name}: include/extends must be resolved before restart policies can be assessed')
        policy = service.get('restart', 'no')
        deploy = service.get('deploy') or {}
        deploy_policy = deploy.get('restart_policy') if isinstance(deploy, dict) else None
        if policy != 'no':
            result['restart_conflicts'].append({'service': name, 'policy': policy, 'source': 'restart'})
        if deploy_policy is not None and (not isinstance(deploy_policy, dict) or deploy_policy.get('condition', 'any') != 'none'):
            result['restart_conflicts'].append({'service': name, 'policy': deploy_policy, 'source': 'deploy.restart_policy'})
    if result['restart_conflicts']:
        reason = 'A service has a restart policy other than no. Docker-controlled restarts bypass cross-stack ordering; this stack is excluded from extended startup.'
        invalid('autostart', reason)
        invalid('dependencies', reason)
        if 'restart' in meta:
            invalid('restart', reason)
    result['eligible'] = not result['invalid_fields']
    return result


def local_compose_path(path, settings):
    original = Path(path)
    if not original.is_absolute() or original.suffix.lower() not in {'.yaml', '.yml', '.json'}:
        raise ValueError('Compose paths must be absolute YAML or JSON paths')
    managed = settings.compose_root.resolve()
    if original.is_relative_to(settings.compose_root):
        resolved = original.resolve()
        if not resolved.is_relative_to(managed):
            raise ValueError('Compose path escapes the managed root')
        return resolved
    root = settings.compose_source_root.resolve()
    mapped = root / str(original).lstrip('/')
    resolved = mapped.resolve()
    if not any(resolved.is_relative_to((root / str(allowed).lstrip('/')).resolve()) for allowed in settings.compose_allowed_roots):
        raise ValueError('Compose path is outside COMPOSE_ALLOWED_ROOTS')
    if not resolved.is_relative_to(root):
        raise ValueError('Compose path escapes the configured host root')
    return resolved


def compose_path_diagnostic(original, settings):
    """Probe ancestors after a read failure, reporting host paths without content."""
    original = Path(original)
    if not original.is_absolute() or '..' in original.parts:
        return None
    managed = original.is_relative_to(settings.compose_root)
    if not managed and not any(original.is_relative_to(allowed) for allowed in settings.compose_allowed_roots):
        return None
    prefix = Path('/') if managed else settings.compose_source_root
    failure = None
    # Work backwards so an inaccessible/missing ancestor supersedes its children.
    for candidate in (original, *original.parents):
        local = prefix / str(candidate).lstrip('/')
        try:
            info = local.stat()
            if candidate != original and not stat.S_ISDIR(info.st_mode):
                failure = f'Not a directory: {candidate}'
            elif not os.access(local, os.X_OK if stat.S_ISDIR(info.st_mode) else os.R_OK, effective_ids=True):
                failure = f'No access to {candidate}'
        except PermissionError:
            failure = f'No access to {candidate}'
        except FileNotFoundError:
            failure = f'Path does not exist: {candidate}'
        except NotADirectoryError:
            failure = f'Not a directory in path: {candidate}'
        except OSError:
            # Do not include exception text: it may expose container-only paths.
            failure = f'Unable to access {candidate}'
    return failure


def load_definition(stack, settings):
    paths = list(stack.get('config_files') or [])
    if not paths:
        directories = ([Path(stack['working_directory'])] if stack.get('working_directory') else [])
        directories += [directory / stack['name'] for directory in settings.compose_search_directories]
        for directory in directories:
            for name in COMPOSE_NAMES:
                candidate = directory / name
                try:
                    if local_compose_path(candidate, settings).is_file():
                        paths = [str(candidate)]
                        break
                except (OSError, ValueError):
                    continue
            if paths:
                break
    if not paths:
        return None, [], ['No readable Compose source was identified from project labels or configured search directories.']
    documents, sources, warnings = [], [], []
    for value in paths:
        try:
            original = Path(value)
            if not original.is_absolute() and stack.get('working_directory'):
                original = Path(stack['working_directory']) / original
            path = local_compose_path(original, settings)
            # Never block on FIFOs/devices and bound input before parsing.
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(descriptor, 'rb') as file:
                if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                    raise ValueError('Compose source must be a regular file')
                text = file.read(MAX_DEFINITION_BYTES + 1)
            if len(text) > MAX_DEFINITION_BYTES:
                raise ValueError('Compose files are limited to 1 MiB')
            documents.append(parse_document(text.decode('utf-8'), path.suffix))
            sources.append(str(original))
        except (OSError, ValueError, UnicodeError, yaml.YAMLError, RecursionError) as exc:
            # Parser diagnostics can contain source lines with credentials.
            diagnostic = compose_path_diagnostic(original, settings) if isinstance(exc, OSError) else None
            warnings.append(f'{value}: {diagnostic}.' if diagnostic else
                            f'{value}: unable to read or parse Compose source ({type(exc).__name__}); check path permissions, allowed roots and YAML/JSON syntax.')
    try:
        document = merge_metadata_documents(documents) if documents else None
    except (ValueError, RecursionError):
        return None, sources, warnings + ['Compose metadata could not be merged safely.']
    return document, sources, warnings


def enrich_stacks(stacks, client, settings, virtual_definitions, model_registry=None):
    from backend.docker.client import DockerError
    from backend.docker.compose.models import ComposeModels
    models = model_registry or ComposeModels(settings)
    virtuals = {item['name']: item for item in virtual_definitions}
    definitions, by_name, runtime, inputs = {}, {}, {}, {}
    for stack in stacks:
        stack['base_kind'] = stack['kind']
        by_name.setdefault(stack['name'], []).append(stack)
        if stack['kind'] != 'compose':
            continue
        virtual = virtuals.get(stack['name'])
        if virtual:
            document, sources, warnings = virtual['definition'], [], []
            source = document.get(SOURCE_KEY) or {}
            structured = isinstance(source, dict) and source.get('type') == 'compose-files'
            sources = source.get('files', []) if structured else []
            if not isinstance(sources, list) or any(not isinstance(path, str) for path in sources):
                sources = []
                warnings.append('Structured source provenance is invalid; review the JSON definition.')
            if structured and (not stack['config_files'] or stack['config_files'] == ['-']):
                stack['config_files'] = list(sources)
                stack['working_directory'] = source.get('project_directory') or stack['working_directory']
            if stack['config_files'] and stack['config_files'] != ['-'] and not (structured and all(path in sources for path in stack['config_files'])):
                warnings.append('A virtual definition has the same name as a discovered file-backed project; metadata is not trusted until the conflict is resolved.')
            stack['definition_origin'] = 'structured' if structured else 'virtual'
            if not structured and (settings.compose_root / stack['name']).is_dir():
                stack['working_directory'] = str(settings.compose_root / stack['name'])
            stack['definition_revision'] = virtual['revision']
        elif not settings.docker_endpoint.startswith('unix://'):
            document, sources, warnings = None, [], ['Compose file discovery is only enabled for local Unix Docker endpoints.']
            stack['definition_origin'] = 'unavailable'
        else:
            from backend.docker.compose.files import discover_paths
            stack['config_files'] = discover_paths(stack, settings)
            document, sources, warnings = load_definition(stack, settings)
            stack['definition_origin'] = 'file' if document is not None else 'unavailable'
        origin = {'type': 'virtual'} if virtual and not structured else {'type': 'file',
            'files': list(stack.get('config_files') or sources), 'directory': stack.get('working_directory'),
            'available': document is not None and not warnings}
        entry = {'document': document if not warnings else None, 'origin': origin, 'warnings': warnings}
        if virtual and structured:
            imported = document
            file_document, _, file_warnings = load_definition({'name': stack['name'], 'config_files': sources,
                'working_directory': source.get('project_directory')}, settings)
            entry.update(document=file_document if not file_warnings else None, stored_definition=imported,
                         stored_revision=virtual['revision'], warnings=file_warnings)
            entry['origin']['available'] = file_document is not None and not file_warnings
        inputs[stack['name']] = entry
        definitions[id(stack)] = document or {}
    resolved = models.update(inputs, stacks)
    for stack in stacks:
        if stack['base_kind'] != 'compose':
            continue
        model = models.summary(stack['name'])
        stack['compose_model'] = model
        stack['origin'] = model['origin']['type']
        stack['template'] = model['origin'].get('stack', '')
        stack['pending'] = model['pending_changes']
        document = resolved.get(stack['name'])
        entry = inputs[stack['name']]
        sources, warnings = entry['origin'].get('files', []), list(entry['warnings'])
        if model.get('error'):
            warnings.append(model['error'])
        meta = validate_metadata(document or definitions[id(stack)])
        meta['sources'], meta['warnings'] = sources, warnings
        if warnings:
            meta['eligible'] = False
        stack['metadata'] = meta
        stack['labels'] = meta['labels']
        if meta['present']:
            stack['kind'] = 'compose-meta'
        definitions[id(stack)] = document or {}
        # Fileless definitions still have services even when nothing was deployed.
        existing = {item['name'] for item in stack['service_items']}
        for name, service in (document or {}).get('services', {}).items() if isinstance((document or {}).get('services'), dict) else []:
            if name not in existing and isinstance(service, dict):
                image = service.get('image', '')
                stack['service_items'].append({'name': name, 'mode': 'compose', 'image': image if isinstance(image, str) else '',
                    'running': 0, 'desired': None, 'state': 'empty'})
        stack['service_items'].sort(key=lambda item: item['name'])
        stack['services'] = len(stack['service_items'])

    def inspect(item):
        identity = item['id']
        if not re.fullmatch(r'[a-fA-F0-9]{12,64}', identity):
            return None
        if identity not in runtime:
            try:
                raw = client.request('/containers/' + identity + '/json')
                runtime[identity] = {'state': raw.get('State') or {},
                    'healthcheck': (raw.get('Config') or {}).get('Healthcheck') or {},
                    'restart': (raw.get('HostConfig') or {}).get('RestartPolicy') or {}}
            except (DockerError, OSError, ValueError):
                runtime[identity] = None
        return runtime[identity]

    def assess(target, desired):
        if target.get('incomplete'):
            return False, 'unknown', 'Runtime inventory is incomplete'
        if target['base_kind'] == 'swarm':
            return False, 'unknown', 'Cross-stack state evaluation currently supports Compose projects only'
        services = definitions.get(id(target), {}).get('services') or {}
        names = set(services) | {item['service'] for item in target['container_items']}
        if not names:
            return False, 'pending', 'No service containers are present'
        for name in sorted(names):
            specification = services.get(name) or {}
            if not isinstance(specification, dict):
                return False, 'unknown', f'{name}: service configuration is invalid'
            children = [item for item in target['container_items'] if item['service'] == name]
            if specification.get('profiles') and not children:
                continue
            deploy = specification.get('deploy') or {}
            replicas = specification.get('scale', deploy.get('replicas', 1) if isinstance(deploy, dict) else 1)
            if type(replicas) is not int or replicas < 1:
                return False, 'unknown', f'{name}: replica count is unresolved or zero'
            children.sort(key=lambda item: item.get('created', 0), reverse=True)
            if len(children) < replicas:
                return False, 'pending', f'{name}: not enough containers are present'
            for child in children[:replicas]:
                details = inspect(child)
                if details is None:
                    return False, 'unknown', f'{name}: container state could not be read'
                state = details['state']
                if desired == 'completed':
                    if state.get('Status') != 'exited':
                        return False, 'pending', f'{name}: job has not completed'
                    if type(state.get('ExitCode')) is not int:
                        return False, 'unknown', f'{name}: exit code is unavailable'
                    if state.get('ExitCode') != 0:
                        return False, 'error', f'{name}: job did not exit successfully'
                else:
                    if not state.get('Running') or state.get('Paused') or state.get('Restarting'):
                        return False, 'pending', f'{name}: container is not started'
                    healthcheck = details['healthcheck']
                    if desired == 'healthy' and healthcheck.get('Test') and healthcheck.get('Test') != ['NONE']:
                        health = (state.get('Health') or {}).get('Status')
                        if health != 'healthy':
                            return False, 'error' if health == 'unhealthy' else 'pending', f'{name}: healthcheck is {health or "unknown"}'
        return True, 'satisfied', 'Required state is satisfied; healthy falls back to started for services without healthchecks'

    graph = {stack['name']: [item['stack'] for item in stack.get('metadata', {}).get('requires', [])] for stack in stacks}
    def reaches(start, goal):
        pending, seen = [start], set()
        while pending:
            name = pending.pop()
            if name == goal:
                return True
            if name not in seen:
                seen.add(name)
                pending.extend(graph.get(name, []))
        return False

    for stack in stacks:
        meta = stack.get('metadata')
        if not meta or not meta['present']:
            continue
        for requirement in meta['requires']:
            candidates = by_name.get(requirement['stack'], [])
            requirement.update({'satisfied': False, 'assessment': 'error', 'target_state': 'failed', 'target_kind': None})
            if not candidates:
                requirement['reason'] = 'Required stack is missing'
            elif len(candidates) != 1:
                requirement['reason'] = 'Stack name is ambiguous across backends'
            else:
                target = candidates[0]
                requirement.update({'target_kind': target['kind'], 'target_state': target['state']})
                if reaches(target['name'], stack['name']):
                    requirement['reason'] = 'Dependency cycle; extended startup is excluded'
                    meta['eligible'] = False
                    meta['invalid_fields'].append({'field': 'dependencies', 'reason': requirement['reason']})
                elif target.get('metadata', {}).get('present') and not target['metadata']['eligible']:
                    requirement['reason'] = 'Required stack metadata is invalid or unavailable'
                else:
                    requirement['satisfied'], requirement['assessment'], requirement['reason'] = assess(target, requirement['state'])
                    if requirement['assessment'] == 'pending' and target['state'] in {'failed', 'unhealthy', 'degraded', 'partial'}:
                        requirement['assessment'] = 'error'
