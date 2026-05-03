# Contributing

Thanks for your interest in contributing to `tgram-analytics/server`.

## License of contributions

This repository is licensed under the **Functional Source License, Version 1.1,
ALv2 Future License** ([FSL-1.1-ALv2](LICENSE)).

By opening a pull request you agree that your contribution is licensed under
the same FSL-1.1-ALv2 terms as the rest of the codebase. Two years after each
release, that release (including your contribution) automatically converts to
Apache License 2.0.

## What lives where

- **This repo** (`tgram-analytics/server`) — open source under FSL-1.1-ALv2.
  The ingestion API, the Telegram bot handlers for reports/alerts/funnels, the
  models, the schemas. Self-hosters get 100% of today's functionality.
- **Client SDKs** (`tgram-analytics-js`, `tgram-analytics-py`,
  `tgram-analytics-dart`) — MIT-licensed. Contributions welcome under MIT.
- **Hosted control plane** — closed source. Signup orchestration, billing,
  tenant provisioning, and quota enforcement for the hosted service live in a
  private repo. We do not accept external contributions there.

## Extension points

The server is designed so that operators (including ourselves) can plug
in custom user resolution, project-creation policies, bot filters, and
extra settings without forking. See the **Extension points** section in
[`README.md`](README.md) for the full surface, and
[`tests/fixtures/reference_plugin.py`](tests/fixtures/reference_plugin.py)
for a working example that exercises every hook. The registry itself is
in [`app/extensions.py`](app/extensions.py) and the discovery loader in
[`app/plugins.py`](app/plugins.py).

Pull requests that change any of those four files (registry, loader,
README extension docs, reference plugin) need extra review since they
are part of the stable public surface.

## Before opening a PR

1. Open an issue first for anything larger than a typo or small bug fix — let's
   agree on the approach before you write the code.
2. Run `make check` (ruff + mypy) and `make test`. Both must be green.
3. Add tests for behavioral changes. `server/tests/` has examples of both unit
   and live-PostgreSQL integration tests.
4. Keep PRs focused. One logical change per PR.
5. Follow the existing code style (enforced by ruff).

## Reporting security issues

Please do not open a public issue for suspected security problems. See
[SECURITY.md](SECURITY.md) if present, otherwise email the maintainer
privately.
