"""Static guard against HTML-injection regressions in bot handlers.

Background: the public ``proj_…`` API key is shipped in the JS SDK bundle,
so any internet user can submit events with attacker-chosen ``event_name``
or property values via ``POST /api/v1/track``. Telegram's ``parse_mode=
"HTML"`` renders ``<a href>``, ``<b>``, ``<code>``, ``<tg-spoiler>`` and
``tg://`` URIs, so any DB-sourced string interpolated into an HTML-mode
message without ``html.escape(...)`` becomes a phishing channel inside
the project owner's bot DM. See ``server/app/bot/handlers/__init__.py``.

This test scans the bot-handler source files for the pattern
``f"…<b>{ATTACKER_FIELD}</b>…"`` and fails if the field is not wrapped in
``html.escape``. It is intentionally narrow: keep the allowlist of
attacker-controlled field names below up to date when adding new sinks.
"""

from __future__ import annotations

import re
from pathlib import Path

# Field names known to be attacker-influenced (sourced from DB columns
# populated via /api/v1/track, /api/v1/pageview, callback data, or
# multi-step flow text input). When any of these is interpolated into a
# Python f-string within an HTML-rendered tag, it must be wrapped in
# html.escape(...). Add new fields here when you introduce them.
ATTACKER_CONTROLLED_FIELDS: tuple[str, ...] = (
    "event_name",
    "event_name_val",
    "property_key",
    "chart_event",
    "row.event_name",
    "alert.event_name",
    "r['value']",
    'r["value"]',
    "row['step']",
    'row["step"]',
)

HANDLERS_DIR = Path(__file__).resolve().parent.parent / "app" / "bot" / "handlers"


def _bad_pattern(field: str) -> re.Pattern[str]:
    """Match ``<tag>{field}</tag>`` or ``<tag>{field…}</tag>`` not wrapped in html.escape."""
    escaped = re.escape(field)
    return re.compile(
        r"<(?:b|i|code|pre|tg-spoiler|u|s)>\{" + escaped + r"[^}]*\}</",
    )


def test_no_unescaped_attacker_fields_in_html_messages() -> None:
    offenders: list[str] = []
    for path in HANDLERS_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), start=1):
            for field in ATTACKER_CONTROLLED_FIELDS:
                if _bad_pattern(field).search(line) and "html.escape" not in line:
                    offenders.append(f"{path.name}:{i}: {line.strip()}")

    assert not offenders, (
        "Bot handler interpolates attacker-controlled string into HTML-rendered "
        "tag without html.escape(...). See server/app/bot/handlers/__init__.py "
        "for the rule.\n\n" + "\n".join(offenders)
    )
