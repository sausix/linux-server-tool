"""IP field validation with an explicit prefix policy."""

import ipaddress
from .ip_classification import classify_ip


def validate_ip(value: str, prefix_policy: str):
    value = value.strip()
    if prefix_policy not in {'required', 'forbidden', 'optional'}:
        raise ValueError('Invalid prefix policy')
    if prefix_policy == 'required' and '/' not in value:
        raise ValueError('A subnet prefix is required, for example /24 or /64.')
    if prefix_policy == 'forbidden' and '/' in value:
        raise ValueError('A subnet prefix is not allowed in this field.')
    if '%' in value:
        raise ValueError('Use the interface selector instead of an address zone identifier.')
    try:
        parsed = ipaddress.ip_interface(value)
    except ValueError as exc:
        raise ValueError('Enter a valid IPv4 or IPv6 address with a valid prefix length.') from exc
    if parsed.ip.is_multicast or parsed.ip.is_unspecified:
        raise ValueError('An interface address must be a unicast address.')
    network = parsed.network
    count = network.num_addresses
    # Do not enumerate IPv6 networks or large IPv4 ranges.
    first = network.network_address
    last = network.broadcast_address
    if parsed.version == 4 and network.prefixlen < 31:
        usable = max(0, count - 2)
        first = first + 1
        last = last - 1
    else:
        usable = count
    return {
        'address': str(parsed), 'ip': str(parsed.ip), 'family': f'ipv{parsed.version}',
        'address_type': classify_ip(str(parsed))['label'],
        'prefix_length': network.prefixlen, 'network': str(network),
        'netmask': str(network.netmask), 'first': str(first), 'last': str(last),
        'total_addresses': str(count), 'usable_addresses': str(usable),
        'broadcast': str(network.broadcast_address) if parsed.version == 4 else None,
        'note': 'IPv6 has no broadcast; address count is mathematical, not a guarantee that every address is assignable.' if parsed.version == 6 else 'IPv4 /31 and /32 use point-to-point or host semantics.',
    }
