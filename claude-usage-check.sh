#!/usr/bin/env bash
#
# Claude usage monitor: reads real subscription quota utilization from the
# Anthropic OAuth usage endpoint, estimates recent token usage from local
# session logs, writes status.json, and pushes it to GitHub.
#
# Designed to be run unattended from cron. It never exits non-zero on failure --
# a failure IS the signal, and gets recorded in status.json.
#
# If the OAuth token has expired (HTTP 401), it renews it automatically with one
# minimal CLI call and retries -- so a headless server stays alive unattended.
#
# Usage: claude-usage-check.sh [--probe]
#   --probe  additionally send a live test call through the Claude Code CLI.
#            Off by default: it costs ~17k tokens per run and the quota endpoint
#            already reports the real numbers. Use it only to verify that calls
#            actually go through, independently of what the API reports.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATUS_FILE="${REPO_DIR}/status.json"
LOG_FILE="${REPO_DIR}/run.log"
PROBE_MODEL="claude-haiku-4-5-20251001"
PROBE_TIMEOUT_SECONDS=120
RUN_PROBE=0

[[ "${1:-}" == "--probe" ]] && RUN_PROBE=1

# cron gives a minimal PATH; make sure the usual install locations are visible.
export PATH="${HOME}/.local/bin:${HOME}/bin:/usr/local/bin:/usr/bin:/bin"

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"${LOG_FILE}"
}

# --- 1. real quota ----------------------------------------------------------
# Authoritative source: the same endpoint Claude Code's own /usage command uses,
# authenticated with the OAuth token already on this machine. Costs no tokens.

fetch_quota() {
  local out
  out="$(python3 "${REPO_DIR}/fetch_limits.py" 2>>"${LOG_FILE}")"
  [[ -n "${out}" ]] && printf '%s' "${out}" || printf '%s' '{"error":"fetch_limits.py produced no output"}'
}

# The OAuth access token expires roughly daily and nothing here renews it on its
# own. On an interactive machine a stray `claude` command keeps it alive, but a
# headless server running only this cron would silently go 401-blind after a day.
# So: if the quota call fails auth, spend one tiny CLI call to refresh the token
# (that is a side effect of any invocation) and retry once. This costs tokens
# only when the token has actually expired -- about once a day, not every run.
refresh_token() {
  command -v claude >/dev/null 2>&1 || { log "token refresh skipped: claude CLI not found"; return 1; }
  timeout "${PROBE_TIMEOUT_SECONDS}" claude \
    -p "Reply with exactly: OK" \
    --model "${PROBE_MODEL}" \
    --output-format json \
    --safe-mode \
    --strict-mcp-config \
    --mcp-config '{"mcpServers":{}}' \
    --disable-slash-commands \
    --no-session-persistence \
    --allowed-tools "" \
    --system-prompt "Token refresh. Reply with exactly: OK" >/dev/null 2>>"${LOG_FILE}"
}

# If auth is permanently broken (logged out, refresh token itself dead), a 401
# would otherwise trigger a refresh attempt on every single run -- 288 pointless
# CLI spawns a day. A cooldown caps attempts to one per hour: retry occasionally
# in case it was transient, but never hammer a dead login.
REFRESH_STATE="${REPO_DIR}/.refresh_state"
REFRESH_COOLDOWN_SECONDS=3600

refresh_in_cooldown() {
  [[ -f "${REFRESH_STATE}" ]] || return 1
  local last now
  last="$(cat "${REFRESH_STATE}" 2>/dev/null)"
  [[ "${last}" =~ ^[0-9]+$ ]] || return 1
  now="$(date +%s)"
  (( now - last < REFRESH_COOLDOWN_SECONDS ))
}

limits_json="$(fetch_quota)"

if printf '%s' "${limits_json}" | grep -q '"error".*HTTP 401'; then
  if refresh_in_cooldown; then
    log "quota call got 401 but a refresh was tried within the last hour; skipping (auth may be broken -- check that Claude Code is logged in)"
  else
    log "quota call got 401; refreshing OAuth token via a minimal CLI call"
    date +%s >"${REFRESH_STATE}"
    if refresh_token; then
      limits_json="$(fetch_quota)"
      if printf '%s' "${limits_json}" | grep -q '"error"'; then
        log "quota still failing after token refresh (auth likely broken)"
      else
        log "token refreshed, quota call recovered"
      fi
    fi
  fi
fi

# --- 2. optional live probe -------------------------------------------------

probe_status="not_run"
probe_error_detail="probe disabled (run with --probe to enable)"

if [[ ${RUN_PROBE} -eq 1 ]]; then
  probe_status="error"
  probe_error_detail=""

  if ! command -v claude >/dev/null 2>&1; then
    probe_error_detail="claude CLI not found in PATH"
    log "probe skipped: ${probe_error_detail}"
  else
    probe_raw="$(cd "${REPO_DIR}" && timeout "${PROBE_TIMEOUT_SECONDS}" claude \
      -p "Reply with exactly: OK" \
      --model "${PROBE_MODEL}" \
      --output-format json \
      --safe-mode \
      --strict-mcp-config \
      --mcp-config '{"mcpServers":{}}' \
      --disable-slash-commands \
      --no-session-persistence \
      --allowed-tools "" \
      --system-prompt "Health probe. Reply with exactly: OK" 2>&1)"
    probe_exit=$?

    if [[ ${probe_exit} -eq 124 ]]; then
      probe_error_detail="probe timed out after ${PROBE_TIMEOUT_SECONDS}s"
    else
      probe_eval="$(PROBE_RAW="${probe_raw}" PROBE_EXIT="${probe_exit}" python3 - <<'PY'
import json, os, re, sys

raw = os.environ.get("PROBE_RAW", "")
exit_code = os.environ.get("PROBE_EXIT", "1")
limit_pattern = re.compile(
    r"rate.?limit|usage limit|quota|too many requests|429|overloaded|"
    r"limit reached|upgrade to|resets? at",
    re.IGNORECASE,
)

def emit(status, detail=""):
    print(json.dumps({"status": status, "detail": detail[:500]}))
    sys.exit(0)

payload = None
for line in raw.splitlines():
    line = line.strip()
    if line.startswith("{"):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue

if payload is None:
    emit("rate_limited" if limit_pattern.search(raw) else "error",
         raw.strip() or f"no JSON output, exit code {exit_code}")

api_status = payload.get("api_error_status")
result = str(payload.get("result") or "")
subtype = str(payload.get("subtype") or "")
blob = " ".join([str(api_status or ""), result, subtype])

if api_status == 429 or limit_pattern.search(blob):
    emit("rate_limited", blob.strip())

if payload.get("is_error") or exit_code != "0":
    emit("error", blob.strip() or f"exit code {exit_code}")

emit("ok", "")
PY
)"
      probe_status="$(printf '%s' "${probe_eval}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])' 2>/dev/null || echo error)"
      probe_error_detail="$(printf '%s' "${probe_eval}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["detail"])' 2>/dev/null || echo "could not parse probe output")"
    fi
    log "probe: ${probe_status} ${probe_error_detail}"
  fi
fi

# --- 3. token estimate from local logs --------------------------------------

usage_json="$(python3 "${REPO_DIR}/estimate_tokens.py" 2>>"${LOG_FILE}")"
if [[ -z "${usage_json}" ]]; then
  usage_json='{"error":"estimate_tokens.py produced no output"}'
fi

# --- 4. write status.json ---------------------------------------------------

decision="$(STATUS_JSON="${STATUS_FILE}" \
PROBE_STATUS="${probe_status}" \
PROBE_DETAIL="${probe_error_detail}" \
PROBE_ENABLED="${RUN_PROBE}" \
USAGE_JSON="${usage_json}" \
LIMITS_JSON="${limits_json}" \
python3 - <<'PY'
import json, os
from datetime import datetime, timezone

def load(name):
    try:
        return json.loads(os.environ[name])
    except json.JSONDecodeError as error:
        return {"error": f"unparseable {name}: {error}"}

usage = load("USAGE_JSON")
limits = load("LIMITS_JSON")
quota_error = limits.get("error")
worst = limits.get("max_percent_used")

# quota_status summarizes the authoritative numbers in one word.
if quota_error:
    quota_status = "error"
elif isinstance(worst, (int, float)):
    if worst >= 100:
        quota_status = "exhausted"
    elif worst >= 90:
        quota_status = "critical"
    elif worst >= 75:
        quota_status = "warning"
    else:
        quota_status = "ok"
else:
    quota_status = "unknown"

probe_status = os.environ["PROBE_STATUS"]
if os.environ["PROBE_ENABLED"] != "1":
    # Keep the field meaningful when no live call was made: mirror the quota.
    effective = "rate_limited" if quota_status == "exhausted" else (
        "error" if quota_status == "error" else "ok")
else:
    effective = probe_status

status = {
    "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),

    # --- authoritative, from Anthropic's usage endpoint ---
    "quota_status": quota_status,
    "session_percent_used": limits.get("session_percent_used"),
    "weekly_percent_used": limits.get("weekly_percent_used"),
    "max_percent_used": worst,
    "session_resets_at": limits.get("session_resets_at"),
    "weekly_resets_at": limits.get("weekly_resets_at"),
    "limits": limits.get("limits"),
    "subscription_type": limits.get("subscription_type"),
    "extra_usage": limits.get("extra_usage"),
    "quota_error_detail": quota_error or "",

    # --- optional live call ---
    "probe_status": effective,
    "probe_error_detail": os.environ["PROBE_DETAIL"],
    "probe_was_live": os.environ["PROBE_ENABLED"] == "1",

    # --- approximate, from local logs ---
    "estimated_tokens_7d": usage.get("estimated_tokens_7d"),
    "usage_breakdown_7d": usage,

    "note": (
        "Percentages are REAL, from Anthropic's OAuth usage endpoint (the same "
        "source as Claude Code's /usage). estimated_tokens_7d is a separate "
        "approximation from local session logs on this machine only, and does "
        "not map linearly onto the percentages. When probe_was_live is false, "
        "probe_status is derived from the quota figures rather than a live call."
    ),
}

# --- decide whether this reading is worth a commit ---
# At a 5-minute cadence most readings are identical or drift by fractions of a
# percent. Committing every one would bury the interesting changes, so only a
# real movement (or a long silence) gets published.

CHANGE_THRESHOLD = 1          # percentage points
HEARTBEAT_HOURS = 6           # publish at least this often regardless

status_path = os.environ["STATUS_JSON"]
previous = None
try:
    with open(status_path, "r", encoding="utf-8") as handle:
        previous = json.load(handle)
except (OSError, json.JSONDecodeError):
    previous = None


def moved(field):
    """True if a percentage field shifted by at least the threshold."""
    if previous is None:
        return True
    old, new = previous.get(field), status.get(field)
    if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
        return old != new
    return abs(new - old) >= CHANGE_THRESHOLD


reasons = []
if previous is None:
    reasons.append("no previous reading")
else:
    if previous.get("quota_status") != status["quota_status"]:
        reasons.append(f"status {previous.get('quota_status')} -> {status['quota_status']}")
    for field in ("session_percent_used", "weekly_percent_used", "max_percent_used"):
        if moved(field):
            reasons.append(f"{field} {previous.get(field)} -> {status.get(field)}")
    if bool(previous.get("quota_error_detail")) != bool(status["quota_error_detail"]):
        reasons.append("quota error state changed")
    if previous.get("probe_status") != status["probe_status"]:
        reasons.append(f"probe {previous.get('probe_status')} -> {status['probe_status']}")

    # Heartbeat: never let the published file go stale for too long.
    try:
        stamp = datetime.strptime(previous["timestamp_utc"], "%Y-%m-%dT%H:%M:%SZ")
        stamp = stamp.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - stamp).total_seconds() / 3600
        if age_hours >= HEARTBEAT_HOURS:
            reasons.append(f"heartbeat ({age_hours:.1f}h since last publish)")
    except (KeyError, ValueError):
        reasons.append("previous timestamp unreadable")

with open(status_path, "w", encoding="utf-8") as handle:
    json.dump(status, handle, indent=2)
    handle.write("\n")

# --- append the reading to a monthly time series ---
# One compact line per run, so trends stay graphable without bloating any file.

history_dir = os.path.join(os.path.dirname(status_path), "history")
os.makedirs(history_dir, exist_ok=True)
history_path = os.path.join(
    history_dir, datetime.now(timezone.utc).strftime("%Y-%m") + ".jsonl"
)
sample = {
    "t": status["timestamp_utc"],
    "quota_status": status["quota_status"],
    "session": status["session_percent_used"],
    "weekly": status["weekly_percent_used"],
    "max": status["max_percent_used"],
    "tokens_7d": status["estimated_tokens_7d"],
}
try:
    with open(history_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sample) + "\n")
except OSError as error:
    print(f"warning: cannot append history: {error}")

# The shell reads this last line to decide whether to publish.
if reasons:
    print("COMMIT " + "; ".join(reasons))
else:
    print("SKIP unchanged")
PY
)"
decision_line="$(printf '%s' "${decision}" | tail -n 1)"

# --- 5. commit and push -----------------------------------------------------
# status.json and the history file are always updated locally; only meaningful
# readings are published, so the repo history stays readable at a 5-minute cadence.

cd "${REPO_DIR}" || { log "cannot cd to ${REPO_DIR}"; exit 0; }

if [[ "${decision_line}" != COMMIT* ]]; then
  log "local update only (${decision_line})"
else
  reason="${decision_line#COMMIT }"
  summary="$(python3 -c "
import json
d = json.load(open('status.json'))
print(f\"{d['quota_status']} session={d.get('session_percent_used')}% weekly={d.get('weekly_percent_used')}%\")
" 2>/dev/null || echo "update")"

  git add status.json history 2>/dev/null
  if [[ -z "$(git diff --cached --name-only)" ]]; then
    log "nothing staged despite decision: ${reason}"
  elif git commit -q -m "chore: usage status $(date -u +%Y-%m-%dT%H:%MZ) (${summary})" -m "${reason}"; then
    if git push -q origin HEAD 2>>"${LOG_FILE}"; then
      log "pushed: ${summary} [${reason}]"
    else
      log "push failed (commit kept locally, will go out next run)"
    fi
  else
    log "commit failed"
  fi
fi

# Keep the log from growing without bound.
if [[ -f "${LOG_FILE}" ]] && [[ "$(wc -l <"${LOG_FILE}")" -gt 2000 ]]; then
  tail -n 500 "${LOG_FILE}" >"${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "${LOG_FILE}"
fi

exit 0
