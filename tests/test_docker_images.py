from unittest.mock import Mock

import pytest

from backend.docker.images import inventory, details, delete_images, storage_snapshot
from backend.docker.client import DockerError
from backend.daemon import DaemonServer
from backend.daemon_config import DaemonSettings
from common.protocol import ProtocolError


def engine(containers=(), services=(), manager=False):
    client = Mock()
    images = [{'Id': 'sha256:' + letter * 64, 'ParentId': '', 'RepoTags': [name], 'Size': 100, 'SharedSize': 40}
              for letter, name in [('a', 'web:latest'), ('b', 'old:1'), ('c', 'spare:1'), ('d', 'scaled:1')]]
    def request(path, **kwargs):
        if path == '/images/json':
            return images
        if path == '/containers/json':
            return list(containers)
        if path == '/services':
            return list(services)
        return {'Id': 'sha256:' + 'a' * 64, 'Os': 'linux', 'Architecture': 'amd64', 'Config': {'Env': ['SECRET=hidden'], 'Labels': {'owner': 'example'}}}
    client.request.side_effect = request
    client.info.return_value = {'Swarm': {'ControlAvailable': manager}}
    return client


def test_usage_and_references_use_immutable_container_image_ids():
    client = engine(containers=[
        {'Id': 'one', 'Names': ['/web'], 'ImageID': 'sha256:' + 'a' * 64, 'State': 'running', 'Labels': {'com.docker.compose.project': 'demo'}},
        {'Id': 'two', 'Names': ['/old'], 'ImageID': 'sha256:' + 'b' * 64, 'Image': 'web:latest', 'State': 'exited'},
    ], manager=True, services=[{'ID': 'service', 'Spec': {'Name': 'test_service', 'Labels': {'com.docker.stack.namespace': 'test'},
        'TaskTemplate': {'ContainerSpec': {'Image': 'docker.io/library/scaled:1'}}}, 'ServiceStatus': {'RunningTasks': 0}}])
    result = {item['name']: item for item in inventory(client)['images']}
    assert result['web:latest']['usage'] == 'active'
    assert result['old:1']['usage'] == 'referenced'
    assert result['spare:1']['usage'] == 'unused'
    assert result['scaled:1']['usage'] == 'referenced'
    assert result['web:latest']['container_items'][0]['id'] == 'one'
    assert result['web:latest']['stack_items'] == [{'kind': 'compose', 'name': 'demo'}]
    assert result['scaled:1']['stack_items'] == [{'kind': 'swarm', 'name': 'test'}]
    assert result['web:latest']['unique_size'] == 60


def test_failed_reference_reads_never_claim_unused():
    client = engine()
    original = client.request.side_effect
    client.request.side_effect = lambda path, **kwargs: (_ for _ in ()).throw(DockerError('denied')) if path == '/containers/json' else original(path, **kwargs)
    result = inventory(client)
    assert result['errors']
    assert {item['usage'] for item in result['images']} == {'unknown'}


def test_detail_whitelist_and_reference_path_encoding():
    client = engine()
    result = details(client, 'registry:5000/team/web:latest')
    assert result['architecture'] == 'amd64'
    assert result['labels'] == {'owner': 'example'}
    assert 'hidden' not in str(result)
    assert client.request.call_args_list[0].args[0] == '/images/registry%3A5000%2Fteam%2Fweb%3Alatest/json'
    with pytest.raises(ValueError):
        details(client, 'bad\nreference')


def test_disabled_docker_denies_image_rpcs():
    server = DaemonServer(DaemonSettings(_env_file=None, docker_access='disabled'))
    for method in ['docker.images', 'docker.image', 'docker.images.storage']:
        with pytest.raises(ProtocolError, match='disabled'):
            server.dispatch(method, {'image': 'test:latest'})


def test_digest_service_reference_and_unknown_shared_storage():
    client = engine(manager=True, services=[{'ID': 'service', 'Spec': {'Name': 'web',
        'TaskTemplate': {'ContainerSpec': {'Image': 'docker.io/library/web:latest@sha256:abc'}}},
        'ServiceStatus': {'RunningTasks': 2}}])
    original = client.request.side_effect
    def request(path, **kwargs):
        result = original(path, **kwargs)
        if path == '/images/json':
            result[0]['RepoDigests'] = ['web@sha256:abc']
            result[0]['SharedSize'] = -1
        return result
    client.request.side_effect = request
    image = next(item for item in inventory(client)['images'] if item['name'] == 'web:latest')
    assert image['usage'] == 'active'
    assert image['services'] == 1
    assert image['shared_size'] is None
    assert image['unique_size'] is None


def test_aggregate_storage_and_delete_preview_revalidation():
    client = engine()
    original = client.request.side_effect
    def request(path, **kwargs):
        if path == '/system/df':
            return {'LayersSize': 280}
        return original(path, **kwargs)
    client.request.side_effect = request
    storage = inventory(client, include_storage=True)['storage']
    assert storage['images'] == 4
    assert storage['unused'] == 4
    assert storage['layers_size'] == 280
    assert storage['logical_size'] == 400
    assert storage['unique_size'] == 240
    assert storage['unused_size'] == 400
    assert len(storage['unused_image_ids']) == 4
    image_id = 'sha256:' + 'a' * 64
    preview = delete_images(client, [image_id])
    assert not preview['errors']
    assert not any(call.kwargs.get('method') == 'DELETE' for call in client.request.call_args_list)
    assert delete_images(client, [image_id], True, {image_id: ['changed:tag']})['errors']
    assert delete_images(client, [image_id], True, {image_id: ['web:latest']})['deleted'] == [image_id]
    delete = next(call for call in client.request.call_args_list if call.kwargs.get('method') == 'DELETE')
    assert delete.kwargs['query'] == {'force': 'false', 'noprune': 'true'}


def test_image_deletion_rejects_references_and_readonly():
    image_id = 'sha256:' + 'a' * 64
    client = engine(containers=[{'Id': 'one', 'ImageID': image_id, 'State': 'exited'}])
    assert delete_images(client, [image_id], True, {image_id: ['web:latest']})['errors']
    assert not any(call.kwargs.get('method') == 'DELETE' for call in client.request.call_args_list)
    with pytest.raises(ValueError):
        delete_images(client, ['web:latest'])
    server = DaemonServer(DaemonSettings(_env_file=None, docker_access='readonly'))
    with pytest.raises(ProtocolError, match='read-only'):
        server.dispatch('docker.images.delete', {'ids': [image_id], 'apply': True})


def test_child_images_are_references_and_block_deletion_before_execution():
    client = engine()
    original = client.request.side_effect
    parent = 'sha256:' + 'a' * 64
    def request(path, **kwargs):
        result = original(path, **kwargs)
        if path == '/images/json':
            result[1]['ParentId'] = parent
        return result
    client.request.side_effect = request
    data = inventory(client)
    image = next(item for item in data['images'] if item['id'] == parent)
    assert image['usage'] == 'referenced'
    assert image['children'] == 1
    assert image['child_items'] == [{'id': 'sha256:' + 'b' * 64, 'name': 'old:1'}]
    assert data['storage']['unused'] == 3
    assert data['storage']['unused_size'] == 300
    assert parent not in data['storage']['unused_image_ids']
    for apply in [False, True]:
        result = delete_images(client, [parent], apply, {parent: ['web:latest']})
        assert 'dependent child images: old:1' in result['errors'][0]
        assert not result['deleted']
    assert not any(call.kwargs.get('method') == 'DELETE' for call in client.request.call_args_list)


def test_missing_dependency_metadata_never_claims_unused():
    client = engine()
    original = client.request.side_effect
    def request(path, **kwargs):
        result = original(path, **kwargs)
        if path == '/images/json':
            for item in result:
                item.pop('ParentId')
        return result
    client.request.side_effect = request
    assert {item['usage'] for item in inventory(client)['images']} == {'unknown'}


def test_delete_conflict_shows_message_without_prefix():
    client = engine()
    original = client.request.side_effect
    message = 'conflict: image has dependent child images'
    def request(path, **kwargs):
        if kwargs.get('method') == 'DELETE':
            raise DockerError(message, status_code=409)
        return original(path, **kwargs)
    client.request.side_effect = request
    image_id = 'sha256:' + 'a' * 64
    assert delete_images(client, [image_id], True, {image_id: ['web:latest']})['errors'] == [message]


@pytest.mark.parametrize(('body', 'expected'), [
    (b'{"message":"conflict: image has dependent child images"}', 'conflict: image has dependent child images'),
    (b'Plain error', 'Plain error'),
])
def test_docker_error_uses_response_message(body, expected):
    from backend.docker.client import DockerClient
    client = DockerClient('unix:///unused', api_version='v1.47')
    connection = Mock()
    connection.getresponse.return_value.status = 409
    connection.getresponse.return_value.read.return_value = body
    client._connection = lambda: connection
    with pytest.raises(DockerError) as error:
        client.request('/images/test', method='DELETE')
    assert str(error.value) == expected
    assert error.value.status_code == 409


def test_image_inventory_and_details_do_not_wait_for_disk_statistics():
    client = engine()
    original = client.request.side_effect
    def request(path, **kwargs):
        if path == '/system/df':
            raise DockerError('timed out')
        return original(path, **kwargs)
    client.request.side_effect = request
    assert len(inventory(client)['images']) == 4
    assert details(client, 'web:latest')['name'] == 'web:latest'
    assert not any(call.args[0] == '/system/df' for call in client.request.call_args_list)
    assert client.request.call_args_list[0].kwargs['query'] == {'all': 'true'}
    with pytest.raises(DockerError, match='timed out'):
        storage_snapshot(client, 30)


def test_storage_snapshot_uses_own_timeout_and_returns_only_layer_fields():
    client = Mock()
    client.request.return_value = {'LayersSize': 123, 'Volumes': [{'secret': 'hidden'}],
        'Images': [{'Id': 'image', 'Size': 100, 'SharedSize': 40}]}
    assert storage_snapshot(client, 30) == {'layers_size': 123, 'images': {'image': {'shared_size': 40, 'unique_size': 60}}}
    client.request.assert_called_once_with('/system/df', timeout=30)


def test_storage_timeout_environment_and_cli_overrides(monkeypatch):
    from frontend.config import Settings
    from frontend.cli import build_parser as frontend_parser
    from backend.daemon import build_parser as daemon_parser
    monkeypatch.setenv('BACKEND_DOCKER_STORAGE_TIMEOUT_SECONDS', '50')
    monkeypatch.setenv('FRONTEND_DAEMON_STORAGE_TIMEOUT_SECONDS', '70')
    assert DaemonSettings(_env_file=None).docker_storage_timeout_seconds == 50
    assert Settings(_env_file=None).daemon_storage_timeout_seconds == 70
    args = daemon_parser().parse_args(['--docker-storage-timeout-seconds', '80'])
    assert DaemonSettings(_env_file=None, docker_storage_timeout_seconds=args.docker_storage_timeout_seconds).docker_storage_timeout_seconds == 80
    args = frontend_parser().parse_args(['--daemon-storage-timeout-seconds', '90'])
    assert Settings(_env_file=None, daemon_storage_timeout_seconds=args.daemon_storage_timeout_seconds).daemon_storage_timeout_seconds == 90
