#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
INIT_SCRIPT="$SCRIPT_DIR/../../bootstrap/init.sh"

WORKDIR="$(mktemp -d)"
MEDIA_DIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR" "$MEDIA_DIR"' EXIT

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

REAL_UID="$(id -u)"
REAL_GID="$(id -g)"

echo "Test 1: fails when both Hetzner vars are unset"
if WORKSPACE="$WORKDIR" MEDIA_ROOT="$MEDIA_DIR" PUID="$REAL_UID" PGID="$REAL_GID" \
    env -u HETZNER_STORAGEBOX_USER -u HETZNER_STORAGEBOX_PASS_OBSCURED \
    sh "$INIT_SCRIPT" 2>/dev/null; then
    fail "expected non-zero exit when both Hetzner vars are unset"
fi

echo "Test 2: fails when only one Hetzner var is set"
if WORKSPACE="$WORKDIR" MEDIA_ROOT="$MEDIA_DIR" PUID="$REAL_UID" PGID="$REAL_GID" \
    HETZNER_STORAGEBOX_USER="someuser" \
    env -u HETZNER_STORAGEBOX_PASS_OBSCURED \
    sh "$INIT_SCRIPT" 2>/dev/null; then
    fail "expected non-zero exit when HETZNER_STORAGEBOX_PASS_OBSCURED is missing"
fi

echo "Test 3: succeeds and creates the directory tree when both Hetzner vars are set"
WORKSPACE="$WORKDIR" MEDIA_ROOT="$MEDIA_DIR" PUID="$REAL_UID" PGID="$REAL_GID" \
    HETZNER_STORAGEBOX_USER="someuser" \
    HETZNER_STORAGEBOX_PASS_OBSCURED="obscuredvalue" \
    sh "$INIT_SCRIPT" || fail "expected success when both Hetzner vars are set"

[ -d "$WORKDIR/config/rclone" ] || fail "config/rclone not created"
[ -d "$WORKDIR/config/jellyfin" ] || fail "config/jellyfin not created"
[ -d "$WORKDIR/config/homepage" ] || fail "config/homepage not created"
[ -d "$WORKDIR/data/rclone-cache" ] || fail "data/rclone-cache not created"
[ -d "$MEDIA_DIR/movies" ] || fail "movies dir not created"
[ -d "$MEDIA_DIR/music" ] || fail "music dir not created"
[ -d "$MEDIA_DIR/downloads" ] || fail "downloads dir not created"

echo "Test 4: idempotent (second run also succeeds cleanly)"
WORKSPACE="$WORKDIR" MEDIA_ROOT="$MEDIA_DIR" PUID="$REAL_UID" PGID="$REAL_GID" \
    HETZNER_STORAGEBOX_USER="someuser" \
    HETZNER_STORAGEBOX_PASS_OBSCURED="obscuredvalue" \
    sh "$INIT_SCRIPT" || fail "second run should also succeed"

echo "All bootstrap tests passed."
