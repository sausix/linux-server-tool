from common.identity import APP_NAME, STORAGE_ID, SESSION_COOKIE, SESSION_SALT, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, TEMP_PREFIX, MANAGED_HEADER
"""Opaque signed cookies backed by revocable SQLite sessions."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import re
import secrets
import threading
import time
from datetime import timezone
from itsdangerous import TimestampSigner, BadSignature
from sqlalchemy import select, update, delete
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.responses import Response
from frontend.models import AuthSession, User


def now():
    return time.time()


def password_epoch(user):
    value = user.password_changed_at
    return value.replace(tzinfo=timezone.utc).timestamp() if value and value.tzinfo is None else value.timestamp() if value else 0


def snapshot(row):
    return {key: getattr(row, key) for key in ('id', 'public_id', 'user_id', 'created_at', 'expires_at',
                                              'absolute_expires_at', 'login_method', 'source_ip', 'user_agent')}


class SessionStore:
    def __init__(self, factory, settings, secret, audit):
        self.factory, self.settings, self.audit = factory, settings, audit
        self.signer = TimestampSigner(secret, salt=SESSION_SALT)
        self.lock = threading.Lock()
        self.watchers = {}

    def create(self, user_id=None, data=None, source_ip='unknown', user_agent='', login_method='anonymous', expected_password_hash=None):
        raw, stamp = secrets.token_urlsafe(32), now()
        with self.factory() as db:
            user = db.get(User, user_id) if user_id else None
            if user_id and (not user or not user.is_active):
                raise ValueError('Account is unavailable')
            if user and expected_password_hash is not None and user.password_hash != expected_password_hash:
                raise ValueError('Credentials changed; sign in again')
            row = AuthSession(id=hashlib.sha256(raw.encode()).hexdigest(), public_id=secrets.token_hex(16), user_id=user_id,
                data=json.dumps(data or {}), created_at=stamp, last_seen=stamp,
                expires_at=stamp + self.settings.session_max_age_seconds,
                absolute_expires_at=stamp + self.settings.session_absolute_max_age_seconds,
                password_epoch=password_epoch(user) if user else 0, login_method=login_method,
                source_ip=source_ip, user_agent=user_agent[:512])
            db.add(row); db.commit()
            return self.signer.sign(raw.encode()).decode(), json.loads(row.data), snapshot(row)

    def load(self, cookie):
        try:
            raw = self.signer.unsign(cookie, max_age=self.settings.session_absolute_max_age_seconds).decode()
            if not re.fullmatch(r'[A-Za-z0-9_-]{43}', raw):
                return None
        except (BadSignature, ValueError, UnicodeError):
            return None
        identity = hashlib.sha256(raw.encode()).hexdigest()
        with self.factory() as db:
            row = db.get(AuthSession, identity)
            if not row or row.revoked_at is not None:
                return None
            if min(row.expires_at, row.absolute_expires_at) <= now():
                expired = True
            else:
                user = db.get(User, row.user_id) if row.user_id else None
                if row.user_id and (not user or not user.is_active or row.password_epoch != password_epoch(user)):
                    expired = True
                else:
                    return json.loads(row.data), snapshot(row)
        if expired:
            self.revoke(identity, reason='expired', event='session.expired')
        return None

    def valid(self, identity, user_id):
        with self.factory() as db:
            row, user = db.get(AuthSession, identity), db.get(User, user_id)
            return bool(row and user and row.user_id == user_id and user.is_active and row.revoked_at is None
                        and min(row.expires_at, row.absolute_expires_at) > now() and row.password_epoch == password_epoch(user))

    def persist(self, identity, data, renew=False):
        stamp = now()
        with self.factory() as db:
            values = {'data': json.dumps(data)}
            if renew:
                row = db.get(AuthSession, identity)
                if not row:
                    return False
                values.update(last_seen=stamp, expires_at=min(stamp + self.settings.session_max_age_seconds, row.absolute_expires_at))
            result = db.execute(update(AuthSession).where(AuthSession.id == identity, AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > stamp, AuthSession.absolute_expires_at > stamp).values(**values))
            db.commit()
            return result.rowcount == 1

    def notify(self, identities):
        with self.lock:
            pairs = [pair for identity in identities for pair in self.watchers.get(identity, set())]
        for loop, event in pairs:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass

    def revoke(self, identity, *, reason='logout', request=None, event='session.revoked'):
        self.revoke_many([identity], reason=reason, request=request, event=event)

    def revoke_user(self, user_id, request=None, reason='password_changed'):
        with self.factory() as db:
            identities = list(db.scalars(select(AuthSession.id).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))))
        self.revoke_many(identities, reason=reason, request=request)

    def revoke_many(self, identities, *, reason, request=None, event='session.revoked'):
        with self.factory() as db:
            rows = db.scalars(select(AuthSession).where(AuthSession.id.in_(identities), AuthSession.revoked_at.is_(None))).all()
            if not rows:
                return
            records = [snapshot(row) for row in rows]
            db.execute(update(AuthSession).where(AuthSession.id.in_([row['id'] for row in records]), AuthSession.revoked_at.is_(None)).values(revoked_at=now()))
            db.commit()
        self.notify([row['id'] for row in records])
        for row in records:
            if row['user_id']:
                with self.factory() as db:
                    user = db.get(User, row['user_id'])
                    fields = {'source_ip': row['source_ip'], 'user_agent': row['user_agent']} if request is None else {}
                    self.audit.emit(event, request, user, session_id=row['public_id'], login_method=row['login_method'], reason=reason, **fields)

    @asynccontextmanager
    async def monitor(self, identity, user_id):
        event, loop = asyncio.Event(), asyncio.get_running_loop()
        with self.lock:
            self.watchers.setdefault(identity, set()).add((loop, event))
        async def poll():
            while not event.is_set():
                if not await run_in_threadpool(self.valid, identity, user_id):
                    event.set(); return
                await asyncio.sleep(.5)
        task = asyncio.create_task(poll())
        try:
            yield event
        finally:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)
            with self.lock:
                self.watchers.get(identity, set()).discard((loop, event))
                if not self.watchers.get(identity):
                    self.watchers.pop(identity, None)


class ServerSessionMiddleware:
    def __init__(self, app, *, store):
        self.app, self.store = app, store

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        from starlette.requests import Request
        cookie = Request(scope).cookies.get(SESSION_COOKIE, '')
        loaded = await run_in_threadpool(self.store.load, cookie) if cookie else None
        data, record = loaded if loaded else ({}, None)
        original = json.dumps(data, sort_keys=True)
        scope['session'], scope['session_record'] = data, record
        async def send_session(message):
            if message['type'] == 'http.response.start':
                current = scope['session']
                token = scope.get('new_session_cookie')
                row = scope.get('session_record')
                destroy = scope.get('destroy_session') or bool(row and not current)
                response = Response()
                if destroy:
                    if row:
                        await run_in_threadpool(self.store.revoke, row['id'], reason='session_cleared')
                    response.delete_cookie(SESSION_COOKIE, secure=self.store.settings.secure_cookie, httponly=True, samesite='lax')
                elif current and (token or json.dumps(current, sort_keys=True) != original or scope.get('authenticated_user')):
                    if row:
                        saved = await run_in_threadpool(self.store.persist, row['id'], current, bool(scope.get('authenticated_user')))
                        if saved:
                            token = token or self.store.signer.sign(self.store.signer.unsign(cookie)).decode()
                        else:
                            token = None
                            response.delete_cookie(SESSION_COOKIE, secure=self.store.settings.secure_cookie, httponly=True, samesite='lax')
                    else:
                        token, _, row = await run_in_threadpool(self.store.create, None, current)
                    if token:
                        max_age = self.store.settings.session_max_age_seconds if self.store.settings.session_persistent else None
                        response.set_cookie(SESSION_COOKIE, token, max_age=max_age, secure=self.store.settings.secure_cookie,
                                            httponly=True, samesite='lax', path='/')
                headers = MutableHeaders(scope=message)
                for key, value in response.raw_headers:
                    if key == b'set-cookie':
                        headers.append('set-cookie', value.decode('latin-1'))
                if current or loaded:
                    headers.add_vary_header('Cookie')
            await send(message)
        await self.app(scope, receive, send_session)
