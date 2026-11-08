# User documentation

The application provides an authenticated web interface for Linux resources, networking,
firewall state and Docker. The web application (`frontend`) and system service
(`backend`) can run independently and communicate through a Unix socket or an
authenticated TCP connection.

## Documentation by component

| Component / area | Guides |
| --- | --- |
| Shared operation | [Getting started](common/getting-started.md), [environment namespaces](common/configuration.md), [optional components](common/components.md), [operations and troubleshooting](common/operations.md) |
| Web application | [Configuration, sessions, HTTPS and reverse proxies](frontend/README.md) |
| Daemon | [Environment settings, access modes and host paths](backend/README.md) |
| System control | [Resources, block mounts, Sysctl and configuration reviews](system/README.md) |
| Network control | [Interfaces, bridges, routing, firewall and address changes](network/README.md) |
| Docker | [Resources and logs](docker/README.md), [stack management](docker/stacks.md), [x-stackmeta](docker/X-STACKMETA.md) |
| Deployment | [Docker Compose](deployment/README.md), [native packages and systemd](deployment/native-installation.md) |
| Security | [Measures and TODOs](security/SECURITY.md), [historical review](security/review.md) |

## Deployment files

Daemon-specific service and environment files live with the daemon. The links
below point to their canonical copies; there are no duplicate files under docs.

| File | Use |
| --- | --- |
| [Docker-focused Compose](deployment/compose.docker.yaml) | Both components with minimal host mounts; [mount exposure and alternatives](deployment/host-access.md) |
| [Combined Compose](deployment/compose.all.yaml) | Web application and daemon together |
| [Web-only Compose](deployment/compose.web.yaml) | Web application connected to an existing daemon |
| [Daemon-only Compose](deployment/compose.daemon.yaml) | Daemon connected to the Linux host and Docker endpoint |
| [Compose environment reference](deployment/compose.env.example) | Complete defaults; deployments need only overrides and their unique secret |
| [Native web environment reference](frontend/frontend.env.example) | Complete native web defaults |
| [Native daemon environment example](../backend/systemd/backend.env.example) | Copy to `/etc/lsdfm/backend.env` for systemd |
| [systemd unit](../backend/systemd/backend.service) | Daemon service using `/opt/lsdfm/backend/.venv` |

the application is distributed under the [MIT License](../LICENSE).
