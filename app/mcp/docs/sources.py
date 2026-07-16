"""Platform → raw GitHub README URL map for the docs federation.

Single source of truth: every place that needs to know "where does the
JS SDK README live?" reads this dict. Keep keys in sync with
:data:`app.mcp.tools.setup._SUPPORTED_PLATFORMS`.

Raw-content URLs are pinned to ``main`` rather than a tag — pinning to
a tag would mean the docs lag behind whatever the SDK published most
recently, which is the worst-of-both-worlds. The 5-minute TTL cache
in :mod:`fetch` already prevents hot-loop traffic; the ``ETag``-based
``commit_sha`` we record gives clients a way to detect drift.
"""

from __future__ import annotations

PLATFORM_SOURCES: dict[str, str] = {
    "js": "https://raw.githubusercontent.com/tgram-analytics/tgram-analytics-js/main/README.md",
    "python": "https://raw.githubusercontent.com/tgram-analytics/tgram-analytics-py/main/README.md",
    "flutter": (
        "https://raw.githubusercontent.com/tgram-analytics/tgram-analytics-flutter/main/README.md"
    ),
    "server": "https://raw.githubusercontent.com/tgram-analytics/server/main/README.md",
}
