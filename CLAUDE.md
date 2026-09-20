# CLAUDE.md — tele_scraper

**Authoritative contract for all work in this repository.** Rules here use RFC-2119 language.
`MUST` / `MUST NOT` are non-negotiable. `SHOULD` requires a written justification in the PR
body to deviate. If this file conflicts with your defaults, **this file wins**. If this file
conflicts with an explicit user instruction in the current conversation, the user wins — and you
MUST say so out loud in your reply.

**Start at §0 — it governs everything below it.**

---

## 0. Rule Zero — confirm before you implement

**MUST ask and receive an answer before implementing anything that is not fully specified.
Never guess. Never assume. Never "fill in something reasonable" and proceed.**

This rule outranks every other rule in this file, including §16's instruction to keep working.
When an unknown blocks part of the work, **stop and ask** rather than inventing an answer.

Concretely, you **MUST** stop and ask when any of these is not already written down in this
file, in the repo, or in an explicit instruction from the user:

- Portal HTML structure, selectors, field names, URLs, auth flow, or status vocabulary.
- The meaning, format, or timezone of any scraped date, duration, or status string.
- Recipients, channels, routing, escalation policy, thresholds, or message wording.
- Any new dependency, service, datastore, top-level directory, module, or notification channel.
- Credentials, endpoints, cluster names, namespaces, registries, schedules, or resource sizing.
- Anything ambiguous in the request, or where two readings produce materially different work.
- Anything destructive, outward-facing, or irreversible (see §16).

**How to ask:**
1. State precisely what is unknown and why it blocks you.
2. Offer the candidate options with a recommendation — do not present a survey.
3. Say what you will do once answered.
4. **Wait.** Do not write speculative code "to be adjusted later".

**Placeholders are not an escape hatch.** `# TODO: confirm selector`, a guessed CSS path, a
made-up field name, an invented status string, or a stubbed recipient list all count as
guessing. They are forbidden.

**While blocked, still deliver.** Complete every part of the task that does **not** depend on
the unknown, then state plainly what was left out and which question blocks it.

**Assumptions are never silent.** If the user has explicitly told you to proceed without
asking, you MAY proceed — and you MUST then write the assumption down in §2 of this file and
call it out in your reply. An assumption that is not recorded in writing is a guess.

---

## 1. Purpose

`tele_scraper` scrapes the telecom self-service portal, determines whether each **purchased
telecloud component** is **ACTIVE** or **NOT ACTIVE** based on its **validity period** and
**expiration time**, and dispatches notifications to **multiple recipients** over **multiple
media** (channels). It runs containerized on a **Kubernetes cluster**.

The system is an **alerting system**. A missed or wrong alert is a production incident.
Correctness and loud failure beat availability and silence. **Never guess a status.**

### 1.1 Glossary (use these exact terms in code, logs, docs, and commits)

| Term | Meaning |
|---|---|
| `component` | One purchased telecloud item (VM, storage, license, bundle) with its own lifecycle. |
| `component_id` | Stable portal-assigned identifier. The primary key. Never derived from display name. |
| `validity_period` | Purchased duration of the component (e.g. 30 days), as sold. **Not the portal's `validityPeriod` field**, which is time *remaining* and goes negative once lapsed — verified arithmetically against live data on 2026-09-17. Never map one to the other. |
| `activated_at` | UTC instant the component's validity started. |
| `expires_at` | UTC instant the component's validity ends. Authoritative for status. |
| `portal_status` | Raw status string as displayed by the portal. Stored verbatim, never trusted alone. **Confirmed on live data: the portal reports `"Active"` for components whose `expirationTime` passed days earlier.** This is the failure the system exists to catch. |
| `status` | Our derived enum. See §6. The only value notifications may act on. |
| `recipient` | A person or group that receives notifications. |
| `channel` | A delivery medium (email, telegram, sms, slack, webhook). |
| `run` | One full scrape → evaluate → notify cycle. |

`telecloud` is one word, lowercase, in identifiers. Do not write `tele cloud`, `TeleCloud`, or
`tele_cloud`.

---

## 2. Baseline decisions

**Confirmed by the user on 2026-09-16.** These are settled. Changing one requires a new
confirmation and an update to this file in the same change.

| # | Decision |
|---|---|
| 1 | Language is **Python 3.12**, toolchain supplied by `uv`. Stack pinned in §4. |
| 2 | Runtime is a **long-running Deployment** with an internal scheduler — **not** a CronJob. |
| 3 | Channels are **email, telegram, slack, discord, sms**, configured **per recipient**; one recipient MAY have several. |
| 4 | Build order: scaffold everything specified now; the portal parser lands **last**, against a real redacted sample. |
| 5 | Scraping runs with the account owner's own credentials and is authorized by them. |
| 6 | **Operational:** the portal password expires **2026-10-14** and the account on **2026-12-17**. `PORTAL_PASSWORD_HASH` must be refreshed after any password change, or every run fails with `AuthError`. Both dates are now watched automatically — see §6. |

**Still OPEN — §0 applies. Do not guess these.**

| # | Open question | Blocks |
|---|---|---|
| 1 | **ANSWERED 2026-09-17.** Login: `POST /api/iam/v1/login` with JSON `{username, password}`, the password being a client-side digest (the portal never sees plaintext). Success returns the usual envelope with `data.token`, a **JWT valid for 2 hours**, carried on later requests in a `token` header. A wrong credential arrives as **HTTP 200 with a non-zero `status`**, not a 401 — treated as `AuthError` and never retried. A fresh token is fetched **per run**, since 2h is shorter than the pod's lifetime. Data: Data source is `GET /api/cbp/thirdapi/v1/renewal_service/renewlist?page&pageSize&sortField=purchaseTime&asc=true`, returning `{status, resMsg, data:{total, items[]}}`. Parser and client are implemented against a redacted capture. **Login partly known (2026-09-17):** `POST /api/iam/v1/login`, JSON body `{"username", "password"}`, where `password` is **hashed client-side** (64 hex chars, consistent with SHA-256) — the portal never receives the plaintext. **Still open:** what an *expired* session looks like on the listing endpoint (401 vs. 200-with-status), and whether the digest is plain unsalted SHA-256 — irrelevant while `PORTAL_PASSWORD_HASH` is used, which replays the browser's digest verbatim. | closed for normal operation |
| 2 | Portal status vocabulary — the exact strings meaning suspended / blocked / inactive. | `PORTAL_SUSPENDED_TOKENS`. Until set, **no component is ever `SUSPENDED`**. |
| 3 | SMS provider (Twilio / Africa's Talking / local gateway) and its credential shape. | `notify/sms.py` |
| 4 | **ANSWERED 2026-09-17:** the portal is a Vue SPA, but its data comes from a JSON API (open 1), so **Playwright is not needed and must not be added**. `selectolax` was removed as dead weight — there is no HTML to parse. | closed |
| 5 | Business timezone — `Africa/Addis_Ababa` is a **code default**, not a confirmed requirement. | display only (`APP_TIMEZONE`) |
| 6 | Non-English locales (e.g. `am`) — wording must come from the user. | `templates/<locale>/` |

---

## 3. Non-negotiables

1. **MUST NOT** hardcode credentials, tokens, phone numbers, emails, chat IDs, URLs, or
   recipient lists anywhere in the repo — including tests, fixtures, docstrings, and examples.
   All of it comes from environment variables or mounted config. Fixtures MUST be redacted.
2. **MUST NOT** commit `.env`, `*.pem`, `*.key`, cookies, session dumps, or real scraped HTML
   containing account data.
3. **MUST NOT** report a component as `ACTIVE` because data was missing, unparseable, or the
   scrape failed. Absence of evidence is `UNKNOWN`, and `UNKNOWN` is an operator alert. See §6.
4. **MUST NOT** silently swallow an exception. No bare `except:`. No `except Exception: pass`.
   Every caught exception is either re-raised, or logged with context **and** reflected in the
   run's exit status and metrics.
5. **MUST NOT** send a notification from unit tests, dry runs, or local development unless
   `NOTIFY_ENABLED=true` is explicitly set. Default is `false` outside production.
6. **MUST NOT** *consume* `:latest`, a floating tag, or an unpinned dependency: no manifest may
   deploy one, and no Dockerfile may build `FROM` one. **Publishing** `:latest` to the registry
   is fine and is done deliberately — it points at the newest release for anyone pulling by
   hand. The hazard is a reference that can move underneath you, not the tag existing.
7. **MUST NOT** run the container as root, or with a writable root filesystem.
8. **MUST** treat every scraped value as untrusted input: validate, coerce, bound-check, and
   never pass it into a shell, SQL string, template, or eval.
9. **MUST** make a `run` idempotent *within a run*: one notification per component-state per
   cycle. Repetition **across** runs is intentional and acknowledgement-gated — see §8.2a.
   (Superseded the original "re-running MUST NOT duplicate" on 2026-09-17 by user decision.)
10. **MUST** store and compute all instants in UTC. Convert to local time only at the
    presentation edge (notification body). Naive datetimes are forbidden.
11. **MUST** ask and get confirmation before implementing anything unspecified. Guessing,
    assuming, or inventing a detail to keep moving is a defect, not initiative. See §0.

---

## 4. Stack (pinned — do not add dependencies without asking)

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.12 | `.python-version` is authoritative. |
| Dependency mgmt | `uv` + `pyproject.toml` + committed `uv.lock` | Lockfile MUST be committed. |
| Browser scrape | Playwright (Chromium) | **Not installed yet** — §2 open question 4. Prefer plain HTTP. |
| HTTP | `httpx` | Timeouts are mandatory on every call. |
| Parsing | none | The portal serves JSON; `selectolax` was removed on 2026-09-17 as unused. Re-adding an HTML parser needs approval (§0). |
| Models / validation | `pydantic` v2 | All boundary data is a validated model. |
| Config | `pydantic-settings` | Single `Settings` object. No `os.getenv` elsewhere. |
| Retries | `tenacity` | Exponential backoff + jitter. |
| Logging | `structlog` → JSON to stdout | No `print`. No file logging. |
| Metrics | `prometheus-client` | Scraped from `/metrics`; no pushgateway. |
| Templating | `jinja2` | Notification bodies only. Autoescape on. |
| Tests | `pytest`, `pytest-asyncio`, `respx`, `freezegun` | |
| Lint / format | `ruff` (lint + format) | |
| Types | `mypy --strict` | |
| Container | Multi-stage Dockerfile, `python:3.12-slim` base | |
| Orchestration | Kubernetes `Deployment`, manifests via Kustomize | Long-running; see §14. |
| Scheduler | in-process `asyncio` loop | No Celery/APScheduler without approval. |

**Adding any new runtime dependency requires explicit user approval.** Ask first; do not install.

---

## 5. Repository layout (enforced)

```
tele_scraper/
├── .github/
│   ├── workflows/ci.yml      # the §17 gate: lint, types, tests, image, manifests, secrets
│   ├── workflows/release.yml # build & push to ghcr.io on a v* tag
│   └── dependabot.yml
├── CLAUDE.md
├── README.md
├── pyproject.toml
├── uv.lock
├── .python-version
├── .env.example              # every var, no real values
├── Dockerfile
├── .dockerignore
├── Makefile
├── src/tele_scraper/
│   ├── __init__.py
│   ├── __main__.py           # CLI entrypoint; thin
│   ├── config.py             # Settings; ONLY place env vars are read
│   ├── errors.py             # TeleScraperError hierarchy
│   ├── models.py             # Component, Status, Evaluation, NotificationEvent
│   ├── runner.py             # one run: scrape -> evaluate -> route -> send
│   ├── scheduler.py          # long-running loop, jitter, graceful shutdown
│   ├── health.py             # /healthz /readyz /metrics HTTP server
│   ├── scraper/
│   │   ├── client.py         # session, auth, fetch, retries
│   │   └── parser.py         # portal JSON -> Component; pure, no I/O
│   ├── domain/
│   │   ├── status.py         # status derivation; pure functions only
│   │   └── rules.py          # thresholds, escalation, quiet hours
│   ├── notify/
│   │   ├── base.py           # Notifier protocol
│   │   ├── registry.py       # channel name -> Notifier, built from Settings
│   │   ├── acks.py           # Telegram Confirm listener (long polling, outbound only)
│   │   ├── router.py         # recipient x channel routing + dedup
│   │   ├── email.py
│   │   ├── telegram.py
│   │   ├── slack.py
│   │   ├── discord.py
│   │   └── sms.py            # BLOCKED: provider undecided (§2 open 3)
│   ├── state/
│   │   └── store.py          # dedup / last-sent state
│   ├── observability/
│   │   ├── logging.py
│   │   └── metrics.py
│   └── templates/<locale>/   # jinja2 notification bodies
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/             # redacted HTML snapshots
└── deploy/
    ├── README.md             # rotate credentials, change cadence, add a recipient
    ├── base/                 # deployment, service, pvc, configmap + routes.json,
    │                         # portal-ca.pem, secret (stub), serviceaccount,
    │                         # servicemonitor, pdb, networkpolicy
    └── overlays/{dev,prod}/
```

**MUST NOT** create top-level directories or files outside this layout without asking.
**MUST NOT** put business logic in `__main__.py`.
`domain/` and `scraper/parser.py` **MUST** be pure: no network, no clock reads, no env access.
Time is injected as a parameter (`now: datetime`), never called inside a pure function.

---

## 6. Status derivation — the core rule

This is the single most important logic in the project. It lives in `domain/status.py`,
is a pure function, and has exhaustive unit tests.

```python
class Status(StrEnum):
    ACTIVE         = "ACTIVE"          # valid, comfortably in the future
    EXPIRING_SOON  = "EXPIRING_SOON"   # valid, but inside the warning window
    EXPIRED        = "EXPIRED"         # expires_at <= now
    SUSPENDED      = "SUSPENDED"       # portal explicitly reports inactive/suspended/blocked
    UNKNOWN        = "UNKNOWN"         # could not determine — ALWAYS an operator alert
```

Derivation order (**first match wins, no reordering**):

1. Portal explicitly reports a terminating/inactive state → `SUSPENDED`.
2. `expires_at` is missing, unparseable, or not timezone-aware → `UNKNOWN`.
3. `expires_at <= now` → `EXPIRED`.
4. `expires_at - now <= WARN_THRESHOLD` → `EXPIRING_SOON`.
5. Otherwise → `ACTIVE`.

Hard rules:

- `UNKNOWN` **MUST NOT** be collapsed into `ACTIVE`, `EXPIRED`, or "skip". It is escalated.
- If `activated_at` and `validity_period` are present but `expires_at` is absent, `expires_at`
  MAY be computed as `activated_at + validity_period` — and the record **MUST** be flagged
  `expires_at_derived=True` and logged. A derived value is never used to *suppress* an alert.
- Comparisons **MUST** use timezone-aware UTC datetimes. Comparing naive datetimes is a bug.
- `WARN_THRESHOLD` is configurable and defaults to a multi-stage ladder: 14d, 7d, 3d, 1d, 12h.
  Each rung fires **at most once** per component per cycle (see §8 dedup).
- A scrape that returns **zero components** is `UNKNOWN` for the whole run, not "all fine".
  Empty result ⇒ alert operators, exit non-zero.
- A scrape that **fails outright** — the portal is unreachable, DNS fails, the credential is
  rejected — is likewise `UNKNOWN` and **alerts**. It used to set an exit code and tell nobody,
  which meant the system was blind and silent for a whole interval; silence is
  indistinguishable from good news, and this is the most severe case, not the least. The
  message states when the next attempt is due, because the recipient's first question is
  whether anything is still trying. The scheduler retries on `RETRY_BACKOFF_SECONDS`, doubling
  to a cap, rather than sleeping the full interval — a pod that loses its first cycle to a
  startup race would otherwise stay blind until the next scheduled run.
- **Our own credentials are watched too** (`MONITOR_CREDENTIALS`, default on). The login
  response returns `passwordExpiryDate` and `expiredDate`, so they cost no extra request. They
  become synthetic components (`kind="credential"`) and run through this same ladder — but on
  the wider `CREDENTIAL_WARN_THRESHOLDS` (default `30d,14d,7d,3d,1d`), because changing a
  portal password needs coordination and a Secret rollout. This matters more than any single
  component: an expired login does not degrade the service, it **silences** it, and the
  resulting absence of alerts is indistinguishable from good news.
- A component the portal **stops listing** is `UNKNOWN`, never silently dropped
  (`DETECT_MISSING_COMPONENTS`, default on). A deliberate deletion and an incomplete listing
  look identical from here, so a human decides: the alert repeats until acknowledged, and
  acknowledging **forgets** the component rather than merely silencing it. Ids are remembered
  only for entries with a real portal id — positional sentinels
  (`unidentified-item-N`) are not identities and are never tracked.

---

## 7. Scraping rules

1. One authenticated session per run; reuse it. Do not re-login per component.
2. Every HTTP call **MUST** set an explicit connect + read timeout. No unbounded waits.
3. Retries: max 3 attempts, exponential backoff with jitter, only on transient errors
   (timeout, connection error, 429, 5xx). **Never** retry a 401/403 — that is a credential
   incident; fail the run and alert.
4. **MUST** honor a configurable delay between requests (`SCRAPE_DELAY_SECONDS`, default `1.0`)
   and a concurrency cap (`SCRAPE_CONCURRENCY`, default `2`). Do not hammer the portal.
5. Set a truthful, identifiable `User-Agent` from config. Do not impersonate a browser to evade
   controls, do not attempt CAPTCHA bypass, and do not scrape endpoints the account is not
   entitled to. If access is blocked, stop and report — do not work around it.
6. Parsing **MUST** be resilient and loud: if an expected selector is missing, produce `UNKNOWN`
   for that component and increment `scrape_parse_failures_total`. Do not return a default.
7. Selectors live in one place (`scraper/parser.py`) as named constants, not inline strings
   scattered across functions.
8. Every parser change **MUST** ship with a corresponding redacted fixture in `tests/fixtures/`
   and a test asserting the new behavior.
9. Raw HTML **MAY** be retained only in memory, or written to a path under `SCRAPE_DEBUG_DIR`
   when `SCRAPE_DEBUG=true`. It **MUST NOT** be logged or committed.

---

## 8. Notification rules

**Routing is data, not code.** Recipients, channels, and severity mapping come from
configuration (`ConfigMap` / `NOTIFY_ROUTES_JSON`). Adding a recipient **MUST NOT** require a
code change.

Routing shape (one route per recipient; a recipient may have several channels):

```json
{
  "routes": [
    {
      "recipient": "ops-team",
      "channels": ["slack", "discord", "email"],
      "statuses": ["EXPIRED", "EXPIRING_SOON", "UNKNOWN", "SUSPENDED"],
      "components": ["*"],
      "locale": "en",
      "quiet_hours": null,
      "mode": "detailed",
      "summary_ack": "components"
    },
    {
      "recipient": "billing-owner",
      "channels": ["email", "telegram", "sms"],
      "statuses": ["EXPIRING_SOON", "EXPIRED"],
      "components": ["*"],
      "locale": "am",
      "quiet_hours": {"start": "22:00", "end": "06:00", "tz": "Africa/Addis_Ababa"},
      "mode": "summary",
      "summary_ack": "components"
    }
  ]
}
```

Rules:

0. Supported channels are `email`, `telegram`, `slack`, `discord`, `sms`. Channels are chosen
   **per recipient**, and one recipient MAY receive on several at once — each is an independent
   delivery with its own dedup record and its own success/failure result.
1. Every channel **MUST** implement the same `Notifier` protocol (`send(event) -> DeliveryResult`).
   Adding a channel **MUST NOT** require touching `router.py` beyond registry wiring.
2. **Dedup is mandatory.** A `(component_id, status, threshold_rung)` triple is sent once per
   recipient per channel until the status or rung changes. State lives in `state/store.py`.
2a. **Acknowledgement gating** (`REQUIRE_ACKNOWLEDGEMENT`, default on). An unconfirmed alert
   repeats **every run**; a human confirming it is what stops it. Rules:
   - An ack is keyed on `(component_id, status, rung)` and is **recipient- and channel-
     independent**: one person confirming clears it for everyone. Recipients on slack and
     discord cannot confirm in-channel, and would otherwise be notified forever.
   - An ack is recorded against the **fingerprint the alert was sent with** (`expires_at` +
     `portal_status`). If the facts change — a renewal lands — the ack lapses and the alert
     returns. An ack covers a situation, never a component.
   - An ack **MUST** expire (`ACK_TTL_DAYS`, default 7). An ack is a snooze; permanent silence
     is how alerting dies quietly.
   - A tighter rung or a changed status produces a new key, so escalation re-arms on its own
     and confirming at 14d can never swallow the 3d warning.
   - Confirmation paths: a Telegram inline button (long polling, **outbound only** — no
     webhook, no ingress) and `--ack <component_id> --by <name>` as the universal fallback.
   Backend is configurable; default is a PVC-backed SQLite file, overridable to Redis.
   If the state store is unreachable, **fail the run** — do not fall back to "send everything".
2b. **Message mode**, per route. `detailed` (the default) sends one message per component.
   `summary` sends one per **status group** — never a single mixed message, because `EXPIRED`
   ignores quiet hours and `EXPIRING_SOON` does not, and one message cannot honour both. A
   digest is critical if anything in it is, so grouping can never downgrade an expiry. An empty
   group produces no message. `summary_ack` decides what confirming a digest acknowledges:
   `components` acknowledges each item listed, so the digest **shrinks** as they are confirmed;
   `digest` acknowledges the set as a unit and re-sends in full if the set changes; `none` is
   informational and repeats every run.
   **The Confirm button itself is Telegram-only** — Slack, Discord, email and SMS are text, and
   giving them buttons would mean an inbound endpoint this workload deliberately does not have
   (§14). The suppression each mode describes still applies on *every* channel, because acks
   are recipient- and channel-independent: one Telegram press, or one `--ack`, clears the
   alert for everyone. A route whose recipients are all on text-only channels therefore
   depends on someone with Telegram or CLI access, or it repeats forever.
3. A partial delivery failure **MUST NOT** abort the remaining sends. Collect results, log each,
   and exit non-zero if any critical delivery failed.
4. `EXPIRED` and `UNKNOWN` are **critical** and ignore quiet hours. `EXPIRING_SOON` respects them.
5. Message bodies are Jinja2 templates in `templates/`, one per channel per locale.
   Templates **MUST** include: component name, `component_id`, status, `expires_at` rendered in
   `APP_TIMEZONE` with the offset shown, and time remaining. Never render a bare UTC timestamp
   to a human without labeling it.
6. Secrets (bot tokens, SMTP passwords, API keys) **MUST NOT** appear in logs, even at DEBUG.
   Redaction is enforced in `observability/logging.py`.
7. Rate-limit responses (429) from a channel **MUST** be respected with backoff, not retried hot.

---

## 9. Configuration

- `config.py` defines one `Settings(BaseSettings)` class. It is the **only** module permitted to
  read `os.environ`. Everything else receives `Settings` by injection.
- Every setting **MUST** have a type, and either a safe default or be required with a clear error.
- The app **MUST** fail fast at startup on invalid config, with a message naming the bad variable.
- `.env.example` **MUST** list every variable with a placeholder and one-line comment, and
  **MUST** be updated in the same commit that adds a setting.
- **Route fields count too.** Anything inside `NOTIFY_ROUTES_JSON` — `mode`, `summary_ack`,
  `quiet_hours` and the rest — is not an environment variable, so the rule above never catches
  it. Every route field **MUST** appear in the `.env.example` routing example *and* in the §8
  routing shape. This is exactly how `mode` and `summary_ack` shipped undocumented.
- This is **enforced, not remembered**: `tests/unit/test_config_documentation.py` compares
  `Settings` and `Route` against `.env.example` and this file, and fails CI on any drift in
  either direction — a new setting left undocumented, or a removed one still advertised.

Required variables (non-exhaustive; keep this table in sync):

| Variable | Required | Purpose |
|---|---|---|
| `PORTAL_BASE_URL` | yes | Portal root URL. |
| `PORTAL_USERNAME` | yes* | Portal account name. From `Secret` only. |
| `PORTAL_PASSWORD_HASH` | yes* | **Preferred.** The digest the browser sends. This service then never handles the plaintext and assumes nothing about the algorithm. |
| `PORTAL_PASSWORD` / `PORTAL_PASSWORD_HASH_ALGO` | no | Plaintext alternative, hashed locally (default `sha256`, **unconfirmed** against the login page). |
| `PORTAL_TOKEN_HEADER` | no (`token`) | Header carrying the session JWT. |

\* Startup requires **one** authentication method: a username plus a password/digest, **or** a borrowed browser session.
| `PORTAL_LOGIN_PATH` / `PORTAL_COMPONENTS_PATH` | no | Portal paths. `PORTAL_COMPONENTS_PATH` defaults to the renewlist API. |
| `PORTAL_PAGE_SIZE` / `PORTAL_MAX_PAGES` | no (`50` / `50`) | Pagination size and a hard bound so a bad `total` cannot loop forever. |
| `PORTAL_SESSION_COOKIE` / `PORTAL_AUTH_HEADER` | no | A session borrowed from a browser, for capture and live testing before the auth flow is known. **Testing only** — expires, never production. |
| `PORTAL_SUSPENDED_TOKENS` | no (empty) | Portal strings meaning suspended. **Empty ⇒ nothing is ever `SUSPENDED`.** |
| `PORTAL_CA_BUNDLE` | no | Extra CA certs **added to** the default trust store, for a portal that omits its intermediate. Verification is never disabled; there is deliberately no insecure-TLS setting. |
| `APP_TIMEZONE` | no (`Africa/Addis_Ababa`) | Display timezone. Storage and compute stay UTC. |
| `WARN_THRESHOLDS` | no (`14d,7d,3d,1d,12h`) | Expiry warning ladder for components. |
| `CREDENTIAL_WARN_THRESHOLDS` | no (`30d,14d,7d,3d,1d`) | Wider ladder for our own login. |
| `MONITOR_CREDENTIALS` / `MONITOR_ACCOUNT_EXPIRY` | no (`true`) | Watch the password and account expiry the login returns. |
| `RUN_INTERVAL_SECONDS` / `RUN_JITTER_SECONDS` | no (`3600` / `30`) | Scheduler cadence. |
| `RUN_TIMEOUT_SECONDS` | no (`900`) | A cycle exceeding this is abandoned. Must stay below `terminationGracePeriodSeconds`. |
| `RETRY_BACKOFF_SECONDS` / `RETRY_BACKOFF_MAX_SECONDS` | no (`60` / `900`) | After a failed run, retry on this doubling delay rather than the full interval. |
| `NOTIFY_ENABLED` | no (`false`) | Master send switch. |
| `NOTIFY_ROUTES_FILE` / `NOTIFY_ROUTES_JSON` | yes (one of) | Recipient × channel routing. File wins. |
| `NOTIFY_TIMEOUT_SECONDS` | no (`15`) | Per-delivery timeout. |
| `REQUIRE_ACKNOWLEDGEMENT` | no (`true`) | Unconfirmed alerts repeat every run until confirmed. |
| `DETECT_MISSING_COMPONENTS` | no (`true`) | Raise `UNKNOWN` when a previously-listed component disappears. |
| `ACK_TTL_DAYS` | no (`7`) | How long a confirmation holds before the alert returns. Never unlimited. |
| `TELEGRAM_ACK_ENABLED` / `TELEGRAM_POLL_TIMEOUT_SECONDS` | no (`true` / `25`) | Poll Telegram for Confirm presses. Outbound only. |
| `DEFAULT_LOCALE` | no (`en`) | Template locale fallback. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_FROM` | if `email` routed | Mail transport. |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | no | Mail auth. |
| `SMTP_TLS` | no (`auto`) | `auto` = implicit TLS on 465, STARTTLS elsewhere. Also `ssl`, `starttls`, `none`. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_API_BASE` | if `telegram` routed | Bot credentials. |
| `STATE_BACKEND` / `STATE_DSN` | yes | Dedup store. Must be persistent, never `emptyDir`. |
| `STATE_RETENTION_DAYS` | no (`30`) | Dedup record retention. |
| `LOG_LEVEL` / `LOG_FORMAT` | no (`INFO` / `json`) | |
| `METRICS_HOST` / `METRICS_PORT` | no (`0.0.0.0` / `9100`) | Health and metrics endpoints. |
| `SCRAPE_TIMEOUT_SECONDS` / `SCRAPE_MAX_ATTEMPTS` | no (`30` / `3`) | Transport bounds. |
| `SCRAPE_DELAY_SECONDS` / `SCRAPE_CONCURRENCY` | no (`1.0` / `2`) | Politeness controls. |
| `SCRAPE_USER_AGENT` | no | Identifiable UA. |
| `SCRAPE_DEBUG` / `SCRAPE_DEBUG_DIR` | no (`false`) | Raw HTML dump. Never logged, never committed. |
| `DRY_RUN` | no (`false`) | Scrape + evaluate, log intended sends, send nothing. |

Webhook URLs are credentials: routes reference them by name via `address_env`, and the value
comes from the `Secret` as a real environment variable.

---

## 10. Code standards

1. `mypy --strict` clean. No `Any` in signatures without an inline `# type: ignore[...]` carrying
   a reason comment. No blanket `# type: ignore`.
2. `ruff check` and `ruff format` clean. Line length 100.
3. Public functions and all modules have docstrings stating **what** and **why**, not how.
4. Functions **SHOULD** stay under 40 lines and 3 levels of nesting. Extract, don't nest.
5. No global mutable state. No singletons other than the configured logger.
6. I/O and pure logic **MUST** be separated. If a function both fetches and decides, split it.
7. Custom exceptions in one module, inheriting from a single `TeleScraperError` base.
8. No `datetime.now()` inside domain logic — inject a clock. This is enforced by review.
9. No commented-out code. No `TODO` without an owner and a tracking reference.
10. Comments explain non-obvious reasoning. Do not narrate what the line already says.

---

## 11. Testing

1. **MUST NOT** merge code without tests for new behavior.
2. Unit tests **MUST NOT** touch the network. `respx` for HTTP, fixtures for HTML.
3. `domain/status.py` requires **100% branch coverage**, including: expired-by-one-second,
   exactly-at-threshold, missing `expires_at`, naive datetime rejection, derived `expires_at`,
   empty component list, and each `SUSPENDED` portal string.
4. Overall line coverage gate: **85%**. CI fails below it.
5. Time-dependent tests **MUST** freeze time (`freezegun`). No `sleep` in tests.
6. Every notifier has a test asserting: payload shape, secret redaction, dedup suppression,
   and 429 backoff behavior.
7. Integration tests hitting the real portal are marked `@pytest.mark.live`, excluded by default,
   and **MUST NOT** run in CI.

---

## 12. Observability

- Logs: JSON to stdout, one event per line. Every log line in a run carries `run_id`.
  Component-scoped lines carry `component_id`. No secrets, no raw HTML, no PII beyond what
  routing requires.
- Metrics (Prometheus, **scraped** from `/metrics` on `METRICS_PORT`; the pod is long-lived):
  `scrape_run_duration_seconds`, `scrape_runs_total{result}`, `scrape_components{status}`,
  `scrape_parse_failures_total`, `scrape_components_derived_expiry`,
  `scrape_components_missing`,
  `notifications_sent_total{channel,status}`, `notifications_failed_total{channel,reason}`,
  `notifications_suppressed_total{channel,reason}`, `acknowledgements_total{channel}`,
  `scrape_last_success_timestamp_seconds`,
  `scheduler_up`. A current count carries no `_total` suffix; only monotonic counters do.
- **MUST** expose `scrape_last_success_timestamp_seconds` so a stale-run alert can be written
  against it. A silently wedged scheduler loop is the top failure mode of this system.
- Health endpoints (served by `health.py`, same port as `/metrics`):
  `/healthz` liveness — process alive and the scheduler loop is not wedged;
  `/readyz` readiness — config valid and the state store reachable.
  A run that produces `UNKNOWN` **MUST NOT** flip liveness; it alerts, it does not restart the pod.
- A failed cycle is **retried on a backoff**, not left until the next interval
  (`RETRY_BACKOFF_SECONDS`, doubling to `RETRY_BACKOFF_MAX_SECONDS`, reset by any success). A
  run that *reached* the portal counts as a success even when what it found is alarming:
  `UNKNOWN` components are findings, not a broken cycle, and must not trigger backoff.
- Exit codes (apply to `--once` mode; the long-running service exits non-zero only on fatal
  startup/config failure): `0` success; `1` scrape failure; `2` parse/UNKNOWN present;
  `3` delivery failure; `4` config error. Document any new code here before using it.

---

## 13. Containerization

1. Multi-stage build. Builder installs with `uv`; runtime copies only the virtualenv and `src/`.
2. Base `python:3.12-slim` pinned by **digest**, not tag alone.
3. Runs as a non-root numeric UID (`10001`). `USER 10001:10001` in the Dockerfile.
4. `readOnlyRootFilesystem: true`; writable paths come from `emptyDir` mounts.
5. No build tools, compilers, shells-as-entrypoint, or `curl` in the final image.
6. `.dockerignore` **MUST** exclude `.git`, `tests/`, `.env*`, `deploy/`, caches.
7. Deployments reference the **git SHA or a digest** — never a version tag, never `:latest`.
   A release publishes three tags: the git SHA (the build's identity, and what manifests use),
   `vX.Y.Z` (a human alias), and `latest` (a pointer to the newest release, for manual pulls).
   `tests/unit/test_release_workflow.py` guards the consuming side; CI additionally greps the
   rendered manifests.
8. Image **MUST** build reproducibly from a clean checkout with no network state beyond
   the pinned lockfile.
9. `ENTRYPOINT ["python", "-m", "tele_scraper"]`; args come from the manifest.

---

## 14. Kubernetes

1. Workload is a **long-running `Deployment`** running an in-process scheduler.
   `replicas: 1` and `strategy: Recreate`. Running two replicas duplicates notifications and is
   forbidden until leader election exists — see rule 11.
2. **MUST** define `livenessProbe` and `readinessProbe` against `/healthz` and `/readyz`, plus a
   `startupProbe` so a slow first scrape does not cause a restart loop. Probe timeouts **MUST**
   exceed the scrape timeout, or Kubernetes will kill runs mid-flight.
3. **MUST** handle `SIGTERM`: stop accepting new cycles, let the in-flight cycle finish, flush
   state, then exit. `terminationGracePeriodSeconds` **MUST** exceed the max cycle duration.
4. Every container **MUST** declare `resources.requests` **and** `resources.limits`
   (cpu, memory). Unbounded pods are rejected in review.
5. `securityContext`: `runAsNonRoot: true`, `allowPrivilegeEscalation: false`,
   `capabilities.drop: ["ALL"]`, `seccompProfile: RuntimeDefault`.
6. Credentials come from a `Secret` mounted as env or file. Secret **manifests with real values
   MUST NOT be committed** — commit a stub with placeholder keys and document the sealing method
   (SealedSecrets / External Secrets) in `deploy/README.md`.
7. Non-secret config in a `ConfigMap`, including `NOTIFY_ROUTES_JSON`. A ConfigMap change
   **MUST** trigger a rollout (checksum annotation on the pod template) — the process reads
   config at startup only.
8. Dedicated `ServiceAccount` with **no** cluster permissions unless justified in writing.
9. The dedup state store **MUST** survive a pod restart: a PVC (`ReadWriteOnce`, matching
   `strategy: Recreate`) or an external store. An `emptyDir` state store is forbidden — it
   re-sends every alert on every restart.
9a. **Known risk, accepted by the user on 2026-09-17.** Production uses `csi-obs-retain`, which
   is object storage mounted through FUSE rather than a POSIX block device. SQLite's WAL mode
   is documented as not working over network filesystems, and FUSE object mounts typically do
   not provide POSIX advisory locking. If the store misbehaves the symptom is `database is
   locked` / `disk I/O error`, and §8.2 then fails the run closed — meaning **no alerts at
   all**, not an alert storm. `csi-disk` (EVS block storage) is the correct substrate if this
   proves unreliable; a `csi-disk-retain` StorageClass preserves the Retain semantics.
10. Kustomize `base/` + `overlays/dev|prod`. Environment differences live **only** in overlays.
   No `kubectl edit`, no imperative cluster mutation as part of a delivered change.
11. Scaling past one replica **MUST NOT** happen without leader election or a distributed lock
    in the dedup store. Document the design here before adding it.
12. A `NetworkPolicy` **SHOULD** restrict egress to the portal and notification endpoints.
13. A `PodDisruptionBudget` **SHOULD** be defined so node drains do not silence alerting.
14. Manifests **MUST** pass `kubectl apply --dry-run=server` and `kubeconform` before hand-off.
15. `deploy/README.md` **MUST** document: how to rotate credentials, how to change the run
    interval, how to add a recipient or channel, and how to trigger a one-off run
    (`kubectl exec deploy/tele-scraper -- python -m tele_scraper --once --dry-run`).

---

## 15. Git & change discipline

- Conventional Commits: `feat|fix|chore|docs|test|refactor|build|ci(scope): subject`.
- One logical change per commit. Do not mix a refactor with a behavior change.
- **MUST NOT** commit or push unless the user asks. **MUST NOT** force-push.
- **MUST NOT** amend or rewrite commits that already exist on a remote branch.
- Never commit on the default branch; branch first.
- Commit messages end with the attribution line the harness specifies, when one is specified.

---

## 16. Working agreement for Claude

**Before writing code:**
- Read this file. Read the existing module you are about to change, in full.
- If the change touches §6 status derivation, §8 dedup, or §14 manifests, state your plan first.

**Always:**
- Prefer editing an existing module over creating a new one.
- Keep changes scoped to what was asked. Do not opportunistically refactor unrelated code.
- Run `make check` (lint + types + tests) before reporting work complete. Report real results —
  if something fails, say so and show the output. Never describe unrun commands as passing.
- When a rule here blocks the requested approach, say which rule and propose the compliant path.

**Never:**
- Never invent portal HTML structure, selectors, field names, or API endpoints. If the real
  markup is unknown, ask for a redacted sample. A guessed selector is worse than no code.
- Never create README/docs/summary files that were not requested.
- Never add a dependency, a top-level directory, or a new notification channel without asking.
- Never weaken a threshold, disable a test, lower the coverage gate, or add `# type: ignore`
  to make a check pass. Fix the cause.
- Never run destructive commands (`kubectl delete`, `docker system prune`, `rm -rf`) against
  anything outside this working directory without explicit confirmation.

**Stop and ask when:** see §0 — it is the governing rule. In short: any unknown, any ambiguity,
any new dependency or module, any destructive or outward-facing action. Ask, then wait.
Specifically, and non-exhaustively:
- The portal's real structure, auth flow, or status vocabulary is unknown.
- The recipient/channel matrix, thresholds, or escalation policy is undefined.
- A requirement here conflicts with what the portal actually returns.
- Two readings of the request would produce materially different work.

---

## 17. Definition of Done

A change is done only when **all** of the following are true:

- [ ] `ruff check` and `ruff format --check` pass.
- [ ] `mypy --strict` passes.
- [ ] `pytest` passes; coverage ≥ 85%; `domain/status.py` at 100% branch coverage.
- [ ] No secret, credential, real recipient, or unredacted HTML added to the repo.
- [ ] `.env.example` and §9 table updated if any setting changed — including **route fields**
      inside `NOTIFY_ROUTES_JSON`, which are not environment variables and are easy to miss.
      `tests/unit/test_config_documentation.py` checks this; do not silence it.
- [ ] New/changed behavior covered by a test that fails without the change.
- [ ] Logs and metrics emitted for the new path; exit codes still match §12.
- [ ] Dockerfile still builds; image runs as non-root with a read-only root filesystem.
- [ ] Manifests pass `kubectl apply --dry-run=server` and `kubeconform`.
- [ ] This file updated if any rule, assumption, or layout changed.
- [ ] No detail was guessed: every previously unspecified decision was confirmed by the user
      (§0), and any authorized assumption is recorded in §2.

---

## 18. Commands

```
make install     # uv sync --frozen
make run         # python -m tele_scraper  (DRY_RUN=true locally)
make check       # ruff + mypy + pytest  <- gate before reporting done
make test        # pytest -m "not live"
make fmt         # ruff format
make image       # docker build -t tele-scraper:$(git rev-parse --short HEAD) .
make manifests   # kustomize build deploy/overlays/dev | kubeconform -strict
make test-notify    # send one test message per recipient/channel (NOTIFY_ENABLED=true)
make telegram-chats # list Telegram chat ids that have messaged the bot
```

`make check` is the gate. If it is red, the work is not done. CI runs the same checks plus
the image build, manifest validation and a secret scan — see `.github/workflows/ci.yml`.
