from common.identity import APP_NAME, STORAGE_ID, SESSION_COOKIE, SESSION_SALT, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, TEMP_PREFIX, MANAGED_HEADER
"""Daemon-owned JSON Compose definitions; never modifies administrator files."""

import json
import math
from pathlib import Path
import re
import sqlite3

MAX_DEFINITION_BYTES = 1024 * 1024


def validate_json_tree(value):
    pending, nodes, remaining = [(value, 0)], 0, MAX_DEFINITION_BYTES
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > 100000 or depth > 32 or remaining < 0:
            raise ValueError('Compose data is too large, recursive or deeply nested')
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError('Compose object keys must be strings')
            remaining -= sum(len(key.encode()) for key in item)
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            remaining -= len(item.encode())
        elif item is not None and type(item) not in {bool, int, float}:
            raise ValueError('Compose data must use JSON-compatible values')
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError('Compose numbers must be finite')
    if remaining < 0:
        raise ValueError('Expanded Compose data is too large')


def validate_definition(name, definition):
    if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,127}', name):
        raise ValueError('Use a Compose project name: lowercase letters, digits, underscores or hyphens')
    template = definition.get('x-stackmeta', {}).get('template') if isinstance(definition, dict) and isinstance(definition.get('x-stackmeta'), dict) else None
    if not isinstance(definition, dict) or not isinstance(definition.get('services', {} if template else None), dict):
        raise ValueError('A Compose definition must be an object with a services object')
    if any(not isinstance(key, str) or (not isinstance(value, dict) and not (template and value is None)) for key, value in definition.get('services', {}).items()):
        raise ValueError('Each Compose service must be a named object')
    validate_json_tree(definition)
    try:
        text = json.dumps(definition, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError('The definition must be a finite JSON document') from exc
    if len(text.encode()) > MAX_DEFINITION_BYTES:
        raise ValueError('Compose definitions are limited to 1 MiB')
    if definition.get('name', name) != name:
        raise ValueError('The Compose name must match the virtual stack name')
    source = definition.get(SOURCE_KEY)
    if source is not None and (not isinstance(source, dict) or source.get('type') != 'compose-files'
            or not isinstance(source.get('files'), list) or not source['files']
            or any(not isinstance(path, str) or not Path(path).is_absolute() for path in source['files'])
            or not isinstance(source.get('project_directory'), str) or not Path(source['project_directory']).is_absolute()):
        raise ValueError('x-frontend-source must record compose-files, absolute source files and an absolute project_directory')
    return text


class StackStore:
    def __init__(self, path):
        self.path = Path(path)

    def list(self):
        if not self.path.exists():
            return []
        with sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stack_definitions'").fetchone():
                return []
            rows = db.execute('SELECT name, definition, revision FROM stack_definitions ORDER BY name').fetchall()
        return [{'name': name, 'definition': json.loads(text), 'revision': revision} for name, text, revision in rows]

    def get(self, name):
        return next((item for item in self.list() if item['name'] == name), None)

    def save(self, name, definition, expected_revision=None):
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
            raise ValueError('Expected revision must be a positive integer')
        text = validate_definition(name, definition)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS stack_definitions (name TEXT PRIMARY KEY, definition TEXT NOT NULL, revision INTEGER NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT revision FROM stack_definitions WHERE name=?', (name,)).fetchone()
            if (row[0] if row else None) != expected_revision:
                raise ValueError('Definition changed or already exists; reload and review again')
            revision = row[0] + 1 if row else 1
            db.execute('INSERT INTO stack_definitions VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET definition=excluded.definition, revision=excluded.revision', (name, text, revision))
        self.path.chmod(0o600)
        return {'name': name, 'revision': revision}

    def replica_counts(self, endpoint):
        if not self.path.exists():
            return {}
        with sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='replica_counts'").fetchone():
                return {}
            return dict(db.execute('SELECT service, replicas FROM replica_counts WHERE endpoint=?', (endpoint,)))

    def remember_replicas(self, endpoint, counts):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS replica_counts (endpoint TEXT, service TEXT, replicas INTEGER NOT NULL, PRIMARY KEY(endpoint, service))')
            for service, replicas in counts.items():
                if replicas > 0:
                    db.execute('INSERT INTO replica_counts VALUES (?, ?, ?) ON CONFLICT(endpoint, service) DO UPDATE SET replicas=excluded.replicas', (endpoint, service, replicas))
        self.path.chmod(0o600)

    def running_definition(self, endpoint, name):
        if not self.path.exists():
            return None
        with sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stack_running_definitions'").fetchone():
                return None
            row = db.execute('SELECT definition FROM stack_running_definitions WHERE endpoint=? AND name=?', (endpoint, name)).fetchone()
        value = json.loads(row[0]) if row else None
        return value.get('compose_definition', value) if isinstance(value, dict) else value

    def running_origins(self, endpoint):
        if not self.path.exists():
            return {}
        with sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stack_running_definitions'").fetchone():
                return {}
            rows = db.execute('SELECT name, definition FROM stack_running_definitions WHERE endpoint=?', (endpoint,)).fetchall()
        return {name: data['origin'] for name, text in rows if isinstance((data := json.loads(text)), dict) and data.get('origin')}

    def remember_running_definition(self, endpoint, name, definition, origin=None):
        validate_json_tree(definition)
        text = json.dumps({'compose_definition': definition, 'origin': origin}, ensure_ascii=False, allow_nan=False)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS stack_running_definitions (endpoint TEXT, name TEXT, definition TEXT NOT NULL, PRIMARY KEY(endpoint, name))')
            db.execute('INSERT INTO stack_running_definitions VALUES (?, ?, ?) ON CONFLICT(endpoint, name) DO UPDATE SET definition=excluded.definition', (endpoint, name, text))
        self.path.chmod(0o600)

    def definition_tree(self, endpoint, name):
        if not self.path.exists():
            return None
        with sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stack_management'").fetchone():
                return None
            row = db.execute('SELECT tree FROM stack_management WHERE name=?', (name,)).fetchone()
        return json.loads(row[0]) if row else None

    def definition_tree_origins(self, endpoint):
        if not self.path.exists():
            return {}
        with sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stack_management'").fetchone():
                return {}
            rows = db.execute('SELECT name, tree FROM stack_management').fetchall()
        result = {}
        for name, text in rows:
            tree = json.loads(text)
            source = next((node for node in tree['nodes'] if node['id'] == tree['source']), None)
            if source and source.get('origin'):
                result[name] = source['origin']
        return result

    def save_definition_tree(self, endpoint, name, tree, expected_revision):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS stack_management (name TEXT PRIMARY KEY, tree TEXT NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT tree FROM stack_management WHERE name=?', (name,)).fetchone()
            revision = json.loads(row[0])['revision'] if row else 0
            if revision != expected_revision:
                raise ValueError('Definition hierarchy changed; reload and review again')
            result = {**tree, 'revision': revision + 1}
            text = json.dumps(result, ensure_ascii=False, allow_nan=False)
            if len(text.encode()) > 16 * 1024 * 1024 or len(tree['nodes']) > 128:
                raise ValueError('Definition history exceeds 128 nodes or 16 MiB; delete unused definitions')
            db.execute('INSERT INTO stack_management VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET tree=excluded.tree', (name, text))
        self.path.chmod(0o600)
        return result
