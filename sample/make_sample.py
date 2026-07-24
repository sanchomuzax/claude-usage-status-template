#!/usr/bin/env python3
"""Generate a synthetic demo history file for the dashboard.

This is a *sample* dataset shipped with the template so newcomers can run
``build_dashboard.py`` and see a populated dashboard before collecting any of
their own data. It is entirely made up: a seeded simulation, not real usage.

    python3 sample/make_sample.py            # writes sample/2026-05.jsonl

The shape (evening-weighted bursts, 5-hour session resets, a slow weekly climb,
one saturation episode, one auth-error gap) mirrors what a real month looks
like without reproducing any actual figures.
"""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEED = 20260504
START = datetime(2026, 5, 4, 0, 0, tzinfo=timezone.utc)
DAYS = 14
STEP_MIN = 5
SESSION_WIN_MIN = 300          # 5-hour session window
WEEKLY_WIN_MIN = 7 * 1440      # 7-day weekly window

# Engineered, clearly-labelled events (different times than the author's data):
EXHAUST_DAY = 5                # a heavy evening that tops out the session window
CRIT_DAY = 11                 # a lighter spike that only reaches "critical"
ERROR_START = START + timedelta(days=9, hours=12, minutes=20)
ERROR_END = START + timedelta(days=9, hours=15, minutes=55)


def diurnal(hour: float) -> float:
    """Activity weight over the day: quiet nights, a late-afternoon/evening peak."""
    evening = math.exp(-((hour - 18) ** 2) / (2 * 3.1 ** 2))
    morning = 0.30 * math.exp(-((hour - 9) ** 2) / (2 * 1.7 ** 2))
    return evening + morning


def main() -> int:
    rng = random.Random(SEED)
    out = Path(__file__).with_name("2026-05.jsonl")

    n = DAYS * 24 * 60 // STEP_MIN
    session_acc = 0.0
    weekly_acc = 0.0
    prev_swin = prev_wwin = -1
    lines = []

    for i in range(n):
        t = START + timedelta(minutes=i * STEP_MIN)
        mins = i * STEP_MIN
        day = (t - START).days
        hour = t.hour + t.minute / 60.0

        swin = mins // SESSION_WIN_MIN
        wwin = mins // WEEKLY_WIN_MIN
        if swin != prev_swin:
            session_acc = 0.0
            prev_swin = swin
        if wwin != prev_wwin:
            weekly_acc = 0.0
            prev_wwin = wwin

        # per-day energy so some days are busier than others
        day_factor = 0.55 + 0.4 * ((rng.random() + math.sin(day)) % 1.0)
        if day == EXHAUST_DAY:
            day_factor = 1.35
        elif day == CRIT_DAY:
            day_factor = 1.05

        weight = diurnal(hour)
        # is the agent doing anything this 5-minute slot?
        active = rng.random() < (0.08 + 0.60 * min(weight, 1.0))
        burst = weight * day_factor * (rng.uniform(1.4, 2.9) if active else 0.0)

        # engineered evenings: EXHAUST_DAY tops out (100), CRIT_DAY only reaches
        # the "critical" band. The 96 cap below keeps CRIT_DAY off "exhausted".
        if active and 18 <= hour <= 21:
            if day == EXHAUST_DAY:
                burst *= 3.0
            elif day == CRIT_DAY:
                burst *= 1.9

        cap = 96.0 if day == CRIT_DAY else 100.0
        session_acc = min(cap, session_acc + burst)
        weekly_acc = min(68.0, weekly_acc + burst * rng.uniform(0.055, 0.095))

        # token estimate: a separate, lower band than the author's (~0.8-1.15M)
        tokens = int(
            860_000
            + 150_000 * (0.5 + 0.5 * math.sin((mins / WEEKLY_WIN_MIN) * math.tau))
            + rng.randint(-24_000, 24_000)
        )

        in_error = ERROR_START <= t <= ERROR_END
        if in_error:
            session = weekly = mx = None
            status = "error"
        else:
            session = round(session_acc)
            weekly = round(weekly_acc)
            mx = max(session, weekly)
            if mx >= 100:
                status = "exhausted"
            elif mx >= 90:
                status = "critical"
            elif mx >= 75:
                status = "warning"
            else:
                status = "ok"

        lines.append(json.dumps({
            "t": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "quota_status": status,
            "session": session,
            "weekly": weekly,
            "max": mx,
            "tokens_7d": tokens,
        }))

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {len(lines)} rows -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
