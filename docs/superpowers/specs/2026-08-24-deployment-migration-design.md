# Deployment migration: from Arcane to remote Docker contexts

Design for moving deployment off a vendor API and onto standard Docker, with a
monorepo of projects, a local client acting on a remote engine, and a
disposable test application as the first thing that ships.

Date: 2026-08-24

## Problem

The stack is shaped by its deployment tool rather than by its own needs.

Arcane accepts exactly two things: a compose document and a `.env`. It never
sees the repository. Three consequences follow, and none of them would be
chosen freely:

- `bootstrap/init.sh` fetches six config files from `raw.githubusercontent.com`
  at deploy time.
- `metrics` and `alerter` fetch their Python from the same place at container
  start.
- The compose file cannot use `include:`, because the included fragments would
  not exist on the host. That blocks decomposing a 19-service, ~700-line file.

The fetches are a genuine failure mode: GitHub being unreachable means
containers do not start. And the coupling is one-directional — nothing about
the stack requires Arcane, but everything is arranged around it.

There is also a diagnostic gap. Arcane's REST API exposes 110 paths and **none
of them return container logs**; logs are WebSocket-only in its UI. When rclone
was killed on 23 August, its exited container sat on the host for hours with
the answer in its logs, and the OOM diagnosis remains an inference because
those logs were not reachable through the API.

## Goals

- Deploy with standard Docker. No vendor in the path.
- A local client acting on a remote engine, triggered by local commands.
- One repository holding every containerised project, each independently
  deployable.
- Config and code reach the host through the deployment tooling, not through a
  network fetch at boot.
- Container logs readable, including after a container has exited.

## Non-goals

- Kubernetes. It solves multi-node scheduling and rolling updates; this is one
  node deployed by hand. It would also make FUSE mount propagation harder —
  `hostPath` with `mountPropagation: Bidirectional` and privileged containers,
  against `:rslave` on a volume today — and k3s alone would take 0.5–1 GB on a
  box with 8 GB that OOM-killed a process this week.
- Migrating every project at once. See Phases.
- Changing what any service does. This is about how they are delivered.

## Prerequisite, unverified

**`arr-vps` currently resolves to the tailscale *container*, not the host.**
Everything here depends on reaching the host itself over SSH.

The first task is to establish that, and the likely answer is installing
Tailscale on the host rather than the container alone — which gives key-free,
ACL-controlled SSH with no exposed port. Failing that, SSH over the public IP
with key auth works but is a step backwards.

Nothing else in this document should be built until this is settled.

## Phase 0: the walking skeleton

A disposable project, deployed end to end, before anything real moves.

```
projects/hello/compose.yml     one service, nginx:alpine, a published port
```

From the laptop:

```bash
docker context create vps --docker "host=ssh://<user>@<host>"
docker context use vps
docker compose -p hello -f projects/hello/compose.yml up -d
```

It must demonstrate, in order:

1. The context connects and `docker ps` lists the existing arrstack containers.
2. `compose up` creates a container on the remote host.
3. The service answers over the tailnet.
4. `docker logs hello-web` returns output.
5. `docker stop hello-web` then `docker logs hello-web` **still** returns output
   — logs on a dead container, the capability Arcane lacks.
6. `compose down` removes it cleanly, leaving arrstack untouched.

If any step fails, the model is wrong and nothing further should be built. The
whole point of the skeleton is to find that out against something disposable.

## Architecture

```
projects/arrstack/       compose.yml + include: fragments
projects/trainer-platform/
projects/hello/          the skeleton, deleted once real projects land
stacks.yml               which projects are enabled
deploy.sh                iterates enabled projects against the current context
```

Each project is its own **compose project** (`-p <name>`), so they deploy
independently: monitoring can be redeployed without bouncing Jellyfin
mid-episode, which several deploys this week did.

`stacks.yml` is a flat list of project names and an enabled flag. `deploy.sh`
reads it, and for each enabled project runs `docker compose -p <name> -f
projects/<name>/compose.yml up -d --remove-orphans`.

### arrstack stays one compose project

Its internal split — core, acquisition, library, consumption, monitoring —
uses native `include:` **within a single compose project**, not separate
projects.

This matters. `depends_on` does not cross compose projects, and
`condition: service_healthy` on `rclone-mount` is what stops slskd scanning an
empty share — a bug fixed on 23 August after it silently shared zero files for
an hour. Splitting arrstack into separate projects would reintroduce it.

`network_mode: "service:tailscale"` and `"service:gluetun"` likewise do not
cross projects.

So: decomposition for readability, one project for correctness.

### The YAML anchor

`x-mount-check: &mount-check` is defined once and referenced by nine services.
**YAML anchors do not cross documents**, and `include:` does not resolve them
across files either.

Two options, to be decided during implementation:

- **`extends:`** — Compose's native cross-file inheritance. Portable, but more
  machinery in the fragments.
- **Repeat the healthcheck** in each fragment that needs it. Three copies
  instead of one anchor, in exchange for fragments that are plain compose files
  with no cross-file references at all.

The second is preferred unless the duplication proves annoying: the value of
the fragments is that they are boring.

## Getting files to the host

Bind mounts were inventoried on 2026-08-23. Of twenty:

| kind | count | examples |
|---|---|---|
| runtime state | 14 | `./config/sonarr`, `./data/metrics`, app databases |
| host paths | 3 | `/mnt/remote-media/*`, the Docker socket |
| **repo-sourced** | **3** | `./config/homepage`, `./config/alerts`, `./config/soularr` |

State and host paths live on the host permanently and are gitignored; a remote
context handles them with no transfer at all. The gap is **three directories of
small text files**, plus the seven Python files currently fetched by `wget`.

Compose sends the *specification* to the remote daemon, not the files, and
relative bind-mount paths resolve on the daemon's filesystem. So those must
arrive some other way. Two native mechanisms:

- **`docker cp`** — a daemon API call, so it works over a remote context.
  `deploy.sh` runs a throwaway container against a named volume and copies the
  directories in. No build, no registry.
- **Build context transfer** — a service with `build:` has its context
  tarballed and sent to the remote daemon. `bootstrap` becomes a real image
  built from the repo, carrying config and Python, copying them into named
  volumes on start.

**Build context transfer is preferred.** It is Docker's own answer to "get
local files to a remote engine", it turns `bootstrap` from a wget script into
an ordinary image, and it removes the GitHub-at-boot dependency entirely.

Either mechanism is incompatible with Arcane, which never receives the repo.
That is the point at which Arcane is left behind rather than accommodated.

## Logging

Set host-wide in `/etc/docker/daemon.json`:

```json
{ "log-driver": "journald" }
```

`docker logs` already works on stopped containers, which covers most incidents.
`journald` additionally survives container *removal*, which `compose up`
performs on every redeploy, and brings rotation and retention from systemd.

`journalctl CONTAINER_NAME=arr-rclone` then answers the question that could not
be answered on 23 August.

## Phases

Each phase is independently valuable and independently revertible.

1. **Host reachability.** Tailscale on the host; confirm SSH from the laptop.
2. **Walking skeleton.** `projects/hello`, all six checks above.
3. **Logging.** `journald` driver; verify logs survive a container removal.
4. **Monorepo skeleton.** `stacks.yml`, `deploy.sh`, `projects/` layout, with
   arrstack's existing single compose file moved in unchanged.
5. **File delivery.** Build-context bootstrap; delete the GitHub fetches.
6. **Decomposition.** Split arrstack's compose with `include:`.
7. **Retire Arcane.** Remove `deploy.py` and `arcane_deploy/`.

Steps 1–3 touch nothing that exists. Step 4 moves a file. The changes with real
blast radius are 5 and 6, by which point the model is proven.

## Testing

- `deploy.sh` is tested against a `stacks.yml` fixture, asserting the exact
  commands it would run, without executing them.
- The merged or included compose must parse to the same 19 services with the
  mount-check healthcheck resolved on all nine that carry it — a diff against
  the current file, byte-identical where possible.
- Phase 2's six checks are the acceptance test for the model itself, and are
  run against a disposable project rather than the stack.

## Risks

**The host may not be reachable over SSH.** Unverified, and everything depends
on it. Phase 1 exists to find out before anything is built.

**Losing Arcane's UI.** Container states are visible through `docker ps` over
the context, and Homepage already carries service health. Logs improve rather
than regress. The loss is a browser view of the stack, replaceable by
`docker context use vps && docker ps`.

**Two mechanisms during migration.** Between phases 4 and 7 both Arcane and the
new path can deploy the same stack. They must not be used concurrently — a
deploy from each would fight over container state. Whichever is authoritative
should be the only one used, and Arcane should be retired promptly rather than
kept as a fallback.

**Concurrent arrstack development.** Feature work continues in the same
repository. Phase 4 moves `docker-compose.yml` into `projects/arrstack/`, and
phase 5 changes how config reaches the host. Sequencing these against feature
work, rather than running both at once, avoids merge friction.
