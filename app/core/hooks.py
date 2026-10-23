"""Test-only synchronisation checkpoints.

Production code calls ``checkpoint(name, **ctx)`` at well-defined points inside a
transaction. With no hook registered it is a no-op. Tests register hooks to pause
a transaction at an exact point (deterministic coordination instead of sleeps) or
to inject a failure.
"""

from collections.abc import Callable

hooks: dict[str, Callable[..., None]] = {}


def checkpoint(name: str, **ctx) -> None:
    hook = hooks.get(name)
    if hook is not None:
        hook(**ctx)
