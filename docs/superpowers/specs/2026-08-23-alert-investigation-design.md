# Interactive alert investigation

Design for turning an alert into a conversation: a notification carries an
**Investigate** button, which summons an agent that gathers evidence, reports
what it found, and asks permission before changing anything. Recurring
failures graduate from model reasoning to a reviewed, deterministic playbook.

Date: 2026-08-23

## Problem

Alerting tells you something broke. It does not tell you what broke, and it
cannot act. Every incident this week ended with a human opening a laptop and
running the same handful of queries: container states and exit codes, the
Arcane event feed, the marker files, the metrics history around the failure.

That work is mechanical often enough to automate, and novel often enough that
a fixed script would not have covered it. The rclone kill was diagnosed by
elimination across four sources; the empty slskd share was a bind mount two
levels inside a FUSE mount, which no playbook would have anticipated.

## Scope

In scope:

- An **Investigate** action on alert notifications.
- An investigator that gathers evidence from a fixed vocabulary of checks.
- Findings and proposals delivered as notifications; actions taken only with
  explicit per-action approval.
- Playbooks: a model-authored, human-reviewed, declarative artifact that makes
  a recurrence cheap and instant.
- Quota awareness, so investigations cannot exhaust the subscription that the
  operator also uses interactively.

Out of scope:

- Autonomous action. Every mutation is approved by a human, every time.
- API-key billing. Investigations draw on the existing subscription; running
  out is an acceptable outcome, a surprise invoice is not.
- Exposing anything to the public internet.
- A web UI. The interface is notifications.

## Architecture

```
alerter          detects, notifies, owns incident records and approval tokens
investigator     node:22-alpine + @anthropic-ai/claude-code, headless
ntfy topic A     alerts and findings out          (existing topic)
ntfy topic B     commands in                      (new)
```

Two topics, two simplex channels. Correlation is by incident ID carried in the
message body, never by channel identity, so a reply to yesterday's alert still
routes to the right incident.

### Reaching the stack from a phone

The **Investigate** button is an ntfy `http` action. These are executed by the
ntfy **client app**, not by ntfy's servers, so the request originates from the
phone. With the phone on the tailnet it reaches `arr-vps:8100` directly and
nothing is exposed.

**Verified on the operator's device (2026-08-23):** both a `view` action
opening `arr-vps:8100/digest/preview` and an `http` action firing `GET
/healthz` worked from the notification, over Tailscale, with nothing exposed.

Topic B is still built, for two reasons: it is how the operator replies when
off the tailnet, and free-text answers to an agent's question cannot be
expressed as buttons.

### Invoking the model

`@anthropic-ai/claude-code` runs headless in the investigator container:

```
claude -p "<prompt>" --output-format stream-json --verbose
```

Authentication is a long-lived token from `claude setup-token`, stored in
`.env`. This draws on the subscription rather than per-token billing, which is
a deliberate choice: exhausting a quota is recoverable, an unexpected invoice
is not.

`--resume <session_id>` continues an investigation across turns. Claude Code
persists session context itself, so the system needs no store of its own for
conversation memory — only the incident record that ties a session to an alert.

## Quota

Headless emits `rate_limit_event` objects in the stream:

```json
{ "type": "rate_limit_event",
  "rateLimitType": "seven_day",
  "utilization": 0.76,
  "status": "allowed_warning",
  "resetsAt": 1787526000,
  "isUsingOverage": false,
  "surpassedThreshold": 0.75 }
```

There are two independent windows, and they behave differently:

| window | meaning | when exhausted |
|---|---|---|
| `five_hour` | the operational gate; what actually blocks a session | **defer** — retry automatically after `resetsAt` |
| `seven_day` | the budget ceiling | **decline** — retry only if `resetsAt` is under 5 hours away |

Deferring is viable for the five-hour window because the alert is still open
when it resets. Declining is right for the weekly window because there is no
near-term recovery to wait for.

These events are **emitted on threshold crossings, not continuously**, so a run
below every threshold may report nothing. The investigator captures every
event it sees and stores the latest reading per window in alerter's database.
Polling for a fresh figure is not viable: a bare `claude -p "ok"` reports a
notional cost around 0.18 USD, which on a quota-based account is not money but
is a real draw on the window. Measuring would consume the thing being measured.

Consequences:

- The **opening** notification quotes the last known figures; the **closing**
  one quotes whatever the run just observed.
- A captured event with `utilization` above 0.9, or a `status` other than
  `allowed`, raises a quota alert through the normal alerting path. No polling,
  no additional cost.
- An investigation can refuse to start, telling the operator why rather than
  spending the remainder.

Notifications show both windows with their reset times:

> Investigating — Storage Box warning
> Quota: 5h 3% (resets in 4h 20m) · weekly 76% (resets Tue 09:00)

## Playbooks

A playbook is **declarative data in the repository**, composed only of checks
the alerter already implements:

```toml
name = "storage-pressure"
matches_rule = "Storage Box"

[[check]]
kind = "metric_delta"
metric = "hetzner_box.used_bytes"
window = "24h"

[[check]]
kind = "metric_latest"
metric = "qbt.seeding_bytes"

[[check]]
kind = "container_state"
name = "arr-rclone"

[[propose]]
action = "stop_container"
target = "arr-soularr"
reason = "halts new acquisition without touching in-flight transfers"

[[propose]]
action = "set_qbt_download_limit"
value = 1048576
reason = "slows growth while the seeding backlog drains"
```

### Why declarative rather than a script

A model-authored shell script executing unattended would discard the
permission model entirely: the operator would approve individual actions while
unreviewed code ran freely beside them. Reviewing shell for safety is hard, and
the failure modes are severe.

Declarative checks can only compose operations already vetted, so **no new
capability can arise from a generated playbook**. Playbooks are reviewable as
data, diffable in a pull request, and testable without a server.

A saved prompt was also considered and rejected: it invokes the model every
time, costing the same quota and taking the same minutes, which defeats the
purpose.

### Why this makes escalation crisp

A declarative playbook either matches a known signature and concludes, or it
does not. That is a boolean. With a script or a prompt, deciding whether the
playbook "worked" would itself require judgement — meaning a model call to
decide whether a model call was needed.

### Lifecycle

1. **First occurrence.** No playbook matches. The model investigates using the
   full check vocabulary, reports findings, proposes actions.
2. **Playbook proposal.** The model emits a playbook covering what it just did.
   It is committed to the repository and reviewed like any other change. It
   does not take effect until merged.
3. **Recurrence.** The playbook runs directly — no model, no quota, results in
   seconds.
4. **Playbook failure.** If its checks do not produce a conclusion, the
   incident escalates to the model, which may propose a revision.

### Growing the vocabulary

When the model needs a check or action that does not exist, it does not
improvise. It emits a **vocabulary proposal** — a description of the operation
it wanted — which the operator reviews and, if accepted, is implemented as a
new `kind` in alerter.

This is the mechanism by which the system's capabilities expand, and it is
deliberately slow: every capability the investigator will ever have arrives
through a reviewed diff.

## Permission model

The investigator holds **read-only credentials by default**. Every mutation
follows the same path:

1. The investigator emits a proposal naming an action from the fixed vocabulary
   and its arguments.
2. Alerter mints a **single-use token**, stores it against the incident with an
   expiry, and sends a notification with one button per proposal.
3. Pressing a button calls back with that token. Alerter verifies it, executes
   the action itself — the investigator never holds write credentials — marks
   the token spent, and reports the outcome.

Tokens are essential rather than decorative: **ntfy topics are writable by
anyone who learns the name**, so topic secrecy cannot be the only control on a
channel that can now mutate the server. Knowing the topic must not be enough to
restart a container.

Actions are also rate-limited per incident, so a stuck loop cannot restart the
same container repeatedly.

## Investigation rate limiting

Investigations are limited **per incident, not per notification**. A flapping
alert must not trigger ten investigations overnight: each draws on the window
before any useful work happens, which is the fastest route to an exhausted
quota when a real incident arrives.

- One investigation per open incident, unless the operator explicitly asks for
  another.
- A hard daily ceiling across all incidents.
- A recurrence handled by a playbook does not count against either.

## Error handling

Every stage fails closed and reports:

- **Model unreachable or quota exhausted** — notify with the reason and the
  reset time; defer or decline per the rules above.
- **A check fails** — record it, continue the remaining checks, and include the
  failure in the findings. A partial investigation is more useful than none.
- **An action fails** — report the error verbatim. Never retry automatically;
  the operator approved one attempt, not a loop.
- **The investigator container is down** — alerting continues unaffected. This
  is why they are separate containers.

## Testing

- The playbook interpreter is pure over a check vocabulary: table-driven tests
  per `kind`, including failures and missing data.
- Token minting, verification, single use and expiry are tested directly,
  including replay of a spent token.
- The rate-limit parser is tested against recorded `rate_limit_event` payloads
  for both windows and every `status` value.
- Model invocation is stubbed. No test calls the API.
- The defer and decline paths are tested against fabricated `resetsAt` values,
  including the case where a weekly window resets within 5 hours.

## Risks

**The investigator is the largest thing on the host.** Node plus the CLI is a
few hundred megabytes, on a box with 8 GB and no headroom to spare. It should
idle small or start per incident, and carry a `mem_limit` like everything else.

**Model-authored playbooks are reviewed by a human who may skim.** The fixed
vocabulary is what makes a skimmed review safe: the worst a bad playbook can do
is check the wrong things and propose an action the operator must still
approve.

**Quota is a shared, non-monetary budget.** The account is quota-based, so an
investigation costs no money — it costs capacity the operator may want for
their own work. The ceilings and the refuse-to-start rule mitigate the
competition but do not remove it. That makes them a usability control rather
than a financial one, and it is why running out is an acceptable outcome.
