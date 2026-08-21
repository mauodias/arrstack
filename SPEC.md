# Technical Specification: Cloud-Native *Arr Stack with Containerized FUSE Storage, AirVPN Egress, & Zero-Trust Networking

## 1. Executive Summary & Architecture Overview

This specification defines the deployment of an automated media acquisition and management stack (*arr ecosystem) hosted on a cloud VPS (Strato). The architecture guarantees complete network isolation (no public ports exposed), zero-trust remote access via Tailscale, persistent remote storage using a containerized `rclone` FUSE mount backed by a Hetzner Storage Box, and isolated VPN egress via AirVPN (with static port forwarding) for torrent downloads to eliminate DMCA/copyright exposure on German infrastructure.

### 1.1 Key Technical Principles
* **Zero Public Exposure:** The entire stack resides on a closed Docker bridge network routed exclusively through a Tailscale sidecar container. Reverse proxies (e.g., Nginx Proxy Manager) are bypassed.
* **Isolated Torrent Egress & Kill Switch:** Torrent traffic (`qbittorrent`) is strictly bound to a `gluetun` VPN sidecar container running AirVPN with static inbound port forwarding enabled (`FIREWALL_VPN_INPUT_PORTS`). If the VPN tunnel drops, network I/O dies instantly without leaking the VPS host IP.
* **Fully Containerized Infrastructure:** No software dependencies are installed directly on the host OS beyond Docker, `containerd`, and the kernel FUSE module (`/dev/fuse`).
* **Shared Volume Propagation:** Storage mounts created inside the `rclone` container are exposed to sibling application containers using Docker's `rshared` and `rslave` volume propagation flags.
* **Component Decoupling:** Storage, network layer, commercial VPN, and core compute engines are modularized to allow zero-downtime migration to local hardware in the future.

---

## 2. System Architecture & Topology Diagram

```
                            [ TAILSCALE OVERLAY NETWORK ]
                                          |
                                +-------------------+
                                | Tailscale Sidecar |
                                +---------+---------+
                                          | (network_mode: "service:tailscale")
                                          |
   Acquisition/management apps (share this netns, talk to each other over 127.0.0.1):
     Radarr (7878)  Sonarr (8989)  Bazarr (6767)  Prowlarr (9696)  Seerr (5055)
     Lidarr (8686)  slskd (5030)   soularr (n/a)   Homepage (3000)
                                          |
   Consumption apps (same netns, read media straight off the mount):
     Navidrome (4533)   Jellyfin (8096)
                                          |
                          Volume Mounts (:rslave, per-app subpaths)
                                          |
                                +---------v---------+
                                | /mnt/remote-media |
                                +---------^---------+
                                          | Volume Mount (:shared)
                                +---------+---------+
                                |   rclone-mount    |
                                |   (FUSE Engine)   |
                                +---------+---------+
                                          | WebDAV / SFTP
                                +---------v---------+
                                | Hetzner Storage   |
                                |       Box         |
                                +-------------------+

  Notes:
  * Homepage (dashboard) aggregates *arr/qBittorrent/Jellyfin stats via their APIs,
    plus rclone 'about' for Storage Box used/free (Section 10).
  * soularr has no exposed port; it bridges Lidarr and slskd (both on this same
    netns) over 127.0.0.1 on a schedule.

 [ TORRENT EGRESS ISOLATION NETWORK ]
           |
 +---------v---------+
 |  Gluetun VPN      | <================ Encrypted Tunnel ================> [ AirVPN Servers ]
 |  (Sidecar)        |                     (WireGuard)                        (Static Port Open)
 +---------+---------+
           | (network_mode: "service:gluetun", static IP 172.28.0.10
           |  on the `vpn_net` bridge, FIREWALL_INPUT_PORTS=8080)
 +---------v---------+
 |    qBittorrent    |
 |  (Port 8080/tcp)  |
 +-------------------+
```

**qBittorrent reachability (Tailscale subnet route, not netns sharing):**
gluetun/qBittorrent are deliberately kept off the Tailscale sidecar's
network namespace — merging them would route Prowlarr/Radarr/Sonarr's
normal traffic through the VPN tunnel too, which is not what any of those
apps want. Instead, qBittorrent is reached via a Tailscale **subnet
route**: `gluetun` and `qbittorrent` share a namespace on a dedicated
Docker bridge network (`vpn_net`, `172.28.0.0/24`, gluetun statically
assigned `172.28.0.10`); the `tailscale` container is multi-homed onto
both its default network *and* `vpn_net`, and advertises
`--advertise-routes=172.28.0.0/24` (with IP-forwarding sysctls enabled).
That route must be approved once in the Tailscale admin console. gluetun's
own firewall additionally requires `FIREWALL_INPUT_PORTS=8080` to accept
that inbound traffic. Once approved, qBittorrent's WebUI is reachable
directly at `172.28.0.10:8080` — including from sibling containers that
share the Tailscale netns (Radarr/Sonarr/Lidarr), since they inherit
tailscale's network interfaces, `vpn_net` included.

Seerr, Lidarr, slskd, Navidrome, and Jellyfin all sit on the Tailscale
sidecar's network like the existing apps (Section 1.1: zero public
exposure). Soulseek (used by slskd) is a P2P network without the
public-peer-list/swarm exposure model of BitTorrent, so slskd does not
route through Gluetun — it shares the same trust model as the other
Tailscale-only application containers, not qBittorrent's.

Bazarr connects to Radarr and Sonarr over `127.0.0.1` (same netns, same
pattern as Prowlarr's app sync) to pull subtitles for content those two
already manage, and reads/writes `/movies` and `/tv` directly (subtitle
files are written alongside the media they belong to) — no `/downloads`
access needed, since it never handles acquisition itself. It has no
relationship to Lidarr/slskd/soularr's music pipeline.

---

## 3. Host System Requirements & Pre-Flight Checklist

Before executing the deployment stack via Docker Compose / Arcane, the executing agent MUST verify and prepare the host system.

### 3.1 Kernel & Driver Requirements
1. **FUSE & TUN Kernel Modules:** Confirm `/dev/fuse` and `/dev/net/tun` exist on the host.
   ```bash
   ls -la /dev/fuse /dev/net/tun
   ```
   If missing, load the required modules:
   ```bash
   sudo modprobe fuse
   sudo modprobe tun
   ```
2. **Mount Propagation Root:** Ensure the parent mount point on the host supports shared mount propagation so Docker can propagate FUSE mounts across container namespaces. This is two steps, not one — `mount --make-rshared /mnt` alone is insufficient, since `/mnt/remote-media` itself was never a mountpoint (a plain directory has no propagation state to mark shared; it must first become a self-bind mount):
   ```bash
   sudo mount --make-rshared /mnt
   sudo mkdir -p /mnt/remote-media
   sudo mountpoint -q /mnt/remote-media || sudo mount --bind /mnt/remote-media /mnt/remote-media
   sudo mount --make-shared /mnt/remote-media
   ```
   Persisted via a systemd unit (`mnt-make-rshared.service`, installed by `setup-host.sh`, Section 11.1) that reruns all four commands before `docker.service` starts, since none of this state survives a reboot on its own.

### 3.2 Directory Hierarchy

All project files (`docker-compose.yml`, `config/*`, `data/*`) live
inside whatever project directory Arcane checks the repo out to — its
exact path is an Arcane implementation detail, not something this spec
should hardcode, which is why every path in Section 5's compose file is
relative (`./config/...`, `./data/...`). Those per-app `config/*` and
`data/rclone-cache` subdirectories don't need manual creation either:
the `bootstrap` init container (Section 11) creates them idempotently,
relative to the project directory, on every deploy.

The one path that genuinely is host-absolute and independent of the
project directory is the rclone mount target, since it's a host mount
point referenced by `/dev/fuse` and bind-mounted into containers by
absolute path (Section 5):

```bash
mkdir -p /mnt/remote-media
```

`/mnt/remote-media` gains `movies`, `tv`, `music`, and `downloads`
subdirectories, but not from `bootstrap` — `bootstrap` runs and completes
before `rclone-mount` ever starts (Section 5), so any directories it
created at that path would only exist on the local disk underneath the
future mount point and would be immediately shadowed the moment
`rclone-mount` FUSE-mounts the Hetzner remote on top of it. Instead,
`rclone-mount` itself creates these directories as the first step of its
own startup command (Section 5), before it mounts: it runs
`rclone mkdir hetzner_box:<dir>` for each of the four directories, which
talks directly to the remote over WebDAV (no FUSE, no mount, no
cross-container propagation involved) using the same credentials the
container already uses for the mount itself. Every application service
that reads/writes under this path — Radarr, Sonarr, Lidarr, slskd/soularr,
Navidrome, Jellyfin, and qBittorrent — depends only on
`rclone-mount: service_healthy`, which is now sufficient since the
directories are guaranteed to exist before the mount (and thus the
healthcheck) can succeed.

---

## 4. Configuration Files Specification

### 4.1 Rclone Configuration (via environment variables — no config file)

Rather than generating a `rclone.conf` file, the `hetzner_box` remote is
defined entirely through `RCLONE_CONFIG_<REMOTE>_<KEY>` environment
variables, which rclone reads natively without any config file present.
This removes a static secrets-bearing file from the picture altogether —
the remote definition flows the same way every other secret in this
spec does: local `.env` → Arcane project env vars (Section 9) → the one
container that needs it (Section 4.3).

The password must still be run through `rclone obscure` once, locally,
before it goes into `.env` — this isn't encryption (it's reversible with
the same rclone binary), but it does mean the raw plaintext password
never appears in the config, in logs, or in the Arcane UI's env var
listing:

```bash
rclone obscure 'your-actual-hetzner-storagebox-password'
```

The resulting values go into `.env` (Section 4.2) as
`HETZNER_STORAGEBOX_USER` and `HETZNER_STORAGEBOX_PASS_OBSCURED`.

### 4.2 Environment Variables (`./.env`, project-relative)
```env
# --- Core (Section 4.2) ---
PUID=1000
PGID=1000
TZ=Europe/Amsterdam
STORAGE_PATH=/mnt/remote-media

# --- Arcane API (Section 9.2) ---
ARCANE_URL=https://arcane.example.com/api
ARCANE_API_TOKEN=
ARCANE_ENVIRONMENT_NAME=
ARCANE_PROJECT_NAME=arr-stack

# --- Hetzner Storage Box / rclone (Section 4.1) ---
# Generate HETZNER_STORAGEBOX_PASS_OBSCURED with: rclone obscure '<password>'
HETZNER_STORAGEBOX_USER=
HETZNER_STORAGEBOX_PASS_OBSCURED=
# rclone's --rc HTTP API (Section 10.1) — internal-network-only, used by the
# Homepage Storage Box widget to query used/free/total space. Any random
# credential works (rclone just needs SOME auth here, never --rc-no-auth,
# since a shell-equivalent API must not be exposed unauthenticated even on
# an internal network). Generate with: openssl rand -base64 24 | tr -d '=+/' | cut -c1-24
RCLONE_RC_USER=
RCLONE_RC_PASS=

# --- Tailscale ---
TS_AUTHKEY=

# --- AirVPN / Gluetun ---
VPN_SERVICE_PROVIDER=airvpn
VPN_TYPE=wireguard
WIREGUARD_PRIVATE_KEY=
WIREGUARD_PRESHARED_KEY=
WIREGUARD_ADDRESSES=
SERVER_COUNTRIES=Netherlands,Switzerland
AIRVPN_FORWARDED_PORT=

# --- Soulseek (slskd) ---
# Web UI login (slskd dashboard access)
SLSKD_USERNAME=
SLSKD_PASSWORD=
SLSKD_API_KEY=
# Soulseek network login (P2P network connection)
SLSKD_SLSK_USERNAME=
SLSKD_SLSK_PASSWORD=

# --- Lidarr / soularr ---
# LIDARR_API_KEY is found in Lidarr's Settings -> General, after first
# startup. Used to generate config/soularr/config.ini during bootstrap.
LIDARR_API_KEY=

# --- Jellyfin ---
JELLYFIN_PUBLISHED_SERVER_URL=http://arr-vps:8096

# --- Homepage dashboard widget API keys ---
# Each value is the target app's own API key, found in that app's
# Settings/General page (Prowlarr, Radarr, Sonarr, Lidarr) or
# Settings/Notifications->API key (Seerr), or slskd's SLSKD_API_KEY value,
# or Jellyfin's API key (Dashboard -> API Keys).
# Navidrome is intentionally not included: its Homepage widget requires
# manual Subsonic-style token/salt setup rather than a simple API key
# (see config/homepage/services.yaml for details).
HOMEPAGE_VAR_PROWLARR_KEY=
HOMEPAGE_VAR_RADARR_KEY=
HOMEPAGE_VAR_SONARR_KEY=
HOMEPAGE_VAR_BAZARR_KEY=
HOMEPAGE_VAR_SEERR_KEY=
HOMEPAGE_VAR_LIDARR_KEY=
HOMEPAGE_VAR_SLSKD_KEY=
HOMEPAGE_VAR_JELLYFIN_KEY=
```

### 4.3 Principle: Per-Service Environment Scoping

`.env` is the single local source of truth for every secret in this
stack, but no service ever consumes it wholesale via Compose's
`env_file:` directive — every service in Section 5 declares an explicit
`environment:` list naming only the variables it actually needs (Compose
still reads `.env` to resolve `${VAR}` placeholders in the compose file
at parse time, which is a different mechanism from injecting the file
into a container). Concretely: only `rclone-mount` sees
`HETZNER_STORAGEBOX_*` and `RCLONE_CONFIG_*`; only `gluetun` sees the
WireGuard keys; only `slskd`/`soularr` see `SLSKD_*`. A compromised or
misbehaving container is limited to the credentials it was explicitly
given, not the entire secret set. This applies when scaffolding every
service, including any added later.

---

## 5. Production Docker Compose Specification

Save as `docker-compose.yml` at the project root:

```yaml
version: "3.8"

services:
  bootstrap:
    image: alpine:latest
    container_name: arr-bootstrap
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
      - HETZNER_STORAGEBOX_USER=${HETZNER_STORAGEBOX_USER}
      - HETZNER_STORAGEBOX_PASS_OBSCURED=${HETZNER_STORAGEBOX_PASS_OBSCURED}
      - LIDARR_API_KEY=${LIDARR_API_KEY}
      - SLSKD_API_KEY=${SLSKD_API_KEY}
    volumes:
      - .:/workspace
      - /mnt/remote-media:/mnt/remote-media
    # NOTE: bootstrap/init.sh is fetched from GitHub at container start rather
    # than being embedded here, so this file and the script never drift out
    # of sync (see commit history: this replaced an earlier approach that
    # inlined the script's full contents into `command:`).
    #
    # Operational tradeoff: this makes bootstrap depend on the repo being
    # pushed to GitHub's `main` branch. A local edit to bootstrap/init.sh will
    # NOT take effect on the next deploy until it is pushed to `main` —
    # Arcane's deploy API only provisions docker-compose.yml and .env on the
    # host, it does not clone the rest of the repo.
    command: ["sh", "-c", "wget -qO /tmp/init.sh https://raw.githubusercontent.com/mauodias/arrstack/main/bootstrap/init.sh && sh /tmp/init.sh"]
    restart: "no"

  rclone-mount:
    image: rclone/rclone:latest
    container_name: arr-rclone
    entrypoint: [""]
    cap_add:
      - SYS_ADMIN
    devices:
      - /dev/fuse:/dev/fuse
    security_opt:
      - apparmor:unconfined
    environment:
      - RCLONE_CONFIG_HETZNER_BOX_TYPE=webdav
      - RCLONE_CONFIG_HETZNER_BOX_URL=https://${HETZNER_STORAGEBOX_USER}.your-storagebox.de
      - RCLONE_CONFIG_HETZNER_BOX_VENDOR=other
      - RCLONE_CONFIG_HETZNER_BOX_USER=${HETZNER_STORAGEBOX_USER}
      - RCLONE_CONFIG_HETZNER_BOX_PASS=${HETZNER_STORAGEBOX_PASS_OBSCURED}
      - RCLONE_RC_USER=${RCLONE_RC_USER}
      - RCLONE_RC_PASS=${RCLONE_RC_PASS}
    volumes:
      - ./data/rclone-cache:/cache
      - /mnt/remote-media:/data:shared
    depends_on:
      bootstrap:
        condition: service_completed_successfully
    command: ["sh", "-c", "rclone mkdir hetzner_box:movies; rclone mkdir hetzner_box:tv; rclone mkdir hetzner_box:music; rclone mkdir hetzner_box:downloads; if grep -q ' /data fuse' /proc/mounts 2>/dev/null; then fusermount -uz /data 2>/dev/null || umount -l /data 2>/dev/null || true; fi; exec rclone mount hetzner_box: /data --allow-non-empty --allow-other --rc --rc-addr :5572 --cache-dir /cache --dir-cache-time 1000h --attr-timeout 1s --vfs-cache-mode full --vfs-cache-max-age 24h --vfs-cache-max-size 100G --vfs-read-chunk-size 64M --vfs-read-chunk-size-limit 1G --buffer-size 32M --umask 002"]
    healthcheck:
      test: ["CMD-SHELL", "ls /data > /dev/null || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 3
    restart: unless-stopped

  tailscale:
    image: tailscale/tailscale:latest
    container_name: arr-tailscale
    hostname: arr-vps
    environment:
      - TS_AUTHKEY=${TS_AUTHKEY}
      - TS_STATE_DIR=/var/lib/tailscale
      - TS_USERSPACE=false
      - TS_EXTRA_ARGS=--advertise-routes=172.28.0.0/24
    volumes:
      - ./config/tailscale:/var/lib/tailscale
    cap_add:
      - NET_ADMIN
      - SYS_MODULE
    sysctls:
      - net.ipv4.ip_forward=1
      - net.ipv6.conf.all.forwarding=1
    devices:
      - /dev/net/tun:/dev/net/tun
    networks:
      default: {}
      vpn_net: {}
    depends_on:
      bootstrap:
        condition: service_completed_successfully
    restart: unless-stopped

  gluetun:
    image: qmcgaw/gluetun:latest
    container_name: arr-gluetun
    cap_add:
      - NET_ADMIN
    devices:
      - /dev/net/tun:/dev/net/tun
    networks:
      vpn_net:
        ipv4_address: 172.28.0.10
    environment:
      - VPN_SERVICE_PROVIDER=${VPN_SERVICE_PROVIDER}
      - VPN_TYPE=${VPN_TYPE}
      - WIREGUARD_PRIVATE_KEY=${WIREGUARD_PRIVATE_KEY}
      - WIREGUARD_PRESHARED_KEY=${WIREGUARD_PRESHARED_KEY}
      - WIREGUARD_ADDRESSES=${WIREGUARD_ADDRESSES}
      - SERVER_COUNTRIES=${SERVER_COUNTRIES}
      - FIREWALL_VPN_INPUT_PORTS=${AIRVPN_FORWARDED_PORT}
      - FIREWALL_OUTBOUND_SUBNETS=100.64.0.0/10
      # Allow inbound traffic to qBittorrent WebUI via Tailscale subnet route
      - FIREWALL_INPUT_PORTS=8080
    restart: unless-stopped

  qbittorrent:
    image: lscr.io/linuxserver/qbittorrent:latest
    container_name: arr-qbittorrent
    network_mode: "service:gluetun"
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
      - TZ=${TZ}
      - WEBUI_PORT=8080
    volumes:
      - ./config/qbittorrent:/config
      - /mnt/remote-media/downloads:/downloads:rslave
    depends_on:
      gluetun:
        condition: service_started
      rclone-mount:
        condition: service_healthy
    restart: unless-stopped

  prowlarr:
    image: lscr.io/linuxserver/prowlarr:latest
    container_name: arr-prowlarr
    network_mode: "service:tailscale"
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
      - TZ=${TZ}
    volumes:
      - ./config/prowlarr:/config
    depends_on:
      tailscale:
        condition: service_started
    restart: unless-stopped

  flaresolverr:
    image: ghcr.io/flaresolverr/flaresolverr:latest
    container_name: arr-flaresolverr
    network_mode: "service:tailscale"
    environment:
      - LOG_LEVEL=info
      - TZ=${TZ}
    depends_on:
      tailscale:
        condition: service_started
    restart: unless-stopped

  radarr:
    image: lscr.io/linuxserver/radarr:latest
    container_name: arr-radarr
    network_mode: "service:tailscale"
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
      - TZ=${TZ}
    volumes:
      - ./config/radarr:/config
      - /mnt/remote-media/movies:/movies:rslave
      - /mnt/remote-media/downloads:/downloads:rslave
    depends_on:
      rclone-mount:
        condition: service_healthy
      tailscale:
        condition: service_started
    restart: unless-stopped

  sonarr:
    image: lscr.io/linuxserver/sonarr:latest
    container_name: arr-sonarr
    network_mode: "service:tailscale"
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
      - TZ=${TZ}
    volumes:
      - ./config/sonarr:/config
      - /mnt/remote-media/tv:/tv:rslave
      - /mnt/remote-media/downloads:/downloads:rslave
    depends_on:
      rclone-mount:
        condition: service_healthy
      tailscale:
        condition: service_started
    restart: unless-stopped

  bazarr:
    image: lscr.io/linuxserver/bazarr:latest
    container_name: arr-bazarr
    network_mode: "service:tailscale"
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
      - TZ=${TZ}
    volumes:
      - ./config/bazarr:/config
      - /mnt/remote-media/movies:/movies:rslave
      - /mnt/remote-media/tv:/tv:rslave
    depends_on:
      rclone-mount:
        condition: service_healthy
      radarr:
        condition: service_started
      sonarr:
        condition: service_started
      tailscale:
        condition: service_started
    restart: unless-stopped

  seerr:
    image: ghcr.io/seerr-team/seerr:latest
    container_name: arr-seerr
    network_mode: "service:tailscale"
    environment:
      - TZ=${TZ}
    volumes:
      - ./config/seerr:/app/config
    depends_on:
      radarr:
        condition: service_started
      sonarr:
        condition: service_started
      tailscale:
        condition: service_started
    restart: unless-stopped

  lidarr:
    image: lscr.io/linuxserver/lidarr:latest
    container_name: arr-lidarr
    network_mode: "service:tailscale"
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
      - TZ=${TZ}
    volumes:
      - ./config/lidarr:/config
      - /mnt/remote-media/music:/music:rslave
      - /mnt/remote-media/downloads:/downloads:rslave
    depends_on:
      rclone-mount:
        condition: service_healthy
      tailscale:
        condition: service_started
    restart: unless-stopped

  slskd:
    image: slskd/slskd:latest
    container_name: arr-slskd
    network_mode: "service:tailscale"
    environment:
      - TZ=${TZ}
      # Web UI login (slskd dashboard)
      - SLSKD_USERNAME=${SLSKD_USERNAME}
      - SLSKD_PASSWORD=${SLSKD_PASSWORD}
      - SLSKD_API_KEY=${SLSKD_API_KEY}
      # Soulseek network login (P2P network connection)
      - SLSKD_SLSK_USERNAME=${SLSKD_SLSK_USERNAME}
      - SLSKD_SLSK_PASSWORD=${SLSKD_SLSK_PASSWORD}
      # slskd's config precedence is:
      #   defaults < environment variables < slskd.yml < command line
      # Environment variables are WEAKER than the YAML file, which is the
      # reverse of most apps. Remote configuration lets the Web UI write
      # slskd.yml, so leaving it on means a stray click in the UI silently
      # outranks everything declared below and the drift is invisible from
      # git. Keeping it off makes this file the single source of truth,
      # consistent with every other service in this stack.
      - SLSKD_REMOTE_CONFIGURATION=false
      # Completed downloads land on the Storage Box at the path Lidarr and
      # soularr both expect (soularr's config.ini download_dir must match).
      # Without this, slskd falls back to APP_DIR/downloads — i.e. inside
      # the ./config/slskd bind mount on local VPS disk, where soularr and
      # Lidarr never look.
      - SLSKD_DOWNLOADS_DIR=/downloads
      # Partial files stay on local disk deliberately: they are written as
      # many small appends, which is pathological over WebDAV/FUSE. Only the
      # finished file crosses to the Storage Box, in one sequential pass.
      - SLSKD_INCOMPLETE_DIR=/app/incomplete
      # Share the music library back to the Soulseek network. Because sharing
      # is directory-based (not per-transfer like BitTorrent seeding), albums
      # Lidarr imports into /music become shared automatically.
      - SLSKD_SHARED_DIR=/music
      - SLSKD_UPLOAD_SLOTS=20
      # Kibibytes per second: 20480 KiB/s = 20 MiB/s.
      - SLSKD_UPLOAD_SPEED_LIMIT=20480
      # The share index defaults to RAM; keep it on disk since the library is
      # large and sits behind a FUSE mount.
      - SLSKD_SHARE_CACHE_STORAGE_MODE=Disk
    volumes:
      - ./config/slskd:/app
      - /mnt/remote-media/downloads:/downloads:rslave
      # Read-only: slskd only ever serves uploads from here. Lidarr owns
      # writing to /music.
      - /mnt/remote-media/music:/music:ro,rslave
    depends_on:
      rclone-mount:
        condition: service_healthy
      tailscale:
        condition: service_started
    restart: unless-stopped

  soularr:
    image: mrusse08/soularr:latest
    container_name: arr-soularr
    network_mode: "service:tailscale"
    environment:
      - TZ=${TZ}
      - SCRIPT_INTERVAL=300
    volumes:
      - ./config/soularr:/data
      - /mnt/remote-media/downloads:/downloads:rslave
    depends_on:
      lidarr:
        condition: service_started
      slskd:
        condition: service_started
      tailscale:
        condition: service_started
    restart: unless-stopped

  navidrome:
    image: deluan/navidrome:latest
    container_name: arr-navidrome
    network_mode: "service:tailscale"
    environment:
      - ND_LOGLEVEL=info
      - TZ=${TZ}
    volumes:
      - ./config/navidrome:/data
      - /mnt/remote-media/music:/music:rslave
    depends_on:
      rclone-mount:
        condition: service_healthy
      tailscale:
        condition: service_started
    restart: unless-stopped

  jellyfin:
    image: lscr.io/linuxserver/jellyfin:latest
    container_name: arr-jellyfin
    network_mode: "service:tailscale"
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
      - TZ=${TZ}
      - JELLYFIN_PublishedServerUrl=${JELLYFIN_PUBLISHED_SERVER_URL}
    volumes:
      - ./config/jellyfin:/config
      - /mnt/remote-media/movies:/movies:rslave
      - /mnt/remote-media/tv:/tv:rslave
      - /mnt/remote-media/music:/music:rslave
    depends_on:
      rclone-mount:
        condition: service_healthy
      tailscale:
        condition: service_started
    restart: unless-stopped

  homepage:
    image: ghcr.io/gethomepage/homepage:latest
    container_name: arr-homepage
    network_mode: "service:tailscale"
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
      - TZ=${TZ}
      - HOMEPAGE_ALLOWED_HOSTS=arr-vps:3000
      - HOMEPAGE_VAR_PROWLARR_KEY=${HOMEPAGE_VAR_PROWLARR_KEY}
      - HOMEPAGE_VAR_RADARR_KEY=${HOMEPAGE_VAR_RADARR_KEY}
      - HOMEPAGE_VAR_SONARR_KEY=${HOMEPAGE_VAR_SONARR_KEY}
      - HOMEPAGE_VAR_BAZARR_KEY=${HOMEPAGE_VAR_BAZARR_KEY}
      - HOMEPAGE_VAR_RCLONE_RC_USER=${RCLONE_RC_USER}
      - HOMEPAGE_VAR_RCLONE_RC_PASS=${RCLONE_RC_PASS}
      - HOMEPAGE_VAR_SEERR_KEY=${HOMEPAGE_VAR_SEERR_KEY}
      - HOMEPAGE_VAR_LIDARR_KEY=${HOMEPAGE_VAR_LIDARR_KEY}
      - HOMEPAGE_VAR_SLSKD_KEY=${HOMEPAGE_VAR_SLSKD_KEY}
      - HOMEPAGE_VAR_JELLYFIN_KEY=${HOMEPAGE_VAR_JELLYFIN_KEY}
    volumes:
      - ./config/homepage:/app/config
      - /var/run/docker.sock:/var/run/docker.sock:ro
    depends_on:
      tailscale:
        condition: service_started
    restart: unless-stopped

networks:
  vpn_net:
    driver: bridge
    ipam:
      config:
        - subnet: 172.28.0.0/24
```

---

## 6. Execution Workflow for Autonomous Agent

### Step 1: Host Inspection
* Verify presence of `/dev/fuse` and `/dev/net/tun`.
* Ensure host directory `/mnt/remote-media` exists.
* Issue `mount --make-rshared /mnt` to ensure propagation works.

### Step 2: Secret & Credentials Provisioning
* Populate `./.env` with `HETZNER_STORAGEBOX_USER`/`HETZNER_STORAGEBOX_PASS_OBSCURED`
  (Section 4.1), `TS_AUTHKEY`, AirVPN WireGuard credentials, and the assigned
  `AIRVPN_FORWARDED_PORT`.

### Step 3: Initialization & Startup
1. Launch `rclone-mount` first and verify health:
   ```bash
   docker compose up -d rclone-mount
   docker compose ps rclone-mount
   ```
2. Launch `gluetun` and verify AirVPN tunnel status:
   ```bash
   docker compose up -d gluetun
   docker logs arr-gluetun
   ```
3. Bring up the full application stack:
   ```bash
   docker compose up -d
   ```

---

## 7. Verification & Post-Deployment Checklist

1. **Egress IP Leak Test (CRITICAL):**
   ```bash
   docker exec -it arr-qbittorrent curl [https://ifconfig.me](https://ifconfig.me)
   ```
   Verify the returned IP matches AirVPN and **NOT** Strato's German host IP.
2. **qBittorrent Connection Port Setup:**
   * Access qBittorrent Web UI (`http://arr-vps:8080`).
   * Go to **Tools -> Options -> Connection**.
   * Set **Port used for incoming connections** to `${AIRVPN_FORWARDED_PORT}`.
   * Disable UPnP / NAT-PMP.
3. **App-to-App Interconnection:**
   * Radarr/Sonarr/Lidarr to Prowlarr: `http://127.0.0.1:9696`
   * Prowlarr to FlareSolverr: `http://127.0.0.1:8191` (proxy for Cloudflare-protected indexers)
   * Radarr/Sonarr/Lidarr to qBittorrent: `http://172.28.0.10:8080` (gluetun's
     static `vpn_net` IP — reachable directly since these apps share the
     Tailscale sidecar's netns, which is multi-homed onto `vpn_net`, per the
     qBittorrent-reachability note in Section 2; **not** `arr-vps:8080`,
     which is for external/browser access via the Tailscale subnet route,
     not container-to-container). The download client field takes
     qBittorrent's WebUI username/password — there is no API-key option.
   * Seerr to Radarr/Sonarr: `http://127.0.0.1:7878` / `http://127.0.0.1:8989`
     (all share the Tailscale sidecar's network namespace, so app-to-app
     calls use `127.0.0.1` with the target app's port, not container names)
   * Soularr to Lidarr: `http://127.0.0.1:8686`; to slskd: `http://127.0.0.1:5030`
   * **Music acquisition flow.** slskd is a download client only — it never
     moves files into the library, exactly like qBittorrent. Lidarr has no
     native slskd support, so soularr bridges them: it reads Lidarr's wanted
     list, searches and starts downloads in slskd, then asks Lidarr to import
     the result. slskd writes completed files to `/downloads` and Lidarr moves
     and renames them into `/music`. All three containers bind the same
     `/mnt/remote-media/downloads` at `/downloads`, which is what makes the
     handoff work; `SLSKD_DOWNLOADS_DIR` and both `download_dir` keys in
     `config/soularr/config.ini` must agree on that path. (slskd's default is
     `APP_DIR/downloads`, which inside the container resolves into the
     `./config/slskd` bind mount on local disk, where neither soularr nor
     Lidarr looks — hence the explicit override.)
   * **Sharing back.** slskd shares `/music` read-only
     (`SLSKD_SHARED_DIR`, mounted `ro`). Soulseek sharing is directory-based
     rather than per-transfer, so an album Lidarr imports into `/music` is
     shared automatically — moving a file out of the download directory adds
     it to the share instead of breaking it, unlike BitTorrent seeding.
     Uploads are read back through the rclone VFS cache, so heavily-requested
     albums occupy cache space (Section 5's `--vfs-cache-max-size`).
   * **slskd configuration is declared, not clicked.** Its precedence is
     `defaults < environment variables < slskd.yml < command line` — env vars
     are *weaker* than the YAML file, the reverse of most applications. With
     remote configuration enabled the Web UI writes `slskd.yml`, so any UI
     edit silently outranks `docker-compose.yml` and leaves no trace in git.
     `SLSKD_REMOTE_CONFIGURATION=false` keeps compose authoritative,
     consistent with every other service here. The consequence is that a host
     which previously ran with it enabled retains a `slskd.yml` that must be
     removed before the environment variables take effect.
   * Jellyfin/Navidrome read directly from `/mnt/remote-media/*`; no API
     linkage to Radarr/Sonarr/Lidarr is required for playback, only for
     Seerr's "available" status if configured

---

## 8. Future Migration Plan (Cloud VPS -> Home Server)

1. Sync `./config/*` SQLite data directories via `rsync`.
2. Remove `rclone-mount` if local arrays replace the cloud mount.
3. Repoint compose volumes to local disk pools.
4. Execute `docker compose up -d` on the local machine.

### 8.1 Future Component: Books (Readarr + Calibre-Web)

Out of scope for the initial PoC (Section 1 priorities are music-first),
but noted for later: **Readarr** would slot in alongside Lidarr/Radarr/
Sonarr (same Servarr acquisition model — Prowlarr indexers, qBittorrent
grabs, organized into `/mnt/remote-media/books`), paired with
**Calibre-Web** (or Calibre-Web-Automated) as the consumption/management
layer, since Readarr only acquires and organizes — it doesn't do format
conversion, metadata editing, or reading-state tracking the way Calibre
does. The existing local Calibre library (with its reading state — shelves,
progress, ratings) would need a one-time migration of the Calibre library
database onto the rclone mount so Calibre-Web can serve it remotely
going forward, rather than an ongoing two-way sync with the local
Calibre install.


---

## 9. GitOps Deployment via Arcane API

Rather than manually recreating this stack through Arcane's web UI, the
stack definition is version-controlled in this repository and pushed to
the running Arcane instance on the VPS through Arcane's REST API. This
keeps the VPS deployment state reproducible from git history and avoids
configuration drift between what's committed and what's actually running.

### 9.1 Repository Layout

`.env` is the only file carrying secrets — per Section 4.1, the Hetzner
remote is defined via environment variables, so there's no second
secrets-bearing config file to template or gitignore separately:

```
.
├── SPEC.md
├── docker-compose.yml
├── .env.example              # template; real .env stays local, gitignored
└── deploy.py                 # Arcane API deploy script
```

Per-app config directories (`config/prowlarr`, `config/radarr`,
`config/seerr`, `config/lidarr`, `config/slskd`, `config/navidrome`,
`config/jellyfin`, etc., per Section 3.2) are runtime state created by the
`bootstrap` container (Section 11), not templated in git.

`.env` (containing the Hetzner Storage Box credentials, Tailscale
authkey, AirVPN WireGuard keys, and the Arcane API token itself —
`ARCANE_URL`/`ARCANE_API_TOKEN` — per Sections 4.1, 4.2, and 9.2) is
created locally from `.env.example` and is covered by `.gitignore`. It
never enters version control.

### 9.2 Deploy Script (`deploy.py`)

A single Python script, run with `uv run deploy.py`, using PEP 723 inline
script metadata to declare its own (minimal) dependencies so no separate
virtualenv or requirements file is needed. It performs a **push-and-apply**
of the compose stack only — it does not perform host-level pre-flight
(kernel modules, directory creation, mount propagation from Section 3),
which remains a manual, documented one-time step run over SSH, since it
sits on a different trust boundary (host shell access) than the Arcane
API (application-level deploy access).

Responsibilities:

1. Load local `.env` for `ARCANE_URL`, `ARCANE_API_TOKEN`, and the stack's
   runtime environment variables (Section 4.2).
2. Authenticate to the Arcane API.
3. Look up the target project/stack (e.g. `arr-stack`) by name.
   - If it exists, update its compose definition and environment.
   - If it doesn't exist, create it.
4. Push the contents of `docker-compose.yml` and the resolved environment
   variables as that project's configuration.
5. Trigger a redeploy (pull images, recreate changed containers).
6. Poll and print the resulting project/container status so the operator
   gets immediate confirmation the deploy succeeded (or a clear failure).

The script is idempotent: running it repeatedly against an unchanged repo
state updates the existing project in place rather than erroring or
creating duplicates.

### 9.3 Out of Scope

* Host pre-flight checks (Section 3) — manual/SSH, not automated here.
* Arcane installation/bootstrapping itself — assumed already running.
* Secret rotation/generation — operator populates local `.env` by hand
  from `.env.example` (including running `rclone obscure`, Section 4.1).

---

## 10. Storage & Health Dashboard (Homepage)

A single-pane-of-glass dashboard is included so status (storage, queues,
now-playing) doesn't require opening each app individually — useful once
the stack has multiple end users (Section 10.3).

### 10.1 What It Shows

Homepage (`gethomepage/homepage`) is configured with widgets pulling
directly from each app's API:

* **Storage:** actual Hetzner Storage Box used/free/total, via a Homepage
  `customapi` widget (`config/homepage/services.yaml`) that calls
  `rclone-mount`'s own `--rc` HTTP API (`--rc --rc-addr :5572`, Section 5)
  at its internal Docker DNS name `http://arr-rclone:5572/operations/about`
  — reachable only within the internal Docker network (no published port),
  authenticated with `RCLONE_RC_USER`/`RCLONE_RC_PASS` (Section 4.2; rclone's
  rc API is explicitly documented as shell-equivalent access, so it's never
  run with `--rc-no-auth`, even though it's already internal-network-only).
  Homepage passes those same credentials through as
  `HOMEPAGE_VAR_RCLONE_RC_USER`/`HOMEPAGE_VAR_RCLONE_RC_PASS`. This shows
  remote capacity only. Local VPS disk usage for the `--vfs-cache`
  directory is deliberately NOT shown: the cache can independently fill up
  and break writes even while the Storage Box has room, but putting both
  numbers on one dashboard reads as two contradictory "storage" figures.
  If it's ever wanted, Homepage's `resources` widget takes a `disk:` path
  and would need that path bind-mounted into the homepage container.
* **Queues/activity:** Radarr, Sonarr, Lidarr wanted/queue counts;
  Prowlarr indexer health; qBittorrent active torrents and ratio; slskd
  active transfers.
* **Now playing:** Jellyfin and Navidrome current sessions.
* **Requests:** Seerr pending request count.
* **soularr:** plain link only (no widget — it has no stats API), for
  quick access to its minimal UI at `:8265`, which shows the generated
  `config.ini`.

### 10.2 Placement

Runs as a 7th Tailscale-network application (Section 5, alongside the
other consumer apps) — no public exposure, consistent with Section 1.1.
It needs read-only access to the Docker socket (`/var/run/docker.sock`)
for container status widgets. Its three config files — `services.yaml` (tiles/widgets),
`settings.yaml` (theme, group order, per-group icons and columns) and
`widgets.yaml` (the clock/weather/resources/search header row) — are
human-authored and committed to git, unlike every other `config/*`
directory, and are fetched from GitHub by `bootstrap` on each deploy
(Section 11.1). Group names in `services.yaml` must match the `layout:`
keys in `settings.yaml` exactly or the group loses its icon/column
settings. Per-app API keys are injected as `HOMEPAGE_VAR_*` environment
variables (Section 4.2), never committed.

---

## 11. Host Bootstrap: Init Container + One-Time Setup Script

Deploying via the Arcane API (Section 9) avoids SSH for day-to-day
updates, but two of the pre-flight steps in Section 3.1 act on the host
kernel/mount namespace (`modprobe fuse`/`modprobe tun`, `mount
--make-rshared /mnt`) and genuinely require host-root privilege — running
them from a container means granting that container the same effective
power as an SSH root session, so there's no privilege-reducing way to
fully avoid SSH for this part. The goal instead is to make the one
unavoidable SSH visit as small and repeatable as possible.

### 11.1 Split of Responsibilities

* **`bootstrap` init container** (added to `docker-compose.yml`,
  deployed via Arcane like everything else): a minimal Alpine-based
  container with a bind mount to the project root and `/mnt/remote-media`
  (regular volume access, no special host privilege). On start it
  idempotently:
  * creates any missing `config/*` subdirectories from Section 3.2,
  * sets ownership to `PUID:PGID`,
  * verifies `.env` exists and defines the required
    `HETZNER_STORAGEBOX_*` keys (Section 4.1) — fails/reports unhealthy
    if not, since these must be populated locally first per Section 9.1,
  * exits 0 once the tree matches expectations.
  Note that `bootstrap` runs and completes *before* `rclone-mount` even
  starts (Section 5), so it deliberately does **not** create
  `/mnt/remote-media/*` subdirectories — anything created there pre-mount
  would only exist on the local disk underneath the future mount point
  and gets shadowed the instant `rclone-mount` FUSE-mounts the remote on
  top of it. That job belongs to `rclone-mount` itself (Section 5): as
  the first steps of its own startup command, before it mounts, it runs
  `rclone mkdir hetzner_box:<dir>` for each of the four directories —
  a plain WebDAV call over HTTP that talks directly to the remote using
  credentials the container already has, with no FUSE mount or
  cross-container propagation involved. Other services depend on
  `bootstrap` via `condition: service_completed_successfully` for
  config/permissions, and on `rclone-mount` via
  `condition: service_healthy` for the media tree — sufficient on its
  own now, since the directories are created before the mount (and thus
  the healthcheck) can succeed — so a fresh Arcane deploy self-heals
  both without any manual step.
* **`setup-host.sh`** (committed to this public repo, fetched via its
  raw GitHub URL): the two host-kernel steps from Section 3.1 that the
  init container structurally cannot perform. Since the repo has no
  secrets in it (Section 9.1), the script is safe to fetch
  unauthenticated:
  ```bash
  curl -fsSL https://raw.githubusercontent.com/<you>/arrstack/main/setup-host.sh -o setup-host.sh
  less setup-host.sh   # review before running anything fetched over the network
  sudo bash setup-host.sh
  ```
  It loads the `fuse`/`tun` kernel modules, runs `mount --make-rshared
  /mnt`, and installs a systemd unit (or `/etc/fstab`-equivalent) so the
  propagation survives a reboot (Section 3.1's persistence note).

### 11.2 Operator Flow

1. Populate local `.env` (Section 9.1), including `rclone obscure`'d
   Hetzner credentials (Section 4.1).
2. Run `deploy.py` (Section 9.2) — this brings up `bootstrap` first,
   which creates/repairs the directory tree, then the rest of the stack.
3. **Once ever** (or after a reboot, if `setup-host.sh`'s systemd unit
   wasn't installed or the VPS is rebuilt): SSH in and run
   `setup-host.sh` as shown above.

No separate script-publishing pipeline (e.g. a GitHub Action) is needed
— the repo being public means its raw URL is already a stable,
always-current source for the script.

### 10.3 Multi-User Note

The intended eventual use includes non-technical family members (e.g.
requesting content via Seerr) rather than just single-operator use. This
doesn't change the architecture in this PoC — Seerr's own user/approval
system (Section 1) already covers request access without exposing
Radarr/Sonarr directly — but it's the reason Homepage's dashboard view is
scoped to you as the operator (via your own Tailscale device), not shared
as a general-access page.