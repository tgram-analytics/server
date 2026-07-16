"""Authorize-page HTML rendering (client identity + hardened token field)."""

from app.mcp.oauth.pages import render_authorize_page


def _page(**overrides) -> str:
    params = dict(
        client_name="Claude",
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        client_id="cid",
        state="st",
        code_challenge="ch",
        csrf_token="csrf123",
        error=None,
    )
    params.update(overrides)
    return render_authorize_page(**params)


def test_shows_client_identity():
    html = _page()
    assert "Claude" in html
    assert "claude.ai" in html  # redirect HOST shown, so admin sees who receives the code


def test_token_field_hardened():
    html = _page()
    assert 'type="password"' in html
    assert 'autocomplete="off"' in html
    assert 'name="csrf_token"' in html and "csrf123" in html


def test_redirect_host_ignores_userinfo_spoof():
    html = _page(redirect_uri="https://claude.ai@evil.example/cb")
    assert "evil.example" in html
    assert "claude.ai@" not in html


def test_html_escapes_client_name():
    html = _page(client_name="<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_error_shown_generically():
    html = _page(error="invalid_token")
    assert "check the token" in html.lower()
