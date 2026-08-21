# arrstack

A self-hosted *arr media stack — Prowlarr, Radarr, Sonarr, Lidarr,
qBittorrent, FlareSolverr, slskd/soularr, Seerr, Navidrome, Jellyfin, and a
Homepage dashboard — running on a VPS behind Tailscale, with AirVPN-isolated
torrent egress (via a Gluetun sidecar) and Hetzner Storage Box-backed media
storage mounted into containers with rclone/FUSE. Deployed GitOps-style to a
running [Arcane](https://arcane.mauricio.cc) instance: nothing is applied
over SSH except a one-time host setup script.

Full architecture, rationale, and per-service configuration notes:
[`SPEC.md`](./SPEC.md).

## Applications

16 services, all defined in `docker-compose.yml`.

### Infrastructure

- **bootstrap** — not user-facing; idempotent setup that runs on every
  deploy to create directories, validate env vars, and fetch config
- **rclone-mount** — mounts the Hetzner Storage Box over WebDAV via FUSE
  so containers see it as a local media path
- **tailscale** — private network access to the stack; nothing is exposed
  to the public internet
- **gluetun** — AirVPN WireGuard tunnel that isolates torrent traffic from
  the rest of the stack's network

### Acquisition & management

- **qbittorrent** — torrent client, routed exclusively through gluetun's
  network namespace
- **prowlarr** — indexer manager that feeds search results to Radarr,
  Sonarr, and Lidarr
- **flaresolverr** — solves Cloudflare challenges for indexers Prowlarr
  can't reach directly
- **radarr** — movie acquisition and library management
- **sonarr** — TV show acquisition and library management
- **lidarr** — music acquisition and library management
- **slskd** — Soulseek P2P client, an alternate source for music beyond
  torrents
- **soularr** — bridges Lidarr's wanted list to slskd so missing albums
  get searched on Soulseek
- **seerr** — request/discovery frontend; the "click request, get media"
  UI for non-technical users, talking to Radarr and Sonarr

### Streaming

- **navidrome** — Subsonic-compatible music streaming server
- **jellyfin** — movie and TV streaming server

### Dashboard

- **homepage** — aggregates status and links for the apps above into a
  single dashboard

## Repo layout

- `docker-compose.yml` — the whole stack: `bootstrap`, `rclone-mount`,
  `tailscale`, `gluetun`, `qbittorrent`, `prowlarr`, `flaresolverr`,
  `radarr`, `sonarr`, `seerr`, `lidarr`, `slskd`, `soularr`, `navidrome`,
  `jellyfin`, `homepage`
- `.env.example` — template for the local `.env` (gitignored, never
  committed)
- `bootstrap/init.sh` — idempotent setup that runs as a compose service on
  every deploy: creates `config/`/`data/`/media directories, validates the
  Hetzner env vars are set, and fetches `config/homepage/services.yaml`
  from GitHub
- `config/homepage/services.yaml` — Homepage's dashboard tile definitions;
  the one file under `config/` that's committed to git rather than
  runtime state (everything else under `config/` is gitignored)
- `arcane_deploy/` — the Arcane API client and CLI logic (`env.py` for
  `.env` parsing, `client.py` for the Arcane API, `cli.py` for the
  entrypoint logic)
- `deploy.py` — thin entrypoint: `uv run deploy.py`
- `setup-host.sh` — one-time host script (loads `fuse`/`tun` kernel
  modules, makes `/mnt` a shared mount point) — the one step that requires
  SSH
- `tests/` — `unittest`-based tests for `arcane_deploy/` plus a shell test
  for `bootstrap/init.sh`

## First-time setup

1. Copy `.env.example` to `.env` and fill in every value: Arcane URL/API
   token/environment name, Hetzner Storage Box credentials (run
   `rclone obscure '<password>'` and paste the *obscured* output into
   `HETZNER_STORAGEBOX_PASS_OBSCURED` — never the plaintext), a Tailscale
   auth key, AirVPN WireGuard config, Soulseek/slskd credentials, and the
   `HOMEPAGE_VAR_*` API keys (see below). `.env` is gitignored and never
   enters version control.
2. SSH into the VPS once and run `sudo bash setup-host.sh`. This loads the
   `fuse`/`tun` kernel modules and makes `/mnt` a shared mount point (with
   a systemd unit so it survives reboots) — see `SPEC.md` for why this is
   needed. This is the only step in the whole workflow that touches the
   host over SSH.
3. Run `uv run deploy.py` to push `docker-compose.yml` and `.env` to
   Arcane and deploy the stack.

## Post-deployment setup

Run once after the stack first comes up (or after a full config wipe). Each
app is reachable at `http://arr-vps:<port>` over Tailscale.

1. **Tailscale** — approve the subnet route in the
   [admin console](https://login.tailscale.com/admin/machines): find `arr-vps`,
   edit its route settings, approve `172.28.0.0/24`. Without this, qBittorrent
   (routed through gluetun) is unreachable even though everything else works.
2. **Prowlarr** (`:9696`) — add indexers, then Settings → Apps → sync
   Radarr/Sonarr/Lidarr so indexers propagate to all three automatically.
3. **Radarr** (`:7878`) / **Sonarr** (`:8989`) / **Lidarr** (`:8686`) — each
   needs: a root folder (`/movies`, `/tv`, `/music`), and a download client
   pointing at qBittorrent (host `arr-vps`, port `8080`, your qBittorrent
   credentials).
4. **Lidarr's API key** (Settings → General) must be copied into `.env` as
   `LIDARR_API_KEY`, then redeploy (`uv run deploy.py`) — bootstrap uses it to
   template `config/soularr/config.ini`. Until this is set, soularr can't
   authenticate to Lidarr. A placeholder value unblocks the deploy itself if
   Lidarr hasn't started yet (chicken-and-egg on a fresh wipe); swap in the
   real key and redeploy again once Lidarr is reachable.
5. **qBittorrent** (`:8080`) — set a download category/save path matching
   what Radarr/Sonarr/Lidarr expect.
6. **slskd** (`:5030`) — confirm it connected to the Soulseek network (its
   own `SLSKD_SLSK_USERNAME`/`PASSWORD` in `.env`, distinct from the Web UI
   login). `SLSKD_REMOTE_CONFIGURATION=true` lets you edit settings from the
   Web UI directly.
7. **soularr** — no UI; verify it's working via `docker logs arr-soularr`.
   Runs every `SCRIPT_INTERVAL` (300s) and depends on Lidarr/slskd being up
   — a `connection refused` on the very first cycle after a fresh deploy is
   usually just Lidarr/slskd still initializing, not a real failure.
8. **Seerr** (`:5055`) — connect it to Radarr/Sonarr in Settings; Lidarr
   music requests are not natively supported (Radarr/Sonarr-only).
9. **Jellyfin** (`:8096`) — run the setup wizard, add `/movies`, `/tv` as
   libraries, then Dashboard → Libraries → Scan All Libraries after any new
   download. Generate an API key (Dashboard → API Keys) for
   `HOMEPAGE_VAR_JELLYFIN_KEY`.
10. **Navidrome** (`:4533`) — points at `/music`; rescans automatically.
11. **Homepage** (`:3000`) — populate the remaining `HOMEPAGE_VAR_*` keys in
    `.env` from each app's own API key (see below), then redeploy.

## Running the tests

```bash
python3 -m unittest discover tests -v
sh tests/bootstrap/test_init.sh
```

## Deploying changes

Normal edits (e.g. `docker-compose.yml`, `.env`): just run
`uv run deploy.py` again — it's idempotent and updates the existing Arcane
project in place.

**Important exception:** `bootstrap/init.sh` and
`config/homepage/services.yaml` are *not* pushed by `deploy.py`. The
`bootstrap` service fetches `bootstrap/init.sh` from GitHub's `main` branch
at container start (so the script and this repo never drift out of sync),
and `init.sh` in turn fetches `config/homepage/services.yaml` from `main`
too. Arcane's deploy API only provisions `docker-compose.yml` and `.env` on
the host — it does not clone the rest of the repo. So if you edit either of
those two files, **you must `git push` to `main` first**, then run
`uv run deploy.py` — otherwise the deploy will run against your old,
already-pushed version and your local edits will silently not take effect.

## Homepage dashboard

For Homepage's dashboard tiles to show live stats, add each app's own API
key to `.env` as `HOMEPAGE_VAR_<APP>_KEY` (e.g. `HOMEPAGE_VAR_RADARR_KEY`).
Find each key on the app's own Settings/General page (Prowlarr, Radarr,
Sonarr, Lidarr), Settings → Notifications → API key (Seerr), the
`SLSKD_API_KEY` value (slskd), or Dashboard → API Keys (Jellyfin). Navidrome
is intentionally left out — its widget needs manual Subsonic-style
token/salt setup instead of a simple API key; see
`config/homepage/services.yaml` for details.
