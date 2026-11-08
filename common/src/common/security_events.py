"""Bounded security event payloads shared by frontend and daemon."""
import ipaddress
from datetime import datetime

EVENTS = {'login.failed', 'login.denied', 'login.throttled', 'login.password_verified', 'login.succeeded',
          'session.created', 'session.revoked', 'session.expired', 'password.changed', 'profile.updated',
          'mfa.registration.started', 'mfa.registration.succeeded', 'mfa.registration.failed',
          'mfa.authentication.started', 'mfa.authentication.succeeded', 'mfa.authentication.failed',
          'mfa.authentication.cancelled', 'mfa.credential.removed'}
LIMITS = {'event_id': 32, 'timestamp': 64, 'event': 64, 'username': 64, 'user_id': 32,
          'session_id': 32, 'login_method': 40, 'source_ip': 64, 'user_agent': 512,
          'outcome': 32, 'reason': 120, 'credential': 64}


def clean(value, limit):
    return ''.join(char for char in str(value) if ord(char) >= 32 and ord(char) != 127)[:limit]


def validate_event(event):
    if not isinstance(event, dict) or event.keys() - LIMITS.keys() or not isinstance(event.get('event'), str) or event.get('event') not in EVENTS:
        raise ValueError('Invalid security event')
    for key, value in event.items():
        if not isinstance(value, str) or len(value) > LIMITS[key] or clean(value, LIMITS[key]) != value:
            raise ValueError('Invalid security event field')
    if not event.get('event_id') or not event.get('timestamp'):
        raise ValueError('Security events require an identifier and timestamp')
    datetime.fromisoformat(event['timestamp'])
    if event.get('source_ip', 'unknown') != 'unknown':
        ipaddress.ip_address(event['source_ip'])
    return dict(event)
