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

echo "Test 5: fetches services.yaml from SERVICES_YAML_URL into config/homepage"
if command -v wget >/dev/null 2>&1; then
    # Real wget is available, but we can't hit the actual internet from a unit
    # test. Instead, put a fake `wget` shim earlier on PATH that records how
    # it was invoked and copies a local fixture into place, exercising
    # init.sh's real logic (URL used, destination path, ordering relative to
    # `mkdir -p config/homepage`) without any network access.
    FAKE_BIN_DIR="$(mktemp -d)"
    FIXTURE_DIR="$(mktemp -d)"
    WORKDIR2="$(mktemp -d)"
    trap 'rm -rf "$WORKDIR" "$MEDIA_DIR" "$FAKE_BIN_DIR" "$FIXTURE_DIR" "$WORKDIR2"' EXIT

    printf -- '- Test Group:\n    - Test Service:\n        href: http://example.test\n' > "$FIXTURE_DIR/services.yaml"
    FAKE_URL="https://example.test/services.yaml"

    cat > "$FAKE_BIN_DIR/wget" <<EOF
#!/bin/sh
# Fake wget for testing: init.sh always calls us as "-qO <dest> <url>".
# Log the call and copy the local fixture to <dest> as if the fetch succeeded.
echo "\$@" >> "$FIXTURE_DIR/wget.calls"
cp "$FIXTURE_DIR/services.yaml" "\$2"
EOF
    chmod +x "$FAKE_BIN_DIR/wget"

    PATH="$FAKE_BIN_DIR:$PATH" \
        WORKSPACE="$WORKDIR2" MEDIA_ROOT="$MEDIA_DIR" PUID="$REAL_UID" PGID="$REAL_GID" \
        HETZNER_STORAGEBOX_USER="someuser" \
        HETZNER_STORAGEBOX_PASS_OBSCURED="obscuredvalue" \
        SERVICES_YAML_URL="$FAKE_URL" \
        sh "$INIT_SCRIPT" || fail "expected success when fetching services.yaml via the fake wget shim"

    [ -f "$WORKDIR2/config/homepage/services.yaml" ] || fail "config/homepage/services.yaml was not written"
    cmp -s "$FIXTURE_DIR/services.yaml" "$WORKDIR2/config/homepage/services.yaml" \
        || fail "fetched services.yaml content does not match fixture"

    grep -q -- "-qO $WORKDIR2/config/homepage/services.yaml $FAKE_URL" "$FIXTURE_DIR/wget.calls" \
        || fail "wget was not invoked with the expected destination and URL (got: $(cat "$FIXTURE_DIR/wget.calls"))"

    echo "Test 5b: fetch failure fails the whole bootstrap loudly"
    cat > "$FAKE_BIN_DIR/wget" <<'EOF'
#!/bin/sh
exit 1
EOF
    chmod +x "$FAKE_BIN_DIR/wget"
    WORKDIR3="$(mktemp -d)"
    trap 'rm -rf "$WORKDIR" "$MEDIA_DIR" "$FAKE_BIN_DIR" "$FIXTURE_DIR" "$WORKDIR2" "$WORKDIR3"' EXIT
    if PATH="$FAKE_BIN_DIR:$PATH" \
        WORKSPACE="$WORKDIR3" MEDIA_ROOT="$MEDIA_DIR" PUID="$REAL_UID" PGID="$REAL_GID" \
        HETZNER_STORAGEBOX_USER="someuser" \
        HETZNER_STORAGEBOX_PASS_OBSCURED="obscuredvalue" \
        SERVICES_YAML_URL="$FAKE_URL" \
        sh "$INIT_SCRIPT" 2>/dev/null; then
        fail "expected non-zero exit when the services.yaml fetch fails"
    fi
    [ ! -d "$WORKDIR3/data" ] || fail "bootstrap should have aborted before creating data/ after a failed fetch"
else
    echo "wget not available in this environment; skipping services.yaml fetch test"
fi

echo "All bootstrap tests passed."
