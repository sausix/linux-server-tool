# Security measures and work remaining

This is the current security reference for the application. It covers an Internet-facing
HTTPS web deployment and the privileged backend connection. Last updated:
2026-11-05. The [earlier review](review.md) is a historical assessment;
its original findings must not be read as the current implementation status.
Security tests reduce risk but do not establish that every vulnerability is absent.

## Implemented measures

| Area | Protection and behavior |
| --- | --- |
| Passwords | Argon2 password hashes, generic credential errors, dummy verification for unknown accounts, password length limits. The recovery admin is created at startup when missing; its generated password is temporary and printed only at initialization. Protect these logs. |
| Sessions | Cookies contain a signed random opaque identifier, not user claims. Only its SHA-256 hash is stored as the database lookup key. Login rotates identifiers and CSRF tokens. Legacy client-side session cookies are rejected. |
| Revocation | Sign out revokes the current database session before responding. A password change revokes **all** sessions of that account, including the requesting session, and requires another login. Security-key registration/removal does the same. Expired, disabled-account and changed-password sessions cannot authorize requests. Concurrent responses cannot resurrect a revoked row. |
| Lifetime | Authenticated requests renew the idle expiry, bounded by an absolute lifetime. Persistent cookies are configurable. Closing a tab never logs out; some browsers restore session cookies on restart. Use Sign out. |
| Streams | Event and Docker log subscriptions monitor the session. Same-process logout signals immediately; other web workers and expiry/account changes are checked at intervals of 0.5 seconds. Streams close with a session-expired event. Data already delivered cannot be recalled. An idle open stream does not indefinitely renew a session. |
| Login throttling | Persistent account and source-IP counters apply across web workers and restarts with the same database/secret. IPv6 attempts are grouped by configurable prefix. Temporary blocks return HTTP 429 with Retry-After. A per-process semaphore bounds simultaneous password verification. Successful authentication clears the account bucket, **not** the source-IP bucket. Password reauthentication and invalid MFA assertions also count. |
| Source restrictions | An optional IP/CIDR allowlist and ordered username/network rules restrict login and authenticated use. Only the ASGI client address is read; Uvicorn may obtain it from explicitly trusted proxies. Arbitrary incoming X-Forwarded-For values are not used by application code. |
| MFA | FIDO2 security keys via WebAuthn, using the maintained py_webauthn library. Challenge, origin, RP ID, signature, user presence, user verification and signature counter are verified. Challenges/grants are random, short-lived, single-use and bound to account, server session and operation. Only public key material is retained. |
| Credential management | Initial key registration requires the current password. With MFA enabled and an existing key registered, adding/removing a key also requires an assertion from an existing key. A short-lived one-use grant authorizes that specific operation. Required mode prevents removal of the last key. |
| Restricted accounts | Temporary/expired passwords allow only the User settings/profile flow, session status and logout. No inventory, editor or logs are accessible. Required MFA without a key grants only an enrollment/profile session after password verification. Password renewal precedes enrollment. |
| Settings | The header Settings link opens a separate User settings navigation. Display name and contact email are editable; changing email needs the current password. Email is unverified and is not an authentication or recovery identity. Active sessions show method, address, user agent and creation time. |
| Audit | Structured security events are logged locally and queued durably in web SQLite for delivery to backend. The daemon logs validated JSON under backend.security, even when Docker/network/system features are disabled. Login success/failure/denial/throttling, login method, session creation/revocation, password/profile changes and MFA actions are recorded. |
| Request sizes | ASGI middleware enforces actual received bytes before form/multipart parsing, including chunked requests. Login has a small budget. Unrelated multipart uploads are rejected. Compose imports have separate compressed/expanded byte and file-count limits. Both total-request and POST budgets are configurable. Validation errors never echo submitted field values. |
| Browser protections | HttpOnly, SameSite=Lax and configurable Secure session cookies; CSRF checks on state-changing routes; escaped templates and safe DOM text rendering; CSP with self-only resources, form-action and base-uri; frame-ancestors none and X-Frame-Options DENY; nosniff and same-origin referrer policy. |
| Caching | Every non-static web response gets Cache-Control: no-store, including authentication, profile, sensitive JSON and streams. Cookie-dependent responses vary on Cookie. Static assets retain normal cache behavior. Configure proxies not to cache authenticated traffic. |
| Privileged operations | Feature access modes cap daemon reads/writes independently. Mutations require authentication, CSRF and explicit previews/confirmations as appropriate. Compose access is bounded to configured roots, validates hashes/revisions, rejects archive traversal/links and preserves file permissions. Drafts are per user and never deploy implicitly. |
| Local service boundaries | Unix sockets use filesystem permissions. TCP daemon listeners require a token. Docker socket access is root-equivalent; exposing it through a web backend makes access to the backend highly privileged. |

## Login policy and security keys

See [web configuration](../frontend/README.md) for every environment variable and
default. Settings also have corresponding CLI flags (`frontend --help`); CLI
values override environment values. Lists and rules use JSON in environment
values. Native `.env` example:

```dotenv
FRONTEND_LOGIN_ALLOWED_NETWORKS=["192.0.2.0/24","2001:db8:1234::/48"]
FRONTEND_WEBAUTHN_MODE=optional
FRONTEND_LOGIN_RULES='[{"users":["admin"],"networks":["192.0.2.0/24"],"webauthn":"required"},{"users":["admin"],"action":"deny"}]'
```

The global source allowlist is always applied first. An empty allowlist imposes
no global source restriction. The **first** matching rule wins; omitted users
mean all users, omitted networks mean all sources. Rules can allow/deny and
choose the WebAuthn mode. With no matching rule, access is allowed using the
global MFA mode. Add an explicit final deny rule for a default-deny rule list.
Usernames are case-insensitive. Unknown source addresses fail configured network
restrictions. Do not configure the reverse proxy as the client allowlist unless
all callers of that proxy should share that access.

| WebAuthn mode | Login behavior |
| --- | --- |
| off | Password-only login; key registration and key authentication are disabled. Existing public credentials remain stored. Removal is possible with password reauthentication. |
| optional (default) | Password-only before enrollment. Once a key is registered, that account **must** also present a key on subsequent logins. Optional does not mean a registered factor can be skipped. |
| required | Password plus key. Accounts without a key receive only the restricted profile/enrollment flow after password verification. |

Register an external FIDO2 security key under **Settings → User**. User
verification (PIN or biometrics) is required; touch-only U2F tokens without user
verification are insufficient. Keep a second key. The browser must support
WebAuthn and use HTTPS (localhost is allowed for development).
`FRONTEND_PUBLIC_URL` supplies the exact expected origin and RP hostname; changing
the RP hostname can make registered keys unusable. Public URL and Secure-cookie
policy remain operator settings and are not changed automatically.

The browser is asked for an external authenticator, but attestation is not used
to certify hardware provenance. This is a cryptographic WebAuthn factor, **not**
a guarantee that a particular hardware model was used. There are no recovery
codes, SMS, TOTP, email recovery or administrator key-reset UI yet. Required-mode
bootstrap permits a password holder to register the first factor; pre-enrollment
and recovery policies need additional controls if that is unacceptable.

## Audit operation and privacy

Events contain bounded, control-character-sanitized fields: event ID, UTC time,
username/user ID, a separate non-secret session audit ID, login method, normalized
source IP, user agent, outcome/reason and an optional credential fingerprint.
Passwords, raw session cookies, bearer tokens, private keys and WebAuthn response
bodies are excluded. Source IP and user agent are personal data; protect logs and
set an appropriate retention policy in journald/the log collector and backups.

Web delivery is asynchronous and at least once. A daemon outage keeps events in
the SQLite outbox and emits a warning. Up to 10,000 queued events are retained;
on overflow the oldest is dropped from the queue with an explicit error, while
local structured logging remains the fallback. Configure a durable local log
sink. Duplicate delivery can occur after a lost acknowledgement; event IDs allow
collector-side deduplication. The queue drains after reconnection. A client with
daemon credentials can submit audit events, so daemon logs are not independently
trusted evidence of web identity until the transport/identity TODOs below are
implemented. Daemon events currently **do not ban** addresses or subnets.

## Deployment requirements

- Terminate HTTPS securely, set the public URL and Secure-cookie policy, and trust
  only the actual reverse proxy. The proxy must replace forwarded headers and
  prevent direct public access to the backend listener.
- Add ingress connection/rate/concurrency, header-size, body-size and read-timeout
  limits. ASGI byte limits do not solve slow clients or aggregate connection DoS.
- Prefer permission-restricted local Unix sockets. Raw daemon TCP has token
  authentication but **no TLS**; do not publish it to an untrusted network.
- Restrict the web data directory, SQLite journals/backups, `.env`, initial admin
  logs and Compose drafts. Recommended: directory 0700, files 0600, dedicated
  service account. The supplied image and systemd unit use a private creation mask (0077); existing files, mounted volumes and direct CLI deployments still need operator permission checks.
- Protect both web and daemon SQLite state against rollback/tampering. Restoring
  an old backup can restore revoked sessions, keys or throttle state. Rotate the
  web secret and require fresh authentication after security-sensitive restores.
- Do not iframe the administration UI. Keep reverse-proxy caches disabled for
  authenticated content. Configure HSTS at the HTTPS ingress after validating
  the domain and its subdomains; do not enable preload indiscriminately.
- Account-wide throttling can be deliberately triggered against an account.
  Blocks are temporary; a gateway allowlist/VPN and external rate limits remain
  valuable. Multiple independent web databases do not share counters/sessions.

## Security TODOs

| Priority | Work remaining |
| --- | --- |
| High | **Secure web–daemon communication:** authenticated encryption (verified mutual TLS or equivalent), peer identity/authorization, token rotation, scoped service credentials and replay-aware audit submission. Keep raw TCP private until implemented. |
| High | Configurable daemon-side IP/subnet blocking based on login events: trusted event sources, thresholds, expiry, allowlists, preview/audit, IPv6 policy and recovery safeguards. Do not automatically ban proxy addresses. |
| High | Dedicated ingress DoS controls: global/per-source connection and request concurrency, read/header timeouts, upload rate/disk quotas, deployment validation. The password semaphore is per process. |
| High | MFA recovery and controlled enrollment: backup/recovery codes, secure administrator recovery, pre-enrollment/first-key approval, recent-auth requirements for further sensitive actions. Recovery must not bypass mandatory MFA silently. |
| Medium | Hardware attestation policy/trust roots if only approved physical tokens may be enrolled; broader real-browser/token interoperability coverage, clone/backup credential policy and alerting. |
| Medium | Audit retention/rotation/export, tamper resistance, delivery metrics/alerts and authenticated deduplication; configurable outbox capacity/overflow policy. Persisted expired/revoked session rows need bounded retention/cleanup. |
| Medium | Strict private filesystem modes for database, journals and backups enforced and verified at startup; review secret encryption at rest and secure backup operations. |
| Medium | Configurable production exposure/authentication of OpenAPI/docs. Ordinary unauthenticated callers can currently read the schema. Restricted profile sessions cannot. |
| Medium | Additional external penetration testing, continuous dependency advisory scans and CI security checks; pinned/reproducible deployments and prompt FastAPI/Starlette/Uvicorn/parser/crypto updates. The workspace development group requires pytest 9.0.3 or newer; continue checking the locked toolchain for advisories. |
| Medium | Review concurrency/transaction boundaries for simultaneous credential changes, session preference updates and definition mutations under multiple workers. Add comprehensive authorization matrices and operation-level audit trails for all privileged mutations. |
| Low | Optional individual-session termination UI, recognizable device naming, suspicious-login notifications and configurable privacy retention. The current UI lists active sessions; logout/password/key changes provide revocation. |
| Low | Add Permissions-Policy and tested additional browser hardening without breaking WebAuthn, log windows, editors or reverse proxies; refine CSP as the UI evolves. |

## Verification

Automated tests exercise real signed WebAuthn responses, origin/challenge/user
verification rejection, replay resistance, session revocation, password-change
logout, restricted profiles, throttling, source rules, request limits and audit
validation. A Chromium browser test also covers the shipped registration/login UI with a
virtual CTAP2 authenticator. Tests use isolated databases and synthetic keys;
they do not prove
physical-token compatibility or validate a real Internet-facing reverse proxy.

## Administrative security assessment

Administrators with completed password/MFA setup can open **Settings → Security**.
The page runs read-only checks against the effective web configuration and asks
the daemon for its local assessment. It shows observations, recommendations and
separate Critical, Warning, Unknown, Info and Pass results. Use **Run checks again**
to refresh; the page does not apply changes or automatically reload.

Frontend checks cover HTTPS/cookie/proxy settings, Host restrictions, environment,
signing-secret persistence, MFA policy/enrollment, required password changes,
session/body/throttle budgets, source restrictions, audit delivery, database
permissions, daemon transport and public schema exposure. Daemon checks cover
its transport and actual socket mode, token configuration, Docker transport/TLS,
component write modes, external Compose protection, source roots, state-file/key
permissions, debug logging and process identity. Secrets and raw file contents
are never included. A long configured token is not proof of sufficient entropy.

Pass means the stated local configuration check passed, not that the complete
system is secure. File checks observe basic mode bits in each process namespace;
parent directories, ACLs and host/container boundaries need separate review.
Configured Docker TLS is not a completed certificate/connection test. Public
TLS, ingress firewall, HSTS, proxy caching/limits, installed dependency advisories,
backup security and audit retention remain externally unverified. An unavailable
or older daemon is shown as Unknown while frontend results remain available.
Non-administrators cannot access the page even by entering its URL directly.

Login diagnostics warn when a request is recognized as HTTP while Secure cookies
are required. Missing/stale sign-in sessions return an HTML explanation with HTTP
403, and audit events distinguish missing sessions from invalid CSRF tokens.
Cookie security and CSRF validation are never automatically relaxed. See
[login troubleshooting](../common/operations.md#login-fails-with-a-csrf-error).

Optional components use shared support gates independently of read/write access.
UI extensions accept validated RGB colors and local application links only; provider
text remains escaped. Disabling Docker does not weaken networking/System access
checks. See [optional components](../common/components.md).

Configuration namespaces can be selected independently for each component.
Custom namespaces do not inherit compatibility-prefixed credentials, reload
children preserve the selected namespace, and startup validation messages omit
sensitive input values. Display branding is escaped text and independent of
persistent session/managed-file identities.

Complete ENV references contain defaults rather than deployment-specific changes.
Optional empty settings select their default; an explicitly empty standard secret
never falls back to a legacy credential. Production web startup and TCP daemon
startup still reject missing required credentials. No shared secret is supplied
in examples or Compose defaults.
