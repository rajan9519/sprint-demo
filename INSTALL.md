# sprint-report — install guide

You have a zip of a small tool that writes your sprint demo report for you. It reads the Claude Code
sessions already on your laptop, pairs them with your git commits, and produces a Markdown and HTML
report with demo items, talking points and status per ticket.

Everything runs on your own machine, under your own Claude Code login. Nothing is uploaded anywhere,
and you never see or handle an API key.

## 1. Before you start

You need:

- **macOS or Linux**
- **Python 3.9+** — `python3 --version` (macOS already has it)
- **git**
- **Claude Code, signed in** — `claude --version`

## 2. Unzip it somewhere permanent

Pick a folder you will not delete, for example `~/tools`. The installer makes links that point back
into this folder.

```bash
mkdir -p ~/tools && cd ~/tools && unzip ~/Downloads/sprint-report-0.2.0.zip && cd sprint-report
```

## 3. Install

Look at what the installer will do first, if you like. It prints every command and changes nothing:

```bash
./install.sh --dry-run
```

Then install:

```bash
./install.sh
```

It creates exactly two symlinks in your home directory and nothing else:

| Link | Purpose |
|---|---|
| `~/.local/bin/sprint-report` | the `sprint-report` command |
| `~/.claude/skills/sprint-report` | the `/sprint-report` skill inside Claude Code |

It refuses to overwrite anything it did not create, never uses `sudo`, and downloads nothing. If it
warns that `~/.local/bin` is not on your PATH, either add it to your `~/.zshrc` or just use the skill
inside Claude Code, which does not need PATH.

**Prefer not to install anything?** Use it as a plugin for one session instead:

```bash
claude --plugin-dir ~/tools/sprint-report
```

## 4. Use it

The easiest way is inside Claude Code. Start a session and type:

```
/sprint-report 2026-09-01
```

Claude will ask how your team decides something is "done" (the branch you merge finished work into),
run the tool, and hand you the report. You can answer up front instead:

```
/sprint-report 2026-09-01 done means merged into develop
```

From a terminal, the same thing:

```bash
sprint-report --since 2026-09-01 --integration-branch develop --open
```

Start with a free, instant dry check that makes no model calls at all:

```bash
sprint-report --since 2026-09-01 --no-llm
```

A full report over a two-week sprint takes 3 to 15 minutes and costs roughly $1 to $2 of model usage
on your own account. It uses Haiku per session and Sonnet once at the end; expensive models are
refused unless you deliberately pass `--allow-expensive`.

## 5. Where your report lands

```
~/sprint-reports/sprint_<since>_<until>/
  report.md             paste into Confluence, Jira or Slack
  report.html           open in a browser, prints cleanly
  report.artifact.html  for Claude's Artifact tool
  data.json             every input, for checking anything the report claims
```

Share `report.md` or `report.html`. Keep `data.json`, `synthesis_input.txt` and `digests/` to
yourself: they contain excerpts of your own conversations. All of it is written readable by you only.

## 6. What is sent where

- Your session transcripts are read from `~/.claude/projects` and stay on your machine.
- A **trimmed, redacted digest** of each session goes to Claude through your own `claude` CLI, the
  same as any prompt you type. Command output and file contents are stripped out first, and strings
  that look like keys, tokens, JWTs or passwords are replaced with `[REDACTED]` before sending.
- Nothing is sent to any other service. The tool makes no network calls of its own.

## 7. Uninstall

```bash
cd ~/tools/sprint-report && ./install.sh --uninstall
```

That removes the two symlinks and nothing else. Delete the folder and `~/sprint-reports` if you want
the reports gone too.

## Troubleshooting

| Problem | Fix |
|---|---|
| `sprint-report: command not found` | `~/.local/bin` is not on your PATH. Add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc`, or use `/sprint-report` in Claude Code. |
| `claude` CLI not found | Install Claude Code and sign in, then re-run. |
| "No Claude Code sessions with activity in that window" | You had no sessions in that date range, or they were in a project you filtered out with `--project`. |
| Everything shows as not done | The branch you passed to `--integration-branch` is not where your work actually merges. Run without it to see the fallback rule the report used. |
| It feels slow | It is one model call per session. `--parallel 8` speeds it up; re-runs reuse cached summaries. |
