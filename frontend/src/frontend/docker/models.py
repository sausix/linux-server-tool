"""Searchable resource models owned by the Docker component."""
from pydantic import BaseModel, Field

class ResourceEntry(BaseModel):
    id: str
    name: str
    state: str = ''
    driver: str = ''

class LabelEntry(BaseModel):
    key: str
    value: str
    stacks: int = 0
    containers: int = 0
    images: int = 0
    services: int = 0
    container_templates: int = 0
    networks: int = 0
    volumes: int = 0
    nodes: int = 0
    engines: int = 0

class VolumeEntry(BaseModel):
    name: str
    anonymous: bool = False
    driver: str = ''
    scope: str = ''
    usage: str = ''
    size: int | None = None
    container: list[str] = Field(default_factory=list)
    service: list[str] = Field(default_factory=list)
    stack: list[str] = Field(default_factory=list)

class ImageEntry(BaseModel):
    id: str
    name: str
    tags: list[str] = Field(default_factory=list)
    digests: list[str] = Field(default_factory=list)
    usage: str = ''
    size: int | None = None
    containers: int = 0
    services: int = 0
    active: int = 0

class ContainerEntry(BaseModel):
    id: str
    name: str
    image: str = ''
    state: str = ''
    health: str = ''
    stack: str = ''
    stack_kind: str = ''
    service: str = ''
    status: str = ''

class StackEntry(BaseModel):
    name: str
    kind: str
    base_kind: str = ''
    description: str = ''
    autostart: bool | None = None
    virtual: bool = False
    stopped: int = 0
    paused: int = 0
    restarting: int = 0
    state: str = ''
    containers: int = 0
    services: int = 0
    running: int = 0
    healthy: int = 0
    unhealthy: int = 0
    networks: int = 0
    volumes: int = 0
    desired_replicas: int | None = None
    running_replicas: int | None = None
    config_files: list[str] = Field(default_factory=list)

from frontend.inventory import register_models
register_models({'resource': ResourceEntry, 'label': LabelEntry, 'volume': VolumeEntry, 'image': ImageEntry, 'container': ContainerEntry, 'stack': StackEntry})
