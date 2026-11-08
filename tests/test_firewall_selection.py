import json

from backend.network import firewall as firewall


def fake_tools(monkeypatch, nft_entries, legacy_rules):
    monkeypatch.setattr(firewall.shutil, 'which', lambda name: '/tools/' + name if name in {'nft', 'iptables-legacy-save', 'ip6tables-legacy-save'} else None)

    def run(command, timeout):
        if command[0].endswith('/nft'):
            return json.dumps({'nftables': nft_entries})
        return legacy_rules if command[0].endswith('/iptables-legacy-save') else ''

    monkeypatch.setattr(firewall, '_run', run)


def test_auto_prefers_legacy_over_empty_nftables(monkeypatch):
    fake_tools(monkeypatch, [{'metainfo': {'version': 'test'}}], '*filter\n:INPUT DROP [0:0]\nCOMMIT\n')
    result = firewall.snapshot()
    assert result['backend'] == 'iptables-legacy'
    assert ':INPUT DROP' in result['ipv4_rules']


def test_auto_reports_both_independent_rulesets(monkeypatch):
    fake_tools(monkeypatch, [{'table': {'family': 'inet', 'name': 'filter'}}], '*filter\n:INPUT DROP [0:0]\nCOMMIT\n')
    result = firewall.snapshot()
    assert result['backends'] == ['nftables', 'iptables-legacy']
    assert result['ruleset']['nftables']
    assert result['ipv4_rules']


def test_explicit_backend_does_not_switch_on_empty_ruleset(monkeypatch):
    fake_tools(monkeypatch, [], '*filter\n:INPUT DROP [0:0]\nCOMMIT\n')
    result = firewall.snapshot(backend='nftables')
    assert result['backend'] == 'nftables'
    assert 'ipv4_rules' not in result


def test_explicit_missing_backend_reports_required_tools(monkeypatch):
    monkeypatch.setattr(firewall.shutil, 'which', lambda name: None)
    result = firewall.snapshot(backend='iptables-nft')
    assert any('iptables-nft-save not found' in error for error in result['errors'])
