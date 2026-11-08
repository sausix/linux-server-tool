from common.identity import APP_NAME, STORAGE_ID, SESSION_COOKIE, SESSION_SALT, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, TEMP_PREFIX, MANAGED_HEADER
"""Previewed stack operations and bounded background jobs."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import ssl
import subprocess
import tempfile
import threading
from urllib.parse import quote, urlsplit
import uuid

from backend.docker.compose.files import discover_paths, read_source
from backend.docker.stacks import snapshot
from backend.docker.compose.definitions import MAX_DEFINITION_BYTES, validate_definition


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


class StackControl:
    def __init__(self, client, settings, store, publish, models=None):
        self.client, self.settings, self.store, self.publish = client, settings, store, publish
        from backend.docker.compose.models import ComposeModels
        self.models = models or ComposeModels(settings, store)
        self.lock = threading.Lock()
        self.jobs, self.active, self.processes = {}, set(), set()
        self.closing = False

    def find(self, name, kind='compose'):
        if not isinstance(name, str) or kind not in {'compose', 'compose-meta', 'swarm'}:
            raise ValueError('Invalid stack name or kind')
        data = snapshot(self.client, settings=self.settings, virtual_definitions=self.store.list(), model_registry=self.models)
        base = 'swarm' if kind == 'swarm' else 'compose'
        matches = [stack for stack in data['stacks'] if stack['name'] == name and stack['base_kind'] == base]
        if len(matches) != 1:
            raise ValueError('Stack not found or ambiguous')
        return matches[0]

    def _command(self):
        command = self.settings.compose_command
        executable = shutil.which(command[0]) if command else None
        if not executable:
            raise ValueError('Compose control tool not found; install Docker CLI and its Compose plugin or configure COMPOSE_COMMAND')
        parsed = urlsplit(self.settings.docker_endpoint)
        endpoint = self.settings.docker_endpoint
        tls = parsed.scheme == 'https' or (parsed.scheme == 'tcp' and parsed.port == 2376)
        if parsed.scheme in {'https', 'http'}:
            endpoint = f'tcp://{parsed.hostname}:{parsed.port or (2376 if tls else 2375)}'
        options = []
        if Path(executable).name == 'docker':
            options = ['--host', endpoint]
            if tls:
                options += ['--tlsverify' if self.settings.docker_tls_verify else '--tls']
                ca = self.settings.docker_tls_ca or ssl.get_default_verify_paths().cafile
                if ca:
                    options += ['--tlscacert', str(ca)]
                for flag, value in [('--tlscert', self.settings.docker_tls_cert), ('--tlskey', self.settings.docker_tls_key)]:
                    if value:
                        options += [flag, str(value)]
        elif tls:
            raise ValueError('TLS Compose control requires the docker compose command prefix')
        return [executable, *options, *command[1:]], endpoint

    def _run(self, project, args):
        prefix, endpoint = self._command()
        state_dir = self.settings.stack_database.parent
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
        with tempfile.TemporaryDirectory(prefix='compose-', dir=state_dir) as temporary:
            env = {'PATH': os.environ.get('PATH', os.defpath), 'LANG': 'C.UTF-8', 'HOME': str(state_dir),
                   'DOCKER_CONFIG': temporary, 'DOCKER_HOST': endpoint}
            if self.settings.docker_api_version != 'auto':
                env['DOCKER_API_VERSION'] = self.settings.docker_api_version.removeprefix('v')
            directory = project.get('directory') or temporary
            command = prefix + ['--ansi', 'never', '--project-name', project['name'], '--project-directory', directory]
            stdin = None
            if project.get('files'):
                for path in project['files']:
                    command += ['--file', path]
            else:
                command += ['--file', '-']; stdin = json.dumps(project['definition']).encode()
            command += args
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                process = subprocess.Popen(command, cwd=directory, env=env, stdin=subprocess.PIPE,
                                           stdout=output, stderr=errors, start_new_session=True)
                with self.lock:
                    self.processes.add(process)
                    closing = self.closing
                try:
                    if closing:
                        self._terminate(process)
                        raise ValueError('Daemon is shutting down')
                    try:
                        process.communicate(stdin, timeout=self.settings.stack_control_timeout_seconds)
                    except subprocess.TimeoutExpired:
                        self._terminate(process)
                        raise ValueError('Compose operation timed out; inspect the stack state before retrying') from None
                finally:
                    with self.lock:
                        self.processes.discard(process)
                if process.returncode:
                    errors.seek(max(0, errors.tell() - 8000))
                    raise ValueError(errors.read().decode(errors='replace').strip() or 'Compose command failed')
                output.seek(0); raw = output.read(MAX_DEFINITION_BYTES + 1)
                if len(raw) > MAX_DEFINITION_BYTES:
                    raise ValueError('Compose output exceeds the size limit')
                return raw.decode('utf-8')

    @staticmethod
    def _terminate(process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=2)

    def close(self):
        with self.lock:
            self.closing = True; processes = list(self.processes)
        for process in processes:
            try:
                self._terminate(process)
            except ProcessLookupError:
                pass

    def project(self, stack):
        record = self.models.records.get(stack['name'])
        if record and record['error']:
            raise ValueError(record['error'])
        resolved = record['desired'].document if record and record['desired'] else None
        meta = (resolved or {}).get('x-stackmeta') or {}
        meta = meta if isinstance(meta, dict) else {}
        tree = self.models.tree(stack['name'])
        layered = tree and tree['active'] != tree['source']
        mapped_source = self.settings.compose_source_root != Path('/') and stack.get('config_files') and stack['config_files'] != ['-']
        if resolved and (layered or mapped_source or meta.get('template') or meta.get('overlay') or stack['name'] in self.models.import_overlays):
            from backend.docker.compose.metadata import local_compose_path
            selected = next((node for node in tree['nodes'] if node['id'] == tree['active']), None) if tree else None
            context = selected.get('context', {}) if selected else {}
            paths = discover_paths(stack, self.settings) if not layered and stack.get('config_files') and stack['config_files'] != ['-'] else []
            directory = context.get('directory') or stack.get('working_directory') or str(self.settings.compose_root / stack['name'])
            mapped_directory = local_compose_path(str(Path(directory) / 'compose.json'), self.settings).parent
            prefixed = mapped_directory != Path(directory).resolve()
            if paths and not prefixed:
                self._identity_paths(paths)
            if not mapped_directory.is_dir() and not paths:
                directory = None
                self._check_virtual_paths(resolved)
            elif prefixed:
                directory = str(mapped_directory)
            if directory and not prefixed:
                self._identity_paths([str(Path(directory) / 'compose.json')])
            sources = [read_source(stack, path, self.settings) for path in paths]
            project = {'name': stack['name'], 'definition': resolved, 'directory': directory,
                    'source_hashes': {source['path']: source['hash'] for source in sources}, 'source_files': paths,
                    **({'host_prefix': str(self.settings.compose_source_root)} if prefixed else {})}
            if paths and not layered and not meta.get('template') and not meta.get('overlay') and stack['name'] not in self.models.import_overlays:
                project['files'] = [str(local_compose_path(path, self.settings)) for path in paths]
            return project
        stored = self.store.get(stack['name'])
        if stored:
            document = stored['definition']
            source = document.get(SOURCE_KEY) or {}
            directory = source.get('project_directory') if isinstance(source, dict) else None
            if not directory and (self.settings.compose_root / stack['name']).is_dir():
                self._check_virtual_paths(document)
                directory = str(self.settings.compose_root / stack['name'])
            if directory:
                self._identity_paths([str(Path(directory) / 'compose.json')])
            else:
                self._check_virtual_paths(document)
            return {'name': stack['name'], 'definition': document, 'directory': directory,
                    'revision': stored['revision']}
        paths = discover_paths(stack, self.settings)
        if not paths:
            return None
        self._identity_paths(paths)
        sources = [read_source(stack, path, self.settings) for path in paths]
        directory = stack.get('working_directory') or str(Path(paths[0]).parent)
        self._identity_paths([str(Path(directory) / 'compose.json')])
        return {'name': stack['name'], 'files': paths,
                'directory': directory,
                'source_hashes': {source['path']: source['hash'] for source in sources}}

    def _identity_paths(self, paths):
        from backend.docker.compose.metadata import local_compose_path
        for path in paths:
            mapped = local_compose_path(path, self.settings)
            if mapped != Path(path).resolve():
                raise ValueError('This operation requires project directories mounted at their original host paths. Use a definition overlay for prefix-mapped stack controls.')

    @staticmethod
    def host_bind_paths(document, project):
        """Compose resolves relative binds in the daemon; Engine needs host paths."""
        prefix = project.get('host_prefix')
        if not prefix:
            return document
        prefix = Path(prefix).resolve()
        for service in document.get('services', {}).values():
            for mount in service.get('volumes', []):
                if isinstance(mount, dict) and mount.get('type') == 'bind' and mount.get('source'):
                    path = Path(mount['source'])
                    if path.is_absolute() and path.is_relative_to(prefix):
                        mount['source'] = str(Path('/') / path.relative_to(prefix))
        for group in ('secrets', 'configs'):
            if any(isinstance(item, dict) and item.get('file') for item in document.get(group, {}).values()):
                raise ValueError('File-backed secrets/configs require project directories mounted at their original host paths')
        return document

    @staticmethod
    def _check_virtual_paths(document):
        if document.get('include'):
            raise ValueError('Resolve includes before starting a virtual definition')
        for service in document.get('services', {}).values():
            if service.get('build') or service.get('extends'):
                raise ValueError('Virtual stacks need prebuilt images and resolved extends')
            for key in ('env_file', 'label_file'):
                values = service.get(key) or []
                for value in values if isinstance(values, list) else [values]:
                    path = value.get('path') if isinstance(value, dict) else value
                    if not isinstance(path, str) or not Path(path).is_absolute():
                        raise ValueError(f'Virtual {key} paths must be absolute')
            for volume in service.get('volumes') or []:
                if isinstance(volume, dict):
                    source = volume.get('source', '') if volume.get('type') == 'bind' else ''
                else:
                    source = volume.split(':')[0] if ':' in volume else ''
                if source and (source.startswith('.') or '/' in source) and not Path(source).is_absolute():
                    raise ValueError('Virtual bind sources must be absolute host paths')
        for key in ('configs', 'secrets'):
            for value in (document.get(key) or {}).values():
                if isinstance(value, dict) and value.get('file') and not Path(value['file']).is_absolute():
                    raise ValueError(f'Virtual {key} file paths must be absolute')

    def convert(self, name, apply=False, expected=None):
        stack = self.find(name)
        paths = discover_paths(stack, self.settings)
        if not paths:
            raise ValueError('No file-based Compose sources were found')
        self._identity_paths(paths)
        sources = [read_source(stack, path, self.settings) for path in paths]
        project = {'name': name, 'files': paths, 'directory': stack.get('working_directory') or str(Path(paths[0]).parent)}
        self._identity_paths([str(Path(project['directory']) / 'compose.json')])
        document = json.loads(self._run(project, ['config', '--format', 'json', '--no-interpolate']))
        document['name'] = name
        document[SOURCE_KEY] = {'type': 'compose-files', 'files': paths, 'project_directory': project['directory']}
        validate_definition(name, document)
        stored = self.store.get(name)
        state = {'sources': {item['path']: item['hash'] for item in sources},
                 'revision': stored['revision'] if stored else None, 'document': fingerprint(document)}
        if apply:
            if state != expected:
                raise ValueError('Sources or structured definition changed after preview; review conversion again')
            self.store.save(name, document, state['revision'])
            self.publish('docker.stack.definition.changed', {'name': name})
        return {'definition': document, 'expected': state, 'applied': apply}

    def prepare(self, name, kind, action):
        if action not in {'start', 'stop', 'restart', 'apply'}:
            raise ValueError('Unknown stack action')
        stack = self.find(name, kind)
        warnings = []
        meta = stack.get('metadata') or {}
        if action != 'stop' and meta.get('present'):
            if meta.get('eligible'):
                blocked = [item['stack'] for item in meta['requires'] if not item.get('satisfied')]
                if blocked:
                    raise ValueError('Required stack conditions are not met: ' + ', '.join(blocked))
            else:
                warnings.append('Metadata is excluded from extended startup. This is a manual stack operation; no dependency ordering is performed.')
        plan = {'name': name, 'kind': stack['base_kind'], 'action': action, 'warnings': warnings}
        if stack['base_kind'] == 'swarm':
            if action == 'apply':
                raise ValueError('Apply is available for Compose stacks only')
            services = self.client.request('/services')
            services = [item for item in services if (item.get('Spec', {}).get('Labels') or {}).get('com.docker.stack.namespace') == name]
            if not services:
                raise ValueError('No Swarm services are available')
            remembered = self.store.replica_counts(fingerprint(self.settings.docker_endpoint))
            normalized = []
            for service in services:
                spec = service['Spec']
                if 'Replicated' not in spec.get('Mode', {}):
                    raise ValueError('Swarm stack control supports replicated services only; global/job modes cannot be safely suspended')
                replicas = spec['Mode']['Replicated'].get('Replicas', 1)
                target = 0 if action == 'stop' else (replicas or remembered.get(service['ID'], 0)) if action == 'start' else replicas
                if action == 'start' and target == 0:
                    raise ValueError('No previous replica count is known for ' + spec.get('Name', service['ID']))
                if action == 'restart' and replicas == 0:
                    raise ValueError('A service is scaled to zero; use Start to restore its saved replica count')
                normalized.append({'id': service['ID'], 'spec': spec, 'version': service['Version']['Index'], 'target': target})
            plan.update({'backend': 'swarm', 'services': normalized})
        else:
            try:
                project = self.project(stack)
                if project:
                    document = self.host_bind_paths(json.loads(self._run(project, ['config', '--format', 'json'])), project)
            except (ValueError, OSError) as exc:
                model = self.models.summary(name)
                if action in {'start', 'apply'} or not stack['container_items'] or action == 'restart' and model and model['pending_changes']:
                    raise
                project = None
                warnings.append('Compose configuration cannot be loaded; only existing containers will be controlled: ' + str(exc))
            if project:
                plan.update({'backend': 'compose', 'project': {**project, 'definition': document}})
                model = self.models.records.get(name)
                if model and model['desired']:
                    plan['model_definition'] = model['desired'].document
                    tree = self.models.tree(name)
                    plan['model_node'] = tree['active'] if tree else None
                    plan['running_node'] = tree['running'] if tree else None
                    details = self.models.details(name)
                    plan['definition_diff'] = details['diff']
                    plan['baseline_evidence'] = details['baseline_evidence']
                    plan['pending_changes'] = details['pending_changes']
                    if action == 'stop' and model['baseline']:
                        baseline_project = {'name': name, 'definition': model['baseline'].document, 'directory': project.get('directory'),
                                            **({'host_prefix': project['host_prefix']} if project.get('host_prefix') else {})}
                        stopped = self.host_bind_paths(json.loads(self._run(baseline_project, ['config', '--format', 'json'])), baseline_project)
                        plan['project'] = {**baseline_project, 'definition': stopped}
                        plan.pop('model_definition', None)
            elif action == 'apply':
                raise ValueError('Apply requires a resolved Compose definition')
            elif stack['container_items']:
                if stack.get('incomplete'):
                    raise ValueError('Runtime inventory is incomplete; existing-container control is not safe')
                plan.update({'backend': 'containers', 'containers': sorted(item['id'] for item in stack['container_items'])})
                warnings.append('No Compose source is available. Only existing containers will be controlled; no resources are created.')
            else:
                raise ValueError('No usable Compose source, virtual definition or existing containers are available')
        return plan

    def preview(self, name, kind, action):
        plan = self.prepare(name, kind, action)
        if len((plan.get('definition_diff') or '').encode()) > MAX_DEFINITION_BYTES:
            raise ValueError('The definition diff exceeds the preview limit; inspect and reduce the saved changes before applying')
        source = ('Database JSON definition' if plan.get('project', {}).get('revision') else
                  ', '.join(plan.get('project', {}).get('files') or plan.get('project', {}).get('source_files') or []) or 'Resolved Compose stack model') if plan['backend'] == 'compose' else 'Existing Engine resources'
        description = ('Start restores saved replica counts, Stop scales replicas to zero, and Restart requests a rolling service update.' if plan['backend'] == 'swarm' else
                       'Only existing containers are controlled; saved configuration changes are not applied.' if plan['backend'] == 'containers' else
                       'Start/Apply use saved Compose configuration without building or pulling. Restart recreates containers when the selected Active definition differs from Running; otherwise it restarts existing containers. Stop retains containers, networks and volumes.')
        return {'name': name, 'kind': plan['kind'], 'action': action, 'backend': plan['backend'],
                'warnings': plan['warnings'], 'fingerprint': fingerprint(plan),
                'definition_diff': plan.get('definition_diff'), 'baseline_evidence': plan.get('baseline_evidence'),
                'source': source, 'description': description}

    def apply(self, name, kind, action, expected):
        plan = self.prepare(name, kind, action)
        if fingerprint(plan) != expected:
            raise ValueError('Stack configuration or resources changed after preview; review again')
        key = (plan['kind'], name)
        with self.lock:
            if self.closing or key in self.active or len(self.active) >= self.settings.stack_max_concurrent_jobs:
                raise ValueError('A stack operation is already running or the daemon is shutting down')
            for identity in list(self.jobs):
                if len(self.jobs) < 50:
                    break
                if self.jobs[identity]['status'] not in {'queued', 'running'}:
                    del self.jobs[identity]
            identity = uuid.uuid4().hex
            job = {'id': identity, 'name': name, 'kind': plan['kind'], 'action': action, 'status': 'queued', 'error': None}
            self.jobs[identity] = job; self.active.add(key)
        threading.Thread(target=self._work, args=(job, plan, key), daemon=True, name='backend-stack-control').start()
        return dict(job)

    def _work(self, job, plan, key):
        try:
            if self.closing:
                raise ValueError('Daemon is shutting down')
            job['status'] = 'running'
            self.publish('docker.stack.action.changed', {'name': job['name'], 'job': job['id']})
            if plan['backend'] == 'compose':
                for path, expected in plan['project'].get('source_hashes', {}).items():
                    source = read_source({'name': plan['name'], 'kind': 'compose', 'config_files': plan['project'].get('source_files') or plan['project'].get('files')}, path, self.settings)
                    if source['hash'] != expected:
                        raise ValueError('Compose source changed before execution; review the operation again')
                deploy = plan['action'] in {'start', 'apply'} or plan['action'] == 'restart' and (
                    plan.get('pending_changes') or plan.get('model_node') != plan.get('running_node'))
                args = ['up', '--detach', '--no-build', '--pull', 'never'] if deploy else [plan['action'], '--timeout', str(self.settings.stack_stop_timeout_seconds)]
                if plan['action'] == 'restart' and deploy:
                    args += ['--force-recreate']
                # Execute the exact normalized configuration reviewed by the user.
                execution = dict(plan['project'])
                execution.pop('files', None)
                self._run(execution, args)
                if deploy and plan.get('model_definition') is not None:
                    self.models.mark_applied(plan['name'], plan['model_definition'], plan.get('model_node'))
            elif plan['backend'] == 'containers':
                for identity in plan['containers']:
                    if self.closing:
                        raise ValueError('Daemon is shutting down')
                    self.client.request('/containers/' + quote(identity, safe='') + '/' + plan['action'], method='POST',
                                        timeout=self.settings.stack_control_timeout_seconds)
            else:
                endpoint = fingerprint(self.settings.docker_endpoint)
                if plan['action'] == 'stop':
                    self.store.remember_replicas(endpoint, {item['id']: item['spec']['Mode']['Replicated'].get('Replicas', 1) for item in plan['services']})
                for service in plan['services']:
                    if self.closing:
                        raise ValueError('Daemon is shutting down')
                    spec = deepcopy(service['spec'])
                    if plan['action'] == 'restart':
                        template = spec.setdefault('TaskTemplate', {})
                        template['ForceUpdate'] = template.get('ForceUpdate', 0) + 1
                    else:
                        spec['Mode']['Replicated']['Replicas'] = service['target']
                    self.client.request('/services/' + quote(service['id'], safe='') + '/update', method='POST',
                                        query={'version': service['version']}, data=spec,
                                        timeout=self.settings.stack_control_timeout_seconds)
            job['status'] = 'succeeded'
        except Exception as exc:
            job['status'], job['error'] = 'failed', str(exc)
        finally:
            with self.lock:
                self.active.discard(key)
            self.publish('docker.stack.action.changed', {'name': job['name'], 'job': job['id']})

    def job(self, identity):
        with self.lock:
            if identity not in self.jobs:
                raise ValueError('Operation not found; daemon may have restarted')
            return dict(self.jobs[identity])
