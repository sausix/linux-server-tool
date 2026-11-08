"""Normalize nftables JSON and iptables-save into one read-only table model."""

import json
import re
import shlex
import hashlib


def expression(value):
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return str(value) if not isinstance(value, list) else '{ ' + ', '.join(expression(item) for item in value) + ' }'
    if 'payload' in value and isinstance(value['payload'], dict):
        payload = value['payload']
        if 'protocol' in payload and 'field' in payload:
            return f"{payload['protocol']} {payload['field']}"
    for kind in ('meta', 'ct'):
        if kind in value and isinstance(value[kind], dict) and 'key' in value[kind]:
            return f"{kind} {value[kind]['key']}"
    if 'prefix' in value and isinstance(value['prefix'], dict):
        prefix = value['prefix']
        if 'addr' in prefix and 'len' in prefix:
            return f"{prefix['addr']}/{prefix['len']}"
    if 'set' in value:
        return expression(value['set'])
    if 'range' in value and isinstance(value['range'], list):
        return '-'.join(expression(item) for item in value['range'])
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def nft_rule(rule, named_counters=None):
    matches, actions, targets, interfaces, counters = [], [], [], [], []
    compatibility_notes = []
    segments = []

    def match_parts(parts):
        if segments:
            segments.append({'text': ' and '})
        segments.extend(parts)

    def interface_parts(value):
        if isinstance(value, str):
            return [{'text': value, 'interface': value}]
        if isinstance(value, dict) and isinstance(value.get('set'), list):
            parts = [{'text': '{ '}]
            for index, item in enumerate(value['set']):
                if index:
                    parts.append({'text': ', '})
                parts.extend(interface_parts(item))
            return parts + [{'text': ' }'}]
        return [{'text': expression(value)}]
    for statement in rule.get('expr', []):
        if not isinstance(statement, dict):
            matches.append(expression(statement))
            match_parts([{'text': expression(statement)}])
            continue
        if 'match' in statement:
            match = statement['match']
            matches.append(f"{expression(match.get('left'))} {match.get('op', '==')} {expression(match.get('right'))}")
            left = match.get('left', {})
            if isinstance(left, dict) and left.get('meta', {}).get('key') in {'iifname', 'oifname'}:
                match_parts([{'text': f"{expression(left)} {match.get('op', '==')} "}] + interface_parts(match.get('right')))
                right = match.get('right')
                values = right.get('set', []) if isinstance(right, dict) else right
                for name in values if isinstance(values, list) else [values]:
                    if isinstance(name, str):
                        interfaces.append({'name': name, 'direction': left['meta']['key']})
            else:
                match_parts([{'text': matches[-1]}])
        elif 'xt' in statement and isinstance(statement['xt'], dict):
            extension = statement['xt']
            kind, name = extension.get('type'), extension.get('name')
            if kind not in {'match', 'target'} or not isinstance(name, str):
                actions.append(expression(statement))
                continue
            parameters = {key: value for key, value in extension.items() if key not in {'type', 'name'}}
            extra = (' ' + expression(parameters)) if parameters else ''
            if not parameters:
                compatibility_notes.append(f'xt {kind} {name}: nftables JSON exposes the extension name only; its parameters cannot be inferred.')
            if kind == 'match':
                text = f'xt match {name}{extra}'
                matches.append(text)
                match_parts([{'text': text}])
            else:
                actions.append(f'{name.lower()} (xt){extra}')
        elif 'counter' in statement:
            counter = statement['counter']
            if isinstance(counter, str):
                counter = (named_counters or {}).get((rule.get('family'), rule.get('table'), counter), {'name': counter})
            if isinstance(counter, dict):
                counters.append({key: str(counter[key]) if key in counter else None for key in ('packets', 'bytes', 'name')})
        elif any(key in statement for key in ('jump', 'goto')):
            key = 'jump' if 'jump' in statement else 'goto'
            body = statement[key]
            target = body.get('target') if isinstance(body, dict) else body
            actions.append(key)
            if isinstance(target, str):
                targets.append(target)
        elif any(key in statement for key in ('accept', 'drop', 'return', 'reject', 'continue', 'queue')):
            key = next(iter(statement))
            actions.append(key + (' ' + expression(statement[key]) if statement[key] is not None else ''))
        else:
            # NAT, maps, verdict maps and extension statements remain lossless text.
            actions.append(expression(statement))
    return {'expression': ' and '.join(matches) or 'any', 'action': '; '.join(actions) or '—',
            'expression_parts': segments or [{'text': 'any'}],
            'targets': targets, 'interfaces': interfaces, 'counters': counters,
            'compatibility_notes': compatibility_notes,
            'handle': str(rule.get('handle', '')), 'raw': json.dumps(rule, ensure_ascii=False, indent=2),
            '_matches': rule.get('_matches', {})}


def legacy_rule(line):
    raw = line
    counter = re.match(r'^\[(\d+):(\d+)\]\s*', line)
    counters = []
    if counter:
        counters.append({'packets': counter[1], 'bytes': counter[2]})
        line = line[counter.end():]
    try:
        tokens = shlex.split(line)
    except ValueError:
        return {'expression': line, 'action': '—', 'targets': [], 'interfaces': [],
                'counters': counters, 'raw': raw, 'chain': ''}
    chain, parts, action, targets, interfaces = '', [], '—', [], []
    interface_positions = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        value = tokens[index + 1] if index + 1 < len(tokens) else None
        if token in ('-A', '--append') and value:
            chain = value
            index += 2
        elif token in ('-j', '--jump', '-g', '--goto') and value:
            action = 'goto' if token in ('-g', '--goto') else 'jump'
            targets.append(value)
            index += 2
        elif token in ('-i', '--in-interface', '-o', '--out-interface') and value:
            interfaces.append({'name': value, 'direction': 'iifname' if token in ('-i', '--in-interface') else 'oifname'})
            interface_positions[len(parts) + 1] = value
            parts += [token, shlex.quote(value)]
            index += 2
        else:
            parts.append(shlex.quote(token))
            index += 1
    segments = []
    for index, part in enumerate(parts):
        if index:
            segments.append({'text': ' '})
        segments.append({'text': part, **({'interface': interface_positions[index]} if index in interface_positions else {})})
    return {'chain': chain, 'expression': ' '.join(parts) or 'any', 'action': action,
            'expression_parts': segments or [{'text': 'any'}],
            'targets': targets, 'interfaces': interfaces, 'counters': counters, 'raw': raw}


def tables(data, named_counters=None):
    groups = {}

    def table(backend, family, name):
        key = (backend, family, name)
        return groups.setdefault(key, {'backend': backend, 'family': family, 'name': name, 'chains': {}})

    entries = (data.get('ruleset') or {}).get('nftables', [])
    named = dict(named_counters or {})
    named.update({(entry['counter'].get('family'), entry['counter'].get('table'), entry['counter'].get('name')): entry['counter'] for entry in entries if 'counter' in entry})
    for entry in entries:
        for kind in ('table', 'chain', 'rule'):
            if kind not in entry:
                continue
            value = entry[kind]
            group = table('nftables', value.get('family', ''), value.get('name') if kind == 'table' else value.get('table', ''))
            if kind == 'table':
                group['_matches'] = value.get('_matches', {})
                continue
            name = value.get('name') if kind == 'chain' else value.get('chain', '')
            chain = group['chains'].setdefault(name, {'name': name, 'rules': []})
            if kind == 'chain':
                chain.update({key: val for key, val in value.items() if key != 'rules'})
            else:
                chain['rules'].append(nft_rule(value, named))
    for family, field in [('ip', 'ipv4_rules'), ('ip6', 'ipv6_rules')]:
        group = None
        for line in (data.get(field) or '').splitlines():
            if line.startswith('*'):
                backend = next((name for name in data.get('backends', []) if name != 'nftables'), data.get('backend', 'iptables'))
                group = table(backend, family, line[1:])
            elif group and line.startswith(':'):
                name, policy, *counter = line[1:].split()
                group['chains'].setdefault(name, {'name': name, 'rules': []}).update(policy=policy,
                    policy_counter=counter[0] if counter else None,
                    _matches=data.get('_legacy_matches', {}).get(f'{family}:{group["name"]}:{name}', {}))
            elif group and re.match(r'^(?:\[\d+:\d+\]\s*)?-A\s', line):
                rule = legacy_rule(line)
                rule['_matches'] = data.get('_legacy_matches', {}).get(f'{family}:{group["name"]}:{line}', {})
                group['chains'].setdefault(rule['chain'], {'name': rule['chain'], 'rules': []})['rules'].append(rule)
    result = []
    for group in groups.values():
        group['chains'] = list(group['chains'].values())
        for chain in group['chains']:
            names = {item['name'] for item in group['chains']}
            occurrences = {}
            for rule in chain['rules']:
                rule['jump_targets'] = [target for target in rule['targets'] if target in names]
                if group['backend'] == 'nftables' and rule.get('handle'):
                    identity = 'handle:' + rule['handle']
                else:
                    # Counter values must not change row identity on each poll.
                    structure = {key: rule.get(key) for key in ('expression', 'action', 'targets', 'interfaces')}
                    identity = hashlib.sha256(json.dumps(structure, sort_keys=True).encode()).hexdigest()
                occurrence = occurrences.get(identity, 0)
                occurrences[identity] = occurrence + 1
                rule['id'] = f'{identity}:{occurrence}'
        result.append(group)
    return result
