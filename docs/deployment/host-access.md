# Host access and Compose source mounts

## Choose the visibility you need

The full [daemon example](compose.daemon.yaml) and [combined example](compose.all.yaml)
provide broad host visibility. These mounts are optional deployment choices, not
requirements for Docker API inventory. Disabling a component does not unmount its
files: remove the mount when that visibility is unnecessary.

The [Docker-focused example](compose.docker.yaml) runs both components without a
host-root or host-`/etc` bind, host networking, host PID namespace or added Linux
capabilities. It retains the Docker socket, application state, listener directory
and a dedicated managed Compose directory. It defaults to application read-only
access; set `BACKEND_DOCKER_ACCESS=rw` to enable supported changes.

This reduces directly exposed host files. **The Docker socket still grants
root-equivalent authority over its Engine host**, including the ability to create
containers with host mounts. Neither `cap_drop: ALL` nor a read-only socket bind
restricts the Docker API. Application `ro` mode is an application policy, not an
Engine-enforced security boundary. Keep the daemon listener private and protect
web accounts accordingly.

## Broad host mounts

| Mount / namespace | Purpose | Exposure and limitations | If omitted |
| --- | --- | --- | --- |
| Host `/` → `/host/root`, read-only, `rslave` | Host procfs/sysfs, OS metadata, mount identity and filesystem usage; also maps Compose paths reported by Docker. | Exposes readable host files, potentially credentials and application data. Read-only prevents ordinary writes through this bind, not disclosure or Docker-mediated writes. Recursive read-only behavior depends on the kernel/runtime; verify submount flags. `rslave` propagates host mount events into the container. | Docker API inventory still works. Host system statistics and external Compose-file reads cannot rely on this prefix. |
| Host `/etc` → `/host/etc`, read-only | Network manager files, Sysctl provenance and local Docker `daemon.json` evidence. | Exposes all readable host configuration under `/etc`, beyond the files required by those features. A root bind already exposes the same files at another path. | Docker API version/info remain available, but host daemon configuration and persistent network settings may be unavailable. |
| `pid: host` | Host process evidence and container network namespace/interface mapping. | Reveals host process information; does not itself mount Compose directories or authorize file reads. | Exact host veth/bridge-port association can be unavailable. Docker-reported network addresses remain available. |
| `network_mode: host`, `NET_ADMIN`, `NET_RAW` | Host network inventory, firewall and supported runtime mutations. | Shares the host network namespace and grants relevant network authority regardless of application access mode. | Docker API management remains possible through its Unix socket; host network control is unavailable. |

`/proc/mounts` or mountinfo describe mounts; they do not contain current free-byte
counts. Filesystem usage needs `statvfs` against a reachable path on each actual
host filesystem. This explains the full example's host-root bind; it does not make
that bind necessary for a Docker-only deployment. The daemon rejects container
filesystem statistics when they cannot be verified as the intended host mount.

## External Compose sources

Docker's Compose labels can report a source path such as
`/root/docker/bind9/docker-compose.yml` and a working directory. These are paths
on the Engine host; the Docker API does not return the file contents. The daemon
must separately have filesystem access to read them.

| Setting | Full example default | Meaning and interaction |
| --- | --- | --- |
| `BACKEND_SYSTEM_HOST_ROOT` | `/host/root` | Host filesystem prefix used for system inspection. With the default Compose host root `/`, this also becomes the external Compose source prefix. |
| `BACKEND_COMPOSE_HOST_ROOT` | `/` | Explicit external source prefix. A value other than `/` takes precedence over the system host root. This setting does not mount anything. |
| `BACKEND_COMPOSE_ALLOWED_ROOTS` | `/srv`, `/opt`, `/home`, `/media`, `/app` | Permitted original host paths for external Compose reads. This is an application allowlist, not container isolation. It does not grant filesystem permissions. |
| `BACKEND_COMPOSE_SEARCH_DIRECTORIES` | Empty list | Explicit fallback locations when Docker's source/working-directory labels are missing. Not needed for correctly labeled stacks; it does not recursively scan the whole host root. |
| `BACKEND_COMPOSE_HOST_READ_ONLY` | `true` | Prevents direct edits to external sources through the application. It does not prevent reads or definition overlays stored in the database. The filesystem mount remains independently read-only. |
| `BACKEND_COMPOSE_ROOT` | `/var/lib/lsdfm/compose` | Dedicated managed resources, described below. These are handled independently of the external-source prefix. |

For example, a full root bind maps the reported path above to
`/host/root/root/docker/bind9/docker-compose.yml`. Reading it also requires
`/root/docker` in the allowed roots and suitable filesystem permissions.

To expose only selected external projects, omit the root bind and bind a chosen
host tree, for example `/srv/compose` to `/external/srv/compose`, read-only. Set
`BACKEND_COMPOSE_HOST_ROOT=/external` and
`BACKEND_COMPOSE_ALLOWED_ROOTS=["/srv/compose"]`. Unrelated paths then have no
corresponding mount. Keep search directories empty unless fallback discovery is
needed. File-backed secrets/configs and Compose-relative resources can require
additional project mounts at their original host paths for deployment; source
inspection alone does not guarantee that deployment can read every resource.

These names are Compose substitution inputs. The full examples accept overrides for all application settings. Keep file paths
and mounts consistent; changing a path setting does not create a filesystem bind.
When using a custom ENV prefix, the effective daemon keys use that prefix.

## Dedicated directory for new managed Compose resources

A managed stack receives its own subdirectory under `BACKEND_COMPOSE_ROOT`.
Compose YAML/JSON, uploaded support files and generated resources can live there;
structured definitions and definition trees are persisted in the separate daemon
state database. Back up both locations.

The directory bind is read/write so the daemon can create and maintain these
resources after reviewed actions. **Use a dedicated directory**, not `/`, a home
directory, or a directory containing unrelated secrets. The bind exposes that
whole directory, so place only intended managed resources there. Creating a
stack does not deploy it.

Use the same absolute path on the host and inside the daemon. When Compose sends
an absolute bind source to the Docker Engine, the Engine resolves it on its own
host, not inside the daemon container. Identical paths keep relative bind mounts
from managed projects meaningful. For a remote Engine the files must also exist
on that Engine host; a daemon-local directory is not uploaded automatically.

## Run the Docker-focused example

From the checkout root, prepare `.env` as described in the
[deployment guide](README.md#prepare-configuration), including a unique web secret
and the correct HTTPS/public-URL settings. Prepare only these directories:

```bash
sudo install -d -m 0750 -o root -g 2000 lsdfm-run
sudo install -d -m 0700 -o root /var/lib/lsdfm/compose

docker compose --project-directory . --env-file .env \
  -f docs/deployment/compose.docker.yaml config --quiet
docker compose --project-directory . --env-file .env \
  -f docs/deployment/compose.docker.yaml up --build -d
```

Adjust the paths/group to `BACKEND_COMPOSE_ROOT`, `BACKEND_RUN_DIRECTORY` and
`BACKEND_SOCKET_GID` when customized. Network and System support are explicitly
disabled in both services, even if the general `.env` enables them. No host root
or `/etc` is mounted, and external source discovery is restricted. The example forwards ordinary application settings but fixes its component
switches, Unix transport and external-source isolation policy. These deliberate
profile restrictions take precedence over the general `.env` reference.

Containers, images, volumes, logs and Compose project grouping remain visible
through the Docker API. Existing stacks outside the managed directory can still
appear from labels, but their source definitions, metadata sections and
source-dependent lifecycle actions may be unavailable. The source-reading and
search settings are deliberately fixed in this example. New managed stacks can
be created and controlled with Docker write access. Host resource dashboards,
network control and exact host bridge-port mapping are not part of this variant.
