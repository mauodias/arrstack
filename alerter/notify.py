"""Composing and pushing notifications."""
import json
import urllib.error
import urllib.request

from evaluate import HEALTHY, WARNING, ERROR

RESEND_ENDPOINT = "https://api.resend.com/emails"

# Resend sits behind Cloudflare, which rejects urllib's default User-Agent
# with a 403 (error 1010) before the request ever reaches the API.
USER_AGENT = "arrstack-alerter/1.0"

# Garmin watches render notifications with a small hardcoded emoji subset
# rather than a full emoji font. The coloured circles arrive as empty boxes;
# these three were confirmed to render on a Forerunner 245.
TAGS = {HEALTHY: "thumbsup", WARNING: "confused", ERROR: "thumbsdown"}
HEADLINE = {
    HEALTHY: "recovered",
    WARNING: "warning",
    ERROR: "error",
}


def format_alert(rule, from_state, to_state, value):
    title = "%s %s" % (rule.name, HEADLINE[to_state])
    shown = "unknown" if value is None else ("%.1f" % value)
    body = "%s moved from %s to %s.\nCurrent value: %s" % (
        rule.name,
        from_state,
        to_state,
        shown,
    )
    return title, body, TAGS[to_state]


def send_ntfy(server, topic, title, body, tags):
    """Push one notification. Returns False rather than raising: a failed
    notification must not interrupt evaluation of the remaining rules."""
    url = "%s/%s" % (server.rstrip("/"), topic)
    request = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers={
            "Title": title,
            "Tags": tags,
            "Content-Type": "text/plain; charset=utf-8",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20):
            return True
    except (urllib.error.URLError, OSError):
        return False


def send_email(api_key, sender, recipient, subject, body_html,
               endpoint=RESEND_ENDPOINT):
    """Send one email through Resend. Returns False rather than raising: a
    day's digest is not worth retrying into a flood."""
    payload = json.dumps(
        {"from": sender, "to": [recipient], "subject": subject, "html": body_html}
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30):
            return True
    except (urllib.error.URLError, OSError):
        return False
