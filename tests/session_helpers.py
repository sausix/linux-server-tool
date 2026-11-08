"""Create real server sessions for ordinary authenticated route fixtures."""
import base64
import json
import secrets
from frontend.models import User


def session_cookie(app, legacy_fixture_payload):
    user_id = json.loads(base64.b64decode(legacy_fixture_payload))['user_id']
    with app.state.session_factory() as db:
        user = db.get(User, user_id)
        # General route fixtures represent users who have completed initial setup.
        user.password_is_temporary = False
        user.password_expires_at = None
        db.commit()
    cookie, _, _ = app.state.sessions.create(user_id, {'user_id': user_id, 'csrf_token': secrets.token_urlsafe(32)},
                                             source_ip='127.0.0.1', user_agent='test fixture', login_method='password')
    return cookie
