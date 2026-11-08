from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, String, Text, ForeignKey, UniqueConstraint, Float, Integer
from sqlalchemy.orm import Mapped, mapped_column

from frontend.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    password_is_temporary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    def password_change_required(self, now: datetime | None = None) -> bool:
        if self.password_is_temporary or self.password_hash is None:
            return True
        if self.password_expires_at is None:
            return False
        reference = now or utcnow()
        expiry = self.password_expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry <= reference


class ComposeDraft(Base):
    __tablename__ = 'compose_drafts'
    __table_args__ = (UniqueConstraint('user_id', 'draft_key'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), nullable=False)
    draft_key: Mapped[str] = mapped_column(String(64), nullable=False)
    stack_name: Mapped[str] = mapped_column(String(128), default='')
    content: Mapped[str] = mapped_column(Text, nullable=False)
    base_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[int] = mapped_column(default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AuthSession(Base):
    __tablename__ = 'auth_sessions'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), index=True)
    data: Mapped[str] = mapped_column(Text, default='{}')
    created_at: Mapped[float] = mapped_column(Float)
    last_seen: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float, index=True)
    absolute_expires_at: Mapped[float] = mapped_column(Float)
    revoked_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    password_epoch: Mapped[float] = mapped_column(Float, default=0)
    login_method: Mapped[str] = mapped_column(String(40), default='anonymous')
    source_ip: Mapped[str] = mapped_column(String(64), default='unknown')
    user_agent: Mapped[str] = mapped_column(String(512), default='')


class LoginBucket(Base):
    __tablename__ = 'login_buckets'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    reset_at: Mapped[float] = mapped_column(Float, index=True)
    blocked_until: Mapped[float] = mapped_column(Float, default=0)


class UserProfile(Base):
    __tablename__ = 'user_profiles'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128), default='')
    email: Mapped[str] = mapped_column(String(254), default='')
    webauthn_handle: Mapped[str] = mapped_column(String(128))


class SecurityKey(Base):
    __tablename__ = 'security_keys'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    credential_id: Mapped[str] = mapped_column(String(2048), unique=True)
    public_key: Mapped[str] = mapped_column(Text)
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(80))
    transports: Mapped[str] = mapped_column(Text, default='[]')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthChallenge(Base):
    __tablename__ = 'auth_challenges'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey('auth_sessions.id'), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    purpose: Mapped[str] = mapped_column(String(80))
    payload: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[float] = mapped_column(Float, index=True)
    consumed_at: Mapped[float | None] = mapped_column(Float)


class AuditOutbox(Base):
    __tablename__ = 'security_audit_outbox'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float, index=True)
