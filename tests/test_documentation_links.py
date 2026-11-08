"""Component documentation and canonical deployment assets stay linked."""
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def test_operator_documentation_has_component_folders_without_duplicate_examples():
    docs = ROOT/'docs'
    assert {path.name for path in docs.iterdir() if path.is_file()} == {'README.md'}
    assert {path.name for path in docs.iterdir() if path.is_dir()} == {'common', 'backend', 'deployment', 'docker', 'network', 'security', 'system', 'frontend'}
    assert not (docs/'examples').exists()
    assert not (docs/'compose').exists()
    assert (ROOT/'backend/systemd/backend.service').is_file()
    assert (ROOT/'backend/systemd/backend.env.example').is_file()
    assert not (ROOT/'backend/systemd/backend.service').is_symlink()


def test_relative_markdown_links_resolve_within_repository():
    broken = []
    for document in [ROOT/'README.md', *(ROOT/'docs').rglob('*.md')]:
        for target in re.findall(r'\[[^\]]*\]\(([^\s)]+)\)', document.read_text()):
            url = urlsplit(target)
            if url.scheme or url.netloc or not url.path:
                continue
            destination = (document.parent/unquote(url.path)).resolve()
            if not destination.is_relative_to(ROOT) or not destination.exists():
                broken.append((str(document.relative_to(ROOT)), target))
    assert not broken, broken
