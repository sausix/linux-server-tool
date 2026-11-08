import errno
import socket
import struct

from backend.network.inventory import IFINFO_MSG, IFLA_TARGET_NETNSID, _attributes, interfaces, veth_peer_links


def attribute(kind, data):
    size = 4 + len(data)
    return struct.pack('=HH', size, kind) + data + b'\0' * ((-size) % 4)


def internal_link(nsid, index, host_index, mac='02:42:ac:16:01:f9'):
    return (IFINFO_MSG.pack(socket.AF_UNSPEC, 0, 1, index, 0, 0) +
            attribute(3, b'eth0\0') + attribute(1, bytes.fromhex(mac.replace(':', ''))) +
            attribute(5, struct.pack('=I', host_index)) +
            attribute(18, attribute(1, b'veth\0')) + attribute(46, struct.pack('=i', nsid)))


def host_link(name='veth-web', index=100, nsid=0):
    return {'name': name, 'index': index, 'kind': 'veth', 'master': 'br-demo',
            'peer_index': 2, 'link_netnsid': nsid, 'mac_address': '02:42:00:00:00:01'}


def test_peer_indices_are_reciprocal_and_scoped_to_each_namespace(monkeypatch):
    calls = []
    def dump(self, kind, body):
        nsid = struct.unpack('=i', dict(_attributes(body, IFINFO_MSG.size))[IFLA_TARGET_NETNSID])[0]
        calls.append(nsid)
        return [internal_link(nsid, 2, 100 if nsid == 0 else 101)]
    monkeypatch.setattr('backend.network.inventory.RouteNetlink.dump', dump)
    links = [host_link(), host_link('veth-two', 101, 1), host_link('veth-three', 102, 0)]
    result = veth_peer_links(links)
    assert set(result) == {'veth-web', 'veth-two'}
    assert result['veth-web']['internal_mac'] != result['veth-web']['port_mac']
    assert result['veth-web']['namespace_id'] == 0
    assert result['veth-two']['namespace_id'] == 1
    assert calls == [0, 1]  # One dump per namespace per snapshot, including namespace ID zero.


def test_unconfirmed_target_namespace_and_missing_permission_do_not_guess(monkeypatch, caplog):
    monkeypatch.setattr('backend.network.inventory._peer_read_errors', set())
    monkeypatch.setattr('backend.network.inventory.RouteNetlink.dump', lambda *args: [internal_link(99, 2, 100)])
    assert veth_peer_links([host_link()]) == {}
    def denied(*args):
        raise PermissionError(errno.EACCES, 'Permission denied')
    monkeypatch.setattr('backend.network.inventory.RouteNetlink.dump', denied)
    assert veth_peer_links([host_link()]) == {}
    assert 'CAP_NET_ADMIN' in caplog.text


def test_host_inventory_keeps_peer_index_and_namespace_id(monkeypatch):
    payload = internal_link(0, 100, 2)
    payload += attribute(37, struct.pack('=i', 0))
    monkeypatch.setattr('backend.network.inventory.RouteNetlink.dump', lambda *args: [payload])
    monkeypatch.setattr('backend.network.inventory._read_interface_stats', lambda: {})
    result = interfaces()[0]
    assert (result['index'], result['peer_index'], result['link_netnsid']) == (100, 2, 0)


def test_daemon_maps_only_local_engines_with_interface_access(monkeypatch):
    from backend.daemon import DaemonServer
    from backend.daemon_config import DaemonSettings
    calls = []
    def peers(links, timeout):
        calls.append(links)
        return {'veth-web': {'internal_index': 2}}
    monkeypatch.setattr('backend.docker.component.veth_peer_links', peers)
    server = DaemonServer(DaemonSettings(_env_file=None, docker_endpoint='unix:///run/docker.sock', interfaces_access='ro'))
    assert server.components['docker'].docker_peer_links([host_link()])['veth-web']['internal_index'] == 2
    server.settings.docker_endpoint = 'tcp://127.0.0.1:2376'
    assert server.components['docker'].docker_peer_links([host_link()]) == {}
    server.settings.docker_endpoint = 'unix:///run/docker.sock'
    server.settings.interfaces_access = 'disabled'
    assert server.components['docker'].docker_peer_links([host_link()]) == {}
    assert len(calls) == 1
