"""Interface exposure based on currently assigned IP addresses."""

import ipaddress


def interface_scope(addresses, flags=()):
    parsed = []
    for value in addresses:
        try:
            address = ipaddress.ip_interface(value).ip
        except ValueError:
            continue
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        parsed.append(address)
    if any(not address.is_loopback and not address.is_private for address in parsed):
        return 'public'
    if parsed:
        return 'loopback' if all(address.is_loopback for address in parsed) else 'private'
    return 'loopback' if 'loopback' in flags else 'unknown'
