from frontend.network.firewall import nft_rule, legacy_rule, tables
from frontend.inventory import filter_inventory
import pytest


@pytest.mark.parametrize('statements,match,action,targets', [
    ([{'xt': {'type': 'match', 'name': 'conntrack'}}, {'accept': None}], 'xt match conntrack', 'accept', []),
    ([{'xt': {'type': 'target', 'name': 'MASQUERADE'}}], 'any', 'masquerade (xt)', []),
    ([{'xt': {'type': 'match', 'name': 'addrtype'}}, {'jump': {'target': 'DOCKER'}}], 'xt match addrtype', 'jump', ['DOCKER']),
])
def test_xt_compatibility_statements_are_classified(statements, match, action, targets):
    raw = {'expr': statements}
    result = nft_rule(raw)
    assert result['expression'] == match
    assert result['action'] == action
    assert result['targets'] == targets
    assert ''.join(part['text'] for part in result['expression_parts']) == match
    assert 'parameters cannot be inferred' in result['compatibility_notes'][0]
    assert 'xt' in result['raw']


def test_xt_additional_fields_and_unknown_types_are_retained():
    result = nft_rule({'expr': [{'xt': {'type': 'match', 'name': 'conntrack', 'options': {'states': ['ESTABLISHED']}}}]})
    assert 'ESTABLISHED' in result['expression']
    assert result['compatibility_notes'] == []
    unknown = nft_rule({'expr': [{'xt': {'type': 'future-kind', 'name': 'extension'}}]})
    assert 'future-kind' in unknown['action']


def test_nft_match_targets_interfaces_and_large_counters():
    raw = {'family': 'inet', 'table': 'filter', 'chain': 'input', 'handle': 12, 'expr': [
        {'match': {'op': '==', 'left': {'meta': {'key': 'iifname'}}, 'right': 'eth0'}},
        {'match': {'op': '==', 'left': {'payload': {'protocol': 'ip', 'field': 'saddr'}},
                   'right': {'prefix': {'addr': '192.0.2.0', 'len': 24}}}},
        {'counter': {'packets': 2**60, 'bytes': 0}}, {'jump': {'target': 'trusted'}},
    ]}
    result = nft_rule(raw)
    assert result['expression'] == 'meta iifname == eth0 and ip saddr == 192.0.2.0/24'
    assert '\n' not in result['expression']
    assert result['targets'] == ['trusted']
    assert result['interfaces'] == [{'name': 'eth0', 'direction': 'iifname'}]
    assert result['counters'][0]['packets'] == str(2**60)
    assert result['counters'][0]['bytes'] == '0'


def test_complex_nft_statements_and_named_counters_are_retained():
    raw = {'family': 'inet', 'table': 'filter', 'expr': [{'counter': 'hits'},
        {'vmap': {'key': {'meta': {'key': 'l4proto'}}, 'data': '@protocols'}}]}
    result = nft_rule(raw, {('inet', 'filter', 'hits'): {'packets': 7, 'bytes': 77}})
    assert result['counters'][0]['packets'] == '7'
    assert 'vmap' in result['action']
    assert 'protocols' in result['raw']
    assert nft_rule({'expr': [{'accept': None}]})['counters'] == []


def test_legacy_counters_and_quoted_expressions():
    result = legacy_rule('[123:4567] -A INPUT -i eth0 -m comment --comment "two words" -g custom')
    assert result['chain'] == 'INPUT'
    assert result['action'] == 'goto'
    assert result['targets'] == ['custom']
    assert result['counters'] == [{'packets': '123', 'bytes': '4567'}]
    assert 'two words' in result['expression']
    assert result['interfaces'][0]['name'] == 'eth0'


def test_counter_save_rules_survive_filter_and_table_normalization():
    data = {'backend': 'iptables-legacy', 'ipv4_rules': '*filter\n:INPUT ACCEPT [0:0]\n:custom - [0:0]\n[123:4567] -A INPUT -i eth0 -j custom\nCOMMIT\n'}
    filtered = filter_inventory('firewall', data, [('firewall.interface', 'eth0')])
    assert '[123:4567] -A INPUT' in filtered['ipv4_rules']
    group = tables(filtered)[0]
    assert group['chains'][0]['rules'][0]['counters'][0]['packets'] == '123'
    full = tables(data)[0]
    assert full['chains'][0]['rules'][0]['jump_targets'] == ['custom']


def test_independent_backends_with_same_table_are_not_merged():
    data = {'backend': 'nftables + iptables-legacy', 'backends': ['nftables', 'iptables-legacy'],
            'ruleset': {'nftables': [{'table': {'family': 'ip', 'name': 'filter'}}]},
            'ipv4_rules': '*filter\n:INPUT ACCEPT [0:0]\nCOMMIT\n'}
    assert len(tables(data)) == 2


def test_rule_identity_ignores_counter_changes_and_tracks_insertions():
    def legacy(count, extra=''):
        return {'backend': 'iptables-legacy', 'ipv4_rules':
            f'*filter\n:INPUT ACCEPT [0:0]\n{extra}[{count}:99] -A INPUT -i eth0 -j ACCEPT\nCOMMIT\n'}
    first = tables(legacy(1))[0]['chains'][0]['rules'][0]
    changed = tables(legacy(2))[0]['chains'][0]['rules'][0]
    inserted = tables(legacy(3, '[0:0] -A INPUT -i eth1 -j DROP\n'))[0]['chains'][0]['rules']
    assert first['id'] == changed['id'] == inserted[1]['id']
    assert inserted[0]['id'] != first['id']
    assert ''.join(part['text'] for part in first['expression_parts']) == first['expression']
    assert [part['interface'] for part in first['expression_parts'] if 'interface' in part] == ['eth0']


def test_nft_expression_links_only_interface_operands_and_keeps_handle_identity():
    data = {'ruleset': {'nftables': [
        {'rule': {'family': 'inet', 'table': 'filter', 'chain': 'input', 'handle': 5, 'expr': [
            {'match': {'left': {'meta': {'key': 'iifname'}}, 'op': '!=', 'right': {'set': ['eth0', 'eth1']}}},
            {'match': {'left': {'ct': {'key': 'state'}}, 'op': '==', 'right': 'established'}},
            {'counter': {'packets': 1, 'bytes': 10}}, {'accept': None}]}}
    ]}}
    first = tables(data)[0]['chains'][0]['rules'][0]
    data['ruleset']['nftables'][0]['rule']['expr'][2]['counter']['packets'] = 2
    second = tables(data)[0]['chains'][0]['rules'][0]
    assert first['id'] == second['id']
    assert [part['interface'] for part in first['expression_parts'] if 'interface' in part] == ['eth0', 'eth1']
    assert ''.join(part['text'] for part in first['expression_parts']) == first['expression']
