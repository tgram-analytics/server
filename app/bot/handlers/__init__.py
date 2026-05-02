"""Telegram bot command/callback handlers.

SECURITY RULE — HTML injection
==============================
Every string sourced from the database, /api/v1/track payloads,
/api/v1/pageview payloads, callback_data, or user-typed flow input MUST be
wrapped in ``html.escape(...)`` before being interpolated into a message
that is sent or edited with ``parse_mode="HTML"`` (this includes captions
on ``InputMediaPhoto`` and ``reply_photo`` when the surrounding call uses
HTML mode).

The ``proj_…`` API key is intentionally public (shipped in the JS SDK
bundle), so any visitor of any customer site can submit events with values
like ``event_name='<a href="…">phish</a>'``. Telegram HTML mode renders
``<a href>``, ``<b>``, ``<code>``, ``<tg-spoiler>`` and ``tg://`` URIs —
making the project owner's bot DM a phishing surface if escaping is
skipped. See ``app/api/ingestion.py`` for the canonical pattern.

This is enforced manually for now. Audit with::

    rg 'parse_mode="HTML"' app/bot/handlers/

and verify every interpolated string field is escaped.
"""
