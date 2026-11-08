# Native service installation

A stable Python 3.12 or newer is required; Python 3.14 is the current tested
checkout default. Experimental Python prereleases are not validated and may
require source builds for dependencies. Install the supplied `frontend` and/or `backend`
commands from their distribution wheels into separate virtual environments.
Set `COMMON_WHEEL`, `FRONTEND_WHEEL` and `BACKEND_WHEEL` to the actual wheel
paths obtained from your trusted artifact source. Distribution names are declared
in each package metadata file; the commands are independent of those names.
They do not require both applications to run on the same machine. The following
assumes installation artifacts have already been obtained; adjust wheel names to
the versions you are installing.

```bash
sudo python3 -m venv /opt/lsdfm/backend/.venv
sudo /opt/lsdfm/backend/.venv/bin/pip install "$COMMON_WHEEL" "$BACKEND_WHEEL"

sudo python3 -m venv /opt/lsdfm/frontend/.venv
sudo /opt/lsdfm/frontend/.venv/bin/pip install "$COMMON_WHEEL" "$FRONTEND_WHEEL"
```

Install only the component you need. Obtain packages and dependencies through
your deployment's trusted artifact source.

## Daemon systemd unit

Use the [unit example](../../backend/systemd/backend.service) and
[daemon environment example](../../backend/systemd/backend.env.example). From the the application checkout
directory, where `docs/` exists:

```bash
getent group lsdfm || sudo groupadd --system lsdfm
sudo install -d -m 0750 -o root -g lsdfm /etc/lsdfm
sudo install -m 0640 -o root -g lsdfm backend/systemd/backend.env.example /etc/lsdfm/backend.env
sudo install -m 0644 backend/systemd/backend.service /etc/systemd/system/lsdfm-backend.service
sudo install -d /opt/lsdfm/backend/docs
sudo cp -R docs/. /opt/lsdfm/backend/docs/
# Keep the linked repository assets available in the offline documentation.
sudo install -d /opt/lsdfm/backend/backend/systemd
sudo install -m 0644 backend/systemd/backend.service /opt/lsdfm/backend/backend/systemd/backend.service
sudo install -m 0644 backend/systemd/backend.env.example /opt/lsdfm/backend/backend/systemd/backend.env.example
sudo install -m 0644 LICENSE /opt/lsdfm/backend/LICENSE
sudo systemctl daemon-reload
sudo systemctl enable --now lsdfm-backend.service
sudo systemctl status lsdfm-backend.service
```

These commands run from the the application checkout directory, where `docs/` exists.
The environment file is optional; without it the daemon uses its native defaults. Review
`/etc/lsdfm/backend.env` before enabling the service. The unit expects the
daemon executable at `/opt/lsdfm/backend/.venv/bin/backend` and uses the `lsdfm` group for
its Unix socket. Add the web-service OS account to that group, then restart the
web process so new group membership is effective.

The unit starts the installed `/opt/lsdfm/backend/.venv/bin/backend` executable directly,
uses `/var/lib/lsdfm/backend` as its working directory and creates private files with
`UMask=0077`. It creates `/run/lsdfm` and `/var/lib/lsdfm/backend`. Its sandbox permits network
administration capabilities and netlink, while filesystem/kernel protections
limit writes. Review `ReadWritePaths` for custom managed-stack or network paths.
`ProtectHome=true` may prevent discovering projects under home directories;
prefer service-owned project locations or an explicit administrator override.
Native startup normally leaves `BACKEND_SYSTEM_HOST_ROOT` unset.

## Web application

Run `frontend` from a directory containing its `.env` file, or inject the
[web environment](../frontend/README.md) using your service manager:

```bash
/opt/lsdfm/frontend/.venv/bin/frontend --daemon-endpoint unix:///run/lsdfm/backend.sock
```

Use a writable persistent SQLite location and a stable secret. Bind HTTP to
loopback for a local reverse proxy; configure TLS/proxy trust before remote use.

## TCP daemon connection

Generate a random token of at least 32 characters. Set these variables in the
appropriate service environment files:

```dotenv
# Daemon
BACKEND_TRANSPORT=tcp
BACKEND_LISTEN_HOST=127.0.0.1
BACKEND_LISTEN_PORT=9876
BACKEND_ACCESS_TOKEN=<generated-token>

# Web application
FRONTEND_DAEMON_ENDPOINT=tcp://127.0.0.1:9876
FRONTEND_DAEMON_TOKEN=<same-generated-token>
```

TCP carries no transport encryption. Use a protected tunnel or otherwise secure
transport between different machines. Prefer Unix socket permissions locally.
