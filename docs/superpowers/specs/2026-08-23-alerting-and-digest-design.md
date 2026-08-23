# Alerting and daily digest

Design for `alerter`, a container that watches the metrics the stack already
collects, pushes threshold alerts to ntfy, and emails a daily digest via
Resend.

Date: 2026-08-23

## Problem

The stack collects 27 metrics every five minutes and charts them, but nothing
reads them unless a human opens the dashboard. Two failures this week went
unnoticed until they had already cascaded: rclone was killed and nine
containers stopped themselves before anyone looked, and slskd spent an hour
sharing zero files after a deploy while reporting itself healthy.

Charts answer "what happened". They do not answer "tell me now".

## Scope

In scope:

- Threshold evaluation over collected metrics, with three states and a
  notification on every transition.
- Push delivery to ntfy.
- A daily email digest covering what arrived, what left, what was contributed,
  capacity, backlog and health.
- A small HTTP surface for health, digest preview and currently-open alerts.

Out of scope:

- A configuration UI. Rules are a file in git.
- Self-hosted ntfy. The server is a variable so this stays a one-line change
  later.
- Paging, escalation, on-call rotation, acknowledgement. This is a media stack,
  not life support.

## Architecture

Three deliverables, two of them relocations of code that already exists:

```
metrics/app.py             collector + chart page   (moved out of docker-compose.yml)
alerter/app.py             evaluation + delivery    (new)
config/alerts/rules.toml   thresholds               (new, git-tracked)
```

### Why a separate container

`alerter` runs beside `metrics` rather than inside it. Alerting can then fail
without stopping collection, which matters precisely when things are going
wrong: an exception in a notification path must not cost us the history we
need to understand the incident. It also gets its own memory limit and can be
restarted alone.

### Why the programs move to files

The metrics program is ~1,100 lines embedded as a string inside
`docker-compose.yml`. That was reasonable at 200 lines and is not at 1,100:
no syntax highlighting, no linting, unreadable diffs, and `$` characters are a
live hazard because Compose interpolates the string.

The "everything Arcane needs is in one file" property has already lapsed —
`bootstrap/init.sh` fetches five config files from GitHub at deploy time.
Fetching Python the same way is consistent with what the stack already does.

Both containers start with:

```yaml
command: ["sh","-c","wget -qO /app/app.py <raw-github-url> && exec python3 /app/app.py"]
```

`python:3.12-alpine` ships busybox `wget`, so this needs no new image, no build
step and no `pip install`. If GitHub is unreachable the container exits
loudly and `restart: unless-stopped` retries — the same failure mode
`bootstrap/init.sh` already has.

Files are fetched from `main`, so a deploy picks up whatever is on `main`.
This matches the existing bootstrap behaviour.

### How alerter reads data

`alerter` opens the metrics SQLite file **read-only** through a shared bind
mount, rather than calling the metrics HTTP API. SQLite supports one writer
and many readers concurrently.

The reason is availability: alerter keeps working when metrics is down, which
is what makes the staleness rule possible. A monitor that goes silent when its
subject dies is the classic failure, and this stack has already demonstrated
it.

`alerter` runs its own loop, independent of the collector's five-minute
cadence.

## Rules

`config/alerts/rules.toml`, parsed with stdlib `tomllib` (built in from Python
3.11, so still no dependencies).

```toml
[[rule]]
metric    = "vps_disk.used_bytes"
of        = "vps_disk.total_bytes"
name      = "VPS disk"
direction = "above"
warning   = 80
error     = 90

[[rule]]
metric    = "slskd.downloads.success_rate"
name      = "Soulseek success rate"
direction = "below"
warning   = 75
error     = 60

[[rule]]
metric    = "slskd.connected"
name      = "Soulseek connection"
direction = "below"
error     = 1
debounce  = 3
```

Fields:

| field | required | meaning |
|---|---|---|
| `metric` | yes | metric name as stored in the samples table |
| `of` | no | second metric to divide by, giving a percentage |
| `name` | yes | human label used in notifications |
| `direction` | yes | `above` or `below` — which side of the threshold is unhealthy |
| `warning` | no | threshold for 🟡; omit for a rule that is only ever red |
| `error` | no | threshold for 🔴; omit for a rule that only ever warns |
| `debounce` | no | consecutive samples required before a state change; default 2 |

`direction` exists because some metrics are unhealthy when high (disk, swap)
and others when low (success rate, connection). Thresholds stay single numbers.

The collector stores absolute values — `vps_disk.used_bytes` and
`vps_disk.total_bytes`, not a percentage — because a percentage loses
information a chart needs. Rules therefore accept an optional `of`, and
evaluate `100 * metric / of` when it is present. That is what makes "90% of
maximum healthy" expressible as `90` without storing derived metrics purely
for alerting. Both samples must share a timestamp; if either is missing the
rule is skipped for that cycle rather than evaluated against a stale half.

Metrics that are already ratios (`slskd.downloads.success_rate`) or booleans
(`slskd.connected`) omit `of` and are compared directly.

## State machine

Each rule holds one of `healthy`, `warning`, `error`. Every transition sends
exactly one notification, in both directions.

```
🟢 → 🟡   warning        🔴 → 🟡   improving
🟡 → 🔴   error          🟡 → 🟢   recovered
🟢 → 🔴   error          🔴 → 🟢   recovered
```

All notifications are sent at ntfy's default priority. No high or urgent
priorities: this stack does not justify bypassing do-not-disturb.

### Debounce

A state change requires `debounce` consecutive samples in the new state
(default 2). At a five-minute cadence that is ten minutes of sustained breach
before anything is sent.

Without it, a value oscillating around its threshold alternates 🔴 and 🟢 all
night. Debounce was chosen over hysteresis because it keeps each threshold a
single number in the config.

### Persistence

`alerter` keeps its own SQLite database on its own volume:

```sql
transitions(ts, rule, from_state, to_state, value, title, body)
```

The latest row per rule is the current state. The same table feeds the digest's
health section and the `/alerts` endpoint, so nothing is stored twice.

Two consequences:

- **A restart does not re-notify.** State is read from disk; an already-red
  rule stays quiet.
- **The first run notifies nothing.** Current state is recorded silently.
  Otherwise the first deploy fires every rule at once, which teaches the
  recipient to mute the channel immediately.

### Built-in staleness rule

Not from config: if the newest sample in the metrics database is older than 15
minutes, that is 🔴. Collection having stopped is the condition under which
every other rule silently becomes meaningless.

## Delivery: ntfy

```
POST {NTFY_SERVER}/{NTFY_TOPIC}
Title, Tags headers; plain-text body
```

`NTFY_SERVER` defaults to `https://ntfy.sh`, so moving to a self-hosted
instance later is one variable.

Public ntfy.sh topics are readable and writable by anyone who knows the name,
so the topic name is the only secret. It must be long and random rather than
guessable, and message bodies must not contain hostnames, paths or
credentials. The realistic worst case is a spoofed alert.

## Daily digest

Sent once a day at `DIGEST_HOUR` in `TZ` (the container is UTC by default; the
user is in Amsterdam).

| section | source |
|---|---|
| Arrived | Lidarr / Sonarr / Radarr / Bazarr history endpoints — titles, not just counts |
| Left | daily torrent snapshot diff |
| Contributed | slskd uploads, qBittorrent uploaded bytes, mean seed ratio |
| Capacity | box usage vs 24h ago, delta, projected days until full (linear extrapolation of the 7-day trend, omitted when shrinking), seeding footprint |
| Backlog | Lidarr wanted movement, queue depth, slskd success-rate trend |
| Health | transitions in the last 24h, anything still breached, failing collectors |

Counts come from metric deltas. Titles require the applications' own history
APIs, so `alerter` receives the same API keys `metrics` already has. Each call
is independently wrapped: one service being down costs one section, not the
email.

### What left

qBittorrent keeps no record of deleted torrents, and `max_ratio_act` now
removes them with their files. So `alerter` snapshots the torrent list daily
(hash, name, size) and diffs against the previous snapshot. Present yesterday,
absent today, and complete when last seen ⇒ it left, named, with its reclaimed
bytes.

### Quiet days

The digest is always sent. A quiet day means no new files arrived — storage
still moved and uploads still happened, so every data section is still
populated. Only the framing changes, leading with a short "quiet day today"
note instead of an arrivals list.

Always sending means a missing email is itself a signal.

### Resend

```
POST https://api.resend.com/emails
Authorization: Bearer {RESEND_API_KEY}
```

`RESEND_FROM` must be on a verified domain. HTML is assembled inline; no
templating dependency.

## HTTP surface

A small `http.server` thread, mirroring what `metrics` already does:

| path | purpose |
|---|---|
| `/healthz` | liveness, for the compose healthcheck |
| `/digest/preview` | renders the digest as HTML and returns it **without sending** |
| `/alerts` | currently-open alerts |

`/digest/preview` exists so the email can be iterated on without waiting a day
per change or spamming a real inbox.

`/alerts` lists every rule not currently `healthy`: notification title, when it
fired, and the current value, each expandable to the full message body that was
sent. `<details>`/`<summary>`, matching the metrics page's existing collector
status block. Dark theme, consistent with the rest of the dashboard.

## Configuration

New `.env` variables, all supplied by the user:

| variable | purpose |
|---|---|
| `NTFY_TOPIC` | long random topic name |
| `NTFY_SERVER` | defaults to `https://ntfy.sh` |
| `RESEND_API_KEY` | Resend token |
| `RESEND_FROM` | sender on a verified domain |
| `RESEND_TO` | recipient |
| `DIGEST_HOUR` | local hour to send, e.g. `8` |
| `TZ` | e.g. `Europe/Amsterdam` |

`alerter` also reuses the API keys already present for Lidarr, Sonarr, Radarr,
Bazarr, qBittorrent, slskd and Hetzner.

## Error handling

Every external call — ntfy, Resend, each application API, the metrics database
— is individually wrapped. A failure is logged and skipped, never retried into
a flood.

If Resend fails, the digest is logged and dropped rather than queued; a day's
digest is not worth a retry storm.

If the metrics database is unreadable, `alerter` stays up and the staleness
rule fires. That is the case where being alive matters most.

## Testing

- Rule evaluation and the state machine are pure functions over a sample
  series: table-driven tests covering each transition, debounce, missing
  thresholds, and both directions.
- Digest rendering runs against a seeded SQLite database with no network.
- Delivery paths are exercised against a local stub rather than real ntfy or
  Resend.
- The existing `tests/bootstrap/test_init.sh` must continue to pass, and
  `config/alerts/rules.toml` must be added to the files `bootstrap/init.sh`
  fetches, to `.gitignore`'s allow-list, and to that test's fixtures.

## Risks

**Moving the metrics program out of compose touches working code.** The stack
has had an unstable day. The move should be a mechanical relocation with no
behavioural change, verified by diffing the served page before and after.

**Fetching from `main` at startup means a bad commit breaks the next restart.**
Already true of `bootstrap/init.sh`; noted rather than solved.

**Reading another service's SQLite couples the two through a file format.**
Accepted deliberately in exchange for alerter surviving a metrics outage. The
schema is two columns and stable.
