---
name: sprint-report
description: Generate a sprint demo report from local Claude Code or Codex sessions plus git commits in a date window. Use when the user asks for a sprint report, sprint demo summary, "what did I work on since <date>", or runs /sprint-report <since> [until].
argument-hint: "<since YYYY-MM-DD> [until YYYY-MM-DD] [merged-into <branch>] [--project SUBSTR]"
---

# Sprint demo report

Arguments: `$ARGUMENTS`

The first date starts the window (required). An optional second date is the last day, inclusive
(default: today). Remaining tokens pass through to the script (`--project my-app`,
`--title "Sprint 42"`, `--refresh`).

## Before running: how is "done" decided?

Status in the report is grounded in git, and the rule is the user's to set. If the user has said
anything like "done means merged into feature/team-integration", "we merge PRs into develop", or names a
branch, pass it as `--integration-branch <branch>` (repeatable). If they have not said, ask in one
short question, offering the branches that exist as options:

```bash
git -C <repo> for-each-ref --sort=-committerdate --format='%(refname:short)' refs/heads | head -12
```

Do not block on the answer. If the user does not care or does not reply, run without the flag: the
tool falls back to the repo's mainline and release branches and prints the rule it used in the
report, so nothing is hidden. Never invent a branch name.

## Steps

1. Locate the generator, using the first that works: `python3 "${CLAUDE_PLUGIN_ROOT}/sprint_report.py"`
   (installed as a plugin), or `sprint-report` (installed via `install.sh`). Confirm with `--version`.
   If neither works, tell the user to clone the sprint-report repo and run its `install.sh`.

2. Run it. Expect several minutes: one model call per session plus one synthesis call.
   Use a long Bash timeout (600000 ms) or run it in the background and poll the log.
   Never run two instances at once.

   ```bash
   <generator> --since <since> [--until <until>] [--integration-branch <branch>] [passthrough]
   ```

   When the user requests Codex sessions, add `--source codex`. For both agents, use
   `--source all`. To summarize with the current Claude login, add `--llm claude`;
   otherwise the command selects the available CLI automatically.

   It prints the output directory, by default `~/sprint-reports/sprint_<since>_<until>/`, holding
   `report.md`, `report.html` (standalone), `report.artifact.html` (fragment for the Artifact tool)
   and `data.json` (all inputs, for auditing).

3. Read `report.md` and reply with a standalone summary: headline achievements, demo items with
   status, the metrics, the definition of done that was applied, and the file paths.

4. If the Artifact tool is available, publish `report.artifact.html` (it already carries its own
   `<title>` and `<style>`; pass favicon "📊") and give the user the link. Otherwise offer
   `open <path>/report.html`.

## Notes

- Claude sessions come from `~/.claude/projects/**.jsonl`; Codex sessions come from
  `~/.codex/sessions/**/*.jsonl`. Nothing needs exporting.
- A branch counts as done when its tip is contained in an integration branch, or when a
  pull-request merge in the window pulled it in. Session text never promotes unmerged work to done.
- Claude model defaults are haiku per session and sonnet for synthesis. Codex uses its
  configured model. Opus and Fable-class Claude models are refused unless
  `--allow-expensive` is passed. Do not add that flag on your own initiative.
- Per-session summaries are cached under `<out>/.cache/`, so re-running later in the sprint only
  summarizes new sessions. `--refresh` redoes them.
- `--no-llm` collects digests and metrics only (seconds, free). `--rerender <data.json>` re-renders
  Markdown and HTML from a previous run with no model calls.
- Never paste `digests/*.txt`, `synthesis_input.txt` or `data.json` to the user or into any other
  tool: they contain transcript excerpts.
