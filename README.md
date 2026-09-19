# Telecloud Subscription Renewal Notifier

Watches the purchased **telecloud components** on Ethio Telecom's self-service portal and tells
people before one lapses — by email, Telegram, Slack or Discord.

> **A note on names.** The Python package, the container and the Kubernetes resources are still
> called `tele_scraper` / `tele-scraper`, so commands below read `python -m tele_scraper` and
> `kubectl ... deploy/tele-scraper`. Scraping is only how this works; renewal notification is
> what it is *for*, which is what the project is named after.

This is an **alerting system**. A missed or wrong alert is a production incident, so the design
prefers loud failure over quiet optimism: nothing is ever reported healthy on the strength of
missing data.

Engineering rules are in [CLAUDE.md](CLAUDE.md), which is authoritative and outranks this file.

## Why it exists

The portal reports `status: "Active"` for **every** component — including ones whose
`expirationTime` passed days earlier. Trusting that field would mean never noticing an expiry.
So the status is derived from the expiry date alone, and the portal's own opinion is recorded
but never allowed to decide.

## Status model

Derived per component, first match wins:

| Status | Meaning |
|---|---|
| `SUSPENDED` | The portal explicitly reports a terminating state (see `PORTAL_SUSPENDED_TOKENS`). |
| `UNKNOWN` | Expiry missing, unparseable, or not timezone-aware. **Always alerts.** |
| `EXPIRED` | `expires_at <= now`. |
| `EXPIRING_SOON` | Inside the warning ladder (default 14d, 7d, 3d, 1d, 12h). |
| `ACTIVE` | Valid, beyond every rung. The quiet state; never notified on. |

Four rules carry most of the safety:

- **`UNKNOWN` is never collapsed into `ACTIVE` or skipped.** Absence of evidence is not evidence
  of health.
- **A scrape returning zero components is a run-level `UNKNOWN`**, not a clean bill of health.
- **A component that disappears from the listing is raised**, not silently dropped — a deletion
  and an incomplete listing look identical from here, so a human decides.
- **The service watches its own login.** An expired portal password does not degrade this
  service, it silences it, and the resulting absence of alerts looks like good news.

## Alerts repeat until someone confirms them

An unconfirmed alert is re-sent **every run**. Confirming is what stops it — not elapsed time.

- Telegram messages carry a **✅ Confirm** button. The bot polls Telegram (`getUpdates`), so this
  works **outbound-only**: no webhook, no Ingress, no public endpoint.
- `--ack <component_id> --by <name>` does the same from the command line, and is the fallback
  for recipients on email, Slack or Discord who cannot confirm in-channel.
- A confirmation is scoped to one `(component, status, rung)`, clears it for **every** recipient,
  lapses if the underlying data changes, and expires after `ACK_TTL_DAYS` (default 7). An ack is
  a snooze, never permanent silence.

Only Telegram can confirm in-channel. That is a deliberate consequence of having no public
endpoint: Telegram can be polled outbound, whereas Slack and Discord buttons require them to
call in. Because an ack clears the alert for everyone, a single Telegram recipient — or anyone
with `--ack` — is enough to quieten a route. A route with **no** Telegram recipient and nobody
using the CLI will repeat forever.

## One message per component, or one per group

A route chooses with `mode`:

```json
{ "recipient": "ops", "mode": "summary", "summary_ack": "components" }
```

`detailed` (the default) sends one message per component. `summary` sends one per **status
group** — nine components across three channels is eighteen messages in detailed mode and six
in summary. Statuses are never mixed in one message, because `EXPIRED` ignores quiet hours and
`EXPIRING_SOON` respects them; a digest is critical if anything in it is; an empty group sends
nothing.

`summary_ack` decides what confirming a digest acknowledges:

| value | effect |
|---|---|
| `components` | acknowledges each item listed, so the digest **shrinks** as items are confirmed |
| `digest` | acknowledges the set as a unit; changing the set re-sends it in full |
| `none` | informational; no Confirm button anywhere, repeats every run |

## Notification channels

`email`, `telegram`, `slack`, `discord` — chosen **per recipient**, and one recipient may receive
on several at once.

| channel | delivers | can confirm in-channel |
|---|---|---|
| telegram | yes | **yes** — inline Confirm button, polled outbound |
| email | yes | no — text only, use `--ack` |
| slack | yes | no — text only, use `--ack` |
| discord | yes | no — text only, use `--ack` |
| sms | **no** — provider undecided | no |
 Routing is configuration ([routes.json](deploy/base/routes.json) in a
ConfigMap), never code: adding a person or a channel needs no code change.

`sms` is declared but **not implemented** — no provider has been chosen. A route may name it, but
a delivery attempt fails loudly rather than silently dropping an alert. See
[sms.py](src/tele_scraper/notify/sms.py) for what is still needed.

## Quick start

```bash
make install                 # uv sync --frozen --all-groups
cp .env.example .env         # then fill in the SECRET-marked values
make check-config            # validate settings and the routing table
make dry-run                 # one cycle: evaluate everything, log intended sends, deliver nothing
make check                   # the gate: ruff + mypy + pytest + coverage
```

Nothing is ever delivered unless `NOTIFY_ENABLED=true`. The default is `false`.

## Commands

| Command | Purpose |
|---|---|
| `make run` | The long-running service: scheduler plus the Telegram ack listener. |
| `make once` | A single cycle; exits with that cycle's code. |
| `make dry-run` | Evaluate and log intended notifications, send nothing. |
| `make check-config` | Validate settings and routing, then exit. |
| `make test-notify` | Send one test message to every recipient and channel. Needs `NOTIFY_ENABLED=true`. |
| `make telegram-chats` | List chat ids that have messaged the bot, for the routing table. |
| `python -m tele_scraper --ack <id> --by <name>` | Confirm outstanding alerts for a component. |
| `python -m tele_scraper --capture <path>` | Save a raw portal response, for diagnosing markup changes. |

## How it talks to the portal

`POST /api/iam/v1/login` returns a JWT valid for about two hours, carried on later requests in a
`token` header. A fresh one is fetched **per run**, since that lifetime is shorter than the pod's.

The password is hashed in the browser and never sent in the clear, so `PORTAL_PASSWORD_HASH`
takes that digest and replays it: this service never handles the real password, and assumes
nothing about the hashing algorithm.

A wrong credential arrives as **HTTP 200 with a non-zero `status`**, not a 401. It is treated as
a credential incident and never retried, so a bad password cannot hammer the account.

Components come from `GET /api/cbp/thirdapi/v1/renewal_service/renewlist`, paginated. One trap
worth knowing: the response's `validityPeriod` is **time remaining**, not the purchased duration
— it goes negative once a component lapses — so it is deliberately never used to derive an expiry.

## Operations

Deployment, releases, credential rotation and day-to-day changes:
[deploy/README.md](deploy/README.md).

Metrics are exposed on `/metrics`, port 9100. The series that matters most is
`scrape_last_success_timestamp_seconds` — **alert on its staleness**. A scheduler that has
quietly stopped looks exactly like "nothing is wrong".

## Contributing

`main` is protected: all five CI checks must pass and the branch must be up to date, enforced on
administrators too. Every change goes through a pull request.

Run `make check` before pushing — CI runs the same gate plus the image build, manifest validation
and a secret scan.
