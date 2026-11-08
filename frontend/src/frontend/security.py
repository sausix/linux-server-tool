from __future__ import annotations

import hmac
import secrets
from datetime import timedelta

from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from frontend.models import User, utcnow

password_hasher = PasswordHash.recommended()
_dummy_password_hash = password_hasher.hash("not-a-real-password")


def normalize_username(username: str) -> str:
    return username.strip().lower()


def verify_credentials(session: Session, username: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.username == normalize_username(username)))
    candidate_hash = user.password_hash if user and user.password_hash else _dummy_password_hash
    valid = password_hasher.verify(password, candidate_hash)
    if not user or not valid or not user.is_active:
        return None
    return user


def set_password(user: User, password: str, max_age_days: int) -> None:
    changed_at = utcnow()
    user.password_hash = password_hasher.hash(password)
    user.password_changed_at = changed_at
    user.password_expires_at = changed_at + timedelta(days=max_age_days)
    user.password_is_temporary = False


def validate_new_password(password: str) -> str | None:
    if len(password) < 12:
        return "The new password must contain at least 12 characters."
    if len(password) > 256:
        return "The new password is too long."
    return None


def ensure_admin(session: Session) -> str | None:
    admin = session.scalar(select(User).where(User.username == "admin"))
    if admin and admin.password_hash:
        admin.is_admin = True
        admin.is_active = True
        session.commit()
        return None
    initial_password = secrets.token_urlsafe(18)
    if admin is None:
        admin = User(username="admin", is_admin=True, is_active=True)
        session.add(admin)
    admin.password_hash = password_hasher.hash(initial_password)
    admin.password_changed_at = utcnow()
    admin.password_expires_at = utcnow()
    admin.password_is_temporary = True
    session.commit()
    return initial_password


def csrf_token(session_data: dict) -> str:
    token = session_data.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session_data["csrf_token"] = token
    return token


def csrf_is_valid(session_data: dict, submitted: str) -> bool:
    expected = session_data.get("csrf_token", "")
    return bool(isinstance(expected, str) and isinstance(submitted, str) and expected and submitted and expected.isascii() and submitted.isascii() and hmac.compare_digest(expected, submitted))
