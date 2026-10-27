"""Read-only collection from the homelab and public release registries.

No Docker write endpoints, container logs, environment variables, volumes or
configuration files are read. Local profile keys are supplied separately by users.
"""
from .collector import collect

__all__ = ["collect"]
