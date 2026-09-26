---
name: sprint-report
description: Generate a sprint demo report from local Codex or Claude Code sessions and git history. Use when the user asks what they worked on during a sprint, requests a demo report, or asks to summarize coding sessions over a date window.
---

# Sprint report

Use the `sprint-report` command installed by this repository's `./install.sh --cli-only`.
If it is missing, tell the user to run that installer from a permanent copy of the repository.

Determine the first day from the user's request. The last day defaults to today. If the
user gave a branch into which finished work is merged, pass `--integration-branch`.
Otherwise use the default git-based rule and state that rule in the final response.

Run `sprint-report --since YYYY-MM-DD --source codex` for Codex work. Use
`--source all` if the user explicitly requests both Codex and Claude sessions.
The command uses the user's Codex login for summarization by default. Do not add
`--no-llm` unless the user asks for collection only.

After it finishes, read `report.md` and give the user the main achievements, demo
items, metrics, applied definition of done, and links to `report.md` and `report.html`.
Keep `data.json`, `synthesis_input.txt`, and `digests/` private; they contain
redacted but potentially sensitive conversation excerpts.
