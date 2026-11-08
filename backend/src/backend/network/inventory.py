"""Linux network inventory collected directly from rtnetlink and procfs/sysfs."""

from __future__ import annotations

import ipaddress
import os
import socket
import struct
import logging
import time
import errno
from pathlib import Path
from typing import Iterator
from common.ip_classification import classify_ip

NLMSG_HDR = struct.Struct("=IHHII")
RTA_HDR = struct.Struct("=HH")
IFINFO_MSG = struct.Struct("=BBHiII")
IFADDR_MSG = struct.Struct("=BBBBI")
RT_MSG = struct.Struct("=BBBBBBBBI")

NLMSG_ERROR = 2
NLMSG_DONE = 3
NLM_F_REQUEST = 1
NLM_F_ACK = 4
NLM_F_DUMP = 0x300
NLM_F_EXCL = 0x200
NLM_F_CREATE = 0x400
RTM_GETLINK = 18
RTM_NEWLINK = 16
RTM_GETADDR = 22
RTM_NEWADDR = 20
RTM_DELADDR = 21
RTM_GETROUTE = 26
RTM_NEWROUTE = 24
RTM_DELROUTE = 25

IFLA_ADDRESS = 1
IFLA_IFNAME = 3
IFLA_MTU = 4
IFLA_LINK = 5
IFLA_MASTER = 10
IFLA_OPERSTATE = 16
IFLA_LINKINFO = 18
IFLA_INFO_KIND = 1
IFLA_LINK_NETNSID = 37
IFLA_TARGET_NETNSID = 46
IFA_ADDRESS = 1
IFA_LOCAL = 2
IFA_LABEL = 3
RTA_DST = 1
RTA_OIF = 4
RTA_GATEWAY = 5
RTA_PRIORITY = 6
RTA_PREFSRC = 7
RTA_TABLE = 15

IFF_FLAGS = {
    0x1: "up",
    0x2: "broadcast",
    0x8: "loopback",
    0x10: "point-to-point",
    0x40: "running",
    0x80: "noarp",
    0x100: "promisc",
    0x1000: "multicast",
    0x10000: "lower-up",
    0x20000: "dormant",
}
ROUTE_TABLES = {0: "unspecified", 253: "default", 254: "main", 255: "local"}
ROUTE_TYPES = {1: "unicast", 2: "local", 3: "broadcast", 6: "blackhole", 7: "unreachable", 8: "prohibit"}
OPER_STATES = {
    0: "unknown",
    1: "not-present",
    2: "down",
    3: "lower-layer-down",
    4: "testing",
    5: "dormant",
    6: "up",
}


class NetlinkError(OSError):
    pass


def _align(length: int) -> int:
    return (length + 3) & ~3


def _attributes(payload: bytes, offset: int) -> Iterator[tuple[int, bytes]]:
    while offset + RTA_HDR.size <= len(payload):
        length, attr_type = RTA_HDR.unpack_from(payload, offset)
        if length < RTA_HDR.size or offset + length > len(payload):
            break
        yield attr_type & 0x3FFF, payload[offset + RTA_HDR.size : offset + length]
        offset += _align(length)


def _cstring(value: bytes) -> str:
    return value.rstrip(b"\0").decode("utf-8", errors="replace")


def _uint32(value: bytes) -> int:
    return struct.unpack_from("=I", value)[0]


def _ip(family: int, value: bytes) -> str:
    return str(ipaddress.ip_address(value[: 4 if family == socket.AF_INET else 16]))


class RouteNetlink:
    def __init__(self, timeout=None):
        self.sequence = os.getpid()
        self.timeout = timeout

    def dump(self, message_type: int, body: bytes) -> list[bytes]:
        self.sequence += 1
        header = NLMSG_HDR.pack(
            NLMSG_HDR.size + len(body), message_type, NLM_F_REQUEST | NLM_F_DUMP, self.sequence, 0
        )
        results: list[bytes] = []
        with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as netlink:
            if self.timeout is not None:
                netlink.settimeout(self.timeout)
            netlink.bind((0, 0))
            netlink.send(header + body)
            finished = False
            while not finished:
                packet = netlink.recv(1024 * 1024)
                offset = 0
                while offset + NLMSG_HDR.size <= len(packet):
                    length, kind, flags, sequence, _pid = NLMSG_HDR.unpack_from(packet, offset)
                    if length < NLMSG_HDR.size:
                        raise NetlinkError("Invalid netlink message length")
                    payload = packet[offset + NLMSG_HDR.size : offset + length]
                    offset += _align(length)
                    if sequence != self.sequence:
                        continue
                    if flags & 0x10:
                        raise NetlinkError(errno.EINTR, 'Netlink dump interrupted; retry the snapshot')
                    if kind == NLMSG_DONE:
                        if len(payload) >= 4 and (error := struct.unpack_from('=i', payload)[0]) < 0:
                            raise NetlinkError(-error, os.strerror(-error))
                        finished = True
                        break
                    if kind == NLMSG_ERROR:
                        error_number = struct.unpack_from("=i", payload)[0]
                        if error_number:
                            raise NetlinkError(-error_number, os.strerror(-error_number))
                        continue
                    results.append(payload)
        return results

    def request(self, message_type: int, body: bytes, flags: int = 0) -> None:
        self.sequence += 1
        header = NLMSG_HDR.pack(
            NLMSG_HDR.size + len(body),
            message_type,
            NLM_F_REQUEST | NLM_F_ACK | flags,
            self.sequence,
            0,
        )
        with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as netlink:
            netlink.bind((0, 0))
            netlink.send(header + body)
            while True:
                packet = netlink.recv(65536)
                offset = 0
                while offset + NLMSG_HDR.size <= len(packet):
                    length, kind, _flags, sequence, _pid = NLMSG_HDR.unpack_from(packet, offset)
                    payload = packet[offset + NLMSG_HDR.size : offset + length]
                    offset += _align(length)
                    if sequence != self.sequence or kind != NLMSG_ERROR:
                        continue
                    error_number = struct.unpack_from("=i", payload)[0]
                    if error_number:
                        raise NetlinkError(-error_number, os.strerror(-error_number))
                    return


def _attribute(attr_type: int, value: bytes) -> bytes:
    length = RTA_HDR.size + len(value)
    return RTA_HDR.pack(length, attr_type) + value + (b"\0" * (_align(length) - length))


def set_link(interface: str, *, up: bool | None = None, mtu: int | None = None) -> dict:
    index = socket.if_nametoindex(interface)
    if up is None and mtu is None:
        raise ValueError("At least one of up or mtu must be supplied")
    if mtu is not None and not 576 <= mtu <= 65536:
        raise ValueError("MTU must be between 576 and 65536")
    flags = 0x1 if up else 0
    change = 0x1 if up is not None else 0
    body = IFINFO_MSG.pack(socket.AF_UNSPEC, 0, 0, index, flags, change)
    if mtu is not None:
        body += _attribute(IFLA_MTU, struct.pack("=I", mtu))
    RouteNetlink().request(RTM_NEWLINK, body)
    return {"interface": interface, "up": up, "mtu": mtu}


def change_address(action: str, interface: str, address: str) -> dict:
    if action not in {"add", "delete"}:
        raise ValueError("Address action must be add or delete")
    from common.ip_validation import validate_ip
    parsed = ipaddress.ip_interface(validate_ip(address, 'required')['address'])
    family = socket.AF_INET if parsed.version == 4 else socket.AF_INET6
    index = socket.if_nametoindex(interface)
    packed = parsed.ip.packed
    body = IFADDR_MSG.pack(family, parsed.network.prefixlen, 0, 0, index)
    body += _attribute(IFA_LOCAL, packed) + _attribute(IFA_ADDRESS, packed)
    message_type = RTM_NEWADDR if action == "add" else RTM_DELADDR
    flags = NLM_F_CREATE | NLM_F_EXCL if action == "add" else 0
    RouteNetlink().request(message_type, body, flags)
    return {"action": action, "interface": interface, "address": str(parsed)}


def change_route(
    action: str,
    destination: str,
    *,
    gateway: str | None = None,
    interface: str | None = None,
    metric: int | None = None,
    family: str | None = None,
) -> dict:
    if action not in {"add", "delete"}:
        raise ValueError("Route action must be add or delete")
    if destination == "default":
        if family not in {"ipv4", "ipv6"}:
            raise ValueError("A default route requires family=ipv4 or family=ipv6")
        network = ipaddress.ip_network("0.0.0.0/0" if family == "ipv4" else "::/0")
    else:
        network = ipaddress.ip_network(destination, strict=False)
    socket_family = socket.AF_INET if network.version == 4 else socket.AF_INET6
    if gateway:
        parsed_gateway = ipaddress.ip_address(gateway)
        if parsed_gateway.version != network.version:
            raise ValueError("Gateway and destination families differ")
    else:
        parsed_gateway = None
    if metric is not None and metric < 0:
        raise ValueError("Route metric must not be negative")
    body = RT_MSG.pack(socket_family, network.prefixlen, 0, 0, 254, 3, 0, 1, 0)
    if network.prefixlen:
        body += _attribute(RTA_DST, network.network_address.packed)
    if parsed_gateway:
        body += _attribute(RTA_GATEWAY, parsed_gateway.packed)
    if interface:
        body += _attribute(RTA_OIF, struct.pack("=I", socket.if_nametoindex(interface)))
    if metric is not None:
        body += _attribute(RTA_PRIORITY, struct.pack("=I", metric))
    message_type = RTM_NEWROUTE if action == "add" else RTM_DELROUTE
    flags = NLM_F_CREATE | NLM_F_EXCL if action == "add" else 0
    RouteNetlink().request(message_type, body, flags)
    return {
        "action": action,
        "destination": str(network),
        "gateway": str(parsed_gateway) if parsed_gateway else None,
        "interface": interface,
        "metric": metric,
    }


def _read_interface_stats() -> dict[str, dict[str, int]]:
    stats: dict[str, dict[str, int]] = {}
    try:
        lines = Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()[2:]
    except OSError:
        return stats
    for line in lines:
        name, raw_values = line.split(":", 1)
        values = [int(value) for value in raw_values.split()]
        stats[name.strip()] = {
            "rx_bytes": values[0],
            "rx_packets": values[1],
            "rx_errors": values[2],
            "rx_dropped": values[3],
            "tx_bytes": values[8],
            "tx_packets": values[9],
            "tx_errors": values[10],
            "tx_dropped": values[11],
        }
    return stats


def interfaces() -> list[dict]:
    body = IFINFO_MSG.pack(socket.AF_UNSPEC, 0, 0, 0, 0, 0)
    statistics = _read_interface_stats()
    links: list[dict] = []
    for payload in RouteNetlink().dump(RTM_GETLINK, body):
        family, _pad, link_type, index, flags, _change = IFINFO_MSG.unpack_from(payload)
        attrs = dict(_attributes(payload, IFINFO_MSG.size))
        name = _cstring(attrs.get(IFLA_IFNAME, b""))
        link_info = dict(_attributes(attrs.get(IFLA_LINKINFO, b""), 0))
        kind = _cstring(link_info.get(IFLA_INFO_KIND, b"")) or None
        if kind is None and Path(f"/sys/class/net/{name}/bridge").is_dir():
            kind = "bridge"
        links.append(
            {
                "index": index,
                "peer_index": _uint32(attrs[IFLA_LINK]) if IFLA_LINK in attrs else None,
                "link_netnsid": struct.unpack_from('=i', attrs[IFLA_LINK_NETNSID])[0] if IFLA_LINK_NETNSID in attrs else None,
                "name": name,
                "family": family,
                "type": link_type,
                "kind": kind,
                "mac_address": attrs.get(IFLA_ADDRESS, b"").hex(":") or None,
                "mtu": _uint32(attrs[IFLA_MTU]) if IFLA_MTU in attrs else None,
                "master_index": _uint32(attrs[IFLA_MASTER]) if IFLA_MASTER in attrs else None,
                "operational_state": OPER_STATES.get(attrs[IFLA_OPERSTATE][0])
                if attrs.get(IFLA_OPERSTATE)
                else None,
                "flags": [name for bit, name in IFF_FLAGS.items() if flags & bit],
                "statistics": statistics.get(name, {}),
            }
        )
    names = {link["index"]: link["name"] for link in links}
    for link in links:
        link["master"] = names.get(link.pop("master_index"))
    annotate_bridges(links)
    return links


_peer_read_errors = set()


def veth_peer_links(links, timeout=5.0):
    """Resolve reciprocal veth indices via read-only target-namespace dumps.

    IFLA_LINK_NETNSID is relative to this daemon's network namespace. Asking
    RTM_GETLINK for that namespace needs CAP_NET_ADMIN, not setns/SYS_ADMIN.
    Never compare interface indices from different namespaces without this ID.
    """
    result, cache = {}, {}
    deadline = time.monotonic() + timeout
    for port in links:
        nsid = port.get('link_netnsid')
        if port.get('kind') != 'veth' or not port.get('master') or nsid is None or nsid < 0 or not port.get('peer_index'):
            continue
        if nsid not in cache:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logging.getLogger(__name__).warning('Docker peer mapping exceeded its read timeout')
                break
            request = IFINFO_MSG.pack(socket.AF_UNSPEC, 0, 0, 0, 0, 0)
            request += RTA_HDR.pack(8, IFLA_TARGET_NETNSID) + struct.pack('=i', nsid)
            cache[nsid] = {}
            try:
                collected = {}
                for payload in RouteNetlink(timeout=remaining).dump(RTM_GETLINK, request):
                    if len(payload) < IFINFO_MSG.size:
                        continue
                    _, _, _, index, _, _ = IFINFO_MSG.unpack_from(payload)
                    attrs = dict(_attributes(payload, IFINFO_MSG.size))
                    if IFLA_TARGET_NETNSID not in attrs or struct.unpack_from('=i', attrs[IFLA_TARGET_NETNSID])[0] != nsid:
                        raise NetlinkError(errno.EOPNOTSUPP, 'Kernel did not confirm the requested target network namespace')
                    info = dict(_attributes(attrs.get(IFLA_LINKINFO, b''), 0))
                    collected[index] = {
                        'index': index, 'name': _cstring(attrs.get(IFLA_IFNAME, b'')),
                        'kind': _cstring(info.get(IFLA_INFO_KIND, b'')),
                        'peer_index': _uint32(attrs[IFLA_LINK]) if IFLA_LINK in attrs else None,
                        'mac': attrs.get(IFLA_ADDRESS, b'').hex(':')}
                cache[nsid] = collected
            except OSError as exc:
                reason = exc.errno or str(type(exc).__name__)
                if reason not in _peer_read_errors:
                    _peer_read_errors.add(reason)
                    logging.getLogger(__name__).warning('Docker veth peer mapping unavailable: %s; target-netns reads require CAP_NET_ADMIN and a compatible local kernel', exc)
        peer = cache[nsid].get(port['peer_index'])
        if peer and peer['kind'] == 'veth' and peer['peer_index'] == port.get('index'):
            result[port['name']] = {'namespace_id': nsid, 'internal_index': peer['index'],
                                    'internal_name': peer['name'], 'internal_mac': peer['mac'],
                                    'port_index': port['index'], 'port_mac': port.get('mac_address')}
    return result


def annotate_bridges(links):
    for bridge in links:
        if bridge.get('kind') != 'bridge':
            continue
        ports = [link for link in links if link.get('master') == bridge['name']]
        bridge['ports'] = [{key: port.get(key) for key in
                            ('name', 'kind', 'mac_address', 'operational_state', 'flags')}
                           for port in ports]
        matches = [port['name'] for port in ports if bridge.get('mac_address') and
                   (port.get('mac_address') or '').lower() == bridge['mac_address'].lower()]
        bridge['mac_source'] = {'matching_ports': matches,
            'note': 'Matching port MAC; inheritance is possible but not proven. Port insertion order is not available.'
                    if matches else 'No matching port MAC; address may be explicit or generated.'}




def addresses() -> list[dict]:
    body = IFADDR_MSG.pack(socket.AF_UNSPEC, 0, 0, 0, 0)
    names = {index: name for index, name in socket.if_nameindex()}
    entries: list[dict] = []
    for payload in RouteNetlink().dump(RTM_GETADDR, body):
        family, prefix, flags, scope, index = IFADDR_MSG.unpack_from(payload)
        if family not in {socket.AF_INET, socket.AF_INET6}:
            continue
        attrs = dict(_attributes(payload, IFADDR_MSG.size))
        value = attrs.get(IFA_LOCAL) or attrs.get(IFA_ADDRESS)
        if not value:
            continue
        peer = attrs.get(IFA_ADDRESS)
        entries.append(
            {
                "interface": names.get(index, str(index)),
                "family": "ipv4" if family == socket.AF_INET else "ipv6",
                "address": _ip(family, value),
                'classification': classify_ip(_ip(family, value)),
                "prefix_length": prefix,
                "peer": _ip(family, peer) if peer and peer != value else None,
                "scope": scope,
                "flags": flags,
                "label": _cstring(attrs.get(IFA_LABEL, b"")) or None,
            }
        )
    return entries


def routes() -> list[dict]:
    body = RT_MSG.pack(socket.AF_UNSPEC, 0, 0, 0, 0, 0, 0, 0, 0)
    names = {index: name for index, name in socket.if_nameindex()}
    entries: list[dict] = []
    for payload in RouteNetlink().dump(RTM_GETROUTE, body):
        family, dst_len, src_len, tos, table, protocol, scope, route_type, flags = RT_MSG.unpack_from(payload)
        if family not in {socket.AF_INET, socket.AF_INET6}:
            continue
        attrs = dict(_attributes(payload, RT_MSG.size))
        table = _uint32(attrs[RTA_TABLE]) if RTA_TABLE in attrs else table
        destination = _ip(family, attrs[RTA_DST]) if RTA_DST in attrs else "default"
        entries.append(
            {
                "family": "ipv4" if family == socket.AF_INET else "ipv6",
                "destination": destination,
                "prefix_length": dst_len,
                "source_prefix_length": src_len,
                "gateway": _ip(family, attrs[RTA_GATEWAY]) if RTA_GATEWAY in attrs else None,
                "preferred_source": _ip(family, attrs[RTA_PREFSRC]) if RTA_PREFSRC in attrs else None,
                "interface": names.get(_uint32(attrs[RTA_OIF])) if RTA_OIF in attrs else None,
                "metric": _uint32(attrs[RTA_PRIORITY]) if RTA_PRIORITY in attrs else None,
                "table": table,
                "table_name": ROUTE_TABLES.get(table),
                "protocol": protocol,
                "scope": scope,
                "type": ROUTE_TYPES.get(route_type, str(route_type)),
                "tos": tos,
                "flags": flags,
            }
        )
    return entries


def snapshot() -> dict:
    return {"interfaces": interfaces(), "addresses": addresses(), "routes": routes()}
