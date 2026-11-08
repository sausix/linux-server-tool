"""Resolve runtime versions from installed distribution metadata."""
from functools import cache
from importlib.metadata import PackageNotFoundError, distributions, packages_distributions, version
from importlib.util import find_spec
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit


@cache
def package_version(package):
    """Find the distribution owning a top-level import package."""
    name = package.split('.', 1)[0]
    owners = packages_distributions().get(name, [])
    if not owners:
        # Editable Hatch installations may omit the top-level package mapping.
        # PEP 610 metadata still identifies their installed source directory.
        origin = Path(find_spec(name).origin).resolve()
        for distribution in distributions():
            direct = json.loads(distribution.read_text('direct_url.json') or '{}')
            url = urlsplit(direct.get('url', ''))
            if url.scheme == 'file' and direct.get('dir_info', {}).get('editable'):
                source = Path(unquote(url.path))
                if any((source / prefix / name / '__init__.py').resolve() == origin for prefix in ('src', '.')):
                    owners.append(distribution.metadata['Name'])
    if not owners:
        raise PackageNotFoundError(name)
    if len(owners) != 1:
        raise RuntimeError(f"Multiple installed distributions provide {name!r}")
    return version(owners[0])
