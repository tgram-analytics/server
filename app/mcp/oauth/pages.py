"""HTML for the paste-token authorize page.

Plain stdlib rendering (html.escape + f-string) — no Jinja: one page,
and keeping it dependency-light makes the escaping obvious to audit.
The page MUST show who is being authorized (client name + redirect
host): the confused-deputy defense from the spec — an attacker can DCR
their own client and link the admin to this (genuine) page, so the
admin needs to see whose callback will receive the code.
"""

from __future__ import annotations

from html import escape
from urllib.parse import urlparse

_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:26rem;margin:12vh auto;padding:0 1rem}"
    "input[type=password]{width:100%;padding:.5rem;font-family:monospace}"
    "button{margin-top:1rem;padding:.5rem 1.5rem}"
    ".who{background:#f2f2f7;border-radius:8px;padding:.8rem 1rem}"
    ".err{color:#b00020}"
)


def render_authorize_page(
    *,
    client_name: str,
    redirect_uri: str,
    client_id: str,
    state: str,
    code_challenge: str,
    csrf_token: str,
    error: str | None = None,
) -> str:
    host = urlparse(redirect_uri).netloc or redirect_uri
    err_html = (
        '<p class="err">That didn&#39;t work — check the token and try again.</p>' if error else ""
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Authorize MCP access</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_STYLE}</style></head><body>
<h1>tgram-analytics</h1>
<div class="who">Authorizing <b>{escape(client_name)}</b><br>
code will be sent to <b>{escape(host)}</b></div>
<p>Paste an MCP token from your Telegram bot
(<code>/mcp_token new &lt;label&gt;</code>). If you didn&#39;t start this
in your own MCP client, close this tab.</p>
{err_html}
<form method="post" action="">
  <input type="password" name="token" autocomplete="off" spellcheck="false"
         placeholder="mcp_..." required>
  <input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
  <input type="hidden" name="client_id" value="{escape(client_id)}">
  <input type="hidden" name="redirect_uri" value="{escape(redirect_uri)}">
  <input type="hidden" name="state" value="{escape(state)}">
  <input type="hidden" name="code_challenge" value="{escape(code_challenge)}">
  <button type="submit">Authorize</button>
</form>
</body></html>"""
