# Network control

The green network category is called **Network monitoring** in read-only mode
and **Network control** when supported writes are enabled. Configuration is its
first subpage, followed by Interfaces, Routing and Firewall.

## Configuration review

The initial read-only review covers IPv4 forwarding, global IPv6 forwarding and
Router Advertisements on `ens3`:

| Setting | Purpose |
| --- | --- |
| `net.ipv4.ip_forward` | Forwarding between IPv4 interfaces |
| `net.ipv6.conf.all.forwarding` | Global IPv6 router behavior |
| `net.ipv6.conf.ens3.accept_ra` | Kernel RA acceptance on ens3, including its interaction with per-interface forwarding |

Adjacent value boxes explain the effects; the current value is highlighted.
An absent ens3 is shown as unavailable rather than silently substituted with
another interface. No settings are written. Forwarding can be necessary for
gateways and routed/NAT container networks; enabling it does not replace a
firewall policy. Persistent file sources and inferred runtime sources are shown
where available. General Sysctl browsing lives under [System control](../system/README.md).

## Overview and interface scopes

The network overview shows compact name/MAC/state boxes. Loopback is first,
followed by other standalone interfaces and then bridges. Bridge ports appear
only inside their bridge box and wrap to fit available width.

| Scope / filter | Meaning | Accent |
| --- | --- | --- |
| `interface.scope=public` | At least one assigned IPv4/IPv6 address is neither loopback nor private under Python IP classification. | `#51eb00` |
| `interface.scope=private` | Addressed interface with no public address and not exclusively loopback. | Network green |
| `interface.scope=loopback` | All assigned addresses are loopback, or an unaddressed link has the loopback flag. | Gray |
| `interface.scope=unknown` | No valid assigned address; shown as **No address**. | Neutral |

Public takes precedence on mixed interfaces. IPv4-mapped IPv6 uses the embedded
IPv4 classification. Persistent-only addresses do not imply current exposure.
Scope and Up/Down badges set filters when clicked.

## Interface details and address changes

Interfaces show type, flags, MTU, MAC, master, assigned addresses and persistent
configuration evidence. Address type labels distinguish IPv4 private/public,
loopback, multicast, link-local, and IPv6 ULA/global/transition classes. Public
address/type text uses the public accent. Blue Docker links identify observed
stack/interface associations and matching Docker subnets.

Bridge ports show state, MAC and flags, with service/internal-MAC information
when mapping is available. Container veth mapping uses namespace/interface-index
evidence; different internal/external MACs are not assumed equal.

**Add address** validates address family and required prefix before a review
shows subnet details. **Delete** opens a confirmation page. Runtime addresses can
offer **Make persistent** when the configuration source is known. Persistent
addresses missing at runtime show **Runtime deleted** and can offer removal from
configuration. Supported persistent edits are limited to the application-owned files.

Runtime changes can be replaced by a network manager or reboot. Inspect the
preview before confirmation. Persistent host `/etc` is read-only in the supplied
containers; full manager activation is best handled by a native daemon with the
required tools and path permissions. systemd-networkd and ifupdown are supported.

## Search and links

```text
interface.name=eth0
interface.scope=public
interface.state=up
interface.subnet=192.168.1.0/24
interface.ip=192.168.1.50
interface.ip4=192.168.1.50
interface.mac=00:11:22:33:44:55
interface.flag=up
```

`interface.ip`, `.ip4` and `.ip6` match interfaces containing that address in an
assigned or configured subnet. `interface.subnet` compares the normalized network
and prefix. Combine conditions with spaces (AND), quote values containing spaces,
or use free text. The `?` beside the filter field shows fields relevant to the page.
Matching fields are highlighted.

## Routing

Routing shows IPv4/IPv6 route tables, destinations, gateways, interface, metric,
scope, protocol and type. Family, table, type, address and interface text set local
route filters. Adjacent icons jump to matching interfaces or Docker stacks.
For example, use `route.family=ipv6`, `route.interface=eth0` or
`route.subnet=192.168.1.0/24`.

## Firewall

Tables and chains contain normalized rules with match expressions, actions,
jump targets and packet/byte counters. Interface names are linked inside match
expressions. Unsupported complex expressions retain text and expandable Raw
details; absent nft xt extension parameters are not invented.

Select a reader with `BACKEND_FIREWALL_BACKEND`. Auto prefers nonempty results;
an empty nftables ruleset does not hide populated legacy iptables rules. Both
independent rulesets can be shown. Explicit backend selection does not silently
fall back when empty/unavailable. Use explicit `iptables-legacy` or
`iptables-nft` when container alternatives differ from the host.

Live counter/rule updates preserve open Raw sections and selections in unchanged
text. Native nft monitoring pushes events; legacy/iptables inventories are checked
periodically. The current UI displays firewall rules read-only.
