#!/usr/bin/env python3
"""Manage a coordinated release while keeping each package independently buildable."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:(?:a|b|rc)\d+)?(?:\.post\d+)?(?:\.dev\d+)?')


def read(path):
    return tomllib.loads(path.read_text(encoding='utf-8'))


def valid_version(value):
    if not VERSION.fullmatch(value):
        raise ValueError('Use MAJOR.MINOR.PATCH, optionally with a canonical prerelease, post or dev suffix')
    return value


def release_version(root):
    return valid_version(read(root/'pyproject.toml')['project']['version'])


def projects(root):
    """Read names from metadata; no distribution name is hardcoded here."""
    result = {root/'pyproject.toml': read(root/'pyproject.toml')['project']}
    config = read(root/'pyproject.toml')['tool']['uv']['workspace']
    excluded = {p.resolve() for pattern in config.get('exclude', []) for p in root.glob(pattern)}
    for pattern in config['members']:
        for folder in sorted(root.glob(pattern)):
            path = folder/'pyproject.toml'
            if folder.resolve() in excluded:
                continue
            if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('Workspace members must have metadata inside the repository')
            result[path] = read(path)['project']
    names = [project['name'] for project in result.values()]
    if len(names) != len(set(names)):
        raise ValueError('Workspace distribution names must be unique')
    return result


def bump_version(value, part):
    valid_version(value)
    numbers = list(map(int, VERSION.fullmatch(value).groups()))
    index = {'major': 0, 'minor': 1, 'patch': 2}[part]
    numbers[index] += 1
    for offset in range(index+1, 3):
        numbers[offset] = 0
    return '.'.join(map(str, numbers))


def consistency_errors(root):
    expected, metadata = release_version(root), projects(root)
    names = {project['name'] for project in metadata.values()}
    errors = []
    for path, project in metadata.items():
        if project['version'] != expected:
            errors.append(f'{path.relative_to(root)}: version differs from the root release')
        groups = [project.get('dependencies', [])]
        groups.extend(project.get('optional-dependencies', {}).values())
        for dependency in (item for group in groups for item in group):
            match = re.match(r'^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?', dependency)
            if match and match[1] in names:
                suffix = dependency[match.end():].split(';', 1)[0].strip()
                if suffix != '==' + expected:
                    errors.append(f'{path.relative_to(root)}: local dependency {match[1]} must pin the release version')
    lockfile = root/'uv.lock'
    if not lockfile.exists():
        errors.append('uv.lock is missing; run uv lock')
    else:
        locked = {item['name']: item['version'] for item in read(lockfile)['package']}
        for name in names:
            if locked.get(name) != expected:
                errors.append(f'uv.lock: {name} differs from the root release')
    return errors


def updated_metadata(text, target, names):
    lines, section, count = [], '', 0
    for line in text.splitlines(keepends=True):
        header = re.match(r'^\[([^]]+)\]\s*$', line.strip())
        if header:
            section = header[1]
        if section == 'project':
            match = re.match(r'^(\s*version\s*=\s*)([\'"])([^\'"]+)([\'"])(.*?)(\r?\n)?$', line)
            if match:
                line = match[1] + match[2] + target + match[4] + match[5] + (match[6] or '')
                count += 1
        lines.append(line)
    if count != 1:
        raise ValueError('Each project must declare exactly one static version')
    text = ''.join(lines)
    choices = '|'.join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    pattern = re.compile(r'(?P<quote>[\'"])(?P<name>(?:' + choices + r')(?:\[[^]]+\])?)==[^\'";]+(?P<marker>\s*;[^\'"]+)?(?P=quote)')
    output, section, depth = [], '', 0
    for line in text.splitlines(keepends=True):
        header = re.match(r'^\[([^]]+)\]\s*$', line.strip())
        if header:
            section, depth = header[1], 0
        dependencies = section == 'project' and (depth > 0 or re.match(r'^\s*dependencies\s*=', line))
        if dependencies or section in {'project.optional-dependencies', 'dependency-groups'}:
            line = pattern.sub(lambda match: match['quote'] + match['name'] + '==' + target + (match['marker'] or '') + match['quote'], line)
        if dependencies:
            # Brackets in quoted extras/markers are not TOML array delimiters.
            structural = re.sub(r'"(?:\\.|[^"\\])*"|\'[^\']*\'|#.*$', '', line)
            depth += structural.count('[') - structural.count(']')
        output.append(line)
    return ''.join(output)


def atomic_write(path, content):
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    handle, temporary = tempfile.mkstemp(prefix='.release-version-', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8', newline='') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def release_lock(root):
    path = root/'.version-bump.lock'
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError('A version update lock exists; check for an active updater before removing a stale lock') from None
    try:
        with os.fdopen(handle, 'w') as stream:
            stream.write(str(os.getpid()) + '\n')
        yield
    finally:
        path.unlink(missing_ok=True)


def apply_version(root, target, *, dry_run=False, offline=False, runner=subprocess.run):
    valid_version(target)
    with release_lock(root):
        metadata = projects(root)
        names = {project['name'] for project in metadata.values()}
        replacements = {path: updated_metadata(path.read_text(encoding='utf-8'), target, names) for path in metadata}
        if dry_run:
            return
        lockfile = root/'uv.lock'
        previous = {path: path.read_text(encoding='utf-8') for path in metadata}
        old_lock = lockfile.read_text(encoding='utf-8') if lockfile.exists() else None
        try:
            for path, text in replacements.items():
                atomic_write(path, text)
            command = ['uv', 'lock'] + (['--offline'] if offline else [])
            outcome = runner(command, cwd=root, capture_output=True, text=True)
            if outcome.returncode:
                raise ValueError('uv lock failed; metadata and lockfile were restored. Run uv lock separately to inspect the dependency error.')
            errors = consistency_errors(root)
            if errors:
                raise ValueError('Generated release metadata is inconsistent: ' + '; '.join(errors))
        except BaseException:
            for path, text in previous.items():
                atomic_write(path, text)
            if old_lock is None:
                lockfile.unlink(missing_ok=True)
            else:
                atomic_write(lockfile, old_lock)
            raise


def main(argv=None, *, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('show', help='Print the canonical root release version')
    commands.add_parser('check', help='Check package versions, local pins and uv.lock')
    bump = commands.add_parser('bump', help='Increment all release metadata together')
    bump.add_argument('part', choices=('major', 'minor', 'patch'))
    set_version = commands.add_parser('set', help='Set an explicit release, e.g. 0.2.0.dev1')
    set_version.add_argument('version', type=valid_version)
    for command in (bump, set_version):
        command.add_argument('--dry-run', action='store_true', help='Validate without changing release files')
        command.add_argument('--offline', action='store_true', help='Resolve the lockfile from cached metadata only')
    args = parser.parse_args(argv)
    try:
        current = release_version(root)
        if args.command == 'show':
            print(current)
        elif args.command == 'check':
            errors = consistency_errors(root)
            if errors:
                raise ValueError('\n'.join(errors))
            print(f'Release {current}: package metadata, local pins and lockfile agree.')
        else:
            target = bump_version(current, args.part) if args.command == 'bump' else args.version
            apply_version(root, target, dry_run=args.dry_run, offline=args.offline)
            print(f'{current} -> {target}' + (' (dry run; release files unchanged)' if args.dry_run else '\nRun uv sync --locked --all-packages to update the root development environment.'))
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
