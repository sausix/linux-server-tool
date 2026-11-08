"""Conservative correlation of runtime links with persistent address declarations."""

import fnmatch
import ipaddress
from pathlib import Path


def declarations(links: list[dict], directory: Path, interfaces_file: Path) -> dict:
    result = {link['name']: {'sources': [], 'addresses': [], 'dynamic': False,
                             'complete': False} for link in links}
    for path in sorted(directory.glob('*.network')):
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        section = ''
        names, addresses = [], []
        dynamic = False
        uncertain = bool(list(Path(str(path) + '.d').glob('*.conf')))
        for line in lines:
            line = line.strip()
            if not line or line.startswith(('#', ';')):
                continue
            if line.startswith('['):
                section = line.strip('[]')
            elif '=' in line:
                key, value = (part.strip() for part in line.split('=', 1))
                if section == 'Match':
                    if key == 'Name':
                        names.extend(value.split())
                    else:
                        uncertain = True
                if key == 'Address' and section in {'Network', 'Address'}:
                    try:
                        addresses.append(str(ipaddress.ip_interface(value)))
                    except ValueError:
                        uncertain = True
                if key == 'DHCP' and value.lower() not in {'no', 'false', '0'}:
                    dynamic = True
        for name, item in result.items():
            if not item['sources'] and any(fnmatch.fnmatchcase(name, pat) for pat in names):
                item.update(sources=[str(path)], addresses=addresses,
                            dynamic=dynamic, complete=not uncertain)
    paths = [interfaces_file, *sorted((interfaces_file.parent / 'interfaces.d').glob('*'))]
    for path in paths:
        if not path.is_file():
            continue
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        current = None
        for line in lines:
            words = line.split('#', 1)[0].split()
            if not words:
                continue
            if words[0] == 'iface' and len(words) >= 4:
                current = result.get(words[1])
                if current is not None:
                    current['sources'].append(str(path))
                    current['dynamic'] |= words[3] in {'dhcp', 'auto'}
                    # Includes, hooks, and legacy netmask forms prevent a complete inference.
                    current['complete'] = False
            elif words[0] == 'address' and len(words) == 2 and current is not None:
                try:
                    if '/' in words[1]:
                        current['addresses'].append(str(ipaddress.ip_interface(words[1])))
                except ValueError:
                    pass
    return result
