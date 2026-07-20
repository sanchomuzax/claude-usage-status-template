#!/usr/bin/env python3
"""Fetch real Claude subscription quota utilization from Anthropic's OAuth API.

Reads the OAuth access token that Claude Code already stores in
~/.claude/.credentials.json and queries the usage endpoint, which reports exact
utilization percentages and reset times per limit window.

Privacy: only quota figures are emitted. The access token, account identifiers,
name and email are never printed -- the profile endpoint is deliberately not
used. Prints a JSON object to stdout and never raises.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
TIMEOUT_SECONDS = 30


def read_access_token():
    """Return (token, subscription_type) from the local credentials file."""
    with CREDENTIALS_PATH.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    oauth = payload.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        raise KeyError("no accessToken in claudeAiOauth")
    return token, oauth.get("subscriptionType")


def fetch_usage(token):
    """Query the usage endpoint and return the decoded response."""
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETA,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def describe_scope(scope):
    """Flatten a limit's scope into a short label, e.g. 'Opus' or 'Opus/api'."""
    if not isinstance(scope, dict):
        return None
    parts = []
    model = scope.get("model")
    if isinstance(model, dict) and model.get("display_name"):
        parts.append(model["display_name"])
    if scope.get("surface"):
        parts.append(str(scope["surface"]))
    return "/".join(parts) or None


def normalize_limits(payload):
    """Reduce the API response to the limit rows we care about."""
    rows = []
    for entry in payload.get("limits") or []:
        if not isinstance(entry, dict):
            continue
        percent = entry.get("percent")
        rows.append(
            {
                "kind": entry.get("kind"),
                "group": entry.get("group"),
                "percent_used": percent,
                "percent_remaining": (100 - percent) if isinstance(percent, (int, float)) else None,
                "severity": entry.get("severity"),
                "resets_at": entry.get("resets_at"),
                "scope": describe_scope(entry.get("scope")),
                "is_active": entry.get("is_active"),
            }
        )
    return rows


def summarize(limits, payload):
    """Pick the headline numbers: the worst limit, plus session and weekly."""
    percents = [row["percent_used"] for row in limits if isinstance(row["percent_used"], (int, float))]
    by_kind = {row["kind"]: row for row in limits}

    def percent_of(kind):
        row = by_kind.get(kind)
        return row["percent_used"] if row else None

    session = percent_of("session")
    if session is None:
        session = (payload.get("five_hour") or {}).get("utilization")
    weekly = percent_of("weekly_all")
    if weekly is None:
        weekly = (payload.get("seven_day") or {}).get("utilization")

    return {
        "max_percent_used": max(percents) if percents else None,
        "session_percent_used": session,
        "weekly_percent_used": weekly,
        "session_resets_at": (payload.get("five_hour") or {}).get("resets_at"),
        "weekly_resets_at": (payload.get("seven_day") or {}).get("resets_at"),
    }


def main():
    if not CREDENTIALS_PATH.is_file():
        json.dump({"error": "credentials file not found; is Claude Code logged in?"}, sys.stdout)
        return 0

    try:
        token, subscription = read_access_token()
    except (OSError, ValueError, KeyError) as error:
        json.dump({"error": f"cannot read credentials: {type(error).__name__}: {error}"}, sys.stdout)
        return 0

    try:
        payload = fetch_usage(token)
    except urllib.error.HTTPError as error:
        detail = f"HTTP {error.code}"
        if error.code in (401, 403):
            detail += " (OAuth token rejected or expired -- run any claude command to refresh it)"
        json.dump({"error": detail}, sys.stdout)
        return 0
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as error:
        json.dump({"error": f"usage request failed: {type(error).__name__}: {error}"}, sys.stdout)
        return 0

    limits = normalize_limits(payload)
    result = {"subscription_type": subscription, "limits": limits}
    result.update(summarize(limits, payload))

    extra = payload.get("extra_usage")
    if isinstance(extra, dict):
        result["extra_usage"] = {
            "is_enabled": extra.get("is_enabled"),
            "utilization": extra.get("utilization"),
            "currency": extra.get("currency"),
        }

    json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
