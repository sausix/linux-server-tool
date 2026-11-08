# Docker Compose deployment

## Choose a Compose file

| File | Services | When to use |
| --- | --- | --- |
| [compose.docker.yaml](compose.docker.yaml) | `frontend`, `backend` | Docker-focused deployment with minimal mounts; no host-root or external Compose source discovery. |
| [compose.all.yaml](compose.all.yaml) | `frontend`, `backend` | Both components on the same server; web waits for daemon health. |
| [compose.web.yaml](compose.web.yaml) | `frontend` | Connect to an existing native/container daemon. |
| [compose.daemon.yaml](compose.daemon.yaml) | `backend` | Provide system access for a separately running web application. |

All commands below run from the application checkout directory. Always pass
`--project-directory .`: it anchors build contexts and relative bind paths to
that directory even though the Compose files live below `docs/deployment`.
The combined file's `extends` paths also use that explicit project directory.

## Storage namespace

The stable filesystem namespace is `lsdfm`, independently of the UI name and
frontend/backend component names. Host paths and container targets use
`/run/lsdfm`, `/var/lib/lsdfm/backend` and `/var/lib/lsdfm/compose`. The local
listener bind defaults to `./lsdfm-run`. Existing deployments must retain or
deliberately move their paths; files and volumes are never migrated automatically.
See [existing state](../common/configuration.md#existing-state-when-adopting-the-new-deployment-layout).

## Shared image

The shared `docker/Dockerfile` builds one image containing `frontend`, `backend` and their
shared library. All public Compose variants use that Dockerfile and the same
`APPLICATION_IMAGE` tag (default `application:local`). The default command is `frontend`;
the daemon service explicitly runs `backend` as root with its listed capabilities.
The combined deployment runs two containers, not a supervisor with two services
inside a single container. The web process stays unprivileged and does not receive
the daemon's host mounts.

Python 3.14 and the committed `uv.lock` provide the application dependency set.
The build excludes development dependencies and installs packages non-editably.
The entrypoint uses a private file-creation mask. A source allowlist in
the build-context root `.dockerignore` excludes local `.env`, databases, test deployments and credentials.
Base images and Debian OS packages still require normal maintenance and updates.

Build directly from the checkout root with:

```bash
docker build -f docker/Dockerfile -t application:local .
```

The build context remains the checkout root. Dockerfile and entrypoint are
centralized under `docker/`; frontend and backend use the same image. The single
`.dockerignore` stays at the context root so both legacy Docker builders and
BuildKit exclude local secrets and runtime data.

## Prepare configuration

```bash
# Create an overrides-only file with a unique production secret.
python3 -c 'import secrets; print("FRONTEND_SECRET_KEY=" + secrets.token_urlsafe(48))' > .env
chmod 600 .env
```

The [complete ENV reference](compose.env.example) lists every application option
and Compose input. Its values match the Compose defaults; copying it unchanged
does not enable additional features or change HTTPS behavior. Prefer a short
`.env` containing only overrides. Native defaults are documented separately in
the [web reference](../frontend/frontend.env.example) and
[daemon reference](../../backend/systemd/backend.env.example), because container
listener addresses, database paths and host mappings deliberately differ.

A missing `.env` is allowed: omit `--env-file .env` and supply the required
`FRONTEND_SECRET_KEY` through the shell or deployment environment. The web
Compose profile defaults to production and always requires a unique secret; neither an absent
file nor the complete reference provides one. Blank optional values mean unset.
Set the public URL,
allowed hostnames and publication address before remote use. The default web
publication is loopback-only. Read [web configuration](../frontend/README.md)
before changing it to a public interface.

Prepare the default shared socket and managed-stack directories:

```bash
sudo install -d -m 0750 -o root -g 2000 lsdfm-run
sudo install -d -m 0700 -o root /var/lib/lsdfm/compose
```

Replace numeric group 2000 with your `BACKEND_SOCKET_GID` if customized.
The web container needs traversal access to this directory through that group.
Use your configured paths if you changed either directory. The Docker socket
must already exist; its bind does not silently create a directory in its place.

The defaults publish loopback-only HTTP with automatic cookie security. Before
public deployment, configure an HTTPS public URL, TLS termination and trusted
proxy addresses. For a private HTTP-only local test, keep an HTTP public URL,
`FRONTEND_COOKIE_SECURE=auto` and `FRONTEND_HTTP_POLICY=allow`; do not expose that
configuration directly to the Internet. See [login troubleshooting](../common/operations.md#login-fails-with-a-csrf-error).

## Start both components

```bash
docker compose --project-directory . --env-file .env \
  -f docs/deployment/compose.all.yaml config --quiet
docker compose --project-directory . --env-file .env \
  -f docs/deployment/compose.all.yaml up --build -d
docker compose --project-directory . --env-file .env \
  -f docs/deployment/compose.all.yaml logs -f
```

Open the configured public URL and use the temporary `admin` password printed
by `frontend`. Change it immediately after signing in.

## Start one component

```bash
# Web application only; configure FRONTEND_DAEMON_ENDPOINT first.
docker compose --project-directory . --env-file .env \
  -f docs/deployment/compose.web.yaml up --build -d

# Daemon only.
docker compose --project-directory . --env-file .env \
  -f docs/deployment/compose.daemon.yaml up --build -d
```

For separate Compose invocations sharing the supplied named volumes, use the
same Compose project name (`-p application`, for example). Both files also need the same
absolute `BACKEND_RUN_DIRECTORY` when launched from different project directories.
The daemon-only file does not publish a TCP listener. Configure a native Unix
socket or protected TCP transport explicitly for a frontend on another machine.

## Daemon service configuration

| Setting | Supplied value | Component | Why / when it is needed |
| --- | --- | --- | --- |
| Service name | `backend` | Daemon | Independent from `frontend`; inspect its logs for host-access failures. |
| User/group | `0:${BACKEND_SOCKET_GID:-2000}` | Socket and filesystem access | Runs as container root with the shared socket group; host filesystem permissions still apply. |
| Network | `network_mode: host` | Interfaces, routes, firewall, network Sysctl | Reads/manages the host network namespace rather than a private container network. |
| Process namespace | `pid: host` | Host process evidence and endpoint/interface association | Makes host PIDs visible for Docker process/namespace inspection. |
| Capabilities | Drop all; add `NET_ADMIN`, `NET_RAW` | Network inventory, firewall, supported runtime changes | Required for the relevant netlink/tool operations; the access switches still default to read-only. |
| Privilege escalation | `no-new-privileges:true` | Service confinement | Prevents acquiring new privileges through executed programs. |
| Init | `true` | Process lifecycle | Reaps child processes and forwards termination signals. |
| Restart | `unless-stopped` | Availability | Restarts after failure/reboot unless deliberately stopped. |
| Listener | Unix socket `/run/lsdfm/backend.sock`, mode `0660`, by default | Frontend connection | Shared through the run-directory bind; the health check reads the effective listener configuration. |
| Health check | Authenticated daemon ping every 15 s; 5 s timeout, 3 retries, 30 s startup grace | Service readiness | Combined Compose waits for this check before starting the web service. |
| Feature mode | Parent access `readonly`; Interfaces/Routing child defaults `rw`, capped to read-only | System, network, Docker | Deliberate read-only deployment. See [daemon access settings](../backend/README.md#component-and-feature-access) before enabling writes. |

## Daemon mounts

Read [host access and Compose source mounts](host-access.md) before enabling broad
host access. It explains the exposure, narrower alternatives and the
[Docker-focused example](compose.docker.yaml). The full examples below deliberately
provide more visibility than Docker management alone requires.

| Host source | Container target | Mode | Daemon component | Needed when / explanation |
| --- | --- | --- | --- | --- |
| `${BACKEND_HOST_FILESYSTEM_ROOT:-/}` | `/host/root` | Read-only bind, `rslave` | System resources, block mounts, host Sysctl provenance; optional external Compose mapping | Supplies host procfs/sysfs, PID 1's mount table and reachable host filesystem paths for `statvfs`. It also exposes host files to the daemon. Mount-list data alone cannot supply free-byte counts. Future propagation depends on host mount sharing. |
| `${BACKEND_RUN_DIRECTORY:-./lsdfm-run}` | `/run/lsdfm` | Read/write bind | Daemon Unix listener | Daemon creates the socket here. Share this directory with the frontend and match socket GID. Relative source is anchored by `--project-directory`. |
| `${BACKEND_DOCKER_SOCKET_PATH:-/run/docker.sock}` | `/run/docker.sock` | Socket bind; source must exist | Local Docker Engine access | Needed for the default Unix endpoint. The socket grants Engine authority; a filesystem read-only bind would not make Docker API calls read-only. Remove this bind when using only a remote Engine or disabling Docker support. |
| `/etc` | `/host/etc` | Read-only bind | Network manager detection, Docker configuration, host file provenance | Supplies persistent host configuration. It deliberately prevents persistent network-file edits through this mount; use a native daemon for full manager integration. |
| `${BACKEND_COMPOSE_ROOT:-/var/lib/lsdfm/compose}` | Same absolute path | Read/write bind | Managed Compose stacks | Dedicated directories for new stacks. Identical host/container paths keep relative Compose bind paths meaningful to the host Docker Engine. Create the source directory and grant access first. |
| Named volume `backend-state` | `/var/lib/lsdfm/backend` | Read/write volume | Stored stack definitions and daemon state | Persists the default SQLite definition database. Managed Compose files use their separate directory bind. Changing the database path outside this volume needs an additional persistent mount. |

Host `/etc`, host-root and Docker-socket mounts serve different roles. Removing
one can reduce visibility even when another still exists. The daemon reports
unavailable reads rather than substituting container filesystem usage. Btrfs
mount validation includes subvolume roots when device numbers differ.

With the default `BACKEND_COMPOSE_HOST_ROOT=/`, external Compose paths automatically
use `BACKEND_SYSTEM_HOST_ROOT=/host/root` and the existing root bind. A dedicated
Compose prefix takes precedence. The bind remains read-only. For editable external sources, add a
separate writable project bind and use the matching source-root mapping; changing
an application access switch alone cannot make the read-only root bind writable.

For example, Docker's project label `/root/docker/bind9/docker-compose.yml` is
read at `/host/root/root/docker/bind9/docker-compose.yml`. Add `/root/docker` to
`BACKEND_COMPOSE_ALLOWED_ROOTS` to permit it. Search directories are unnecessary
when Docker reports the source path or working directory. The daemon does not
recursively scan the host root; `BACKEND_COMPOSE_SEARCH_DIRECTORIES` only provides
explicit fallback locations when those labels are missing. Prefix mapping allows
discovery, reading and definition-layer controls. Relative service bind sources
are translated to host paths. File-backed Compose secrets/configs and structured
conversion still need project mounts at their original host paths; see
[definition layers](../docker/stacks.md#definition-tree).

## Web service configuration and mounts

| Setting / source | Default target or value | Needed when / explanation |
| --- | --- | --- |
| Container HTTP listener | `0.0.0.0:8000` | Override `FRONTEND_BIND_HOST` / `FRONTEND_PORT`; publication follows the configured container port. |
| Published HTTP address | `127.0.0.1:8000` | Change `FRONTEND_PUBLISHED_BIND` / `FRONTEND_PUBLISHED_PORT` for your proxy/network. |
| Extra group | `${BACKEND_SOCKET_GID:-2000}` | Allows the non-root web user to connect to the daemon's group-writable socket. |
| Named volume `frontend-data` | `/frontend/data`, read/write | Persists users and per-user Compose drafts. |
| `${BACKEND_RUN_DIRECTORY:-./lsdfm-run}` | `/run/lsdfm`, read-only bind | Gives the frontend access to the existing socket without letting it replace the socket directory. |
| Health check | Configured local `/healthz` every 30 s | Follows bind address, port and direct TLS configuration. |

The web service does not need the host Docker socket, host-root bind or network
capabilities. Its system access goes through backend.

## Compose substitution variables

These values are read by Compose on the host. They are **not** daemon settings
unless explicitly mapped into the service's environment.

| Variable | Compose default | Used by | Required or used when |
| --- | --- | --- | --- |
| `APPLICATION_IMAGE` | `application:local` | All services | Common local image tag; both commands are included. Compose builds it from the shared `docker/Dockerfile`. |
| `BACKEND_SOCKET_GID` | `2000` | Daemon primary group and web extra group | Both services share a Unix socket. Need not be a named group on the host; existing directory permissions must permit access. |
| `BACKEND_RUN_DIRECTORY` | `./lsdfm-run` | Both socket-directory binds | Shared socket; use an absolute path across separate project directories. |
| `BACKEND_DOCKER_SOCKET_PATH` | `/run/docker.sock` | Daemon Docker socket bind | Default local Docker endpoint; must be an existing socket. |
| `BACKEND_HOST_FILESYSTEM_ROOT` | `/` | Daemon host-root bind | Full host resource/mount visibility; normally keep the real host root. |
| `FRONTEND_PUBLISHED_BIND` | `127.0.0.1` | Web port publication | Choose the host interface that accepts HTTP/proxy traffic. |
| `FRONTEND_PUBLISHED_PORT` | `8000` | Web port publication | Host port; `FRONTEND_PORT` selects the container port. |
| `FRONTEND_SECRET_KEY` | No fallback | Web sessions | Required by web/combined Compose; generate a unique random value of at least 32 characters. |
| `FRONTEND_DAEMON_TOKEN` | Empty | Web authentication token | Optional for Unix transport. The daemon inherits this token unless `BACKEND_ACCESS_TOKEN` explicitly overrides it. Keep both sides identical. |

Other substituted daemon/web variables are listed in their
[daemon](../backend/README.md) and [web](../frontend/README.md) references.
The full Compose examples forward every Settings field, including listener, TLS
paths, shutdown timeout and host-mapped configuration paths. Use `.env` overrides
for these values. Certificate paths and custom filesystem paths must exist inside
the relevant container; configuration alone does not create mounts. The
Docker-focused variant deliberately fixes its isolation settings, source policy
and Unix transport; see [host access](host-access.md).

## Updating and persistence

Rebuild and recreate the affected services after updating their images or mounts:

```bash
docker compose --project-directory . --env-file .env \
  -f docs/deployment/compose.all.yaml up --build -d
```

`down` preserves named volumes unless `--volumes` is supplied. Keep the web secret,
web data volume, daemon state volume and managed stack directory in backups.
See [operations](../common/operations.md#backups).

For changed ENV namespaces or an existing installation, review
[configuration and existing state](../common/configuration.md) before recreating
services. The runtime environment keys follow the configured prefix; Compose
substitution inputs retain their neutral example names.
