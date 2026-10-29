"""Read-only collection from the homelab and public release registries.

No Docker write endpoints or container logs are used. Environment variable values are
read only for the harmless allowlist in ripe.autoprofile; secrets are never read or stored.
"""
from .collector import collect

__all__ = ["collect"]
