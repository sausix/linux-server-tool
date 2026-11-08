"""Read-only review of a fixed, documented set of Linux kernel settings."""

import fnmatch
from pathlib import Path

from backend.system.sysctl import assignments, normalize_key, normalize_value, SYSCTL_DIRECTORIES


NETWORK = 'https://docs.kernel.org/networking/ip-sysctl.html'
KERNEL = 'https://docs.kernel.org/admin-guide/sysctl/kernel.html'
PERF = 'https://docs.kernel.org/admin-guide/perf-security.html'
YAMA = 'https://docs.kernel.org/admin-guide/LSM/Yama.html'


def option(key, title, choices, documentation, note=''):
    return {'key': key, 'title': title, 'choices': [{'value': str(value), 'explanation': text} for value, text in choices],
            'documentation': documentation, 'note': note}


CATALOG = {
    'network': [
        option('net.ipv4.ip_forward', 'IPv4 forwarding', [
            (0, 'IPv4 packets are not routed between interfaces. A host-only setup can use this; routed container networks and gateways may need forwarding.'),
            (1, 'IPv4 packets can be routed between interfaces, subject to routes and firewall rules. Gateways and routed or NAT container networks commonly need this.')], NETWORK + '#ip-forward',
            'Changing this value resets IPv4 parameters to host/router defaults. Forwarding is not a firewall policy.'),
        option('net.ipv6.conf.all.forwarding', 'IPv6 forwarding', [
            (0, 'Global IPv6 forwarding is disabled and host behavior is selected. Review per-interface forwarding and Router Advertisement settings as well.'),
            (1, 'Global IPv6 forwarding is enabled with router behavior. Router Advertisements normally require accept_ra=2 on interfaces that still learn upstream routes.')], NETWORK,
            'Global forwarding affects interface forwarding behavior; accepting upstream advertisements may still be necessary.'),
        option('net.ipv6.conf.ens3.accept_ra', 'Router Advertisements · ens3', [
            (0, 'The kernel ignores Router Advertisements on ens3. Addresses and routes must come from other configuration.'),
            (1, 'The kernel accepts Router Advertisements only while forwarding is disabled on ens3. Router mode suppresses acceptance.'),
            (2, 'The kernel accepts Router Advertisements even with forwarding enabled. This supports an IPv6 router learning its upstream configuration.')], NETWORK,
            'This reviews ens3 specifically. Network managers can implement RA handling in userspace; this value alone does not prove route availability.'),
    ],
    'system': [
        option('kernel.dmesg_restrict', 'Kernel log access', [
            (0, 'Unprivileged kernel-log access is permitted by this setting.'),
            (1, 'Kernel-log access requires CAP_SYSLOG. Unprivileged diagnostics are restricted.')], KERNEL + '#dmesg-restrict'),
        option('kernel.kptr_restrict', 'Kernel pointer exposure', [
            (0, 'Kernel pointer output uses the normal hashed-pointer behavior.'),
            (1, '%pK pointers are hidden unless CAP_SYSLOG and matching real/effective credentials permit disclosure.'),
            (2, '%pK pointers are hidden regardless of privileges.')], KERNEL + '#kptr-restrict',
            'This controls affected pointer output, not every possible kernel address disclosure.'),
        option('kernel.unprivileged_bpf_disabled', 'Unprivileged BPF', [
            (0, 'Unprivileged bpf() calls are allowed by this setting.'),
            (1, 'Unprivileged bpf() calls are disabled until reboot; this setting cannot be cleared in the running kernel.'),
            (2, 'Unprivileged bpf() calls are disabled but an administrator can change the setting later.')], KERNEL + '#unprivileged-bpf-disabled',
            'CAP_BPF or CAP_SYS_ADMIN can permit privileged BPF use.'),
        option('kernel.perf_event_paranoid', 'Performance monitoring access', [
            (-1, 'No perf-specific scope restrictions are imposed on unprivileged monitoring. Other access checks can still apply.'),
            (0, 'Unprivileged monitoring can include system-wide events, excluding raw/ftrace tracepoints. Both user and kernel execution can be profiled.'),
            (1, 'Unprivileged monitoring is limited to per-process events. Kernel execution can still be profiled.'),
            (2, 'Unprivileged profiling is limited to per-process user-space execution. Kernel profiling is excluded.')], PERF,
            'CAP_PERFMON grants privileged monitoring access. Distribution-specific values above 2 may add restrictions; their exact meaning is not assumed here.'),
        option('kernel.yama.ptrace_scope', 'Process tracing policy', [
            (0, 'Classic ptrace checks allow attaching to dumpable processes with the same UID. Applications may restrict access further.'),
            (1, 'Attaching normally requires a descendant relationship under classic checks. PR_SET_PTRACER can authorize another debugger; CAP_SYS_PTRACE can bypass the restriction.'),
            (2, 'ptrace operations require CAP_SYS_PTRACE, including tracing child processes. Ordinary debugging and crash handlers may stop working.'),
            (3, 'Attaching and PTRACE_TRACEME are disabled for all processes. This mode cannot be relaxed in the running kernel.')], YAMA,
            'This option exists only when Yama support is available.'),
    ],
}


def snapshot(category, settings):
    if category not in CATALOG:
        raise ValueError('Unknown configuration category')
    host_root = settings.system_host_root
    proc_root = host_root / 'proc/sys' if host_root else Path('/proc/sys')
    if host_root:
        sources = {'directories': [host_root / path.relative_to('/') for path in SYSCTL_DIRECTORIES],
                   'legacy': host_root / 'etc/sysctl.conf'}
    else:
        sources = settings.sysctl_sources
    declarations, errors = assignments(**sources, include_kernel=True)
    items = []
    for definition in CATALOG[category]:
        key = normalize_key(definition['key'])
        path = proc_root / key
        value, error = None, None
        try:
            value = path.read_text().strip()
        except FileNotFoundError:
            error = 'Not available: this kernel option or interface is absent.'
        except (OSError, UnicodeError) as exc:
            error = f'Unable to read this setting: {exc}'
        exact = [item for item in declarations if item['key'] == key]
        matching = exact or [item for item in declarations if len(key.split('/')) == len(item['key'].split('/')) and all(
            fnmatch.fnmatchcase(part, pattern) for part, pattern in zip(key.split('/'), item['key'].split('/')))]
        candidates = [item for item in matching if item['value'] is not None]
        systemd = [item for item in candidates if not item['legacy']]
        effective = [group[-1] for group in (systemd, candidates) if group]
        state = 'unknown' if value is None else 'unconfigured' if not effective else 'configured' if all(
            normalize_value(item['value']) == normalize_value(value) for item in effective) else 'changed'
        items.append({**definition, 'value': value, 'path': str(path), 'error': error, 'state': state,
                      'known_value': any(choice['value'] == value for choice in definition['choices']),
                      'sources': matching, 'systemd_candidate': systemd[-1] if systemd else None,
                      'procps_candidate': candidates[-1] if candidates else None})
    if category == 'network':
        try:
            forwarding = (proc_root / 'net/ipv6/conf/ens3/forwarding').read_text().strip()
        except OSError:
            forwarding = None
        ra = items[2]
        if ra['value'] == '1' and forwarding == '1':
            ra['assessment'] = 'accept_ra=1 with ens3 forwarding=1: kernel Router Advertisement acceptance is suppressed.'
        elif forwarding is not None:
            ra['assessment'] = f'ens3 forwarding={forwarding}. Review upstream IPv6 addressing and routes before changing RA behavior.'
    return {'category': category, 'items': items, 'errors': errors, 'read_only': True,
            'note': 'Live values are read from procfs. File declarations are persistent candidates, not proof of what was applied. Network values follow the daemon network namespace; host networking is required to inspect host network settings from a container. No values are changed and no kernel defaults are assumed.'}
