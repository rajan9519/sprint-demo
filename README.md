# sprint-report

Turn everything you did with Claude Code during a sprint into a demo-ready report.

It reads the local transcripts of **every** Claude Code session in a date window (terminal CLI,
Claude desktop app and VS Code all write to the same store), pulls your git commits, branches and
merged PRs from the repos those sessions touched, asks Claude to summarize each session and then
to synthesize one sprint report, and renders it as Markdown and a self-contained HTML page.

```bash
sprint-report --since 2026-09-01                  # until today
sprint-report --since 2026-09-01 --until 2026-09-12 --open
```

Inside any Claude Code session:

```
/sprint-report 2026-09-01
```

## Install (for you and your team)

Everything runs locally against your own `~/.claude` transcripts and your own `claude` login, so
each teammate installs it once on their machine. Two ways:

**A. As a Claude Code plugin** (recommended once this repo is pushed to Bitbucket/GitHub):

```bash
claude plugin marketplace add <git-url-of-this-repo>
claude plugin install sprint-report@sprint-tools
```

That adds the `/sprint-report` skill to every Claude Code session. To run it from a shell as well,
also do B, or call `python3 <plugin dir>/sprint_report.py` directly. To try the plugin from a local
clone without installing: `claude --plugin-dir /path/to/sprint-report`.

**B. From a clone**:

```bash
git clone <git-url-of-this-repo> ~/tools/sprint-report
~/tools/sprint-report/install.sh
```

`install.sh` symlinks a `sprint-report` command into `~/.local/bin` and the skill into
`~/.claude/skills/sprint-report`. `install.sh --uninstall` removes both. Nothing else is written
outside the repo except reports in `~/sprint-reports/`.

Requirements: macOS or Linux, Python 3.9+ (standard library only), `git`, and the `claude` CLI
logged in. The tool never handles API keys; it inherits whatever auth `claude` already has.

## What the report contains

- Executive summary and headline achievements (first-slide material)
- Sprint-at-a-glance metrics: sessions, prompts, approximate active time, commits, tickets, PRs,
  lines changed, daily activity
- **Demo items**: what changed, why it matters, concrete demo steps, talking points, evidence
  (commits, PRs, files)
- All work streams grouped by ticket with status (done / in progress / blocked / planned)
- Technical decisions, challenges and resolutions, learnings
- Carry-over to next sprint, risks and asks
- Appendices: commits, sessions analysed, daily activity

## How it works

```
~/.claude/projects/**/*.jsonl ─┐
~/.claude/plans/*.md           ├─► collect ─► digest per session (redacted, truncated)
desktop titles (Library/…)     ┘                     │
git log / for-each-ref ────────────────► git work    │
                                                     ▼
                                   map: claude -p (haiku) × N sessions ──► cached JSON summaries
                                                     │
                                                     ▼
                                   reduce: claude -p (sonnet) × 1 ────────► report JSON
                                                     │
                                                     ▼
                                   render ─► report.md · report.html · report.artifact.html · data.json
```

1. **Collect.** Every session file whose last write is after `--since` is parsed. Only messages
   inside the window count. Per session the tool keeps: metadata (project, branch, launch source,
   model), the human prompts and assistant replies (truncated), files edited, git and build/test
   commands run, sub-agent tasks, tool errors, ticket ids (`PREFIX-1234`). Tool output (file
   contents, command output) is dropped: it is the bulk of a transcript and adds nothing to a
   sprint summary. Secrets that look like keys, tokens, JWTs or `password=` are redacted before
   anything leaves the machine.
2. **Git.** For each repo touched by a session (worktrees are folded into their main repo) it
   collects your commits across all branches in the window (`--author` defaults to the repo's
   `user.email`), branches with commits, PR numbers from merge commit subjects, and the merge
   facts described below.
3. **Map.** One `claude -p` call per session returns a structured summary (work items, status,
   problems, decisions, follow-ups). Results are cached in `<out>/.cache/sessions/` keyed by the
   session file's size and mtime, so re-running later in the sprint only processes new sessions.
   Sessions above ~90k characters are split into parts and merged.
4. **Reduce.** One call receives the metrics, git facts, plans and all session summaries and
   returns the report JSON (grouped by ticket, never by session).
5. **Render.** Markdown for pasting into Confluence/Slack/Jira, a standalone HTML page
   (light/dark aware, print friendly), a fragment for Claude's Artifact tool, and `data.json`
   with every input for auditing. If the synthesis call fails, a fallback report listing the raw
   work items is written instead so a run is never wasted.

## How "done" is decided

You decide, and the report says which rule it used. Status is grounded in git, never in the
model's impression of a conversation.

Tell it where finished work lands:

```bash
sprint-report --since 2026-09-01 --integration-branch develop
```

Or, inside Claude Code, just say it: *"generate my sprint report since Sep 1, done means merged
into develop"*. The skill maps that to the flag. The flag is repeatable if your team has
more than one such branch.

Then:

1. **Merged.** A branch is done when its tip is contained in one of those branches (`git
   merge-base --is-ancestor`), or when a pull-request merge inside the window pulled it in. The
   report names the pull request that did it, ignoring later release-train merges that merely
   carried it along.
2. **Not merged.** Commits sit only on the feature branch, so the stream is `in_progress`
   ("awaiting review", "on branch"), never done. Reachability from a sibling feature branch does
   not count.
3. **Session evidence** (a `git commit`/`git push` in a transcript, you saying it is finished)
   sharpens the description but cannot promote unmerged work to done.
4. **No commits at all** makes a stream exploratory or in progress, based on what the sessions show.

Without the flag, the fallback is the repo's mainline and release refs (`main`, `master`,
`develop`, `release*`), and the report's "Done means" row plus the run log both spell that out, so
the rule is never hidden. The exact facts the model received are in `synthesis_input.txt` under
`DEFINITION OF DONE`, `GIT WORK` and `TICKET INDEX`.

## Models and cost

Defaults are deliberately cheap: **haiku** for the per-session summaries and **sonnet** for the
final synthesis. A two-week sprint with ~35 sessions costs on the order of a dollar and takes
3 to 8 minutes. Opus/Fable-class models are refused unless you pass `--allow-expensive`.

## Options

| Flag | Default | Purpose |
|---|---|---|
| `--since`, `--until` | until = today | Window, inclusive, in local time |
| `--out DIR` | `$SPRINT_REPORT_OUT` or `~/sprint-reports` | Output root; a `sprint_<since>_<until>/` folder is created inside |
| `--project SUBSTR` / `--exclude-project SUBSTR` | | Filter sessions by working directory (repeatable) |
| `--repo PATH` | | Extra git repos to scan (repeatable) |
| `--integration-branch BRANCH` | mainline/release refs | Where finished work lands; defines "done" (repeatable) |
| `--author EMAIL` | repo `user.email` | Git author filter |
| `--name`, `--title` | derived | Engineer display name, report title override |
| `--map-model` / `--reduce-model` | `haiku` / `sonnet` | Models for per-session and final calls |
| `--map-effort` / `--reduce-effort` | `medium` / `high` | Effort levels |
| `--allow-expensive` | off | Permit opus/fable-class models |
| `--parallel N` | 4 | Concurrent per-session calls |
| `--limit N` | | Only summarize the first N sessions (testing) |
| `--refresh` | | Ignore cached per-session summaries |
| `--keep-digests` | | Also write the per-session digest text that was sent to the model |
| `--no-llm` | | Collect and write digests/metrics only, no model calls |
| `--rerender DATA_JSON` | | Re-render md/html from a previous `data.json`, no model calls |
| `--no-subagents` | | Skip delegated sub-agent transcripts |
| `--open` | | Open the HTML when done (macOS) |

## Output

```
~/sprint-reports/
  .cache/sessions/<session-id>.json     per-session summaries (reused across runs)
  sprint_2026-09-01_2026-09-11/
    report.md                            Markdown report
    report.html                          standalone HTML report
    report.artifact.html                 same page as a fragment for Claude's Artifact tool
    data.json                            everything: digests, summaries, commits, metrics, report JSON
    synthesis_input.txt                  exactly what the final call received
    digests/                             (with --keep-digests / --no-llm) per-session digest text
```

Files are created with owner-only permissions because they contain excerpts of your
conversations. Share `report.md`/`report.html`; keep `digests/`, `synthesis_input.txt` and
`data.json` to yourself.

## Typical sprint flow

- Mid-sprint sanity check: `sprint-report --since 2026-09-01 --no-llm` (seconds, free).
- Demo day: `sprint-report --since 2026-09-01 --integration-branch <your-branch> --open`. Most
  per-session summaries may already be cached from earlier runs.
- Tweak prompts in `sprintreport/prompts.py` (bump `PROMPT_VERSION` to invalidate the cache) or
  the layout in `sprintreport/render.py`, then `--rerender` to iterate without new model calls.

## Layout

```
sprint_report.py               CLI entry point and orchestration
sprintreport/collect.py        session scanning, digests, desktop titles, plans
sprintreport/gitwork.py        commits, branches, PRs, merge/"done" detection
sprintreport/llm.py            claude -p wrapper (structured output, retries) and JSON cache
sprintreport/prompts.py        system prompts and JSON schemas for both stages
sprintreport/render.py         Markdown and HTML rendering
sprintreport/util.py           time helpers, truncation, secret redaction, ticket detection
skills/sprint-report/SKILL.md  the /sprint-report skill (plugin and install.sh both use it)
.claude-plugin/                plugin.json + marketplace.json so `claude plugin install` works
install.sh                     symlink installer for the command and the skill
```
