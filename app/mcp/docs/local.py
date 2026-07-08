"""Cloud-specific docs read from the filesystem.

Placeholder for Phase 6+. The federation strategy is two-tier:

1. Public SDK READMEs — fetched from GitHub via :mod:`fetch`.
2. Cloud-only docs (billing, plan limits, dashboard tour) — read
   from a directory inside the cloud overlay package.

This module pre-declares the lookup surface so callers
(``get_integration_guide``, future tools) can ask "is there a
cloud-specific doc for *key*?" without branching on whether the
filesystem source actually exists yet. Returning ``None`` is the
explicit "not provided in this version" signal.

A future PR will populate this with real docs (e.g. a
``cloud_docs/billing.md`` shipped with the wheel) and wire the
loader; the function signature here is the contract that PR must
preserve.
"""

from __future__ import annotations


def get_local_doc(key: str) -> str | None:  # noqa: ARG001 — placeholder
    """Return cloud-specific docs body for *key*, or ``None`` if absent.

    Phase 6 v1 always returns ``None``. Callers should fall through
    to :mod:`fetch` (or render a "see [link]" message) when this
    returns ``None``.
    """
    return None
