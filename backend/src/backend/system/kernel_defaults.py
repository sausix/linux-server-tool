"""Resolve selected build defaults from local kernel configuration, never literals."""

import gzip
import json
import logging
import platform
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger('backend.defaults')

# These are semantic mappings, not default values. Missing keys stay unknown.
CONFIG_TO_SYSCTL = {
    'CONFIG_DEFAULT_TCP_CONG': 'net/ipv4/tcp_congestion_control',
    'CONFIG_DEFAULT_NET_SCH': 'net/core/default_qdisc',
}


def discover(paths=None, release=None):
    release = release or platform.release()
    if paths is None:
        paths = [Path('/proc/config.gz'), Path('/boot') / f'config-{release}',
                 Path('/lib/modules') / release / 'build/.config']
    for path in paths:
        path = Path(path)
        try:
            if path.suffix == '.gz':
                with gzip.open(path, 'rt', encoding='utf-8') as stream:
                    text = stream.read()
            else:
                text = path.read_text(encoding='utf-8')
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, EOFError) as exc:
            logger.warning('Cannot read kernel configuration %s: %s', path, exc)
            continue
        if not any(line.startswith('CONFIG_') and '=' in line for line in text.splitlines()):
            logger.warning('No kernel configuration assignments found in %s', path)
            continue
        defaults = {}
        for number, line in enumerate(text.splitlines(), 1):
            key, separator, raw = line.partition('=')
            if not separator or key not in CONFIG_TO_SYSCTL:
                continue
            try:
                value = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(value, str) or not value:
                continue
            defaults[CONFIG_TO_SYSCTL[key]] = {'value': value, 'file': str(path),
                'line': number, 'config_key': key, 'kernel_release': release,
                'kind': 'kernel-build-config'}
        logger.info('Kernel defaults: %s mapped values from %s (kernel %s)', len(defaults), path, release)
        return defaults
    logger.info('No readable local kernel configuration; kernel defaults remain unknown')
    return {}


@lru_cache(maxsize=1)
def local_defaults():
    """One scan per service process, shared by inventory and event monitoring."""
    return discover()
