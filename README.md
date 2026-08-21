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

17 services, all defined in `docker-compose.yml`.

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
- **bazarr** — subtitle acquisition for content managed by Radarr/Sonarr
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
  `radarr`, `sonarr`, `bazarr`, `seerr`, `lidarr`, `slskd`, `soularr`,
  `navidrome`, `jellyfin`, `homepage`
- `.env.example` — template for the local `.env` (gitignored, never
  committed)
- `bootstrap/init.sh` — idempotent setup that runs as a compose service on
  every deploy: creates `config/`/`data/` directories (media directories
  under `/mnt/remote-media` are created separately, by `rclone-mount`
  itself over WebDAV — see `SPEC.md` Section 3.2), validates the Hetzner/
  Lidarr/slskd env vars are set, fetches Homepage's three config files
  from GitHub, and templates `config/soularr/config.ini` from
  `config/soularr/config.ini.template` (also fetched from GitHub) using
  `LIDARR_API_KEY`/`SLSKD_API_KEY`
- `config/homepage/` — the Homepage dashboard's three human-authored
  config files, the only files under `config/` committed to git rather
  than being runtime state (everything else under `config/` is gitignored):
  `services.yaml` (tiles and their widgets), `settings.yaml` (theme, group
  order, per-group icons and column counts), and `widgets.yaml` (the
  clock/weather/resources/search header row). Group names in
  `services.yaml` must match the `layout:` keys in `settings.yaml` exactly,
  or a group silently loses its icon and column settings and falls back to
  alphabetical ordering.
- `arcane_deploy/` — the Arcane API client and CLI logic (`env.py` for
  `.env` parsing, `client.py` for the Arcane API, `cli.py` for the
  entrypoint logic)
- `deploy.py` — thin entrypoint: `uv run deploy.py`
- `setup-host.sh` — one-time (idempotent, safe to rerun) host script:
  loads `fuse`/`tun` kernel modules, makes `/mnt` a shared mount point,
  and makes `/mnt/remote-media` its own shared self-bind mount (required
  for `rclone-mount`'s FUSE mount to actually propagate into sibling
  containers — a plain directory has no propagation state on its own),
  installing a systemd unit so all of it survives a reboot — the one step
  that requires SSH
- `tests/` — `unittest`-based tests for `arcane_deploy/` plus a shell test
  for `bootstrap/init.sh`

## First-time setup

1. Copy `.env.example` to `.env` and fill in every value: Arcane URL/API
   token/environment name, Hetzner Storage Box credentials (run
   `rclone obscure '<password>'` and paste the *obscured* output into
   `HETZNER_STORAGEBOX_PASS_OBSCURED` — never the plaintext), a Tailscale
   auth key, AirVPN WireGuard config, Soulseek/slskd credentials, and the
   `LIDARR_API_KEY` (a placeholder is fine for now — see Post-deployment
   step 4 below) and `SLSKD_API_KEY` non-empty (`bootstrap` fails otherwise),
   and `RCLONE_RC_USER`/`RCLONE_RC_PASS` (any random credential — this
   authenticates rclone's internal-only `--rc` API, used by Homepage's
   Storage Box widget; generate with
   `openssl rand -base64 24 | tr -d '=+/' | cut -c1-24`).
   The `HOMEPAGE_VAR_*` API keys can't be filled in yet — they only exist
   after each app's own first-run setup (see Post-deployment setup below);
   leave them blank for now. `.env` is gitignored and never enters version
   control.
2. SSH into the VPS once and run `sudo bash setup-host.sh`. This loads the
   `fuse`/`tun` kernel modules and makes both `/mnt` and
   `/mnt/remote-media` shared mount points (with a systemd unit so it
   survives reboots) — see `SPEC.md` Section 3.1 for why this is needed.
   This is the only step in the whole workflow that touches the host over
   SSH, and it's idempotent — safe to rerun any time (e.g. after a full
   config wipe) if you're ever unsure of its state.
3. Run `uv run deploy.py` to push `docker-compose.yml` and `.env` to
   Arcane and deploy the stack.

## Post-deployment setup

Run once after the stack first comes up (or after a full config wipe). Each
app is reachable at `http://arr-vps:<port>` over Tailscale. Homepage's
`HOMEPAGE_VAR_*` key for each app (see below) is grabbed inline, in the same
visit, rather than as a separate pass at the end — it's usually sitting on
the same Settings page you're already on.

1. **Tailscale** — *requires: nothing.* Approve the subnet route in the
   [admin console](https://login.tailscale.com/admin/machines): find `arr-vps`,
   edit its route settings, approve `172.28.0.0/24`. Without this, qBittorrent
   (routed through gluetun) is unreachable even though everything else works.
   Everything below depends on this, since it's what makes `arr-vps:<port>`
   reachable at all.
2. **qBittorrent** (`:8080`) — *requires: step 1.* Default login is
   `admin` / a random password generated on first start — retrieve it with
   `docker logs arr-qbittorrent | grep -i password`, then change it under
   WebUI settings. Set a download category/save path. Do this before
   Radarr/Sonarr/Lidarr, since their download-client setup (step 4) points
   at it. No API key needed for its Homepage tile (widget uses your
   qBittorrent login instead).
3. **slskd** (`:5030`) — *requires: step 1.* Nothing to configure in the
   Web UI: `SLSKD_REMOTE_CONFIGURATION=false`, so slskd's settings are
   declared entirely in `docker-compose.yml` (download/share paths, upload
   limits) and `.env` (credentials). This is deliberate — slskd's precedence
   is `defaults < env vars < slskd.yml < command line`, so environment
   variables are *weaker* than the YAML file, and a Web UI edit would
   silently outrank compose with no trace in git. Just log in and confirm
   it connected to the Soulseek network and that `/music` shows up as a
   share. Its `HOMEPAGE_VAR_SLSKD_KEY` is just `SLSKD_API_KEY`, already set.
   Do this before soularr (step 9).

   **On a host that previously ran with remote configuration enabled**, an
   existing `config/slskd/slskd.yml` will keep overriding the compose
   settings. Check whether its `soulseek.username`/`password` match `.env`,
   then delete it and redeploy so the environment variables take effect.

4. **Radarr** (`:7878`) / **Sonarr** (`:8989`) / **Lidarr** (`:8686`) —
   *requires: step 2.* Each app's first visit prompts you to set an
   authentication username/password — do this before anything else. Then:
   a root folder each (`/movies`, `/tv`, `/music`), and a download client
   pointing at qBittorrent. **Use qBittorrent's internal address
   `172.28.0.10:8080`, not `arr-vps:8080`** — Radarr/Sonarr/Lidarr share
   Tailscale's container network namespace, which is also multi-homed onto
   the `vpn_net` subnet, so they can reach gluetun's fixed IP directly; the
   `arr-vps` hostname path is for your own browser/Tailscale-client access,
   not container-to-container. The download client field asks for
   qBittorrent's **WebUI username/password** (from step 2) — there's no
   API-key-based option for it, in any of the three apps. Grab each app's
   own API key (Settings → General) for its `HOMEPAGE_VAR_*_KEY`. For
   **Lidarr** specifically, also copy its key into `.env`'s
   `LIDARR_API_KEY` (bootstrap uses it to template
   `config/soularr/config.ini` — until set, soularr can't authenticate; a
   placeholder value unblocks the deploy itself if Lidarr hasn't started
   yet, swap in the real key and redeploy again once reachable) and
   redeploy (`uv run deploy.py`) before step 8. All three of these must
   exist before Prowlarr's app sync (step 5) or Seerr's connections
   (step 9) can point at them.
5. **Prowlarr** (`:9696`) — *requires: step 4.* Like step 4, its first
   visit prompts for an authentication username/password — set that first.
   Add indexers, then Settings → Apps → sync Radarr/Sonarr/Lidarr so
   indexers propagate to all three automatically — this needs those three
   already reachable with known API keys. **Download clients are NOT
   configured here** — Prowlarr only syncs indexers to the *arr apps; each
   of Radarr/Sonarr/Lidarr keeps its own separate qBittorrent connection
   (set in step 4). For indexers blocked by Cloudflare, add FlareSolverr as
   an indexer proxy: Settings → Indexer Proxies → add FlareSolverr, URL
   `http://127.0.0.1:8191` (it shares the same Tailscale network namespace),
   then apply it to the indexers that need it. Grab Prowlarr's own API key
   (Settings → General) for `HOMEPAGE_VAR_PROWLARR_KEY`.
6. **Bazarr** (`:6767`) — *requires: step 4 (Radarr/Sonarr already
   configured, so Bazarr has something to connect to).* First visit
   prompts for an authentication username/password like the other arr
   apps — set that first. Settings → Radarr / Settings → Sonarr: connect
   using `127.0.0.1:7878`/`127.0.0.1:8989` (same-namespace, like the rest
   of Section 5's app-interconnection notes) and each app's API key from
   step 4. Settings → Languages: add the subtitle languages you want.
   Settings → Providers: add subtitle providers — anonymous/no-account
   providers work out of the box with lower rate limits; an
   OpenSubtitles.com account (added here, not `.env` — Bazarr stores it
   in its own config) gives better reliability if you have one. Grab its
   API key (Settings → General → Security) for `HOMEPAGE_VAR_BAZARR_KEY`.
7. **Jellyfin** (`:8096`) — *requires: nothing (independent of the arr
   apps' config, only needs the filesystem).* Run the setup wizard —
   **finish the whole wizard first**; the API key appears mid-wizard on one
   of its screens, which is easy to miss or misread as optional. If you
   miss it, generate one afterward via Dashboard → API Keys. Add `/movies`,
   `/tv` as libraries, then Dashboard → Libraries → Scan All Libraries
   after any new download. That key goes to `HOMEPAGE_VAR_JELLYFIN_KEY`.
   Real-time library monitoring is unreliable over the rclone mount (relies
   on `inotify`, which rclone's FUSE layer doesn't generate) — set up a
   scheduled **Scan Media Library** task (Dashboard → Scheduled Tasks,
   minimum interval 15 min) as the reliable fallback; it scans all
   libraries together, no per-library scheduling exists.
8. **Navidrome** (`:4533`) — *requires: nothing.* Points at `/music`;
   rescans automatically. Intentionally not on Homepage (its widget needs
   manual Subsonic-style token/salt setup, not a simple API key).
9. **soularr** — *requires: steps 3 and 4 (Lidarr's `LIDARR_API_KEY` must
   already be in `.env` and redeployed, and slskd must be up).* Mostly
   headless, but it does serve a minimal Web UI at `:8265` — there's little
   to configure there, but it's useful for viewing the generated
   `config.ini`. It's on the Homepage dashboard as a plain link (no
   widget/API key — soularr has no stats API). Verify it's actually working
   via `docker logs arr-soularr`. Runs every `SCRIPT_INTERVAL` (300s) — a
   `connection refused` on the very first cycle after a fresh deploy is
   usually just Lidarr/slskd still initializing, not a real failure.
10. **Seerr** (`:5055`) — *requires: step 4 (Radarr/Sonarr already
    configured).* Complete its own setup wizard and connect it to
    Radarr/Sonarr in Settings first — **its API key only becomes available
    after setup is finished**, not before. Lidarr music requests are not
    natively supported (Radarr/Sonarr-only). Grab the API key
    (Settings → Notifications → API key) for `HOMEPAGE_VAR_SEERR_KEY`.

After collecting the keys above, redeploy once more (`uv run deploy.py`) so
Homepage picks them all up.

## Running the tests

```bash
python3 -m unittest discover tests -v
sh tests/bootstrap/test_init.sh
```

## Deploying changes

Normal edits (e.g. `docker-compose.yml`, `.env`): just run
`uv run deploy.py` again — it's idempotent and updates the existing Arcane
project in place.

**Important exception:** `bootstrap/init.sh` and everything under
`config/homepage/` are *not* pushed by `deploy.py`. The `bootstrap` service
fetches `bootstrap/init.sh` from GitHub's `main` branch at container start
(so the script and this repo never drift out of sync), and `init.sh` in
turn fetches `config/homepage/{services,settings,widgets}.yaml` from `main`
too. Arcane's deploy API only provisions `docker-compose.yml` and `.env` on
the host — it does not clone the rest of the repo. So if you edit any of
those files, **you must `git push` to `main` first**, then run
`uv run deploy.py` — otherwise the deploy will run against your old,
already-pushed version and your local edits will silently not take effect.

## Homepage dashboard

For Homepage's dashboard tiles to show live stats, add each app's own API
key to `.env` as `HOMEPAGE_VAR_<APP>_KEY` (e.g. `HOMEPAGE_VAR_RADARR_KEY`).
Find each key on the app's own Settings/General page (Prowlarr, Radarr,
Sonarr, Lidarr), Settings → Notifications → API key (Seerr), the
`SLSKD_API_KEY` value (slskd), Dashboard → API Keys (Jellyfin), or
Settings → General → Security (Bazarr). Navidrome is intentionally left
out — its widget needs manual Subsonic-style token/salt setup instead of
a simple API key; see `config/homepage/services.yaml` for details.

The Storage tile (Hetzner Storage Box used/free/total) doesn't use an
app API key — it queries `rclone-mount`'s own `--rc` HTTP API directly
over the internal Docker network (never published externally), using
`RCLONE_RC_USER`/`RCLONE_RC_PASS` from `.env` (any random credential;
see First-time setup above).
