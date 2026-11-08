# Historical public HTTPS deployment security review

> This report predates the security implementation. Current measures, limitations
> and outstanding work are maintained in [SECURITY.md](SECURITY.md).

Review date: 2026-11-05. Findings describe the code and installed development
environment at this date. No application behavior or deployment settings were
changed during this review.

## Scope and assessment

The review covered authentication, signed sessions, cookies, CSRF, authorization,
HTML/JavaScript output, iframe protections, uploads, live streams, proxy trust,
local storage and dependency advisories. Reproduction used isolated temporary
SQLite databases, synthetic accounts and mocked daemon responses. No live server,
real Docker resources or host network configuration was modified.

HTTPS is necessary but does not address the confirmed session-revocation and
availability weaknesses below. These should be addressed before relying on the
application as a publicly reachable privileged administration service. Existing
proxy rate limits or identity-provider protections can reduce exposure, but their
configuration was not supplied or verified. This is a source review with targeted
checks, not a claim that every possible vulnerability has been excluded.

## Prioritized findings

| ID | Priority | Finding | Evidence and exposure | Recommended action |
| --- | --- | --- | --- | --- |
| S1 | High | Copied sessions survive logout and password changes | A previously valid cookie returned HTTP 200 from `/api/session` after logout, and again after changing the account password. Logout only removes the current browser's cookie. Possession of a copied cookie is required. | Use revocable server-side sessions with random session identifiers. Revoke the current session on logout and other sessions on password changes. Rotate the current session after authentication and credential changes. |
| S2 | High | Authentication lacks application-level throttling and MFA | Login performs expensive Argon2 verification without account/IP throttling, progressive delay or MFA. Repeated bad credentials receive the ordinary login response. This exposes password guessing, credential stuffing and CPU/memory exhaustion unless an external control already limits them. | Add account-aware and trusted-client-IP-aware throttling with progressive backoff and audit events. Put MFA/SSO at the gateway or implement a second factor. Avoid permanent lockouts that an attacker can trigger against the recovery account. |
| S3 | High if ingress is unrestricted | Anonymous multipart uploads can consume temporary storage before authentication | A synthetic unauthenticated POST to `/login` with an unrelated 1,100,000-byte file was parsed and spooled to disk before the invalid CSRF token caused HTTP 403. No global body/file quota or frontend concurrency limit is configured. This is independent of historical parser CVEs. | Enforce request-byte limits before parsing, at both proxy and ASGI layers. Auth forms need small limits and should reject unrelated uploads. Give import/editor routes separate budgets. Add request, connection, time and temporary-storage limits. |
| S4 | Medium | Live streams do not promptly honor account/session revocation | Authentication is checked when a stream opens; stream loops run until their configured duration or disconnect. They do not periodically recheck account status. | Associate streams with revocable sessions and close them on logout, password change or account disablement; revalidate during heartbeats. |
| S5 | Medium policy concern | Temporary/expired-password accounts can still read potentially sensitive data | An account marked as requiring a password change could read synthetic container logs (HTTP 200), while stack editing correctly returned HTTP 403. Ordinary active users can also read Docker inventories/logs without a separate sensitive-read permission. Logs and labels may contain credentials. | Decide which reads are appropriate before a required password change. Limit such sessions to password update/logout if that is the policy. Separate general monitoring from logs, configuration and other sensitive reads. |
| S6 | Medium, configuration-dependent | Secure-cookie behavior depends on the configured public URL | HTTPS at the proxy alone does not enable the cookie's Secure flag: automatic selection uses `FRONTEND_PUBLIC_URL`. Production configuration can still explicitly allow insecure cookies. | Set an HTTPS public URL and `FRONTEND_COOKIE_SECURE=always`. Reject contradictory production settings. Consider a `__Host-` cookie name in a future change; retain Path=/ and no Domain attribute. |
| S7 | Medium/low hardening | Sensitive responses do not uniformly prohibit caching | `/password` did not return Cache-Control: no-store. Only selected HTML paths apply it globally, although many JSON and editor responses already set it. | Apply no-store to login/password pages and authenticated HTML/JSON, including logs and configuration. Keep appropriate caching for static assets. Do not cache authenticated responses at the proxy. |
| S8 | Medium, local access required | Web database confidentiality depends on filesystem defaults | A database created under a normal umask had mode 0644. It contains password hashes and may contain Compose drafts with secrets. Actual access also depends on all ancestor directory permissions and volume ownership. | Use a dedicated service identity, a private data directory and restrictive database/journal/backup permissions; for example directory 0700 and files 0600. Protect initial-password logs and secret-key configuration too. |
| S9 | High only on an untrusted daemon network | Frontend-to-daemon TCP is not encrypted | The optional TCP client sends token-authenticated JSON without TLS. Browser HTTPS protects a different connection. | Prefer the permission-restricted Unix socket. For remote daemons, use a protected tunnel/private authenticated encrypted network; never expose the raw daemon listener publicly. |
| S10 | Low | API documentation/schema remain public | `/openapi.json` is available without signing in. This reveals routes and parameter structure, not authentication credentials. | Disable or authenticate OpenAPI/docs in production if not needed. Route authorization remains mandatory either way. |

### Session lifetime and recovery

The current timeout is renewable and has no separate absolute lifetime. A copied
valid session can therefore be kept alive by continued authenticated requests.
Add an idle timeout and a separate absolute lifetime, with reauthentication for
sensitive actions. A browser-session cookie without Max-Age does not guarantee
logout when the browser closes: browsers may restore session cookies.

Until session revocation is implemented, rotating `FRONTEND_SECRET_KEY` and restarting
the web application invalidates existing signed cookies globally. A password
change alone does not currently accomplish that. Do not put secrets, Compose
contents or password hashes into signed cookie session data: signed data is not
encrypted.

### Resource budgets

The bounded stack import handlers are a useful existing protection. The gap is
other routes that allow framework form parsing before their authentication/CSRF
checks. Multipart field-count and field-size limits are not a total upload-byte
quota. Small authentication budgets, larger editor/import budgets and explicit
concurrency limits should be selected and tested together; rejecting Content-Length
alone is insufficient for streamed requests. Current Starlette provides a
[request body limit middleware](https://starlette.dev/middleware/#requestbodylimitmiddleware)
that counts received bytes.

## Existing protections that are working

| Area | Observed protection and limits |
| --- | --- |
| Password storage | Argon2 via pwdlib; generic login errors; a dummy hash for unknown accounts. |
| Cookies | Under the tested HTTPS public URL, cookies had Secure, HttpOnly and SameSite=Lax. No Domain attribute is configured. Cookie signatures prevent ordinary tampering. |
| CSRF | State-changing handlers check session-bound random tokens, with constant-time comparison. Important mutations also require administrator write access and a current password. |
| Iframes/clickjacking | X-Frame-Options: DENY and CSP frame-ancestors 'none' reject embedding, including same-origin iframes. |
| XSS | Jinja autoescaping, JSON escaping in data/script contexts, and DOM textContent/createElement usage. No innerHTML/eval-style sink was found in frontend source. A synthetic log-title payload was escaped rather than emitted as executable markup. This is not proof that all XSS possibilities are absent. |
| CSP | Same-origin defaults prohibit third-party scripts, inline scripts and eval by default. No unsafe-inline or unsafe-eval exception was observed. |
| Browser boundaries | No permissive CORS sharing was observed. Log popups use noopener/noreferrer. Static assets come from a dedicated asset directory; a targeted encoded traversal request returned 404. |
| Authorization/data handling | Compose editors and mutations require elevated access; drafts are queried by owner. SQLAlchemy uses bound expressions for the inspected credential queries. Source/import operations have path, size, hash and archive validation. |

CSRF, SameSite and CORS solve different problems. SameSite and absence of CORS do
not replace CSRF checks; HttpOnly does not prevent malicious same-origin code from
performing actions. Preserve the existing defenses when adding routes.

## Dependency advisory check

The scanner queried advisory data for 40 installed third-party package entries,
with none skipped. All 31 packages in the frontend's resolved runtime dependency
closure were covered. No known advisory match was returned for that runtime
closure at review time.

| Package | Installed version inspected |
| --- | --- |
| FastAPI | 0.162.0 |
| Starlette | 1.10.0 |
| Uvicorn | 0.54.0 |
| python-multipart | 0.0.32 |
| Jinja2 | 3.1.6 |
| itsdangerous | 2.2.0 |
| pwdlib / argon2-cffi | 0.3.1 / 25.1.0 |
| SQLAlchemy | 2.1.6 |
| pydantic / pydantic-settings | 2.14.2 / 2.16.2 |

The scanner returned two duplicate records for one development-only issue:
**pytest 8.4.2, CVE-2025-71176 / GHSA-6w46-j5rx-g56g**, fixed in 9.0.3. It concerns
local temporary-directory handling; it is not an exposed FastAPI endpoint and
pytest is not declared as a frontend production dependency. Upgrade development
and CI installations. See the
[reviewed advisory](https://github.com/advisories/GHSA-6w46-j5rx-g56g).

The inspected Starlette/python-multipart versions are above the fixes for the
reviewed historical
[Range processing issue](https://github.com/Kludex/starlette/security/advisories/GHSA-7f5h-v6xp-fcq8),
[Host parsing issue](https://github.com/Kludex/starlette/security/advisories/GHSA-86qp-5c8j-p5mr),
[urlencoded form-limit issue](https://github.com/Kludex/starlette/security/advisories/GHSA-82w8-qh3p-5jfq)
and [multipart header issue](https://github.com/Kludex/python-multipart/security/advisories/GHSA-pp6c-gr5w-3c5g).
The application's broad dependency ranges still permit older vulnerable releases.
Maintain tested constraints/lock data and advisory scanning in CI and scan the
actual deployed image. The inspected environment uses Python 3.14; the supplied
web image builds on Python 3.12. Server/container package versions and OS packages
were not verified by this local audit.

## Deployment measures

1. Protect public administration with MFA or an identity-aware gateway; keep
   application authentication and administrator/write checks as well.
2. Make backend listeners reachable only from the actual reverse proxy. Trust
   only its IP addresses in `FRONTEND_FORWARDED_ALLOW_IPS`, not all clients. Have the
   proxy overwrite forwarded headers and use an exact Host allowlist.
3. Set `FRONTEND_PUBLIC_URL` to the public HTTPS URL and
   `FRONTEND_COOKIE_SECURE=always`. Select HTTPS enforcement with correct forwarded
   scheme handling; adapt direct HTTP health checks before changing that policy.
4. Configure HSTS at the HTTPS edge and verify it on public responses. The
   application itself does not currently emit it. Use includeSubDomains/preload
   only when the entire affected domain supports HTTPS permanently.
5. Add body/rate/concurrency limits, request timeouts and private temporary-storage
   quotas. Allow the documented import limits only on the relevant routes.
6. Keep the existing CSP and frame protections; additionally consider
   object-src 'none', base-uri 'none', explicit script/connect directives and a
   restrictive Permissions-Policy. Cover errors and redirects where appropriate.
7. Use restrictive file permissions, protected backups and structured security
   logging. Log authentication failures, session revocation and privileged changes;
   never log passwords, cookies, CSRF tokens or full Compose secrets.
8. Keep daemon features read-only unless control is needed and preserve socket
   permissions. Docker write access is effectively host administration access.

## Reference guidance

- [OWASP session management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)
- [OWASP authentication and throttling](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html)
- [OWASP FastAPI security](https://cheatsheetseries.owasp.org/cheatsheets/FastAPI_Security_Cheat_Sheet.html)
- [OWASP clickjacking defense](https://cheatsheetseries.owasp.org/cheatsheets/Clickjacking_Defense_Cheat_Sheet.html)
- [OWASP XSS prevention](https://cheatsheetseries.owasp.org/cheatsheets/Cross_Site_Scripting_Prevention_Cheat_Sheet.html)
- [Starlette session semantics](https://starlette.dev/middleware/#sessionmiddleware)
- [FastAPI behind a proxy](https://fastapi.tiangolo.com/advanced/behind-a-proxy/)
- [Cookie attributes and prefixes](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie)
