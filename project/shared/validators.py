"""Shared validation helpers for module inputs."""
from __future__ import annotations


def require_non_empty(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} cannot be empty")
    return value
