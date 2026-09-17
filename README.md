# tele_scraper

Scrapes the telecom self-service portal, determines whether each purchased **telecloud
component** is active based on its **validity period** and **expiration time**, and notifies
multiple recipients across multiple channels. Runs as a long-lived Deployment on Kubernetes.

This is an **alerting system**. A missed or wrong alert is a production incident, so the design
prefers loud failure over quiet optimism: nothing is ever reported healthy on the strength of
missing data.

Engineering rules for this repository are in [CLAUDE.md](CLAUDE.md), which is authoritative.

## Status model

Derived per component, first match wins:

| Status | Meaning |
|---|---|
| `SUSPENDED` | The portal explicitly reports a terminating/inactive state. |
| `UNKNOWN` | Expiry missing, unparseable, or not timezone-aware. **Always alerts.** |
| `EXPIRED` | `expires_at <= now`. |
| `EXPIRING_SOON` | Inside the warning ladder (default 14d, 7d, 3d, 1d, 12h). |
| `ACTIVE` | Valid, beyond every rung. The quiet state; never notified on. |

Two rules carry most of the safety:

- **`UNKNOWN` is never collapsed into `ACTIVE` or skipped.** Absence of evidence is not evidence
  of health.
- **A scrape returning zero components is a run-level `UNKNOWN`**, not a clean bill of health.

Each rung of the ladder fires once per component per recipient per channel. Dedup keys on
`(recipient, channel, address, component_id, status, rung)` and only the latest state is kept,
so a component that recovers and degrades again alerts again.

## Notification channels

`email`, `telegram`, `slack`, `discord`, `sms` — chosen **per recipient**, and one recipient may
receive on several at once. Routing is configuration (a JSON table in a ConfigMap), never code:
adding a person or a channel needs no code change.

`sms` is not implemented: the provider has not been chosen. Routes may name it, but a delivery
attempt fails loudly rather than silently dropping an alert. See
[sms.py](src/tele_scraper/notify/sms.py) for what is needed.

## Quick start

```bash
make install                 # uv sync --frozen --all-groups
cp .env.example .env         # then fill in the SECRET-marked values
make check-config            # validate settings and the routing table
make dry-run                 # one cycle: scrape, evaluate, log intended sends, deliver nothing
make check                   # the gate: ruff + mypy + pytest + coverage
```

Nothing is ever delivered unless `NOTIFY_ENABLED=true`. The default is `false`.

## What is not built yet

The portal's real structure has not been supplied, and guessing selectors was deliberately
refused (CLAUDE.md §0). Two modules are therefore explicit, loudly-failing seams:

- [`scraper/client.py`](src/tele_scraper/scraper/client.py) — `login()` needs the real auth flow.
- [`scraper/parser.py`](src/tele_scraper/scraper/parser.py) — needs a redacted sample page.

Both raise with the exact list of outstanding questions. Everything else — configuration, the
status ladder, routing, dedup, quiet hours, all four working channels, metrics, health probes,
the container, and the manifests — is implemented and tested.

## Testing against the real portal

The portal's auth flow is not implemented, so `login()` refuses to guess. To reach the real
site anyway, borrow a session from a logged-in browser:

1. Log into the portal in your browser, open the components page.
2. **DevTools → Network → reload →** click the page request → **Request Headers** → copy the
   entire `Cookie:` value. (Use the Network tab, not `document.cookie` — session cookies are
   usually `HttpOnly` and will not appear there.)
3. Put it in `.env` as `PORTAL_SESSION_COOKIE=...`. You do **not** need `PORTAL_USERNAME` or
   `PORTAL_PASSWORD` for this — startup requires one authentication method, not both. For a
   token-based portal use `PORTAL_AUTH_HEADER='Bearer ...'` instead.

   Split the page URL so the host and the path are separate:

   ```
   # https://portal.example/market/my-space/renewals
   PORTAL_BASE_URL=https://portal.example
   PORTAL_COMPONENTS_PATH=/market/my-space/renewals
   ```

   Putting the whole URL in `PORTAL_BASE_URL` will not work — paths are appended to it, not
   replaced. Alternatively skip both and pass the full URL to `--url`.
4. Capture it:

```bash
uv run python -m tele_scraper --capture tests/fixtures/components
# optionally target a different path:
uv run python -m tele_scraper --capture out --url /api/v2/components
```

It saves the raw response and tells you what the portal serves — `json`, `html`,
`login_page` (the session was not accepted; grab a fresh cookie), or `unknown`. That verdict
is the observation that decides the scraper's design.

**Redact the saved file** — account numbers, MSISDNs, names, emails — before sharing it. Leave
element structure, class names, date strings and status wording intact; those are what the
parser reads.

`--capture` is read-only: no parsing, no state store, no notifications.

## Operations

Deployment, credential rotation, and day-to-day changes: [deploy/README.md](deploy/README.md).

Metrics are scraped from `/metrics` on port 9100. The series that matters most is
`scrape_last_success_timestamp_seconds` — **alert on its staleness**. A scheduler that has
quietly stopped is this system's worst failure mode, because it looks exactly like "nothing is
wrong".
