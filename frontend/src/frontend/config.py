from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
import ipaddress

from pydantic import field_validator, Field, model_validator, BaseModel, ConfigDict
from pydantic_settings import SettingsConfigDict
from common.configuration import ComponentSettings
from common.identity import APP_NAME, STORAGE_ID, LEGACY_FRONTEND_PREFIX, LEGACY_BACKEND_PREFIX

logger = logging.getLogger("frontend.config")


class LoginRule(BaseModel):
    model_config = ConfigDict(extra='forbid')
    users: list[str] = ['*']
    networks: list[str] = []
    action: Literal['allow', 'deny'] = 'allow'
    webauthn: Literal['off', 'optional', 'required'] | None = None

    @model_validator(mode='after')
    def valid_networks(self):
        for value in self.networks:
            ipaddress.ip_network(value, strict=False)
        if not self.users or any(not value or len(value) > 64 for value in self.users):
            raise ValueError('Login rules need usernames or *')
        return self


class Settings(ComponentSettings):
    prefix_selector = "FRONTEND_ENV_PREFIX"
    legacy_prefix = LEGACY_FRONTEND_PREFIX
    """Runtime configuration. Explicit constructor/CLI values override environment."""

    model_config = SettingsConfigDict(
        env_prefix="FRONTEND_", env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True, hide_input_in_errors=True
    )

    docker_support: bool = Field(default=True, validation_alias="DOCKER_SUPPORT")
    network_support: bool = Field(default=True, validation_alias="NETWORK_SUPPORT")
    system_support: bool = Field(default=True, validation_alias="SYSTEM_SUPPORT")

    app_name: str = APP_NAME
    environment: Literal["development", "production", "test"] = "development"
    bind_host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    public_url: str = "http://localhost:8000"
    database_url: str = "sqlite:///./data/frontend.db"
    secret_key: str | None = None
    session_max_age_seconds: int = Field(default=3600, ge=60)
    session_persistent: bool = True
    session_absolute_max_age_seconds: int = Field(default=43200, ge=60, le=2592000)
    login_allowed_networks: list[str] = []
    login_rules: list[LoginRule] = []
    webauthn_mode: Literal['off', 'optional', 'required'] = 'optional'
    login_failure_limit: int = Field(default=5, ge=1, le=100)
    login_ip_failure_limit: int = Field(default=20, ge=1, le=1000)
    login_failure_window_seconds: int = Field(default=900, ge=10, le=86400)
    login_block_seconds: int = Field(default=300, ge=1, le=86400)
    login_max_concurrency: int = Field(default=4, ge=1, le=32)
    login_ipv6_prefix: int = Field(default=64, ge=0, le=128)
    request_max_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    post_max_bytes: int = Field(default=4 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    login_max_bytes: int = Field(default=16 * 1024, ge=1024, le=64 * 1024)
    upload_max_bytes: int = Field(default=4 * 1024 * 1024, ge=1024, le=4 * 1024 * 1024)
    upload_max_files: int = Field(default=64, ge=1, le=64)
    password_max_age_days: int = Field(default=90, ge=1)
    http_policy: Literal["allow", "redirect", "https-only"] = "allow"
    cookie_secure: Literal["auto", "always", "never"] = "auto"
    trusted_hosts: str = "localhost,127.0.0.1"
    forwarded_allow_ips: str = "127.0.0.1"
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None
    daemon_endpoint: str = f"unix:///run/{STORAGE_ID}/backend.sock"
    daemon_token: str | None = None
    daemon_timeout_seconds: float = Field(default=1.0, gt=0, le=30)
    daemon_inventory_timeout_seconds: float = Field(default=15.0, gt=0, le=300)
    daemon_storage_timeout_seconds: float = Field(default=45.0, gt=0, le=660)
    daemon_control_timeout_seconds: float = Field(default=150.0, gt=0, le=1900)
    shutdown_timeout_seconds: int = Field(default=10, ge=1, le=300)

    @field_validator('app_name', mode='before')
    @classmethod
    def default_display_name(cls, value):
        return APP_NAME if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def validate_runtime(self) -> "Settings":
        for value in self.login_allowed_networks:
            ipaddress.ip_network(value, strict=False)
        if self.post_max_bytes > self.request_max_bytes or self.upload_max_bytes + 65536 > self.request_max_bytes or self.login_max_bytes > self.post_max_bytes:
            raise ValueError('Request limit must cover POST and upload budgets; login limit must not exceed POST limit')
        parsed = urlparse(self.public_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError("public_url must be an absolute HTTP(S) URL")
        if bool(self.ssl_certfile) != bool(self.ssl_keyfile):
            raise ValueError("ssl_certfile and ssl_keyfile must be configured together")
        if self.environment == "production" and not self.secret_key:
            raise ValueError("SECRET_KEY is required in production")
        daemon_endpoint = urlparse(self.daemon_endpoint)
        if daemon_endpoint.scheme not in {"unix", "tcp"}:
            raise ValueError("daemon_endpoint must use unix:///path or tcp://host:port")
        if daemon_endpoint.scheme == "tcp" and (
            not daemon_endpoint.hostname or daemon_endpoint.port is None
        ):
            raise ValueError("TCP daemon_endpoint must include a host and port")
        return self

    def effective_secret_key(self) -> str:
        if self.secret_key:
            if len(self.secret_key) < 32:
                raise ValueError("SECRET_KEY must contain at least 32 characters")
            return self.secret_key
        logger.warning(
            "SECRET_KEY is unset; using an ephemeral development key. "
            "Sessions will not survive a restart."
        )
        return secrets.token_urlsafe(48)

    @property
    def secure_cookie(self) -> bool:
        if self.cookie_secure == "always":
            return True
        if self.cookie_secure == "never":
            return False
        return urlparse(self.public_url).scheme == "https"

    @property
    def trusted_host_list(self) -> list[str]:
        hosts = [value.strip() for value in self.trusted_hosts.split(",") if value.strip()]
        public_host = urlparse(self.public_url).hostname
        if public_host and public_host not in hosts:
            hosts.append(public_host)
        return hosts or ["localhost"]

    def ensure_sqlite_directory(self) -> None:
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            return
        raw_path = self.database_url.removeprefix(prefix).split("?", 1)[0]
        if raw_path == ":memory:":
            return
        Path(raw_path).expanduser().parent.mkdir(parents=True, exist_ok=True)

