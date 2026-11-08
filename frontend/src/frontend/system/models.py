"""Searchable system settings."""
from pydantic import BaseModel, Field
class OptionEntry(BaseModel):
    key: str = ''
    value: str = ''
    family: str = ''
    interface: str = ''
    source: list[str] = Field(default_factory=list)
    classes: list[str] = Field(default_factory=list)
    prefix: str = ''
    state: str = ''
