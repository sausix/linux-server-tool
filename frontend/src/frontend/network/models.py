"""Searchable models owned by this component."""
from pydantic import BaseModel, Field

class InterfaceEntry(BaseModel):
    name: str
    scope: str = 'unknown'
    type: str = ''
    mac: str = ''
    ip: list[str] = Field(default_factory=list)
    ip4: list[str] = Field(default_factory=list)
    ip6: list[str] = Field(default_factory=list)
    subnet: list[str] = Field(default_factory=list)
    flag: list[str] = Field(default_factory=list)
    state: str = ''
    address_type: list[str] = Field(default_factory=list)


class RouteEntry(BaseModel):
    family: str = ''
    destination: str = ''
    subnet: str = ''
    gateway: str = ''
    table: str = ''
    interface: str = ''
    type: str = ''
    metric: str = ''

class FirewallEntry(BaseModel):
    backend: str = ''
    family: str = ''
    table: str = ''
    chain: str = ''
    rule: str = ''
    policy: str = ''
    expression: str = ''
    action: str = ''
    target: list[str] = Field(default_factory=list)
    interface: list[str] = Field(default_factory=list)
