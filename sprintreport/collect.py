"""Scan Claude Code session transcripts and build compact per-session digests.

Sources (all local, read-only):
  * ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl      -- every session, regardless of
    whether it was started from the terminal CLI, the Claude desktop app, or the IDE extension
    (they all share this store; the `entrypoint` field tells them apart).
  * ~/.claude/projects/<encoded-cwd>/<session-id>/subagents/  -- delegated sub-agent transcripts.
  * ~/Library/Application Support/Claude/claude-code-sessions/**/local_*.json -- desktop titles.
  * ~/.claude/plans/*.md                                       -- plan-mode documents.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .util import (clean_user_text, find_tickets, fmt_local, human_minutes, parse_ts, redact,
                   short_project, truncate, uniq)

# Truncation budget per turn (chars). Pasted logs in user prompts get cut aggressively.
USER_HEAD, USER_TAIL = 900, 250
ASST_HEAD = 1200
SUBAGENT_TASK_HEAD, SUBAGENT_RESULT_HEAD = 400, 700
MAX_TOOL_ERRORS = 8
MAX_SUBAGENTS = 15
ACTIVE_GAP_MIN = 30  # gaps longer than this are not counted as active time

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# Command classification is token based (first word of each shell segment) so that a `grep jest`
# or `cat electron-builder.cjs` is not mistaken for a build step.
SEG_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")
GIT_OPS = {"commit", "push", "checkout", "switch", "merge", "rebase", "cherry-pick", "tag", "stash",
           "worktree", "revert", "branch", "pull"}
PKG_MANAGERS = {"npm", "yarn", "pnpm", "bun"}
PKG_SUBCMDS = {"test", "run", "start", "ci", "rebuild", "build", "lint", "install"}
NPX_TOOLS = {"jest", "vitest", "playwright", "tsc", "eslint", "electron-builder", "electron-rebuild", "mocha",
             "node-gyp", "prebuild", "cmake-js"}
DIRECT_TOOLS = {"pytest", "jest", "vitest", "playwright", "mocha", "tsc", "eslint", "mvn", "gradle", "gradlew",
                "./gradlew", "make", "cmake", "electron-builder", "xcodebuild", "codesign", "notarytool",
                "productbuild", "pkgbuild", "signtool", "xcrun", "spctl", "stapler", "node-gyp"}
SCRIPT_HINT = re.compile(r"(build|test|release|sign|package|deploy|promot|notar)", re.I)


def classify_command(cmd: str) -> Tuple[List[str], List[str]]:
    """Return (git segments, build/test segments) found in a shell command string."""
    git_segs: List[str] = []
    build_segs: List[str] = []
    for seg in SEG_SPLIT.split(cmd or ""):
        seg = seg.strip()
        if not seg or seg.startswith("#"):
            continue
        if seg.count('"') % 2 or seg.count("'") % 2:
            continue  # the split landed inside a quoted string (e.g. grep -E "a|b"); not a real command
        toks = seg.split()
        while toks and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]) or toks[0] in ("sudo", "time", "exec")):
            toks.pop(0)
        if not toks or toks[0] in ("cd", "echo", "cat", "grep", "sed", "ls", "head", "tail"):
            continue
        head = toks[0]
        sub = toks[1] if len(toks) > 1 else ""
        if head == "git":
            if sub == "-C" and len(toks) > 3:
                sub = toks[3]
            if sub in GIT_OPS:
                git_segs.append(seg)
        elif head in PKG_MANAGERS:
            if sub in PKG_SUBCMDS:
                build_segs.append(seg)
        elif head == "npx":
            if sub in NPX_TOOLS:
                build_segs.append(seg)
        elif head in DIRECT_TOOLS:
            build_segs.append(seg)
        elif head in ("python", "python3") and sub == "-m" and len(toks) > 2 and toks[2] in ("pytest", "unittest", "build"):
            build_segs.append(seg)
        elif head in ("go", "cargo") and sub in ("test", "build", "run", "vet"):
            build_segs.append(seg)
        elif head == "docker" and sub in ("build", "compose", "run"):
            build_segs.append(seg)
        elif head == "az" and sub == "pipelines":
            build_segs.append(seg)
        elif head == "gh" and sub in ("pr", "run", "workflow"):
            build_segs.append(seg)
        elif (head.startswith("./") or head.endswith((".sh", ".ps1"))) and SCRIPT_HINT.search(head):
            build_segs.append(seg)
    return git_segs, build_segs


@dataclass
class Turn:
    ts: str          # ISO in UTC
    role: str        # 'U' or 'A'
    text: str


@dataclass
class SubagentDigest:
    agent_id: str
    agent_type: str
    description: str
    task: str
    result: str


@dataclass
class SessionDigest:
    session_id: str
    path: str
    project_dir: str
    cwd: str
    project: str
    title: str
    entrypoint: str
    version: str
    git_branches: List[str]
    models: Dict[str, int]
    started_at: Optional[str]      # first in-range message
    ended_at: Optional[str]        # last in-range message
    first_seen: Optional[str]      # first message overall (may be before window)
    last_seen: Optional[str]
    n_user_prompts: int
    n_assistant_msgs: int
    n_tool_uses: int
    active_minutes: float
    tool_counts: Dict[str, int]
    files_touched: Dict[str, int]
    files_read: int
    git_commands: List[str]
    build_test_commands: List[str]
    skills_used: List[str]
    artifacts: List[str]
    agents_spawned: List[str]
    questions_asked: int
    plan_mode: bool
    tool_errors: List[str]
    tickets: List[str]
    subagents: List[SubagentDigest]
    turns: List[Turn]
    fingerprint: str = ""
    day_prompt_counts: Dict[str, int] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    def digest_chars(self) -> int:
        return sum(len(t.text) for t in self.turns)

    @property
    def is_trivial(self) -> bool:
        """No assistant output, or nothing beyond a couple of slash commands: not worth an LLM call."""
        return self.n_assistant_msgs == 0 or self.digest_chars() < 150


# --------------------------------------------------------------------- helpers

def _text_of_user_content(content: Any) -> Tuple[str, List[Dict[str, Any]]]:
    """Return (human text, tool_result blocks) for a user message's content."""
    if isinstance(content, str):
        return content, []
    texts: List[str] = []
    results: List[Dict[str, Any]] = []
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                texts.append(b.get("text") or "")
            elif bt == "tool_result":
                results.append(b)
            elif bt == "image":
                texts.append("[image attached]")
    return "\n".join(texts), results


def _tool_result_text(block: Dict[str, Any]) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    return ""


def _iter_jsonl(path: str) -> Iterator[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(o, dict):
                yield o


def load_desktop_titles() -> Dict[str, Dict[str, Any]]:
    """Map CLI session id -> desktop metadata (title, model, archived flag)."""
    base = os.path.expanduser("~/Library/Application Support/Claude/claude-code-sessions")
    out: Dict[str, Dict[str, Any]] = {}
    if not os.path.isdir(base):
        return out
    for f in glob.glob(os.path.join(base, "*", "*", "local_*.json")):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                o = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        sid = o.get("cliSessionId")
        if not sid:
            continue
        cur = out.get(sid)
        rec = {
            "title": o.get("title") or "",
            "title_source": o.get("titleSource") or "",
            "model": o.get("model") or "",
            "archived": bool(o.get("isArchived")),
            "cwd": o.get("cwd") or "",
        }
        # A user-provided title beats an auto title.
        if cur is None or (rec["title_source"] == "user" and cur.get("title_source") != "user"):
            out[sid] = rec
    return out


def load_plans(since: dt.datetime, until: dt.datetime, claude_dir: str, excerpt_chars: int = 2500) -> List[Dict[str, Any]]:
    plans_dir = os.path.join(claude_dir, "plans")
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(plans_dir):
        return out
    for f in sorted(glob.glob(os.path.join(plans_dir, "*.md"))):
        try:
            st = os.stat(f)
            mtime = dt.datetime.fromtimestamp(st.st_mtime, tz=dt.timezone.utc)
            if not (since <= mtime < until):
                continue
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                body = fh.read()
        except OSError:
            continue
        title = ""
        for line in body.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        out.append({
            "file": os.path.basename(f),
            "modified": mtime.isoformat(),
            "title": title or os.path.basename(f),
            "tickets": find_tickets(title, body[:5000]),
            "excerpt": redact(truncate(body, excerpt_chars)),
        })
    return out


# ------------------------------------------------------------- session parsing

def parse_subagents(session_dir: str) -> List[SubagentDigest]:
    sub_dir = os.path.join(session_dir, "subagents")
    out: List[SubagentDigest] = []
    if not os.path.isdir(sub_dir):
        return out
    for jf in sorted(glob.glob(os.path.join(sub_dir, "agent-*.jsonl")))[:MAX_SUBAGENTS]:
        agent_id = os.path.basename(jf)[len("agent-"):-len(".jsonl")]
        meta: Dict[str, Any] = {}
        mf = jf[:-len(".jsonl")] + ".meta.json"
        if os.path.exists(mf):
            try:
                with open(mf, "r", encoding="utf-8") as fh:
                    meta = json.load(fh)
            except (OSError, json.JSONDecodeError):
                meta = {}
        task = ""
        last_asst = ""
        for o in _iter_jsonl(jf):
            t = o.get("type")
            m = o.get("message") or {}
            if t == "user" and not task:
                txt, _ = _text_of_user_content(m.get("content"))
                task = clean_user_text(txt)
            elif t == "assistant":
                c = m.get("content")
                if isinstance(c, list):
                    txt = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
                    if txt.strip():
                        last_asst = txt
        out.append(SubagentDigest(
            agent_id=agent_id,
            agent_type=str(meta.get("agentType") or ""),
            description=str(meta.get("description") or ""),
            task=redact(truncate(task, SUBAGENT_TASK_HEAD)),
            result=redact(truncate(last_asst, SUBAGENT_RESULT_HEAD)),
        ))
    return out


def parse_session(path: str, project_dir: str, since: dt.datetime, until: dt.datetime,
                  tz: dt.tzinfo, desktop_titles: Dict[str, Dict[str, Any]],
                  include_subagents: bool = True) -> Optional[SessionDigest]:
    """Parse one session file. Returns None if it has no human prompt inside the window."""
    session_id = os.path.basename(path)[:-len(".jsonl")]
    cwd = ""
    entrypoint = ""
    version = ""
    branches: List[str] = []
    models: Counter = Counter()
    custom_title = ""
    first_seen: Optional[dt.datetime] = None
    last_seen: Optional[dt.datetime] = None
    started: Optional[dt.datetime] = None
    ended: Optional[dt.datetime] = None
    turns: List[Turn] = []
    n_user = n_asst = n_tools = 0
    files_read = 0
    tool_counts: Counter = Counter()
    files_touched: Counter = Counter()
    git_cmds: List[str] = []
    build_cmds: List[str] = []
    skills: List[str] = []
    artifacts: List[str] = []
    agents: List[str] = []
    tool_errors: List[str] = []
    questions = 0
    plan_mode = False
    ticket_text: List[str] = []
    day_counts: Counter = Counter()
    active_min = 0.0
    prev_ts: Optional[dt.datetime] = None
    pending_asst: List[str] = []
    pending_asst_ts: Optional[dt.datetime] = None

    def flush_asst() -> None:
        nonlocal pending_asst, pending_asst_ts
        if pending_asst and pending_asst_ts:
            text = "\n".join(pending_asst).strip()
            if text:
                turns.append(Turn(pending_asst_ts.isoformat(), "A", redact(truncate(text, ASST_HEAD))))
        pending_asst = []
        pending_asst_ts = None

    for o in _iter_jsonl(path):
        t = o.get("type")
        if t == "custom-title":
            custom_title = o.get("customTitle") or custom_title
            continue
        if t not in ("user", "assistant"):
            continue
        if o.get("isSidechain"):
            continue  # legacy in-file sub-agent traffic; handled via subagents/ dir
        cwd = cwd or o.get("cwd") or ""
        entrypoint = entrypoint or o.get("entrypoint") or ""
        version = version or o.get("version") or ""
        gb = o.get("gitBranch")
        if gb and gb not in branches:
            branches.append(gb)
        ts = parse_ts(o.get("timestamp"))
        if ts:
            first_seen = first_seen or ts
            last_seen = ts
        if not ts or not (since <= ts < until):
            continue
        m = o.get("message") or {}
        content = m.get("content")

        # Active-time heuristic: sum gaps between consecutive in-window messages < 30 min.
        if prev_ts is not None:
            gap = (ts - prev_ts).total_seconds() / 60.0
            if 0 <= gap <= ACTIVE_GAP_MIN:
                active_min += gap
        prev_ts = ts

        if t == "user":
            text, results = _text_of_user_content(content)
            for r in results:
                if r.get("is_error") and len(tool_errors) < MAX_TOOL_ERRORS:
                    err = _tool_result_text(r).strip()
                    if err:
                        tool_errors.append(redact(truncate(err, 220)))
            text = clean_user_text(text)
            if not text:
                continue
            flush_asst()
            n_user += 1
            day_counts[ts.astimezone(tz).strftime("%Y-%m-%d")] += 1
            started = started or ts
            ended = ts
            ticket_text.append(text[:4000])
            turns.append(Turn(ts.isoformat(), "U", redact(truncate(text, USER_HEAD, USER_TAIL))))
        else:  # assistant
            if m.get("model"):
                models[m["model"]] += 1
            if not isinstance(content, list):
                continue
            started = started or ts
            ended = ts
            texts: List[str] = []
            has_text = False
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    if (b.get("text") or "").strip():
                        texts.append(b["text"])
                        has_text = True
                elif bt == "tool_use":
                    n_tools += 1
                    name = str(b.get("name") or "?")
                    inp = b.get("input") or {}
                    if not isinstance(inp, dict):
                        inp = {}
                    key = name.split("__")[1] if name.startswith("mcp__") and name.count("__") >= 2 else name
                    tool_counts["mcp:" + key if name.startswith("mcp__") else key] += 1
                    if name in EDIT_TOOLS:
                        fp = inp.get("file_path") or inp.get("notebook_path")
                        if fp:
                            files_touched[str(fp)] += 1
                    elif name == "Read":
                        files_read += 1
                    elif name == "Bash":
                        g_segs, b_segs = classify_command(str(inp.get("command") or ""))
                        git_cmds.extend(redact(truncate(x, 200)) for x in g_segs)
                        build_cmds.extend(redact(truncate(x, 160)) for x in b_segs)
                    elif name == "Agent":
                        d = inp.get("description") or inp.get("prompt") or ""
                        agents.append(redact(truncate(str(d), 120)))
                    elif name == "Skill":
                        skills.append(str(inp.get("skill") or ""))
                    elif name == "Artifact":
                        if not inp.get("action") or inp.get("action") == "publish":
                            artifacts.append(str(inp.get("title") or inp.get("file_path") or "artifact"))
                    elif name in ("EnterPlanMode", "ExitPlanMode"):
                        plan_mode = True
                    elif name == "AskUserQuestion":
                        questions += 1
            if has_text:
                n_asst += 1
                if pending_asst_ts is None:
                    pending_asst_ts = ts
                pending_asst.extend(texts)
    flush_asst()

    if n_user == 0:
        return None

    desk = desktop_titles.get(session_id) or {}
    title = desk.get("title") or custom_title or ""
    if not title:
        # Fall back to the first human prompt that is not a slash command, single line.
        user_turns = [tr for tr in turns if tr.role == "U"]
        pick = next((tr for tr in user_turns if not tr.text.lstrip().startswith("/")), None)
        pick = pick or (user_turns[0] if user_turns else None)
        if pick:
            title = re.sub(r"\s+", " ", pick.text)[:80]
    subagents = parse_subagents(path[:-len(".jsonl")]) if include_subagents else []
    tickets = find_tickets(title, " ".join(branches), " ".join(ticket_text), " ".join(git_cmds))
    try:
        st = os.stat(path)
        fp = f"{st.st_size}:{int(st.st_mtime)}:{n_user}:{n_asst}"
    except OSError:
        fp = f"?:{n_user}:{n_asst}"

    return SessionDigest(
        session_id=session_id,
        path=path,
        project_dir=project_dir,
        cwd=cwd or desk.get("cwd") or "",
        project=short_project(cwd or desk.get("cwd") or ""),
        title=title,
        entrypoint=entrypoint or "unknown",
        version=version,
        git_branches=branches,
        models=dict(models),
        started_at=started.isoformat() if started else None,
        ended_at=ended.isoformat() if ended else None,
        first_seen=first_seen.isoformat() if first_seen else None,
        last_seen=last_seen.isoformat() if last_seen else None,
        n_user_prompts=n_user,
        n_assistant_msgs=n_asst,
        n_tool_uses=n_tools,
        active_minutes=round(active_min, 1),
        tool_counts=dict(tool_counts.most_common()),
        files_touched=dict(files_touched.most_common()),
        files_read=files_read,
        git_commands=uniq(git_cmds)[:40],
        build_test_commands=uniq(build_cmds)[:25],
        skills_used=uniq(skills),
        artifacts=uniq(artifacts),
        agents_spawned=uniq(agents)[:20],
        questions_asked=questions,
        plan_mode=plan_mode,
        tool_errors=tool_errors,
        tickets=tickets,
        subagents=subagents,
        turns=turns,
        fingerprint=fp,
        day_prompt_counts=dict(sorted(day_counts.items())),
    )


def iter_session_files(claude_dir: str, since: dt.datetime) -> Iterator[Tuple[str, str]]:
    """Yield (project_dir_name, path) for session files possibly touched after `since`."""
    projects = os.path.join(claude_dir, "projects")
    if not os.path.isdir(projects):
        return
    since_epoch = since.timestamp()
    for proj in sorted(os.listdir(projects)):
        pdir = os.path.join(projects, proj)
        if not os.path.isdir(pdir):
            continue
        for f in sorted(glob.glob(os.path.join(pdir, "*.jsonl"))):
            try:
                if os.stat(f).st_mtime < since_epoch:
                    continue  # last write predates the window: nothing in range
            except OSError:
                continue
            yield proj, f


def collect_sessions(claude_dir: str, since: dt.datetime, until: dt.datetime, tz: dt.tzinfo,
                     include: Optional[List[str]] = None, exclude: Optional[List[str]] = None,
                     min_prompts: int = 1, include_subagents: bool = True,
                     log=lambda *_: None) -> List[SessionDigest]:
    desktop = load_desktop_titles()
    out: List[SessionDigest] = []
    scanned = 0
    for proj, f in iter_session_files(claude_dir, since):
        scanned += 1
        try:
            d = parse_session(f, proj, since, until, tz, desktop, include_subagents=include_subagents)
        except Exception as e:  # keep going; one corrupt file must not kill the report
            log(f"  ! failed to parse {f}: {e}")
            continue
        if d is None or d.n_user_prompts < min_prompts:
            continue
        hay = (d.cwd + " " + d.project).lower()
        if include and not any(s.lower() in hay for s in include):
            continue
        if exclude and any(s.lower() in hay for s in exclude):
            continue
        out.append(d)
    log(f"  scanned {scanned} candidate session files, kept {len(out)}")
    out.sort(key=lambda d: d.started_at or "")
    return out


# ---------------------------------------------------------------- digest text

def render_digest_text(d: SessionDigest, tz: dt.tzinfo, turns: Optional[List[Turn]] = None,
                       part: Optional[Tuple[int, int]] = None) -> str:
    """Plain-text digest of a session, the input for the per-session summarization call."""
    turns = d.turns if turns is None else turns
    lines: List[str] = []
    lines.append("SESSION METADATA")
    lines.append(f"- session id: {d.session_id}")
    lines.append(f"- title: {d.title}")
    lines.append(f"- project directory: {d.cwd}  (short: {d.project})")
    lines.append(f"- git branches seen: {', '.join(d.git_branches) or '-'}")
    lines.append(f"- launched from: {d.entrypoint}   claude version: {d.version or '?'}")
    s = parse_ts(d.started_at)
    e = parse_ts(d.ended_at)
    lines.append(f"- in-window activity: {fmt_local(s, tz)} -> {fmt_local(e, tz)} local time; "
                 f"active ~{human_minutes(d.active_minutes)}")
    if d.first_seen and s and parse_ts(d.first_seen) and parse_ts(d.first_seen) < s - dt.timedelta(minutes=1):
        lines.append(f"- NOTE: session began earlier ({fmt_local(parse_ts(d.first_seen), tz)}); "
                     f"only messages inside the reporting window are shown")
    lines.append(f"- counts: {d.n_user_prompts} user prompts, {d.n_assistant_msgs} assistant messages, "
                 f"{d.n_tool_uses} tool calls, {d.files_read} file reads")
    if d.models:
        lines.append(f"- models: {', '.join(f'{k} x{v}' for k, v in d.models.items())}")
    if d.tickets:
        lines.append(f"- ticket ids mentioned: {', '.join(d.tickets)}")
    if d.tool_counts:
        top = ", ".join(f"{k} x{v}" for k, v in list(d.tool_counts.items())[:12])
        lines.append(f"- tools used: {top}")
    if d.files_touched:
        lines.append("- files edited/written (count):")
        for fp, n in list(d.files_touched.items())[:30]:
            lines.append(f"    {n:3d}  {fp}")
        if len(d.files_touched) > 30:
            lines.append(f"    ... and {len(d.files_touched) - 30} more")
    if d.git_commands:
        lines.append("- git commands run by the assistant:")
        for c in d.git_commands[:25]:
            lines.append(f"    $ {c}")
    if d.build_test_commands:
        lines.append("- build/test commands run:")
        for c in d.build_test_commands[:15]:
            lines.append(f"    $ {c}")
    if d.skills_used:
        lines.append(f"- skills invoked: {', '.join(d.skills_used)}")
    if d.artifacts:
        lines.append(f"- artifacts published: {', '.join(d.artifacts)}")
    if d.agents_spawned:
        lines.append(f"- sub-agents spawned: {'; '.join(d.agents_spawned[:10])}")
    if d.plan_mode:
        lines.append("- plan mode was used in this session")
    if d.questions_asked:
        lines.append(f"- clarifying questions asked to the user: {d.questions_asked}")
    if d.tool_errors:
        lines.append("- sample of tool errors encountered:")
        for err in d.tool_errors:
            lines.append("    ! " + err.replace("\n", " ")[:220])
    if d.subagents:
        lines.append("")
        lines.append("DELEGATED SUB-AGENT TASKS")
        for sa in d.subagents:
            lines.append(f"- [{sa.agent_type or 'agent'}] {sa.description or '(no description)'}")
            if sa.task:
                lines.append("    task: " + sa.task.replace("\n", " "))
            if sa.result:
                lines.append("    result: " + sa.result.replace("\n", " "))
    lines.append("")
    hdr = "CONVERSATION (U = user, A = assistant; long messages truncated)"
    if part:
        hdr += f"  -- PART {part[0]} of {part[1]}"
    lines.append(hdr)
    for tr in turns:
        ts = parse_ts(tr.ts)
        lines.append(f"[{fmt_local(ts, tz, with_date=False)}] {tr.role}: {tr.text}")
    return "\n".join(lines)
