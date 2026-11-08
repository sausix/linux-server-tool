from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from backend.docker.compose.models import ComposeModels, Definition, merge_patch, normalize_labels
from backend.daemon_config import DaemonSettings
from backend.docker.compose.definitions import StackStore
from backend.docker.compose.metadata import load_definition, validate_metadata
from backend.events import EventBroker


def settings(tmp_path):
    return DaemonSettings(_env_file=None, compose_allowed_roots=[tmp_path], compose_root=tmp_path / 'managed',
                          stack_database=tmp_path / 'state.sqlite3')


def entry(document, origin=None):
    return {'document': document, 'origin': origin or {'type': 'virtual'}, 'warnings': []}


def test_complete_file_structure_is_parsed_and_updates_without_changing_running_baseline(tmp_path):
    config = settings(tmp_path)
    source = tmp_path / 'compose.yaml'
    source.write_text('services: {web: {image: "web:1"}}\nnetworks: {front: {driver: bridge}}\nvolumes: {data: {}}\nsecrets: {password: {external: true}}\nx-stackmeta: {labels: {owner: ops}}\n')
    stack = {'name': 'app', 'config_files': [str(source)], 'working_directory': str(tmp_path)}
    document, _, warnings = load_definition(stack, config)
    assert not warnings
    assert all(key in document for key in ('networks', 'volumes', 'secrets'))
    models = ComposeModels(config)
    models.update({'app': entry(document, {'type': 'file', 'files': [str(source)], 'directory': str(tmp_path), 'available': True})},
                  [{'name': 'app', 'kind': 'compose', 'running': 1}])
    before = models.details('app')
    assert before['baseline_evidence'] == 'observed-unverified'
    source.write_text(source.read_text().replace('web:1', 'web:2'))
    broker = EventBroker()
    broker.observe(models.refresh)
    broker.publish_from_thread('docker.compose.changed', {})
    after = models.details('app')
    assert after['definition']['services']['web']['image'] == 'web:2'
    assert after['running_definition']['services']['web']['image'] == 'web:1'
    assert after['pending_changes'] and 'web:2' in after['diff']
    assert after['overlay'][0]['op'] == 'replace'
    source.unlink()
    broker.publish_from_thread('docker.compose.changed', {})
    unavailable = models.details('app')
    assert unavailable['definition'] is None and unavailable['error']
    assert unavailable['origin']['type'] == 'file' and not unavailable['origin']['available']
    assert unavailable['running_definition'] == after['running_definition']


def test_template_chains_add_override_remove_and_do_not_mutate_bases(tmp_path):
    models = ComposeModels(settings(tmp_path))
    base = {'services': {'web': {'image': 'web:1', 'environment': {'A': '1', 'B': '2'}}}, 'volumes': {'data': {}},
            'x-stackmeta': {'labels': ['owner=ops', 'empty']}}
    middle = {'x-stackmeta': {'template': 'base', 'labels': {'stage': 'test'},
                             'overlay': {'services': {'web': {'environment': {'B': None, 'C': '3'}}}}}}
    child = {'x-stackmeta': {'template': 'middle', 'overlay': {'volumes': {'data': None}}},
             'services': {'web': {'image': 'web:2'}}}
    resolved = models.update({'base': entry(base), 'middle': entry(middle), 'app': entry(child)}, [])
    assert resolved['app']['services']['web'] == {'image': 'web:2', 'environment': {'A': '1', 'C': '3'}}
    assert resolved['app']['volumes'] == {}
    assert resolved['app']['x-stackmeta']['labels'] == {'owner': 'ops', 'empty': '', 'stage': 'test'}
    assert base['services']['web']['environment']['B'] == '2'
    assert models.summary('app')['origin']['type'] == 'template'
    missing = models.update({'base': entry(None)}, [])
    assert 'app' not in missing and models.summary('app')['error']


def test_cycles_are_unavailable_and_applied_definition_survives_restart(tmp_path):
    config = settings(tmp_path)
    store = StackStore(config.stack_database)
    models = ComposeModels(config, store)
    models.update({'a': entry({'x-stackmeta': {'template': 'b'}}), 'b': entry({'x-stackmeta': {'template': 'a'}})}, [])
    assert 'cycle' in models.summary('a')['error']
    document = {'services': {'web': {'image': 'web:1', 'command': None}}}
    models.update({'app': entry(document)}, [])
    models.mark_applied('app', document)
    assert store.running_definition(config.docker_endpoint, 'app') == document
    other = ComposeModels(config, store)
    other.update({'app': entry({'services': {'web': {'image': 'web:2', 'command': None}}})}, [])
    assert other.details('app')['baseline_evidence'] == 'applied'
    assert other.details('app')['running_definition'] == document
    assert other.details('app')['pending_changes']
    frozen = Definition.capture(document)
    copy = frozen.document
    copy['services'].clear()
    assert frozen.document == document
    with pytest.raises(FrozenInstanceError):
        frozen.text = 'changed'


@pytest.mark.parametrize('labels,expected', [({'owner': 'ops', 'flag': None}, {'flag': '', 'owner': 'ops'}),
    (['owner=old', 'owner=ops', 'empty', 'url=a=b'], {'empty': '', 'owner': 'ops', 'url': 'a=b'})])
def test_stack_labels_follow_compose_mapping_and_list_forms(labels, expected):
    metadata = validate_metadata({'services': {}, 'x-stackmeta': {'labels': labels}})
    assert metadata['labels'] == expected and metadata['eligible']
    assert normalize_labels(labels) == expected


@pytest.mark.parametrize('labels', [True, ['x', 3], {'owner': False}, {'com.docker.compose.project': 'bad'}, {'': 'bad'}])
def test_invalid_stack_label_forms_are_reported(labels):
    meta = validate_metadata({'services': {}, 'x-stackmeta': {'labels': labels}})
    assert not meta['eligible'] and meta['invalid_fields'][0]['field'] == 'labels'
