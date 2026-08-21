#!/bin/sh
set -eu

WORKSPACE="${WORKSPACE:-/workspace}"
MEDIA_ROOT="${MEDIA_ROOT:-/mnt/remote-media}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

CONFIG_DIRS="rclone tailscale prowlarr radarr sonarr qbittorrent gluetun seerr lidarr slskd soularr navidrome jellyfin homepage"
MEDIA_DIRS="movies tv music downloads"

# services.yaml is a git-committed, human-authored exception to the usual
# rule that config/* is runtime state never committed to the repo. It is
# fetched from GitHub here (same pattern docker-compose.yml uses to fetch
# this very script) so operator edits actually reach the host on redeploy.
SERVICES_YAML_URL="${SERVICES_YAML_URL:-https://raw.githubusercontent.com/mauodias/arrstack/main/config/homepage/services.yaml}"

if [ -z "${HETZNER_STORAGEBOX_USER:-}" ] || [ -z "${HETZNER_STORAGEBOX_PASS_OBSCURED:-}" ]; then
    echo "ERROR: HETZNER_STORAGEBOX_USER and HETZNER_STORAGEBOX_PASS_OBSCURED must be set (populate .env from .env.example first)." >&2
    exit 1
fi

for dir in $CONFIG_DIRS; do
    mkdir -p "$WORKSPACE/config/$dir"
done

if ! wget -qO "$WORKSPACE/config/homepage/services.yaml" "$SERVICES_YAML_URL"; then
    echo "ERROR: failed to fetch services.yaml from $SERVICES_YAML_URL" >&2
    exit 1
fi
echo "Fetched config/homepage/services.yaml"

mkdir -p "$WORKSPACE/data/rclone-cache"

for dir in $MEDIA_DIRS; do
    mkdir -p "$MEDIA_ROOT/$dir"
done

chown -R "${PUID}:${PGID}" "$WORKSPACE/config" "$WORKSPACE/data" "$MEDIA_ROOT"

echo "Bootstrap complete."
