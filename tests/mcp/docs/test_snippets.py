"""Tests for the Jinja snippet templates rendered by ``mcp.docs.render``.

We test the rendered string directly via the Jinja environment to
avoid coupling these snippet-shape assertions to the ownership-check
plumbing — the tool-level test (``tests/mcp/test_setup_tools.py``)
covers ownership.

Asserted invariants:

- Each platform produces output with the right init pattern.
- Variables (``api_key``, ``server_url``, ``package_name``) are
  substituted into the snippet.
- Autoescape is on: passing an HTML-active ``api_key`` (``<script>``)
  doesn't crash the renderer; the output is consumable as a code
  snippet either way.
"""

from __future__ import annotations

import pytest

from app.mcp.docs.render import _env, _resolve_server_url


@pytest.mark.parametrize(
    ("platform", "expected_substring"),
    [
        ("js", "TGA.init"),
        ("python", "TGA("),
        ("flutter", "package:tgram_analytics"),
    ],
)
def test_snippet_renders_with_expected_init_pattern(platform: str, expected_substring: str):
    template = _env.get_template(f"{platform}.jinja2")
    rendered = template.render(
        api_key="tga_test_abc",
        server_url="https://example.test",
        package_name="tgram-analytics",
    )
    assert expected_substring in rendered
    assert "tga_test_abc" in rendered or "tga_test_abc" in rendered.replace("&#34;", '"').replace(
        "&#39;", "'"
    )
    assert "https://example.test" in rendered or (
        "https://example.test" in rendered.replace("&#x2F;", "/")
    )


def test_js_snippet_matches_sdk_init_signature():
    template = _env.get_template("js.jinja2")
    rendered = template.render(
        api_key="k",
        server_url="https://api.example",
        package_name="tgram-analytics",
    )
    # The JS SDK exposes a *default* export — a named import would be
    # undefined at runtime.
    assert "import TGA from 'tgram-analytics'" in rendered
    assert "import { TGA }" not in rendered
    # init(apiKey, options): key is the first positional argument; only
    # serverUrl lives in the options object (camelCase, not snake_case).
    assert "TGA.init('k', {" in rendered
    assert "serverUrl" in rendered
    assert "apiKey" not in rendered


def test_python_snippet_uses_snake_case_kwargs():
    template = _env.get_template("python.jinja2")
    rendered = template.render(
        api_key="k",
        server_url="https://api.example",
        package_name="tgram-analytics",
    )
    assert "api_key=" in rendered
    assert "server_url=" in rendered


def test_flutter_snippet_matches_sdk_init_signature():
    template = _env.get_template("flutter.jinja2")
    rendered = template.render(
        api_key="k",
        server_url="https://api.example",
        package_name="tgram_analytics",
    )
    # The Dart SDK is a singleton: static TGA.init(apiKey, serverUrl) with
    # both arguments positional. There is no public constructor.
    assert "TGA.init('k', 'https://api.example')" in rendered
    assert "TGA(apiKey:" not in rendered
    # track(eventName, sessionId, {properties}) — sessionId is required.
    assert "TGA.track('signup', 'session-123'" in rendered
    # Version pin must track the released SDK (pubspec.yaml: 0.2.0).
    assert "^0.2.0" in rendered
    assert "^0.1.0" not in rendered


def test_active_input_is_rendered_verbatim():
    """Angle-bracket input must not raise or be dropped.

    Autoescape is off (snippets are code, not HTML), so the value is
    spliced in verbatim. The point: rendering must not raise, and the
    input must survive in the output rather than being silently dropped.
    """
    template = _env.get_template("js.jinja2")
    rendered = template.render(
        api_key="<script>alert(1)</script>",
        server_url="https://api.example",
        package_name="tgram-analytics",
    )
    assert "TGA.init" in rendered
    assert "alert" in rendered  # input survived


def test_placeholder_key_is_not_html_escaped():
    """The default ``<YOUR_API_KEY>`` placeholder renders with literal angle
    brackets so the snippet is copy-paste-ready (regression: autoescape
    used to emit ``&lt;YOUR_API_KEY&gt;``)."""
    template = _env.get_template("js.jinja2")
    rendered = template.render(
        api_key="<YOUR_API_KEY>",
        server_url="https://api.example",
        package_name="tgram-analytics",
    )
    assert "<YOUR_API_KEY>" in rendered
    assert "&lt;" not in rendered


# ── server_url resolution ─────────────────────────────────────────────
# Regression: the snippet used to hardcode https://api.tgram-analytics.com
# regardless of which instance rendered it. Projects and API keys are
# per-instance, so a key minted by a self-hosted MCP was rejected with
# 400 "Invalid API key" by the canonical host — a silently broken install.


def _patch_settings(monkeypatch, **attrs):
    from types import SimpleNamespace

    monkeypatch.setattr("app.core.config.get_settings", lambda: SimpleNamespace(**attrs))


def test_server_url_uses_instance_public_url(monkeypatch):
    """The snippet points at *this* instance, not a hardcoded host."""
    _patch_settings(monkeypatch, mcp_effective_public_url="https://tga.example.com")

    assert _resolve_server_url() == "https://tga.example.com"


def test_server_url_strips_trailing_slash(monkeypatch):
    _patch_settings(monkeypatch, mcp_effective_public_url="https://tga.example.com/")

    assert _resolve_server_url() == "https://tga.example.com"


def test_server_url_falls_back_when_settings_unavailable(monkeypatch):
    """No settings (CI without env vars) ⇒ canonical host, not a crash."""

    def _boom():
        raise RuntimeError("no env")

    monkeypatch.setattr("app.core.config.get_settings", _boom)

    assert _resolve_server_url() == "https://api.tgram-analytics.com"


def test_server_url_falls_back_when_public_url_empty(monkeypatch):
    _patch_settings(monkeypatch, mcp_effective_public_url="")

    assert _resolve_server_url() == "https://api.tgram-analytics.com"


@pytest.mark.parametrize("platform", ["js", "python", "flutter"])
def test_every_platform_snippet_uses_configured_base_url(platform: str, monkeypatch):
    """Every template — not just js — must carry the instance's own URL."""
    _patch_settings(monkeypatch, mcp_effective_public_url="https://tga.example.com")

    from app.mcp.docs.render import _PACKAGE_NAMES, _TEMPLATES

    rendered = _env.get_template(_TEMPLATES[platform]).render(
        api_key="<YOUR_API_KEY>",
        server_url=_resolve_server_url(),
        package_name=_PACKAGE_NAMES[platform],
    )
    assert "https://tga.example.com" in rendered
    assert "api.tgram-analytics.com" not in rendered
