"""Explain runtime configuration candidates without asserting an unaudited writer."""

FORWARDING_KEYS = ('net.ipv4.ip_forward', 'net.ipv6.conf.all.forwarding')


def needs_docker_source(items):
    values = {item['key']: item for item in items}
    return all(values.get(key, {}).get('value') == '1' for key in FORWARDING_KEYS) and any(
        not values[key].get('sources') for key in FORWARDING_KEYS)


def annotate_docker_source(items):
    if not needs_docker_source(items):
        return
    for item in items:
        if item['key'] in FORWARDING_KEYS and not item.get('sources'):
            item['configuration_sources'] = [{'name': 'Docker daemon', 'kind': 'runtime', 'confidence': 'inferred',
                'description': 'Both global forwarding settings are enabled and no persistent declaration was found for this setting. A reachable local Docker daemon commonly enables forwarding at startup; the original writer was not audited.',
                'documentation': 'https://docs.docker.com/engine/network/packet-filtering-firewalls/'}]
