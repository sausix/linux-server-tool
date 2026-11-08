"""Secret-free findings for read-only security configuration assessments."""
import stat
from pathlib import Path

STATUSES = {'pass', 'warning', 'critical', 'unknown', 'info'}


def finding(key, title, status, observation, recommendation):
    if status not in STATUSES:
        raise ValueError('Invalid assessment status')
    return dict(id=key, title=title, status=status, observation=observation, recommendation=recommendation)


def private_file(key, title, path):
    try:
        mode = Path(path).stat().st_mode
    except FileNotFoundError:
        return finding(key, title, 'unknown', 'The file does not exist in this process filesystem.',
                       'Check the configured location and protect its directory, journals and backups.')
    except OSError:
        return finding(key, title, 'unknown', 'File permissions could not be inspected.',
                       'Verify permissions using the service account and the host filesystem.')
    private = stat.S_ISREG(mode) and not mode & 0o077
    return finding(key, title, 'pass' if private else 'warning', f'Observed file mode: {stat.S_IMODE(mode):04o}.',
                   'Use a dedicated service identity, a private data directory and mode 0600 for sensitive files. Parent permissions, ACLs, journals and backups need separate review.')
