from unittest.mock import Mock

import pytest

from backend.docker.volumes import inventory, details, delete_volumes, fingerprint, storage
from backend.docker.client import DockerError
from backend.daemon import DaemonServer
from backend.daemon_config import DaemonSettings
from common.protocol import ProtocolError
from frontend.inventory import matches_resource, parse_filters
from frontend.docker.models import VolumeEntry


def engine(containers=(), services=(), manager=False):
    client = Mock()
    volumes = [{'Name': name, 'Driver': 'local', 'CreatedAt': '2026-11-02T12:00:00Z',
                'Labels': {'owner': 'demo'} if name == 'active' else {}, 'Options': {}, 'Scope': 'local'}
               for name in ['unused', 'active', 'referenced', 'service']]
    def request(path, **kwargs):
        if path == '/volumes':
            return {'Volumes': volumes}
        if path == '/containers/json':
            return list(containers)
        if path == '/services':
            return list(services)
        if path == '/system/df':
            return {'Volumes': [{'Name': 'unused', 'UsageData': {'Size': 1234}}]}
        if kwargs.get('method') == 'DELETE':
            return None
        return next(item for item in volumes if '/volumes/' + item['Name'] == path)
    client.request.side_effect = request
    client.info.return_value = {'Swarm': {'ControlAvailable': manager}}
    return client, volumes


def test_volume_usage_includes_stopped_containers_and_service_specs():
    containers = [{'Id': name, 'Names': ['/' + name], 'State': state,
                   'Labels': {'com.docker.compose.project': 'demo'},
                   'Mounts': [{'Type': 'volume', 'Name': name, 'Destination': '/data'}]}
                  for name, state in [('active', 'running'), ('referenced', 'exited')]]
    services = [{'ID': 'svc', 'Spec': {'Name': 'svc', 'Labels': {'com.docker.stack.namespace': 'cluster'},
                'TaskTemplate': {'ContainerSpec': {'Mounts': [{'Type': 'volume', 'Source': 'service'}]}}}}]
    client, _ = engine(containers, services, manager=True)
    result = {item['name']: item for item in inventory(client)['volumes']}
    assert {name: item['usage'] for name, item in result.items()} == {
        'active': 'active', 'referenced': 'referenced', 'service': 'referenced', 'unused': 'unused'}
    assert result['active']['stack_items'] == [{'kind': 'compose', 'name': 'demo'}]
    assert result['service']['stack'] == ['cluster']
    filters, words = parse_filters([('q', 'volume.container=active volume.stack=demo label.key=owner label.value=demo')])
    highlights = {}
    assert matches_resource('volume', VolumeEntry(**result['active']), result['active']['labels'], filters, words, highlights)
    assert 'volume.container' in highlights


def test_anonymous_label_presence_sort_order_and_compose_service_filter():
    client, raw = engine(containers=[{'Id': 'c1', 'Names': ['/web'], 'State': 'running',
        'Labels': {'com.docker.compose.project': 'demo', 'com.docker.compose.service': 'web'},
        'Mounts': [{'Type': 'volume', 'Name': 'active', 'Destination': '/data'}]}])
    raw[1]['Labels']['com.docker.volume.anonymous'] = ''
    raw.extend([{'Name': 'A-name', 'Labels': {}}, {'Name': 'a-name', 'Labels': {}}, {'Name': '0' * 64, 'Labels': {}}])
    data = inventory(client)
    assert data['volumes'][-1]['name'] == 'active'
    assert data['volumes'][-1]['anonymous'] is True
    assert next(item for item in data['volumes'] if item['name'] == '0' * 64)['anonymous'] is False
    assert [item['name'] for item in data['volumes'][:-1]] == sorted(
        [item['name'] for item in data['volumes'][:-1]], key=lambda name: (name.casefold(), name))
    active = data['volumes'][-1]
    assert active['service'] == ['web']
    assert active['service_items'] == [{'id': '', 'name': 'web', 'stack': 'demo', 'stack_kind': 'compose'}]
    filters, words = parse_filters([('q', 'volume.anonymous=true volume.service=web')])
    assert matches_resource('volume', VolumeEntry(**active), active['labels'], filters, words, {})


def test_incomplete_inventory_never_claims_unused():
    client, _ = engine()
    original = client.request.side_effect
    def request(path, **kwargs):
        if path == '/containers/json':
            raise DockerError('denied')
        return original(path, **kwargs)
    client.request.side_effect = request
    assert {item['usage'] for item in inventory(client)['volumes']} == {'unknown'}
    assert delete_volumes(client, ['unused'], True, {})['errors']
    assert not any(call.kwargs.get('method') == 'DELETE' for call in client.request.call_args_list)


def test_volume_detail_preserves_driver_and_cluster_inspect_fields():
    client, raw = engine()
    raw[0].update({'Mountpoint': '/var/lib/docker/volumes/unused/_data', 'Status': {'driver': 'ready'},
                   'ClusterVolume': {'ID': 'cluster'}, 'UsageData': {'Size': 123, 'RefCount': 0}})
    result = details(client, 'unused')
    assert result['inspect'] == raw[0]
    assert result['size'] == 123 and result['usage'] == 'unused'
    assert result['errors'] == []
    for name in ['../data', '', None, 'x/y']:
        with pytest.raises(ValueError):
            details(client, name)


def test_delete_rechecks_references_and_identity_and_never_forces():
    client, raw = engine()
    preview = delete_volumes(client, ['unused'])
    expected = {'unused': preview['items'][0]['fingerprint']}
    assert not preview['applied'] and not preview['deleted']
    raw[0]['CreatedAt'] = '2026-11-02T13:00:00Z'
    assert delete_volumes(client, ['unused'], True, expected)['errors']
    expected['unused'] = fingerprint(raw[0])
    result = delete_volumes(client, ['unused'], True, expected)
    assert result['deleted'] == ['unused']
    assert client.request.call_args.kwargs == {'method': 'DELETE', 'query': {'force': 'false'}}
    assert storage(client, 45) == {'volumes': {'unused': 1234}}
    assert client.request.call_args.kwargs == {'timeout': 45}


def test_reference_added_after_preview_blocks_deletion():
    containers = []
    client, _ = engine(containers)
    preview = delete_volumes(client, ['unused'])
    containers.append({'Id': 'new', 'State': 'created', 'Mounts': [{'Type': 'volume', 'Name': 'unused'}]})
    result = delete_volumes(client, ['unused'], True, {'unused': preview['items'][0]['fingerprint']})
    assert result['errors'] and not result['deleted']


def test_disabled_and_readonly_daemon_guards():
    disabled = DaemonServer(DaemonSettings(_env_file=None, docker_access='disabled'))
    for method in ['docker.volumes', 'docker.volumes.storage', 'docker.volumes.delete']:
        with pytest.raises(ProtocolError, match='disabled'):
            disabled.dispatch(method, {})
    readonly = DaemonServer(DaemonSettings(_env_file=None, docker_access='ro'))
    with pytest.raises(ProtocolError, match='read-only'):
        readonly.dispatch('docker.volumes.delete', {'ids': ['unused']})


@pytest.mark.parametrize('ids', [[], ['../etc'], ['x/y'], ['bad\nname'], 'unused'])
def test_invalid_deletion_names_are_rejected(ids):
    with pytest.raises(ValueError):
        delete_volumes(Mock(), ids)
