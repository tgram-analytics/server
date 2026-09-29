# renderer

Screenshot service for tap heatmaps. It loads one public web page in headless
Chromium (Playwright's `chromium-headless-shell`) and returns a full-page PNG.
The tgram-analytics server draws the heat layer on top of it (Pillow, in
`app/services/heatmap.py`) and sends the result in Telegram.

It runs as its own container. The API server never starts a browser and only
talks to this service over HTTP (`SCREENSHOT_URL`). Without it, heatmaps are
sent as text only.

## API

```
GET /health
  -> 200 {"status": "ok"}

GET /shot?url=<http(s) url>&width=390&dpr=2&max_height=6000
  Authorization: Bearer <RENDERER_TOKEN>      (only when RENDERER_TOKEN is set)
  -> 200 image/png
     X-Final-Url:   URL after redirects
     X-Login-Wall:  1 when the page is a login screen, else 0
     X-Doc-Height:  document height in CSS px (before the height cap)
     X-Doc-Width:   document width in CSS px
```

| Parameter | Range | Default |
|---|---|---|
| `width` | 240-2000 CSS px | 390 |
| `dpr` | 1-3 | 2 |
| `max_height` | 200-20000 CSS px | 6000 |

Errors: `400` bad URL or scheme, `401` missing or wrong token, `403` the host
(or anything the page loaded) resolves to a non-public address, `502` the
browser failed, `503` busy for more than 30 s, `504` navigation or total
timeout.

## Behaviour

- One screenshot at a time (a queue of one). Chromium is launched for each
  request and closed after it, so the idle container holds no browser.
- Viewport `width` x 844 CSS px, mobile emulation below 768 px. Waits for the
  `load` event (20 s timeout), then up to 8 s for network idle, then 1 s.
  Total budget per request: 40 s.
- Full-page PNG. The document width is capped at 2000 CSS px. The height is
  clipped to `max_height` CSS px and to a budget of 24 million device pixels
  (`width * dpr * height * dpr`).
- Login wall: the page has an `input[type=password]`, or navigation ended on
  a different path that contains `login`, `signin`, `sign-in` or `auth`.
- The page is loaded like a first-time visitor: no cookies, no stored state,
  service workers blocked, downloads refused.
- Requests to `/api/v1/track`, `/api/v1/pageview` and `/api/v1/taps` are
  blocked, so a screenshot is not counted as a visit by the analytics SDK.

## Network guard

- The target URL must be `http` or `https`, with no credentials. Its host is
  resolved once and every address must be public. Loopback, private
  (RFC 1918, ULA), link-local (including `169.254.169.254`), CGNAT,
  multicast, reserved, NAT64 (`64:ff9b::/96`) and IPv4-compatible IPv6
  addresses are refused.
- Chromium is launched with `--host-resolver-rules="MAP <host> <checked-ip>"`
  for that host, so the top-level page loads from the address that was
  checked. A DNS answer that changes after the check has no effect on it.
- Sub-requests (all pages of the browser context) go through a route check
  and are aborted when their host resolves to a non-public address.
- Redirect hops are not seen by the route check, so every request URL (plus
  WebSockets) is checked again after the screenshot. If one of their hosts
  resolves to a non-public address, the screenshot is discarded (`403`).
- Popups are blocked (`--block-new-web-contents`) and any extra page is
  closed.

**Limit.** Hosts other than the page host (images, scripts, redirect targets
on another host) are resolved by Chromium on its own. The checks above use a
separate DNS lookup, so a host that changes its DNS answer between the two
lookups can make Chromium send a request to a private address. The post-shot
check cannot detect that: it does not see the address Chromium connected to.

**Multi-tenant deployments must run the renderer on a network with no route
to private, CGNAT (`100.64.0.0/10`, which includes tailnet addresses),
link-local or other internal ranges**, for example a dedicated Docker network
with an egress firewall. The in-process checks alone are not enough there.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `RENDERER_TOKEN` | empty | When set, `/shot` requires `Authorization: Bearer <token>`. Set it whenever the port is reachable from outside a private network. |
| `PORT` | `8080` | Listen port. |
| `LOG_LEVEL` | `INFO` | Python log level. |

On the server side set `SCREENSHOT_URL` to this service's base URL and
`SCREENSHOT_TOKEN` to the same token.

## Memory and size

Measured on 2026-09-29 (x86_64, image built from this directory, container
limited with `--memory 1g --shm-size 256m`; page: a public, image-heavy
landing page, 5,569 CSS px tall on mobile and 4,721 CSS px on desktop):

| Case | Container memory peak (cgroup) | PNG | Time |
|---|---|---|---|
| idle | 35 MB | - | - |
| 390 px, DPR 2 (780 x 11138 px image) | 451 MB | 2.3 MB | 9.4 s |
| 1280 px, DPR 1 (1280 x 4721 px image) | 406 MB | 2.3 MB | 8.9 s |

Outside a container, the sum of RSS over the process tree (uvicorn, the
Playwright driver and all Chromium processes) peaked at ~930 MB; that sum
counts shared pages more than once. Proportional set size (PSS) peaked at
~600 MB.

Guidance:

- Set a **1 GB memory limit** and `shm_size: 256m` (Chromium also runs with
  `--disable-dev-shm-usage`). The 24 Mpx budget bounds the bitmap for any
  width, DPR and page height.
- Run **one replica**. The service renders one page at a time by design.
- Image size: 1.27 GB on disk (344 MB compressed). Most of it is the
  headless shell (262 MB) and the system libraries and fonts that
  `playwright install --with-deps` adds.

## Run

```bash
# Docker
docker build -t tgram-renderer renderer/
docker run --rm -p 8080:8080 --memory 1g --shm-size 256m \
  -e RENDERER_TOKEN=change-me tgram-renderer
curl -H "Authorization: Bearer change-me" \
  "http://localhost:8080/shot?url=https://example.com/&width=390&dpr=2" -o shot.png

# Docker Compose (from the repository root)
docker compose --profile heatmaps up -d
```

## Tests

```bash
pip install pytest pytest-asyncio
pytest renderer/tests
```

The guard tests use only the standard library. CI also builds the image and
takes one screenshot of `https://example.com/`.
