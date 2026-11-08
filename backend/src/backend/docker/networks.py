"""Associate Docker networks with interfaces visible in the daemon namespace."""

import socket
import ipaddress


def local_interface_names():
    try:
        return {name for _, name in socket.if_nameindex()}
    except OSError:
        return set()


def container_endpoints(container):
    """Select endpoint fields shared by container list and inspect responses."""
    labels = container.get('Labels') or (container.get('Config') or {}).get('Labels') or {}
    stack = labels.get('com.docker.compose.project') or labels.get('com.docker.stack.namespace', '')
    kind = 'compose' if labels.get('com.docker.compose.project') else 'swarm' if stack else ''
    result = []
    for name, value in ((container.get('NetworkSettings') or {}).get('Networks') or {}).items():
        value = value or {}
        result.append({'name': name, 'network_id': value.get('NetworkID', ''),
                       'container_id': container.get('Id', ''),
                       'container': container.get('Name', '').lstrip('/') or ', '.join(sorted(n.lstrip('/') for n in container.get('Names', []))) or container.get('Id', '')[:12],
                       'service': labels.get('com.docker.compose.service') or labels.get('com.docker.swarm.service.name', ''),
                       'service_stack': stack, 'service_stack_kind': kind,
                       'mac': value.get('MacAddress', ''),
                       'ip4': value.get('IPAddress', ''), 'ip4_prefix_length': value.get('IPPrefixLen'),
                       'gateway4': value.get('Gateway', ''),
                       'ip6': value.get('GlobalIPv6Address', ''), 'ip6_prefix_length': value.get('GlobalIPv6PrefixLen'),
                       'gateway6': value.get('IPv6Gateway', ''),
                       'interface': None, 'interface_source': None, 'bridge_interface': None,
                       'stack': '', 'stack_kind': ''})
    return sorted(result, key=lambda item: (item['name'].casefold(), item['name']))


def associate_endpoints(endpoints, networks, interfaces=(), peer_links=None):
    """Join Docker endpoint identity to a namespace-scoped reciprocal veth pair."""
    links = {item['name']: item for item in interfaces}
    peer_links = peer_links or {}
    by_id = {item['Id']: item for item in networks if item.get('Id')}
    by_name = {item['Name']: item for item in networks if item.get('Name')}
    claims = {}
    for endpoint in endpoints:
        network = by_id.get(endpoint['network_id']) or by_name.get(endpoint['name'])
        if network:
            key = (network.get('Id') or network.get('Name'), (endpoint.get('mac') or '').lower())
            claims.setdefault(key, set()).add(endpoint.get('container_id') or endpoint.get('container'))
    for endpoint in endpoints:
        endpoint.update(interface=None, interface_source=None, bridge_interface=None, port_mac=None,
                        internal_index=None, port_index=None, namespace_id=None, stack='', stack_kind='')
        network = by_id.get(endpoint['network_id']) or by_name.get(endpoint['name'])
        if not network:
            continue
        labels = network.get('Labels') or {}
        stack = labels.get('com.docker.compose.project') or labels.get('com.docker.stack.namespace', '')
        endpoint.update(stack=stack, stack_kind='compose' if labels.get('com.docker.compose.project') else 'swarm' if stack else '')
        bridge = network_interface(network, set(links))['interface']
        endpoint['bridge_interface'] = bridge
        mac = (endpoint.get('mac') or '').lower()
        if not bridge or network.get('Driver') != 'bridge' or not mac or mac == '00:00:00:00:00:00':
            continue
        if len(claims.get((network.get('Id') or network.get('Name'), mac), set())) != 1:
            continue
        candidates = []
        for name, peer in peer_links.items():
            port = links.get(name, {})
            if (port.get('kind') == 'veth' and port.get('master') == bridge and
                    port.get('index') == peer.get('port_index') and
                    port.get('peer_index') == peer.get('internal_index') and
                    port.get('link_netnsid') == peer.get('namespace_id') and
                    (peer.get('internal_mac') or '').lower() == mac):
                candidates.append((port, peer))
        if len(candidates) == 1:
            port, peer = candidates[0]
            endpoint.update(interface=port['name'],
                            interface_source='Docker endpoint MAC, target netns and reciprocal veth indices',
                            port_mac=peer.get('port_mac'), internal_index=peer['internal_index'],
                            port_index=peer['port_index'], namespace_id=peer['namespace_id'])
    return endpoints


def network_interface(network, names):
    options = network.get('Options') or {}
    driver = network.get('Driver')
    candidate, source = None, None
    if driver == 'bridge':
        candidate = options.get('com.docker.network.bridge.name')
        source = 'Docker bridge option'
        if not candidate:
            if options.get('com.docker.network.bridge.default_bridge') == 'true':
                candidate = 'docker0'
            elif network.get('Id'):
                candidate = 'br-' + network['Id'][:12]
            source = 'Docker bridge naming convention'
    elif driver in {'macvlan', 'ipvlan'}:
        candidate, source = options.get('parent'), 'Docker parent option'
    if candidate not in names:
        return {'interface': None, 'interface_source': None}
    return {'interface': candidate, 'interface_source': source}


def annotate_interfaces(interfaces, networks, containers=(), peer_links=None):
    names = {item['name'] for item in interfaces}
    links = {item['name']: item for item in interfaces}
    associations = {name: set() for name in names}
    network_names = {name: set() for name in names}
    for network in networks:
        name = network_interface(network, names)['interface']
        if not name:
            continue
        network_names[name].add(network.get('Name', ''))
        labels = network.get('Labels') or {}
        for label, kind in [('com.docker.compose.project', 'compose'),
                            ('com.docker.stack.namespace', 'swarm')]:
            if labels.get(label):
                associations[name].add((kind, labels[label]))
    for item in interfaces:
        item['docker_networks'] = sorted(network_names[item['name']])
        item['docker_stacks'] = [{'kind': kind, 'name': name}
                                 for kind, name in sorted(associations[item['name']])]
        item['docker_services'] = []
    endpoints = [endpoint for container in containers for endpoint in container_endpoints(container)]
    associate_endpoints(endpoints, networks, interfaces, peer_links)
    for endpoint in endpoints:
        if not endpoint['interface']:
            continue
        link = links[endpoint['interface']]
        link['docker_networks'] = sorted(set(link['docker_networks']) | {endpoint['name']})
        if endpoint['service_stack']:
            association = {'kind': endpoint['service_stack_kind'], 'name': endpoint['service_stack']}
            if association not in link['docker_stacks']:
                link['docker_stacks'].append(association)
        if endpoint['service'] and endpoint['service_stack']:
            value = {'kind': endpoint['service_stack_kind'], 'stack': endpoint['service_stack'],
                     'service': endpoint['service'], 'internal_mac': endpoint['mac'],
                     'internal_index': endpoint['internal_index'], 'source': endpoint['interface_source']}
            if value not in link['docker_services']:
                link['docker_services'].append(value)
    for link in interfaces:
        link['docker_services'].sort(key=lambda item: (item['stack'], item['service'], item['internal_mac']))
        link['docker_stacks'].sort(key=lambda item: (item['kind'], item['name']))
        for port in link.get('ports', []):
            peer = links.get(port['name'], {})
            port['docker_services'] = peer.get('docker_services', [])
            port['docker_stacks'] = peer.get('docker_stacks', [])


def docker_subnets(networks, names, addresses=()):
    """Return subnets only for Docker networks with a visible Linux interface."""
    result, docker_names = set(), set()
    for network in networks:
        name = network_interface(network, names)['interface']
        if name:
            docker_names.add(name)
            for config in (network.get('IPAM') or {}).get('Config') or []:
                try:
                    result.add(str(ipaddress.ip_network(config.get('Subnet', ''), strict=False)))
                except ValueError:
                    pass
    for address in addresses:
        if address.get('interface') in docker_names:
            try:
                result.add(str(ipaddress.ip_network(f"{address['address']}/{address['prefix_length']}", strict=False)))
            except (ValueError, KeyError):
                pass
    return sorted(result)
