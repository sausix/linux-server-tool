# Daemon environment reference

The [complete native ENV reference](../../backend/systemd/backend.env.example)
lists all options with their native defaults. A deployment file only needs
overrides; omitted files and unchanged references have identical Settings.

These tables describe `backend`. Environment names start with `BACKEND_`.
Command-line values override environment values; see `backend --help` for flags.
When started directly, the daemon also loads `.env` from its working directory.
A systemd unit uses its configured EnvironmentFile. Compose substitution reads
its `--env-file`; only variables passed into the container reach the daemon.

The supplied native environment example uses `/run/lsdfm` and `/var/lib/lsdfm/backend`.
Standalone defaults use the same stable storage namespace. Existing deployments
should explicitly retain their paths until deliberately moved; see
[existing state](../common/configuration.md#existing-state-when-adopting-the-new-deployment-layout).

**Default** means the daemon's own default. The Compose examples deliberately
override several defaults; their values are stated separately. Paths inside a
container must refer to mounted container paths, not merely existing host paths.
Lists use JSON array syntax in environment files.

## Listener and client authentication

| Variable | Default | Component | Required or used when | Meaning |
| --- | --- | --- | --- | --- |
| `BACKEND_TRANSPORT` | `unix` | Listener | Always | `unix` or `tcp`; examples use Unix sockets. |
| `BACKEND_UNIX_SOCKET` | Storage-ID-scoped path | Unix listener | Transport is `unix` | Socket filename. Frontend endpoint must point to the same socket. |
| `BACKEND_SOCKET_MODE` | `0660` | Unix listener | Transport is `unix` | Octal permissions; socket ownership comes from the service user/group. |
| `BACKEND_LISTEN_HOST` | `127.0.0.1` | TCP listener | Transport is `tcp` | Address to bind. Not used for Unix sockets. |
| `BACKEND_LISTEN_PORT` | `9876` | TCP listener | Transport is `tcp` | Port, 1–65535. |
| `BACKEND_ACCESS_TOKEN` | Unset | Client authentication | Required for TCP; optional for Unix | At least 32 characters for TCP. Match `FRONTEND_DAEMON_TOKEN` in the web application. Prefer environment files over command-line secrets. |
| `BACKEND_MAX_CLIENTS` | `64` | Listener | Always | Concurrent client limit, 1–1024; subscriptions and log streams hold connections. |

TCP authentication does not encrypt traffic. Keep the listener on loopback or
inside a protected tunnel/private transport. The full Compose profiles default to Unix transport and forward listener
overrides. Their health check reads the effective settings. When changing
transport, configure a reachable frontend endpoint and the same token on both
sides; a path setting alone does not add a mount. The Docker-focused profile
intentionally retains its fixed Unix transport.

## Component and feature access

Parent switches accept `disabled`, `ro`/`readonly` and `rw`/`readwrite`.
Feature switches accept `disabled`, `ro` and `rw`. Parent read-only access caps
the corresponding child at read-only. Disabled features reject access and stop
their component monitors.

| Variable | Daemon default | Compose value | Component | Required or used when |
| --- | --- | --- | --- | --- |
| `BACKEND_DOCKER_ACCESS` | `readonly` | `readonly` | Docker | Enable inventories and logs with `ro`; supported confirmed mutations require `rw`. |
| `BACKEND_NETWORK_ACCESS` | `readonly` | `readonly` | Network parent | Caps Interfaces, Routing and Firewall; `disabled` blocks network access. |
| `BACKEND_SYSTEM_ACCESS` | `readonly` | `readonly` | System parent | Controls system resources and caps Sysctl independently of networking. |
| `BACKEND_INTERFACES_ACCESS` | `rw` | `rw` (parent-capped) | Interface inventory and addresses | Effective writes also require network parent `rw`; OS permissions still apply. |
| `BACKEND_ROUTING_ACCESS` | `rw` | `rw` (parent-capped) | Routing | Effective route writes also require network parent `rw`. |
| `BACKEND_FIREWALL_ACCESS` | `ro` | `ro` | Firewall | Inventory and monitoring; the current web interface has no firewall rule editor. |
| `BACKEND_SYSCTL_ACCESS` | `ro` | `ro` | Sysctl | Caps the general Sysctl viewer under System control. No Sysctl writes are currently exposed. |

Configuration review pages are read-only under their category's parent access
switch, including when that parent is `rw`. Write access is permission to use
supported operations, not a guarantee that a write operation exists for every
feature. UI mutations additionally require an administrator with a current
password and confirmation.

## Docker endpoint and TLS

| Variable | Default | Component | Required or used when | Meaning |
| --- | --- | --- | --- | --- |
| `BACKEND_DOCKER_ENDPOINT` | `unix:///var/run/docker.sock` | Docker client | Docker access enabled | Unix socket or `tcp://`, `http://`, `https://` endpoint. Compose defaults to `unix:///run/docker.sock`. |
| `BACKEND_DOCKER_API_VERSION` | `auto` | Docker client | Docker access enabled | Negotiate Engine API, or pin a value such as `v1.43`. |
| `BACKEND_DOCKER_TIMEOUT_SECONDS` | `5` | Docker client | Ordinary Engine requests and stream setup | Timeout in seconds, greater than 0 and at most 120. Idle event/log streams remain open. |
| `BACKEND_DOCKER_STORAGE_TIMEOUT_SECONDS` | `30` | Image/volume storage accounting | Storage statistics requested | Timeout in seconds, greater than 0 and at most 600. Keep the frontend storage timeout higher. |
| `BACKEND_DOCKER_TLS_VERIFY` | `true` | Docker TLS | TLS endpoint | Verify peer certificate/hostname. Configure trust instead of disabling verification. |
| `BACKEND_DOCKER_TLS_CA` | Unset | Docker TLS | Private/custom CA | PEM CA certificate path readable inside the daemon environment. |
| `BACKEND_DOCKER_TLS_CERT` | Unset | Docker mutual TLS | Engine requires client certificate | PEM client certificate. Must be supplied together with TLS key. |
| `BACKEND_DOCKER_TLS_KEY` | Unset | Docker mutual TLS | Client certificate supplied | Matching private key; mount read-only and restrict filesystem permissions. |

The supplied Compose files target a local Unix Engine. For a remote TLS endpoint,
set the forwarded TLS variables, provide readable certificate files or read-only mounts,
and remove an unnecessary local Docker socket bind. Local process/configuration
evidence and host-interface mapping cannot identify a remote Engine's state.

## Docker log viewer

| Variable | Default | Component | Required or used when | Meaning |
| --- | --- | --- | --- | --- |
| `BACKEND_DOCKER_LOG_DEFAULT_LINES` | `200` | Log history/live viewer | Viewer opens without explicit line limit | Default lines; 1–20000 and no greater than maximum lines. |
| `BACKEND_DOCKER_LOG_MAX_LINES` | `5000` | Log history/live viewer | Any log request | Maximum requested line count; 1–20000. |
| `BACKEND_DOCKER_LOG_MAX_STREAMS` | `32` | Stack/service log fan-out | Multiple containers are selected | Maximum streams per viewer; 1–128. |
| `BACKEND_DOCKER_LOG_DISCOVERY_INTERVAL_SECONDS` | `2` | Live stack/service logs | Live mode | Fallback discovery interval, 0.05–60 seconds; Docker events also trigger discovery. |

## Stack definitions, source paths and controls

| Variable | Default | Component | Required or used when | Meaning |
| --- | --- | --- | --- | --- |
| `BACKEND_STACK_DATABASE` | Storage-ID-scoped path | Stack definitions | Stored/structured definitions used | Absolute persistent SQLite filename; the service needs write access. |
| `BACKEND_COMPOSE_ROOT` | Storage-ID-scoped path | New managed stacks | Creating or finding managed file stacks | Absolute dedicated directory; `/` is rejected. Each new stack gets a subdirectory. Compose uses the same configured absolute path for the environment value, host bind source and container bind target. |
| `BACKEND_COMPOSE_HOST_ROOT` | `/` | External Compose sources | Reading existing host projects through a bind prefix | A dedicated daemon-visible prefix representing host `/`. With `/`, automatically reuse `BACKEND_SYSTEM_HOST_ROOT` when configured; otherwise use the local filesystem. Does not add a mount or bypass allowed roots. |
| `BACKEND_COMPOSE_HOST_READ_ONLY` | `true` | External source editing | External Compose sources | Keep existing host projects read-only. `false` permits permission-checked edits only with Docker write access and a writable mount. |
| `BACKEND_COMPOSE_ALLOWED_ROOTS` | `["/srv","/opt","/home","/media","/app"]` | Source access | Existing Compose files read/edited | Allowed absolute host paths. This is an allowlist, not a directory mount or automatic recursive search. |
| `BACKEND_COMPOSE_SEARCH_DIRECTORIES` | `[]` | Stack discovery | Find additional local YAML/JSON projects | Explicit absolute host search directories, also bounded by allowed roots. Leave empty if only discovered/managed projects are needed. |
| `BACKEND_COMPOSE_COMMAND` | `["docker","compose"]` | Compose lifecycle | Start/stop/restart file-backed Compose stacks | Executable argument list; the command must exist in the daemon environment. |
| `BACKEND_STACK_CONTROL_TIMEOUT_SECONDS` | `120` | Lifecycle jobs | Start/stop/restart running | Timeout per command, greater than 0 and at most 1800 seconds. |
| `BACKEND_STACK_MAX_CONCURRENT_JOBS` | `2` | Lifecycle jobs | Multiple control requests | Maximum concurrent jobs, 1–32. |
| `BACKEND_STACK_STOP_TIMEOUT_SECONDS` | `10` | Compose stop/restart | Stop timeout passed to Compose | Grace period in seconds, 1–300. |

For source mapping and relative bind mounts, see [Compose mounts](../deployment/README.md#daemon-mounts)
and [stack files](../docker/stacks.md). Keep the managed host path and the path seen by the
Docker Engine consistent when stack definitions contain relative bind mounts.

## Linux resources, network managers and firewall

| Variable | Default | Component | Required or used when | Meaning |
| --- | --- | --- | --- | --- |
| `BACKEND_SYSTEM_HOST_ROOT` | Unset | Host resources, mount identity and Sysctl provenance | Daemon runs in a container with a host-root bind | Absolute daemon-visible host root. Compose sets `/host/root`; native services normally leave unset. Procfs network settings still use the daemon network namespace. |
| `BACKEND_HOST_ETC_DIRECTORY` | `/etc` | Host configuration evidence | Host `/etc` is mounted under another prefix | Compose sets `/host/etc`. Supplies Docker daemon.json and sysctl sources; with a host root, vendor/runtime sysctl directories are also considered. |
| `BACKEND_NETWORKD_DIRECTORY` | `/etc/systemd/network` | systemd-networkd | Backend detection/configuration | Compose sets `/host/etc/systemd/network` for read-only host evidence. Supported persistent writes need a native writable path and manager access. |
| `BACKEND_INTERFACES_FILE` | `/etc/network/interfaces` | ifupdown | Backend detection/configuration | Compose sets `/host/etc/network/interfaces`; associated snippets are found relative to this file. |
| `BACKEND_FIREWALL_BACKEND` | `auto` | Firewall readers/monitors | Firewall enabled | `auto`, `nftables`, `iptables`, `iptables-legacy`, `iptables-nft`. Select explicitly if host/container alternatives differ. |
| `BACKEND_COMMAND_TIMEOUT_SECONDS` | `15` | Manager, firewall and inspection tools | External tool execution | Timeout in seconds, greater than 0 and at most 300. |

## Events and logging

| Variable | Default | Component | Required or used when | Meaning |
| --- | --- | --- | --- | --- |
| `BACKEND_EVENTS_ENABLED` | `true` | Inventory subscriptions | Live inventory updates | Enables daemon event sources; enabled feature switches determine which sources start. Log viewer Live is a separate control. |
| `BACKEND_LOG_LEVEL` | `INFO` | Service diagnostics | Always | `DEBUG`, `INFO`, `WARNING`, `ERROR`. Tool/access failures are reported to stderr/journal. |

Compose-only variables such as `BACKEND_SOCKET_GID` and `BACKEND_DOCKER_SOCKET_PATH`
are not daemon settings. See their separate [Compose table](../deployment/README.md#compose-substitution-variables).

## Shared component switches

These names are identical for the web application and daemon, without an the application/backend prefix.
They accept true/false and default to true for existing deployments. CLI overrides environment values.
A false switch takes precedence over component read/write access settings.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DOCKER_SUPPORT` | `true` | Load Docker support; false disables its daemon API, event sources and web routes. |
| `NETWORK_SUPPORT` | `true` | Enable network access and UI; does not control System/Sysctl. |
| `SYSTEM_SUPPORT` | `true` | Enable system access, including Sysctl, independently of networking. |

## Configurable environment prefix

The tables use the standard example prefix. See [environment namespaces](../common/configuration.md)
for per-component selectors, CLI precedence, compatibility fallbacks and Compose
input translation. Custom namespaces do not inherit legacy secrets.
