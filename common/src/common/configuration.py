"""Project-independent environment selection with isolated custom prefixes."""
import os
from pathlib import Path
from typing import ClassVar
import re
from dotenv import dotenv_values
from pydantic import PrivateAttr
from pydantic_settings import BaseSettings, EnvSettingsSource, DotEnvSettingsSource, SettingsConfigDict


class ComponentSettings(BaseSettings):
    # Complete reference files represent optional values as empty assignments.
    model_config = SettingsConfigDict(env_ignore_empty=True)
    prefix_selector: ClassVar[str]
    legacy_prefix: ClassVar[str] = ''
    _effective_prefix: str = PrivateAttr(default='')

    def __init__(self, _env_prefix=None, _env_file='.env', **values):
        file_values = {}
        paths = [_env_file] if isinstance(_env_file, (str, Path)) else (_env_file or [])
        for path in paths:
            file_values.update(dotenv_values(path, encoding='utf-8'))
        prefix = (_env_prefix if _env_prefix is not None else os.environ.get(self.prefix_selector,
                  file_values.get(self.prefix_selector, self.model_config.get('env_prefix', ''))))
        if not isinstance(prefix, str) or (prefix and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*_', prefix)):
            raise ValueError('Environment prefixes must be empty or an identifier ending in underscore')
        super().__init__(_env_prefix=prefix, _env_file=_env_file, **values)
        self._effective_prefix = prefix

    @property
    def env_prefix(self):
        return self._effective_prefix

    def env_name(self, field):
        return field.upper() if field.endswith('_support') else self.env_prefix + field.upper()

    def format_env_references(self, text):
        default = self.model_config.get('env_prefix', '')
        return re.sub(re.escape(default) + r'([A-Z][A-Z0-9_]*)',
                      lambda match: self.env_name(match.group(1).lower()), text) if default else text

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        # An explicit empty standard value selects its default and must not
        # resurrect a credential from a legacy namespace or a lower source.
        raw = {}
        paths = dotenv_settings.env_file
        paths = [paths] if isinstance(paths, (str, Path)) else (paths or [])
        for path in paths:
            raw.update({key.lower(): value for key, value in dotenv_values(path).items()})
        raw.update({key.lower(): value for key, value in os.environ.items()})
        empty_defaults = {}
        for name, field in settings_cls.model_fields.items():
            alias = field.validation_alias if isinstance(field.validation_alias, str) else None
            key = alias or env_settings.env_prefix + name
            if raw.get(key.lower()) == '':
                empty_defaults[alias or name] = field.default
        sources = (init_settings, lambda: empty_defaults, env_settings, dotenv_settings)
        # Custom namespaces never fall back to another application's configuration.
        if cls.legacy_prefix and env_settings.env_prefix == cls.model_config.get('env_prefix'):
            sources += (EnvSettingsSource(settings_cls, env_prefix=cls.legacy_prefix),
                        DotEnvSettingsSource(settings_cls, env_prefix=cls.legacy_prefix,
                            env_file=dotenv_settings.env_file, env_file_encoding=dotenv_settings.env_file_encoding))
        return (*sources, file_secret_settings)
