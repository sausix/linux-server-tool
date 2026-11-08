"""Authentication policy and session promotion shared by password and WebAuthn."""
import hashlib
import secrets
from fastapi import HTTPException
from sqlalchemy import select, func
from frontend.models import User, SecurityKey
from frontend.security import normalize_username
from frontend.login_policy import login_policy, source_ip


class Authentication:
    def __init__(self, settings, factory, sessions, throttle, audit):
        self.settings, self.factory, self.sessions, self.throttle, self.audit = settings, factory, sessions, throttle, audit

    def policy(self, username, request):
        return login_policy(self.settings, normalize_username(username), source_ip(request))

    def guard_login(self, username, request):
        allowed, mode = self.policy(username, request)
        if not allowed:
            self.audit.emit('login.denied', request, username=username, login_method='password', reason='source_rule', outcome='denied')
            raise HTTPException(403, 'Sign in is not allowed from this source')
        retry = self.throttle.blocked(username, source_ip(request))
        if retry:
            self.audit.emit('login.throttled', request, username=username, reason='failure_limit', outcome='blocked')
            raise HTTPException(429, 'Too many failed sign-in attempts. Try again later.', headers={'Retry-After': str(retry)})
        return mode

    def failed(self, username, request, method='password', reason='invalid_credentials'):
        self.throttle.failure(username, source_ip(request))
        self.audit.emit('login.failed', request, username=username, login_method=method, outcome='failure', reason=reason)

    def current_user(self, request, db):
        row = request.scope.get('session_record') or {}
        user = db.get(User, row.get('user_id')) if row.get('user_id') else None
        if not user or not self.sessions.valid(row['id'], user.id):
            return None
        allowed, mode = self.policy(user.username, request)
        if not allowed:
            self.sessions.revoke(row['id'], reason='source_rule', request=request)
            request.session.clear(); request.scope['destroy_session'] = True
            return None
        keys = db.scalar(select(func.count()).select_from(SecurityKey).where(SecurityKey.user_id == user.id))
        if mode != 'off' and keys and row['login_method'] != 'password+webauthn':
            self.sessions.revoke(row['id'], reason='mfa_policy', request=request)
            request.session.clear(); request.scope['destroy_session'] = True
            return None
        required_password = user.password_change_required()
        required_enrollment = mode == 'required' and not keys
        allowed_profile = {'/settings', '/settings/user', '/settings/user/profile', '/settings/user/password', '/password', '/logout', '/api/session'}
        path = request.url.path
        if required_password or required_enrollment:
            if path not in allowed_profile and not (required_enrollment and not required_password and path.startswith('/settings/user/security-keys/')):
                raise HTTPException(403, 'Profile update required', headers={'X-Profile-Required': 'true'})
        request.scope['authenticated_user'] = True
        request.scope['password_change_required'] = required_password
        request.scope['mfa_enrollment_required'] = required_enrollment
        return user

    def complete(self, user, request, method):
        allowed, mode = self.policy(user.username, request)
        if not allowed:
            raise HTTPException(403, 'Sign in is not allowed from this source')
        old = request.scope.get('session_record')
        data = {'user_id': user.id, 'csrf_token': secrets.token_urlsafe(32)}
        cookie, data, row = self.sessions.create(user.id, data, source_ip(request), request.headers.get('user-agent', ''), method, user.password_hash)
        if old:
            self.sessions.revoke(old['id'], reason='session_rotated', request=request)
        request.session.clear(); request.session.update(data)
        request.scope.update(session_record=row, new_session_cookie=cookie, authenticated_user=True)
        self.throttle.success(user.username, source_ip(request))
        self.audit.emit('session.created', request, user, login_method=method, outcome='success')
        self.audit.emit('login.succeeded', request, user, login_method=method, outcome='success')
        return '/settings/user' if user.password_change_required() or method == 'password-enrollment' else '/'

    @staticmethod
    def password_fingerprint(user):
        return hashlib.sha256((user.password_hash or '').encode()).hexdigest()
