# claude-usage-status

> **This is the shareable template.** All measurements in `status.json` and
> `history/` are dummy data illustrating the output format -- they are not real
> usage figures. Code is mirrored automatically from a private working copy;
> open issues and pull requests here.

Automated monitor for Claude subscription quota. A cron job runs every 5 minutes,
refreshes [`status.json`](status.json), and publishes it when the reading changes
meaningfully.

## What it does

1. **Quota** — queries Anthropic's OAuth usage endpoint (`/api/oauth/usage`), the
   same source Claude Code's own `/usage` command uses, authenticated with the
   OAuth token already stored locally by Claude Code. Returns **real utilization
   percentages** and reset times. Costs no tokens.
2. **Estimate** — separately scans local session logs
   (`~/.claude/projects/**/*.jsonl`) and sums reported token counters for 7 days.
3. **Publish** — writes `status.json` and commits + pushes it.

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

## Template repo

Code is mirrored to a shareable template repo with dummy data by
`sync-template.sh`. A `post-commit` hook runs it automatically, but only when a
code or doc file changes -- the cron job's constant `status.json` commits do not
trigger a sync.

Git hooks are not versioned, so after cloning, install it once:

```sh
cp hooks/post-commit .git/hooks/post-commit && chmod +x .git/hooks/post-commit
```

## Privacy

No secrets, account identifiers, name or email are ever written to this repo —
only quota figures. The OAuth token is read from `~/.claude/.credentials.json`,
used for one request, and never logged or committed. The profile endpoint, which
returns personal data, is deliberately not used.
