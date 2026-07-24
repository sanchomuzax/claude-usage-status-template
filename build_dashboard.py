#!/usr/bin/env python3
"""Build a self-contained HTML telemetry dashboard from a history/*.jsonl file.

Runtime is LLM-free: pure Python standard library. All metrics are computed
here and embedded as JSON; the charts are drawn client-side by vanilla JS.

Two things are worth knowing:

* Times are shown in the **local timezone of the machine that builds the
  report** (honouring the ``TZ`` environment variable), not UTC. The stored
  history is UTC; only the display is localised, so everyone's report reads in
  their own local time.
* The page has an **EN/HU language toggle** and defaults to English.

Usage:
    python3 build_dashboard.py history/2026-07.jsonl
    python3 build_dashboard.py history/2026-07.jsonl -o somewhere/else.html

The output defaults to the input path with a ``.html`` extension, i.e. the
report lands next to its data (``history/2026-07.jsonl`` ->
``history/2026-07.html``) rather than in the repo root.
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# order used for the status-distribution list
STATUS_ORDER = ["ok", "warning", "error", "critical", "exhausted"]

# --- local timezone of the build machine (honours $TZ) ----------------------
_NOW_LOCAL = datetime.now().astimezone()
LOCAL_TZ = _NOW_LOCAL.tzinfo
TZ_OFFSET_MIN = int((_NOW_LOCAL.utcoffset() or timedelta(0)).total_seconds() // 60)


def _tz_label() -> str:
    name = _NOW_LOCAL.strftime("%Z")
    if name and name.isalpha():
        return name
    sign = "+" if TZ_OFFSET_MIN >= 0 else "-"
    a = abs(TZ_OFFSET_MIN)
    return f"UTC{sign}{a // 60:02d}:{a % 60:02d}"


TZ_LABEL = _tz_label()


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def to_local(dt: datetime) -> datetime:
    return dt.astimezone(LOCAL_TZ)


def loc(ts: str) -> datetime:
    return parse_ts(ts).astimezone(LOCAL_TZ)


def load_rows(path: Path) -> list[dict]:
    rows = []
    for ln, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"warn: skipping malformed line {ln}: {e}", file=sys.stderr)
            continue
        if "t" not in r:
            continue
        r["_dt"] = parse_ts(r["t"])
        rows.append(r)
    rows.sort(key=lambda r: r["_dt"])
    return rows


def build_payload(rows: list[dict], year: int, month_num: int) -> dict:
    t0 = rows[0]["_dt"]
    total_min = round((rows[-1]["_dt"] - t0).total_seconds() / 60)

    def minute(dt: datetime) -> int:
        return round((dt - t0).total_seconds() / 60)

    series = [{
        "t": r["t"],
        "m": minute(r["_dt"]),
        "s": r.get("session"),
        "w": r.get("weekly"),
        "x": r.get("max"),
        "k": r.get("tokens_7d"),
        "q": r.get("quota_status"),
    } for r in rows]

    # --- status counts ---
    status: dict[str, int] = {}
    for r in rows:
        q = r.get("quota_status", "unknown")
        status[q] = status.get(q, 0) + 1
    n = len(rows)

    # --- hourly average session, bucketed by LOCAL hour ---
    buckets: dict[int, list[int]] = {h: [] for h in range(24)}
    for r in rows:
        s = r.get("session")
        if s is not None:
            buckets[to_local(r["_dt"]).hour].append(s)
    hourly = [{
        "h": h,
        "avg": round(st.mean(buckets[h]), 1) if buckets[h] else 0,
        "n": len(buckets[h]),
    } for h in range(24)]
    hmax = max((d["avg"] for d in hourly), default=0)
    peak_band = None
    if hmax > 0:
        thr = 0.6 * hmax
        pk = max(range(24), key=lambda h: hourly[h]["avg"])
        lo_h = hi_h = pk
        while lo_h - 1 >= 0 and hourly[lo_h - 1]["avg"] >= thr:
            lo_h -= 1
        while hi_h + 1 <= 23 and hourly[hi_h + 1]["avg"] >= thr:
            hi_h += 1
        peak_band = {"lo": lo_h, "hi": hi_h}

    # --- day gridlines at LOCAL midnight boundaries ---
    days = []
    first_local = to_local(t0)
    last_local = to_local(rows[-1]["_dt"])
    cur = first_local.replace(hour=0, minute=0, second=0, microsecond=0)
    while cur <= last_local:
        mm = round((cur - t0).total_seconds() / 60)
        if 0 <= mm <= total_min:
            days.append({"m": mm, "label": cur.strftime("%m-%d")})
        cur = cur + timedelta(days=1)

    # --- peaks ---
    def peak_row(key):
        best = None
        for r in rows:
            v = r.get(key)
            if v is not None and (best is None or v > best.get(key, -1)):
                best = r
        return best

    ps = peak_row("session")
    pw = peak_row("weekly")
    toks = [r["tokens_7d"] for r in rows if r.get("tokens_7d") is not None]
    avg_tok = round(st.mean(toks)) if toks else 0
    ok_count = status.get("ok", 0)
    exh = status.get("exhausted", 0)
    crit = status.get("critical", 0)
    span_days = max(1, round(total_min / 1440))
    interval = round(total_min / (n - 1)) if n > 1 else 0

    # --- KPI tiles: language-neutral facts; text is assembled in JS per language ---
    kpis = [
        {"key": "samples", "value": n,
         "note": {"type": "interval", "interval": interval, "days": span_days}},
        {"key": "okShare", "value": round(ok_count / n * 100, 1), "unit": "%",
         "note": {"type": "okRows", "n": ok_count}},
        {"key": "sessionPeak", "value": ps.get("session") if ps else 0, "unit": "%",
         "accent": "exh" if exh else "crit",
         "note": {"type": "peakTime", "date": loc(ps["t"]).strftime("%m-%d"),
                  "time": loc(ps["t"]).strftime("%H:%M"),
                  "status": ps.get("quota_status")} if ps else {"type": "none"}},
        {"key": "weeklyPeak", "value": pw.get("weekly") if pw else 0, "unit": "%",
         "accent": "weekly",
         "note": {"type": "weeklyTime", "date": loc(pw["t"]).strftime("%m-%d"),
                  "time": loc(pw["t"]).strftime("%H:%M")} if pw else {"type": "none"}},
        {"key": "saturation", "value": exh + crit, "accent": "crit",
         "note": {"type": "sat", "exh": exh, "crit": crit}},
        {"key": "tokensAvg",
         "value": round(avg_tok / 1e6, 2) if avg_tok else 0, "unit": "M",
         "note": ({"type": "tokRange", "lo": round(min(toks) / 1e6, 2),
                   "hi": round(max(toks) / 1e6, 2)} if toks else {"type": "noData"})},
    ]

    # --- saturation episodes (contiguous critical/exhausted runs) ---
    episodes = []
    i = 0
    while i < len(rows):
        if rows[i].get("quota_status") in ("critical", "exhausted"):
            j = i
            while j + 1 < len(rows) and rows[j + 1].get("quota_status") in ("critical", "exhausted"):
                j += 1
            run = rows[i:j + 1]
            peak = max(run, key=lambda r: r.get("session") or -1)
            steps = [{"time": loc(r["t"]).strftime("%H:%M"), "val": r.get("session"),
                      "cls": "hot" if r.get("quota_status") == "exhausted" else "warn"}
                     for r in run]
            if j + 1 < len(rows):
                nxt = rows[j + 1]
                steps.append({"time": loc(nxt["t"]).strftime("%H:%M"),
                              "val": nxt.get("session"), "cls": "reset"})
            episodes.append({
                "date": loc(run[0]["t"]).strftime("%Y-%m-%d"),
                "start": loc(run[0]["t"]).strftime("%H:%M"),
                "end": loc(run[-1]["t"]).strftime("%H:%M"),
                "peak": peak.get("session"),
                "weeklyAt": peak.get("weekly"),
                "hasExhausted": any(r.get("quota_status") == "exhausted" for r in run),
                "steps": steps,
            })
            i = j + 1
        else:
            i += 1
    episodes.sort(key=lambda e: (e["hasExhausted"], e["peak"] or 0), reverse=True)
    episodes = episodes[:3]

    # --- error blocks (contiguous quota_status == error runs) ---
    error_blocks = []
    i = 0
    while i < len(rows):
        if rows[i].get("quota_status") == "error":
            j = i
            while j + 1 < len(rows) and rows[j + 1].get("quota_status") == "error":
                j += 1
            run = rows[i:j + 1]
            dur_h = (run[-1]["_dt"] - run[0]["_dt"]).total_seconds() / 3600
            error_blocks.append({
                "date": loc(run[0]["t"]).strftime("%Y-%m-%d"),
                "start": loc(run[0]["t"]).strftime("%H:%M"),
                "end": loc(run[-1]["t"]).strftime("%H:%M"),
                "count": len(run), "hours": round(dur_h, 1),
                "logAlive": any(r.get("tokens_7d") is not None for r in run),
            })
            i = j + 1
        else:
            i += 1
    error_blocks.sort(key=lambda b: b["count"], reverse=True)
    error_blocks = error_blocks[:2]

    return {
        "meta": {
            "n": n, "year": year, "monthNum": month_num,
            "firstDate": to_local(t0).strftime("%Y-%m-%d"),
            "lastDate": to_local(rows[-1]["_dt"]).strftime("%Y-%m-%d"),
            "firstFull": to_local(t0).strftime("%Y-%m-%d %H:%M:%S"),
            "lastFull": to_local(rows[-1]["_dt"]).strftime("%Y-%m-%d %H:%M:%S"),
            "spanDays": span_days, "interval": interval,
            "tzLabel": TZ_LABEL, "tzOffsetMin": TZ_OFFSET_MIN,
        },
        "series": series,
        "hourly": hourly,
        "peakBand": peak_band,
        "status": status,
        "statusOrder": STATUS_ORDER,
        "days": days,
        "kpis": kpis,
        "episodes": episodes,
        "errorBlocks": error_blocks,
    }


def year_month_from(path: Path, rows: list[dict]) -> tuple[int, int]:
    try:
        y, m = path.stem.split("-")[:2]
        return int(y), int(m)
    except (ValueError, IndexError):
        dt = to_local(rows[0]["_dt"])
        return dt.year, dt.month


def render(payload: dict) -> str:
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    m = payload["meta"]
    en_month = ["", "January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"][m["monthNum"]]
    title = f"Claude Quota — {en_month} {m['year']}"
    return (TEMPLATE
            .replace("__TITLE__", title)
            .replace("/*__DATA__*/", data_json))


def main(argv=None) -> int:
    # Windows consoles (and redirected streams) default to the locale codepage
    # (cp1250/cp1252), where the "→" in the success line raises UnicodeEncodeError
    # and exits 1 -- even though the HTML was already written. Force UTF-8 so the
    # wrapper never sees a false "regeneration failed". No-op on Unix.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="Build a Claude-quota telemetry dashboard from a jsonl history file.")
    ap.add_argument("jsonl", type=Path, help="path to history/<YYYY-MM>.jsonl")
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="output HTML path (default: the input path with a .html "
                         "extension, e.g. history/2026-07.jsonl -> history/2026-07.html)")
    args = ap.parse_args(argv)

    if not args.jsonl.is_file():
        print(f"error: no such file: {args.jsonl}", file=sys.stderr)
        return 2
    rows = load_rows(args.jsonl)
    if not rows:
        print("error: no usable rows in input", file=sys.stderr)
        return 1

    out = args.output or args.jsonl.with_suffix(".html")
    year, month_num = year_month_from(args.jsonl, rows)
    payload = build_payload(rows, year, month_num)
    out.write_text(render(payload), encoding="utf-8")
    mo = payload["meta"]
    print(f"ok: {mo['n']} rows → {out}  ({mo['firstFull']} → {mo['lastFull']} {TZ_LABEL})")
    return 0


TEMPLATE = r"""<title>__TITLE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{
    color-scheme: light dark;
    --bg:#f7f9fb; --panel:#ffffff; --panel-2:#f1f5f9;
    --ink:#0f172a; --ink-2:#475569; --ink-3:#7c8ba0;
    --line:#e2e8f0; --line-2:#eef2f6; --grid:#e8edf3;
    --session:#0EA5E9; --weekly:#DB2777;
    --ok:#16a34a; --warn:#d97706; --crit:#ea580c; --exh:#dc2626; --err:#64748b;
    --plot:#ffffff;
    --shadow:0 1px 2px rgba(15,23,42,.05), 0 8px 24px -12px rgba(15,23,42,.12);
    --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
    --sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#0f1113; --panel:#16191d; --panel-2:#1b1f24;
      --ink:#e6eaf0; --ink-2:#a3adba; --ink-3:#78828f;
      --line:#282d34; --line-2:#20242a; --grid:#23282f;
      --session:#3E9BD0; --weekly:#E0559E;
      --ok:#22c55e; --warn:#f59e0b; --crit:#fb923c; --exh:#f87171; --err:#94a3b8;
      --plot:#1a1a19;
      --shadow:0 1px 2px rgba(0,0,0,.4), 0 10px 30px -14px rgba(0,0,0,.6);
    }
  }
  :root[data-theme="light"]{
    color-scheme: light;
    --bg:#f7f9fb; --panel:#ffffff; --panel-2:#f1f5f9;
    --ink:#0f172a; --ink-2:#475569; --ink-3:#7c8ba0;
    --line:#e2e8f0; --line-2:#eef2f6; --grid:#e8edf3;
    --session:#0EA5E9; --weekly:#DB2777;
    --ok:#16a34a; --warn:#d97706; --crit:#ea580c; --exh:#dc2626; --err:#64748b;
    --plot:#ffffff;
    --shadow:0 1px 2px rgba(15,23,42,.05), 0 8px 24px -12px rgba(15,23,42,.12);
  }
  :root[data-theme="dark"]{
    color-scheme: dark;
    --bg:#0f1113; --panel:#16191d; --panel-2:#1b1f24;
    --ink:#e6eaf0; --ink-2:#a3adba; --ink-3:#78828f;
    --line:#282d34; --line-2:#20242a; --grid:#23282f;
    --session:#3E9BD0; --weekly:#E0559E;
    --ok:#22c55e; --warn:#f59e0b; --crit:#fb923c; --exh:#f87171; --err:#94a3b8;
    --plot:#1a1a19;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 10px 30px -14px rgba(0,0,0,.6);
  }

  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
    line-height:1.5;-webkit-font-smoothing:antialiased;}
  .wrap{max-width:1120px;margin:0 auto;padding:32px 20px 64px;}

  header.top{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:16px;
    padding-bottom:20px;border-bottom:1px solid var(--line);margin-bottom:24px;}
  .eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.14em;text-transform:uppercase;
    color:var(--session);margin:0 0 6px;font-weight:600;}
  h1{font-size:clamp(22px,3.4vw,30px);margin:0;letter-spacing:-.02em;text-wrap:balance;font-weight:680;}
  .sub{color:var(--ink-2);font-size:13.5px;margin:8px 0 0;max-width:60ch;}
  .head-right{display:flex;flex-direction:column;align-items:flex-end;gap:10px;}
  .range{font-family:var(--mono);font-size:12px;color:var(--ink-2);text-align:right;
    background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px;box-shadow:var(--shadow);}
  .range b{color:var(--ink);font-weight:600}
  .range .lbl{color:var(--ink-3);text-transform:uppercase;letter-spacing:.1em;font-size:10px;display:block;margin-bottom:2px}

  .lang{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;
    font-family:var(--mono);font-size:11px;background:var(--panel);box-shadow:var(--shadow);}
  .lang button{appearance:none;border:0;background:transparent;color:var(--ink-3);cursor:pointer;
    padding:5px 11px;font:inherit;font-weight:600;letter-spacing:.05em;transition:background .12s,color .12s;}
  .lang button[aria-pressed="true"]{background:var(--session);color:#fff;}
  .lang button:focus-visible{outline:2px solid var(--session);outline-offset:2px;}

  .kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:24px;}
  @media (max-width:880px){.kpis{grid-template-columns:repeat(3,1fr)}}
  @media (max-width:520px){.kpis{grid-template-columns:repeat(2,1fr)}}
  .kpi{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 14px 12px;
    box-shadow:var(--shadow);position:relative;overflow:hidden;}
  .kpi .k-lbl{font-size:11px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.07em;font-weight:600}
  .kpi .k-val{font-family:var(--mono);font-size:26px;font-weight:600;letter-spacing:-.02em;margin-top:6px;
    font-variant-numeric:tabular-nums;line-height:1;}
  .kpi .k-val small{font-size:14px;color:var(--ink-3);font-weight:500}
  .kpi .k-note{font-size:11.5px;color:var(--ink-2);margin-top:5px}
  .kpi .rail{position:absolute;left:0;top:0;bottom:0;width:3px;border-radius:3px}

  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px 18px 14px;
    box-shadow:var(--shadow);margin-bottom:20px;}
  .card h2{font-size:15px;margin:0;font-weight:640;letter-spacing:-.01em;display:flex;align-items:center;gap:8px}
  .card .desc{color:var(--ink-2);font-size:12.5px;margin:4px 0 10px}
  .card-head{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:8px}
  .legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--ink-2);font-family:var(--mono)}
  .legend span{display:inline-flex;align-items:center;gap:6px}
  .legend i{width:11px;height:11px;border-radius:3px;display:inline-block}

  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px}
  @media (max-width:820px){.grid2{grid-template-columns:1fr}}
  .grid2 .card{margin-bottom:0}

  svg{display:block;width:100%;height:auto;overflow:visible}
  .axis text{font-family:var(--mono);font-size:10.5px;fill:var(--ink-3)}
  .axis line{stroke:var(--grid)}
  .thr line{stroke-dasharray:3 4;stroke-width:1}
  .thr text{font-family:var(--mono);font-size:9.5px}

  .tip{position:fixed;pointer-events:none;z-index:50;background:var(--panel);border:1px solid var(--line);
    border-radius:9px;box-shadow:var(--shadow);padding:8px 10px;font-size:12px;min-width:132px;
    opacity:0;transition:opacity .09s;transform:translate(-50%,-108%)}
  .tip .tt{font-family:var(--mono);font-size:10.5px;color:var(--ink-3);margin-bottom:5px;letter-spacing:.02em}
  .tip .row{display:flex;align-items:center;gap:7px;justify-content:space-between;margin-top:3px}
  .tip .row .nm{display:flex;align-items:center;gap:6px;color:var(--ink-2)}
  .tip .row .nm i{width:9px;height:9px;border-radius:2px}
  .tip .row .vl{font-family:var(--mono);font-weight:600;color:var(--ink);font-variant-numeric:tabular-nums}
  .tip .st{margin-top:5px;padding-top:5px;border-top:1px solid var(--line-2);font-family:var(--mono);font-size:10.5px}

  .statlist{display:flex;flex-direction:column;gap:9px;margin-top:4px}
  .statrow{display:grid;grid-template-columns:96px 1fr 88px;align-items:center;gap:10px}
  .statrow .nm{font-size:12.5px;display:flex;align-items:center;gap:7px}
  .statrow .nm i{width:9px;height:9px;border-radius:2px;flex:none}
  .statrow .track{height:14px;background:var(--panel-2);border-radius:4px;overflow:hidden}
  .statrow .fill{height:100%;border-radius:4px 3px 3px 4px;transition:width .5s cubic-bezier(.2,.7,.2,1)}
  .statrow .num{font-family:var(--mono);font-size:12px;text-align:right;color:var(--ink-2);font-variant-numeric:tabular-nums}
  .statrow .num b{color:var(--ink);font-weight:600}

  .callout{background:var(--panel);border:1px solid var(--line);
    border-left:3px solid var(--exh);border-radius:12px;padding:16px 18px;margin-bottom:20px;box-shadow:var(--shadow)}
  .callout.err{border-left-color:var(--err)}
  .callout.good{border-left-color:var(--ok)}
  .callout h3{margin:0 0 4px;font-size:14px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .callout p{margin:0;color:var(--ink-2);font-size:13px;max-width:74ch}
  .callout .seq{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px;font-family:var(--mono);font-size:11px}
  .callout .seq span{background:var(--panel-2);border:1px solid var(--line);border-radius:6px;padding:4px 8px;white-space:nowrap}
  .callout .seq span.hot{border-color:var(--exh);color:var(--exh);font-weight:600}
  .callout .seq span.warn{border-color:var(--crit);color:var(--crit)}

  .pill{font-family:var(--mono);font-size:10px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;
    padding:2px 7px;border-radius:20px;border:1px solid currentColor}

  footer{margin-top:8px;color:var(--ink-3);font-size:11.5px;font-family:var(--mono);
    border-top:1px solid var(--line);padding-top:14px;display:flex;flex-wrap:wrap;gap:8px 18px;justify-content:space-between}
  @media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>

<div class="wrap">
  <header class="top">
    <div>
      <p class="eyebrow" id="eyebrow"></p>
      <h1 id="title"></h1>
      <p class="sub" id="sub"></p>
    </div>
    <div class="head-right">
      <div class="lang" role="group" aria-label="Language">
        <button type="button" data-lang="en" aria-pressed="true">EN</button>
        <button type="button" data-lang="hu" aria-pressed="false">HU</button>
      </div>
      <div class="range" id="range"></div>
    </div>
  </header>

  <section class="kpis" id="kpis"></section>

  <section class="card">
    <div class="card-head">
      <div>
        <h2 id="c1-title"></h2>
        <p class="desc" id="c1-desc"></p>
      </div>
      <div class="legend">
        <span><i style="background:var(--session)"></i>session (5h)</span>
        <span><i style="background:var(--weekly)"></i>weekly (7d)</span>
      </div>
    </div>
    <div id="chart-main"></div>
  </section>

  <div class="grid2">
    <section class="card">
      <h2 id="c2-title"></h2>
      <p class="desc" id="c2-desc"></p>
      <div id="chart-hourly"></div>
    </section>
    <section class="card">
      <h2 id="st-title"></h2>
      <p class="desc" id="st-desc"></p>
      <div class="statlist" id="statlist"></div>
    </section>
  </div>

  <section class="card">
    <div class="card-head">
      <div>
        <h2 id="tok-title"></h2>
        <p class="desc" id="tok-desc"></p>
      </div>
      <div class="legend"><span><i style="background:var(--session)"></i>tokens_7d</span></div>
    </div>
    <div id="chart-tokens"></div>
  </section>

  <div id="callouts"></div>

  <footer>
    <span id="foot-src"></span>
    <span id="foot-range"></span>
    <span id="foot-note"></span>
  </footer>
</div>

<div class="tip" id="tip"></div>

<script id="data" type="application/json">/*__DATA__*/</script>
<script>
const DATA = JSON.parse(document.getElementById('data').textContent);
const S=DATA.series, H=DATA.hourly, ST=DATA.status, M=DATA.meta, DAYS=DATA.days;
const cs=getComputedStyle(document.documentElement);
const c=n=>cs.getPropertyValue(n).trim();
const NS='http://www.w3.org/2000/svg';
const el=(n,a={})=>{const e=document.createElementNS(NS,n);for(const k in a)e.setAttribute(k,a[k]);return e;};
const tip=document.getElementById('tip');
const showTip=(html,x,y)=>{tip.innerHTML=html;tip.style.left=x+'px';tip.style.top=y+'px';tip.style.opacity=1;};
const hideTip=()=>{tip.style.opacity=0;};
const totalMin=S[S.length-1].m;
const stColor={ok:'--ok',warning:'--warn',critical:'--crit',exhausted:'--exh',error:'--err'};

let LANG='en';
const locale=()=>LANG==='hu'?'hu-HU':'en-US';
const nf=n=>new Intl.NumberFormat(locale()).format(n);
const nf2=n=>new Intl.NumberFormat(locale(),{minimumFractionDigits:2,maximumFractionDigits:2}).format(n);
const nf1=n=>new Intl.NumberFormat(locale(),{minimumFractionDigits:1,maximumFractionDigits:1}).format(n);

// local wall-clock formatting locked to the build machine's timezone
const TZ=M.tzLabel, OFF=M.tzOffsetMin;
function fmtLocal(isoUtc){
  const d=new Date(new Date(isoUtc).getTime()+OFF*60000);
  const p=x=>String(x).padStart(2,'0');
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth()+1)}-${p(d.getUTCDate())} `+
         `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())} ${TZ}`;
}

const MONTHS={
  en:["","January","February","March","April","May","June","July","August","September","October","November","December"],
  hu:["","január","február","március","április","május","június","július","augusztus","szeptember","október","november","december"]
};

const I18N={
  en:{
    eyebrow:"Claude subscription quota · telemetry",
    title:()=>`Quota utilization — ${MONTHS.en[M.monthNum]} ${M.year}`,
    sub:"Sampled every five minutes. The session (5-hour) and weekly (7-day) windows share a 0–100% scale; the token estimate and the time-of-day pattern have their own panels. Times shown in "+TZ+".",
    period:"Period",
    kpiLabel:{samples:"Samples",okShare:"“ok” share",sessionPeak:"Session peak",weeklyPeak:"Weekly peak",saturation:"Saturation",tokensAvg:"tokens_7d avg"},
    kpiNote(nt){switch(nt.type){
      case"interval":return `every ~${nt.interval} min, over ${nt.days} days`;
      case"okRows":return `${nf(nt.n)} rows comfortably within budget`;
      case"peakTime":return `${nt.date} ${nt.time} · ${nt.status}`;
      case"weeklyTime":return `${nt.date} ${nt.time} · weekly window`;
      case"sat":return `${nt.exh}× exhausted · ${nt.crit}× critical`;
      case"tokRange":return `${nf2(nt.lo)}–${nf2(nt.hi)}M range`;
      case"noData":return "no data";default:return "—";}},
    c1t:"Session & weekly utilization over time",
    c1d:"Shared percentage scale. Dashed lines: warning 75%, critical 90%, exhausted 100%.",
    c2t:"Time-of-day pattern",
    c2d:"Average session% per hour.",
    peak:(lo,hi)=>`${lo}–${hi} peak`,
    stt:"Quota status distribution",
    std:n=>`The ${nf(n)} samples by state.`,
    tokt:"Estimated 7-day token usage",
    tokd:"tokens_7d from local logs — the rolling 7-day window sum.",
    noTok:"No tokens_7d data.",
    epTitle:e=>`Saturation episode — ${e.date}, ${e.start}–${e.end} ${TZ}`,
    epBody:e=>`Session-window peak ${e.peak}%, weekly at ${e.weeklyAt==null?"n/a":e.weeklyAt+"%"} — typically it is the short 5-hour window, not the weekly budget, that fills up in an intense session.`,
    reset:"reset",
    goodTitle:"No saturation",
    goodBody:"No sample reached the critical (90%) level in this period — the budget stayed comfortable.",
    errTitle:b=>`Auth outage — ${b.date}, ${b.start}–${b.end} ${TZ}`,
    errBody:b=>`${b.count} consecutive error rows for ~${b.hours>=1?nf1(b.hours)+" h":Math.round(b.hours*60)+" min"}.`+
      (b.logAlive?" tokens_7d kept updating, so the local log was alive — only the Anthropic OAuth query failed (usually a logout on the machine). Configuration, not quota.":""),
    footSrc:n=>`Source: history · ${nf(n)} rows`,
    footNote:"generated report — committed to the repo",
    tipSession:"session",tipWeekly:"weekly",tipAvg:"avg session",tipSamples:n=>`${n} samples`,peakTag:"peak"
  },
  hu:{
    eyebrow:"Claude subscription quota · telemetria",
    title:()=>`Kvóta-kihasználtság — ${M.year}. ${MONTHS.hu[M.monthNum]}`,
    sub:"Ötpercenkénti mintavétel. A session (5 órás) és weekly (7 napos) ablak közös 0–100% skálán; a token-becslés és a napszaki minta külön panelen. Az idők "+TZ+" szerint.",
    period:"Időszak",
    kpiLabel:{samples:"Mintavétel",okShare:"„ok” arány",sessionPeak:"Session csúcs",weeklyPeak:"Weekly csúcs",saturation:"Telítődés",tokensAvg:"tokens_7d átlag"},
    kpiNote(nt){switch(nt.type){
      case"interval":return `~${nt.interval} percenként, ${nt.days} napon át`;
      case"okRows":return `${nf(nt.n)} sor bőven kereten belül`;
      case"peakTime":return `${nt.date} ${nt.time} · ${nt.status}`;
      case"weeklyTime":return `${nt.date} ${nt.time} · heti ablak`;
      case"sat":return `${nt.exh}× exhausted · ${nt.crit}× critical`;
      case"tokRange":return `${nf2(nt.lo)}–${nf2(nt.hi)}M tartomány`;
      case"noData":return "nincs adat";default:return "—";}},
    c1t:"Session & weekly kihasználtság az időben",
    c1d:"Közös százalékos skála. Szaggatott vonalak: warning 75%, critical 90%, exhausted 100%.",
    c2t:"Napszaki minta",
    c2d:"Átlagos session% óránként.",
    peak:(lo,hi)=>`${lo}–${hi} csúcs`,
    stt:"Quota-státusz megoszlás",
    std:n=>`A ${nf(n)} mintavétel állapot szerint.`,
    tokt:"Becsült 7 napos tokenfelhasználás",
    tokd:"tokens_7d a helyi logokból — a gördülő 7 napos ablak összege.",
    noTok:"Nincs tokens_7d adat.",
    epTitle:e=>`Telítődési epizód — ${e.date}, ${e.start}–${e.end} ${TZ}`,
    epBody:e=>`A session-ablak csúcsa ${e.peak}%, a heti keret ekkor ${e.weeklyAt==null?"n/a":e.weeklyAt+"%"} — jellemzően nem a heti, hanem a rövid 5 órás ablak telik be egy intenzív munkamenetben.`,
    reset:"reset",
    goodTitle:"Nincs telítődés",
    goodBody:"Az időszakban egyetlen mintavétel sem érte el a critical (90%) szintet — a keret végig kényelmes maradt.",
    errTitle:b=>`Auth-kiesés — ${b.date}, ${b.start}–${b.end} ${TZ}`,
    errBody:b=>`${b.count} egymást követő error sor ~${b.hours>=1?nf1(b.hours)+" órán át":Math.round(b.hours*60)+" percen át"}.`+
      (b.logAlive?" A tokens_7d közben frissült, tehát a helyi log élt — csak az Anthropic OAuth-lekérdezés bukott (jellemzően kijelentkezés a gépen). Konfigurációs, nem kvóta-probléma.":""),
    footSrc:n=>`Forrás: history · ${nf(n)} sor`,
    footNote:"generált riport — a repóba commitolva",
    tipSession:"session",tipWeekly:"weekly",tipAvg:"átlag session",tipSamples:n=>`${n} mintavétel`,peakTag:"csúcs"
  }
};
const T=()=>I18N[LANG];

// ---------- render helpers that depend on language ----------
function renderText(){
  const t=T();
  document.documentElement.lang=LANG;
  document.getElementById('eyebrow').textContent=t.eyebrow;
  document.getElementById('title').textContent=t.title();
  document.getElementById('sub').textContent=t.sub;
  document.getElementById('range').innerHTML=
    `<span class="lbl">${t.period}</span><b>${M.firstDate}</b> → <b>${M.lastDate}</b>`;
  document.getElementById('c1-title').textContent=t.c1t;
  document.getElementById('c1-desc').textContent=t.c1d;
  document.getElementById('c2-title').textContent=t.c2t;
  document.getElementById('c2-desc').textContent=t.c2d;
  document.getElementById('st-title').textContent=t.stt;
  document.getElementById('st-desc').textContent=t.std(M.n);
  document.getElementById('tok-title').textContent=t.tokt;
  document.getElementById('tok-desc').textContent=t.tokd;
  document.getElementById('foot-src').textContent=t.footSrc(M.n);
  document.getElementById('foot-range').textContent=`${M.firstFull} → ${M.lastFull} ${TZ}`;
  document.getElementById('foot-note').textContent=t.footNote;
  const pk=document.getElementById('peak-label');
  if(pk&&DATA.peakBand)pk.textContent=t.peak(DATA.peakBand.lo,DATA.peakBand.hi);
  renderKpis();renderStatus();renderCallouts();
}
function renderKpis(){
  const t=T();
  document.getElementById('kpis').innerHTML=DATA.kpis.map(k=>{
    const rail=k.accent?('--'+k.accent):(k.key==='okShare'?'--ok':'--session');
    const val=(k.unit==='%'||k.key==='saturation'||k.key==='samples')?nf(k.value):nf2(k.value);
    return `<div class="kpi"><span class="rail" style="background:var(${rail})"></span>
      <div class="k-lbl">${t.kpiLabel[k.key]}</div>
      <div class="k-val">${val}${k.unit?`<small>${k.unit}</small>`:''}</div>
      <div class="k-note">${t.kpiNote(k.note)}</div></div>`;
  }).join('');
}
function renderStatus(){
  const mx=Math.max(1,...DATA.statusOrder.map(k=>ST[k]||0));
  document.getElementById('statlist').innerHTML=DATA.statusOrder.map(k=>{
    const v=ST[k]||0,pct=v/M.n*100,col=stColor[k]||'--err';
    return `<div class="statrow">
      <div class="nm"><i style="background:var(${col})"></i>${k}</div>
      <div class="track"><div class="fill" style="width:${(v/mx*100).toFixed(1)}%;background:var(${col})"></div></div>
      <div class="num"><b>${nf(v)}</b> · ${nf1(pct)}%</div></div>`;
  }).join('');
}
function renderCallouts(){
  const t=T();let html='';
  if(DATA.episodes.length){
    DATA.episodes.forEach(e=>{
      const pc=e.hasExhausted?'--exh':'--crit',pill=e.hasExhausted?'exhausted':'critical';
      const steps=e.steps.map(s=>`<span class="${s.cls==='reset'?'':s.cls}">${s.time} · ${s.val==null?'—':s.val+'%'}${s.cls==='reset'?' '+t.reset:''}</span>`).join('');
      html+=`<div class="callout" style="border-left-color:var(${pc})">
        <h3>🔺 ${t.epTitle(e)} <span class="pill" style="color:var(${pc})">${pill}</span></h3>
        <p>${t.epBody(e)}</p><div class="seq">${steps}</div></div>`;
    });
  }else{
    html+=`<div class="callout good"><h3>✅ ${t.goodTitle} <span class="pill" style="color:var(--ok)">ok</span></h3>
      <p>${t.goodBody}</p></div>`;
  }
  DATA.errorBlocks.forEach(b=>{
    html+=`<div class="callout err"><h3>⚠ ${t.errTitle(b)} <span class="pill" style="color:var(--err)">error ×${b.count}</span></h3>
      <p>${t.errBody(b)}</p></div>`;
  });
  document.getElementById('callouts').innerHTML=html;
}

function dayGrid(svg,x,mT,ih){
  const g=el('g',{class:'axis'});
  DAYS.forEach(d=>{
    g.appendChild(el('line',{x1:x(d.m),y1:mT,x2:x(d.m),y2:mT+ih,stroke:c('--line-2')}));
    const t=el('text',{x:x(d.m)+4,y:mT+ih+16,'text-anchor':'start'});t.textContent=d.label;g.appendChild(t);
  });
  svg.appendChild(g);
}

// ============ MAIN TIME CHART ============
(function(){
  const W=1040,Hh=320,mL=34,mR=14,mT=14,mB=26,iw=W-mL-mR,ih=Hh-mT-mB;
  const x=m=>mL+m/totalMin*iw, y=v=>mT+(100-v)/100*ih;
  const svg=el('svg',{viewBox:`0 0 ${W} ${Hh}`,role:'img','aria-label':'Session & weekly'});
  const g=el('g',{class:'axis'});
  [0,25,50,75,100].forEach(v=>{g.appendChild(el('line',{x1:mL,y1:y(v),x2:W-mR,y2:y(v)}));
    const t=el('text',{x:mL-6,y:y(v)+3,'text-anchor':'end'});t.textContent=v;g.appendChild(t);});
  svg.appendChild(g);
  dayGrid(svg,x,mT,ih);
  const thr=el('g',{class:'thr'});
  [[75,'--warn','warning'],[90,'--crit','critical'],[100,'--exh','exhausted']].forEach(([v,col,lb])=>{
    thr.appendChild(el('line',{x1:mL,y1:y(v),x2:W-mR,y2:y(v),stroke:c(col)}));
    const t=el('text',{x:W-mR,y:y(v)-4,'text-anchor':'end',fill:c(col)});t.textContent=lb+' '+v+'%';thr.appendChild(t);});
  svg.appendChild(thr);
  const linePath=key=>{let d='',open=false;S.forEach(p=>{const v=p[key];
    if(v==null){open=false;return;}const X=x(p.m),Y=y(v);d+=(open?'L':'M')+X.toFixed(1)+' '+Y.toFixed(1)+' ';open=true;});return d;};
  const areaPath=key=>{const segs=[];let seg='',open=false,lastX=0;S.forEach(p=>{const v=p[key];
    if(v==null){if(open){segs.push(seg+`L ${lastX} ${y(0)} Z`);seg='';open=false;}return;}
    const X=x(p.m),Y=y(v);if(!open){seg=`M ${X} ${y(0)} L ${X} ${Y} `;open=true;}else seg+=`L ${X} ${Y} `;lastX=X;});
    if(open)segs.push(seg+`L ${lastX} ${y(0)} Z`);return segs.join(' ');};
  svg.appendChild(el('path',{d:areaPath('s'),fill:c('--session'),opacity:.10}));
  svg.appendChild(el('path',{d:linePath('s'),fill:'none',stroke:c('--session'),'stroke-width':2,'stroke-linejoin':'round','stroke-linecap':'round'}));
  svg.appendChild(el('path',{d:linePath('w'),fill:'none',stroke:c('--weekly'),'stroke-width':2,'stroke-linejoin':'round','stroke-linecap':'round'}));
  const cross=el('line',{x1:0,y1:mT,x2:0,y2:mT+ih,stroke:c('--ink-3'),'stroke-width':1,opacity:0});svg.appendChild(cross);
  const dS=el('circle',{r:4,fill:c('--session'),stroke:c('--plot'),'stroke-width':2,opacity:0});
  const dW=el('circle',{r:4,fill:c('--weekly'),stroke:c('--plot'),'stroke-width':2,opacity:0});
  svg.appendChild(dS);svg.appendChild(dW);
  const over=el('rect',{x:mL,y:mT,width:iw,height:ih,fill:'transparent'});svg.appendChild(over);
  over.addEventListener('mousemove',ev=>{
    const t=T();const r=svg.getBoundingClientRect();const px=(ev.clientX-r.left)/r.width*W;const mm=(px-mL)/iw*totalMin;
    let lo=0,hi=S.length-1;while(lo<hi){const md=(lo+hi)>>1;if(S[md].m<mm)lo=md+1;else hi=md;}
    let p=S[lo];if(lo>0&&Math.abs(S[lo-1].m-mm)<Math.abs(p.m-mm))p=S[lo-1];
    const cx=x(p.m);cross.setAttribute('x1',cx);cross.setAttribute('x2',cx);cross.setAttribute('opacity',1);
    if(p.s!=null){dS.setAttribute('cx',cx);dS.setAttribute('cy',y(p.s));dS.setAttribute('opacity',1);}else dS.setAttribute('opacity',0);
    if(p.w!=null){dW.setAttribute('cx',cx);dW.setAttribute('cy',y(p.w));dW.setAttribute('opacity',1);}else dW.setAttribute('opacity',0);
    showTip(`<div class="tt">${fmtLocal(p.t)}</div>`+
      (p.s!=null?`<div class="row"><span class="nm"><i style="background:${c('--session')}"></i>${t.tipSession}</span><span class="vl">${p.s}%</span></div>`:'')+
      (p.w!=null?`<div class="row"><span class="nm"><i style="background:${c('--weekly')}"></i>${t.tipWeekly}</span><span class="vl">${p.w}%</span></div>`:'')+
      `<div class="st" style="color:${c(stColor[p.q]||'--err')}">● ${p.q}${p.k!=null?' · '+nf2(p.k/1e6)+'M tok':''}</div>`,
      ev.clientX,ev.clientY);});
  over.addEventListener('mouseleave',()=>{hideTip();cross.setAttribute('opacity',0);dS.setAttribute('opacity',0);dW.setAttribute('opacity',0);});
  document.getElementById('chart-main').appendChild(svg);
})();

// ============ HOURLY BARS ============
(function(){
  const W=520,Hh=250,mL=30,mR=8,mT=12,mB=30,iw=W-mL-mR,ih=Hh-mT-mB;
  const maxv=Math.max(1,...H.map(d=>d.avg)),bw=iw/24,y=v=>mT+(1-v/maxv)*ih;
  const band=DATA.peakBand;
  const svg=el('svg',{viewBox:`0 0 ${W} ${Hh}`,role:'img','aria-label':'hourly'});
  const g=el('g',{class:'axis'});
  const step=maxv<=20?5:10;
  for(let v=0;v<=maxv;v+=step){g.appendChild(el('line',{x1:mL,y1:y(v),x2:W-mR,y2:y(v)}));
    const t=el('text',{x:mL-5,y:y(v)+3,'text-anchor':'end'});t.textContent=v;g.appendChild(t);}
  svg.appendChild(g);
  H.forEach(d=>{
    const peak=band&&d.h>=band.lo&&d.h<=band.hi;
    const bx=mL+d.h*bw,yy=y(d.avg),hh=mT+ih-yy;
    const rect=el('rect',{x:bx+1.2,y:yy,width:bw-2.4,height:Math.max(hh,0),rx:3,fill:c('--session'),opacity:peak?1:.42});
    svg.appendChild(rect);
    const hit=el('rect',{x:bx,y:mT,width:bw,height:ih,fill:'transparent'});
    hit.addEventListener('mouseenter',ev=>{const t=T();rect.setAttribute('opacity',peak?1:.7);
      showTip(`<div class="tt">${String(d.h).padStart(2,'0')}:00 ${TZ}</div>
        <div class="row"><span class="nm"><i style="background:${c('--session')}"></i>${t.tipAvg}</span><span class="vl">${nf1(d.avg)}%</span></div>
        <div class="st">${t.tipSamples(d.n)}${peak?' · '+t.peakTag:''}</div>`,ev.clientX,ev.clientY);});
    hit.addEventListener('mousemove',ev=>{tip.style.left=ev.clientX+'px';tip.style.top=ev.clientY+'px';});
    hit.addEventListener('mouseleave',()=>{rect.setAttribute('opacity',peak?1:.42);hideTip();});
    svg.appendChild(hit);
    if(d.h%4===0){const t=el('text',{x:bx+bw/2,y:mT+ih+16,'text-anchor':'middle'});
      t.setAttribute('font-family','var(--mono)');t.setAttribute('font-size','10');t.setAttribute('fill',c('--ink-3'));
      t.textContent=String(d.h).padStart(2,'0');svg.appendChild(t);}
  });
  if(band){const bl=el('text',{id:'peak-label',x:mL+((band.lo+band.hi)/2+.5)*bw,y:mT+10,'text-anchor':'middle',fill:c('--session')});
    bl.setAttribute('font-family','var(--mono)');bl.setAttribute('font-size','10');bl.setAttribute('font-weight','600');
    svg.appendChild(bl);}
  document.getElementById('chart-hourly').appendChild(svg);
})();

// ============ TOKENS AREA ============
(function(){
  const vals=S.filter(p=>p.k!=null);
  const wrap=document.getElementById('chart-tokens');
  if(!vals.length){wrap.innerHTML=`<p class="desc" id="tok-empty"></p>`;return;}
  const W=1040,Hh=210,mL=54,mR=14,mT=14,mB=26,iw=W-mL-mR,ih=Hh-mT-mB;
  const mn=Math.min(...vals.map(p=>p.k)),mx=Math.max(...vals.map(p=>p.k));
  const pad=(mx-mn)*0.12||mx*0.05||1,lo=mn-pad,hi=mx+pad;
  const x=m=>mL+m/totalMin*iw,y=v=>mT+(1-(v-lo)/(hi-lo))*ih;
  const svg=el('svg',{viewBox:`0 0 ${W} ${Hh}`,role:'img','aria-label':'tokens_7d'});
  const g=el('g',{class:'axis'});
  for(let i=0;i<=4;i++){const v=lo+(hi-lo)*i/4;g.appendChild(el('line',{x1:mL,y1:y(v),x2:W-mR,y2:y(v)}));
    const t=el('text',{x:mL-6,y:y(v)+3,'text-anchor':'end'});t.textContent=(v/1e6).toFixed(2)+'M';g.appendChild(t);}
  svg.appendChild(g);
  dayGrid(svg,x,mT,ih);
  let dS='',dA='',open=false,lastX=0;
  vals.forEach(p=>{const X=x(p.m),Y=y(p.k);dS+=(open?'L':'M')+X.toFixed(1)+' '+Y.toFixed(1)+' ';
    if(!open)dA+=`M ${X} ${y(lo)} L ${X} ${Y} `;else dA+=`L ${X} ${Y} `;lastX=X;open=true;});
  dA+=`L ${lastX} ${y(lo)} Z`;
  svg.appendChild(el('path',{d:dA,fill:c('--session'),opacity:.12}));
  svg.appendChild(el('path',{d:dS,fill:'none',stroke:c('--session'),'stroke-width':2,'stroke-linejoin':'round'}));
  const cross=el('line',{x1:0,y1:mT,x2:0,y2:mT+ih,stroke:c('--ink-3'),opacity:0});svg.appendChild(cross);
  const dot=el('circle',{r:4,fill:c('--session'),stroke:c('--plot'),'stroke-width':2,opacity:0});svg.appendChild(dot);
  const over=el('rect',{x:mL,y:mT,width:iw,height:ih,fill:'transparent'});svg.appendChild(over);
  over.addEventListener('mousemove',ev=>{
    const r=svg.getBoundingClientRect();const px=(ev.clientX-r.left)/r.width*W;const mm=(px-mL)/iw*totalMin;
    let best=vals[0];for(const p of vals)if(Math.abs(p.m-mm)<Math.abs(best.m-mm))best=p;
    const cx=x(best.m);cross.setAttribute('x1',cx);cross.setAttribute('x2',cx);cross.setAttribute('opacity',1);
    dot.setAttribute('cx',cx);dot.setAttribute('cy',y(best.k));dot.setAttribute('opacity',1);
    showTip(`<div class="tt">${fmtLocal(best.t)}</div>
      <div class="row"><span class="nm"><i style="background:${c('--session')}"></i>tokens_7d</span><span class="vl">${nf(best.k)}</span></div>`,
      ev.clientX,ev.clientY);});
  over.addEventListener('mouseleave',()=>{hideTip();cross.setAttribute('opacity',0);dot.setAttribute('opacity',0);});
  svg.appendChild(over);
  wrap.appendChild(svg);
})();

// ---------- language toggle ----------
function setLang(l){
  LANG=l;
  document.querySelectorAll('.lang button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.lang===l)));
  renderText();
  const e=document.getElementById('tok-empty');if(e)e.textContent=T().noTok;
}
document.querySelectorAll('.lang button').forEach(b=>b.addEventListener('click',()=>setLang(b.dataset.lang)));
setLang('en');
</script>
"""


if __name__ == "__main__":
    raise SystemExit(main())
