"""Source network rules and persistent account/IP failure counters."""
import hashlib
import hmac
import ipaddress
import time
import threading
from sqlalchemy import select, delete, text
from frontend.models import LoginBucket


def source_ip(request):
    try:
        value = ipaddress.ip_address(request.client.host)
        return str(value.ipv4_mapped or value) if value.version == 6 else str(value)
    except (ValueError, AttributeError):
        return 'unknown'


def in_networks(address, networks):
    try:
        ip = ipaddress.ip_address(address)
        return any(ip in ipaddress.ip_network(value, strict=False) for value in networks)
    except ValueError:
        return False


def login_policy(settings, username, address):
    if settings.login_allowed_networks and not in_networks(address, settings.login_allowed_networks):
        return False, settings.webauthn_mode
    for rule in settings.login_rules:
        if '*' not in rule.users and username not in [value.strip().lower() for value in rule.users]:
            continue
        if rule.networks and not in_networks(address, rule.networks):
            continue
        return rule.action == 'allow', rule.webauthn or settings.webauthn_mode
    return True, settings.webauthn_mode


class LoginThrottle:
    def __init__(self, factory, settings, secret):
        self.factory, self.settings, self.secret = factory, settings, secret.encode()
        self.slots = threading.BoundedSemaphore(settings.login_max_concurrency)

    def keys(self, username, address):
        try:
            ip = ipaddress.ip_address(address)
            if ip.version == 6:
                address = str(ipaddress.ip_network(f'{ip}/{self.settings.login_ipv6_prefix}', strict=False))
        except ValueError:
            address = 'unknown'
        return [hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest() for value in ('user:' + username, 'ip:' + address)]

    def blocked(self, username, address):
        now = time.time()
        with self.factory() as db:
            rows = db.scalars(select(LoginBucket).where(LoginBucket.id.in_(self.keys(username, address)))).all()
            return max([max(0, int(row.blocked_until - now) + 1) for row in rows if row.reset_at > now] or [0])

    def failure(self, username, address):
        now = time.time()
        with self.factory() as db:
            if db.bind.dialect.name == 'sqlite':
                db.execute(text('BEGIN IMMEDIATE'))
            db.execute(delete(LoginBucket).where(LoginBucket.reset_at < now))
            for key, limit in zip(self.keys(username, address), (self.settings.login_failure_limit, self.settings.login_ip_failure_limit)):
                row = db.get(LoginBucket, key)
                if row is None:
                    row = LoginBucket(id=key, failures=0, reset_at=now + self.settings.login_failure_window_seconds, blocked_until=0)
                    db.add(row)
                row.failures += 1
                if row.failures >= limit:
                    row.blocked_until = now + self.settings.login_block_seconds
                    row.reset_at = max(row.reset_at, row.blocked_until)
            db.commit()

    def success(self, username, address):
        with self.factory() as db:
            db.execute(delete(LoginBucket).where(LoginBucket.id == self.keys(username, address)[0])); db.commit()
