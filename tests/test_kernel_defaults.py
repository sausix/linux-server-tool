import gzip

from backend.system import kernel_defaults as kernel_defaults
from backend.system.sysctl import snapshot


def test_reads_values_and_provenance_from_compressed_local_config(tmp_path):
    path = tmp_path / 'config.gz'
    with gzip.open(path, 'wt') as stream:
        stream.write('CONFIG_DEFAULT_TCP_CONG="local-algorithm"\nCONFIG_DEFAULT_NET_SCH="local-qdisc"\n')
    values = kernel_defaults.discover([path], 'test-kernel')
    assert values['net/ipv4/tcp_congestion_control']['value'] == 'local-algorithm'
    assert values['net/core/default_qdisc']['value'] == 'local-qdisc'
    assert values['net/ipv4/tcp_congestion_control']['file'] == str(path)
    assert values['net/ipv4/tcp_congestion_control']['kernel_release'] == 'test-kernel'


def test_missing_symbols_are_unknown_and_preferred_config_is_authoritative(tmp_path):
    preferred = tmp_path / 'preferred'
    preferred.write_text('CONFIG_IPV6=y\n')
    fallback = tmp_path / 'fallback'
    fallback.write_text('CONFIG_DEFAULT_TCP_CONG="another-build"\n')
    assert kernel_defaults.discover([preferred, fallback]) == {}
    assert kernel_defaults.discover([tmp_path / 'absent']) == {}
    assert kernel_defaults.discover([tmp_path / 'absent', fallback])['net/ipv4/tcp_congestion_control']['value'] == 'another-build'


def test_default_resolution_is_cached_per_process(monkeypatch):
    calls = []
    monkeypatch.setattr(kernel_defaults, 'discover', lambda: calls.append(True) or {})
    kernel_defaults.local_defaults.cache_clear()
    try:
        kernel_defaults.local_defaults()
        kernel_defaults.local_defaults()
        assert calls == [True]
    finally:
        kernel_defaults.local_defaults.cache_clear()


def test_inventory_compares_only_known_local_build_defaults(tmp_path):
    root = tmp_path / 'proc'
    path = root / 'net/ipv4/tcp_congestion_control'
    path.parent.mkdir(parents=True)
    path.write_text('runtime-algorithm')
    defaults = {'net/ipv4/tcp_congestion_control': {'value': 'local-algorithm'}}
    result = snapshot(root, [], tmp_path / 'absent', False, defaults)
    assert result['options'][0]['kernel_default'] == 'local-algorithm'
    assert result['options'][0]['state'] == 'changed'
    assert snapshot(root, [], tmp_path / 'absent', False, {})['options'][0]['kernel_default'] is None
