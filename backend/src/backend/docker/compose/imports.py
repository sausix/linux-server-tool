from common.identity import APP_NAME, STORAGE_ID, SESSION_COOKIE, SESSION_SALT, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, TEMP_PREFIX, MANAGED_HEADER
"""Publish definition resources in a dedicated daemon-owned directory."""
from pathlib import Path
import re
import shutil
import yaml

from common.stack_files import validate_bundle
from backend.docker.compose.managed import create, workspace
from backend.docker.compose.metadata import parse_document
from backend.docker.compose.definitions import MAX_DEFINITION_BYTES, validate_definition


def prepare(settings, name, params):
    identity = params.get('identity')
    if not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{32}', identity):
        raise ValueError('A definition identity is required')
    root = workspace(settings, name) / DEFINITIONS_DIRECTORY
    if not root.resolve().is_relative_to(settings.compose_root.resolve()):
        raise ValueError('Definition directory escapes the managed stack root')
    if any(path.is_symlink() for path in (workspace(settings, name), root)):
        raise ValueError('Definition directories must not be symbolic links')
    files, main = params.get('files'), params.get('main_file')
    decoded = validate_bundle(files, main)
    if len(decoded[main]) > MAX_DEFINITION_BYTES:
        raise ValueError('Compose definitions are limited to 1 MiB')
    try:
        definition = parse_document(decoded[main].decode('utf-8'), Path(main).suffix.lower())
    except (ValueError, UnicodeError, yaml.YAMLError, RecursionError):
        # YAML parsers can embed file contents in their exception text.
        raise ValueError('Unable to parse the Compose definition; check YAML/JSON syntax') from None
    if isinstance(definition, dict) and 'name' in definition:
        definition['name'] = name
    validate_definition(name, definition)
    scoped = settings.model_copy(update={'compose_root': root})
    assessment = create(scoped, identity, files, main)
    context = {'directory': str(root / identity / Path(main).parent), 'asset_root': str(root / identity)}
    return definition, context, assessment, scoped


def publish(scoped, params, expected_hash):
    return create(scoped, params['identity'], params['files'], params['main_file'], apply=True, expected_hash=expected_hash)


def discard(context, settings):
    path = Path(context['asset_root'])
    if (path.name and re.fullmatch(r'[a-f0-9]{32}', path.name) and path.parent.name == DEFINITIONS_DIRECTORY
            and path.resolve().is_relative_to(settings.compose_root.resolve()) and not path.is_symlink()):
        shutil.rmtree(path)
