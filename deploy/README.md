# Deploying tele_scraper

Kustomize, one base plus `dev` and `prod` overlays. Environment differences live only in
overlays (CLAUDE.md §14.10).

```bash
kubectl kustomize deploy/overlays/dev            # render
kubectl apply -k deploy/overlays/dev --dry-run=server
kubeconform -strict -ignore-missing-schemas <(kubectl kustomize deploy/overlays/dev)
kubectl apply -k deploy/overlays/dev
```

`-ignore-missing-schemas` is needed only for the `ServiceMonitor`, whose schema ships with the
Prometheus Operator CRDs.

## Shape of the workload

A single-replica `Deployment` with `strategy: Recreate`, not a CronJob. The cadence lives in the
process (`RUN_INTERVAL_SECONDS`).

**Do not scale beyond one replica.** Each replica would evaluate and notify independently, and
the SQLite dedup store is not shared, so every recipient would be paged once per replica.
Scaling out requires leader election or a shared store first (CLAUDE.md §14.11).

The dedup store lives on a PVC. It must never be an `emptyDir`: on every restart the pod would
forget what it had already sent and re-alert on everything outstanding.

## Releasing an image

Images are built and published by CI, not from a laptop — a locally built image is
unreproducible and unattributable.

```bash
git tag v0.1.0
git push origin v0.1.0
```

That fires [release.yml](../.github/workflows/release.yml), which re-runs the full gate (ruff,
mypy, pytest) before building, then pushes to GitHub's own container registry. It authenticates
with the built-in `GITHUB_TOKEN`, so there are no registry credentials to configure or rotate.

Two tags are published and **`:latest` deliberately is not** (CLAUDE.md §13.7):

```
ghcr.io/bereketfeyissa/telecloud-subscription-renewal-notifier:<full-git-sha>
ghcr.io/bereketfeyissa/telecloud-subscription-renewal-notifier:v0.1.0
```

Deploy the **SHA**, not the version tag: a version tag can be moved, a SHA cannot. The run's
summary page prints the exact reference to paste into the overlay's `newTag`.

### One-time setup after the first release

A new GHCR package is **private by default**, and the cluster cannot pull it until you either
make it public, or create a pull secret:

```bash
# Option A - public image, simplest, nothing in the cluster to maintain
gh api -X PATCH user/packages/container/telecloud-subscription-renewal-notifier \
  -f visibility=public

# Option B - keep it private, give the cluster a read-only token
kubectl create secret docker-registry ghcr \
  --namespace tele-scraper \
  --docker-server=ghcr.io \
  --docker-username=BereketFeyissa \
  --docker-password=<a PAT with read:packages> \
  && kubectl patch serviceaccount tele-scraper -n tele-scraper \
       -p '{"imagePullSecrets":[{"name":"ghcr"}]}'
```

### Refreshing the base image digest

The Dockerfile pins `python:3.12-slim` by digest. To move it deliberately:

```bash
docker pull python:3.12-slim
docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim
```

Dependabot will not propose Python major or minor bumps; the interpreter is pinned by
CLAUDE.md §4 and `requires-python`.

## Before first deploy

1. **Release an image** and set `newTag` in the overlay to the published SHA (above).
2. **Create the real Secret** (see below) — including `PORTAL_PASSWORD_HASH`, the digest the
   browser sends rather than the password itself.
3. **Set `PORTAL_SUSPENDED_TOKENS`** once the portal's vocabulary is known. While it is empty,
   no component is ever classified `SUSPENDED`. Expiry detection does not depend on it.
4. **Leave `NOTIFY_ENABLED=false`** until a dry run against the real portal looks right, then
   verify delivery with `--test-notify` before trusting it.

## Credentials

`deploy/base/secret.stub.yaml` has placeholder keys and **no values**. Never commit real ones.

Supply the real Secret with SealedSecrets:

```bash
kubectl create secret generic tele-scraper-secrets \
  --namespace tele-scraper \
  --from-literal=PORTAL_PASSWORD='...' \
  --from-literal=SMTP_PASSWORD='...' \
  --from-literal=TELEGRAM_BOT_TOKEN='...' \
  --from-literal=SLACK_WEBHOOK_OPS='https://hooks.slack.com/services/...' \
  --from-literal=DISCORD_WEBHOOK_OPS='https://discord.com/api/webhooks/...' \
  --dry-run=client -o yaml | kubeseal --format yaml > deploy/overlays/prod/sealedsecret.yaml
```

Webhook URLs are credentials, so the routing table references them by **name** (`address_env`)
and the value arrives from the Secret.

### Rotating a credential

```bash
# 1. Update the Secret (re-seal, or edit the external secret store).
# 2. Restart: the process reads configuration only at startup.
kubectl rollout restart deployment/prod-tele-scraper -n tele-scraper
kubectl rollout status  deployment/prod-tele-scraper -n tele-scraper
```

Rotating the portal password is urgent if you ever see `AuthError` in the logs: a 401/403 is
treated as a credential incident and is never retried, precisely so a rotation does not lock the
account.

## Changing the run interval

Edit `RUN_INTERVAL_SECONDS` in the overlay's `configMapGenerator`, then apply. The ConfigMap is
generated with a name-suffix hash, so the pod template changes and Kubernetes rolls
automatically — no manual restart needed.

`RUN_TIMEOUT_SECONDS` bounds one cycle. `terminationGracePeriodSeconds` in the Deployment must
stay larger than it, or a cycle will be killed mid-flight on shutdown.

## Adding a recipient or a channel

Edit `deploy/base/routes.json` and apply. No code change, no image rebuild.

```json
{
  "recipient": "network-lead",
  "channels": [
    { "channel": "telegram", "address": "123456789" },
    { "channel": "discord", "address_env": "DISCORD_WEBHOOK_NETWORK" }
  ],
  "statuses": ["EXPIRED", "UNKNOWN"],
  "components": ["db-*"],
  "locale": "en",
  "quiet_hours": { "start": "22:00", "end": "06:00", "tz": "Africa/Addis_Ababa" }
}
```

- `channels` — one entry per medium; a recipient may have as many as they like.
- `address` for a plain value, `address_env` for anything secret (add the key to the Secret too).
- `statuses` — `ACTIVE` is rejected; routing on it would notify on every healthy component.
- `components` — glob patterns matched against component id **or** display name.
- `quiet_hours` — holds `EXPIRING_SOON` and `SUSPENDED`. `EXPIRED` and `UNKNOWN` are critical and
  always page. A held alert is not marked as sent, so it goes out when the window closes.

Validate before applying:

```bash
NOTIFY_ROUTES_FILE=deploy/base/routes.json make check-config
```

## Verifying channel credentials

Before trusting an alert to reach anyone, prove the credentials work:

```bash
kubectl exec -n tele-scraper deploy/prod-tele-scraper -- \
  python -m tele_scraper --test-notify
```

Sends one clearly-marked test message to every recipient on every channel, through the same
templates a real alert uses. Requires `NOTIFY_ENABLED=true`, because it genuinely sends
(CLAUDE.md §3.5). It bypasses dedup, quiet hours and acknowledgement — it is testing delivery,
not suppression. Exit `0` if every channel worked, `3` if any failed, with the per-channel error.

Telegram needs a chat id, which Telegram only reveals after someone contacts the bot. Message
the bot (or add it to the group), then:

```bash
kubectl exec -n tele-scraper deploy/prod-tele-scraper -- \
  python -m tele_scraper --telegram-chats
```

Put the printed `chat_id` into the route's Telegram `address`.

## One-off run

```bash
kubectl exec -n tele-scraper deploy/prod-tele-scraper -- \
  python -m tele_scraper --once --dry-run
```

Drop `--dry-run` to actually deliver. Exit codes: `0` clean, `1` scrape failure,
`2` an `UNKNOWN` was present, `3` a delivery failed, `4` configuration or state-store failure.

## Monitoring

Scraped from `/metrics` on port 9100.

| Series | Use |
|---|---|
| `scrape_last_success_timestamp_seconds` | **Alert on staleness.** The single most important series. |
| `scheduler_up` | 0 when the loop has stopped. |
| `scrape_components{status}` | Current count per derived status. Alert on `UNKNOWN > 0`. |
| `scrape_parse_failures_total` | Rising means the portal's markup changed. |
| `notifications_failed_total{channel,reason}` | Delivery problems, by channel. |
| `notifications_suppressed_total{channel,reason}` | Dedup, quiet hours, dry run, notify disabled. |
| `scrape_components_derived_expiry` | Components whose expiry was computed, not scraped. |
| `scrape_components_missing` | Components the portal has stopped listing. Non-zero means an unconfirmed deletion, or an incomplete listing. |

A suggested staleness rule:

```yaml
- alert: TeleScraperStale
  expr: time() - scrape_last_success_timestamp_seconds > 3 * 3600
  for: 10m
  annotations:
    summary: tele_scraper has not completed a successful cycle in over 3 hours
```

Health endpoints: `/healthz` (liveness — is the loop still checking in?) and `/readyz`
(readiness — config valid and state store reachable). A run that produces `UNKNOWN` does **not**
flip liveness; that is an alert about the portal, not a reason to restart the pod.
