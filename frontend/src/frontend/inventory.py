"""Search models and URL filters for inventory categories."""

import ipaddress
import json
import shlex
import re

from pydantic import BaseModel, Field
from common.ip_classification import classify_ip
from common.interface_scope import interface_scope
from frontend.network.firewall import nft_rule, legacy_rule

from frontend.network.models import InterfaceEntry, RouteEntry, FirewallEntry
from frontend.system.models import OptionEntry

MODELS = {'interface': InterfaceEntry, 'route': RouteEntry, 'option': OptionEntry, 'sysctl': OptionEntry, 'firewall': FirewallEntry}
FIELDS = set()


def register_models(models):
    MODELS.update(models)
    FIELDS.update(f'{prefix}.{field}' for prefix, model in MODELS.items() for field in model.model_fields)
    FIELDS.update({'class', 'sysctl.class', 'option.class', 'prefix'})


register_models({})


def parse_filters(parameters):
    filters, words = [], []
    for key, value in parameters:
        if not value and key != 'label.value':
            continue
        if key == 'q':
            for token in shlex.split(value):
                if '=' in token:
                    name, expected = token.split('=', 1)
                    if name not in FIELDS:
                        raise ValueError(f'Unknown filter: {name}')
                    filters.append((name, expected))
                else:
                    words.append(token.casefold())
        elif key in FIELDS:
            filters.append((key, value))
        else:
            raise ValueError(f'Unknown filter: {key}')
    return filters, words


def address_matches(field, actual, expected):
    """Compare normalized subnets, or test whether a host belongs to one."""
    try:
        network = ipaddress.ip_interface(actual).network
        if field.endswith('.subnet'):
            return '/' in expected and network == ipaddress.ip_network(expected, strict=False)
        return ipaddress.ip_interface(expected).ip in network
    except ValueError:
        return False


def matches(models, filters, words, highlights=None):
    values = {f'{prefix}.{key}': value for prefix, model in models.items()
              for key, value in model.model_dump().items()}
    for key, expected in filters:
        key = {'class': 'sysctl.classes', 'sysctl.class': 'sysctl.classes',
               'option.class': 'option.classes', 'prefix': 'sysctl.prefix'}.get(key, key)
        actual = values.get(key, [])
        actual = actual if isinstance(actual, list) else [actual]
        if key in {'sysctl.prefix', 'option.prefix'}:
            if not any(str(value) == expected.rstrip('.') or str(value).startswith(expected.rstrip('.') + '.') for value in actual):
                return False
        elif key in {'interface.ip', 'interface.ip4', 'interface.ip6', 'interface.subnet', 'route.subnet'}:
            if not any(address_matches(key, value, expected) for value in actual):
                return False
        elif not any(str(value).casefold() == expected.casefold() for value in actual):
            return False
    text = json.dumps(list(values.values()), ensure_ascii=False).casefold()
    qualified = all(word in text for word in words)
    if qualified and highlights is not None:
        aliases = {'class': 'sysctl.classes', 'sysctl.class': 'sysctl.classes',
                   'option.class': 'option.classes', 'prefix': 'sysctl.prefix'}
        for field, value in values.items():
            items = value if isinstance(value, list) else [value]
            matched = []
            for item in items:
                def field_match(key, expected):
                    if key == 'interface.subnet' and field in {'interface.ip', 'interface.ip4', 'interface.ip6'}:
                        return address_matches(key, item, expected)
                    if aliases.get(key, key) != field:
                        return False
                    if field in {'interface.ip', 'interface.ip4', 'interface.ip6', 'interface.subnet', 'route.subnet'}:
                        return address_matches(field, item, expected)
                    if field.endswith('.prefix'):
                        return str(item) == expected.rstrip('.') or str(item).startswith(expected.rstrip('.') + '.')
                    return str(item).casefold() == expected.casefold()
                direct = any(field_match(key, expected) for key, expected in filters)
                if direct or any(word in str(item).casefold() for word in words):
                    matched.append(str(item))
            if matched:
                highlights[field] = matched
    return qualified

def matches_resource(prefix, model, labels, filters, words, highlights):
    """Keep key and value conditions bound to the same resource label."""
    if any(key.startswith('label.') for key, _ in filters):
        return any(matches({prefix: model, 'label': MODELS['label'](key=key, value=value)}, filters, words, highlights)
                   for key, value in (labels or {}).items())
    return matches({prefix: model}, filters, words, highlights)


def interface_models(data):
    result = {}
    for link in data.get('interfaces', []):
        name = link['name']
        addresses = [item for item in data.get('addresses', []) if item.get('interface') == name and item.get('address')]
        ips = [f"{item['address']}/{item['prefix_length']}" if item.get('prefix_length') is not None else item['address'] for item in addresses]
        scope = interface_scope(ips, link.get('flags', []))
        ips += [value for value in data.get('configuration', {}).get(name, {}).get('addresses', []) if value not in ips]
        result[name] = InterfaceEntry(name=name,
            scope=scope,
            type=link.get('kind') or ('loopback' if 'loopback' in link.get('flags', []) else 'Ethernet' if link.get('type') == 1 else str(link.get('type', ''))),
            mac=link.get('mac_address') or '', ip=ips,
            ip4=[value for value in ips if ':' not in value],
            ip6=[value for value in ips if ':' in value],
            subnet=sorted({str(ipaddress.ip_interface(value).network) for value in ips}), flag=link.get('flags', []),
            state=link.get('operational_state') or '')
        result[name].address_type = sorted({classify_ip(value)['type'] for value in ips})
    return result


def firewall_entries(data):
    entries = []
    for item in data.get('ruleset', {}).get('nftables', []):
        kind = next((name for name in ('table', 'chain', 'rule') if name in item), None)
        if not kind:
            continue
        value = item[kind]
        normalized = nft_rule(value) if kind == 'rule' else {}
        entries.append((item, FirewallEntry(backend='nftables', family=value.get('family', ''),
            table=value.get('name', '') if kind == 'table' else value.get('table', ''),
            chain=value.get('name', '') if kind == 'chain' else value.get('chain', ''),
            policy=value.get('policy', ''), rule=json.dumps(value.get('expr', ''), ensure_ascii=False),
            expression=normalized.get('expression', ''), action=normalized.get('action', ''),
            target=normalized.get('targets', []), interface=[item['name'] for item in normalized.get('interfaces', [])])))
    return entries


def filter_inventory(view, data, parameters, interfaces=None):
    if view == 'interfaces':
        data = {**data, 'address_types': {}}
        scopes = interface_models(data)
        data['interfaces'] = [{**link, 'scope': scopes[link['name']].scope} for link in data.get('interfaces', [])]
        for link in data['interfaces']:
            if 'ports' in link:
                link['ports'] = [{**port, 'scope': scopes[port['name']].scope if port['name'] in scopes else 'unknown'} for port in link['ports']]
        values = [item['address'] for item in data.get('addresses', [])]
        values += [value for config in data.get('configuration', {}).values() for value in config.get('addresses', [])]
        for value in values:
            try:
                data['address_types'][value.split('/')[0]] = {**classify_ip(value), 'public': interface_scope([value]) == 'public'}
            except ValueError:
                pass
    filters, words = parse_filters(parameters)
    if not filters and not words:
        return data
    data = dict(data)
    links = interface_models(data if view == 'interfaces' else interfaces or {})
    if view == 'interfaces':
        hits = {}
        for name, model in links.items():
            highlights = {}
            if matches({'interface': model}, filters, words, highlights):
                hits[name] = highlights
        names = set(hits)
        data['interfaces'] = [{**item, '_matches': hits[item['name']]} for item in data.get('interfaces', []) if item['name'] in names]
        data['addresses'] = [item for item in data.get('addresses', []) if item['interface'] in names]
    elif view in {'routing', 'sysctl'}:
        for collection, prefix, model in [('routes', 'route', RouteEntry), ('options', 'option', OptionEntry)]:
            found = []
            for item in data.get(collection, []):
                values = {key: str(item.get(key) if item.get(key) is not None else '') for key in model.model_fields if key not in {'source', 'classes'}}
                if prefix == 'route' and item.get('destination') not in {None, 'default'} and item.get('prefix_length') is not None:
                    values['subnet'] = str(ipaddress.ip_network(f"{item['destination']}/{item['prefix_length']}", strict=False))
                if prefix == 'option':
                    values['source'] = [source['file'] for source in item.get('sources', [])]
                    values['source'] += [source['name'] for source in item.get('configuration_sources', [])]
                    values['classes'] = item.get('classes', [])
                entry = model(**values)
                context = {prefix: entry}
                if prefix == 'option':
                    context['sysctl'] = entry
                if item.get('interface') in links:
                    context['interface'] = links[item['interface']]
                elif view == 'sysctl' and item.get('interface'):
                    context['interface'] = InterfaceEntry(name=item['interface'])
                highlights = {}
                if matches(context, filters, words, highlights):
                    found.append({**item, '_matches': highlights})
            data[collection] = found
    else:
        if data.get('ruleset') and (data.get('ipv4_rules') or data.get('ipv6_rules')):
            nft = filter_inventory(view, {key: value for key, value in data.items() if key not in {'ipv4_rules', 'ipv6_rules'}}, parameters)
            legacy = filter_inventory(view, {key: value for key, value in data.items() if key != 'ruleset'}, parameters)
            return {**nft, **{key: legacy[key] for key in ('ipv4_rules', 'ipv6_rules', '_legacy_matches') if key in legacy}}
        if data.get('ruleset'):
            entries = firewall_entries(data)
            selected = [(raw, model) for raw, model in entries if matches({'firewall': model}, filters, words)]
            tables = {(model.family, model.table) for _, model in selected}
            chains = {(model.family, model.table, model.chain) for _, model in selected}
            retained = []
            for raw, model in entries:
                if (any(raw is selected_raw for selected_raw, _ in selected)
                    or ('table' in raw and (model.family, model.table) in tables)
                    or ('chain' in raw and (model.family, model.table, model.chain) in chains)):
                    highlights = {}
                    matches({'firewall': model}, filters, words, highlights)
                    kind = next(iter(raw))
                    retained.append({kind: {**raw[kind], '_matches': highlights}})
            data['ruleset'] = {'nftables': retained}
        else:
            data['_legacy_matches'] = {}
            for family, field in [('ip', 'ipv4_rules'), ('ip6', 'ipv6_rules')]:
                output, table, headers, rules = [], '', [], []
                def flush():
                    if rules:
                        names = {legacy_rule(line)['chain'] for line in rules if re.match(r'^(?:\[\d+:\d+\]\s*)?-A\s', line)}
                        names.update(line.split(': ', 1)[1] for line in rules if line.startswith('# Matching chain: '))
                        selected_headers = [line for line in headers if line[1:].split()[0] in names]
                        output.extend(['*' + table, *selected_headers, *rules, 'COMMIT'])
                for line in (data.get(field) or '').splitlines():
                    if line.startswith('*'):
                        flush()
                        table, headers, rules = line[1:], [], []
                    elif line.startswith(':'):
                        name, policy, *_ = line[1:].split()
                        backend = next((name for name in data.get('backends', []) if name != 'nftables'), data.get('backend', 'iptables'))
                        model = FirewallEntry(backend=backend, family=family, table=table, chain=name, policy=policy)
                        headers.append(line)
                        highlights = {}
                        if matches({'firewall': model}, filters, words, highlights):
                            data['_legacy_matches'][family + ':' + table + ':' + name] = highlights
                            rules.append('# Matching chain: ' + name)
                    elif re.match(r'^(?:\[\d+:\d+\]\s*)?-A\s', line):
                        normalized = legacy_rule(line)
                        backend = next((name for name in data.get('backends', []) if name != 'nftables'), data.get('backend', 'iptables'))
                        model = FirewallEntry(backend=backend, family=family, table=table, chain=normalized['chain'], rule=line,
                            expression=normalized['expression'], action=normalized['action'],
                            target=normalized['targets'], interface=[item['name'] for item in normalized['interfaces']])
                        highlights = {}
                        if matches({'firewall': model}, filters, words, highlights):
                            data['_legacy_matches'][family + ':' + table + ':' + line] = highlights
                            rules.append(line)
                flush()
                data[field] = '\n'.join(output)
    return data
