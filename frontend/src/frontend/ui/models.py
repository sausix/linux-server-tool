"""Indexed UI extensions. Providers can replace or remove only their own entries."""
from copy import deepcopy
import re
from urllib.parse import urlsplit


def color(value):
    if not isinstance(value, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', value):
        raise ValueError('An accent must be an RGB hex color')
    return value


def badge(text, accentcolor, href=None, icon=None):
    value = {'text': str(text), 'accentcolor': color(accentcolor)}
    if href is not None:
        target = urlsplit(href)
        if not href.startswith('/') or href.startswith('//') or target.scheme or target.netloc or '\\' in href or any(ord(char) < 32 for char in href):
            raise ValueError('UI links must be local absolute paths')
        value['href'] = href
    if icon is not None:
        if not re.fullmatch(r'[a-z][a-z0-9-]*', icon):
            raise ValueError('Invalid icon identifier')
        value['icon'] = icon
    return value


class UIModels:
    def __init__(self):
        self.providers = {}
        self.index = {}

    def replace(self, provider, entries, address_accents=()):
        """Atomically replace a provider snapshot; absence removes stale overrides."""
        prepared = deepcopy(entries)
        for entry in prepared.values():
            if 'accentcolor' in entry:
                color(entry['accentcolor'])
            for item in entry.get('links', []) + entry.get('badges', []):
                badge(item['text'], item['accentcolor'], item.get('href'), item.get('icon'))
            for column in entry.get('columns', {}).values():
                for item in column['items']:
                    badge(item['text'], item['accentcolor'], item.get('href'), item.get('icon'))
        addresses = deepcopy(list(address_accents))
        for item in addresses:
            color(item['accentcolor'])
        self.providers[provider] = (prepared, addresses)
        self._rebuild()

    def clear(self, provider):
        self.providers.pop(provider, None)
        self._rebuild()

    def _rebuild(self):
        self.index = {}
        for entries, _ in self.providers.values():
            for name, patch in entries.items():
                target = self.index.setdefault(name, {'links': [], 'badges': [], 'columns': {}})
                if 'accentcolor' in patch:
                    target['accentcolor'] = patch['accentcolor']
                target['links'].extend(patch.get('links', []))
                target['badges'].extend(patch.get('badges', []))
                target['columns'].update(patch.get('columns', {}))

    def interface(self, name):
        return deepcopy(self.index.get(name, {}))

    def snapshot(self):
        return {'interfaces': deepcopy(self.index), 'address_accents': [deepcopy(item)
                for _, addresses in self.providers.values() for item in addresses]}
