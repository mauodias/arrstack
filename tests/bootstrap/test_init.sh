#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
INIT_SCRIPT="$SCRIPT_DIR/../../bootstrap/init.sh"

WORKDIR="$(mktemp -d)"
MEDIA_DIR="$(mktemp -d)"
FAKE_BIN_DIR="$(mktemp -d)"
FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR" "$MEDIA_DIR" "$FAKE_BIN_DIR" "$FIXTURE_DIR"' EXIT

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

REAL_UID="$(id -u)"
REAL_GID="$(id -g)"

# We can't hit the actual internet from a unit test. Instead, put a fake
# `wget` shim on PATH ahead of the real one that records how it was invoked and
# copies a local fixture into place, exercising init.sh's real logic (URL
# used, destination path, ordering relative to `mkdir -p`) without any
# network access. Every test below runs with this shim on PATH and with
# HOMEPAGE_CONFIG_BASE_URL / SOULARR_CONFIG_TEMPLATE_URL pointed at fixture URLs.
printf -- '- Test Group:\n    - Test Service:\n        href: http://example.test\n' > "$FIXTURE_DIR/services.yaml"
printf -- 'title: test\ntheme: dark\n' > "$FIXTURE_DIR/settings.yaml"
printf -- '- datetime:\n    text_size: xl\n' > "$FIXTURE_DIR/widgets.yaml"
FAKE_BASE_URL="https://example.test/homepage"
HOMEPAGE_FILES="services.yaml settings.yaml widgets.yaml"

printf -- '[Lidarr]\napi_key = __LIDARR_API_KEY__\nhost_url = http://127.0.0.1:8686\ndownload_dir = /downloads\n\n[Slskd]\napi_key = __SLSKD_API_KEY__\nhost_url = http://127.0.0.1:5030\nurl_base = /\ndownload_dir = /downloads\ndelete_searches = False\n' > "$FIXTURE_DIR/config.ini.template"
FAKE_SOULARR_URL="https://example.test/config.ini.template"

# Fake wget for testing: init.sh always calls us as "-qO <dest> <url>".
# Dispatch by URL so every fetch (the three Homepage config files and
# soularr's config.ini.template) is served by this one shim, and log each
# call.
cat > "$FAKE_BIN_DIR/wget" <<EOF
#!/bin/sh
echo "\$@" >> "$FIXTURE_DIR/wget.calls"
case "\$3" in
    "$FAKE_BASE_URL"/*) cp "$FIXTURE_DIR/\${3##*/}" "\$2" ;;
    "$FAKE_SOULARR_URL") cp "$FIXTURE_DIR/config.ini.template" "\$2" ;;
    *) exit 1 ;;
esac
EOF
chmod +x "$FAKE_BIN_DIR/wget"

PATH="$FAKE_BIN_DIR:$PATH"
export PATH

echo "Test 1: fails when both Hetzner vars are unset"
if WORKSPACE="$WORKDIR" MEDIA_ROOT="$MEDIA_DIR" PUID="$REAL_UID" PGID="$REAL_GID" \
    HOMEPAGE_CONFIG_BASE_URL="$FAKE_BASE_URL" SOULARR_CONFIG_TEMPLATE_URL="$FAKE_SOULARR_URL" \
    env -u HETZNER_STORAGEBOX_USER -u HETZNER_STORAGEBOX_PASS_OBSCURED \
    sh "$INIT_SCRIPT" 2>/dev/null; then
    fail "expected non-zero exit when both Hetzner vars are unset"
fi

echo "Test 2: fails when only one Hetzner var is set"
if WORKSPACE="$WORKDIR" MEDIA_ROOT="$MEDIA_DIR" PUID="$REAL_UID" PGID="$REAL_GID" \
    HOMEPAGE_CONFIG_BASE_URL="$FAKE_BASE_URL" SOULARR_CONFIG_TEMPLATE_URL="$FAKE_SOULARR_URL" \
    HETZNER_STORAGEBOX_USER="someuser" \
    env -u HETZNER_STORAGEBOX_PASS_OBSCURED \
    sh "$INIT_SCRIPT" 2>/dev/null; then
    fail "expected non-zero exit when HETZNER_STORAGEBOX_PASS_OBSCURED is missing"
fi

echo "Test 2b: fails when both LIDARR_API_KEY and SLSKD_API_KEY are unset"
if WORKSPACE="$WORKDIR" MEDIA_ROOT="$MEDIA_DIR" PUID="$REAL_UID" PGID="$REAL_GID" \
    HOMEPAGE_CONFIG_BASE_URL="$FAKE_BASE_URL" SOULARR_CONFIG_TEMPLATE_URL="$FAKE_SOULARR_URL" \
    HETZNER_STORAGEBOX_USER="someuser" \
    HETZNER_STORAGEBOX_PASS_OBSCURED="obscuredvalue" \
    env -u LIDARR_API_KEY -u SLSKD_API_KEY \
    sh "$INIT_SCRIPT" 2>/dev/null; then
    fail "expected non-zero exit when both LIDARR_API_KEY and SLSKD_API_KEY are unset"
fi

echo "Test 2c: fails when only LIDARR_API_KEY is set"
if WORKSPACE="$WORKDIR" MEDIA_ROOT="$MEDIA_DIR" PUID="$REAL_UID" PGID="$REAL_GID" \
    HOMEPAGE_CONFIG_BASE_URL="$FAKE_BASE_URL" SOULARR_CONFIG_TEMPLATE_URL="$FAKE_SOULARR_URL" \
    HETZNER_STORAGEBOX_USER="someuser" \
    HETZNER_STORAGEBOX_PASS_OBSCURED="obscuredvalue" \
    LIDARR_API_KEY="lidarrkey" \
    env -u SLSKD_API_KEY \
    sh "$INIT_SCRIPT" 2>/dev/null; then
    fail "expected non-zero exit when SLSKD_API_KEY is missing"
fi

echo "Test 2d: fails when only SLSKD_API_KEY is set"
if WORKSPACE="$WORKDIR" MEDIA_ROOT="$MEDIA_DIR" PUID="$REAL_UID" PGID="$REAL_GID" \
    HOMEPAGE_CONFIG_BASE_URL="$FAKE_BASE_URL" SOULARR_CONFIG_TEMPLATE_URL="$FAKE_SOULARR_URL" \
    HETZNER_STORAGEBOX_USER="someuser" \
    HETZNER_STORAGEBOX_PASS_OBSCURED="obscuredvalue" \
    SLSKD_API_KEY="slskdkey" \
    env -u LIDARR_API_KEY \
    sh "$INIT_SCRIPT" 2>/dev/null; then
    fail "expected non-zero exit when LIDARR_API_KEY is missing"
fi

echo "Test 3: succeeds and creates the directory tree when all required vars are set"
WORKSPACE="$WORKDIR" MEDIA_ROOT="$MEDIA_DIR" PUID="$REAL_UID" PGID="$REAL_GID" \
    HOMEPAGE_CONFIG_BASE_URL="$FAKE_BASE_URL" SOULARR_CONFIG_TEMPLATE_URL="$FAKE_SOULARR_URL" \
    HETZNER_STORAGEBOX_USER="someuser" \
    HETZNER_STORAGEBOX_PASS_OBSCURED="obscuredvalue" \
    LIDARR_API_KEY="reallidarrkey123" \
    SLSKD_API_KEY="realslskdkey456" \
    sh "$INIT_SCRIPT" || fail "expected success when all required vars are set"

[ -d "$WORKDIR/config/rclone" ] || fail "config/rclone not created"
[ -d "$WORKDIR/config/jellyfin" ] || fail "config/jellyfin not created"
[ -d "$WORKDIR/config/homepage" ] || fail "config/homepage not created"
[ -d "$WORKDIR/config/bazarr" ] || fail "config/bazarr not created"
[ -d "$WORKDIR/config/soularr" ] || fail "config/soularr not created"
[ -d "$WORKDIR/data/rclone-cache" ] || fail "data/rclone-cache not created"

echo "Test 4: idempotent (second run also succeeds cleanly)"
WORKSPACE="$WORKDIR" MEDIA_ROOT="$MEDIA_DIR" PUID="$REAL_UID" PGID="$REAL_GID" \
    HOMEPAGE_CONFIG_BASE_URL="$FAKE_BASE_URL" SOULARR_CONFIG_TEMPLATE_URL="$FAKE_SOULARR_URL" \
    HETZNER_STORAGEBOX_USER="someuser" \
    HETZNER_STORAGEBOX_PASS_OBSCURED="obscuredvalue" \
    LIDARR_API_KEY="reallidarrkey123" \
    SLSKD_API_KEY="realslskdkey456" \
    sh "$INIT_SCRIPT" || fail "second run should also succeed"

echo "Test 5: fetches all Homepage config files from HOMEPAGE_CONFIG_BASE_URL into config/homepage"
WORKDIR2="$(mktemp -d)"
trap 'rm -rf "$WORKDIR" "$MEDIA_DIR" "$FAKE_BIN_DIR" "$FIXTURE_DIR" "$WORKDIR2"' EXIT

: > "$FIXTURE_DIR/wget.calls"
WORKSPACE="$WORKDIR2" MEDIA_ROOT="$MEDIA_DIR" PUID="$REAL_UID" PGID="$REAL_GID" \
    HETZNER_STORAGEBOX_USER="someuser" \
    HETZNER_STORAGEBOX_PASS_OBSCURED="obscuredvalue" \
    LIDARR_API_KEY="reallidarrkey123" \
    SLSKD_API_KEY="realslskdkey456" \
    HOMEPAGE_CONFIG_BASE_URL="$FAKE_BASE_URL" \
    SOULARR_CONFIG_TEMPLATE_URL="$FAKE_SOULARR_URL" \
    sh "$INIT_SCRIPT" || fail "expected success when fetching Homepage config via the fake wget shim"

for f in $HOMEPAGE_FILES; do
    [ -f "$WORKDIR2/config/homepage/$f" ] || fail "config/homepage/$f was not written"
    cmp -s "$FIXTURE_DIR/$f" "$WORKDIR2/config/homepage/$f" \
        || fail "fetched $f content does not match fixture"
    grep -q -- "-qO $WORKDIR2/config/homepage/$f $FAKE_BASE_URL/$f" "$FIXTURE_DIR/wget.calls" \
        || fail "wget was not invoked with the expected destination and URL for $f (got: $(cat "$FIXTURE_DIR/wget.calls"))"
done

echo "Test 5c: soularr's config.ini is generated with placeholders substituted for real API keys"
[ -f "$WORKDIR2/config/soularr/config.ini" ] || fail "config/soularr/config.ini was not written"
grep -q -- "-qO $WORKDIR2/config/soularr/config.ini $FAKE_SOULARR_URL" "$FIXTURE_DIR/wget.calls" \
    || fail "wget was not invoked with the expected destination and URL for config.ini.template (got: $(cat "$FIXTURE_DIR/wget.calls"))"
grep -q "api_key = reallidarrkey123" "$WORKDIR2/config/soularr/config.ini" \
    || fail "LIDARR_API_KEY placeholder was not substituted with the real value"
grep -q "api_key = realslskdkey456" "$WORKDIR2/config/soularr/config.ini" \
    || fail "SLSKD_API_KEY placeholder was not substituted with the real value"
if grep -q -e "__LIDARR_API_KEY__" -e "__SLSKD_API_KEY__" "$WORKDIR2/config/soularr/config.ini"; then
    fail "placeholder tokens are still present in the generated config.ini"
fi

echo "Test 5b: a Homepage config fetch failure fails the whole bootstrap loudly"
cat > "$FAKE_BIN_DIR/wget" <<'EOF'
#!/bin/sh
exit 1
EOF
chmod +x "$FAKE_BIN_DIR/wget"
WORKDIR3="$(mktemp -d)"
trap 'rm -rf "$WORKDIR" "$MEDIA_DIR" "$FAKE_BIN_DIR" "$FIXTURE_DIR" "$WORKDIR2" "$WORKDIR3"' EXIT
if WORKSPACE="$WORKDIR3" MEDIA_ROOT="$MEDIA_DIR" PUID="$REAL_UID" PGID="$REAL_GID" \
    HETZNER_STORAGEBOX_USER="someuser" \
    HETZNER_STORAGEBOX_PASS_OBSCURED="obscuredvalue" \
    LIDARR_API_KEY="reallidarrkey123" \
    SLSKD_API_KEY="realslskdkey456" \
    HOMEPAGE_CONFIG_BASE_URL="$FAKE_BASE_URL" \
    SOULARR_CONFIG_TEMPLATE_URL="$FAKE_SOULARR_URL" \
    sh "$INIT_SCRIPT" 2>/dev/null; then
    fail "expected non-zero exit when a Homepage config fetch fails"
fi
[ ! -d "$WORKDIR3/data" ] || fail "bootstrap should have aborted before creating data/ after a failed fetch"

echo "Test 5d: soularr config.ini fetch failure fails the whole bootstrap loudly"
cat > "$FAKE_BIN_DIR/wget" <<EOF
#!/bin/sh
echo "\$@" >> "$FIXTURE_DIR/wget.calls"
case "\$3" in
    "$FAKE_BASE_URL"/*) cp "$FIXTURE_DIR/\${3##*/}" "\$2" ;;
    *) exit 1 ;;
esac
EOF
chmod +x "$FAKE_BIN_DIR/wget"
WORKDIR4="$(mktemp -d)"
trap 'rm -rf "$WORKDIR" "$MEDIA_DIR" "$FAKE_BIN_DIR" "$FIXTURE_DIR" "$WORKDIR2" "$WORKDIR3" "$WORKDIR4"' EXIT
if WORKSPACE="$WORKDIR4" MEDIA_ROOT="$MEDIA_DIR" PUID="$REAL_UID" PGID="$REAL_GID" \
    HETZNER_STORAGEBOX_USER="someuser" \
    HETZNER_STORAGEBOX_PASS_OBSCURED="obscuredvalue" \
    LIDARR_API_KEY="reallidarrkey123" \
    SLSKD_API_KEY="realslskdkey456" \
    HOMEPAGE_CONFIG_BASE_URL="$FAKE_BASE_URL" \
    SOULARR_CONFIG_TEMPLATE_URL="$FAKE_SOULARR_URL" \
    sh "$INIT_SCRIPT" 2>/dev/null; then
    fail "expected non-zero exit when the config.ini.template fetch fails"
fi
[ ! -d "$WORKDIR4/data" ] || fail "bootstrap should have aborted before creating data/ after a failed config.ini.template fetch"

echo "All bootstrap tests passed."
