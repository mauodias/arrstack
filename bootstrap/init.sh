#!/bin/sh
set -eu

WORKSPACE="${WORKSPACE:-/workspace}"
MEDIA_ROOT="${MEDIA_ROOT:-/mnt/remote-media}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

CONFIG_DIRS="rclone tailscale prowlarr radarr sonarr qbittorrent gluetun seerr lidarr slskd soularr navidrome jellyfin homepage"

# services.yaml is a git-committed, human-authored exception to the usual
# rule that config/* is runtime state never committed to the repo. It is
# fetched from GitHub here (same pattern docker-compose.yml uses to fetch
# this very script) so operator edits actually reach the host on redeploy.
SERVICES_YAML_URL="${SERVICES_YAML_URL:-https://raw.githubusercontent.com/mauodias/arrstack/main/config/homepage/services.yaml}"

# soularr has no web UI, so its config.ini (with real API keys) can't be
# hand-authored through a UI and can't be committed as-is (public repo). The
# template below is committed (placeholders only, no secrets), fetched from
# GitHub the same way services.yaml is, then the placeholders are substituted
# with real values from the environment to produce the real config.ini.
SOULARR_CONFIG_TEMPLATE_URL="${SOULARR_CONFIG_TEMPLATE_URL:-https://raw.githubusercontent.com/mauodias/arrstack/main/config/soularr/config.ini.template}"

if [ -z "${HETZNER_STORAGEBOX_USER:-}" ] || [ -z "${HETZNER_STORAGEBOX_PASS_OBSCURED:-}" ]; then
    echo "ERROR: HETZNER_STORAGEBOX_USER and HETZNER_STORAGEBOX_PASS_OBSCURED must be set (populate .env from .env.example first)." >&2
    exit 1
fi

if [ -z "${LIDARR_API_KEY:-}" ] || [ -z "${SLSKD_API_KEY:-}" ]; then
    echo "ERROR: LIDARR_API_KEY and SLSKD_API_KEY must be set (populate .env from .env.example first)." >&2
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

if ! wget -qO "$WORKSPACE/config/soularr/config.ini" "$SOULARR_CONFIG_TEMPLATE_URL"; then
    echo "ERROR: failed to fetch config.ini.template from $SOULARR_CONFIG_TEMPLATE_URL" >&2
    exit 1
fi
sed "s|__LIDARR_API_KEY__|$LIDARR_API_KEY|g; s|__SLSKD_API_KEY__|$SLSKD_API_KEY|g" "$WORKSPACE/config/soularr/config.ini" > "$WORKSPACE/config/soularr/config.ini.tmp"
mv "$WORKSPACE/config/soularr/config.ini.tmp" "$WORKSPACE/config/soularr/config.ini"
echo "Generated config/soularr/config.ini"

mkdir -p "$WORKSPACE/data/rclone-cache"

chown -R "${PUID}:${PGID}" "$WORKSPACE/config" "$WORKSPACE/data" "$MEDIA_ROOT"

echo "Bootstrap complete."
