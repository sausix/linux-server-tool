# Optional components

Use the same switches in the web application and daemon environments:

```dotenv
DOCKER_SUPPORT=true
NETWORK_SUPPORT=true
SYSTEM_SUPPORT=true
```

All three default to true for existing deployments. The names have no the application or
backend prefix. Compose examples explicitly forward them into both services.
Native processes read them from their own environment or working-directory `.env`.
For separate hosts, configure both deployments consistently; a local file is not
automatically distributed to the other host.

| Switch | Disabled behavior |
| --- | --- |
| `DOCKER_SUPPORT=false` | Docker implementation is not initialized in the daemon. Docker API operations, subscriptions, web pages and menu entries are unavailable. Network/System continue without Docker decorations. |
| `NETWORK_SUPPORT=false` | Network access and navigation are unavailable. System and Sysctl remain independently available. |
| `SYSTEM_SUPPORT=false` | System resources and Sysctl are unavailable, independently of network support. |

These switches control component availability. Existing `BACKEND_DOCKER_ACCESS`,
`BACKEND_NETWORK_ACCESS`, `BACKEND_SYSTEM_ACCESS` and feature-specific modes still
control disabled/read-only/read-write permissions. A false support switch always
takes precedence; setting access to `rw` cannot re-enable it. Enabling a switch
does not override a disabled or read-only access mode.

Both commands accept `--docker-support` / `--no-docker-support`,
`--network-support` / `--no-network-support` and
`--system-support` / `--no-system-support`. CLI options override the environment.
Recreate Compose services after environment changes.

When Docker is available, interface presentation may include a Docker accent,
stack links and service information. These are optional decorations: underlying
interface identity, addresses, flags and scope remain Linux network data.
Decorations disappear when the association disappears or Docker support is
removed. Scope badges retain their networking meaning. A decoration does not
establish ownership or authorize changes to the underlying interface.
