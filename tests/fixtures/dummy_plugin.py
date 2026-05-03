"""Minimal plugin for testing :func:`app.plugins.load_plugins`.

Exercises only the env-var discovery path: defining a top-level
``register()`` callable that the loader will invoke.
"""

from __future__ import annotations

# Module-level flag flipped on each register() call so tests can assert
# the loader actually invoked the plugin.
register_call_count = 0


def register() -> None:
    """Plugin entry point. Bumps the module-level counter."""
    global register_call_count
    register_call_count += 1
