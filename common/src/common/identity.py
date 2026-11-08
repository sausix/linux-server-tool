"""Display branding and stable compatibility identifiers from a package resource."""
from importlib.resources import files
import tomllib

_CONFIG = tomllib.loads(files(__package__).joinpath('application.toml').read_text(encoding='utf-8'))
APP_NAME = _CONFIG['name']
STORAGE_ID = _CONFIG['storage-id']
LEGACY_FRONTEND_PREFIX = _CONFIG['legacy-frontend-prefix']
LEGACY_BACKEND_PREFIX = _CONFIG['legacy-backend-prefix']
LEGACY_BACKEND_DIRECTORY = _CONFIG['legacy-backend-directory']
SESSION_COOKIE = _CONFIG['session-cookie']
SESSION_SALT = _CONFIG['session-salt']
SOURCE_KEY = 'x-' + STORAGE_ID + '-source'
MANIFEST_NAME = '.' + STORAGE_ID + '-stack.json'
DEFINITIONS_DIRECTORY = '.' + STORAGE_ID + '-definitions'
TEMP_PREFIX = '.' + STORAGE_ID + '-'

MANAGED_HEADER = '# Managed by ' + STORAGE_ID.upper()
