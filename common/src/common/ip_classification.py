"""Human-readable IPv4/IPv6 address types, independent of interface state."""

import ipaddress


def classify_ip(value):
    ip = ipaddress.ip_interface(value).ip
    scope = None
    embedded = None
    if ip.is_unspecified:
        kind, label = 'unspecified', 'Unspecified'
    elif ip.is_loopback:
        kind, label = 'loopback', 'Localhost / loopback'
    elif ip.is_multicast:
        kind, label = 'multicast', 'Multicast'
        if ip.version == 6:
            code = ip.packed[1] & 15
            scope = {0: 'reserved', 1: 'interface-local', 2: 'link-local', 3: 'realm-local',
                     4: 'admin-local', 5: 'site-local', 8: 'organization-local', 14: 'global',
                     15: 'reserved'}.get(code, f'unassigned ({code})')
    elif ip.version == 4:
        if ip == ipaddress.ip_address('255.255.255.255'):
            kind, label = 'broadcast', 'Limited broadcast'
        elif ip.is_link_local:
            kind, label = 'link-local', 'Link-local'
        elif any(ip in ipaddress.ip_network(net) for net in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
            kind, label = 'private', 'Private (RFC 1918)'
        elif ip in ipaddress.ip_network('100.64.0.0/10'):
            kind, label = 'shared', 'Shared address space / CGNAT'
        elif any(ip in ipaddress.ip_network(net) for net in ('192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24')):
            kind, label = 'documentation', 'Documentation'
        elif ip in ipaddress.ip_network('198.18.0.0/15'):
            kind, label = 'benchmark', 'Benchmark testing'
        elif ip.is_reserved:
            kind, label = 'reserved', 'Reserved'
        elif ip.is_global:
            kind, label = 'public', 'Public unicast'
        else:
            kind, label = 'special', 'Special-purpose'
    elif ip.ipv4_mapped:
        kind, label = 'ipv4-mapped', 'IPv4-mapped IPv6'
        embedded = str(ip.ipv4_mapped)
    elif ip.is_link_local:
        kind, label = 'link-local', 'Link-local unicast'
    elif ip in ipaddress.ip_network('fc00::/7'):
        kind, label = 'unique-local', 'Unique local (ULA / private)'
    elif ip.is_site_local:
        kind, label = 'site-local', 'Site-local (deprecated)'
    elif any(ip in ipaddress.ip_network(net) for net in ('2001:db8::/32', '3fff::/20')):
        kind, label = 'documentation', 'Documentation'
    elif ip.sixtofour:
        kind, label = '6to4', '6to4 transition'
        embedded = str(ip.sixtofour)
    elif ip.teredo:
        kind, label = 'teredo', 'Teredo transition'
    elif any(ip in ipaddress.ip_network(net) for net in ('64:ff9b::/96', '64:ff9b:1::/48')):
        kind, label = 'nat64', 'IPv4/IPv6 translation (NAT64)'
    elif ip in ipaddress.ip_network('100::/64'):
        kind, label = 'discard', 'Discard-only'
    elif ip.is_global and ip in ipaddress.ip_network('2000::/3'):
        kind, label = 'public', 'Global unicast (public)'
    else:
        kind, label = 'special', 'Reserved / special-purpose'
    return {'type': kind, 'label': label, 'scope': scope,
            'embedded_ipv4': embedded, 'family': f'ipv{ip.version}'}
