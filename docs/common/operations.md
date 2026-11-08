# Operations and troubleshooting

## Service logs and readiness

For Compose, use the same project directory, environment file and Compose file
used at startup:

```bash
docker compose --project-directory . --env-file .env \
  -f docs/deployment/compose.all.yaml ps
docker compose --project-directory . --env-file .env \
  -f docs/deployment/compose.all.yaml logs -f backend frontend
```

For systemd:

```bash
journalctl -u backend.service -b
systemctl status backend.service
```

Enabled-component startup checks report missing tools and inaccessible system
interfaces to stderr/journal. Failed commands include useful error output.
Disabled components are not probed. `/healthz` is the web application's health
endpoint; the daemon Compose check verifies its Unix listener.

## Common symptoms

| Symptom | Check |
| --- | --- |
| Daemon unavailable | Listener path/transport, socket owner/group/mode, shared run directory and matching token. Restart the web process after changing OS group membership. |
| No effect from `.env` | Direct startup reads from its working directory; Compose reads the project `.env` or explicit `--env-file` and forwards documented fields; systemd uses its optional EnvironmentFile. CLI values override ENV. |
| Inventory HTTP 503 | Read the failing operation/error and daemon logs. General ping and inventory have separate frontend timeouts. |
| Interfaces unavailable under systemd | Unit must permit `AF_NETLINK`; check `CAP_NET_ADMIN` and service sandboxing. |
| Firewall tool errors | Check reader availability, effective capabilities, host network namespace and selected backend. A tool being installed does not prove access. |
| nftables empty but iptables populated | Use `auto` or explicitly select the correct iptables backend; container alternatives can differ from the host. |
| Host mount usage unknown | Verify `/host/root`, mapped submount visibility/propagation and permissions. Mismatched device/subvolume identity is deliberately rejected. |
| Sysctl entries unavailable | Some options/interfaces do not exist or are unreadable. Check network namespace and host-root/config mounts; write-only actions/secrets are intentionally excluded. |
| Image/volume accounting slow | Increase daemon storage timeout and keep the frontend storage timeout above it. Inventory remains usable while statistics load separately. |
| Compose source unavailable | Verify local endpoint, allowed roots, source mapping, mounted directories and filesystem permissions. Remote project paths are not local evidence. |
| Persistent network edits fail in Compose | Host `/etc` is read-only in the example. Use native manager integration for supported persistent edits. |
| Login/session errors after restart | Keep a stable web secret and persistent database. Expired sessions redirect page requests to login; protected APIs return authentication errors. |
| Log stream missing output | Check Docker log driver support, selected endpoint, history limits and daemon discovery interval. Deleted containers cannot supply history. |

The daemon's Docker event connection can stay idle without a warning. Repeated
stream-unavailable warnings indicate an actual connection/setup problem rather
than absence of container events. Closing clients and stopping the service should
release log/event connections without broken-pipe tracebacks.

## Backups

| Data | Default location | Why retain it |
| --- | --- | --- |
| Web accounts and per-user drafts | Native `./data/frontend.db`; Compose `frontend-data` at `/frontend/data` | User passwords are hashed; drafts may contain sensitive configuration text. |
| Stable web secret | Web environment/secret store | Required alongside the web database to preserve server sessions across restarts. |
| Stored stack definitions | `/var/lib/lsdfm/backend/stacks.sqlite3`; Compose `backend-state` | Structured definitions and daemon stack records. |
| Managed file stacks | `/var/lib/lsdfm/compose` by default | Compose files and supplementary stack files. |
| External project sources | Your configured host paths | Remain separate from the managed stack directory. |

Use a consistent SQLite backup method or stop the owning service before copying
database files. Protect backups and environment files as sensitive data. Docker
volumes containing application data require their own application-consistent
backup plan; backing up the application's database is not a Docker data backup.

## Access changes and service updates

After changing a Compose mount, recreate the affected service. Rebuilding alone
does not replace the running container. Native systemd unit edits require
`systemctl daemon-reload` before restarting. Feature access flags do not override
read-only mounts, file ownership, Linux capabilities or network-manager behavior.

Use the `frontend` command for normal startup/shutdown so long-lived browser
connections receive shutdown notification. Log/confirmation previews can expire
after a web restart; review them again. Multi-worker deployments need session
affinity for worker-local confirmation/log checkpoints.

## Login fails with a CSRF error

A sign-in form needs the same server session cookie on GET and POST. If the
browser uses an HTTP server IP while `FRONTEND_COOKIE_SECURE=always`, it will not
return the Secure cookie. The login audit then has no session ID. The HTTPS
example assumes TLS termination; publishing port 8000 alone does not provide TLS.

For a private, direct HTTP test, explicitly configure:

```dotenv
FRONTEND_PUBLIC_URL=http://192.0.2.10:8000
FRONTEND_COOKIE_SECURE=auto
FRONTEND_HTTP_POLICY=allow
```

Replace the example address with the actual server. Recreate the web service to
apply Compose environment changes, then reload the login page. For public use,
keep HTTPS and Secure cookies enabled. Behind a TLS reverse proxy, trust only the
proxy and forward the correct HTTPS scheme. The application never automatically
downgrades cookie security based on an incoming HTTP request.

Other causes include blocked cookies, a stale form after a restart/secret change,
expired sessions, or different web instances using different databases/secrets.
Reload the form, use a consistent hostname, and inspect the browser's cookie
warnings. Clear old site cookies if the hostname/security setup changed. Never
work around the error by disabling CSRF validation.
