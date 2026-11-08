# System control

The orange System control category contains **Configuration**, **Sysctl** and
the system overview. Access is controlled by `BACKEND_SYSTEM_ACCESS`; networking
can be disabled independently.

## Resource overview

The overview shows OS/kernel/architecture, uptime, RAM/swap, CPU load and logical
core usage, disks/partitions and block-backed filesystem mounts. CPU usage needs
two samples. Resource polling runs every five seconds while Live refresh is on.

RAM's stacked bar uses disjoint used, buffer, cache/reclaimable-slab and free
segments. **Available** is a kernel estimate that overlaps these categories and
is not an extra segment. Swap has a separate bar when configured.

**CPU** starts below OS/memory and shows separate 1/5/15-minute load bars on a
shared scale. Load counts runnable and uninterruptible tasks; it is not CPU
usage and may exceed the logical CPU count. **CPU core usages** follows with
full-width busy/idle bars beside per-core percentages.

Block-mount rows are grouped by device path. Each device contains its mounts,
filesystems and individual usage bars; Btrfs can have several mounts on the same
device. Used, reserved/unavailable and available segments account for capacity.
Do not sum capacities of mounts that share storage.

Only block-backed mounts are included. The supplied daemon Compose service reads
the host through a read-only root bind. Missing or mismatched host mounts remain
unknown rather than showing container usage; Btrfs validation includes subvolume
roots. See [mount configuration](../deployment/README.md#daemon-mounts).

## Configuration review

The read-only Configuration page explains these initial settings:

| Setting | Subject |
| --- | --- |
| `kernel.dmesg_restrict` | Kernel log access |
| `kernel.kptr_restrict` | Affected kernel pointer output |
| `kernel.unprivileged_bpf_disabled` | Unprivileged BPF use |
| `kernel.perf_event_paranoid` | Performance monitoring access |
| `kernel.yama.ptrace_scope` | Process tracing restrictions |

Possible values appear in adjacent boxes; the current value is highlighted.
Missing kernel features remain unavailable. Distribution-specific values are
shown without assuming undocumented behavior. Effects and compatibility tradeoffs
are explained alongside links to upstream documentation. A selected value is an
observation, not a security certification or a universal recommendation.

## Sysctl

Sysctl lists the available network kernel tunables with current values,
classifications, candidate persistent assignments and file/line sources.
`BACKEND_SYSCTL_ACCESS` controls the viewer under the system parent mode.
The current interface is read-only even when configured `rw`.

Examples:

```text
sysctl.key=net.ipv4.ip_forward
prefix=net.ipv6.route
sysctl.interface=eth0
class=routing
class=socket-buffer
prefix=net.core
```

Core socket tunables include `rmem_max`, `rmem_default`, `wmem_max`, `wmem_default`
and `optmem_max`, with TCP/UDP memory controls and queue/receive-budget settings.
Byte and page units are labelled where applicable.

| State | Meaning |
| --- | --- |
| `configured` | Live value matches the inspected persistent candidates. |
| `changed` | Live value differs from candidates, or from a known local build default when unconfigured. |
| `default` | Matches a known local build default, or has no persistent candidate and an unknown original default. Read the source note to distinguish these cases. |

Unknown defaults display `?`. Only stable defaults available from local kernel
configuration are shown; runtime values are not assumed to be defaults.
Whitespace in vector values is normalized for comparisons.

Source discovery checks sysctl.d directories and sysctl.conf. The systemd and
procps candidates are separate because legacy sysctl.conf handling differs.
File declarations are candidates, not proof of what was loaded; managers and
runtime programs can change values afterward. Host bind prefixes are removed
from displayed paths while actual file operations retain their original paths.

When both global forwarding settings are enabled without a persistent declaration
for a setting, a reachable local Docker endpoint can produce **Configuration
Source: Docker daemon (inferred)**. Explicitly disabled Docker forwarding or a
configured Docker nftables backend suppresses this inference. Live values do not
record who originally wrote them.

Legacy `/network/sysctl` page links redirect to `/system/sysctl` and retain filters.
