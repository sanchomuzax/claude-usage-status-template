# claude-usage-status

> **This is the shareable template.** All measurements in `status.json` and
> `history/` are dummy data illustrating the output format -- they are not real
> usage figures. Code is mirrored automatically from a private working copy;
> open issues and pull requests here.

Automated monitor for Claude subscription quota. A cron job runs every 5 minutes,
refreshes [`status.json`](status.json), and publishes it when the reading changes
meaningfully.

## Quick start

You need **one always-on machine** (a home server, a Raspberry Pi, a cheap VPS —
anything that stays powered) with **Claude Code installed and logged in**. Verify
with `claude -p "hi"`; if that answers, you are ready.

**1. Make your own copy — and keep it private.**
On GitHub click **“Use this template” → Create a new repository**, name it
whatever you like, and set it to **Private**. This is important: the repo records
your usage over time, which is your data. Then clone it onto the always-on
machine:

```sh
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git ~/claude-usage-status
cd ~/claude-usage-status
```

**2. Drop the template-only files** (only present if you started from the
template — skip if they are not there):

```sh
rm -f sync-template.sh template_guard.py test_template_guard.py
rm -rf hooks
```

**3. Run it once by hand** and check it collected real numbers:

```sh
./claude-usage-check.sh
cat status.json      # session_percent_used etc. should be filled in, quota_status: "ok"
```

If `quota_status` is `error`, make sure Claude Code is logged in on this machine.

**4. Schedule it every 5 minutes** with cron (`crontab -e`), using the real path:

```
*/5 * * * * ~/claude-usage-status/claude-usage-check.sh >/dev/null 2>&1
```

Cron restarts with the machine, so this survives reboots. Watch `status.json` and
`history/` fill up over the next hour.

**5. Let your agents use it.** Tell your Claude Code agent (or put it in a project
`CLAUDE.md`) to read and follow the rules in
[ORCHESTRATOR_PROMPT.md](ORCHESTRATOR_PROMPT.md). If the agent runs on the same
machine, point it at the local file. If it runs elsewhere (browser, another
host), have it `git pull` this **private** repo first — a plain link will not be
readable, but an authenticated clone/pull works.

> **Keeping an agent current after you update this repo.** A `git pull` refreshes
> the *code*: the agent runs `budget_check.py` fresh each time, so new script
> behaviour applies automatically — you don't need to explain the internals. But
> an agent that already loaded `ORCHESTRATOR_PROMPT.md` into its context earlier
> keeps its *old* understanding; pulling the file to disk does not change what the
> agent already read. So after the prompt or scripts change, tell the agent to
> **`git pull` and then re-read `ORCHESTRATOR_PROMPT.md`** — otherwise it will
> interpret the new output (e.g. `CACHED`, `UNKNOWN`, a `[conserve]` tag) with the
> old rules.

**6. (Optional) pick a spending strategy** — see *Spending strategy* below to
make agents burn the whole allowance or keep a reserve for you.

That's it. From here the sections below explain what each field means and how the
pieces work.

## What it does

1. **Quota** — queries Anthropic's OAuth usage endpoint (`/api/oauth/usage`), the
   same source Claude Code's own `/usage` command uses, authenticated with the
   OAuth token already stored locally by Claude Code. Returns **real utilization
   percentages** and reset times. Costs no tokens.
2. **Estimate** — separately scans local session logs
   (`~/.claude/projects/**/*.jsonl`) and sums reported token counters for 7 days.
3. **Publish** — writes `status.json` and commits + pushes it.

The OAuth access token expires roughly daily. On a headless server nothing would
renew it, so the monitor would go 401-blind after a day. To prevent that, when a
quota call returns HTTP 401 the runner makes **one** minimal CLI call (renewing
the token is a side effect of any invocation) and retries — costing tokens only
when the token has actually expired, roughly once a day. If auth is *permanently*
broken (you logged out, the refresh token itself died), a one-hour cooldown stops
it from retrying on every run; it tries again at most once an hour and logs that
the login likely needs fixing.

## Spending strategy (`budget_check.py`)

`budget_check.py` turns the quota into a `GO` / `CAUTION` / `STOP` verdict for
orchestrators (see [ORCHESTRATOR_PROMPT.md](ORCHESTRATOR_PROMPT.md)). How
aggressive it is depends on a profile:

| profile | intent |
|---|---|
| `balanced` (default) | spend freely, stop before the window runs out |
| `greedy` | use nearly the whole allowance before pulling back |
| `conserve` | protect a reserve — warn and stop early (e.g. leave half the week) |

```sh
BUDGET_PROFILE=conserve python3 budget_check.py --brief
```

Any single threshold can be overridden without a profile, e.g. stop weekly work
at 50%:

```sh
BUDGET_WEEKLY_STOP=50 python3 budget_check.py --brief
```

A non-default profile is shown in the output (e.g. `STOP | [conserve] | …`) so a
conservative stop is never mistaken for a near-empty account.

## What you get

| field | meaning |
|---|---|
| `session_percent_used` | 5-hour session window utilization (real) |
| `weekly_percent_used` | 7-day window utilization (real) |
| `max_percent_used` | the worst of all active limits |
| `limits[]` | every limit window, including model-scoped ones |
| `session_resets_at` / `weekly_resets_at` | when each window resets |
| `quota_status` | `ok` <75%, `warning` ≥75%, `critical` ≥90%, `exhausted` 100% |
| `estimated_tokens_7d` | approximate token count from local logs |

## The optional live probe

```sh
./claude-usage-check.sh --probe
```

Sends a real minimal call through the CLI and reports whether it went through.
**Off by default**: it costs ~17k tokens per run (the CLI's irreducible base
context) and the quota endpoint already reports the real numbers. Enable it only
to verify independently that calls actually succeed. When it is off,
`probe_status` is derived from the quota figures and `probe_was_live` is `false`.

## Accuracy notes

- The **percentages are authoritative** — they come from Anthropic's servers.
- The **token counts are an approximation**. They cover only sessions logged on
  this machine, and do not map linearly onto the percentages (different models
  consume quota at different rates). `estimated_tokens_7d` counts plain input +
  output; cache reads and writes are reported separately in `usage_breakdown_7d`,
  because they are orders of magnitude larger and would distort the total.

## Files

| file | purpose |
|---|---|
| `claude-usage-check.sh` | the runner invoked by cron |
| `budget_check.py` | GO/CAUTION/STOP verdict for agents ([prompt](ORCHESTRATOR_PROMPT.md)) |
| `fetch_limits.py` | real quota fetcher |
| `estimate_tokens.py` | 7-day token estimator |
| `status.json` | latest result (overwritten each run) |
| `history/YYYY-MM.jsonl` | one compact line per run, for trends |
| `history/YYYY-MM.html` | the published report for that month; rebuilt, committed & pushed each meaningful reading |
| `build_dashboard.py` | turns a history file into an offline HTML dashboard |
| `sample/2026-05.jsonl` | synthetic demo data to try the dashboard right away |
| `sample/2026-05.html` | a rendered demo report, committed so it's visible without running anything |
| `run.log` | local run log, git-ignored |

## Schedule and publishing

```
*/5 * * * * ~/claude-usage-status/claude-usage-check.sh
```

Cron restarts with the machine, so this survives a reboot with no extra setup.

`status.json` and the history file are refreshed **every 5 minutes locally**, but
a commit is only pushed when the reading actually means something:

- `quota_status` changed (e.g. `ok` → `warning`)
- any headline percentage moved by ≥1 point
- the quota or probe error state changed
- nothing has been published for 6 hours (heartbeat)

This keeps the repo history readable instead of drowning it in 288 identical
commits a day. History lines written while nothing changed are not lost — they
ride along with the next commit.

### Coverage

The **percentages are account-wide**: they come from Anthropic's servers, so
usage from any machine, the phone app, or claude.ai is included. The **token
estimate is machine-local** — it only sees sessions logged on this host. Sessions
driven remotely (e.g. from the web UI) still count as local when the agent runs
here.

## Dashboard

`build_dashboard.py` turns any monthly history file into a single, self-contained
HTML dashboard — KPI tiles, a session/weekly time series with the warning /
critical / exhausted thresholds, the hourly usage pattern, the status mix, the
token trend, and auto-detected saturation and auth-error episodes. The page has an
**EN/HU language toggle** (English by default) and shows all times in the **local
timezone of the machine that builds it** (via `$TZ`), so the report reads in your
own local time — the stored history stays UTC.

```sh
python3 build_dashboard.py history/2026-07.jsonl
# -> history/2026-07.html  (open it in any browser)
```

It is **offline and LLM-free**: pure Python standard library, no network calls.
Everything is computed from the jsonl and embedded into the page; the charts draw
client-side with vanilla JavaScript. A full month (~8,900 lines) builds in well
under a second. By default the report is written **next to its data**
(`history/2026-07.jsonl` → `history/2026-07.html`), so months stay together and
the repo root stays clean.

The generated `history/<month>.html` **is committed and published** — it is the
report, part of the repo, not a throwaway. GitHub does not render HTML inline, so
to read it open the raw file in a browser (Raw → save, or `git pull` and open it
locally). A committed sample is included so you can see one immediately without
cloning-and-running:

- **[`sample/2026-05.html`](sample/2026-05.html)** — a rendered demo report built
  from `sample/2026-05.jsonl` (two weeks of synthetic readings, not real usage).
  Rebuild it with:

  ```sh
  python3 build_dashboard.py sample/2026-05.jsonl   # -> sample/2026-05.html
  ```

You can delete the `sample/` directory once you've seen it; `make_sample.py` in
there regenerates the demo data deterministically if you ever want it back.

**Live view.** `claude-usage-check.sh` rebuilds `history/<current-month>.html`
whenever a reading is worth committing (the same "something changed" signal used
for git — so it does not rewrite the file on every 5-minute run) and **commits and
pushes it alongside `status.json`**. The report on GitHub therefore tracks the
latest meaningful reading on its own. The rebuild is gated to the still-open
month, so it rolls over to a new file on the 1st with no configuration.

## Running on Windows

The core (`budget_check.py`, `fetch_limits.py`, `estimate_tokens.py`) runs
unmodified on Windows 11 with **Git Bash + Task Scheduler**. A few
platform-specific gotchas (thanks to a user who ported it):

- **Line endings.** The repo ships a `.gitattributes` that pins `*.sh`/`*.py`
  to LF, so a default clone (`core.autocrlf=true`) can't rewrite the scripts to
  CRLF — which otherwise makes bash fail with `python3^M: command not found`. If
  you cloned *before* this file existed, re-normalise once:
  `git add --renormalize . && git checkout .`.

- **`python3` doesn't exist.** Windows ships `py` (or `python`), not `python3`.
  Either create a venv and point the scripts at it, or alias it. The venv
  interpreter lives at `.venv\Scripts\python.exe` (not `.venv/bin/python`):

  ```sh
  py -m venv .venv
  PY="$(pwd)/.venv/Scripts/python.exe"   # use "$PY" in place of python3 for manual runs
  ```

- **Running the script by hand.** Typing `bash` on Windows often launches WSL,
  not Git Bash. Use the full path:

  ```powershell
  & "C:\Program Files\Git\bin\bash.exe" -lc "'/c/Users/YOU/claude-usage-status/claude-usage-check.sh'"
  ```

- **Scheduling (cron replacement).** Use Task Scheduler. To avoid a black
  console window flashing every 5 minutes, launch a tiny hidden VBS via
  `wscript.exe` instead of calling `bash.exe` directly. Save `run-hidden.vbs`:

  ```vbscript
  Set sh = CreateObject("WScript.Shell")
  cmd = """C:\Program Files\Git\bin\bash.exe"" -lc ""'/c/Users/YOU/claude-usage-status/claude-usage-check.sh'"""
  sh.Run cmd, 0, False
  ```

  Then a Task Scheduler task, every 5 minutes: program `wscript.exe`, argument
  the full path to `run-hidden.vbs`. If your machine's clock is UTC but you want
  local time in the report, set `TZ=Europe/Budapest` (or your zone) in the task's
  environment.

The Python entry points already force UTF-8 output, so the `→` in the log line
won't crash on a cp1250/cp1252 console.

## Template repo

Code is mirrored to a shareable template repo with dummy data by
`sync-template.sh`. A `post-commit` hook runs it automatically, but only when a
code or doc file changes -- the cron job's constant `status.json` commits do not
trigger a sync.

Before anything is committed to the public template, `template_guard.py` scans
the built output and **aborts the sync** if it finds credentials, a username,
an absolute home path, an email address, real measurements in place of the dummy
data, or any file not on its allowlist. Run its test suite with:

```sh
python3 test_template_guard.py
```

Git hooks are not versioned, so after cloning, install it once:

```sh
cp hooks/post-commit .git/hooks/post-commit && chmod +x .git/hooks/post-commit
```

## Privacy

No secrets, account identifiers, name or email are ever written to this repo —
only quota figures. The OAuth token is read from `~/.claude/.credentials.json`,
used for one request, and never logged or committed. The profile endpoint, which
returns personal data, is deliberately not used.
