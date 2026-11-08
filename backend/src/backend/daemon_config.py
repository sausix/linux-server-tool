from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, model_validator, field_validator
from pydantic_settings import SettingsConfigDict
from common.configuration import ComponentSettings
from common.identity import APP_NAME, STORAGE_ID, LEGACY_FRONTEND_PREFIX, LEGACY_BACKEND_PREFIX


class DaemonSettings(ComponentSettings):
    prefix_selector = "BACKEND_ENV_PREFIX"
    legacy_prefix = LEGACY_BACKEND_PREFIX
    model_config = SettingsConfigDict(
        env_prefix="BACKEND_", env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True, hide_input_in_errors=True
    )

    docker_support: bool = Field(default=True, validation_alias="DOCKER_SUPPORT")
    network_support: bool = Field(default=True, validation_alias="NETWORK_SUPPORT")
    system_support: bool = Field(default=True, validation_alias="SYSTEM_SUPPORT")

    transport: Literal["unix", "tcp"] = "unix"
    unix_socket: Path = Path(f"/run/{STORAGE_ID}/backend.sock")
    socket_mode: str = "0660"
    listen_host: str = "127.0.0.1"
    listen_port: int = Field(default=9876, ge=1, le=65535)
    access_token: str | None = None
    max_clients: int = Field(default=64, ge=1, le=1024)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    docker_access: Literal["disabled", "readonly", "readwrite"] = "readonly"
    network_access: Literal["disabled", "readonly", "readwrite"] = "readonly"
    system_access: Literal['disabled', 'readonly', 'readwrite'] = 'readonly'
    system_host_root: Path | None = None
    interfaces_access: Literal['disabled', 'ro', 'rw'] = 'rw'
    routing_access: Literal['disabled', 'ro', 'rw'] = 'rw'
    firewall_access: Literal['disabled', 'ro', 'rw'] = 'ro'
    firewall_backend: Literal['auto', 'nftables', 'iptables', 'iptables-legacy', 'iptables-nft'] = 'auto'
    sysctl_access: Literal['disabled', 'ro', 'rw'] = 'ro'
    docker_endpoint: str = "unix:///var/run/docker.sock"
    docker_timeout_seconds: float = Field(default=5.0, gt=0, le=120)
    docker_storage_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    docker_log_max_lines: int = Field(default=5000, ge=1, le=20000)
    docker_log_default_lines: int = Field(default=200, ge=1, le=20000)
    docker_log_max_streams: int = Field(default=32, ge=1, le=128)
    docker_log_discovery_interval_seconds: float = Field(default=2.0, ge=.05, le=60)
    docker_api_version: str = "auto"
    docker_tls_verify: bool = True
    docker_tls_ca: Path | None = None
    docker_tls_cert: Path | None = None
    docker_tls_key: Path | None = None
    stack_database: Path = Path(f'/var/lib/{STORAGE_ID}/backend/stacks.sqlite3')
    compose_host_root: Path = Path('/')
    compose_host_read_only: bool = True
    compose_root: Path = Path(f'/var/lib/{STORAGE_ID}/compose')
    compose_allowed_roots: list[Path] = [Path('/srv'), Path('/opt'), Path('/home'), Path('/media'), Path('/app')]
    compose_search_directories: list[Path] = []
    compose_command: list[str] = ['docker', 'compose']
    stack_control_timeout_seconds: float = Field(default=120, gt=0, le=1800)
    stack_max_concurrent_jobs: int = Field(default=2, ge=1, le=32)
    stack_stop_timeout_seconds: int = Field(default=10, ge=1, le=300)
    networkd_directory: Path = Path("/etc/systemd/network")
    interfaces_file: Path = Path("/etc/network/interfaces")
    host_etc_directory: Path = Path('/etc')
    command_timeout_seconds: float = Field(default=15.0, gt=0, le=300)
    events_enabled: bool = True

    @field_validator('docker_access', 'network_access', 'system_access', mode='before')
    @classmethod
    def access_aliases(cls, value):
        return {'ro': 'readonly', 'rw': 'readwrite'}.get(value, value)

    def feature_access(self, feature):
        category = self.system_access if feature == 'sysctl' else self.network_access
        if category == 'disabled':
            return 'disabled'
        value = getattr(self, feature + '_access')
        if value == 'disabled':
            return value
        return 'ro' if category == 'readonly' or value == 'ro' else 'rw'

    @property
    def features(self):
        return {feature: self.feature_access(feature) for feature in
                ('interfaces', 'routing', 'firewall', 'sysctl')}

    @property
    def compose_source_root(self):
        """Reuse the host filesystem mapping unless Compose has a dedicated prefix."""
        if self.compose_host_root != Path('/'):
            return self.compose_host_root
        return self.system_host_root or self.compose_host_root

    @property
    def sysctl_sources(self):
        from backend.system.sysctl import SYSCTL_DIRECTORIES
        if self.system_host_root is not None:
            return {'directories': [self.system_host_root / path.relative_to('/') for path in SYSCTL_DIRECTORIES],
                    'legacy': self.system_host_root / 'etc/sysctl.conf'}
        # Container mounts must not mix the image's distro defaults into host state.
        if self.host_etc_directory != Path('/etc'):
            return {'directories': [self.host_etc_directory / 'sysctl.d'],
                    'legacy': self.host_etc_directory / 'sysctl.conf'}
        return {}

    @model_validator(mode="after")
    def validate_transport(self) -> "DaemonSettings":
        for component in ("docker", "network", "system"):
            if not getattr(self, component + "_support"):
                setattr(self, component + "_access", "disabled")
        if self.docker_log_default_lines > self.docker_log_max_lines:
            raise ValueError('docker_log_default_lines must not exceed docker_log_max_lines')
        if self.system_host_root is not None and not self.system_host_root.is_absolute():
            raise ValueError('system_host_root must be an absolute path')
        if not self.stack_database.is_absolute() or not self.compose_host_root.is_absolute() or not self.compose_root.is_absolute():
            raise ValueError('stack_database, compose_root and compose_host_root must be absolute paths')
        if not self.compose_allowed_roots or any(not path.is_absolute() for path in self.compose_allowed_roots + self.compose_search_directories):
            raise ValueError('Compose roots and search directories must be absolute paths, with at least one allowed root')
        if self.compose_root == Path('/'):
            raise ValueError('compose_root must be a dedicated directory, not the filesystem root')
        if not self.compose_command or any(not isinstance(item, str) or not item or '\x00' in item for item in self.compose_command):
            raise ValueError('compose_command must be a nonempty argument list')
        try:
            mode = int(self.socket_mode, 8)
        except ValueError as exc:
            raise ValueError("socket_mode must be an octal value such as 0660") from exc
        if mode < 0 or mode > 0o777:
            raise ValueError("socket_mode must be between 0000 and 0777")
        if self.transport == "tcp" and (
            not self.access_token or len(self.access_token) < 32
        ):
            raise ValueError("ACCESS_TOKEN with at least 32 characters is required for TCP")
        if self.docker_support:
            docker_endpoint = urlparse(self.docker_endpoint)
            if docker_endpoint.scheme not in {"unix", "tcp", "http", "https"}:
                raise ValueError("docker_endpoint must use unix://, tcp://, http://, or https://")
            if docker_endpoint.scheme == "unix" and not docker_endpoint.path:
                raise ValueError("Unix Docker endpoint must include a socket path")
            if docker_endpoint.scheme != "unix" and not docker_endpoint.hostname:
                raise ValueError("TCP Docker endpoint must include a hostname")
            if bool(self.docker_tls_cert) != bool(self.docker_tls_key):
                raise ValueError("docker_tls_cert and docker_tls_key must be configured together")
            if self.docker_api_version != "auto" and not self.docker_api_version.startswith("v"):
                raise ValueError("docker_api_version must be auto or look like v1.43")
        return self

    @property
    def socket_mode_int(self) -> int:
        return int(self.socket_mode, 8)

