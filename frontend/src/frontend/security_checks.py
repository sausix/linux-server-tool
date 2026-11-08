"""Administrative security assessment of effective frontend settings."""
import ipaddress
import time
from urllib.parse import urlparse
from sqlalchemy import select, func
from common.security_checks import finding, private_file
from frontend.models import User, SecurityKey, AuditOutbox


def assess(settings, request, db):
    checks = [finding('environment-prefix', 'Environment namespace', 'info',
        'Active prefix: ' + (settings.env_prefix or '(none)'),
        'Set the component ENV_PREFIX selector or use --env-prefix. Custom prefixes are isolated from compatibility names; shared support switches remain unprefixed.')]
    checks.append(finding('components', 'Component support switches', 'info',
        ', '.join(name + ': ' + ('enabled' if getattr(settings, name + '_support') else 'disabled') for name in ('docker', 'network', 'system')),
        'Use the same DOCKER_SUPPORT, NETWORK_SUPPORT and SYSTEM_SUPPORT values for web and daemon. Access modes independently restrict enabled components.'))
    def add(*args):
        checks.append(finding(*args[:3], *(settings.format_env_references(value) for value in args[3:])))
    https = urlparse(settings.public_url).scheme == 'https'
    add('public-https', 'Public HTTPS URL', 'pass' if https else 'critical',
        'The public URL uses HTTPS.' if https else 'The public URL uses HTTP.',
        'Set FRONTEND_PUBLIC_URL to the actual public HTTPS origin. This check does not verify the public certificate or reverse proxy.')
    add('cookie', 'Secure session cookie', 'pass' if settings.secure_cookie else 'critical',
        'Secure cookies are enabled.' if settings.secure_cookie else 'Session cookies may be sent over HTTP.',
        'Use FRONTEND_COOKIE_SECURE=always or auto with an HTTPS public URL. HttpOnly and SameSite=Lax are enforced by the application.')
    add('http-policy', 'Backend HTTP policy', 'warning' if settings.http_policy == 'allow' else 'pass',
        f'FRONTEND_HTTP_POLICY is {settings.http_policy}.',
        'Use https-only or redirect after validating trusted proxy forwarding; keep the backend unreachable from untrusted clients.')
    add('observed-https', 'Current request scheme', 'pass' if request.url.scheme == 'https' else 'warning',
        'The ASGI request is recognized as HTTPS.' if request.url.scheme == 'https' else 'The ASGI request is recognized as HTTP.',
        'Check trusted proxy headers or direct TLS. This observation is not an independent test of public TLS.')
    broad_hosts = '*' in settings.trusted_host_list or any(value.startswith('*.') for value in settings.trusted_host_list)
    add('hosts', 'HTTP Host allowlist', 'warning' if broad_hosts else 'pass',
        'Wildcard hosts are allowed.' if broad_hosts else 'The Host allowlist contains no wildcard entries.',
        'Allow only the required public and local health-check hostnames in FRONTEND_TRUSTED_HOSTS.')
    trust = [value.strip() for value in settings.forwarded_allow_ips.split(',') if value.strip()]
    broad_proxy = '*' in trust
    for value in trust:
        try:
            broad_proxy |= ipaddress.ip_network(value, strict=False).prefixlen == 0
        except ValueError:
            pass  # Uvicorn also supports Unix socket paths.
    add('proxy', 'Forwarded-header trust', 'critical' if broad_proxy else 'info',
        'All-source proxy trust is configured.' if broad_proxy else 'Forwarded-header trust is limited or disabled.',
        'Trust only actual proxy peers, replace incoming forwarded headers at ingress, and prevent direct public backend access. Network topology cannot be verified here.')
    add('environment', 'Production environment', 'pass' if settings.environment == 'production' else 'warning',
        f'The environment is {settings.environment}.', 'Set FRONTEND_ENVIRONMENT=production for deployment.')
    add('secret', 'Persistent session signing secret', 'pass' if settings.secret_key else 'warning',
        'A persistent secret is configured.' if settings.secret_key else 'An ephemeral development secret is in use.',
        'Provide a random secret of at least 32 characters through protected configuration. Its value is never displayed; entropy cannot be inferred from length.')
    reachable, fallback = [], True
    for rule in settings.login_rules:
        reachable.append(rule)
        if '*' in rule.users and not rule.networks:
            fallback = False
            break
    policy_requires = (not fallback or settings.webauthn_mode == 'required') and all(
        rule.action == 'deny' or (rule.webauthn or settings.webauthn_mode) == 'required' for rule in reachable)
    add('mfa-policy', 'FIDO2 login policy', 'pass' if policy_requires else 'warning',
        'Allowed login rules inherit or require WebAuthn.' if policy_requires else f'The default WebAuthn mode is {settings.webauthn_mode}; mandatory MFA is not ensured for every allowed rule.',
        'Prefer required MFA for privileged Internet-facing accounts. Required mode initially permits restricted password-based enrollment; review that bootstrap risk.')
    users = db.scalars(select(User).where(User.is_active.is_(True))).all()
    counts = dict(db.execute(select(SecurityKey.user_id, func.count()).group_by(SecurityKey.user_id)).all())
    missing = sum(not counts.get(user.id) for user in users)
    single = sum(counts.get(user.id) == 1 for user in users)
    add('mfa-enrollment', 'Active-account security keys', 'warning' if missing or single else 'pass',
        f'{missing} active account(s) have no security key; {single} have only one.',
        'Enroll FIDO2 keys for every account and keep a second key. Recovery codes and a key-recovery UI are not implemented.')
    expired = sum(user.password_change_required() for user in users)
    add('passwords', 'Required password changes', 'warning' if expired else 'pass',
        f'{expired} active account(s) require a password change.', 'Replace temporary or expired passwords; such accounts have profile-only access.')
    add('sessions', 'Session expiry budgets', 'warning' if settings.session_max_age_seconds > 3600 or settings.session_absolute_max_age_seconds > 43200 else 'pass',
        f'Idle limit: {settings.session_max_age_seconds} s; absolute limit: {settings.session_absolute_max_age_seconds} s.',
        'Use short idle and absolute lifetimes appropriate to privileged administration. Logout revokes server sessions; password/key changes revoke all account sessions.')
    add('bodies', 'Request and upload budgets', 'info',
        f'Request: {settings.request_max_bytes} bytes; POST: {settings.post_max_bytes}; login: {settings.login_max_bytes}; upload: {settings.upload_max_bytes}, {settings.upload_max_files} files.',
        'These validated limits run before parsing. Keep budgets minimal and also enforce connection, header, rate and read-timeout limits at ingress.')
    add('throttle', 'Login failure limits', 'info',
        f'Account: {settings.login_failure_limit}; source: {settings.login_ip_failure_limit}; window: {settings.login_failure_window_seconds} s; block: {settings.login_block_seconds} s.',
        'Persistent account/source throttling supplements ingress rate limits. Restrict sources where possible; deliberate temporary account lockouts remain possible.')
    add('sources', 'Login source restrictions', 'pass' if settings.login_allowed_networks else 'info',
        'A global source allowlist is configured.' if settings.login_allowed_networks else 'No global source allowlist is configured; individual login rules may still restrict access.',
        'Consider FRONTEND_LOGIN_ALLOWED_NETWORKS or a VPN for administration. Rules use first-match semantics; add a final deny rule for default-deny behavior.')
    count, oldest = db.execute(select(func.count(), func.min(AuditOutbox.created_at))).one()
    age = max(0, int(time.time() - oldest)) if oldest else 0
    add('audit', 'Daemon audit delivery queue', 'warning' if count >= 1000 or age > 60 else 'pass',
        f'{count} queued event(s); oldest age: {age} s. Queue capacity: 10000.',
        'Investigate a growing queue or delivery warnings. Protect local logs, configure retention, and monitor overflow; an empty queue does not prove durable log retention.')
    if settings.database_url.startswith('sqlite:///') and ':memory:' not in settings.database_url:
        checks.append(private_file('database', 'Web database permissions', settings.database_url.removeprefix('sqlite:///').split('?', 1)[0]))
    add('daemon-channel', 'Web-to-daemon transport', 'pass' if settings.daemon_endpoint.startswith('unix://') else 'critical',
        'A Unix socket is configured.' if settings.daemon_endpoint.startswith('unix://') else 'The web-to-daemon TCP channel has no native TLS.',
        'Prefer restricted Unix sockets; keep raw TCP private and use an authenticated encrypted tunnel. Native secure daemon transport remains a security TODO.')
    add('schema', 'API documentation exposure', 'warning', 'API documentation and the OpenAPI schema are publicly available.',
        'Restrict /docs, /redoc and /openapi.json at ingress if public schema access is unnecessary. Endpoint authentication remains enforced.')
    add('external', 'External security controls', 'unknown',
        'Public certificates, HSTS, proxy caching, ingress limits, installed CVEs, backups and host isolation are not independently verified.',
        'Review the deployment and perform dependency scanning and external testing. This page is a configuration assessment, not a penetration test or certification.')
    return checks
