from unittest.mock import Mock

import pytest

from backend.docker.labels import inventory
from backend.docker.client import DockerError
from backend.daemon import DaemonServer
from backend.daemon_config import DaemonSettings
from frontend.inventory import matches, parse_filters, matches_resource
from frontend.docker.models import LabelEntry, ContainerEntry
from common.protocol import ProtocolError


def test_label_counts_group_by_key_value_and_distinct_resource():
    client = Mock()
    labels = {'owner': 'ops', 'empty': ''}
    def request(path, **kwargs):
        return {'/containers/json': [{'Id': 'one', 'Labels': labels}, {'Id': 'two', 'Labels': {'owner': 'other'}}],
                '/images/json': [{'Id': 'image', 'Labels': labels}, {'Id': 'image', 'Labels': labels}],
                '/volumes': {'Volumes': [{'Name': 'volume', 'Labels': labels}]},
                '/services': [{'ID': 'service', 'Spec': {'Labels': labels,
                    'TaskTemplate': {'ContainerSpec': {'Labels': labels}}}}],
                '/nodes': [{'ID': 'node', 'Spec': {'Labels': labels}}]}[path]
    client.request.side_effect = request
    client.networks.return_value = [{'Id': 'network', 'Labels': labels}]
    client.info.return_value = {'ID': 'engine', 'Labels': ['owner=ops', 'empty='], 'Swarm': {'ControlAvailable': True}}
    data = inventory(client)
    assert not data['errors']
    row = next(item for item in data['labels'] if item['key'] == 'owner' and item['value'] == 'ops')
    for kind in ['containers', 'images', 'services', 'container_templates', 'networks', 'volumes', 'nodes', 'engines']:
        assert row[kind] == 1
    assert len(data['labels']) == 3
    filters, words = parse_filters([('q', 'label.key=owner label.images=1')])
    assert matches({'label': LabelEntry(**row)}, filters, words)
    filters, words = parse_filters([('q', 'label.value=')])
    assert matches({'label': LabelEntry(key='empty', value='')}, filters, words)


def test_stack_labels_are_counted_once_and_separately_from_container_labels():
    client = Mock()
    client.request.side_effect = lambda path, **kwargs: {'Volumes': []} if path == '/volumes' else []
    client.networks.return_value = []
    client.info.return_value = {}
    stacks = [{'kind': 'compose-meta', 'base_kind': 'compose', 'name': name, 'labels': {'owner': 'ops'}} for name in ['one', 'one', 'two']]
    result = inventory(client, stacks)
    assert result['labels'][0]['stacks'] == 2
    assert result['labels'][0]['containers'] == 0


def test_partial_label_failure_is_reported_without_losing_other_sources():
    client = Mock()
    client.request.side_effect = DockerError('denied')
    client.networks.return_value = [{'Id': 'network', 'Labels': {'owner': 'ops'}}]
    client.info.return_value = {}
    data = inventory(client)
    assert data['errors']
    assert data['labels'][0]['networks'] == 1
    assert data['labels'][0]['containers'] == 0


def test_disabled_docker_blocks_label_inventory():
    server = DaemonServer(DaemonSettings(_env_file=None, docker_access='disabled'))
    with pytest.raises(ProtocolError, match='disabled'):
        server.dispatch('docker.labels', {})


def test_resource_filter_binds_label_key_and_value_together():
    model = ContainerEntry(id='container', name='web')
    filters, words = parse_filters([('label.key', 'owner'), ('label.value', 'ops')])
    assert matches_resource('container', model, {'owner': 'ops'}, filters, words, {})
    assert not matches_resource('container', model, {'owner': 'other', 'team': 'ops'}, filters, words, {})
    assert not matches_resource('container', model, {}, filters, words, {})
    filters, words = parse_filters([('label.key', 'empty'), ('label.value', '')])
    assert matches_resource('container', model, {'empty': ''}, filters, words, {})
    assert not matches_resource('container', model, {'empty': 'other'}, filters, words, {})


def test_other_label_resource_overviews_include_only_safe_fields():
    from backend.docker.resources import inventory as resources
    client = Mock()
    client.networks.return_value = [{'Id': 'one', 'Name': 'demo', 'Labels': {'owner': 'ops'}, 'Driver': 'bridge', 'Secret': 'hidden'}]
    assert resources(client, 'networks')['resources'] == [{'id': 'one', 'name': 'demo', 'labels': {'owner': 'ops'}, 'state': '', 'driver': 'bridge'}]
    client.info.return_value = {'Swarm': {'ControlAvailable': True}}
    client.request.return_value = [{'ID': 'svc', 'Spec': {'Name': 'demo_web', 'Labels': {'owner': 'service'},
        'TaskTemplate': {'ContainerSpec': {'Labels': {'owner': 'template'}, 'Env': ['hidden']}}}}]
    assert resources(client, 'services')['resources'][0]['labels'] == {'owner': 'service'}
    assert resources(client, 'container_templates')['resources'][0]['labels'] == {'owner': 'template'}
    assert 'hidden' not in str(resources(client, 'container_templates'))
    with pytest.raises(ValueError):
        resources(client, 'invalid')
