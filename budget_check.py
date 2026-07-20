#!/usr/bin/env python3
"""Decide whether it is safe to start or continue expensive agent work.

Combines two questions an orchestrator needs answered before fanning out
subagents:

  1. Headroom -- how close is the 5-hour session window to its limit?
  2. Burn rate -- is the 7-day window being consumed faster than a steady pace
     would allow, so that it would run out before it resets?

Prints a JSON verdict and exits with a status code the shell can gate on:
  0 = GO       start anything
  1 = CAUTION  small/sequential work only, no large fan-outs
  2 = STOP     do not start new work; checkpoint and wind down
  3 = UNKNOWN  quota could not be read (fail open, but say so)

Usage:
  python3 budget_check.py           # JSON verdict
  python3 budget_check.py --brief   # single human-readable line
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent

# Session headroom thresholds (percent of the 5-hour window used).
SESSION_CAUTION = 70
SESSION_STOP = 88

# Weekly thresholds (percent of the 7-day window used).
WEEKLY_STOP = 92

# Burn rate = actual usage / usage a steady pace would have reached by now.
# Above 1.0 means "ahead of budget". Some overshoot is normal and fine.
BURN_CAUTION = 1.4
BURN_STOP = 2.0

# Burn rate is noisy at the very start of a window: 1% used in the first minutes
# reads as a huge multiplier while meaning nothing. Rather than going blind for
# the first day, gate on absolute consumption as well -- a fast burn only counts
# once enough has actually been spent to matter.
MIN_ELAPSED_FRACTION = 0.03      # ~5 hours into the week
BURN_CAUTION_FLOOR = 10          # weekly % that must be spent before warning
BURN_STOP_FLOOR = 35             # weekly % that must be spent before stopping

SESSION_WINDOW = timedelta(hours=5)
WEEKLY_WINDOW = timedelta(days=7)

# When the live call is unavailable (e.g. an agent running on another machine),
# we fall back to the last published status.json. That reading has a shelf life:
# reporting an expired number confidently is worse than admitting we cannot see.
CACHE_MAX_AGE_MINUTES = 90


def parse_timestamp(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def load_limits():
    """Fetch live quota; fall back to status.json if the call fails."""
    try:
        completed = subprocess.run(
            [sys.executable, str(REPO_DIR / "fetch_limits.py")],
            capture_output=True, text=True, timeout=45,
        )
        data = json.loads(completed.stdout)
        if not data.get("error"):
            return data, "live"
    except (OSError, ValueError, subprocess.SubprocessError):
        pass

    try:
        with (REPO_DIR / "status.json").open("r", encoding="utf-8") as handle:
            cached = json.load(handle)
        return cached, "cached"
    except (OSError, ValueError):
        return {"error": "no live quota and no readable status.json"}, "none"


def elapsed_fraction(resets_at, window):
    """How far through the window we are, as 0.0-1.0."""
    end = parse_timestamp(resets_at)
    if end is None:
        return None
    start = end - window
    now = datetime.now(timezone.utc)
    total = window.total_seconds()
    elapsed = (now - start).total_seconds()
    if total <= 0:
        return None
    return min(max(elapsed / total, 0.0), 1.0)


def minutes_until(resets_at):
    end = parse_timestamp(resets_at)
    if end is None:
        return None
    return max(0, round((end - datetime.now(timezone.utc)).total_seconds() / 60))


def cache_age_minutes(limits):
    """How old a cached reading is, from the timestamp it was written with."""
    stamp = parse_timestamp(limits.get("timestamp_utc"))
    if stamp is None:
        return None
    return round((datetime.now(timezone.utc) - stamp).total_seconds() / 60)


def session_window_expired(limits):
    """True if the cached reading's 5-hour window has already reset.

    Once that happens the session figure describes a window that no longer
    exists, so it must not be used -- this is exactly how a stale reading
    misleads: it reports high usage for a window that has since emptied.
    """
    end = parse_timestamp(limits.get("session_resets_at"))
    return end is not None and end <= datetime.now(timezone.utc)


def evaluate(limits, source="live", age=None):
    session = limits.get("session_percent_used")
    weekly = limits.get("weekly_percent_used")

    # A cached reading whose session window has expired tells us nothing about
    # the current window. Drop the figure rather than report it confidently.
    session_stale = source == "cached" and session_window_expired(limits)
    if session_stale:
        session = None

    fraction = elapsed_fraction(limits.get("weekly_resets_at"), WEEKLY_WINDOW)
    burn = None
    projected = None
    if (isinstance(weekly, (int, float)) and fraction
            and fraction >= MIN_ELAPSED_FRACTION):
        ideal = fraction * 100
        if ideal > 0:
            burn = round(weekly / ideal, 2)
            projected = round(weekly / fraction)

    verdict = "GO"
    reasons = []

    def escalate(level, reason):
        nonlocal verdict
        order = {"GO": 0, "CAUTION": 1, "STOP": 2}
        if order[level] > order[verdict]:
            verdict = level
        reasons.append(reason)

    if isinstance(session, (int, float)):
        if session >= SESSION_STOP:
            escalate("STOP", f"session window {session}% used")
        elif session >= SESSION_CAUTION:
            escalate("CAUTION", f"session window {session}% used")

    if isinstance(weekly, (int, float)) and weekly >= WEEKLY_STOP:
        escalate("STOP", f"weekly window {weekly}% used")

    if burn is not None and isinstance(weekly, (int, float)):
        pace = f"burning {burn}x steady pace, on track for {projected}% by reset"
        if burn >= BURN_STOP and weekly >= BURN_STOP_FLOOR:
            escalate("STOP", pace)
        elif burn >= BURN_CAUTION and weekly >= BURN_CAUTION_FLOOR:
            escalate("CAUTION", pace)

    # Being blind on the 5-hour window is itself a reason for restraint: never
    # hand out a GO based on a reading that cannot see the current session.
    if session_stale:
        escalate(
            "CAUTION",
            f"stale cache ({age}m old): its 5-hour window already reset, "
            "session figure unusable -- run `git pull` for a fresher reading",
        )

    return {
        "verdict": verdict,
        "reasons": reasons,
        "session_percent_used": session,
        "session_figure_stale": session_stale,
        "weekly_percent_used": weekly,
        "weekly_burn_rate": burn,
        "weekly_projected_at_reset": projected,
        "session_resets_in_minutes": minutes_until(limits.get("session_resets_at")),
        "weekly_resets_in_minutes": minutes_until(limits.get("weekly_resets_at")),
    }


def main():
    limits, source = load_limits()

    age = cache_age_minutes(limits) if source == "cached" else None

    if limits.get("error"):
        result = {
            "verdict": "UNKNOWN",
            "reasons": [limits["error"]],
            "source": source,
        }
        code = 3
    elif source == "cached" and (age is None or age > CACHE_MAX_AGE_MINUTES):
        # Too old to reason from. Say so plainly instead of guessing.
        stale = "unknown age" if age is None else f"{age} minutes old"
        result = {
            "verdict": "UNKNOWN",
            "reasons": [
                f"no live quota access and the cached reading is {stale} "
                f"(limit {CACHE_MAX_AGE_MINUTES}m) -- run `git pull` in the "
                "claude-usage-status repo, or ask the user for a fresh reading"
            ],
            "source": source,
            "data_age_minutes": age,
        }
        code = 3
    else:
        result = evaluate(limits, source=source, age=age)
        result["source"] = source
        result["data_age_minutes"] = age
        code = {"GO": 0, "CAUTION": 1, "STOP": 2}[result["verdict"]]

    if "--brief" in sys.argv:
        bits = [result["verdict"]]
        # Always show where the numbers came from: a reader must never have to
        # guess whether this was live or a cached snapshot.
        if result.get("source") == "cached":
            age = result.get("data_age_minutes")
            bits.append(f"CACHED{f' {age}m old' if age is not None else ''}")
        elif result.get("source") == "live":
            bits.append("live")
        if result.get("session_percent_used") is not None:
            bits.append(f"session {result['session_percent_used']}%")
        elif result.get("session_figure_stale"):
            bits.append("session unknown (stale)")
        if result.get("weekly_percent_used") is not None:
            bits.append(f"weekly {result['weekly_percent_used']}%")
        if result.get("weekly_burn_rate") is not None:
            bits.append(f"burn {result['weekly_burn_rate']}x")
        if result["reasons"]:
            bits.append("(" + "; ".join(result["reasons"]) + ")")
        print(" | ".join(bits))
    else:
        json.dump(result, sys.stdout, indent=2)
        print()

    return code


if __name__ == "__main__":
    sys.exit(main())
