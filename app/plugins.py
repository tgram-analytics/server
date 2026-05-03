"""Plugin discovery for the OSS server.

At startup, the FastAPI lifespan calls :func:`load_plugins`, which:

1. Iterates Python entry points in the ``tgram_analytics.extensions``
   group and invokes each loaded callable with no arguments. This is the
   preferred mechanism: a downstream package declares the entry point in
   its own ``pyproject.toml`` and gets auto-discovered after
   ``pip install``.

2. Imports each module named in the ``TGA_EXTENSIONS`` env var
   (comma-separated) and calls ``module.register()`` if it exists. This
   is the fallback mechanism for ad-hoc setups, tests, and Docker
   deployments that pin extension modules at runtime.

Plugins register their hooks via :mod:`app.extensions`; this module is
purely the discovery mechanism. A plugin's ``register()`` is the right
place to call ``register_user_resolver``, ``register_project_pre_create``,
``register_bot_filter``, and/or to monkey-patch ``app.core.config.Settings``
with a subclass that adds extra env vars.

Failures during plugin load propagate to startup so misconfiguration is
loud, not silently degraded.
"""

from __future__ import annotations

import importlib
import logging
import os
from importlib.metadata import entry_points

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "tgram_analytics.extensions"
ENV_VAR = "TGA_EXTENSIONS"


def load_plugins() -> list[str]:
    """Discover and load all plugins.

    Returns the list of loaded plugin descriptors (``"entry_point:<name>"``
    or ``"env:<module>"``), in load order.

    Idempotent across calls only in the sense that entry points are
    stable; callers should invoke once per process at startup.
    """
    loaded: list[str] = []

    # 1. Entry points
    eps = entry_points(group=ENTRY_POINT_GROUP)
    for ep in eps:
        obj = ep.load()
        if callable(obj):
            obj()
        loaded.append(f"entry_point:{ep.name}")
        logger.info("loaded plugin via entry point: %s", ep.name)

    # 2. Env var
    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        for module_name in (m.strip() for m in env.split(",") if m.strip()):
            module = importlib.import_module(module_name)
            register = getattr(module, "register", None)
            if callable(register):
                register()
            loaded.append(f"env:{module_name}")
            logger.info("loaded plugin via env var: %s", module_name)

    return loaded
