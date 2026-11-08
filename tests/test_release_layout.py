"""Public release files exclude local state and use one shared runtime image."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_gitignore_keeps_local_deployments_and_secrets_out_of_first_add(tmp_path):
    repository = tmp_path/'index.git'
    subprocess.run(['git', 'init', '--bare', '-q', str(repository)], check=True)
    git = ['git', '--git-dir='+str(repository), '--work-tree='+str(ROOT)]
    ignored = ['.env', '.env.production', 'compose.yaml', 'main.py', 'MAINTAINER.md',
        'data/users.db', 'backend-run/daemon.sock', 'testserver/private.yaml',
        '.ruff_cache/state', 'backend/.ruff_cache/state', 'tls/server.key',
        'tls/server.pem', 'runtime.sqlite3-wal']
    public = ['README.md', 'docker/Dockerfile', '.dockerignore', 'uv.lock', '.python-version',
        'docs/deployment/compose.all.yaml', 'docs/deployment/compose.env.example',
        'backend/systemd/backend.env.example', 'frontend/src/frontend/app.py',
        'backend/src/backend/daemon.py', 'docker/entrypoint.sh']
    result = subprocess.run(git+['check-ignore', '--no-index', '--stdin'],
        input='\n'.join(ignored+public)+'\n', cwd=ROOT, check=True, capture_output=True, text=True)
    assert set(result.stdout.splitlines()) == set(ignored)


def test_service_is_canonical_in_its_component():
    canonical = ROOT/'backend/systemd/backend.service'
    assert canonical.is_file() and not canonical.is_symlink()
    assert not (ROOT/'docs/examples').exists()
    text = canonical.read_text()
    assert 'ExecStart=/opt/lsdfm/backend/.venv/bin/backend\n' in text
    assert 'WorkingDirectory=/var/lib/lsdfm/backend\n' in text
    assert 'UMask=0077' in text
    assert '/usr/bin/python3 /lsdfm/' not in text


def test_shared_image_installs_locked_runtime_and_excludes_private_context():
    assert not (ROOT/'Dockerfile').exists()
    assert not (ROOT/'backend/Dockerfile').exists()
    dockerfile = (ROOT/'docker/Dockerfile').read_text()
    assert 'uv sync --locked --all-packages --no-dev --no-editable' in dockerfile
    assert 'COPY frontend/src' in dockerfile and 'COPY backend/src' in dockerfile
    assert 'USER frontend' in dockerfile and 'CMD ["frontend"]' in dockerfile
    assert 'COPY . ' not in dockerfile
    assert 'COPY docker/entrypoint.sh ' in dockerfile
    ignore = (ROOT/'.dockerignore').read_text().splitlines()
    assert ignore[0] == '**'
    assert '!common/src/**' in ignore and '!frontend/src/**' in ignore and '!backend/src/**' in ignore
    assert '**/.env' in ignore and '**/*.key' in ignore
    assert 'umask 0077' in (ROOT/'docker/entrypoint.sh').read_text()


def test_root_readme_is_public_overview():
    text = (ROOT/'README.md').read_text()
    from common.identity import APP_NAME
    assert text.startswith('# ' + APP_NAME + '\n')
    assert '(docs/README.md)' in text and '(LICENSE)' in text
    assert 'maintainer notes' not in text.lower()
    assert '/media/ssd/' not in text


def test_runtime_paths_use_stable_application_namespace():
    from common.identity import STORAGE_ID
    from backend.daemon_config import DaemonSettings
    from frontend.config import Settings

    assert DaemonSettings.model_fields['unix_socket'].default == Path(f'/run/{STORAGE_ID}/backend.sock')
    assert Settings.model_fields['daemon_endpoint'].default == f'unix:///run/{STORAGE_ID}/backend.sock'
    assert DaemonSettings.model_fields['stack_database'].default == Path(f'/var/lib/{STORAGE_ID}/backend/stacks.sqlite3')
    assert DaemonSettings.model_fields['compose_root'].default == Path(f'/var/lib/{STORAGE_ID}/compose')
    service = (ROOT/'backend/systemd/backend.service').read_text()
    assert f'RuntimeDirectory={STORAGE_ID}\n' in service
    assert f'StateDirectory={STORAGE_ID} {STORAGE_ID}/backend {STORAGE_ID}/compose\n' in service
    files = [ROOT/'docker/Dockerfile', ROOT/'backend/systemd/backend.service',
             ROOT/'backend/systemd/backend.env.example', *list((ROOT/'docs/deployment').glob('compose.*'))]
    for file in files:
        text = file.read_text()
        for generic in ('/run/backend', '/var/lib/backend', '/backend/state'):
            assert generic not in text, file
