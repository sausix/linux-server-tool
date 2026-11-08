# Web configuration and HTTPS

The web command is `frontend`. The display name comes from the bundled application configuration
and can be overridden through `APP_NAME` in the selected environment namespace. Direct startup reads
`.env` in the working directory. Explicit CLI values override environment values.
`frontend --help` lists flags. Compose sets its own service environment from the
substitution file; all documented Settings fields are forwarded. Unknown variables
are not forwarded. The [complete native ENV reference](frontend.env.example)
matches the defaults when no `.env` exists; use only overrides in your deployment.

## Application, listener and database

| Variable | Native default | When / purpose |
| --- | --- | --- |
| `FRONTEND_APP_NAME` | Package metadata | Application title setting. |
| `FRONTEND_ENVIRONMENT` | `development` | `development`, `production`, `test`; Compose uses production. |
| `FRONTEND_BIND_HOST` | `127.0.0.1` | Listener address; Compose defaults to `0.0.0.0` inside the container. |
| `FRONTEND_PORT` | `8000` | Listener port, 1–65535; Compose publication targets this port. |
| `FRONTEND_PUBLIC_URL` | `http://localhost:8000` | Public absolute HTTP(S) URL; controls cookie auto-security and allowed public hostname. |
| `FRONTEND_DATABASE_URL` | `sqlite:///./data/frontend.db` | Persistent web database. Compose uses `sqlite:////frontend/data/frontend.db`. |
| `FRONTEND_SECRET_KEY` | Unset | Required in production; at least 32 characters. Development fallback is temporary and invalidates sessions on restart. |
| `FRONTEND_SHUTDOWN_TIMEOUT_SECONDS` | `10` | Graceful connection drain, 1–300 seconds. |

## Passwords and sessions

| Variable | Default | When / purpose |
| --- | --- | --- |
| `FRONTEND_SESSION_MAX_AGE_SECONDS` | `3600` | Server-side inactivity timeout, minimum 60 seconds. Authenticated use renews it. |
| `FRONTEND_SESSION_PERSISTENT` | `true` | Persistent cookie across browser restarts; `false` emits a browser-session cookie. |
| `FRONTEND_PASSWORD_MAX_AGE_DAYS` | `90` | Password age before profile-only access and required change; minimum 1 day. |

Cookies are shared between tabs. Closing a tab does not end a session, and browser
session restoration may restore session cookies. Use **Sign out** explicitly.
Sessions survive a web restart only with a stable secret. User passwords are
stored as Argon2 hashes; Compose drafts in the web database can contain sensitive
configuration text and need protected backups.

## HTTP policy, TLS and proxy trust

| Variable | Default | When / purpose |
| --- | --- | --- |
| `FRONTEND_HTTP_POLICY` | `allow` | `allow`, `redirect`, `https-only`; the last rejects requests not recognized as HTTPS. |
| `FRONTEND_COOKIE_SECURE` | `auto` | `auto` follows HTTPS public URL; `always` or `never` explicitly overrides. |
| `FRONTEND_TRUSTED_HOSTS` | `localhost,127.0.0.1` | Comma-separated allowed HTTP hosts; public URL hostname is also included. |
| `FRONTEND_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxy addresses/networks whose forwarded headers Uvicorn trusts. |
| `FRONTEND_SSL_CERTFILE` | Unset | Certificate path for direct TLS; requires the matching key setting. |
| `FRONTEND_SSL_KEYFILE` | Unset | Private key for direct TLS; requires certificate setting. |

## Daemon connection and operation timeouts

| Variable | Default | When / purpose |
| --- | --- | --- |
| `FRONTEND_DAEMON_ENDPOINT` | Storage-ID-scoped path | Unix socket or `tcp://host:port`; frontend OS account needs socket permissions. |
| `FRONTEND_DAEMON_TOKEN` | Unset | Match daemon access token when configured; required for authenticated TCP. |
| `FRONTEND_DAEMON_TIMEOUT_SECONDS` | `1.0` | Ping, ordinary connection and subscription setup timeout; up to 30 s. |
| `FRONTEND_DAEMON_INVENTORY_TIMEOUT_SECONDS` | `15.0` | Inventory calls; up to 300 s. |
| `FRONTEND_DAEMON_STORAGE_TIMEOUT_SECONDS` | `45.0` | Docker image/volume disk accounting; up to 660 s and normally above daemon storage timeout. |
| `FRONTEND_DAEMON_CONTROL_TIMEOUT_SECONDS` | `150.0` | Stack control calls; up to 1900 s and normally above daemon command timeout. |

## HTTPS and reverse proxies

For direct HTTPS, configure both TLS files, set an HTTPS public URL and use
`https-only`. Mount certificates read-only into a container before passing their
paths; the supplied web Compose file forwards both TLS settings. Certificate
files still need to be readable inside the container, using explicit read-only
mounts where necessary.

For a TLS-terminating reverse proxy:

1. Set `FRONTEND_PUBLIC_URL=https://your-host.example`.
2. Configure the public hostname in `FRONTEND_TRUSTED_HOSTS`.
3. Trust only the proxy address/network in `FRONTEND_FORWARDED_ALLOW_IPS`.
4. Configure the proxy to replace incoming forwarded protocol/client-IP headers.
5. Use `https-only` or `redirect` once trusted HTTPS forwarding is working.

Avoid trusting all forwarded-header senders when clients can connect directly.
`COOKIE_SECURE=auto` sets Secure cookies with an HTTPS public URL. The default
Compose health check uses direct HTTP, so adapt the check if the web service
requires HTTPS for every request.

## Login policies, MFA and size limits

Full behavior and security TODOs: [SECURITY.md](../security/SECURITY.md). All limits are enforced
before parsing; reverse-proxy limits are still needed for connection-level DoS.

| Variable | Default | When / purpose |
| --- | --- | --- |
| `FRONTEND_SESSION_ABSOLUTE_MAX_AGE_SECONDS` | `43200` | Absolute session lifetime, 60–2592000 s; authenticated activity cannot extend it. |
| `FRONTEND_LOGIN_ALLOWED_NETWORKS` | `[]` | JSON source IP/CIDR allowlist; empty means no global restriction. |
| `FRONTEND_LOGIN_RULES` | `[]` | Ordered JSON user/network allow/deny and WebAuthn rules; first match wins. See SECURITY.md. |
| `FRONTEND_WEBAUTHN_MODE` | `optional` | off, optional or required. Optional requires a factor once enrolled. |
| `FRONTEND_LOGIN_FAILURE_LIMIT` | `5` | Failed attempts per username before a temporary block, 1–100. |
| `FRONTEND_LOGIN_IP_FAILURE_LIMIT` | `20` | Failed attempts per source IP/prefix before a block, 1–1000. |
| `FRONTEND_LOGIN_FAILURE_WINDOW_SECONDS` | `900` | Failure accounting window, 10–86400 s; extended to cover an active block. |
| `FRONTEND_LOGIN_BLOCK_SECONDS` | `300` | Temporary authentication block, 1–86400 s. |
| `FRONTEND_LOGIN_MAX_CONCURRENCY` | `4` | Concurrent password verifications per web process, 1–32. |
| `FRONTEND_LOGIN_IPV6_PREFIX` | `64` | IPv6 prefix length for shared source throttling, 0–128; IPv4 uses individual IPs. |
| `FRONTEND_REQUEST_MAX_BYTES` | `8388608` | Maximum HTTP body before parsing, 1 KiB–64 MiB; must cover POST and upload plus 64 KiB overhead. |
| `FRONTEND_POST_MAX_BYTES` | `4194304` | Ordinary POST/PUT/PATCH/DELETE body cap, at most request budget; imports use separate upload budget. |
| `FRONTEND_LOGIN_MAX_BYTES` | `16384` | Password login body cap, 1–64 KiB and at most POST budget. |
| `FRONTEND_UPLOAD_MAX_BYTES` | `4194304` | Compose compressed and expanded upload cap, 1 KiB–4 MiB; cannot exceed the shared RPC/file limit. |
| `FRONTEND_UPLOAD_MAX_FILES` | `64` | Files per Compose import, 1–64; applies after archive expansion too. |

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
