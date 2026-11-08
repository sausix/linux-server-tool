# LSDFM

**Linux Server Docker Firewall Manager** is a web interface for Linux system
resources, network configuration, firewall rules and Docker workloads.

LSDFM combines live monitoring with explicit, reviewed administrative actions.
Components can be enabled independently and operated read-only or with selected
write permissions.

> **Development status:** LSDFM is under active development. Interfaces and stored
> definitions may change. Review the security documentation before exposing this
> privileged administration service to a network.
> 
> It's recommended to expose the daemon and frontend only to private access.
> 
> Do not expose the daemon's socket and listening ports to other components on your system.
> 
> Better do not expose the web frontend to public networks.

## Features

- **System:** operating-system information, memory, CPU usage, block devices,
  mounted filesystem usage, Sysctl inspection and configuration reviews.
- **Network:** interfaces, addresses, bridges, routes, IPv4/IPv6 classification
  and configuration-source comparisons. Firewall tables, chains and rules from
  nftables and iptables are presented together.
- **Docker:** stacks, services, containers, images, volumes, labels and live logs,
  with resource links, storage information and reviewed lifecycle operations.
- **Compose management:** file-backed or stored definitions, editable drafts,
  definition trees and optional `x-stackmeta` descriptions and dependencies.
- **Live updates:** Docker events, Linux netlink and configuration-file watches
  update the interface. Automatic refresh can be paused per session.
- **Authentication:** server-side sessions, login throttling, source-network
  rules and optional or required FIDO2/WebAuthn security keys.

Some features remain incomplete; metadata declarations do not imply that an
orchestration scheduler or every write operation is implemented.

## Components

| Package | Purpose |
| --- | --- |
| `lsdfm-web` (`frontend`) | FastAPI/Jinja2 web interface with SQLite account and session storage |
| `lsdfmd` (`backend`) | System service that reads and controls the configured Linux/Docker resources |
| `lsdfm-common` | Shared JSON-RPC 2.0 communication and resource helpers |

The web application and daemon can run independently, including on separate
hosts with a protected connection. Docker support is optional. Shared
`DOCKER_SUPPORT`, `NETWORK_SUPPORT` and `SYSTEM_SUPPORT` switches control
availability; separate daemon access modes limit permissions.

## Run with Docker Compose

A single [Dockerfile](docker/Dockerfile) builds an image containing both applications.
The supplied Compose files start the web service, daemon, or both in separate
containers. Run the following from this checkout's root:

```bash
cp docs/deployment/compose.env.example .env
# Edit .env: generate a unique secret and configure the public URL/TLS settings.
# Prepare the daemon's host directories and mounts as described in docs/deployment/README.md.
docker compose --project-directory . --env-file .env \
  -f docs/deployment/compose.all.yaml up --build -d
```

The example environment assumes HTTPS termination. Publishing the HTTP port does
not enable TLS. Follow the [deployment guide](docs/deployment/README.md) before startup,
including its directory preparation and HTTP-only local-test instructions.
The web application's initial temporary `admin` password is printed in its
startup logs and must be changed after sign-in.

For individual components, use [web-only Compose](docs/deployment/compose.web.yaml)
or [daemon-only Compose](docs/deployment/compose.daemon.yaml). Native package and
systemd installation is also supported.

## Documentation

Start with the [documentation index](docs/README.md).

| Guide | Contents |
| --- | --- |
| [Getting started](docs/common/getting-started.md) | First startup and sign-in |
| [Docker Compose](docs/deployment/README.md) | Shared image, deployment choices, mounts and persistence |
| [Native installation](docs/deployment/native-installation.md) | Separate packages and the systemd unit |
| [Optional components](docs/common/components.md) | Shared feature switches and access modes |
| [Web configuration](docs/frontend/README.md) | Environment, sessions, HTTPS and proxy trust |
| [Daemon configuration](docs/backend/README.md) | Component settings, defaults and requirements |
| [Security](docs/security/SECURITY.md) | Implemented protections, deployment requirements and outstanding work |
| [Stack metadata](docs/docker/X-STACKMETA.md) | Supported `x-stackmeta` syntax and semantics |
| [Operations](docs/common/operations.md) | Troubleshooting, logs and backups |

## Security

The daemon may have root-equivalent authority through Docker or Linux network
capabilities. Keep its API private, prefer permission-restricted Unix sockets,
use HTTPS for the public web interface and enable only the access you need.
Raw daemon TCP currently has no native TLS; secure remote connections externally.

The examples start component access read-only. Read-only application settings do
not remove the authority granted by a mounted Docker socket or host filesystem.
See [SECURITY.md](docs/security/SECURITY.md) for limitations and remaining work.

## License

LSDFM is distributed under the [MIT License](LICENSE).

## Development environment and versions

Use one environment at the repository root:

```sh
uv sync --locked --all-packages --python 3.14
```

Select `.venv/bin/python` in your IDE. Component-local virtual environments are
not needed.

The root `pyproject.toml` defines the coordinated release version. The version
tool updates component metadata, exact workspace dependency pins and `uv.lock`
together, preserving independent package builds. Python `__version__` values
come from installed distribution metadata.

```sh
python scripts/version.py check
python scripts/version.py bump patch --dry-run
python scripts/version.py bump patch
# Alternatively: python scripts/version.py set 0.2.0.dev1
uv sync --locked --all-packages
```

Use `minor` or `major` for larger increments. A failed dependency resolution
restores the previous metadata and lockfile. Version changes do not create Git
tags; reinstall or sync packages and restart processes to use the new version.

## Branding and configuration

The Python packages are `frontend`, `backend` and `common`. Branding defaults and
compatibility identifiers are centralized in `common/src/common/application.toml`. Set
`FRONTEND_ENV_PREFIX` and `BACKEND_ENV_PREFIX` for custom runtime namespaces.
See [configuration and compatibility](docs/common/configuration.md), especially
when retaining existing databases and Docker volumes.
