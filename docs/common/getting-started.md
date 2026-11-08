# Getting started

## Choose a deployment

| Deployment | Suitable when |
| --- | --- |
| [Combined Compose](../deployment/README.md#start-both-components) | Run the web interface and daemon on the same Linux server |
| [Web only](../deployment/README.md#start-one-component) | An backend service already exists on this or another machine |
| [Daemon only](../deployment/README.md#start-one-component) | Run the system service on a server and connect a separately installed web interface |
| [Native systemd service](../deployment/native-installation.md) | Use host network managers and permission-checked persistent changes directly |

The daemon requires access only to the components you enable. Start with read-only
access and check the [daemon settings](../backend/README.md) before enabling
write operations. A Docker socket grants root-equivalent Engine access even when
the application itself is configured read-only.

## First login

Open the configured public web URL. The first web startup creates `admin` and
prints its temporary initial password to the web application's logs. Log in and
set a new password. The initial password is printed only when the account has
no password; normal restarts do not print the existing password.

For Compose, view the startup output with:

```bash
docker compose --project-directory . --env-file .env \
  -f docs/deployment/compose.all.yaml logs frontend
```

Use the appropriate single-service file if you started only one component.

The dashboard reports the daemon connection and available component access.
The web interface can start while the daemon is unavailable; system and Docker
pages become available once connectivity and permissions are correct.

## Sessions and live updates

Authenticated activity renews the session inactivity timeout. Use **Sign out**
to revoke the current session on the server. Temporary or expired passwords
restrict access to **Settings → User** until updated. Password changes sign out
all sessions and require another login. Security keys can be registered in User
settings; see [Security](../security/SECURITY.md) for MFA policy and recovery limitations.

The **Live refresh** badge pauses or resumes automatic page updates for the
session. Pausing closes inventory subscriptions and stops periodic refreshes.
The log viewer has a separate explicit Live control.

Before using a public hostname, configure the public URL, allowed hosts and
[HTTPS or a trusted reverse proxy](../frontend/README.md#https-and-reverse-proxies).
