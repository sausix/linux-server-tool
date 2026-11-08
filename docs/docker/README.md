# Docker resources

The blue Docker category is **Docker monitoring** in read-only mode and
**Docker control** with write access. A Unix Docker socket grants root-equivalent
Engine access; the configured the application access mode governs what the application exposes.

## Overview and configuration

The overview shows active endpoint, connection status and version. Configuration
separates Engine-reported values, local daemon.json contents, running dockerd
binding arguments and available systemd socket/service evidence. It reports a
missing, unreadable or invalid daemon.json explicitly and redacts sensitive values.

`-H`/`--host` and JSON `hosts` can be duplicate settings that prevent Docker from
starting. Config files and unit definitions are evidence, not proof of the values
loaded at process startup. Remote endpoints do not inherit local host evidence.

## Stacks and containers

Stacks include labelled Compose projects and Swarm stacks, stopped containers,
and remaining labelled networks/volumes. Managed files and stored definitions can
remain visible without running resources. Stack names open details; the expandable
Stacks menu also links directly to them. State/kind text sets filters.

| Observed state | Background |
| --- | --- |
| Running / healthy | Blue |
| Starting | Yellow |
| Empty | Gray |
| Partial/degraded, unhealthy, failed | Increasing red tones |
| Stopped | Standard background |

Stack details group containers under their services and show networks/endpoints,
volumes and Compose definition information. Network endpoint tables distinguish
internal and port MACs, IPv4/IPv6 prefixes/gateways and mapped host interfaces.
Unproven interface associations remain unknown.

Containers are sorted by stack and name. Names open details with image, state,
health, timestamps, restart policy, networks, mounts, volumes and nonempty labels.
Environment variables, command arguments and health-check output are not exposed
by these views. Containers can be filtered with `container.stack=demo`,
`container.state=running`, `container.health=unhealthy`, labels or free text.
See [stack files and controls](stacks.md) for lifecycle operations.

## Images

Images show tags, observed references and storage sizes. Active references use
blue, stopped/service/child-image references use amber, unused images use gray.
Incomplete reference reads leave usage unknown. Engine-reported parent/child
dependencies count as references; shared layers alone do not imply that relation.

Details link to containers, stacks, services and related images. Immutable IDs are
preferred for container image references. Remote worker images and Compose files
on disk are not a complete image-reference inventory.

Storage statistics load separately so a slow accounting query does not block
the list. Endpoint totals remain independent of list filters. Logical image sizes
can count shared layers repeatedly; exclusive bytes and unused-image totals are
not a guarantee of space reclaimed by deletion. Refresh the statistics explicitly
after inventory changes.

In write mode, checkboxes and top/bottom toolbars select visible rows or review
all unused images. Every deletion requires confirmation of the exact IDs/tags
and a fresh reference check. Used, unknown or parent images with dependent
children are rejected. No force or automatic parent-image pruning is used.
Failed entries retain their detail links; successfully deleted entries do not.

## Volumes

Volumes have a general inventory and appear in stack/container details. Names
open details with labels, driver options/status, mountpoint, creation time,
references, accounting and the complete volume inspect response.

| Filter | Meaning |
| --- | --- |
| `volume.name=data` | Volume name |
| `volume.container=web` | Referencing container name or full ID |
| `volume.stack=demo` | Owning or referencing stack |
| `volume.service=web` | Compose or Swarm service |
| `volume.usage=unused` / `volume.state=unused` | Observed usage |
| `volume.anonymous=true` | Presence of Docker's anonymous-volume label |
| `volume.driver=local` / `volume.scope=local` | Driver / scope |
| `label.key=owner label.value=ops` | Labels on the same volume |

Active volumes use blue; referenced volumes amber; unused volumes gray. Stopped
containers and Swarm service specifications still count as references. Incomplete
inventory becomes unknown, not unused. Compose files alone do not reserve volumes.
Usage refers to containers visible to this Engine and service specifications on
a manager, not an audit of every remote node.

Volumes sort by name with anonymous volumes last. An empty value of
`com.docker.volume.anonymous` still marks the volume anonymous; older Engines or
Compose versions may omit that label. Hexadecimal names alone are not treated
as proof. Container/service/stack text sets volume filters; adjacent icons jump
to the related resource.

Size comes from Engine accounting when available. Mountpoints are not traversed
to estimate it. Storage refresh is separate from inventory. Write-mode selection,
**Delete selected**, **Delete unused** and per-row Delete open confirmations.
Deleting an unused volume permanently removes its data; references and reviewed
identity are checked again, with no force deletion.

## Labels

Labels group by key and then value, with per-resource-type counts. Positive counts
open the relevant overview with both label key/value filters. Empty values can
be searched with `q=label.value=`. Unsupported manager-only sources report
incomplete counts rather than claiming the resources are absent.

## Log viewer

Logs buttons open a separate window for a stack, service or container. The
default is 200 lines; change it with `BACKEND_DOCKER_LOG_DEFAULT_LINES`.
**Fetch last lines** loads history. Enabling **Live** fetches current history first
and then appends output, retaining the configured limit as a rolling buffer.

Names have distinct colors and timestamps; STDERR timestamps are red when Docker
provides a separate stream. TTY containers combine STDOUT/STDERR. Scrolling away
from the end pauses auto-scroll while output continues to arrive.

Live stack/service logs discover newly created, restarted and recently finished
containers through events plus fallback checks. Reconnects use timestamp-based
positions. Docker retention limits still apply, and deleted containers cannot be
read. Some logging drivers do not support history retrieval. Manager-visible
service counts do not make remote worker log streams locally available.

Docker `ro` is sufficient for logs. The viewer's Live control is independent of
the session's inventory refresh preference. Close the window or turn Live off to
release streams. Multi-worker web deployments need session affinity for transient
history checkpoints and confirmation previews.
