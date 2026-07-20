#!/usr/bin/env python3
"""Approximate 7-day Claude Code token usage from local session logs.

Reads ~/.claude/projects/**/*.jsonl, keeps assistant records whose timestamp
falls inside the window, and sums their reported usage counters.

This is an ESTIMATE, not an official quota figure:
  - only sessions logged on this machine are visible
  - assistant records are deduplicated by requestId, but retries the CLI never
    logged are invisible
  - cache reads/writes are counted separately from plain input tokens
Prints a JSON object to stdout. Never raises: on failure it reports an error
field so the caller can still write a status file.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

WINDOW_DAYS = 7
LOG_ROOT = Path.home() / ".claude" / "projects"


def parse_timestamp(raw):
    """Parse an ISO-8601 timestamp, returning None if unusable."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def iter_usage_records(path, cutoff):
    """Yield (request_id, usage_dict) for in-window assistant records."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or '"usage"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stamp = parse_timestamp(record.get("timestamp"))
                if stamp is None or stamp < cutoff:
                    continue
                usage = (record.get("message") or {}).get("usage")
                if not isinstance(usage, dict):
                    continue
                request_id = record.get("requestId") or record.get("uuid")
                yield request_id, usage
    except OSError as error:
        print(f"warning: cannot read {path.name}: {error}", file=sys.stderr)


def collect(cutoff):
    """Walk every session log and total the deduplicated usage counters."""
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    seen = set()
    sessions = 0

    for path in sorted(LOG_ROOT.rglob("*.jsonl")):
        sessions += 1
        for request_id, usage in iter_usage_records(path, cutoff):
            key = (path.name, request_id)
            if request_id and key in seen:
                continue
            if request_id:
                seen.add(key)
            for field in totals:
                value = usage.get(field)
                if isinstance(value, int):
                    totals[field] += value

    return totals, sessions, len(seen)


def main():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=WINDOW_DAYS)

    if not LOG_ROOT.is_dir():
        json.dump(
            {"error": f"session log directory not found: {LOG_ROOT}"},
            sys.stdout,
        )
        return 0

    try:
        totals, sessions, messages = collect(cutoff)
    except Exception as error:  # last-resort guard: caller must always get JSON
        json.dump({"error": f"{type(error).__name__}: {error}"}, sys.stdout)
        return 0

    billable = totals["input_tokens"] + totals["output_tokens"]
    everything = billable + totals["cache_creation_input_tokens"] + totals["cache_read_input_tokens"]

    json.dump(
        {
            "window_days": WINDOW_DAYS,
            "sessions_scanned": sessions,
            "messages_counted": messages,
            "input_tokens": totals["input_tokens"],
            "output_tokens": totals["output_tokens"],
            "cache_creation_input_tokens": totals["cache_creation_input_tokens"],
            "cache_read_input_tokens": totals["cache_read_input_tokens"],
            "estimated_tokens_7d": billable,
            "estimated_tokens_7d_including_cache": everything,
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
