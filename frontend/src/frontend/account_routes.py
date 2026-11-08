"""User settings, password changes and FIDO2/WebAuthn ceremonies."""
from datetime import timedelta, datetime, timezone
import hashlib
import re
import time
from fastapi import Body, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select, update, delete
from sqlalchemy.orm import Session
from frontend.models import User, SecurityKey, AuthSession, utcnow
from frontend.security import csrf_is_valid, password_hasher, validate_new_password


def install_account_routes(app, render, get_db, current_user, safe_return_path):
    auth, sessions, audit, webauthn = app.state.authentication, app.state.sessions, app.state.security_audit, app.state.webauthn

    def user_access(request, db):
        user = current_user(request, db)
        if not user:
            raise HTTPException(401, 'Sign in required')
        return user

    def csrf(request, value):
        if not csrf_is_valid(request.session, value):
            raise HTTPException(403, 'Invalid CSRF token')

    def password_proof(request, user, value):
        auth.guard_login(user.username, request)
        if not auth.throttle.slots.acquire(blocking=False):
            raise HTTPException(429, 'Authentication capacity is busy', headers={'Retry-After': '2'})
        try:
            valid = isinstance(value, str) and len(value) <= 256 and password_hasher.verify(value, user.password_hash or '')
        finally:
            auth.throttle.slots.release()
        if not valid:
            auth.failed(user.username, request, 'password-reauthentication')
            raise HTTPException(403, 'The current password is incorrect')

    def profile_response(request, user, db, error=None, notice=None):
        profile = webauthn.profile(user, db)
        keys = webauthn.keys(user.id, db)
        _, mode = auth.policy(user.username, request)
        active = db.scalars(select(AuthSession).where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > time.time(), AuthSession.absolute_expires_at > time.time()).order_by(AuthSession.created_at.desc()).limit(50)).all()
        for item in active:
            item.created_display = datetime.fromtimestamp(item.created_at, timezone.utc).isoformat(timespec='seconds')
        return render(request, 'user_settings.html', user=user, hide_search=True, profile=profile, keys=keys,
                      mfa_mode=mode, required=user.password_change_required(), error=error, notice=notice,
                      enrollment_required=request.scope.get('mfa_enrollment_required'), active_sessions=active)

    @app.get('/settings')
    @app.get('/password')
    def settings_redirect(request: Request, db: Session = Depends(get_db)):
        user_access(request, db)
        return RedirectResponse('/settings/user', status_code=303)

    @app.get('/settings/user')
    def settings_user(request: Request, db: Session = Depends(get_db)):
        return profile_response(request, user_access(request, db), db)

    @app.get('/settings/security')
    def security_settings(request: Request, db: Session = Depends(get_db)):
        from collections import Counter
        from common.client import DaemonUnavailable
        from common.protocol import ProtocolError
        from common.security_checks import finding
        from frontend.security_checks import assess
        user = user_access(request, db)
        if not user.is_admin:
            raise HTTPException(403, 'Administrator access required')
        frontend = assess(auth.settings, request, db)
        try:
            result = app.state.daemon_client.call('security.checks', {})
            checks = result['checks']
            # Older or incompatible daemons must not render arbitrary objects.
            if (not isinstance(checks, list) or len(checks) > 100 or any(
                    not isinstance(item, dict) or item.get('status') not in {'pass', 'warning', 'critical', 'unknown', 'info'}
                    or any(not isinstance(item.get(key), str) or len(item[key]) > 4096 for key in
                           ('id', 'title', 'observation', 'recommendation')) for item in checks)):
                raise ValueError('Invalid assessment')
        except (DaemonUnavailable, ProtocolError, ValueError, KeyError, TypeError):
            checks = [finding('unavailable', 'Daemon assessment unavailable', 'unknown',
                'The daemon could not provide a compatible security assessment.',
                'Check connectivity and update backend if necessary. Review daemon logs; raw connection errors are not displayed here.')]
        counts = Counter(item['status'] for item in frontend + checks)
        return render(request, 'security_settings.html', user=user, hide_search=True,
            frontend_checks=frontend, daemon_checks=checks,
            summary={key: counts[key] for key in ('critical', 'warning', 'unknown', 'info', 'pass')},
            checked_at=utcnow().isoformat(timespec='seconds'))

    @app.post('/settings/user/profile')
    def profile_save(request: Request, display_name: str = Form(''), email: str = Form(''),
                     current_password: str = Form(''), csrf_token: str = Form(...), db: Session = Depends(get_db)):
        user = user_access(request, db); csrf(request, csrf_token)
        profile = webauthn.profile(user, db)
        if len(display_name) > 128 or any(ord(char) < 32 for char in display_name) or len(email) > 254 or (email and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email)):
            return profile_response(request, user, db, error='Enter a valid display name and contact email')
        if email != profile.email:
            password_proof(request, user, current_password)
        profile.display_name, profile.email = display_name.strip(), email.strip()
        db.commit(); audit.emit('profile.updated', request, user, outcome='success')
        return profile_response(request, user, db, notice='Profile saved')

    @app.post('/password')
    @app.post('/settings/user/password')
    def password_change(request: Request, current_password: str = Form(...), new_password: str = Form(...),
                        confirm_password: str = Form(...), csrf_token: str = Form(...), db: Session = Depends(get_db)):
        user = user_access(request, db); csrf(request, csrf_token)
        error = validate_new_password(new_password)
        if error:
            return profile_response(request, user, db, error=error)
        password_proof(request, user, current_password)
        if new_password != confirm_password:
            return profile_response(request, user, db, error='The new passwords do not match.')
        if password_hasher.verify(new_password, user.password_hash or ''):
            return profile_response(request, user, db, error='The new password must differ from the current password.')
        stamp = utcnow()
        changed = db.execute(update(User).where(User.id == user.id, User.password_hash == user.password_hash).values(
            password_hash=password_hasher.hash(new_password), password_changed_at=stamp,
            password_expires_at=stamp + timedelta(days=auth.settings.password_max_age_days), password_is_temporary=False))
        db.commit()
        if changed.rowcount != 1:
            raise HTTPException(409, 'Credentials changed; reload and sign in again')
        audit.emit('password.changed', request, user, outcome='success')
        sessions.revoke_user(user.id, request, reason='password_changed')
        request.session.clear(); request.scope['destroy_session'] = True
        return RedirectResponse('/login?reason=password-changed', status_code=303)

    def pending_user(request, db):
        uid = request.session.get('mfa_user')
        user = db.get(User, uid) if isinstance(uid, int) else None
        if (not user or not user.is_active or time.time() - request.session.get('password_verified_at', 0) >= 300
                or request.session.get('password_proof') != auth.password_fingerprint(user)):
            raise HTTPException(401, 'Sign in with your password again')
        mode = auth.guard_login(user.username, request)
        if mode == 'off':
            raise HTTPException(409, 'Security-key sign-in is disabled; sign in again')
        return user

    @app.post('/login/webauthn/options')
    def login_key_options(request: Request, body: dict = Body(...), db: Session = Depends(get_db)):
        csrf(request, body.get('csrf_token'))
        user = pending_user(request, db)
        try:
            options = webauthn.authentication_options(request, user, db, 'login')
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        audit.emit('mfa.authentication.started', request, user, login_method='password+webauthn')
        return options

    @app.post('/login/webauthn/verify')
    def login_key_verify(request: Request, body: dict = Body(...), db: Session = Depends(get_db)):
        csrf(request, body.get('csrf_token')); user = pending_user(request, db)
        try:
            webauthn.verify_authentication(request, user, db, 'login', body.get('challenge_id'), body.get('credential'))
        except Exception:
            auth.failed(user.username, request, 'password+webauthn', 'invalid_assertion')
            audit.emit('mfa.authentication.failed', request, user, outcome='failure', reason='invalid_assertion')
            raise HTTPException(403, 'Security-key verification failed. Try again.') from None
        audit.emit('mfa.authentication.succeeded', request, user, outcome='success', login_method='password+webauthn')
        target = safe_return_path(request.session.get('return_to', '/'))
        try:
            result = auth.complete(user, request, 'password+webauthn')
        except ValueError:
            raise HTTPException(401, 'Credentials changed; sign in again') from None
        return {'redirect': result if result != '/' else target}

    @app.post('/login/webauthn/cancel')
    def login_key_cancel(request: Request, body: dict = Body(...), db: Session = Depends(get_db)):
        csrf(request, body.get('csrf_token')); user = pending_user(request, db)
        audit.emit('mfa.authentication.cancelled', request, user, outcome='failure', reason='client_cancelled')
        return {'accepted': True}

    def operation(body):
        value = body.get('operation')
        if value == 'register':
            return value
        if value == 'remove' and type(body.get('key_id')) is int:
            return 'remove:' + str(body['key_id'])
        raise HTTPException(422, 'Invalid security-key operation')

    @app.post('/settings/user/security-keys/authorize/options')
    def authorize_options(request: Request, body: dict = Body(...), db: Session = Depends(get_db)):
        user = user_access(request, db); csrf(request, body.get('csrf_token'))
        password_proof(request, user, body.get('current_password'))
        purpose = 'manage-' + operation(body)
        try:
            options = webauthn.authentication_options(request, user, db, purpose)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        audit.emit('mfa.authentication.started', request, user, reason='credential_management')
        return options

    @app.post('/settings/user/security-keys/authorize/verify')
    def authorize_verify(request: Request, body: dict = Body(...), db: Session = Depends(get_db)):
        user = user_access(request, db); csrf(request, body.get('csrf_token')); target = operation(body)
        try:
            webauthn.verify_authentication(request, user, db, 'manage-' + target, body.get('challenge_id'), body.get('credential'))
        except Exception:
            auth.failed(user.username, request, 'webauthn-step-up', 'invalid_assertion')
            audit.emit('mfa.authentication.failed', request, user, outcome='failure', reason='credential_management')
            raise HTTPException(403, 'Security-key verification failed') from None
        grant = webauthn.challenge(request, user.id, 'grant-' + target, {}, lifetime=120)
        audit.emit('mfa.authentication.succeeded', request, user, outcome='success', reason='credential_management')
        return {'grant': grant}

    def require_grant(request, user, db, body, target):
        _, mode = auth.policy(user.username, request)
        if mode != 'off' and webauthn.keys(user.id, db):
            try:
                webauthn.consume(request, user.id, 'grant-' + target, body.get('grant'))
            except ValueError:
                raise HTTPException(403, 'Verify an existing security key before changing credentials') from None

    @app.post('/settings/user/security-keys/register/options')
    def register_options(request: Request, body: dict = Body(...), db: Session = Depends(get_db)):
        user = user_access(request, db); csrf(request, body.get('csrf_token'))
        if auth.policy(user.username, request)[1] == 'off':
            raise HTTPException(403, 'Security keys are disabled by the login policy')
        password_proof(request, user, body.get('current_password'))
        require_grant(request, user, db, body, 'register')
        try:
            options = webauthn.registration_options(request, user, db, body.get('name'))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        audit.emit('mfa.registration.started', request, user)
        return options

    @app.post('/settings/user/security-keys/register/verify')
    def register_verify(request: Request, body: dict = Body(...), db: Session = Depends(get_db)):
        user = user_access(request, db); csrf(request, body.get('csrf_token'))
        if auth.policy(user.username, request)[1] == 'off':
            raise HTTPException(403, 'Security keys are disabled')
        try:
            key = webauthn.verify_registration(request, user, db, body.get('challenge_id'), body.get('credential'))
        except Exception:
            audit.emit('mfa.registration.failed', request, user, outcome='failure', reason='invalid_attestation')
            raise HTTPException(403, 'Security-key registration failed. Try again.') from None
        audit.emit('mfa.registration.succeeded', request, user, outcome='success', credential=hashlib.sha256(key.credential_id.encode()).hexdigest()[:32])
        sessions.revoke_user(user.id, request, reason='security_key_registered')
        request.session.clear(); request.scope['destroy_session'] = True
        return {'redirect': '/login?reason=security-changed'}

    @app.post('/settings/user/security-keys/remove')
    def remove_key(request: Request, body: dict = Body(...), db: Session = Depends(get_db)):
        user = user_access(request, db); csrf(request, body.get('csrf_token'))
        if type(body.get('key_id')) is not int:
            raise HTTPException(422, 'Invalid security key')
        key = db.scalar(select(SecurityKey).where(SecurityKey.id == body['key_id'], SecurityKey.user_id == user.id))
        if not key:
            raise HTTPException(404, 'Security key not found')
        if auth.policy(user.username, request)[1] == 'required' and len(webauthn.keys(user.id, db)) <= 1:
            raise HTTPException(409, 'Register another security key before removing the last one')
        password_proof(request, user, body.get('current_password'))
        require_grant(request, user, db, body, 'remove:' + str(key.id))
        fingerprint = hashlib.sha256(key.credential_id.encode()).hexdigest()[:32]
        db.delete(key); db.commit()
        audit.emit('mfa.credential.removed', request, user, outcome='success', credential=fingerprint)
        sessions.revoke_user(user.id, request, reason='security_key_removed')
        request.session.clear(); request.scope['destroy_session'] = True
        return {'redirect': '/login?reason=security-changed'}

    @app.post('/settings/user/security-keys/cancel')
    def cancel_key(request: Request, body: dict = Body(...), db: Session = Depends(get_db)):
        user = user_access(request, db); csrf(request, body.get('csrf_token'))
        audit.emit('mfa.registration.failed', request, user, reason='client_cancelled', outcome='failure')
        return {'accepted': True}
