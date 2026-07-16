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

from app.mcp.docs.render import _env


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
