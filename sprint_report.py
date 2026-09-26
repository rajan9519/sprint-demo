#!/usr/bin/env python3
"""Build a sprint demo report from local Codex or Claude Code sessions in a date window.

    ./sprint_report.py --since 2026-09-01                 # until today
    ./sprint_report.py --since 2026-09-01 --until 2026-09-12 --open
    ./sprint_report.py --since 2026-09-01 --no-llm        # collect only, no model calls

Pipeline
  1. collect   scan local Codex and/or Claude Code JSONL files, keep messages inside
               the window, build a compact redacted digest per session
  2. git       commits / branches / PRs by you in the repos those sessions touched
  3. map       one selected CLI call per session -> structured summary (cached on disk)
  4. reduce    one selected CLI call over everything -> sprint report JSON
  5. render    Markdown + self-contained HTML (+ data.json with all inputs for auditing)
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import math
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from sprintreport import __version__  # noqa: E402
from sprintreport.collect import (SessionDigest, Turn, collect_sessions, load_plans,  # noqa: E402
                                  render_digest_text)
from sprintreport.codex_collect import collect_codex_sessions  # noqa: E402
from sprintreport.gitwork import RepoWork, collect_git_work, render_git_text, resolve_repos  # noqa: E402
from sprintreport.llm import ClaudeCLI, CodexCLI, ClaudeError, JsonCache, stderr_log  # noqa: E402
from sprintreport.prompts import (PROMPT_VERSION, SESSION_SCHEMA, SESSION_SYSTEM, SPRINT_SCHEMA,  # noqa: E402
                                  SPRINT_SYSTEM, session_prompt, sprint_prompt)
from sprintreport.render import render_html, render_markdown  # noqa: E402
from sprintreport.util import (day_end_exclusive, day_start, fmt_local, human_minutes, local_tz,  # noqa: E402
                               parse_date, parse_ts, uniq)

MAX_MAP_CHARS = 90_000          # digest larger than this is split into parts for the map step
EXPENSIVE_MODEL_MARKERS = ("opus", "fable", "mythos")
ENTRY_LABEL = {"cli": "Terminal", "claude-desktop": "Desktop app", "claude-vscode": "VS Code",
               "sdk-ts": "SDK", "sdk-py": "SDK", "codex-vscode": "Codex desktop/IDE",
               "codex-desktop": "Codex desktop", "codex-cli": "Codex CLI"}


# ------------------------------------------------------------------ arguments

def parse_args(argv: List[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="sprint_report.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", "--from", dest="since", default="", help="first day of the window, YYYY-MM-DD")
    ap.add_argument("--until", "--to", dest="until", default=None, help="last day (inclusive), default today")
    ap.add_argument("--out", default=os.environ.get("SPRINT_REPORT_OUT") or os.path.expanduser("~/sprint-reports"),
                    help="output root (default: $SPRINT_REPORT_OUT or ~/sprint-reports)")
    ap.add_argument("--claude-dir", default=os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"),
                    help="Claude Code config dir holding projects/ (default: $CLAUDE_CONFIG_DIR or ~/.claude)")
    ap.add_argument("--codex-dir", default=os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"),
                    help="Codex config dir holding sessions/ (default: $CODEX_HOME or ~/.codex)")
    ap.add_argument("--source", choices=["auto", "claude", "codex", "all"], default="auto",
                    help="session source; auto uses available local stores (default: auto)")
    ap.add_argument("--llm", choices=["auto", "claude", "codex"], default="auto",
                    help="summarizer CLI; auto prefers Claude for Claude-only reports, Codex otherwise")
    ap.add_argument("--project", action="append", default=[], metavar="SUBSTR",
                    help="only sessions whose working directory contains SUBSTR (repeatable)")
    ap.add_argument("--exclude-project", action="append", default=[], metavar="SUBSTR",
                    help="skip sessions whose working directory contains SUBSTR (repeatable)")
    ap.add_argument("--repo", action="append", default=[], metavar="PATH", help="extra git repo to scan (repeatable)")
    ap.add_argument("--integration-branch", "--done-when-merged-to", dest="integration", action="append",
                    default=[], metavar="BRANCH",
                    help="branch your team merges finished work into, e.g. develop or main. "
                         "This defines 'done': a branch counts as done once its tip is contained there. "
                         "Repeatable. Without it, mainline and release refs (main/master/develop, release*) are used "
                         "and the rule is stated in the report.")
    ap.add_argument("--author", default="", help="git author filter (default: each repo's user.email)")
    ap.add_argument("--name", default="", help="engineer display name (default: from git commits)")
    ap.add_argument("--title", default="", help="override the report title")
    ap.add_argument("--map-model", default="", help="model for per-session summaries (default: haiku for Claude; Codex configured model)")
    ap.add_argument("--reduce-model", default="", help="model for the final synthesis (default: sonnet for Claude; Codex configured model)")
    ap.add_argument("--map-effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--reduce-effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--allow-expensive", action="store_true",
                    help="permit opus/fable-class models (refused by default to keep runs cheap)")
    ap.add_argument("--parallel", type=int, default=4, help="concurrent model calls in the map step (default 4)")
    ap.add_argument("--timeout", type=int, default=900, help="seconds per model call (default 900)")
    ap.add_argument("--limit", type=int, default=0, help="summarize only the first N sessions (testing)")
    ap.add_argument("--min-prompts", type=int, default=1, help="ignore sessions with fewer human prompts")
    ap.add_argument("--no-subagents", action="store_true", help="do not read delegated sub-agent transcripts")
    ap.add_argument("--no-llm", action="store_true", help="collect and write digests only; no model calls")
    ap.add_argument("--refresh", action="store_true", help="ignore cached per-session summaries")
    ap.add_argument("--keep-digests", action="store_true", help="write the per-session digest text sent to the model")
    ap.add_argument("--rerender", metavar="DATA_JSON", default="",
                    help="re-render report.md/html from a previous run's data.json (no collection, no model calls)")
    ap.add_argument("--open", action="store_true", help="open the HTML report when done (macOS)")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--version", action="version", version=f"sprint-report {__version__}")
    return ap.parse_args(argv)


# ----------------------------------------------------------------- map step

def chunk_turns(turns: List[Turn], max_chars: int) -> List[List[Turn]]:
    total = sum(len(t.text) for t in turns)
    n = max(1, math.ceil(total / max_chars))
    if n == 1:
        return [turns]
    target = total / n
    chunks: List[List[Turn]] = [[]]
    acc = 0
    for t in turns:
        if acc >= target and len(chunks) < n:
            chunks.append([])
            acc = 0
        chunks[-1].append(t)
        acc += len(t.text)
    return [c for c in chunks if c]


def merge_parts(parts: List[Dict[str, Any]]) -> Dict[str, Any]:
    if len(parts) == 1:
        return parts[0]
    out = dict(parts[0])
    for key in ("work_items", "technical_highlights", "problems", "decisions", "followups", "tickets", "tags"):
        merged: List[Any] = []
        seen = set()
        for p in parts:
            for item in p.get(key) or []:
                k = json.dumps(item, sort_keys=True)
                if k not in seen:
                    seen.add(k)
                    merged.append(item)
        out[key] = merged
    out["outcome"] = " ".join(str(p.get("outcome") or "").strip() for p in parts).strip()
    out["status"] = parts[-1].get("status") or out.get("status")
    out["is_noise"] = all(bool(p.get("is_noise")) for p in parts)
    return out


def summarize_session(cli: ClaudeCLI, cache: JsonCache, d: SessionDigest, tz: dt.tzinfo, model: str,
                      effort: str, refresh: bool, provider: str) -> Tuple[Dict[str, Any], bool]:
    fp = f"{d.fingerprint}|v{PROMPT_VERSION}|{provider}|{model}|{effort}"
    if not refresh:
        cached = cache.get(d.session_id, fp)
        if cached is not None:
            return cached, True
    full = render_digest_text(d, tz)
    parts_out: List[Dict[str, Any]] = []
    if len(full) <= MAX_MAP_CHARS:
        res, _ = cli.run(session_prompt(full), SESSION_SYSTEM, model, SESSION_SCHEMA, effort,
                         label=f"session {d.session_id[:8]}")
        parts_out.append(res)
    else:
        chunks = chunk_turns(d.turns, MAX_MAP_CHARS - 6000)
        for i, ch in enumerate(chunks, 1):
            text = render_digest_text(d, tz, turns=ch, part=(i, len(chunks)))
            res, _ = cli.run(session_prompt(text, (i, len(chunks))), SESSION_SYSTEM, model, SESSION_SCHEMA, effort,
                             label=f"session {d.session_id[:8]} part {i}/{len(chunks)}")
            parts_out.append(res)
    summary = merge_parts([p for p in parts_out if isinstance(p, dict)] or [{}])
    cache.put(d.session_id, fp, summary)
    return summary, False


# --------------------------------------------------------------- aggregation

def build_daily(sessions: List[SessionDigest], repos: List[RepoWork], since_d: dt.date, until_d: dt.date,
                tz: dt.tzinfo) -> List[Dict[str, Any]]:
    prompts: Counter = Counter()
    sess_days: Dict[str, set] = {}
    commits: Counter = Counter()
    for s in sessions:
        for day, n in s.day_prompt_counts.items():
            prompts[day] += n
            sess_days.setdefault(day, set()).add(s.session_id)
    for rw in repos:
        for c in rw.commits:
            t = parse_ts(c.authored_at)
            if t:
                commits[t.astimezone(tz).strftime("%Y-%m-%d")] += 1
    out = []
    d = since_d
    while d <= until_d:
        key = d.isoformat()
        out.append({"date": key, "weekday": d.strftime("%a"), "prompts": prompts.get(key, 0),
                    "commits": commits.get(key, 0), "sessions": len(sess_days.get(key, ()))})
        d += dt.timedelta(days=1)
    return out


def engineer_name(repos: List[RepoWork], override: str) -> str:
    if override:
        return override
    names: Counter = Counter()
    email = ""
    for rw in repos:
        email = email or rw.author
        for c in rw.commits:
            if not rw.author or c.email.lower() == rw.author.lower():
                names[c.author] += 1
    if names:
        name = names.most_common(1)[0][0]
        return f"{name} <{email}>" if email else name
    return email or getpass.getuser()


def done_rule_text(repos: List[RepoWork]) -> str:
    rules = uniq(rw.done_rule for rw in repos if rw.done_rule)
    if not rules:
        return "not determined (no git history collected)"
    if len(rules) == 1:
        return rules[0]
    return "; ".join(f"{rw.name}: {rw.done_rule}" for rw in repos if rw.done_rule)


def build_metrics(sessions: List[SessionDigest], trivial: int, repos: List[RepoWork], plans: List[Dict[str, Any]],
                  tickets: List[str]) -> List[Tuple[str, Any, str]]:
    prompts = sum(s.n_user_prompts for s in sessions)
    asst = sum(s.n_assistant_msgs for s in sessions)
    tools = sum(s.n_tool_uses for s in sessions)
    active = sum(s.active_minutes for s in sessions)
    files = set()
    for s in sessions:
        files.update(s.files_touched.keys())
    subagents = sum(len(s.subagents) for s in sessions)
    commits = [c for rw in repos for c in rw.commits]
    real = [c for c in commits if not c.is_merge]
    ins = sum(c.insertions for c in real)
    dels = sum(c.deletions for c in real)
    cfiles = sum(c.files_changed for c in real)
    prs = uniq(p for rw in repos for p in rw.prs)
    branches = sum(len(rw.branches) for rw in repos)
    by_entry = Counter(ENTRY_LABEL.get(s.entrypoint, s.entrypoint) for s in sessions)
    m: List[Tuple[str, Any, str]] = [
        ("Sessions analysed", len(sessions), "card"),
        ("Prompts to coding agents", prompts, "card"),
        ("Active time (approx.)", human_minutes(active), "card"),
        ("Commits", f"{len(real)}" + (f" +{len(commits) - len(real)} merges" if len(commits) > len(real) else ""), "card"),
        ("Tickets touched", len(tickets), "card"),
        ("PRs merged", len(prs), "card"),
        ("Sessions by source", " · ".join(f"{k} {v}" for k, v in by_entry.most_common()), "row"),
        ("Trivial sessions skipped", trivial, "row"),
        ("Assistant messages / tool calls", f"{asst:,} / {tools:,}", "row"),
        ("Files edited by agents (distinct)", len(files), "row"),
        ("Sub-agent tasks delegated", subagents, "row"),
        ("Lines changed (non-merge commits)", f"+{ins:,} / −{dels:,} across {cfiles:,} file changes", "row"),
        ("Branches with commits", branches, "row"),
        ("Tickets", ", ".join(tickets) if tickets else "—", "row"),
        ("Repositories", ", ".join(rw.name for rw in repos) or "—", "row"),
        ("\u201cDone\u201d means", done_rule_text(repos), "row"),
        ("Plan documents written", len(plans), "row"),
    ]
    if prs:
        m.append(("Pull requests", ", ".join(prs), "row"))
    return m


def build_context_text(since_d: dt.date, until_d: dt.date, tz: dt.tzinfo, engineer: str,
                       metrics: List[Tuple[str, Any, str]], daily: List[Dict[str, Any]], repos: List[RepoWork],
                       plans: List[Dict[str, Any]], sessions: List[SessionDigest],
                       summaries: Dict[str, Dict[str, Any]], ticket_index: Dict[str, Dict[str, int]]) -> str:
    L: List[str] = []
    L.append(f"SPRINT WINDOW: {since_d} -> {until_d} inclusive (timezone {tz}). Engineer: {engineer}.")
    L.append(f"Report generated: {dt.datetime.now(tz).strftime('%Y-%m-%d %H:%M')}")
    L.append("")
    L.append("COMPUTED METRICS (authoritative; do not recompute):")
    for label, value, _ in metrics:
        L.append(f"- {label}: {value}")
    L.append("")
    L.append("DAILY ACTIVITY (date: sessions / prompts / commits):")
    for d in daily:
        if d["prompts"] or d["commits"]:
            L.append(f"- {d['date']} {d['weekday']}: {d['sessions']} / {d['prompts']} / {d['commits']}")
    L.append("")
    if ticket_index:
        L.append("TICKET INDEX (ticket: sessions mentioning it / commits referencing it / merge status from git):")
        for t, v in ticket_index.items():
            L.append(f"- {t}: {v['sessions']} sessions / {v['commits']} commits / {v.get('hint', '')}")
        L.append("")
    L.append("DEFINITION OF DONE (given by the engineer, or the stated fallback): "
             + done_rule_text(repos))
    L.append("Use it exactly as written. Do not infer a different rule from branch names.")
    L.append("")
    L.append("GIT WORK")
    L.append(render_git_text(repos, tz))
    if plans:
        L.append("PLAN DOCUMENTS WRITTEN DURING THE SPRINT (plan mode)")
        for p in plans:
            L.append(f"- {p['title']}  [{p['file']}, modified {p['modified'][:10]}, tickets: {', '.join(p['tickets']) or '-'}]")
            L.append("  " + p["excerpt"][:1500].replace("\n", "\n  "))
        L.append("")
    L.append("SESSION SUMMARIES (chronological; produced by a first-pass model from the full transcripts)")
    for i, s in enumerate(sessions, 1):
        summ = summaries.get(s.session_id)
        if not summ:
            continue
        st = parse_ts(s.started_at)
        L.append("")
        L.append(f"### Session {i}: {fmt_local(st, tz)} | {ENTRY_LABEL.get(s.entrypoint, s.entrypoint)} | {s.project} | "
                 f"branch: {', '.join(s.git_branches) or '-'} | {s.n_user_prompts} prompts, "
                 f"~{human_minutes(s.active_minutes)} active | tickets: {', '.join(s.tickets) or '-'}")
        L.append(json.dumps(summ, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(L)


def fallback_report(reason: str, sessions: List[SessionDigest], summaries: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """If the synthesis call fails, still produce a usable report from the per-session summaries."""
    streams = []
    for s in sessions:
        summ = summaries.get(s.session_id)
        if not summ or summ.get("is_noise"):
            continue
        for w in summ.get("work_items") or []:
            streams.append({"name": w.get("title") or summ.get("title") or s.title,
                            "tickets": w.get("tickets") or [], "status": w.get("status") or "in_progress",
                            "summary": w.get("description") or "", "details": w.get("evidence") or [],
                            "branches": s.git_branches, "prs": []})
    return {"sprint_title": "Sprint report (synthesis step failed)",
            "executive_summary": f"The final synthesis call did not succeed ({reason}). "
                                 "Below is the un-merged list of work items extracted from each session.",
            "headline_achievements": [], "demo_items": [], "work_streams": streams,
            "technical_decisions": [], "challenges": [], "learnings": [], "carry_over": [],
            "risks_and_asks": [], "metrics_commentary": ""}


# --------------------------------------------------------------------- main

HTML_DOC = ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n{head}\n</head>\n"
            "<body>\n{body}\n</body>\n</html>\n")


def write_outputs(run_dir: str, report: Dict[str, Any], ctx: Dict[str, Any], log) -> Tuple[str, str]:
    """Write report.md, report.html (standalone) and report.artifact.html (fragment for Claude's Artifact tool)."""
    md_path = os.path.join(run_dir, "report.md")
    html_path = os.path.join(run_dir, "report.html")
    frag_path = os.path.join(run_dir, "report.artifact.html")
    fragment = render_html(report, ctx)
    # The fragment starts with <title>...</title><style>...</style>; move those into <head> for the file.
    head_end = fragment.find("</style>")
    head, body = (fragment[:head_end + len("</style>")], fragment[head_end + len("</style>"):]) if head_end > 0 else ("", fragment)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(report, ctx))
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(HTML_DOC.format(head=head, body=body))
    with open(frag_path, "w", encoding="utf-8") as fh:
        fh.write(fragment)
    return md_path, html_path


def rerender(args: argparse.Namespace, log) -> int:
    with open(args.rerender, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    ctx = data.get("ctx")
    report = data.get("report")
    if not ctx or not report:
        print("error: data.json lacks 'ctx'/'report' (produced by an older version?)", file=sys.stderr)
        return 2
    if args.title:
        ctx["title_override"] = args.title
    if not ctx.get("short_title"):
        names = ", ".join(r.get("name", "") for r in data.get("repos") or [] if r.get("name")) or "Sprint"
        ctx["short_title"] = f"{names} · {ctx['since']} → {ctx['until']}"
    ctx["metrics"] = [tuple(m) for m in ctx["metrics"]]
    run_dir = os.path.dirname(os.path.abspath(args.rerender))
    md_path, html_path = write_outputs(run_dir, report, ctx, log)
    print(f"Re-rendered\n  Markdown : {md_path}\n  HTML     : {html_path}")
    if args.open and sys.platform == "darwin":
        subprocess.run(["open", html_path], check=False)
    return 0


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    log = (lambda *_: None) if args.quiet else stderr_log
    tz = local_tz()
    if args.rerender:
        return rerender(args, log)
    has_claude = os.path.isdir(os.path.join(args.claude_dir, "projects"))
    has_codex = os.path.isdir(os.path.join(args.codex_dir, "sessions"))
    source = args.source
    if source == "auto":
        source = "all" if has_claude and has_codex else "claude" if has_claude else "codex"
    if args.llm == "auto":
        preferred = "claude" if source == "claude" else "codex"
        other = "codex" if preferred == "claude" else "claude"
        llm_name = preferred if shutil.which(preferred) or not shutil.which(other) else other
    else:
        llm_name = args.llm
    args.map_model = args.map_model or ("haiku" if llm_name == "claude" else "")
    args.reduce_model = args.reduce_model or ("sonnet" if llm_name == "claude" else "")
    for m in ((args.map_model, args.reduce_model) if llm_name == "claude" else ()):
        if any(x in m.lower() for x in EXPENSIVE_MODEL_MARKERS) and not args.allow_expensive:
            print(f"error: {m!r} is an expensive model; defaults are haiku (per session) and sonnet (synthesis). "
                  "Pass --allow-expensive if you really want it.", file=sys.stderr)
            return 2
    if not args.since:
        print("error: --since YYYY-MM-DD is required", file=sys.stderr)
        return 2
    try:
        since_d = parse_date(args.since)
        until_d = parse_date(args.until) if args.until else dt.date.today()
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if until_d < since_d:
        print("error: --until is before --since", file=sys.stderr)
        return 2
    since = day_start(since_d, tz)
    until = day_end_exclusive(until_d, tz)
    suffix = "" if source == "claude" else f"_{source}"
    run_dir = os.path.join(os.path.abspath(args.out), f"sprint_{since_d}_{until_d}{suffix}")
    os.makedirs(run_dir, mode=0o700, exist_ok=True)
    t0 = time.time()

    log(f"sprint-report {__version__} · window {since_d} → {until_d} ({tz}) · output {run_dir}")
    log(f"[1/5] collecting {source} sessions ...")
    sessions_all: List[SessionDigest] = []
    if source in ("claude", "all"):
        sessions_all.extend(collect_sessions(args.claude_dir, since, until, tz, include=args.project,
                                             exclude=args.exclude_project, min_prompts=args.min_prompts,
                                             include_subagents=not args.no_subagents, log=log))
    if source in ("codex", "all"):
        sessions_all.extend(collect_codex_sessions(args.codex_dir, since, until, tz, include=args.project,
                                                   exclude=args.exclude_project, min_prompts=args.min_prompts,
                                                   include_subagents=not args.no_subagents, log=log))
    sessions_all.sort(key=lambda s: s.started_at or "")
    trivial = [s for s in sessions_all if s.is_trivial]
    sessions = [s for s in sessions_all if not s.is_trivial]
    if args.limit:
        sessions = sessions[:args.limit]
    log(f"  {len(sessions)} sessions with real work, {len(trivial)} trivial ones skipped")
    if not sessions:
        print(f"No {source} sessions with activity in that window.", file=sys.stderr)
        return 1

    log("[2/5] collecting git history ...")
    roots = resolve_repos([s.cwd for s in sessions_all], args.repo, log=log)
    repos = collect_git_work(roots, since, until, args.author, integration=args.integration, log=log)
    if args.integration and roots and not any(rw.done_targets for rw in repos):
        names = ", ".join(args.integration)
        print(f"error: none of the branches you gave ({names}) exist in any scanned repo, so every "
              f"stream would read as not done.", file=sys.stderr)
        for rw in repos:
            p = subprocess.run(["git", "-C", rw.root, "for-each-ref", "--sort=-committerdate",
                                "--format=%(refname:short)", "refs/heads"],
                               capture_output=True, text=True, check=False)
            avail = [l.strip() for l in p.stdout.splitlines() if l.strip()][:10]
            print(f"  {rw.name} has: {', '.join(avail)}", file=sys.stderr)
        return 2
    plans = load_plans(since, until, args.claude_dir) if source in ("claude", "all") else []
    log(f"  {len(plans)} plan documents in window")

    # Ticket index across sessions and commits.
    ticket_sessions: Counter = Counter()
    ticket_commits: Counter = Counter()
    for s in sessions:
        for t in s.tickets:
            ticket_sessions[t] += 1
    for rw in repos:
        for c in rw.commits:
            for t in c.tickets:
                ticket_commits[t] += 1
    tickets = sorted(set(ticket_sessions) | set(ticket_commits),
                     key=lambda t: (-(ticket_sessions[t] + ticket_commits[t]), t))
    ticket_index = {}
    for t in tickets:
        branches = [b for rw in repos for b in rw.branches if t in (b.get("tickets") or [])]
        merged = [b["name"] for b in branches if b.get("merged")]
        unmerged = [b["name"] for b in branches if not b.get("merged")]
        if merged:
            hint = "merged (" + ", ".join(merged[:3]) + ")"
        elif ticket_commits[t]:
            hint = "commits exist but branch NOT MERGED" if unmerged else "commits exist"
        else:
            hint = "mentioned in sessions only, no commits"
        ticket_index[t] = {"sessions": ticket_sessions[t], "commits": ticket_commits[t],
                           "branches": [b["name"] for b in branches], "hint": hint}
    metrics = build_metrics(sessions, len(trivial), repos, plans, tickets)
    daily = build_daily(sessions, repos, since_d, until_d, tz)
    engineer = engineer_name(repos, args.name)

    if args.keep_digests or args.no_llm:
        ddir = os.path.join(run_dir, "digests")
        os.makedirs(ddir, mode=0o700, exist_ok=True)
        for s in sessions:
            p = os.path.join(ddir, f"{parse_ts(s.started_at).astimezone(tz).strftime('%Y%m%d-%H%M')}_{s.session_id[:8]}.txt")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(render_digest_text(s, tz))
            os.chmod(p, 0o600)
        log(f"  digests written to {ddir}")

    summaries: Dict[str, Dict[str, Any]] = {}
    report: Dict[str, Any]
    cli = ClaudeCLI(timeout=args.timeout, log=log) if llm_name == "claude" else CodexCLI(timeout=args.timeout, log=log)
    if args.no_llm:
        log("[3/5] map step skipped (--no-llm)")
        log("[4/5] reduce step skipped (--no-llm)")
        report = fallback_report("--no-llm", sessions, summaries)
        report["sprint_title"] = f"Sprint {since_d} → {until_d} (collected data only)"
        report["executive_summary"] = ("Run without --no-llm to generate the narrative. This file lists the collected "
                                       "sessions, commits and metrics only.")
    else:
        if not cli.available():
            print(f"error: `{llm_name}` CLI not found on PATH; install it or pass --no-llm", file=sys.stderr)
            return 3
        cache = JsonCache(os.path.join(os.path.abspath(args.out), ".cache", "sessions"))
        log(f"[3/5] summarizing {len(sessions)} sessions with {args.map_model or 'Codex default'} (parallel {args.parallel}) ...")
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as ex:
            futs = {ex.submit(summarize_session, cli, cache, s, tz, args.map_model, args.map_effort, args.refresh, llm_name): s
                    for s in sessions}
            for fut in as_completed(futs):
                s = futs[fut]
                done += 1
                try:
                    summ, cached = fut.result()
                    summaries[s.session_id] = summ
                    tag = "cached" if cached else (args.map_model or "Codex")
                    log(f"  [{done:2d}/{len(sessions)}] {tag:7s} {s.title[:60]!r}"
                        + ("  (noise)" if summ.get("is_noise") else ""))
                except ClaudeError as e:
                    log(f"  [{done:2d}/{len(sessions)}] FAILED  {s.title[:60]!r}: {e}")
        if not summaries:
            print("error: every per-session summary failed; see messages above", file=sys.stderr)
            return 4
        log(f"[4/5] synthesizing the sprint report with {args.reduce_model or 'Codex default'} ...")
        context = build_context_text(since_d, until_d, tz, engineer, metrics, daily, repos, plans, sessions,
                                     summaries, ticket_index)
        with open(os.path.join(run_dir, "synthesis_input.txt"), "w", encoding="utf-8") as fh:
            fh.write(context)
        os.chmod(os.path.join(run_dir, "synthesis_input.txt"), 0o600)
        try:
            report, usage = cli.run(sprint_prompt(context), SPRINT_SYSTEM, args.reduce_model, SPRINT_SCHEMA,
                                    args.reduce_effort, label="sprint synthesis")
            if not isinstance(report, dict):
                raise ClaudeError("synthesis returned no JSON object")
            log(f"  synthesis took {usage.get('duration_ms', 0) / 1000:.0f}s over {len(context):,} chars of input")
        except ClaudeError as e:
            log(f"  ! synthesis failed: {e}")
            report = fallback_report(str(e), sessions, summaries)

    log("[5/5] rendering ...")
    ctx = {
        "since": since_d.isoformat(), "until": until_d.isoformat(), "tz_name": str(tz), "engineer": engineer,
        "generated_at": dt.datetime.now(tz).strftime("%Y-%m-%d %H:%M"), "title_override": args.title,
        "short_title": f"{', '.join(rw.name for rw in repos) or 'Sprint'} · {since_d} → {until_d}",
        "metrics": metrics, "daily": daily,
        "commits": [{
            "date": fmt_local(parse_ts(c.authored_at), tz)[:10], "short": c.short, "subject": c.subject,
            "refs": c.refs, "insertions": c.insertions, "deletions": c.deletions, "files_changed": c.files_changed,
            "is_merge": c.is_merge, "repo": c.repo, "tickets": c.tickets, "prs": c.prs,
        } for rw in repos for c in sorted(rw.commits, key=lambda c: c.authored_at, reverse=True)],
        "sessions": [{
            "date": fmt_local(parse_ts(s.started_at), tz)[:10], "time": fmt_local(parse_ts(s.started_at), tz)[11:],
            "entrypoint": ENTRY_LABEL.get(s.entrypoint, s.entrypoint), "project": s.project,
            "title": (summaries.get(s.session_id) or {}).get("title") or s.title, "prompts": s.n_user_prompts,
            "active": human_minutes(s.active_minutes),
            "status": (summaries.get(s.session_id) or {}).get("status") or "n/a",
        } for s in sessions],
        "llm": {"provider": llm_name, "map_model": args.map_model or "Codex default", "reduce_model": args.reduce_model or "Codex default", "calls": cli.total_calls,
                "cost_usd": round(cli.total_cost_usd, 4)},
    }
    md_path, html_path = write_outputs(run_dir, report, ctx, log)
    data_path = os.path.join(run_dir, "data.json")
    with open(data_path, "w", encoding="utf-8") as fh:
        json.dump({
            "version": __version__, "window": {"since": since_d.isoformat(), "until": until_d.isoformat(), "tz": str(tz)},
            "engineer": engineer, "metrics": metrics, "daily": daily, "tickets": ticket_index,
            "repos": [rw.to_json() for rw in repos], "plans": plans, "report": report, "ctx": ctx,
            "session_summaries": summaries,
            "sessions": [s.to_json() for s in sessions],
            "trivial_sessions": [{"session_id": s.session_id, "title": s.title, "started_at": s.started_at} for s in trivial],
            "llm": ctx["llm"],
        }, fh, ensure_ascii=False, indent=1, default=str)
    os.chmod(data_path, 0o600)

    elapsed = time.time() - t0
    print(f"\nSprint report ready ({elapsed:.0f}s, {cli.total_calls} LLM calls"
          + (f", est. ${cli.total_cost_usd:.2f}" if cli.total_cost_usd else "") + ")")
    print(f"  Markdown : {md_path}")
    print(f"  HTML     : {html_path}")
    print(f"  Data     : {data_path}")
    if args.open and sys.platform == "darwin":
        subprocess.run(["open", html_path], check=False)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
