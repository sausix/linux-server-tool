from __future__ import annotations
from common.identity import APP_NAME, STORAGE_ID, SESSION_COOKIE, SESSION_SALT, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, TEMP_PREFIX, MANAGED_HEADER
import logging
import os
import re
from backend.docker.client import DockerClient, DockerError
from backend.docker.configuration import inspect_local, read_config
from backend.docker.stacks import snapshot as stacks_snapshot
from backend.docker.containers import inventory as container_inventory, details as container_details
from backend.docker.images import inventory as image_inventory, details as image_details, delete_images, storage_snapshot
from backend.docker.volumes import inventory as volume_inventory, details as volume_details, storage as volume_storage, delete_volumes
from backend.docker.labels import inventory as label_inventory
from backend.docker.resources import inventory as resource_inventory
from backend.docker.compose.definitions import StackStore, validate_definition
from backend.docker.compose.metadata import validate_metadata, local_compose_path
from backend.docker.compose.files import read_source, save_source
from backend.docker.compose.control import StackControl
from backend.docker.logs import history as log_history, LiveLogs
from backend.docker.compose.managed import create as create_managed_stack, workspace
from backend.docker.networks import annotate_interfaces, docker_subnets, associate_endpoints
from backend.network.inventory import interfaces, addresses
from backend.network.inventory import veth_peer_links
from urllib.parse import urlsplit
from common.protocol import ProtocolError
from backend.docker.runtime_sources import needs_docker_source, annotate_docker_source
from backend.docker.compose.models import ComposeModels
from backend.docker.events import DockerEventSource
logger = logging.getLogger("backend.docker")

class DockerComponent:
    def __init__(self, server):
        self.server = server
        settings = server.settings
        server.stack_store = StackStore(settings.stack_database)
        server.compose_models = ComposeModels(settings, self.stack_store)
        self.events.observe(self.compose_models.refresh)
        self.event_runtime = None
        server.docker = DockerClient(
            settings.docker_endpoint,
            api_version=settings.docker_api_version,
            timeout=settings.docker_timeout_seconds,
            tls_verify=settings.docker_tls_verify,
            tls_ca=settings.docker_tls_ca,
            tls_cert=settings.docker_tls_cert,
            tls_key=settings.docker_tls_key,
        )
        server.stack_control = StackControl(self.docker, settings, self.stack_store, self.events.publish_from_thread, self.compose_models)

    def __getattr__(self, name):
        return getattr(self.server, name)

    event_source = DockerEventSource

    def enrich(self, model, result):
        if self.settings.docker_access == 'disabled':
            return
        if model == 'interfaces':
            self.enrich_interfaces(result)
        elif model in {'routes', 'firewall'}:
            result.update(self.docker_network_context())
        elif model == 'sysctl' and self.settings.feature_access('interfaces') != 'disabled':
            result.update(self.docker_network_context())

    def enrich_interfaces(self, inventory):
        if self.settings.docker_access == 'disabled':
            return
        try:
            networks = self.docker.networks()
            annotate_interfaces(inventory['interfaces'], networks)
            inventory['docker_subnets'] = docker_subnets(networks, {item['name'] for item in inventory['interfaces']}, inventory['addresses'])
            try:
                containers = self.docker.request('/containers/json', query={'all': 'true'})
                annotate_interfaces(inventory['interfaces'], networks, containers, self.docker_peer_links(inventory['interfaces']))
            except (DockerError, OSError, ValueError):
                inventory['extension_errors'] = ['Some integration associations are unavailable']
        except (DockerError, OSError, ValueError):
            inventory['extension_errors'] = ['Integration associations are unavailable']

    def live_logs(self, params):
        return LiveLogs(self.docker, self.settings, params, self.events)

    def interface_origins(self):
        return self.docker_network_context()['interface_origins']

    def annotate_configuration_sources(self, items):
        if not needs_docker_source(items) or self.settings.docker_access == 'disabled' or urlsplit(self.settings.docker_endpoint).scheme != 'unix':
            return
        configured = read_config(self.settings.host_etc_directory / 'docker/daemon.json')
        if configured['error'] or configured['settings'].get('ip-forward') is False or configured['settings'].get('firewall-backend') == 'nftables':
            return
        try:
            if self.docker.ping().get('reachable'):
                annotate_docker_source(items)
        except (DockerError, OSError, ValueError):
            return

    def check_stack_name(self, name):
        target = workspace(self.settings, name)
        if os.path.lexists(target) or self.stack_store.get(name) or self.stack_store.definition_tree(self.settings.docker_endpoint, name):
            raise ValueError('Stack name or directory is already in use')
        inventory = stacks_snapshot(self.docker, virtual_definitions=self.stack_store.list())
        if inventory['errors']:
            raise ValueError('Cannot verify a free stack name while Docker inventory is unavailable or incomplete')
        if any(stack['name'] == name for stack in inventory['stacks']):
            raise ValueError('Stack name is already in use')
        return {'name': name, 'directory': str(target), 'available': True}

    def docker_peer_links(self, links):
        if self.settings.feature_access('interfaces') == 'disabled' or urlsplit(self.settings.docker_endpoint).scheme != 'unix':
            return {}  # A remote engine's indices/MACs cannot establish local namespace identity.
        return veth_peer_links(links, timeout=self.settings.docker_timeout_seconds)

    def docker_network_context(self):
        empty = {'interface_origins': {}, 'docker_subnets': [], 'interface_stacks': {}}
        if self.settings.docker_access == 'disabled':
            return empty
        try:
            links = interfaces()
            names = {link['name'] for link in links}
            networks = self.docker.networks()
            annotate_interfaces(links, networks)
            try:
                containers = self.docker.request('/containers/json', query={'all': 'true'})
                annotate_interfaces(links, networks, containers, self.docker_peer_links(links))
            except (DockerError, OSError, ValueError):
                pass  # Keep bridge associations when endpoint correlation is unavailable.
            origins = {link['name']: 'docker' for link in links if link.get('docker_networks')}
            return {'interface_origins': origins,
                    'interface_stacks': {link['name']: link['docker_stacks'] for link in links if link.get('docker_stacks')},
                    'docker_subnets': docker_subnets(networks, names, addresses() if origins else [])}
        except (DockerError, OSError, ValueError):
            return empty

    def dispatch(self, method, params):
        if method == "docker.ping":
            return self.docker.ping()
        if method == "docker.version":
            return self.docker.version()
        if method == "docker.info":
            return self.docker.info()
        if method == 'docker.configuration':
            return inspect_local(self.settings.docker_endpoint,
                                 config_path=self.settings.host_etc_directory / 'docker/daemon.json',
                                 timeout=self.settings.command_timeout_seconds)
        if method == 'docker.overview':
            overview = {
                'endpoint': self.safe_endpoint(self.settings.docker_endpoint),
                'access': self.settings.docker_access,
                'api_version_setting': self.settings.docker_api_version,
                'tls_verify': self.settings.docker_tls_verify,
                'client_certificate': bool(self.settings.docker_tls_cert),
                'reachable': False, 'version': {}, 'info': {}, 'errors': [],
            }
            try:
                overview['reachable'] = self.docker.ping()['reachable']
            except (DockerError, OSError, ValueError) as exc:
                logger.warning('docker.overview connection failed: %s', exc)
                overview['errors'].append(f'Connection: {exc}')
            if not overview['reachable']:
                if not overview['errors']:
                    overview['errors'].append('Engine ping did not return OK')
                return overview
            for section, method, keys in (
                ('version', self.docker.version, ('Version', 'ApiVersion', 'MinAPIVersion', 'GitCommit', 'GoVersion', 'Os', 'Arch', 'KernelVersion', 'BuildTime')),
                ('info', self.docker.info, ('Name', 'ID', 'ServerVersion', 'OperatingSystem', 'OSType', 'Architecture', 'KernelVersion', 'NCPU', 'MemTotal', 'DockerRootDir', 'Driver', 'LoggingDriver', 'CgroupDriver', 'CgroupVersion', 'Containers', 'ContainersRunning', 'ContainersPaused', 'ContainersStopped', 'Images', 'IPv4Forwarding', 'BridgeNfIptables', 'BridgeNfIp6tables', 'LiveRestoreEnabled', 'Debug', 'SecurityOptions', 'Labels')),
            ):
                try:
                    raw = method()
                    overview[section] = {key: raw[key] for key in keys if key in raw}
                except (DockerError, OSError, ValueError) as exc:
                    logger.warning('docker.overview %s failed: %s', section, exc)
                    overview['errors'].append(f'{section}: {exc}')
            return overview
        if method == "docker.networks":
            return self.docker.networks()
        if method == 'docker.stack.name.check':
            return self.check_stack_name(params.get('name'))
        if method == 'docker.stack.create':
            self.check_stack_name(params.get('name'))
            result = create_managed_stack(self.settings, params.get('name'), params.get('files'), params.get('main_file'),
                                          apply=params.get('apply') is True, expected_hash=params.get('expected_hash'))
            if result['applied']:
                self.events.publish_from_thread('docker.stack.definition.changed', {'name': result['name']})
            return result
        if method == 'docker.stacks':
            links, peers = None, {}
            if self.settings.feature_access('interfaces') != 'disabled':
                try:
                    links = interfaces()
                    peers = self.docker_peer_links(links)
                except OSError as exc:
                    logger.debug('Stack bridge ports unavailable: %s', exc)
            result = stacks_snapshot(self.docker, settings=self.settings, virtual_definitions=self.stack_store.list(),
                                     model_registry=self.compose_models,
                                     interface_links=links, peer_links=peers,
                                     interface_names={link['name'] for link in links} if links is not None else None)
            if isinstance(params.get('runtime'), str):
                from backend.docker.stacks import runtime_info
                for stack in result['stacks']:
                    if stack['name'] == params['runtime'] and stack.get('running', 0):
                        stack['runtime'] = runtime_info(self.docker, stack)
            if self.event_runtime:
                paths = []
                if self.settings.docker_endpoint.startswith('unix://'):
                    for stack in result['stacks']:
                        for path in stack.get('metadata', {}).get('sources', []):
                            try:
                                paths.append(local_compose_path(path, self.settings))
                            except (OSError, ValueError):
                                continue
                if self.settings.stack_database.exists():
                    paths.append(self.settings.stack_database)
                self.event_runtime.add_compose_paths(paths)
            return result
        if method == 'docker.stack.model':
            if not isinstance(params.get('name'), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', params['name']):
                raise ValueError('A valid Compose stack name is required')
            self.dispatch('docker.stacks', {})
            return self.compose_models.details(params.get('name'))
        if method == 'docker.stack.model.change':
            if not isinstance(params.get('name'), str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,127}', params['name']):
                raise ValueError('A valid Compose stack name is required')
            inventory = self.dispatch('docker.stacks', {})
            if sum(stack['name'] == params['name'] for stack in inventory['stacks']) != 1:
                raise ValueError('Stack name must identify one unique stack')
            result = self.compose_models.change_tree(params['name'], params)
            if result['applied']:
                self.events.publish_from_thread('docker.stack.definition.changed', {'name': params['name']})
            return result
        if method == 'docker.stack.model.node':
            self.dispatch('docker.stack.model', {'name': params.get('name')})
            from backend.docker.compose.tree import document
            tree = self.compose_models.tree(params['name'])
            selected = tree['active'] if tree and params.get('node') == 'active' else params.get('node')
            if not tree or not any(node['id'] == selected for node in tree['nodes']):
                raise ValueError('Definition node not found')
            return self.compose_models.node_info(params['name'], selected)
        if method == 'docker.stack.definition.get':
            definition = self.stack_store.get(params.get('name'))
            if definition is None:
                raise ValueError('Virtual stack definition not found')
            return definition
        if method in {'docker.stack.file.get', 'docker.stack.file.save', 'docker.stack.convert', 'docker.stack.action', 'docker.stack.job'}:
            # Keep injected/test clients and runtime clients aligned.
            self.stack_control.client = self.docker
            name, kind = params.get('name'), params.get('kind', 'compose')
            if method == 'docker.stack.job':
                return self.stack_control.job(params.get('id'))
            if method == 'docker.stack.action':
                if params.get('apply') is True:
                    return self.stack_control.apply(name, kind, params.get('action'), params.get('expected'))
                return self.stack_control.preview(name, kind, params.get('action'))
            if method == 'docker.stack.convert':
                return self.stack_control.convert(name, params.get('apply') is True, params.get('expected'))
            stack = self.stack_control.find(name, kind)
            if method == 'docker.stack.file.get':
                return {**read_source(stack, params.get('path'), self.settings), 'kind': stack['kind'], 'origin': stack.get('definition_origin')}
            result = save_source(stack, params.get('path'), params.get('content'), params.get('expected_hash'), self.settings, params.get('apply', False))
            if result['applied']:
                self.events.publish_from_thread('docker.compose.changed', {'name': name})
            return result
        if method == 'docker.stack.definition.save':
            name, definition = params.get('name'), params.get('definition')
            validate_definition(name, definition)
            expected = params.get('expected_revision')
            if expected is not None and (type(expected) is not int or expected < 1):
                raise ValueError('Expected revision must be a positive integer')
            existing = self.stack_store.get(name)
            if existing is None and not definition.get(SOURCE_KEY) and os.path.lexists(workspace(self.settings, name)):
                raise ValueError('Stack directory already exists')
            if (existing['revision'] if existing else None) != expected:
                raise ValueError('Definition changed or already exists; reload and review again')
            if existing is None:
                discovered = stacks_snapshot(self.docker)
                if discovered['errors']:
                    raise ValueError('Cannot check project-name conflicts while Docker inventory is incomplete')
                if any(item['name'] == name and item['kind'] == 'compose' for item in discovered['stacks']):
                    raise ValueError('A discovered Compose project already uses this name; choose another virtual stack name')
            if params.get('apply', False) is not True:
                return {'name': name, 'metadata': validate_metadata(definition), 'applied': False}
            directory = workspace(self.settings, name)
            made_directory = False
            if existing is None and not definition.get(SOURCE_KEY):
                if os.path.lexists(directory):
                    raise ValueError('Stack directory already exists')
                directory.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
                directory.mkdir(mode=0o750)
                made_directory = True
            try:
                result = self.stack_store.save(name, definition, expected)
            except Exception:
                if made_directory:
                    directory.rmdir()
                raise
            self.events.publish_from_thread('docker.stack.definition.changed', {'name': name})
            return {**result, 'applied': True}
        if method == 'docker.containers':
            return container_inventory(self.docker)
        if method == 'docker.logs.tail':
            return log_history(self.docker, params, self.settings)
        if method == 'docker.container':
            result = container_details(self.docker, params.get('id'))
            links, peers, networks = [], {}, []
            try:
                networks = self.docker.networks()
            except (DockerError, OSError, ValueError) as exc:
                result.setdefault('network_errors', []).append(f'Network ownership unavailable: {exc}')
            if self.settings.feature_access('interfaces') != 'disabled':
                try:
                    links = interfaces()
                    peers = self.docker_peer_links(links)
                except OSError as exc:
                    result.setdefault('network_errors', []).append(f'Host interface mapping unavailable: {exc}')
            associate_endpoints(result['networks'], networks, links, peers)
            return result
        if method == 'docker.images':
            return image_inventory(self.docker)
        if method == 'docker.volumes':
            return volume_inventory(self.docker)
        if method == 'docker.volume':
            return volume_details(self.docker, params.get('name'))
        if method == 'docker.volumes.storage':
            return volume_storage(self.docker, self.settings.docker_storage_timeout_seconds)
        if method == 'docker.volumes.delete':
            result = delete_volumes(self.docker, params.get('ids'), params.get('apply', False), params.get('expected'))
            if result['deleted']:
                self.events.publish_from_thread('docker.volume.changed', {'names': result['deleted']})
            return result
        if method == 'docker.labels':
            stacks = self.dispatch('docker.stacks', {})
            result = label_inventory(self.docker, stacks['stacks'])
            result['errors'].extend(stacks['errors'])
            result['errors'].extend(f'{stack["name"]}: stack label definition is unavailable' for stack in stacks['stacks'] if stack.get('compose_model', {}).get('error'))
            return result
        if method == 'docker.resources':
            return resource_inventory(self.docker, params.get('kind'))
        if method == 'docker.images.storage':
            return storage_snapshot(self.docker, self.settings.docker_storage_timeout_seconds)
        if method == 'docker.image':
            return image_details(self.docker, params.get('image'))
        if method == 'docker.images.delete':
            return delete_images(self.docker, params.get('ids'), params.get('apply', False), params.get('expected'))
        raise ProtocolError(-32601, "Method not found")
