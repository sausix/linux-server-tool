"""Restrict incomplete accounts even on otherwise public HTML routes."""
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select, func
from frontend.models import User, SecurityKey


class ProfileGateMiddleware:
    def __init__(self, app, *, authentication):
        self.app, self.authentication = app, authentication

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or (scope.get('path', '').startswith('/static/') or scope.get('path') == '/assets/component-theme.css'):
            return await self.app(scope, receive, send)
        row = scope.get('session_record') or {}
        if row.get('user_id'):
            request = Request(scope)
            def restrictions():
                with self.authentication.factory() as db:
                    user = db.get(User, row['user_id'])
                    if not user:
                        return False, False
                    mode = self.authentication.policy(user.username, request)[1]
                    count = db.scalar(select(func.count()).select_from(SecurityKey).where(SecurityKey.user_id == user.id))
                    return user.password_change_required(), mode == 'required' and not count
            password, enrollment = await run_in_threadpool(restrictions)
            path = scope.get('path', '')
            allowed = {'/settings', '/settings/user', '/settings/user/profile', '/settings/user/password', '/password', '/logout', '/api/session'}
            if (password or enrollment) and path not in allowed and not (enrollment and not password and path.startswith('/settings/user/security-keys/')):
                response = (JSONResponse({'detail': 'Profile update required'}, status_code=403, headers={'X-Profile-Required': 'true'})
                            if path.startswith('/api/') else RedirectResponse('/settings/user', status_code=303))
                return await response(scope, receive, send)
        await self.app(scope, receive, send)
