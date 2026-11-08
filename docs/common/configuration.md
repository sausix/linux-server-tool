# Display name and environment namespaces

The display name is read from the bundled application configuration and can be overridden with the
frontend's `APP_NAME` setting. Changing that name does not rename user databases,
session cookies, managed-file ownership markers or stored definition references.
Those compatibility identifiers are separate from branding.

The standard environment namespaces are `FRONTEND_` and `BACKEND_`. Their
selectors are fixed, project-independent bootstrap variables:

```dotenv
FRONTEND_ENV_PREFIX=CONSOLE_
BACKEND_ENV_PREFIX=HOSTSERVICE_

CONSOLE_PUBLIC_URL=https://console.example.org
CONSOLE_COOKIE_SECURE=always
# Supply a unique, randomly generated secret before production startup.
CONSOLE_SECRET_KEY=
HOSTSERVICE_UNIX_SOCKET=/run/lsdfm/backend.sock
```

For direct/native startup, write settings using the selected prefix. The
selectors can be set in the process environment or its `.env` file. Process
selectors override file selectors; `--env-prefix PREFIX_` overrides both. Explicit
CLI setting values take precedence over all environment/file values. Prefixes
must be empty or identifiers ending in `_`; nonempty prefixes are recommended.

```bash
frontend --env-prefix CONSOLE_
backend --env-prefix HOSTSERVICE_
```

JSON lists and login rules work in custom namespaces. `DOCKER_SUPPORT`,
`NETWORK_SUPPORT` and `SYSTEM_SUPPORT` remain shared and unprefixed.

## Compatibility and isolation

With the standard namespace selected, previous project-prefixed settings remain
fallback inputs, using compatibility names recorded in application configuration. New
standard-prefix values take precedence, including standard-prefix `.env` values
over legacy process variables. CLI values win over both.

A custom namespace is isolated: it does not read compatibility-prefixed secrets
or settings. Configure its production secret and any backend TCP token explicitly.
The frontend's reload child inherits the same selected namespace.

The stable storage namespace is independent of the display name, Python imports
and ENV prefixes. Standalone defaults and supplied deployments use
`/run/lsdfm/backend.sock`, `/var/lib/lsdfm/backend/stacks.sqlite3` and
`/var/lib/lsdfm/compose`. Generic component names are not global host namespaces.
Changing branding does not rename persistent directories.
The Python imports are `frontend`, `backend` and `common`; installed distribution
names and previous console aliases remain declared in package metadata. Prefer
the neutral `frontend` and `backend` commands in service automation.

## Compose input variables

Compose uses the stable `FRONTEND_*` / `BACKEND_*` variables in the example `.env`
as **deployment inputs**. It translates them to the selected runtime prefix using
list-form environment entries. For example, set `FRONTEND_ENV_PREFIX=CONSOLE_`
and continue setting `FRONTEND_SECRET_KEY` in the Compose substitution file;
the container receives `CONSOLE_SECRET_KEY`. All Settings fields are forwarded by the full Compose profiles. Unknown variables
in the substitution file are not forwarded.

`APPLICATION_IMAGE`, volume-name selectors and shared component switches are
Compose/bootstrap inputs, not renamed runtime settings. Both services must use
consistent socket paths and authentication tokens, regardless of prefix spelling.

## Existing state when adopting the new deployment layout

No databases or files are automatically moved. Back up state before changing
paths or deployment names. The example services are now named `frontend` and
`backend`, with frontend data under `/frontend/data` and backend state under
`/var/lib/lsdfm/backend`. Managed Compose paths must still match their host paths.

To retain existing named volumes, set `FRONTEND_DATA_VOLUME` and
`BACKEND_STATE_VOLUME` to their actual Docker volume names. Set
`FRONTEND_DATABASE_URL` to the existing database filename inside the frontend's
new mount, for example `sqlite:////frontend/data/existing.db`. The backend state
volume retains its `stacks.sqlite3` filename by default. Verify mounts and file
ownership before startup; selecting a new empty volume starts with empty state.

Existing installations must explicitly retain their previous socket, database and
managed-directory settings until an administrator moves them. Update both ends
of the socket connection together. Keep managed host/container paths identical;
changing the Compose root can affect relative bind sources in saved definitions.
No automatic migration or cleanup of previous directories occurs.

## Defaults and overrides-only files

The complete native references for [web](../frontend/frontend.env.example) and
[daemon](../../backend/systemd/backend.env.example) contain every option and match
missing-file defaults. The [Compose reference](../deployment/compose.env.example)
contains every option plus deployment inputs and matches the full Compose
profiles. Container-specific defaults are explicit in that reference.

Keep only changes in a deployment `.env`; unset and empty optional values use
defaults. A nonempty custom prefix remains isolated from legacy namespaces.
Secrets have no shared fallback: production web startup requires its unique
secret, and TCP daemon startup requires its authentication token. Absence of a
file does not relax these requirements.
